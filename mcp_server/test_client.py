"""MCP 自测：真实 MCP 客户端 stdio 连接，验证"协议只读 + 人工批准"边界。

验收点：
1. 工具列表含只读+提案，无 approve_and_execute_refund / execute_refund（MCP 无批准/执行能力）
2. 查数 / 诊断 / 提案成功
3. 提案后 refunds 仍 0 行（MCP 无法自批结算）
4. 调用与 POST /approve 相同的人工函数批准 → 执行 → refunds 1 行
5. 重复执行仍 1 行（幂等）
"""
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".mcp_lib2"))
sys.path.insert(0, str(ROOT))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PY = sys.executable
SERVER = str(ROOT / "mcp_server" / "server.py")


def count_ledger(led) -> int:
    """用已打开的写连接查行数（避免同文件多配置连接冲突）。"""
    return led.execute("SELECT count(*) FROM refunds").fetchone()[0]


def extract_proposal_id(s: str) -> str | None:
    m = re.search(r'"proposal_id"\s*:\s*"(RF[0-9a-f]+)"', s)
    return m.group(1) if m else None


async def main():
    params = StdioServerParameters(command=PY, args=[SERVER], cwd=str(ROOT))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = [t.name for t in (await session.list_tools()).tools]
            print("[1] 工具列表:", names)
            assert "query_data" in names and "propose_refund" in names and "refund_status" in names
            assert "approve_and_execute_refund" not in names, "MCP 不应有自批工具"
            assert "execute_refund" not in names, "MCP 不应有执行工具"

            q = await session.call_tool("query_data", {"fact": "gmv", "dimensions": ["platform"]})
            print("[2] 查数 ok:", json.loads(q.content[0].text)["ok"])

            d = await session.call_tool("diagnose", {})
            ddata = json.loads(d.content[0].text)
            print("[3] 诊断:", ddata["data"].get("worst_platform"),
                  ddata["data"].get("prev_month"), "->", ddata["data"].get("cur_month"))

            p = await session.call_tool("propose_refund",
                                        {"order_id": "O000123", "amount": 88.0, "reason": "MCP 自测"})
            pid = extract_proposal_id(p.content[0].text)
            stats = json.loads((await session.call_tool("ledger_stats", {})).content[0].text)
            print("[4] 提案:", pid, "账本:", stats)
            assert pid and stats["refunds"] == 0, "MCP 提案后不得写 refunds"

            st = await session.call_tool("refund_status", {"proposal_id": pid})
            status = json.loads(st.content[0].text)["status"]
            print("[5] 提案状态:", status)
            assert status == "pending"

    # 退出 MCP 会话：以下为人工入口（与 POST /approve 内部完全相同的函数）
    import duckdb
    from app.runtime.executor import Executor
    from app.runtime.models import Approval, RefundProposal
    from app.tools.builtin import approve_in_ledger
    shop = duckdb.connect(str(ROOT / "data" / "shop.db"), read_only=True)
    led = duckdb.connect(str(ROOT / "data" / "ledger.db"))
    ex = Executor(shop, led)
    ledger_res = approve_in_ledger(ex, pid, "human-reviewer", "approve", "人工批准", "low")
    print("[6] 人工批准账本:", ledger_res)

    from app.runtime.loop import Context
    from app.llm import LLM
    ctx = Context("mcp-human-test", ex, LLM())
    ctx.state.pending_proposal = RefundProposal(
        proposal_id=pid, order_id="O000123", amount=88.0, reason="MCP 自测")
    ctx.approval = Approval(proposal_id=pid, approver="human-reviewer", decision="approve")
    r1 = await ex.execute("execute_refund", {}, ctx)
    n1 = count_ledger(led)
    print("[7] 批准后执行:", r1.data, "refunds 行数:", n1)
    assert n1 == 1, "批准后应恰好 1 行"

    r2 = await ex.execute("execute_refund", {}, ctx)
    n2 = count_ledger(led)
    print("[8] 重复执行:", r2.data, "refunds 行数:", n2)
    assert n2 == 1, "重复执行不得新增行（幂等）"
    shop.close(); led.close()
    print("\n✅ MCP 边界通过：只读+提案 / 无自批工具 / 人工批准后恰好一次 / 幂等")


if __name__ == "__main__":
    asyncio.run(main())
