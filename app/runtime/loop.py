"""Agent Loop 主状态机 —— 整个作品的心脏。

固定循环 OBSERVE→SCORE→PLAN→PRECHECK→EXECUTE→VERIFY→TRACE。
- 按步骤名执行：每个步骤在 run 里有明确分支，工具步按步骤名从注册表取，不用 ctx.tool 代替。
- 执行前校验：未知指标/非法字段在 EXECUTE 之前拦下，记失败轨迹，不查库不写账本。
- 失败只重规划一次：第二次仍失败就拒答，不在 for 里改 steps 列表。
- 成功后写 last_plan：下一轮没有新指标名时继承 fact/时间，只覆盖新 filter。
- while + 索引推进；退款到 human_gate 挂起，/approve 走 resume 续跑。
"""
import json
import re
from collections import OrderedDict

from ..llm import LLM
from ..semantic.facts import FACTS
from ..semantic.normalize import (normalize_plan, user_names_fact, constrain_utterance_time,
                                  constrain_utterance_fact, constrain_utterance_dimensions)
from .models import StepRecord, Verdict, Approval, Task, SemanticPlan, Filter
from .executor import Executor
from .router import decide
from .orchestrator import Orchestrator
from .verifier import verify
from .budget import Budget
from .memory import Memory, ConversationState
from .trace import Trace

MAX_STEPS = 8
MAX_CONSECUTIVE_FAIL = 2

SYSTEM_PROMPT = """你是电商运营分析 Agent。只输出 JSON，不要多余文字。
可选事实(fact)：gmv, order_cnt, aov, buyer_cnt, discount_rate
可选维度(dimensions)：platform, city, user_level, month, category
可选过滤(filters)：{"field": "platform|city", "value": "..."}
输出格式：{"fact": "...", "intent": "query|diagnose", "dimensions": [], "filters": []}
用户要退款时输出：{"tool":"propose_refund","order_id":"...","amount":数字,"reason":"..."}
你只能选择给定枚举内的值，编造不存在的指标会被系统拦截。"""


class Context:
    """一次请求的运行时上下文，串起所有组件。"""
    def __init__(self, session_id: str, executor: Executor, llm: LLM):
        self.session_id = session_id
        self.trace = Trace(session_id)
        self.budget = Budget()
        self.memory = Memory(self.trace, self.budget)
        self.state: ConversationState = self.memory.state
        self.executor = executor
        self.llm = llm
        self.approval: Approval | None = None
        self.tasks: dict[str, Task] = OrderedDict()
        self.utterance = ""
        self.plan: dict = {}
        self.tool = ""

    def save_state(self):
        """会话状态落盘（原子替换），供进程重启后恢复。"""
        st = self.state
        self.trace.save_state({
            "last_plan": st.last_plan.model_dump() if st.last_plan else None,
            "last_intent": st.last_intent,
            "hints": st.hints,
            "pending_proposal": st.pending_proposal.model_dump() if st.pending_proposal else None,
            "result": _safe(st.result),
            "budget_used": self.budget.used,
            "tasks": {tid: _safe(t) for tid, t in self.tasks.items()},
        })

    def restore_state(self) -> bool:
        """从磁盘恢复会话状态；成功返回 True。"""
        data = self.trace.load_state()
        if not data:
            return False
        if data.get("last_plan"):
            self.state.last_plan = SemanticPlan(**data["last_plan"])
        self.state.last_intent = data.get("last_intent", "")
        self.state.hints = data.get("hints", [])
        if data.get("pending_proposal"):
            from .models import RefundProposal
            self.state.pending_proposal = RefundProposal(**data["pending_proposal"])
        self.state.result = data.get("result")
        self.budget.used = data.get("budget_used", 0)
        if data.get("tasks"):
            self.tasks = OrderedDict(
                (tid, Task(**t)) for tid, t in data["tasks"].items())
        return True


class Loop:
    def __init__(self, executor: Executor, llm: LLM):
        self.executor = executor
        self.llm = llm
        self.orchestrator = Orchestrator()

    async def _llm_plan(self, ctx: Context, utterance: str) -> dict:
        messages = ctx.memory.assemble(utterance, SYSTEM_PROMPT)
        text, usage = await self.llm.chat(messages, task_type=ctx.state.last_intent or "query")
        ctx.budget.charge(usage)
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return normalize_plan(data)
        except Exception:
            pass
        return {"_parse_error": text[:120]}

    def _inherit(self, ctx: Context, plan: dict, utterance: str) -> dict:
        """多轮口径继承：fact/时间/filter 各自判断。
        - 用户点名了新指标 → 用新 fact；否则沿用 last_plan.fact。
        - 新 filter 优先；没有新 filter 时沿用 last_plan.filter（改指标也保留平台等上下文）。
        """
        last = ctx.state.last_plan
        if not last or plan.get("tool"):
            return plan
        merged = dict(plan)
        if not user_names_fact(utterance):
            merged["fact"] = last.fact
            merged["time_range"] = plan.get("time_range") or last.time_range
        if not plan.get("filters"):
            merged["filters"] = [f.model_dump() for f in last.filters]
        return normalize_plan(merged)

    def _as_semantic_plan(self, plan: dict) -> SemanticPlan:
        return SemanticPlan(
            fact=plan.get("fact") or "",
            dimensions=[d for d in (plan.get("dimensions") or [])],
            filters=[Filter(**f) if isinstance(f, dict) else f for f in (plan.get("filters") or [])],
            time_range=plan.get("time_range"),
            intent=plan.get("intent", "query") if plan.get("intent") in ("query", "diagnose") else "query",
        )

    async def _precheck(self, step_name: str, ctx: Context) -> Verdict | None:
        """执行前校验：拦住未知指标/非法字段/未批准写，不查库不写账本。

        返回 None 表示放行；返回 Verdict 表示拦截。
        """
        if step_name in ("query_data", "diagnose"):
            fact = ctx.plan.get("fact")
            # 模型显式拒答（如真实模型识别指标不可算）：直接拦，不进执行、不查库
            if ctx.plan.get("error"):
                return Verdict(ok=False, retryable=False,
                               reason=str(ctx.plan.get("message") or ctx.plan["error"]),
                               checks=[{"check": "model_declined", "ok": False,
                                        "error": ctx.plan["error"]}])
            if not fact:
                return Verdict(ok=False, retryable=False, reason="缺少有效指标，拒绝凭空查询",
                               checks=[{"check": "metric_whitelist", "ok": False, "value": fact}])
            if fact not in FACTS:
                return Verdict(ok=False, retryable=True,
                               reason=f"未知指标 {fact}，可用: {list(FACTS)}",
                               checks=[{"check": "metric_whitelist", "ok": False, "value": fact}])
            from ..semantic.compiler import FIELD_MAP
            used = set(ctx.plan.get("dimensions") or []) | {f.get("field") for f in (ctx.plan.get("filters") or []) if isinstance(f, dict)}
            bad = used - set(FIELD_MAP)
            if bad:
                return Verdict(ok=False, retryable=True, reason=f"字段不在白名单: {sorted(bad)}",
                               checks=[{"check": "field_whitelist", "ok": False, "fields": sorted(bad)}])
        return None

    async def run(self, utterance: str, ctx: Context, emit) -> dict:
        ctx.utterance = utterance
        intent = decide(utterance, ctx.state.last_intent, bool(ctx.state.pending_proposal))
        task = None if intent == "clarify" else self.orchestrator.build_task(intent, utterance)
        steps = self.orchestrator.plan_steps(task) if task else ["ask_user"]
        if task:
            ctx.tasks[task.id] = task
        ctx.state.last_intent = intent

        if intent in ("approve", "reject"):
            return {"status": "need_approve_endpoint"}

        replanned = False           # 失败只重规划一次
        last_result = None
        step_idx = 0
        while step_idx < len(steps):
            if step_idx + 1 > MAX_STEPS:
                return await self._finish(ctx, emit, "max_steps", last_result)
            if ctx.budget.available() <= 0:
                return await self._finish(ctx, emit, "budget_exhausted", last_result)

            step_name = steps[step_idx]           # 当前步骤名 —— 唯一依据
            observed = f"step={step_name}; task={task.type if task else 'clarify'}"
            verdict: Verdict | None = None
            result = None

            # ---- 规划 ----
            if step_name == "ask_user":
                rec = StepRecord(step=step_idx + 1, event="clarify", observed=observed,
                                 decision="clarify", result={"question": "请说明要查询的指标或要执行的操作"},
                                 tokens=ctx.budget.step_usage())
                ctx.trace.append(rec); await emit(rec.model_dump())
                return {"status": "clarify", "question": "请说明要查询的指标或要执行的操作"}

            if step_name == "llm_plan":
                plan = await self._llm_plan(ctx, utterance)
                if plan.get("tool") == "propose_refund" and task and task.type != "refund":
                    task.type = "refund"
                    steps = self.orchestrator.plan_steps(task)   # 动态纠正编排
                plan = self._inherit(ctx, plan, utterance)        # 多轮口径继承
                # 原话相对时间优先：mock 与真实模型都走同一道约束，覆盖模型漏填/填错的
                # time_range，并去掉模型误加的 month 维度（"上个月GMV"要单月合计不是12行）。
                plan = constrain_utterance_time(plan, utterance)
                # 真实模型全量评测暴露的两处偷换：未知指标映射成 gmv、按品类被重规划丢掉。
                # 与相对时间同理，原话点名始终高于模型计划，重规划后再约束一次。
                plan = constrain_utterance_fact(plan, utterance)
                plan = constrain_utterance_dimensions(plan, utterance)
                ctx.plan = plan
                declined = bool(plan.get("error"))
                bad = "_parse_error" in plan
                rec = StepRecord(step=step_idx + 1, observed=observed, decision="llm_plan",
                                 tool="llm_plan", result=plan,
                                 verdict={"ok": not bad and not declined}, tokens=ctx.budget.step_usage())
                ctx.trace.append(rec); await emit(rec.model_dump())
                if declined:
                    # 模型已显式拒绝编造 → 立即拒答，不进任何执行/查询步骤
                    reason = str(plan.get("message") or plan["error"])
                    final = StepRecord(step=step_idx + 1, event="final", decision="refuse",
                                       result={"reason": reason, "stage": "llm_plan"},
                                       tokens=ctx.budget.step_usage())
                    ctx.trace.append(final); await emit(final.model_dump())
                    ctx.save_state()
                    return {"status": "refuse", "reason": reason}
                if bad:
                    verdict = Verdict(ok=False, retryable=True, reason="LLM 输出无法解析为 JSON")
                else:
                    step_idx += 1
                    continue

            # ---- 执行前校验（未知指标/非法字段不查库）----
            elif step_name in ("query_data", "diagnose"):
                verdict = await self._precheck(step_name, ctx)
                if verdict is None:
                    sp = self._as_semantic_plan(ctx.plan)
                    result = await self.executor.execute(step_name, {"plan": sp}, ctx)
                    verdict = verify(step_name, ctx.plan, result, ctx)
                    if result.ok:
                        ctx.state.result = result.data
                        ctx.state.last_plan = sp                  # 成功才写 last_plan

            elif step_name == "propose_refund":
                # 退款提案也走注册表，参数在顶层
                args = {"order_id": ctx.plan.get("order_id", ""),
                        "amount": ctx.plan.get("amount", 0),
                        "reason": ctx.plan.get("reason", "用户申请退款")}
                result = await self.executor.execute("propose_refund", args, ctx)
                verdict = verify("propose_refund", ctx.plan, result, ctx)

            elif step_name == "search_knowledge":
                # RAG 检索：不经 LLM 规划，证据来自文档库；结果直接写 state 供界面展示
                result = await self.executor.execute("search_knowledge",
                                                     {"query": utterance}, ctx)
                verdict = verify("search_knowledge", ctx.plan, result, ctx)
                if result.ok:
                    ctx.state.result = result.data

            elif step_name == "human_gate":
                proposal = ctx.state.pending_proposal
                if not proposal:
                    return await self._finish(ctx, emit, "no_proposal", None)
                if task:
                    task.status = "await_human"
                rec = StepRecord(step=step_idx + 1, event="human_gate", observed=observed,
                                 decision="await_human", tool="human_gate",
                                 result={"proposal": proposal.model_dump()},
                                 tokens=ctx.budget.step_usage())
                ctx.trace.append(rec); await emit(rec.model_dump())
                ctx.save_state()   # 挂起点落盘：重启后仍能批准
                return {"status": "await_human", "proposal": proposal.model_dump(),
                        "task_id": task.id if task else ""}

            else:
                # 未知步骤：不猜工具，直接记失败
                verdict = Verdict(ok=False, retryable=False, reason=f"未知步骤 {step_name}")

            # ---- TRACE ----
            rec = StepRecord(step=step_idx + 1, observed=observed,
                             decision=step_name, tool=step_name if result is not None else None,
                             result=_safe(result), verdict=verdict.model_dump() if verdict else None,
                             state_delta={"fact": ctx.plan.get("fact"), "step": step_name},
                             tokens=ctx.budget.step_usage())
            ctx.trace.append(rec); await emit(rec.model_dump())
            last_result = result

            # ---- 状态判定 ----
            if verdict and verdict.ok:
                step_idx += 1
                continue

            # 失败：记错误轨迹，决定是否重规划一次
            ctx.budget.charge_penalty()
            reason = verdict.reason if verdict else "未知错误"
            err = StepRecord(step=step_idx + 1, event="error", decision="step_failed",
                             result={"step": step_name, "reason": reason},
                             verdict=verdict.model_dump() if verdict else None,
                             tokens=ctx.budget.step_usage())
            ctx.trace.append(err); await emit(err.model_dump())

            if (verdict and not verdict.retryable) or replanned:
                refuse = StepRecord(step=step_idx + 1, event="final", decision="refuse",
                                    result={"reason": reason}, tokens=ctx.budget.step_usage())
                ctx.trace.append(refuse); await emit(refuse.model_dump())
                ctx.save_state()
                return {"status": "refuse", "reason": reason}
            # 只重规划一次：回退到 llm_plan，注入自省 hint
            replanned = True
            ctx.state.add_hint(reason)
            step_idx = 0

        return await self._finish(ctx, emit, "completed", last_result)

    async def resume(self, task_id: str, approval: Approval, ctx: Context, emit) -> dict:
        """人工决策回写：鉴权后续跑同一 Loop（挂起/恢复是一等公民能力）。"""
        task = ctx.tasks.get(task_id) if task_id else None
        if not task:
            # 磁盘恢复后 task_id 可能丢失 → 从挂起状态的 task 反查
            task = next((t for t in ctx.tasks.values() if t.status == "await_human"), None)
        if not task and ctx.tasks:
            task = next(reversed(list(ctx.tasks.values())), None)
        proposal = ctx.state.pending_proposal
        rec = StepRecord(step=len(ctx.trace.steps) + 1, event="human_decision",
                         decision=f"human_{approval.decision}",
                         result={"proposal_id": approval.proposal_id, "approver": approval.approver,
                                 "comment": approval.comment, "decided_at": approval.decided_at},
                         tokens=ctx.budget.step_usage())
        ctx.trace.append(rec); await emit(rec.model_dump())
        if approval.decision == "reject":
            if task:
                task.status = "rejected"
            ctx.state.pending_proposal = None
            return await self._finish(ctx, emit, "human_rejected", {"proposal_id": approval.proposal_id})
        if not proposal or proposal.proposal_id != approval.proposal_id:
            return await self._finish(ctx, emit, "proposal_mismatch", None)
        if proposal.risk_level == "high" and len(approval.comment.strip()) < 2:
            return await self._finish(ctx, emit, "need_comment", {"reason": "高风险退款需填写审批备注"})
        ctx.approval = approval
        if task:
            task.status = "running"
            result = await self.executor.execute("execute_refund", {}, ctx)
            verdict = verify("execute_refund", ctx.plan, result, ctx)
            r = StepRecord(step=len(ctx.trace.steps) + 1, decision="execute_refund",
                           tool="execute_refund", result=_safe(result),
                           verdict=verdict.model_dump(), tokens=ctx.budget.step_usage())
            ctx.trace.append(r); await emit(r.model_dump())
            if result.ok:
                ctx.state.pending_proposal = None
                ctx.approval = None
                task.status = "done"
                return await self._finish(ctx, emit, "completed", result.data)
            return await self._finish(ctx, emit, "execute_failed", (result.error or {}))
        return await self._finish(ctx, emit, "task_lost", None)

    async def _finish(self, ctx: Context, emit, reason: str, data) -> dict:
        rec = StepRecord(step=len(ctx.trace.steps) + 1, event="final", decision="finish",
                         result={"reason": reason, "data": _safe(data)},
                         tokens=ctx.budget.step_usage())
        ctx.trace.append(rec)
        await emit(rec.model_dump())
        ctx.save_state()
        return {"status": reason, "data": _safe(data), "trace": str(ctx.trace.path),
                "tokens": ctx.budget.step_usage()}


def _safe(obj):
    """把 Pydantic 对象转成可序列化 dict。"""
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, (list, tuple)):
        return [_safe(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _safe(v) for k, v in obj.items()}
    return obj
