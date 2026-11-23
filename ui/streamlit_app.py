"""Streamlit 演示界面：聊天 + 轨迹面板 + 退款审批卡片。"""
import json
import os
import sys
from pathlib import Path

import httpx
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 渲染函数在独立模块，import 即定义——避免 Streamlit 自上而下重跑脚本时
# 调用（原第 90 行）先于定义（原第 147 行）导致 `name 'summarize' is not defined`。
from ui.render import summarize  # noqa: E402

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
st.caption("自研 Harness 内核（Loop / 编排 / 记忆 / 工具 / 预算 / 事件溯源）· 查数 / 诊断 / 退款HITL / 安全拒绝 / RAG知识检索")

API = st.sidebar.text_input("API 地址", os.environ.get("MINI_HARNESS_API_BASE", "http://127.0.0.1:8000"))
st.sidebar.markdown("---")
st.sidebar.markdown("**演示问题（点击发送）**")
examples = {
    "① 查数：GMV": "上个月GMV是多少",
    "② 诊断：GMV下降": "分析GMV下降的原因",
    "③ 退款HITL": "给订单O000123退款300元",
    "④ 安全拒绝": "净利润率是多少",
    "⑤ 多轮记忆①": "淘宝平台GMV是多少",
    "⑤ 多轮记忆②": "换成京东平台的订单量呢",
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
        fact = None  # 从轨迹 llm_plan 事件取指标名，供渲染区分金额/订单量/比率
        with st.spinner("正在运行 Agent 循环（规划 → 校验 → 执行），请稍候…"):
            try:
                with httpx.stream("POST", f"{API}/run", json={
                    "question": question, "session_id": st.session_state.session_id
                }, timeout=120) as resp:
                    for line in resp.iter_lines():
                        if line.startswith("data: "):
                            data = json.loads(line[6:])
                            events.append(data)
                            if data.get("decision") == "llm_plan" and isinstance(data.get("result"), dict):
                                fact = data["result"].get("fact") or fact
                            if data.get("event") == "human_gate":
                                st.session_state.pending = data
                            if data.get("event") == "done":
                                answer = summarize(data["result"], fact=fact)
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
            with st.spinner("正在写回人工决策并执行退款…"):
                try:
                    with httpx.Client(timeout=60) as client:
                        r = client.post(f"{API}/approve", json={
                            "session_id": st.session_state.session_id,
                            "task_id": pending.get("task_id", ""),
                            "proposal_id": prop["proposal_id"], "approver": approver,
                            "decision": decision, "comment": comment})
                    res = r.json()
                except Exception as e:
                    res = {"status": "请求失败", "result": {"reason": str(e)}, "events": []}
            # 退款单号在 result.data.refund_id（后端 resume 已解包）；别只显示 status。
            result_obj = res.get("result") or {}
            rdata = result_obj.get("data") if isinstance(result_obj, dict) else None
            refund_id = rdata.get("refund_id") if isinstance(rdata, dict) else None
            if decision == "approve" and refund_id:
                content = f"✅ 人工已批准并完成退款：{refund_id}（流水写入独立 ledger 库）"
            else:
                content = f"人工已{decision}：{res.get('status')}"
            st.session_state.messages.append({"role": "assistant", "content": content})
            st.session_state.pending = None
            st.session_state["events"].extend(res.get("events", []))
            st.rerun()
