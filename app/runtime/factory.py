"""编排内核工厂 —— 上层零改动切换自研 Loop / LangGraph。

用法：
  MINI_HARNESS_RUNTIME=loop       自研 while 状态机（默认）
  MINI_HARNESS_RUNTIME=langgraph  LangGraph StateGraph 编排

两个内核共用 Context / Executor / steps.run_step / Verifier，
只有"循环怎么驱动"不同。工厂是这条设计结论的落点：换内核 = 换一行配置。
"""
import os

from .loop import Loop
from .langgraph_loop import LangGraphLoop


def build_runtime(executor, llm):
    name = os.getenv("MINI_HARNESS_RUNTIME", "loop").strip().lower()
    if name == "langgraph":
        return LangGraphLoop(executor, llm)
    if name == "loop":
        return Loop(executor, llm)
    raise ValueError(f"未知 MINI_HARNESS_RUNTIME={name!r}，可选: loop / langgraph")


def runtime_name() -> str:
    return os.getenv("MINI_HARNESS_RUNTIME", "loop").strip().lower()
