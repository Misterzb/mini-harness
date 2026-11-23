"""共享步骤执行层 —— 自研 Loop 与 LangGraph 内核共用的业务动作。

设计要点（也是本项目对"LangGraph 只是编排层"的代码级证明）：
- 编排方式（while 循环 / LangGraph StateGraph）是可替换的皮；
- 下面的步骤语义（路由 → 规划 → 原话约束 → 预检 → 执行 → 校验 → 挂起）才是 Harness 内核；
- 两个内核调用同一个 run_step()，行为逐字一致，评测结果没有"两套口径"的风险。

本模块不持有循环状态：step_idx / replanned 等由调用方（编排层）维护，
StepOutcome 只描述"这一步发生了什么"以及"编排层下一步该怎么走"。
"""
from dataclasses import dataclass
from typing import Any

from .models import StepRecord, Verdict
from .verifier import verify
from ..semantic.normalize import (
    constrain_utterance_time, constrain_utterance_fact, constrain_utterance_dimensions,
)


@dataclass
class StepOutcome:
    """单步执行结果。编排层据此推进 / 重规划 / 终结。"""
    verdict: Verdict | None = None      # 校验裁决（query/diagnose/refund/knowledge 步）
    result: Any = None                  # ToolResult，供编排层写通用 TRACE
    response: dict | None = None        # 非 None = 本步已终结整轮（refuse/clarify/await_human）
    steps: list[str] | None = None      # 非 None = 编排步骤被动态改写（如 llm_plan 纠正为退款）
    advanced: bool = False              # True = 本步已自行 emit 轨迹，编排层只需 step_idx+1


async def run_step(loop, step_name: str, ctx, utterance: str, task,
                   step_idx: int, emit) -> StepOutcome:
    """执行一个语义步骤。loop 提供共享能力（llm_plan/inherit/precheck/executor/finish），
    但不决定是否循环——那是编排层的事。"""
    observed = f"step={step_name}; task={task.type if task else 'clarify'}"

    # ---- 澄清 ----
    if step_name == "ask_user":
        question = "请说明要查询的指标或要执行的操作"
        rec = StepRecord(step=step_idx + 1, event="clarify", observed=observed,
                         decision="clarify", result={"question": question},
                         tokens=ctx.budget.step_usage())
        ctx.trace.append(rec)
        await emit(rec.model_dump())
        return StepOutcome(response={"status": "clarify", "question": question})

    # ---- 规划：LLM 出 SemanticPlan + 用户原话约束链（两道防偷换的关键闸）----
    if step_name == "llm_plan":
        plan = await loop._llm_plan(ctx, utterance)
        new_steps = None
        if plan.get("tool") == "propose_refund" and task and task.type != "refund":
            task.type = "refund"
            new_steps = loop.orchestrator.plan_steps(task)   # 动态纠正编排
        plan = loop._inherit(ctx, plan, utterance)            # 多轮口径继承
        plan = constrain_utterance_time(plan, utterance)
        plan = constrain_utterance_fact(plan, utterance)
        plan = constrain_utterance_dimensions(plan, utterance)
        ctx.plan = plan
        declined = bool(plan.get("error"))
        bad = "_parse_error" in plan
        rec = StepRecord(step=step_idx + 1, observed=observed, decision="llm_plan",
                         tool="llm_plan", result=plan,
                         verdict={"ok": not bad and not declined},
                         tokens=ctx.budget.step_usage())
        ctx.trace.append(rec)
        await emit(rec.model_dump())
        if declined:
            reason = str(plan.get("message") or plan["error"])
            final = StepRecord(step=step_idx + 1, event="final", decision="refuse",
                               result={"reason": reason, "stage": "llm_plan"},
                               tokens=ctx.budget.step_usage())
            ctx.trace.append(final)
            await emit(final.model_dump())
            ctx.save_state()
            return StepOutcome(response={"status": "refuse", "reason": reason}, steps=new_steps)
        if bad:
            return StepOutcome(
                verdict=Verdict(ok=False, retryable=True, reason="LLM 输出无法解析为 JSON"),
                steps=new_steps)
        return StepOutcome(advanced=True, steps=new_steps)

    # ---- 查询 / 诊断：预检 → 执行 Registry → 校验（未知指标不查库）----
    if step_name in ("query_data", "diagnose"):
        verdict = await loop._precheck(step_name, ctx)
        result = None
        if verdict is None:
            sp = loop._as_semantic_plan(ctx.plan)
            result = await loop.executor.execute(step_name, {"plan": sp}, ctx)
            verdict = verify(step_name, ctx.plan, result, ctx)
            if result.ok:
                ctx.state.result = result.data
                ctx.state.last_plan = sp
        return StepOutcome(verdict=verdict, result=result)

    # ---- 退款提案：走 Registry，access=proposal 只能写 proposals ----
    if step_name == "propose_refund":
        args = {"order_id": ctx.plan.get("order_id", ""),
                "amount": ctx.plan.get("amount", 0),
                "reason": ctx.plan.get("reason", "用户申请退款")}
        result = await loop.executor.execute("propose_refund", args, ctx)
        return StepOutcome(verdict=verify("propose_refund", ctx.plan, result, ctx),
                           result=result)

    # ---- RAG 知识检索：不经 LLM 规划，证据来自文档库 ----
    if step_name == "search_knowledge":
        result = await loop.executor.execute("search_knowledge", {"query": utterance}, ctx)
        verdict = verify("search_knowledge", ctx.plan, result, ctx)
        if result.ok:
            ctx.state.result = result.data
        return StepOutcome(verdict=verdict, result=result)

    # ---- 人工闸门：挂起点落盘，进程重启后仍可批准 ----
    if step_name == "human_gate":
        proposal = ctx.state.pending_proposal
        if not proposal:
            return StepOutcome(response={"status": "no_proposal", "data": None})
        if task:
            task.status = "await_human"
        rec = StepRecord(step=step_idx + 1, event="human_gate", observed=observed,
                         decision="await_human", tool="human_gate",
                         result={"proposal": proposal.model_dump()},
                         tokens=ctx.budget.step_usage())
        ctx.trace.append(rec)
        await emit(rec.model_dump())
        ctx.save_state()
        return StepOutcome(response={
            "status": "await_human", "proposal": proposal.model_dump(),
            "task_id": task.id if task else ""})

    # ---- 未知步骤不猜工具 ----
    return StepOutcome(verdict=Verdict(ok=False, retryable=False,
                                       reason=f"未知步骤 {step_name}"))
