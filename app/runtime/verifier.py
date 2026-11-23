"""Verifier：幻觉防御 + 结果校验 + 人工授权。全场最有说服力的一幕。"""
from .models import Verdict
from ..semantic.facts import FACTS


def verify(tool: str, plan: dict, result, ctx) -> Verdict:
    checks = []

    # 检查 1：指标名必须在 FactDef 白名单（防"净利润率"这种编造指标）
    if tool in ("query_data", "diagnose"):
        fact = plan.get("fact")
        if fact and fact not in FACTS:
            checks.append({"check": "metric_whitelist", "ok": False, "value": fact,
                           "hint": f"可用指标: {list(FACTS)}"})

    # 检查 2：工具执行成功
    if not result.ok:
        checks.append({"check": "tool_result", "ok": False,
                       "reason": (result.error or {}).get("message", "工具失败")})

    # 检查 3：结果非空（防"空结果硬编答案"）
    if tool in ("query_data", "diagnose") and result.ok and not result.data:
        checks.append({"check": "nonempty_evidence", "ok": False,
                       "reason": "查询无数据，不得生成结论"})

    # 检查 2b：RAG 无依据拒答不可重试（再检索一次结果一样，浪费预算）
    if tool == "search_knowledge" and not result.ok:
        code = (result.error or {}).get("code")
        if code == "NO_GROUNDED_EVIDENCE":
            return Verdict(ok=False, retryable=False,
                           reason=(result.error or {}).get("message", "知识库无依据"),
                           checks=[{"check": "grounded_retrieval", "ok": False, "code": code}])

    # 检查 4：写操作必须有人工批准（Hard Control，不可绕过）
    if tool == "execute_refund":
        approval = getattr(ctx, "approval", None)
        if not approval or approval.decision != "approve":
            checks.append({"check": "human_authorization", "ok": False,
                           "reason": "退款执行无人工批准记录，Hard Control 拒绝"})

    failed = [c for c in checks if not c["ok"]]
    if not failed:
        return Verdict(ok=True, checks=checks,
                       evidence_refs=[result.meta.get("query_digest")] if result.meta.get("query_digest") else [])

    first = failed[0]
    # 安全类（人授权/字段越权）不可重试；指标编造/空结果可回环纠正
    retryable = first["check"] in {"metric_whitelist", "nonempty_evidence", "tool_result"}
    return Verdict(ok=False, checks=checks, retryable=retryable,
                   reason=first.get("hint") or first.get("reason", "校验失败"))
