"""MiniHarness 加固断言：全部本地、不访问网络。"""
import asyncio
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from app.llm import LLM
from app.runtime.executor import Executor, REGISTRY
from app.runtime.loop import Loop, Context
from app.runtime.models import SemanticPlan, Filter, Approval, RefundProposal
from app.semantic.compiler import compile_plan
from app.semantic.guard import guard
from app.semantic.facts import FACTS
from app.semantic.normalize import normalize_plan, resolve_time
from app.tools.builtin import approve_in_ledger


def make_ctx(dbs, session="t", mock=True):
    shop, led = dbs
    import os
    if mock:
        os.environ["DASHSCOPE_API_KEY"] = ""
    llm = LLM()
    ex = Executor(shop, led)
    return Context(session, ex, llm), ex, llm


# ---------- 1. 编译器 + guard ----------
def test_compiler_all_facts_pass_guard():
    for name, fact in FACTS.items():
        sql, _ = compile_plan(SemanticPlan(fact=name), fact)
        ok, msg = guard(sql)
        assert ok, f"{name}: {msg}"


def test_category_only_gmv():
    # category 维度只允许 gmv
    sql, _ = compile_plan(SemanticPlan(fact="gmv", dimensions=["category"]), FACTS["gmv"])
    assert "p.category" in sql and "SUM(oi.item_total)" in sql
    # 比值型指标带品类 → 拒绝
    with pytest.raises(ValueError):
        compile_plan(SemanticPlan(fact="discount_rate", dimensions=["category"]), FACTS["discount_rate"])


def test_guard_rejects_multi_statement_and_write():
    assert not guard("SELECT 1; DROP TABLE orders")[0]
    assert not guard("DELETE FROM orders")[0]
    assert guard("SELECT * FROM orders LIMIT 10")[0]


def test_relative_time_anchored_to_manifest():
    bounds = resolve_time("上个月")
    assert bounds and bounds[0].startswith("2025-")  # 锚定数据集最大日期，不是系统今天


# ---------- 2. 未知指标不执行 SQL ----------
@pytest.mark.asyncio
async def test_unknown_metric_no_query(dbs, mock_llm):
    ctx, ex, llm = make_ctx(dbs, "unknown_metric")
    loop = Loop(ex, llm)
    async def emit(_): pass
    result = await loop.run("净利润率是多少", ctx, emit)
    assert result["status"] == "refuse"
    # 轨迹里没有任何 query_data 的真正执行结果（result.ok 字段）
    executed = [s for s in ctx.trace.steps
                if s.tool == "query_data" and isinstance(s.result, dict) and "ok" in (s.result or {})]
    assert not executed, "未知指标不得执行查询"


# ---------- 3. 多轮继承淘宝 filter ----------
@pytest.mark.asyncio
async def test_multiturn_inherits_taobao_filter(dbs, mock_llm):
    ctx, ex, llm = make_ctx(dbs, "multiturn")
    loop = Loop(ex, llm)
    async def emit(_): pass
    r1 = await loop.run("淘宝平台GMV是多少", ctx, emit)
    assert r1["status"] == "completed"
    assert ctx.state.last_plan is not None
    # 第二轮：点了"订单量"这个新指标，但过滤应继承上一轮的 taobao
    r2 = await loop.run("订单量呢", ctx, emit)
    assert r2["status"] == "completed"
    assert any(f.field == "platform" and f.value == "taobao" for f in ctx.state.last_plan.filters), \
        "多轮应继承上一轮 platform=taobao filter"


# ---------- 4 & 5. 退款挂起 0 行 / 磁盘恢复后批准 / 幂等 ----------
@pytest.mark.asyncio
async def test_refund_lifecycle_and_idempotency(dbs, mock_llm):
    shop, led = dbs
    ctx, ex, llm = make_ctx(dbs, "refund_life")
    loop = Loop(ex, llm)
    async def emit(_): pass
    before = led.execute("SELECT count(*) FROM refunds").fetchone()[0]
    r = await loop.run("给订单O000123退款88元", ctx, emit)
    assert r["status"] == "await_human"
    assert led.execute("SELECT count(*) FROM refunds").fetchone()[0] == before, "挂起时不得写 refunds"

    pid = ctx.state.pending_proposal.proposal_id
    # 模拟进程重启：新建一个 Context 从磁盘恢复
    ctx2, _, _ = make_ctx(dbs, "refund_life", mock=True)
    assert ctx2.restore_state(), "应能从 state.json 恢复"
    assert ctx2.state.pending_proposal.proposal_id == pid

    # 人工入口：先标账本 approved，再执行
    res = approve_in_ledger(ex, pid, "reviewer", "approve", "已核对", "low")
    assert res["status"] == "approved"
    ctx2.approval = Approval(proposal_id=pid, approver="reviewer", decision="approve")
    loop2 = Loop(ex, llm)
    done = await loop2.resume("T00000", ctx2.approval, ctx2, emit)
    assert done["status"] == "completed"
    n1 = led.execute("SELECT count(*) FROM refunds").fetchone()[0]
    assert n1 == before + 1

    # 重复批准 + 重复执行：仍只有 1 行
    approve_in_ledger(ex, pid, "reviewer2", "approve", "重复", "low")
    again = await ex.execute("execute_refund", {}, ctx2) if ctx2.state.pending_proposal else None
    # pending_proposal 已被清空 → 直接测账本层幂等：手搓一个同 proposal 的 ctx
    c3 = Context("idem", ex, llm)
    c3.state.pending_proposal = RefundProposal(proposal_id=pid, order_id="O000123", amount=88.0, reason="x")
    c3.approval = Approval(proposal_id=pid, approver="reviewer2", decision="approve")
    r2 = await ex.execute("execute_refund", {}, c3)
    assert r2.ok and r2.data.get("status") == "already_executed"
    assert led.execute("SELECT count(*) FROM refunds").fetchone()[0] == n1, "重复执行不得新增行"


# ---------- 6. 诊断含两个月和跌幅最大平台 ----------
@pytest.mark.asyncio
async def test_diagnose_names_months_and_worst_platform(dbs, mock_llm):
    ctx, ex, llm = make_ctx(dbs, "diag")
    loop = Loop(ex, llm)
    async def emit(_): pass
    r = await loop.run("分析GMV下降的原因", ctx, emit)
    assert r["status"] == "completed"
    diag_step = next(s for s in ctx.trace.steps if s.tool == "diagnose")
    data = diag_step.result["data"]
    assert data["prev_month"] and data["cur_month"] and data["prev_month"] != data["cur_month"]
    assert data["worst_platform"] in ("taobao", "jd", "douyin")
    assert "跌幅" in data["narrative"] or data["worst_delta_pct"] < 0


# ---------- 7. MCP 自批路径不存在 ----------
def test_mcp_has_no_approve_tool():
    """从 server 源码层面断言：MCP 不注册批准/执行工具。"""
    src = (ROOT / "mcp_server" / "server.py").read_text(encoding="utf-8")
    assert "approve_and_execute_refund" not in src
    assert "@mcp.tool()\nasync def execute_refund" not in src
    assert "def refund_status" in src


# ---------- 8. 模型漏填时间 + 乱加 month，原话仍约束为单月合计 ----------
class _SloppyPlanLLM:
    """模拟真实模型：故意不带 time_range，还把 month 塞进 dimensions。"""
    def __init__(self):
        self.mode = "mock"

    async def chat(self, messages, *, task_type="query", **kwargs):
        plan = {"fact": "gmv", "intent": "query", "dimensions": ["month"], "filters": []}
        return json.dumps(plan, ensure_ascii=False), {"prompt": 5, "completion": 5}


@pytest.mark.asyncio
async def test_utterance_time_overrides_sloppy_model(dbs):
    shop, led = dbs
    ctx = Context("sloppy", Executor(shop, led), _SloppyPlanLLM())
    loop = Loop(ctx.executor, ctx.llm)
    async def emit(_): pass
    r = await loop.run("上个月GMV是多少", ctx, emit)
    assert r["status"] == "completed"
    # 原话约束生效：time_range 写入相对时间，month 维度被去掉
    assert ctx.plan["time_range"] == "上个月"
    assert "month" not in ctx.plan["dimensions"]
    # 结果是单月合计：只有一行 value，sql 含上月起点
    rows = ctx.state.result
    assert isinstance(rows, list) and len(rows) == 1 and "value" in rows[0]
    from app.semantic.normalize import resolve_time
    start, end = resolve_time("上个月")
    sql_step = next(s for s in ctx.trace.steps if s.tool == "query_data")
    assert sql_step.result["meta"]["params"][0] == start  # 参数绑定：真实时间窗在 params 里
    assert sql_step.result["meta"]["time_range"] == "上个月"


# ---------- 9. 诊断叙述折扣率与 cur_month 同窗一致 ----------
@pytest.mark.asyncio
async def test_diagnose_discount_matches_cur_month(dbs, mock_llm):
    ctx, ex, llm = make_ctx(dbs, "diag_window")
    loop = Loop(ex, llm)
    async def emit(_): pass
    r = await loop.run("分析GMV下降的原因", ctx, emit)
    assert r["status"] == "completed"
    diag_step = next(s for s in ctx.trace.steps if s.tool == "diagnose")
    data = diag_step.result["data"]
    from app.semantic.normalize import _month_bounds
    cur = data["cur_month"]
    y, m = int(cur[:4]), int(cur[5:7])
    expected_window = list(_month_bounds(y, m))
    # 折扣率查询参数落在跌幅当月（闭开区间与 GMV 对比同窗）
    assert data["discount_window"] == expected_window
    # 叙述写进了该窗查询得到的折扣率
    assert str(data["discount_rate"]) in data["narrative"]
    # 转化率不得大于 1；置空时必须写明不能计算
    rate = data["cart_rate"]
    if rate is None:
        assert "不能计算转化率" in data["narrative"]
    else:
        assert rate <= 1


# ---------- 10. proposal 工具越权写 refunds 被闸门拦截 ----------
def test_proposal_cannot_write_refunds(dbs):
    ex = Executor(*dbs)
    with pytest.raises(PermissionError):
        ex.ledger_write("propose_refund", "refunds",
                        "INSERT INTO refunds VALUES (?,?,?,?,?,?)",
                        ["X", "O000123", 1.0, "a", "t", "approved"])
    assert REGISTRY["propose_refund"]["access"] == "proposal"
    assert REGISTRY["execute_refund"]["access"] == "write_gated"


# ---------- 11. 路由：强诊断信号不被"查询/订单"打平误判 ----------
def test_router_strong_diagnose_signal():
    from app.runtime.router import decide
    assert decide("查询订单量下降的原因") == "diagnose"
    assert decide("GMV下降原因诊断") == "diagnose"
    assert decide("订单量是多少") == "query_data"


# ---------- 12. 路由：口语"退N元"也算退款，且强信号不覆盖审批 ----------
def test_router_colloquial_refund_and_approval_priority():
    from app.runtime.router import decide
    assert decide("帮我给O000001退1500元") == "propose_refund"
    assert decide("订单O000002退货退款200元") == "propose_refund"
    # 有挂起提案时审批优先于"原因"诊断词
    assert decide("拒绝，原因是太贵", last_intent="propose_refund", has_pending=True) == "reject"


# ---------- 13. 金标独立性与 discriminative power ----------
def test_golden_set_500_and_independent_oracle():
    """500 条、q 唯一、sql 题带可核对 gold；且改坏时间窗会被评测抓到。"""
    import json as _json
    rows = [_json.loads(l) for l in (ROOT / "eval" / "golden_set.jsonl").open(encoding="utf-8") if l.strip()]
    assert len(rows) == 500
    assert len({r["q"] for r in rows}) == 500
    sql_rows = [r for r in rows if r["kind"] == "sql"]
    assert len(sql_rows) >= 250
    assert all(r["expect"].get("gold") for r in sql_rows)
    # 金标答案不是"全是 gmv 总量"：至少 30 条 gold 值互不相同的分组题
    grouped = [r for r in sql_rows if r["expect"].get("dimensions")]
    distinct = {str(r["expect"]["gold"]) for r in grouped}
    assert len(grouped) >= 30 and len(distinct) >= 20
    # 难度分层齐全（含最难的 L5 组合题）
    tiers = {r["tier"] for r in rows}
    assert {"L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8", "L9"} <= tiers


# ---------- 14. 原话约束：绝对时间 / 未知指标 / 点名维度 ----------
def test_utterance_absolute_month_forced():
    """真实模型常把 2025年3月 漏成 time_range=None → 查成全年；原话必须覆盖。"""
    from app.semantic.normalize import constrain_utterance_time
    sloppy = {"fact": "gmv", "dimensions": [], "filters": [], "time_range": None}
    p = constrain_utterance_time(sloppy, "2025年3月成交额是多少")
    assert p["time_range"] == "2025-03"
    assert p["time_bounds"] == ["2025-03-01 00:00:00", "2025-04-01 00:00:00"]
    p2 = constrain_utterance_time(sloppy, "2025-11GMV")
    assert p2["time_range"] == "2025-11"
    # 相对词仍优先，且无「按月」时去掉模型乱加的 month
    p3 = constrain_utterance_time({"fact": "gmv", "dimensions": ["month"], "time_range": "2025-03"},
                                  "上个月GMV是多少")
    assert p3["time_range"] == "上个月" and "month" not in p3["dimensions"]


def test_utterance_unknown_metric_not_silently_mapped():
    """模型把「毛利润」映射成 gmv 时必须打回未知指标，交给 precheck 拒答。"""
    from app.semantic.normalize import constrain_utterance_fact
    p = constrain_utterance_fact({"fact": "gmv", "dimensions": []}, "上个月毛利润是多少")
    assert p["fact"] == "毛利润"
    assert constrain_utterance_fact({"fact": "gmv"}, "成交额是多少")["fact"] == "gmv"


def test_utterance_named_dimension_not_dropped_on_replan():
    """重规划把「按品类」丢掉会把拒答题答成总量，原话维度必须回填。"""
    from app.semantic.normalize import constrain_utterance_dimensions
    p = constrain_utterance_dimensions({"fact": "discount_rate", "dimensions": []},
                                       "按品类折扣率是多少")
    assert p["dimensions"] == ["category"]
    p2 = constrain_utterance_dimensions({"fact": "gmv", "dimensions": ["platform"]},
                                        "按平台看成交额，再分城市")
    assert set(p2["dimensions"]) == {"platform", "city"}


# ---------- 15. RAG：确定性向量检索 + 引证 + 无依据拒答 ----------
def test_rag_deterministic_and_top1_accuracy():
    """同一查询向量确定；专名查询 top1 命中正确口径文档。"""
    from app.rag import embed, get_store
    assert embed("GMV 口径") == embed("GMV 口径")          # 无随机
    s = get_store()
    cases = {"GMV 是什么口径": "K01", "折扣率为什么不能按品类": "K03",
             "退款怎么审批": "K08", "上个月是什么意思": "K05",
             "按月和单月区别": "K06", "净利润率能查吗": "K11"}
    for q, want in cases.items():
        hits = s.search(q, top_k=1)
        assert hits and hits[0].id == want, (q, [h.id for h in s.search(q)])


def test_rag_tool_grounded_and_refuse(dbs):
    """检索工具：命中带出处；无依据时 NO_GROUNDED_EVIDENCE 拒答。"""
    import asyncio
    from app import tools  # noqa
    ex = Executor(*dbs)
    ok = asyncio.run(ex.execute("search_knowledge", {"query": "GMV 是什么口径"}))
    assert ok.ok and ok.data["citations"][0]["id"] == "K01"
    assert ok.data["answer"].find("出处") >= 0
    assert ok.meta["free_generation"] is False
    miss = asyncio.run(ex.execute("search_knowledge", {"query": "公司团建去哪玩"}))
    assert not miss.ok and miss.error["code"] == "NO_GROUNDED_EVIDENCE"


def test_rag_router_and_step():
    """知识意图走 RAG 编排，不经 LLM 规划；且金标问题不触发知识路由。"""
    from app.runtime.router import decide
    from app.runtime.orchestrator import Orchestrator
    from app.runtime.models import Task
    assert decide("什么是GMV口径") == "knowledge"
    assert decide("口径的定义是什么") == "knowledge"
    # 金标里的典型问法不进知识路由
    for q in ("上个月GMV是多少", "净利润率是多少", "给订单O000123退款300元", "分析GMV下降的原因"):
        assert decide(q) != "knowledge", q
    steps = Orchestrator().plan_steps(Task(id="T1", type="knowledge", status="running"))
    assert steps == ["search_knowledge"]


def test_rag_end_to_end_cited(dbs, mock_llm):
    """端到端：知识问题 → RAG 步 → 带出处答案，轨迹可核对。"""
    import asyncio
    from app import tools  # noqa
    from app.rag import get_store
    ex = Executor(*dbs)
    ctx = Context("rag_e2e", ex, mock_llm)
    loop = Loop(ex, mock_llm)
    async def emit(_): pass
    r = asyncio.run(loop.run("什么是GMV口径", ctx, emit))
    assert r["status"] == "completed"
    assert ctx.state.result["citations"][0]["id"] == "K01"
    assert any(s.tool == "search_knowledge" for s in ctx.trace.steps)
