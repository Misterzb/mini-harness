"""FastAPI 服务：SSE 流式推送执行轨迹 + 人工审批回写。"""
import asyncio
import json
import uuid
from contextlib import asynccontextmanager

import duckdb
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from pathlib import Path

from .llm import LLM
from .runtime.executor import Executor
from .runtime.loop import Context
from .runtime.factory import build_runtime, runtime_name
from .runtime.models import Approval
from . import tools  # noqa: F401  — import 触发工具注册

ROOT = Path(__file__).resolve().parents[1]
SHOP_DB = ROOT / "data" / "shop.db"
LEDGER_DB = ROOT / "data" / "ledger.db"

_state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not SHOP_DB.exists():
        raise RuntimeError("请先运行 python scripts/build_db.py 创建数据库")
    shop = duckdb.connect(str(SHOP_DB), read_only=True)
    ledger = duckdb.connect(str(LEDGER_DB))
    executor = Executor(shop, ledger)
    llm = LLM()
    _state["executor"] = executor
    _state["llm"] = llm
    _state["loop"] = build_runtime(executor, llm)
    _state["contexts"] = {}
    print(f"[MiniHarness] 启动，LLM 模式={llm.mode}，编排内核={runtime_name()}，模型={llm.__class__.__name__}")
    yield
    shop.close(); ledger.close()


app = FastAPI(title="MiniHarness", version="1.0", lifespan=lifespan)


class RunRequest(BaseModel):
    question: str
    session_id: str | None = None


class ApproveRequest(BaseModel):
    session_id: str
    task_id: str = ""
    proposal_id: str
    approver: str = "demo-reviewer"
    decision: str = "approve"
    comment: str = ""


def sse(event: dict) -> str:
    return f"event: {event.get('event', 'step')}\ndata: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


@app.post("/run")
async def run(req: RunRequest):
    session_id = req.session_id or uuid.uuid4().hex[:12]
    loop: Loop = _state["loop"]
    ctx = Context(session_id, _state["executor"], _state["llm"])
    # 同一 session_id 先读磁盘状态：重启后多轮口径/挂起提案不丢
    ctx.restore_state()
    _state["contexts"][session_id] = ctx

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        async def emit(step: dict):
            await queue.put(sse(step))

        task = asyncio.create_task(loop.run(req.question, ctx, emit))

        async def drain():
            result = await task
            _state["contexts"][session_id] = ctx
            await queue.put(sse({"event": "done", "status": result.get("status"),
                                 "result": result, "session_id": session_id}))

        drainer = asyncio.create_task(drain())
        while True:
            item = await queue.get()
            yield item
            if json.loads(item.split("data: ", 1)[1].strip()).get("event") == "done":
                break
        await drainer

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/approve")
async def approve(req: ApproveRequest):
    """人工决策回写。用 session_id 定位会话，内存没有就从磁盘恢复。"""
    from .tools.builtin import approve_in_ledger
    contexts: dict[str, Context] = _state["contexts"]
    ctx = contexts.get(req.session_id)
    if not ctx:
        # 进程重启后内存为空 → 从 state.json 恢复同一会话
        ctx = Context(req.session_id, _state["executor"], _state["llm"])
        if not ctx.restore_state() or not ctx.state.pending_proposal:
            raise HTTPException(404, "找不到挂起的会话，请重新发起退款请求")
        contexts[req.session_id] = ctx
    proposal = ctx.state.pending_proposal
    if not proposal or proposal.proposal_id != req.proposal_id:
        raise HTTPException(409, "提案 ID 与当前会话的挂起提案不一致")

    # 先在账本标记人工决策（这是人工入口独有的能力，MCP 客户端做不到）
    ledger_result = approve_in_ledger(
        _state["executor"], req.proposal_id, req.approver, req.decision,
        req.comment, proposal.risk_level)
    if ledger_result.get("status") == "need_comment":
        raise HTTPException(400, "高风险退款需填写至少两个字的审批备注")

    approval = Approval(proposal_id=req.proposal_id, approver=req.approver,
                        decision=req.decision, comment=req.comment)  # type: ignore[arg-type]
    events: list[dict] = []

    async def emit(step: dict):
        events.append(step)

    result = await _state["loop"].resume(req.task_id, approval, ctx, emit)
    return JSONResponse({"status": result.get("status"), "events": events, "result": result})


@app.get("/health")
async def health():
    return {"ok": True, "llm_mode": _state["llm"].mode if "llm" in _state else "init"}
