"""双内核一致性：自研 Loop 与 LangGraph 内核跑同一问题，结果必须逐字一致。

这是"LangGraph 是一层编排皮"的可执行证据：
换内核只换驱动循环的方式，步骤语义（steps.run_step）共用，因此行为不分叉。
全部本地 mock，不访问网络。
"""
import asyncio
import json
from pathlib import Path

import pytest

from app.llm import LLM
from app.runtime.executor import Executor
from app.runtime.loop import Loop, Context
from app.runtime.langgraph_loop import LangGraphLoop
from app.runtime.factory import build_runtime
from app import tools  # noqa: F401 — 触发工具注册（测试间不依赖导入顺序）

ROOT = Path(__file__).resolve().parents[1]


def _fresh(dbs, session, runtime):
    """每个内核用独立 session，避免 state.json 互相覆盖。"""
    shop, led = dbs
    import os
    os.environ["DASHSCOPE_API_KEY"] = ""
    llm = LLM()
    ex = Executor(shop, led)
    ctx = Context(session, ex, llm)
    engine = Loop(ex, llm) if runtime == "loop" else LangGraphLoop(ex, llm)
    return ctx, engine, llm


async def _run(dbs, session, runtime, question):
    ctx, engine, _ = _fresh(dbs, session, runtime)
    events = []

    async def emit(e):
        events.append(e)

    result = await engine.run(question, ctx, emit)
    return result, [e.get("decision") for e in events], ctx


# 一条查数 + 一条拒答 + 一条知识检索 + 一条退款挂起，覆盖所有步骤分支
CASES = [
    ("query", "上个月GMV是多少"),
    ("query", "淘宝平台订单量是多少"),
    ("refuse", "净利润率是多少"),
    ("knowledge", "什么是GMV口径"),
    ("diagnose", "分析GMV下降的原因"),
    ("refund", "给订单O000001退款300元"),
]


@pytest.mark.parametrize("tag,question", CASES)
def test_dual_runtime_same_status(dbs, tag, question):
    r1, tr1, ctx1 = asyncio.run(_run(dbs, f"lg_cmp_loop_{tag}", "loop", question))
    r2, tr2, ctx2 = asyncio.run(_run(dbs, f"lg_cmp_graph_{tag}", "langgraph", question))
    assert r1.get("status") == r2.get("status"), \
        f"{question}: loop={r1.get('status')} graph={r2.get('status')}"
    # 步骤决策序列一致（编排路径等价，不只是最终状态碰巧相同）
    assert tr1 == tr2, f"{question}: 轨迹决策分叉\nloop={tr1}\ngraph={tr2}"


def test_dual_runtime_same_query_values(dbs):
    """查数类比较实际数值（不只是 status），证明编译/执行结果不分叉。"""
    q = "上个月GMV是多少"
    r1, _, ctx1 = asyncio.run(_run(dbs, "lg_val_loop", "loop", q))
    r2, _, ctx2 = asyncio.run(_run(dbs, "lg_val_graph", "langgraph", q))
    assert ctx1.state.result == ctx2.state.result, \
        f"查数结果不一致:\nloop={ctx1.state.result}\ngraph={ctx2.state.result}"


def test_dual_runtime_refund_hitl(dbs):
    """退款：两内核都必须在人工批准前零写入 refunds。"""
    q = "给订单O000002退款300元"
    r1, _, ctx1 = asyncio.run(_run(dbs, "lg_hitl_loop", "loop", q))
    r2, _, ctx2 = asyncio.run(_run(dbs, "lg_hitl_graph", "langgraph", q))
    assert r1.get("status") == r2.get("status") == "await_human"
    assert ctx1.state.pending_proposal and ctx2.state.pending_proposal
    p1, p2 = ctx1.state.pending_proposal, ctx2.state.pending_proposal
    assert p1.order_id == p2.order_id and abs(p1.amount - p2.amount) < 1e-6


def test_factory_selects_runtime(dbs, monkeypatch):
    shop, led = dbs
    import os
    os.environ["DASHSCOPE_API_KEY"] = ""
    llm = LLM()
    ex = Executor(shop, led)
    monkeypatch.setenv("MINI_HARNESS_RUNTIME", "langgraph")
    assert isinstance(build_runtime(ex, llm), LangGraphLoop)
    monkeypatch.setenv("MINI_HARNESS_RUNTIME", "loop")
    assert isinstance(build_runtime(ex, llm), Loop)
