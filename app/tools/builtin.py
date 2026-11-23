"""内置工具：查数 / 诊断 / 退款提案 / 退款执行。import 即注册。"""
import uuid
from ..runtime.models import ToolResult, RefundProposal, Approval, SemanticPlan, Filter
from ..runtime.executor import register_tool, digest
from ..semantic.facts import FACTS
from ..semantic.compiler import compile_plan
from ..semantic.guard import guard


def _as_plan(raw: dict) -> SemanticPlan:
    """LLM 输出是 dict → 归一化为契约对象（Pydantic 强校验）。"""
    if isinstance(raw, SemanticPlan):
        return raw
    data = dict(raw or {})
    data["filters"] = [Filter(**f) if isinstance(f, dict) else f for f in data.get("filters", [])]
    data.pop("tool", None)
    data.pop("order_id", None)
    data.pop("amount", None)
    data.pop("reason", None)
    return SemanticPlan(**data)


@register_tool("query_data", "按语义计划只读查询指标", access="read_only")
def query_data(args: dict, executor, ctx) -> ToolResult:
    raw = args["plan"]
    plan = _as_plan(raw)
    fact_name = plan.fact
    if fact_name not in FACTS:
        return ToolResult(ok=False, error={"code": "UNKNOWN_FACT", "retryable": True,
                          "category": "semantic", "message": f"未知指标: {fact_name}"})
    try:
        sql, params = compile_plan(plan, FACTS[fact_name])
    except ValueError as e:
        return ToolResult(ok=False, error={"code": "FIELD_REJECT", "retryable": True,
                          "category": "security", "message": str(e)})
    ok, msg = guard(sql)
    if not ok:
        return ToolResult(ok=False, error={"code": "GUARD_REJECT", "retryable": True,
                          "category": "security", "message": msg})
    rows = executor.con.execute(sql, params).fetchall()
    data = [dict(zip([c[0] for c in executor.con.description], r)) for r in rows] if rows else []
    # SQL 用参数绑定，实际时间窗在 params 里——一并写进轨迹元数据，便于审计核对
    # （"上个月"是否真的落在 2025-11-01 起，看 params 而不是猜 SQL 字符串）。
    return ToolResult(ok=True, data=data, meta={"row_count": len(rows), "sql": sql,
                      "params": params, "time_range": plan.time_range,
                      "query_digest": digest(sql)})


@register_tool("search_knowledge", "RAG 知识检索：向量召回口径文档 + 引证回答（低于阈值拒答）", access="read_only")
def search_knowledge(args: dict, executor, ctx) -> ToolResult:
    """检索增强回答：答案只拼接命中文档原文并标出处，不自由生成、不查业务库。"""
    from ..rag import get_store
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(ok=False, error={"code": "EMPTY_QUERY", "retryable": False,
                          "category": "validation", "message": "检索问题为空"})
    hits = get_store().search(query, top_k=int(args.get("top_k", 3)))
    if not hits:
        # 阈值以上无命中：宁可拒答，也不用常识脑补（这是 RAG 的幻觉闸门）
        return ToolResult(ok=False, error={"code": "NO_GROUNDED_EVIDENCE", "retryable": False,
                          "category": "grounding", "message": "知识库无相关依据，拒绝凭常识回答"})
    answer = "\n".join(f"{h.text}  —— 出处 {h.cite()}，相似度 {h.score}" for h in hits)
    return ToolResult(ok=True, data={
        "query": query,
        "answer": answer,
        "citations": [{"id": h.id, "title": h.title, "score": h.score, "source": "app/rag/knowledge.json"}
                      for h in hits],
        "hit_count": len(hits),
    }, meta={"retriever": "char-ngram-hash-cosine", "top_k": int(args.get("top_k", 3)),
             "grounded": True, "free_generation": False})


@register_tool("diagnose", "多步诊断：相邻两月分平台 GMV 对比 + 漏斗定位", access="read_only")
def diagnose(args: dict, executor, ctx) -> ToolResult:
    """诊断结论必须来自可核对的时间窗：比较相邻两月，点名跌幅最大平台。"""
    from ..semantic.normalize import load_manifest, _month_bounds
    import datetime as _dt

    plan = _as_plan(args["plan"])
    # 锚定数据集最大日期：扫描全年相邻月对，选 GMV 跌幅最大的一对（降幅做在哪个月都能点名）
    anchor = _dt.datetime.strptime(load_manifest()["max_order_time"][:19], "%Y-%m-%d %H:%M:%S")
    sql_month = ("SELECT strftime(o.order_time, '%Y-%m') AS ym, SUM(o.total_amount) AS value "
                 "FROM orders o WHERE o.payment_time IS NOT NULL "
                 "GROUP BY ym ORDER BY ym ASC LIMIT 1000")
    ok, msg = guard(sql_month)
    if not ok:
        raise ValueError(f"月度汇总 SQL 未过 guard: {msg}")
    months = [(r[0], round(r[1] or 0, 2)) for r in executor.con.execute(sql_month).fetchall()]

    def gmv_by_platform(start: str, end: str) -> dict:
        sql = ("SELECT o.platform, SUM(o.total_amount) AS value FROM orders o "
               "WHERE o.payment_time IS NOT NULL AND o.order_time >= ? AND o.order_time < ? "
               "GROUP BY o.platform ORDER BY value DESC LIMIT 1000")
        ok, msg = guard(sql)
        if not ok:
            raise ValueError(f"诊断 SQL 未过 guard: {msg}")
        rows = executor.con.execute(sql, [start, end]).fetchall()
        return {r[0]: round(r[1] or 0, 2) for r in rows}

    # 找相邻两月中 GMV 环比降幅最大的一对
    best = None
    for i in range(1, len(months)):
        prev_ym, prev_v = months[i - 1]
        cur_ym, cur_v = months[i]
        drop = (cur_v - prev_v) / prev_v if prev_v else 0.0
        if best is None or drop < best[0]:
            best = (drop, prev_ym, cur_ym)
    if best is None:
        return ToolResult(ok=False, error={"code": "NO_MONTHS", "retryable": False,
                          "category": "data", "message": "无足够月份做诊断"})
    _, prev_ym, cur_ym = best
    prev_y, prev_m = int(prev_ym[:4]), int(prev_ym[5:7])
    cur_y, cur_m = int(cur_ym[:4]), int(cur_ym[5:7])
    cur_start, cur_end = _month_bounds(cur_y, cur_m)
    prev_start, prev_end = _month_bounds(prev_y, prev_m)

    cur = gmv_by_platform(cur_start, cur_end)
    prev = gmv_by_platform(prev_start, prev_end)
    platforms = set(cur) | set(prev)
    deltas = {}
    for pf in platforms:
        a, b = prev.get(pf, 0.0), cur.get(pf, 0.0)
        deltas[pf] = round((b - a) / a, 4) if a else 0.0
    worst = min(platforms, key=lambda p: deltas[p]) if platforms else ""

    # 漏斗：只查 user_behavior，不再 orders LEFT JOIN user_behavior（平台级笛卡尔积会
    # 让 COUNT DISTINCT 掩盖放大，随机数据下加购会话数会超过浏览会话数 → 假转化率 >1）。
    # 会话级条件聚合：先算每会话的首个浏览/加购时间，只有"先有浏览再有加购"才算转化。
    funnel_sql = (
        "SELECT "
        "SUM(CASE WHEN first_view IS NOT NULL THEN 1 ELSE 0 END) AS views, "
        "SUM(CASE WHEN first_view IS NOT NULL AND first_cart IS NOT NULL "
        "AND first_view <= first_cart THEN 1 ELSE 0 END) AS carts "
        "FROM ("
        "SELECT session_id, "
        "min(CASE WHEN behavior_type='浏览' THEN behavior_time END) AS first_view, "
        "min(CASE WHEN behavior_type='加购' THEN behavior_time END) AS first_cart "
        "FROM user_behavior "
        "WHERE platform = ? AND behavior_time >= ? AND behavior_time < ? "
        "GROUP BY session_id"
        ") t LIMIT 1000")
    ok, msg = guard(funnel_sql)
    if not ok:
        raise ValueError(f"漏斗 SQL 未过 guard: {msg}")
    frow = executor.con.execute(funnel_sql, [worst, cur_start, cur_end]).fetchone()
    views = int(frow[0] or 0) if frow else 0
    carts = int(frow[1] or 0) if frow else 0
    # 防御：浏览会话为 0 或加购数反超浏览数 → 置空，不输出大于 1 的转化率
    if views == 0 or carts > views:
        cart_rate = None
        funnel_text = "该窗行为数据不能计算转化率"
    else:
        cart_rate = round(carts / views, 3)
        funnel_text = f"该平台加购转化率 {cart_rate}（{carts}/{views}）"
    funnel = [{"platform": worst, "views": views, "carts": carts, "cart_rate": cart_rate}]

    # 折扣率必须用与 GMV 对比同一个当前月闭开区间：time_range 设为 cur_ym(YYYY-MM)，
    # resolve_time 会解析成 [_month_bounds 的 start, end)，与 cur_start/cur_end 一致。
    cur_ym = f"{cur_y:04d}-{cur_m:02d}"
    dplan = SemanticPlan(fact="discount_rate", dimensions=["platform"],
                         filters=[{"field": "platform", "value": worst}],
                         time_range=cur_ym, intent="query")
    dsql, dparams = compile_plan(dplan, FACTS["discount_rate"])
    ok, msg = guard(dsql)
    if not ok:
        raise ValueError(f"折扣 SQL 未过 guard: {msg}")
    drow = executor.con.execute(dsql, dparams).fetchall()
    discount = round(drow[0][1], 4) if drow and drow[0][1] is not None else None

    data = {
        "prev_month": f"{prev_y:04d}-{prev_m:02d}", "cur_month": cur_ym,
        "prev_gmv": prev, "cur_gmv": cur, "delta_pct": deltas,
        "worst_platform": worst, "worst_delta_pct": deltas.get(worst, 0.0),
        "funnel": funnel, "cart_rate": cart_rate, "discount_rate": discount,
        "discount_window": [cur_start, cur_end],
        "narrative": (f"{cur_ym} 对比 {prev_y:04d}-{prev_m:02d}，"
                      f"{worst} 平台 GMV 跌幅最大（{deltas.get(worst, 0.0) * 100:.1f}%）。"
                      f"{funnel_text}，折扣率 {discount}（{cur_ym} 同窗）。"),
    }
    return ToolResult(ok=True, data=data, meta={"steps": 4, "query_digest": digest(
        f"{cur}{prev}{worst}"), "guard": "passed"})


@register_tool("propose_refund", "生成退款提案（不执行，落 proposals 账本）", access="proposal")
def propose_refund(args: dict, executor, ctx) -> ToolResult:
    order_id = args.get("order_id", "")
    amount = float(args.get("amount", 0))
    # 只读确认订单存在与金额
    rows = executor.con.execute(
        "SELECT order_id, total_amount, order_status FROM orders WHERE order_id = ?", [order_id]).fetchall()
    if not rows:
        return ToolResult(ok=False, error={"code": "ORDER_NOT_FOUND", "retryable": True,
                          "category": "data", "message": f"订单 {order_id} 不存在"})
    real_amount = float(rows[0][1] or 0)
    risk = "high" if amount > real_amount or amount > 2000 else "low"
    proposal = RefundProposal(
        proposal_id=f"RF{uuid.uuid4().hex[:8]}", order_id=order_id,
        amount=amount, reason=args.get("reason", "用户申请退款"),
        evidence_refs=[digest(order_id + str(real_amount))], risk_level=risk)
    # 提案落账本（pending）：走受控写入出口，access=proposal 只允许写 proposals；
    # 内部 Loop 与外部 MCP 共用同一条路径，都无法借此写 refunds。
    from datetime import datetime
    executor.ledger_write(
        "propose_refund", "proposals",
        "INSERT INTO proposals(proposal_id, order_id, amount, reason, risk_level, "
        "evidence_refs, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        [proposal.proposal_id, proposal.order_id, proposal.amount, proposal.reason,
         proposal.risk_level, ",".join(proposal.evidence_refs), "pending",
         datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    if ctx:
        ctx.state.pending_proposal = proposal
    return ToolResult(ok=True, data=proposal, meta={"order_amount": real_amount, "risk": risk,
                      "ledger": "proposals/pending"})


def approve_in_ledger(executor, proposal_id: str, approver: str, decision: str,
                      comment: str, risk_level: str) -> dict:
    """人工入口共用的批准函数：把 proposals 状态 pending → approved/rejected。

    只标记账本状态，不写 refunds。真正的执行由 execute_refund 做（单事务幂等）。
    """
    if decision == "reject":
        executor.ledger.execute(
            "UPDATE proposals SET status='rejected' WHERE proposal_id=? AND status='pending'",
            [proposal_id])
        return {"status": "rejected"}
    if risk_level == "high" and len((comment or "").strip()) < 2:
        return {"status": "need_comment"}
    cur = executor.ledger.execute(
        "UPDATE proposals SET status='approved' WHERE proposal_id=? AND status='pending'",
        [proposal_id])
    changed = cur.rowcount if hasattr(cur, "rowcount") else -1
    if changed == 0:
        # 已批准过（幂等）或不存在 —— 区分一下
        row = executor.ledger.execute(
            "SELECT status FROM proposals WHERE proposal_id=?", [proposal_id]).fetchone()
        return {"status": row[0] if row else "not_found", "already": True}
    return {"status": "approved"}


@register_tool("execute_refund", "执行已批准退款（单事务幂等，写独立 ledger 库）", access="write_gated")
def execute_refund(args: dict, executor, ctx) -> ToolResult:
    approval: Approval = ctx.approval
    proposal = ctx.state.pending_proposal
    if not proposal or proposal.proposal_id != approval.proposal_id:
        return ToolResult(ok=False, error={"code": "PROPOSAL_MISMATCH", "retryable": False,
                          "category": "auth", "message": "批准提案与当前提案不一致"})
    # 账本必须是 approved 状态才执行（MCP 无法伪造：它改不了 proposals.status）
    row = executor.ledger.execute(
        "SELECT status FROM proposals WHERE proposal_id=?", [proposal.proposal_id]).fetchone()
    if not row or row[0] != "approved":
        return ToolResult(ok=False, error={"code": "NOT_APPROVED", "retryable": False,
                          "category": "auth", "message": "账本中该提案未获人工批准"})
    # 幂等：只有 refunds 里没有该 proposal_id 才插入（主键冲突即视为已执行）
    exists = executor.ledger.execute(
        "SELECT 1 FROM refunds WHERE proposal_id=?", [proposal.proposal_id]).fetchone()
    if exists:
        return ToolResult(ok=True, data={"refund_id": proposal.proposal_id, "status": "already_executed"},
                          meta={"layer": "ledger_write", "idempotent": True})
    executor.ledger_write(
        "execute_refund", "refunds",
        "INSERT INTO refunds VALUES (?,?,?,?,?,?)",
        [proposal.proposal_id, proposal.order_id, proposal.amount,
         approval.approver, approval.decided_at.replace("T", " "), "approved"])
    return ToolResult(ok=True, data={"refund_id": proposal.proposal_id, "status": "approved"},
                      meta={"layer": "ledger_write", "approver": approval.approver, "idempotent": True})
