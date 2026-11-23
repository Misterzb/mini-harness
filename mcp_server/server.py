"""MiniHarness MCP Server —— 把 Runtime 的只读能力 + 退款提案暴露给外部 Agent。

安全边界（本次加固的核心）：
- MCP 是协议层，只能读（query_data/diagnose）、提案（propose_refund 写 proposals=pending）、
  查提案状态（refund_status 读账本）。
- MCP 里没有、也不可能有"批准"工具：批准只能由人工入口 POST /approve 写 proposals.status=approved。
  外部客户端即使传 approver/decision 参数也找不到可调用的工具；直接调 execute_refund 会被
  Executor 的 write_gated + 账本 status 双重拒绝（NOT_APPROVED）。
- 提案状态读账本 proposals 表，不读进程内单个 _ctx，避免多提案互相覆盖。

启动：python mcp_server/server.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".mcp_lib2"))
sys.path.insert(0, str(ROOT))

import duckdb
from mcp.server.fastmcp import FastMCP

from app.runtime.executor import Executor
from app.runtime.loop import Context
from app import tools  # noqa: F401 — 触发注册
from app.llm import LLM

SHOP_DB = ROOT / "data" / "shop.db"
LEDGER_DB = ROOT / "data" / "ledger.db"

mcp = FastMCP("MiniHarness")

# Executor 无状态可复用；Context 只用于工具签名兼容（propose 需要一个 ctx 挂 state）。
_shop = duckdb.connect(str(SHOP_DB), read_only=True)
_ledger = duckdb.connect(str(LEDGER_DB))
_executor = Executor(_shop, _ledger)
_ctx = Context(session_id="mcp-demo", executor=_executor, llm=LLM())


def _dump(result) -> dict:
    def enc(o):
        if hasattr(o, "model_dump"):
            return o.model_dump()
        return str(o)
    out = {"ok": result.ok}
    if result.data is not None:
        out["data"] = json.loads(json.dumps(result.data, default=enc, ensure_ascii=False))
    if result.error:
        out["error"] = result.error
    if result.meta:
        out["meta"] = result.meta
    return out


@mcp.tool()
def list_tools_info() -> dict:
    """列出本 Runtime 注册的工具及其权限属性（read_only / proposal / write_gated）。"""
    from app.runtime.executor import REGISTRY
    return {name: {"description": t["description"], "access": t["access"]}
            for name, t in REGISTRY.items()}


@mcp.tool()
async def query_data(fact: str, dimensions: list[str] | None = None,
                     filters: list[dict] | None = None, time_range: str | None = None) -> dict:
    """只读查数。fact: gmv/order_cnt/aov/buyer_cnt/discount_rate；dimension: platform/city/user_level/month/category。"""
    plan = {"fact": fact, "dimensions": dimensions or [], "filters": filters or [],
            "time_range": time_range, "intent": "query"}
    result = await _executor.execute("query_data", {"plan": plan}, _ctx)
    return _dump(result)


@mcp.tool()
async def search_knowledge(query: str, top_k: int = 3) -> dict:
    """RAG 知识检索：向量召回口径/规则文档并按出处引证；无相关依据则拒答，不查业务库。"""
    result = await _executor.execute("search_knowledge", {"query": query, "top_k": top_k}, _ctx)
    return _dump(result)


@mcp.tool()
async def diagnose(time_range: str | None = None) -> dict:
    """多步诊断：扫描相邻月找最大 GMV 降幅，点名平台 + 漏斗 + 折扣率。"""
    result = await _executor.execute("diagnose", {"plan": {"fact": "gmv", "intent": "diagnose"}}, _ctx)
    return _dump(result)


@mcp.tool()
async def propose_refund(order_id: str, amount: float, reason: str = "用户申请退款") -> dict:
    """生成退款提案，只写 proposals 账本(status=pending)，不执行退款。必须人工批准后才可结算。"""
    result = await _executor.execute(
        "propose_refund", {"order_id": order_id, "amount": amount, "reason": reason}, _ctx)
    out = _dump(result)
    if result.ok:
        out["hint"] = ("提案为 pending，未执行退款。请人工在 POST /approve 批准；"
                       "MCP 无批准工具，无法自行结算。")
    return out


@mcp.tool()
def refund_status(proposal_id: str) -> dict:
    """查询提案/退款状态（读账本，不读进程内存）。status 可能为 pending/approved/rejected。"""
    row = _ledger.execute("SELECT status FROM proposals WHERE proposal_id=?", [proposal_id]).fetchone()
    if not row:
        return {"ok": False, "error": {"code": "NOT_FOUND", "message": "提案不存在"}}
    refunded = _ledger.execute("SELECT 1 FROM refunds WHERE proposal_id=?", [proposal_id]).fetchone()
    return {"ok": True, "proposal_id": proposal_id, "status": row[0],
            "executed": bool(refunded)}


@mcp.tool()
def ledger_stats() -> dict:
    """只读统计：提案数与已执行退款数（供审计/自测，不经进程内存）。"""
    return {"proposals": _ledger.execute("SELECT count(*) FROM proposals").fetchone()[0],
            "refunds": _ledger.execute("SELECT count(*) FROM refunds").fetchone()[0]}


if __name__ == "__main__":
    print("[MiniHarness MCP] stdio server, tools=7 (read-only + RAG + propose, no approve tool)", file=sys.stderr)
    mcp.run(transport="stdio")
