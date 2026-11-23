"""工具注册表 + 单一执行出口。
访问属性 access = read_only / proposal / write_gated 是注册表元信息（Hard Controls），不是 prompt 约束。
- read_only：只读分析库，不写账本。
- proposal：只能写 proposals（提案表），不得写 refunds（结算表）。
- write_gated：只能写 refunds，且必须人工批准。
"""
import asyncio
import hashlib
import time
from typing import Callable

from ..runtime.models import ToolResult

REGISTRY: dict[str, dict] = {}

# access -> 允许写入的账本表。写操作必须走 ledger_write，受此表约束。
ACCESS_WRITE_TABLES: dict[str, set[str]] = {
    "read_only":  set(),
    "proposal":   {"proposals"},
    "write_gated": {"refunds"},
}


def register_tool(name: str, description: str, access: str = "read_only"):
    """access: read_only | proposal | write_gated（write_gated 必须人工批准后才可执行）"""
    def deco(fn: Callable):
        REGISTRY[name] = {"name": name, "description": description,
                          "access": access, "fn": fn}
        return fn
    return deco


class Executor:
    def __init__(self, con, ledger_con):
        self.con = con          # 只读分析库
        self.ledger = ledger_con  # 可写退款库

    def ledger_write(self, tool: str, table: str, sql: str, params: list):
        """受控账本写入：tool 的 access 决定能否写该表（Hard Controls 的实物证据）。

        propose_refund 标 proposal → 只能写 proposals，写 refunds 会被拦。
        execute_refund 标 write_gated → 只能写 refunds（批准校验在 execute 里已过）。
        直接绕过本方法用 executor.ledger.execute 不属于工具出口，不计入权限模型。
        """
        meta = REGISTRY.get(tool)
        access = meta["access"] if meta else "read_only"
        allowed = ACCESS_WRITE_TABLES.get(access, set())
        if table not in allowed:
            raise PermissionError(
                f"工具 {tool}(access={access}) 无权写表 {table}；允许: {sorted(allowed) or '无'}")
        return self.ledger.execute(sql, params)

    async def execute(self, name: str, args: dict, ctx=None) -> ToolResult:
        tool = REGISTRY.get(name)
        if not tool:
            return ToolResult(ok=False, error={"code": "NO_SUCH_TOOL", "retryable": False,
                              "category": "routing", "message": f"工具 {name} 未注册"})
        # 写权限闸门：write_gated 工具必须有人工批准记录
        if tool["access"] == "write_gated":
            approval = getattr(ctx, "approval", None) if ctx else None
            if not approval or approval.decision != "approve":
                return ToolResult(ok=False, error={"code": "NOT_APPROVED", "retryable": False,
                                  "category": "auth", "message": "无人工批准，禁止执行写操作"})
        started = time.time()
        try:
            result = tool["fn"](args, self, ctx)
            if asyncio.iscoroutine(result):
                result = await result
            if not isinstance(result, ToolResult):
                result = ToolResult(ok=True, data=result)
            result.meta.setdefault("latency_ms", int((time.time() - started) * 1000))
            result.meta["access"] = tool["access"]
            return result
        except Exception as e:
            return ToolResult(ok=False, error={"code": "TOOL_ERROR", "retryable": True,
                              "category": "runtime", "message": str(e)},
                              meta={"latency_ms": int((time.time() - started) * 1000)})


def digest(sql: str) -> str:
    return hashlib.md5(sql.encode("utf-8")).hexdigest()[:8]
