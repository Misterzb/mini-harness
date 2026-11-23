"""任务编排：主任务 → Task DAG，逐步推进。

步骤名即执行意图：每个步骤在 Loop 里有明确的处理分支，
verify 不再作为独立"步骤"存在（它是每一步执行后的内置动作），避免落到默认分支重复执行。
"""
from .models import Task


class Orchestrator:
    def build_task(self, intent: str, utterance: str) -> Task:
        type_map = {"query_data": "query", "diagnose": "diagnose",
                    "propose_refund": "refund", "clarify": "clarify",
                    "knowledge": "knowledge"}
        return Task(id=f"T{abs(hash(utterance)) % 100000:05d}",
                    type=type_map.get(intent, "query"), status="running")

    def plan_steps(self, task: Task) -> list[str]:
        """Task DAG：只列需要实际执行的步骤名（面试讲"任务编排"指着这里）。"""
        return {
            "query": ["llm_plan", "query_data"],
            "diagnose": ["llm_plan", "diagnose"],
            "refund": ["llm_plan", "propose_refund", "human_gate"],
            "knowledge": ["search_knowledge"],   # RAG 检索不走 LLM 规划：证据来自文档库而非模型
            "clarify": ["ask_user"],
        }.get(task.type, ["llm_plan", "query_data"])

    def all_done(self, task: Task, step_idx: int, steps: list[str]) -> bool:
        return step_idx >= len(steps)
