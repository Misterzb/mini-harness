"""Streamlit 演示界面：聊天 + 轨迹面板 + 退款审批卡片。"""
import json
import os
import sys
from pathlib import Path

import httpx
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

st.set_page_config(page_title="MiniHarness · Agent Runtime Demo", layout="wide")


def _password_gate():
    """公网口令闸门：设置 MINI_HARNESS_DEMO_PASSWORD 才启用，本地开发留空免口令。
    口令只挡 UI 入口；FastAPI 只绑 127.0.0.1，公网无法绕过 UI 直接刷 API。"""
    expected = os.environ.get("MINI_HARNESS_DEMO_PASSWORD", "").strip()
    if not expected or st.session_state.get("_auth_ok"):
        return
    st.title("MiniHarness · 访问验证")
    with st.form("auth_form"):
        pwd = st.text_input("访问口令", type="password")
        if st.form_submit_button("进入演示"):
            if pwd == expected:
                st.session_state["_auth_ok"] = True
                st.rerun()
            else:
                st.error("口令错误，请重试")
    st.stop()


_password_gate()

st.title("MiniHarness · 最小 Agent Runtime")
st.caption("自研 Harness 内核（Loop / 编排 / 记忆 / 工具 / 预算 / 事件溯源）· 查数 / 诊断 / 退款HITL / 安全拒绝")

API = st.sidebar.text_input("API 地址", os.environ.get("MINI_HARNESS_API_BASE", "http://127.0.0.1:8000"))
st.sidebar.markdown("---")
st.sidebar.markdown("**演示问题（点击填入）**")
examples = {
    "① 查数：GMV": "上个月GMV是多少",
    "② 诊断：GMV下降": "分析GMV下降的原因",
    "③ 退款HITL": "给订单O000123退款300元",
    "④ 安全拒绝": "净利润率是多少",
    "⑤ 多轮记忆①": "淘宝平台GMV是多少",
    "⑤ 多轮记忆②": "换成天猫平台的订单量呢",
    "⑥ RAG知识检索": "什么是GMV口径",
}
for label, text in examples.items():
    if st.sidebar.button(label, use_container_width=True):
        st.session_state["pending_question"] = text

if "messages" not in st.session_state:
    st.session_state.messages = []
if "pending" not in st.session_state:
    st.session_state.pending = None
if "session_id" not in st.session_state:
    import uuid
    st.session_state.session_id = uuid.uuid4().hex[:12]

col_chat, col_trace = st.columns([1, 1])

with col_chat:
    st.subheader("对话")
    for m in st.session_state.messages:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

    question = st.session_state.pop("pending_question", None) or st.chat_input("输入问题")
    if question and st.session_state.get("_just_sent") != question:
        st.session_state["_just_sent"] = question
        st.session_state.messages.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)
        events = []
        answer = ""
        try:
            with httpx.stream("POST", f"{API}/run", json={
                "question": question, "session_id": st.session_state.session_id
            }, timeout=120) as resp:
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        data = json.loads(line[6:])
                        events.append(data)
                        if data.get("event") == "human_gate":
                            st.session_state.pending = data
                        if data.get("event") == "done":
                            answer = summarize(data["result"])
        except Exception as e:
            answer = f"请求失败: {e}"
        st.session_state.messages.append({"role": "assistant", "content": answer or "见右侧轨迹"})
        st.session_state["events"] = events
        with st.chat_message("assistant"):
            st.markdown(answer or "见右侧轨迹")
        st.rerun()

with col_trace:
    st.subheader("执行轨迹（事件溯源）")
    events = st.session_state.get("events", [])
    for ev in events:
        if ev.get("event") in ("done", "clarify"):
            continue
        icon = {"human_gate": "⏸", "human_decision": "✋", "error": "⛔", "final": "✅"}.get(ev.get("event"), "•")
        label = f"{icon} step{ev.get('step', '?')} · {ev.get('decision', ev.get('event', ''))}"
        with st.expander(label, expanded=(ev.get("event") in ("human_gate", "error"))):
            st.json({k: ev[k] for k in ("event", "tool", "decision", "verdict", "tokens") if k in ev})
            if ev.get("result") is not None:
                st.json(ev["result"])

    pending = st.session_state.get("pending")
    if pending and pending.get("event") == "human_gate":
        prop = pending["result"]["proposal"]
        st.warning(f"⏸ 等待人工审批：订单 {prop['order_id']} · 金额 ¥{prop['amount']} · 风险 {prop['risk_level']}")
        with st.form("approve_form"):
            approver = st.text_input("审批人", "demo-reviewer")
            comment = st.text_input("审批备注")
            c1, c2 = st.columns(2)
            approve = c1.form_submit_button("✅ 批准")
            reject = c2.form_submit_button("❌ 拒绝")
        if approve or reject:
            decision = "approve" if approve else "reject"
            with httpx.Client(timeout=60) as client:
                r = client.post(f"{API}/approve", json={
                    "session_id": st.session_state.session_id,
                    "task_id": pending.get("task_id", ""),
                    "proposal_id": prop["proposal_id"], "approver": approver,
                    "decision": decision, "comment": comment})
                res = r.json()
            st.session_state.messages.append({"role": "assistant",
                "content": f"人工已{decision}：{res.get('status')}"})
            st.session_state.pending = None
            st.session_state["events"].extend(res.get("events", []))
            st.rerun()


def _tool_rows(data: dict) -> tuple[bool, any]:
    """done.result.data 历史上带过 ToolResult 包装层 {ok,data,error,meta}；
    后端已统一解包，这里再兜一层，兼容任何历史/旁路调用。"""
    if isinstance(data, dict) and "ok" in data and "data" in data \
            and "meta" in data and "error" in data:
        return True, data["data"]
    return False, data


def summarize(result: dict) -> str:
    if not result:
        return ""
    status = result.get("status")
    data = result.get("data")
    # 终态类：reason/question 在顶层，data 常为空——别显示 "None"
    if status == "clarify":
        q = result.get("question") or (data.get("question") if isinstance(data, dict) else None)
        return "我需要确认一下您的需求：" + str(q or "请补充要查询的指标或操作")
    if status in ("refuse", "human_rejected", "execute_failed", "proposal_mismatch", "need_comment"):
        reason = result.get("reason")
        if not reason and isinstance(data, dict):
            reason = data.get("reason") or data.get("error")
        if not reason:
            reason = "命中安全边界或执行失败，详见右侧执行轨迹"
        return f"⛔ 已拦截/拒绝：{reason}"
    if status == "await_human":
        return "⏸ 已生成退款提案，等待人工审批（见右侧审批卡片）"

    _, data = _tool_rows(data)
    # 查数：行集 list[dict]。单行单值显示成金额，多行保留表格，别甩裸 Python 字面量。
    if isinstance(data, list) and data:
        if len(data) == 1 and set(data[0].keys()) == {"value"}:
            return f"**结果**：¥{data[0]['value']:,.2f}"
        rows = "\n".join("- " + " · ".join(f"{k}={v}" for k, v in r.items()) for r in data[:20])
        more = f"\n\n（共 {len(data)} 行，仅显示前 20 行）" if len(data) > 20 else ""
        return f"**结果（{len(data)} 行）**\n{rows}{more}"
    if isinstance(data, dict):
        # 诊断：真实字段 prev_gmv/cur_gmv/worst_platform/funnel/narrative
        if "worst_platform" in data and "cur_gmv" in data:
            lines = [f"**诊断：{data['cur_month']} vs {data['prev_month']}（按平台 GMV）**"]
            for k in sorted(data.get("cur_gmv", {}), key=lambda x: data["cur_gmv"][x]):
                pct = data.get("delta_pct", {}).get(k, 0) * 100
                lines.append(f"- {k}: ¥{data['cur_gmv'][k]:,.2f} ({pct:+.1f}%)")
            lines.append(f"\n跌幅最大：**{data['worst_platform']}**")
            for f in data.get("funnel", []):
                rate = f.get("cart_rate")
                rate_s = "不可计算" if rate is None else f"{rate:.3f}"
                lines.append(f"- 漏斗({f['platform']}): 浏览{f['views']} / 加购{f['carts']} / 加购率{rate_s}")
            lines.append("\n" + str(data.get("narrative", "")))
            return "\n".join(lines)
        if "refund_id" in data:
            return f"✅ 退款已执行：{data['refund_id']}（人工批准，流水写入独立 ledger 库）"
        if "citations" in data and "answer" in data:
            lines = ["**知识检索（向量召回 · 引证回答）**", data["answer"]]
            lines.append("\n出处：" + "、".join(f"{c['id']} {c['title']}({c['score']})"
                                            for c in data["citations"]))
            return "\n\n".join(lines)
        payload = json.dumps(data, ensure_ascii=False, default=str)
        return f"```json\n{payload[:1600]}\n```"
    return str(data)[:800] if data else f"状态：{status}"
