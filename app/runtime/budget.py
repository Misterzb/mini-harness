"""Token 预算与上下文裁剪：预算既是省钱手段，也是终止条件之一。"""
from dataclasses import dataclass, field


@dataclass
class Budget:
    total: int = 8000
    reserve: int = 1200
    per_step: int = 1500
    used: int = 0
    step_log: list[dict] = field(default_factory=list)

    def available(self) -> int:
        return self.total - self.used - self.reserve

    def reset(self):
        """每轮用户请求重置用量。预算约束的是"单次 Agent 循环"，不是会话终身额度：
        否则多轮对话第 4~5 轮必触发 budget_exhausted，真实 UI 里表现为越用越答不出。
        单轮内耗尽仍会终止（available<=0 的终止检查不变）。"""
        self.used = 0
        self.step_log.clear()

    def charge(self, usage: dict):
        cost = int(usage.get("prompt", 0) + usage.get("completion", 0))
        self.used += cost
        self.step_log.append({"step": len(self.step_log) + 1, **usage})

    def charge_penalty(self, n: int = 80):
        self.used += n                      # 失败回环也计入成本

    def step_usage(self) -> dict:
        return {"used": self.used, "available": self.available(), "calls": len(self.step_log)}

    def trim(self, messages: list[dict]) -> list[dict]:
        """system 保留，历史从旧到新按预算丢弃，末条用户消息必留。"""
        if len(messages) <= 2:
            return messages
        kept = [messages[0]]
        tokens = len(str(messages[0]))
        for m in reversed(messages[1:-1]):
            size = len(str(m))
            if tokens + size > self.available():
                break
            kept.insert(1, m)
            tokens += size
        kept.append(messages[-1])
        return kept
