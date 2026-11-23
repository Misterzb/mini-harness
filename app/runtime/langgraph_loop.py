"""LangGraph 编排内核 —— 与自研 Loop 并列的第二种编排方式。

核心论点：LangGraph 是"编排层的一层皮"，不是另一个 Harness。
- 两种内核共用 Context / Executor / Router / Orchestrator / steps.run_step / Verifier；
- 差别只在"循环怎么驱动"：自研 Loop 用 while + step_idx，本内核用 StateGraph + 条件边；
- 因此同一条金标在两个内核上跑，结果必须逐字一致——一致性由 test_dual_runtime 保证。

LangGraph 在这里提供的真实价值：
1. 显式状态机：节点/边可可视化，循环与重试路径一目了然；
2. 条件边把"成功推进 / 失败重规划一次 / 终结"表达成图，而不是散落在 while 里的 if；
3. 若接入真正的检查点后端，可把编排状态也托管给框架。

不使用 checkpointer：本项目的挂起恢复走 Context.save_state()（账本状态 + 多轮口径），
与框架检查点重复且会分叉。这里刻意让框架只负责编排，不接管业务状态。

并发安全：ctx/emit 作为 state 字段随每次 ainvoke 流转，不放实例属性。
"""
from typing import TypedDict, Any, Callable
from types import MethodType

from langgraph.graph import StateGraph, END

from .models import StepRecord
from .steps import run_step
from .router import decide
from .executor import Executor
from .orchestrator import Orchestrator
from .loop import Context, Loop, MAX_STEPS
from ..llm import LLM


class GraphState(TypedDict):
    """图上流转的状态。业务状态在 ctx 里，这里只放编排控制量 + 请求级 io。"""
    utterance: str
    ctx: Context
    emit: Callable
    task: Any
    steps: list[str]
    step_idx: int
    replanned: bool
    last_result: Any
    response: dict | None
    finalized: bool


class LangGraphLoop:
    """与 Loop 同接口的编排内核：run() / resume() 一模一样，上层零改动。"""

    def __init__(self, executor: Executor, llm: LLM):
        self.executor = executor
        self.llm = llm
        self.orchestrator = Orchestrator()
        # 共享能力绑定到本内核（run_step 通过 loop.<method> 调用，签名与 Loop 一致）
        self._llm_plan = MethodType(Loop._llm_plan, self)
        self._inherit = MethodType(Loop._inherit, self)
        self._as_semantic_plan = MethodType(Loop._as_semantic_plan, self)
        self._precheck = MethodType(Loop._precheck, self)
        self._finish = MethodType(Loop._finish, self)
        self.graph = self._build()

    # ------------------------------------------------------------ 图节点
    async def _route(self, state: GraphState) -> dict:
        ctx = state["ctx"]
        intent = decide(state["utterance"], ctx.state.last_intent,
                        bool(ctx.state.pending_proposal))
        task = None if intent == "clarify" else self.orchestrator.build_task(
            intent, state["utterance"])
        steps = self.orchestrator.plan_steps(task) if task else ["ask_user"]
        if task:
            ctx.tasks[task.id] = task
        ctx.state.last_intent = intent
        if intent in ("approve", "reject"):
            return {"response": {"status": "need_approve_endpoint"}, "finalized": True}
        return {"task": task, "steps": steps, "step_idx": 0}

    async def _step(self, state: GraphState) -> dict:
        ctx, emit = state["ctx"], state["emit"]
        # 步骤全部走完 → completed（对应自研 Loop while 正常退出后的 _finish）
        if state["step_idx"] >= len(state["steps"]):
            result = await self._finish(ctx, emit, "completed", state["last_result"])
            return {"response": result, "finalized": True}
        # while 顶部的两道终止检查（与自研 Loop 等价）
        if state["step_idx"] + 1 > MAX_STEPS:
            result = await self._finish(ctx, emit, "max_steps", state["last_result"])
            return {"response": result, "finalized": True}
        if ctx.budget.available() <= 0:
            result = await self._finish(ctx, emit, "budget_exhausted", state["last_result"])
            return {"response": result, "finalized": True}

        step_name = state["steps"][state["step_idx"]]
        # 共享步骤语义 —— 与自研 Loop 完全同一份实现
        outcome = await run_step(self, step_name, ctx, state["utterance"],
                                 state["task"], state["step_idx"], emit)
        patch: dict = {"last_result": outcome.result}
        if outcome.steps is not None:
            patch["steps"] = outcome.steps
        if outcome.response is not None:
            patch["response"] = outcome.response
            patch["finalized"] = True
            return patch
        if outcome.advanced:
            patch["step_idx"] = state["step_idx"] + 1
            return patch

        verdict = outcome.verdict
        result = outcome.result
        observed = (f"step={step_name}; "
                    f"task={state['task'].type if state['task'] else 'clarify'}")
        rec = StepRecord(
            step=state["step_idx"] + 1, observed=observed, decision=step_name,
            tool=step_name if result is not None else None,
            result=_safe(result), verdict=verdict.model_dump() if verdict else None,
            state_delta={"fact": ctx.plan.get("fact"), "step": step_name},
            tokens=ctx.budget.step_usage())
        ctx.trace.append(rec)
        await emit(rec.model_dump())

        if verdict and verdict.ok:
            patch["step_idx"] = state["step_idx"] + 1
            return patch

        # 失败：记错误轨迹 + 预算惩罚（与自研 Loop 一致）
        ctx.budget.charge_penalty()
        reason = verdict.reason if verdict else "未知错误"
        err = StepRecord(step=state["step_idx"] + 1, event="error", decision="step_failed",
                         result={"step": step_name, "reason": reason},
                         verdict=verdict.model_dump() if verdict else None,
                         tokens=ctx.budget.step_usage())
        ctx.trace.append(err)
        await emit(err.model_dump())

        if (verdict and not verdict.retryable) or state["replanned"]:
            refuse = StepRecord(step=state["step_idx"] + 1, event="final", decision="refuse",
                                result={"reason": reason}, tokens=ctx.budget.step_usage())
            ctx.trace.append(refuse)
            await emit(refuse.model_dump())
            ctx.save_state()
            patch["response"] = {"status": "refuse", "reason": reason}
            patch["finalized"] = True
            return patch
        ctx.state.add_hint(reason)
        patch["replanned"] = True
        patch["step_idx"] = 0          # 回退到 llm_plan 重规划（只一次）
        return patch

    async def _finish_node(self, state: GraphState) -> dict:
        result = await self._finish(state["ctx"], state["emit"], "completed",
                                    state["last_result"])
        return {"response": result, "finalized": True}

    # ------------------------------------------------------------ 条件边
    @staticmethod
    def _after_route(state: GraphState) -> str:
        return "__end__" if state.get("finalized") else "step"

    @staticmethod
    def _after_step(state: GraphState) -> str:
        """成功/重规划都回到 step 节点自循环；终结走 END 或 finish。"""
        if state.get("finalized"):
            return "__end__"
        # 正常推进到最后一步之后，下一步由 _step 顶部的 MAX_STEPS 检查收口；
        # 这里统一回到 step，让终止逻辑集中在节点内（单一真相源）。
        return "step"

    def _build(self):
        b = StateGraph(GraphState)
        b.add_node("route", self._route)
        b.add_node("step", self._step)
        b.add_node("finish", self._finish_node)
        b.set_entry_point("route")
        b.add_conditional_edges("route", self._after_route,
                                {"step": "step", "__end__": END})
        b.add_conditional_edges("step", self._after_step,
                                {"step": "step", "__end__": END})
        return b.compile()

    # ------------------------------------------------------------ 公共接口（与 Loop 一致）
    async def run(self, utterance: str, ctx: Context, emit) -> dict:
        state: GraphState = {
            "utterance": utterance, "ctx": ctx, "emit": emit, "task": None,
            "steps": [], "step_idx": 0, "replanned": False, "last_result": None,
            "response": None, "finalized": False,
        }
        final = await self.graph.ainvoke(state)
        return final.get("response") or {"status": "completed"}

    async def resume(self, task_id: str, approval, ctx: Context, emit) -> dict:
        """人工决策后续跑：复用 Loop.resume（挂起恢复不是编排差异，无需另写）。"""
        return await Loop(self.executor, self.llm).resume(task_id, approval, ctx, emit)


def _safe(obj):
    from .loop import _safe as loop_safe
    return loop_safe(obj)
