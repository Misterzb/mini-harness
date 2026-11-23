"""Streamlit 演示界面：聊天 + 轨迹面板 + 退款审批卡片。"""
import json
import sys
from pathlib import Path

import httpx
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

st.set_page_config(page_title="MiniHarness · Agent Runtime Demo", layout="wide")
st.title("MiniHarness · 最小 Agent Runtime")
st.caption("自研 Harness 内核（Loop / 编排 / 记忆 / 工具 / 预算 / 事件溯源）· 查数 / 诊断 / 退款HITL / 安全拒绝")

API = st.sidebar.text_input("API 地址", "http://127.0.0.1:8000")
st.sidebar.markdown("---")
st.sidebar.markdown("**演示问题（点击填入）**")
examples = {
    "① 查数：GMV": "上个月GMV是多少",
    "② 诊断：GMV下降": "分析GMV下降的原因",
    "③ 退款HITL": "给订单O000123退款300元",
    "④ 安全拒绝": "净利润率是多少",
    "⑤ 多轮记忆": "淘宝平台GMV是多少",
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


def summarize(result: dict) -> str:
    if not result:
        return ""
    status = result.get("status")
    data = result.get("data")
    if status == "clarify":
        return "我需要确认一下您的需求：" + str(data)
    if status in ("refuse", "human_rejected", "execute_failed", "proposal_mismatch"):
        return f"⛔ 已拦截/拒绝：{data}"
    if isinstance(data, dict):
        if "gmv_by_platform" in data:
            lines = ["**诊断结果（按平台 GMV）**"]
            for k, v in sorted(data["gmv_by_platform"].items(), key=lambda x: x[1]):
                lines.append(f"- {k}: ¥{v:,.2f}")
            lines.append("\n**漏斗定位**")
            for f in data["funnel"]:
                lines.append(f"- {f['platform']}: 浏览{f['views']} / 加购{f['carts']} / 下单{f['orders']} (加购率{f['cart_rate']})")
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
