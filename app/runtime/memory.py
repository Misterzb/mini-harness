"""四层记忆：L1 工作 / L2 事件 / L3 语义口径 / L4 偏好。"""
from dataclasses import dataclass, field
from typing import Any
from .models import SemanticPlan
from .budget import Budget
from .trace import Trace


@dataclass
class ConversationState:
    task: Any | None = None
    slots: dict = field(default_factory=dict)
    last_plan: SemanticPlan | None = None          # 多轮复用口径的关键
    last_intent: str = ""
    hints: list[str] = field(default_factory=list)  # 失败自省注入
    budget: Budget | None = None
    result: Any | None = None
    pending_proposal: Any | None = None

    def add_hint(self, hint: str):
        if hint and hint not in self.hints:
            self.hints.append(hint)


class Memory:
    def __init__(self, trace: Trace, budget: Budget):
        self.trace = trace
        self.budget = budget
        self.state = ConversationState(budget=budget)
        # L4：用户偏好（演示用，可落 JSON 文件）
        self.prefs: dict[str, Any] = {"default_days": 30, "default_fact": "gmv"}

    def topic_signature(self, text: str) -> str:
        """同口径不同 filter = 同 topic（简化关键词抽取，够演示）。"""
        intent = "query"
        if any(k in text for k in ("诊断", "下降", "为什么", "原因")):
            intent = "diagnose"
        return intent

    def inherit_plan(self, text: str) -> SemanticPlan | None:
        """L3 语义口径锁：追问只改 filter，不重造口径。"""
        if self.state.last_plan and self.topic_signature(text) == self.state.last_intent:
            return self.state.last_plan
        return None

    def assemble(self, utterance: str, system_prompt: str) -> list[dict]:
        inherited = self.inherit_plan(utterance)
        hints = "\n".join(f"- {h}" for h in self.state.hints[-3:])
        context_lines = []
        if inherited:
            context_lines.append(f"上一轮口径：fact={inherited.fact}, 维度={inherited.dimensions}, 过滤={[f.model_dump() for f in inherited.filters]}")
        if hints:
            context_lines.append("自省提示（上一步失败原因）：\n" + hints)
        if self.state.result:
            context_lines.append(f"上一步结果摘要：{str(self.state.result)[:400]}")
        user_content = utterance
        if context_lines:
            user_content = "\n\n[上下文]\n" + "\n".join(context_lines) + "\n\n[当前问题]\n" + utterance
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        return self.budget.trim(messages)
