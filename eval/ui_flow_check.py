"""从 UI 视角走完整链路：UI -> POST /run (SSE) -> Runtime -> 前端渲染。
复刻 streamlit_app.py 的两段前端逻辑（summarize + 轨迹面板），逐环节检查对齐。
用 TestClient 在进程内走真实 HTTP 请求；默认走 .env 的真实百炼（不 mock）。
如需离线自测：MINI_HARNESS_MOCK=1 python eval/ui_flow_check.py
"""
import ast
import json
import os
import sys
from pathlib import Path

if os.getenv("MINI_HARNESS_MOCK") == "1":
    os.environ["DASHSCOPE_API_KEY"] = ""
os.environ.setdefault("MINI_HARNESS_RUNTIME", "loop")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import duckdb
from fastapi.testclient import TestClient
from app.main import app

# --- 提取 streamlit_app.py 里真实的渲染函数（不触发 UI 顶层代码）---
# summarize 依赖 _tool_rows 辅助函数，必须一并提取，否则 NameError。
src = (ROOT / "ui" / "streamlit_app.py").read_text(encoding="utf-8")
tree = ast.parse(src)
want = {"_tool_rows", "summarize"}
nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in want]
assert {n.name for n in nodes} == want, f"未提取到全部渲染函数: {[n.name for n in nodes]}"
ns = {"json": json}
exec(compile(ast.Module(body=nodes, type_ignores=[]), "<render>", "exec"), ns)
summarize = ns["summarize"]

QUESTIONS = [
    ("① 查数GMV", "上个月GMV是多少"),
    ("② 诊断GMV下降", "分析GMV下降的原因"),
    ("③ 退款HITL", "给订单O000123退款300元"),
    ("④ 安全拒绝", "净利润率是多少"),
    ("⑤ 多轮记忆-第1轮", "淘宝平台GMV是多少"),
    ("⑤ 多轮记忆-第2轮", "换成天猫平台的订单量呢"),
    ("⑥ RAG知识检索", "什么是GMV口径"),
]

TRACE_SKIP = ("done", "clarify")


def render_trace_panel(events):
    """复刻 UI 轨迹面板：遍历 events，跳过 done/clarify，每行显示 step/decision/result。"""
    lines = []
    for ev in events:
        if ev.get("event") in TRACE_SKIP:
            continue
        label = f"step{ev.get('step', '?')}·{ev.get('decision', ev.get('event',''))}"
        has_result = "result" in ev and ev.get("result") is not None
        lines.append(f"    [{ev.get('event')}] {label}"
                     f" tool={ev.get('tool')} verdict={(ev.get('verdict') or {}).get('ok')}"
                     f" result={'YES' if has_result else 'none'}")
    return lines


def run_sse(client, question, session_id):
    """完全复刻 UI：httpx.stream POST /run，逐行读 data: ，收集 events。"""
    events = []
    answer = ""
    with client.stream("POST", "/run", json={"question": question, "session_id": session_id}) as resp:
        for line in resp.iter_lines():
            if line.startswith("data: "):
                data = json.loads(line[6:])
                events.append(data)
                if data.get("event") == "done":
                    answer = summarize(data["result"])
    return events, answer


def main():
    print("=" * 78)
    mode = "mock" if os.getenv("MINI_HARNESS_MOCK") == "1" else "真实百炼"
    print(f"MiniHarness UI 全流程对齐检查（{mode}，TestClient 走真实 HTTP 链）")
    print("=" * 78)

    with TestClient(app) as client:
        # 查一个真实存在的订单用于退款
        shop = duckdb.connect(str(ROOT / "data" / "shop.db"), read_only=True)
        rows = shop.execute("SELECT order_id FROM orders LIMIT 3").fetchall()
        shop.close()
        real_order = rows[0][0] if rows else "O000123"
        print(f"[数据] 用于退款的真实订单: {real_order}\n")

        sid = "ui-check-session"

        for idx, (label, q) in enumerate(QUESTIONS):
            # 退款问题用真实订单号替换
            q_use = q.replace("O000123", real_order) if "退款" in q else q
            print(f"\n{'='*78}")
            print(f"【{label}】 问: {q_use}")
            print("-" * 78)
            try:
                events, answer = run_sse(client, q_use, sid)
            except Exception as e:
                print(f"  !! 请求异常: {type(e).__name__}: {e}")
                continue

            # 找到 done 事件的原始 result，分析 data 结构
            done = next((e for e in events if e.get("event") == "done"), None)
            result = done.get("result", {}) if done else {}
            print(f"  status      : {result.get('status')}")
            data = result.get("data")
            if isinstance(data, dict):
                print(f"  result.data keys: {list(data.keys())}")
            else:
                print(f"  result.data : {repr(data)[:120]}")

            print(f"  聊天区渲染  :")
            for ln in answer.splitlines():
                print(f"    | {ln[:110]}")

            print(f"  轨迹面板({len([e for e in events if e.get('event') not in TRACE_SKIP])} 步可见):")
            for ln in render_trace_panel(events):
                print(ln)

            # 退款：检查 human_gate 事件字段是否够 /approve 使用
            if idx == 2:
                hg = next((e for e in events if e.get("event") == "human_gate"), None)
                if hg:
                    prop = (hg.get("result") or {}).get("proposal", {})
                    print(f"  [human_gate] 事件字段: {list(hg.keys())}")
                    print(f"  [human_gate] task_id 在事件中: {'task_id' in hg}")
                    print(f"  [human_gate] proposal: id={prop.get('proposal_id')} "
                          f"order={prop.get('order_id')} amount={prop.get('amount')} risk={prop.get('risk_level')}")
                    # 复刻 UI 的 /approve
                    approve_payload = {
                        "session_id": sid,
                        "task_id": hg.get("task_id", ""),
                        "proposal_id": prop.get("proposal_id", ""),
                        "approver": "demo-reviewer",
                        "decision": "approve",
                        "comment": "UI 全流程测试批准",
                    }
                    print(f"  [approve] 提交 task_id={approve_payload['task_id']!r}")
                    try:
                        r = client.post("/approve", json=approve_payload)
                        print(f"  [approve] HTTP {r.status_code}")
                        body = r.json()
                        print(f"  [approve] status={body.get('status')}")
                        print(f"  [approve] 新增事件:")
                        for ev in body.get("events", []):
                            print(f"      [{ev.get('event')}] step{ev.get('step')} "
                                  f"{ev.get('decision')} result={str(ev.get('result'))[:90]}")
                        print(f"  [approve] 渲染答案: {summarize(body.get('result', {}))[:120]}")
                    except Exception as e:
                        print(f"  [approve] !! 异常: {type(e).__name__}: {e}")
                else:
                    print("  [human_gate] !! 未收到 human_gate 事件（退款未挂起）")

    print(f"\n{'='*78}")
    print("检查结束")
    print("=" * 78)


if __name__ == "__main__":
    main()
