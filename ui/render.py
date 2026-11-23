"""Streamlit 聊天区渲染函数：独立模块，确保定义先于 UI 脚本中的调用。

历史上这两个函数写在 streamlit_app.py 末尾，而调用在第 90 行——Streamlit 每次
交互从上到下重跑脚本，用户输入时执行到调用处函数尚未定义，线上报
`name 'summarize' is not defined`。抽成模块后 import 即定义，根治该类顺序 bug。
"""
import json


def tool_rows(data: dict) -> tuple[bool, any]:
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

    _, data = tool_rows(data)
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
