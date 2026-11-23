"""前端契约回归：把 UI 实际依赖的三条不变量钉死，防止再次漂移。

三个曾真实发生的 bug：
1. done.result.data 是 ToolResult 包装层而非业务数据 → 前端渲染成原始 JSON；
2. 失败自省 hint 跨请求遗留 → 问完"净利润率"后连"淘宝GMV"都被带偏拒答；
3. 重规划时 step_idx 归零 → 轨迹 step 编号重复（step2→step1→step2）。

全部本地 mock，不访问网络；自研 Loop 与 LangGraph 双内核都覆盖。
"""
import asyncio

from app.llm import LLM
from app.runtime.executor import Executor
from app.runtime.loop import Loop, Context
from app.runtime.langgraph_loop import LangGraphLoop
from app import tools  # noqa: F401 — 触发工具注册


def _engine(dbs, runtime):
    shop, led = dbs
    llm = LLM()
    ex = Executor(shop, led)
    engine = Loop(ex, llm) if runtime == "loop" else LangGraphLoop(ex, llm)
    return ex, llm, engine


def _run(engine, ctx, question):
    events = []

    async def emit(e):
        events.append(e)

    result = asyncio.run(engine.run(question, ctx, emit))
    return result, events


def test_done_data_is_unwrapped(dbs, mock_llm, monkeypatch):
    """bug1: 查数/诊断/RAG 的 done.result.data 必须是业务字段，无 ok/data/meta 包装。"""
    import os
    monkeypatch.setenv("MINI_HARNESS_TRACE_DIR", os.path.join(os.getcwd(), "traces"))
    for runtime in ("loop", "langgraph"):
        ex, llm, engine = _engine(dbs, runtime)
        ctx = Context(f"contract_done_{runtime}", ex, llm)
        result, _ = _run(engine, ctx, "上个月GMV是多少")
        assert result["status"] == "completed"
        data = result["data"]
        # 查数业务数据是行集 list；关键是外层不得再有 ToolResult 的 ok/meta/error 包装
        assert not (isinstance(data, dict) and "ok" in data and "meta" in data), \
            f"{runtime}: done.data 仍是 ToolResult 包装: {data}"
        assert isinstance(data, list) and "value" in data[0], \
            f"{runtime}: done.data 缺业务字段: {data}"


def test_reject_diagnose_rag_payloads(dbs, mock_llm, monkeypatch):
    """诊断/RAG 的 done.data 必须含前端真实渲染字段。"""
    import os
    monkeypatch.setenv("MINI_HARNESS_TRACE_DIR", os.path.join(os.getcwd(), "traces"))
    checks = [
        ("分析GMV下降的原因", ("worst_platform", "cur_gmv", "funnel", "narrative")),
        ("什么是GMV口径", ("answer", "citations")),
    ]
    for runtime in ("loop", "langgraph"):
        ex, llm, engine = _engine(dbs, runtime)
        for i, (q, keys) in enumerate(checks):
            ctx = Context(f"contract_payload_{runtime}_{i}", ex, llm)
            result, _ = _run(engine, ctx, q)
            assert result["status"] == "completed", f"{runtime}/{q}: {result}"
            for k in keys:
                assert k in result["data"], f"{runtime}/{q}: done.data 缺 {k}: {result['data']}"


def test_hints_not_leak_across_requests(dbs, mock_llm, monkeypatch):
    """bug2: 拒绝后的 reason 不得污染下一轮无关问题（同一 session 连续 run）。"""
    import os
    monkeypatch.setenv("MINI_HARNESS_TRACE_DIR", os.path.join(os.getcwd(), "traces"))
    for runtime in ("loop", "langgraph"):
        ex, llm, engine = _engine(dbs, runtime)
        ctx = Context(f"contract_hints_{runtime}", ex, llm)
        r1, _ = _run(engine, ctx, "净利润率是多少")
        assert r1["status"] == "refuse"
        assert ctx.state.hints, "拒绝轮应产生本轮自省 hint"
        r2, _ = _run(engine, ctx, "淘宝平台GMV是多少")
        assert r2["status"] == "completed", \
            f"{runtime}: 上一轮拒绝污染了下一轮: {r2}"
        assert ctx.state.result and isinstance(ctx.state.result, list), \
            f"{runtime}: 下一轮查数无结果: {ctx.state.result}"


def test_budget_reset_across_requests(dbs, mock_llm, monkeypatch):
    """bug4: Token 预算约束单轮循环，连续请求不得累计到 budget_exhausted。"""
    import os
    monkeypatch.setenv("MINI_HARNESS_TRACE_DIR", os.path.join(os.getcwd(), "traces"))
    for runtime in ("loop", "langgraph"):
        ex, llm, engine = _engine(dbs, runtime)
        ctx = Context(f"contract_budget_{runtime}", ex, llm)
        for i in range(6):
            result, _ = _run(engine, ctx, "上个月GMV是多少")
            assert result["status"] == "completed", \
                f"{runtime}: 第 {i + 1} 轮连续请求失败: {result}"


def test_summarize_defined_before_call_in_streamlit_script():
    """线上 bug：`summarize` 定义在脚本末尾、调用在中部，Streamlit 自上而下重跑时
    用户输入触发调用 → NameError。渲染函数必须在调用前已 import/定义。"""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    src = (root / "ui" / "streamlit_app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    def_lineno = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "summarize":
            def_lineno = node.lineno
        if isinstance(node, ast.ImportFrom) and any(a.name == "summarize" for a in node.names):
            def_lineno = node.lineno  # 从 ui.render import 也算“已定义”
    assert def_lineno is not None, "streamlit_app.py 未定义/导入 summarize"

    call_lineno = min(
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "summarize"
    )
    assert def_lineno < call_lineno, \
        f"summarize 定义(行{def_lineno})必须在首次调用(行{call_lineno})之前"

    # 渲染模块真实可 import，且六个演示结果都能渲染不抛异常
    from ui.render import summarize as render
    for result in (
        {"status": "completed", "data": [{"value": 1063384.91}]},
        {"status": "refuse", "reason": "未定义的指标"},
        {"status": "clarify", "question": "请明确指标"},
        {"status": "await_human", "data": None},
        {"status": "completed", "data": {"answer": "GMV=成交额", "citations": [
            {"id": "K01", "title": "口径卡", "score": 0.9}]}},
    ):
        assert isinstance(render(result), str) and render(result), result


def test_render_formats_by_fact_and_shows_refund_id():
    """线上演示暴露：订单量被显示成 ¥0.00、淘宝带平台字段金额没千分位。
    渲染必须按 fact 区分金额/计数/比率，且对带维度行的 value 同样格式化。"""
    from pathlib import Path
    from ui.render import summarize, format_value

    root = Path(__file__).resolve().parents[1]
    src = (root / "ui" / "streamlit_app.py").read_text(encoding="utf-8")

    # 演示词不得再查库里不存在的天猫
    assert "换成天猫" not in src, "演示脚本仍在查 tmall（库里只有淘宝/京东/抖音）"
    assert "换成京东" in src

    # 订单量：整数千分位，无 ¥、无小数
    cnt = summarize({"status": "completed", "data": [{"value": 1234.0}]}, fact="order_cnt")
    assert "1,234" in cnt and "¥" not in cnt and ".0" not in cnt, cnt
    # 金额：¥ + 千分位
    money = summarize({"status": "completed", "data": [{"value": 3946884.14}]}, fact="gmv")
    assert "¥3,946,884.14" in money, money
    # 带维度行：value 同样按 fact 格式化（淘宝 GMV 千分位）
    dim = summarize({"status": "completed", "data": [
        {"platform": "taobao", "value": 3946884.1399999973}]}, fact="gmv")
    assert "platform=taobao" in dim and "¥3,946,884.14" in dim, dim
    # 比率：百分比
    rate = summarize({"status": "completed", "data": [{"value": 0.125}]}, fact="discount_rate")
    assert "12.5%" in rate, rate
    # 直接调 format_value 也应一致
    assert format_value(0, "order_cnt") == "0"
    assert format_value(42.7, "buyer_cnt") == "43"


def test_approve_shows_refund_id():
    """线上演示暴露：批准后对话只写 completed，退款单号只在右侧轨迹。
    UI 应从 result.data.refund_id 提取并写进对话。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "ui" / "streamlit_app.py").read_text(encoding="utf-8")
    assert "refund_id" in src, "UI 未从审批结果提取退款单号"
    assert "spinner" in src, "UI 请求过程缺少 loading 提示"


def test_step_numbers_monotonic(dbs, mock_llm, monkeypatch):
    """bug3: 拒绝触发重规划后，轨迹 step 编号必须单调递增、不重复。"""
    import os
    monkeypatch.setenv("MINI_HARNESS_TRACE_DIR", os.path.join(os.getcwd(), "traces"))
    for runtime in ("loop", "langgraph"):
        ex, llm, engine = _engine(dbs, runtime)
        ctx = Context(f"contract_steps_{runtime}", ex, llm)
        _, events = _run(engine, ctx, "净利润率是多少")
        steps = [e["step"] for e in events]
        assert steps == list(range(1, len(steps) + 1)), \
            f"{runtime}: step 编号非单调/重复: {steps}"
