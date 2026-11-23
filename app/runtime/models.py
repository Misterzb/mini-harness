"""统一契约 —— Runtime 的类型系统。所有模块只认这些结构。"""
from pydantic import BaseModel, Field
from typing import Any, Literal
from datetime import datetime


class SemanticPlan(BaseModel):
    fact: str
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list["Filter"] = Field(default_factory=list)
    time_range: str | None = None
    intent: Literal["query", "diagnose"] = "query"


class Filter(BaseModel):
    field: str
    value: Any


SemanticPlan.model_rebuild()


class Task(BaseModel):
    id: str
    type: Literal["query", "diagnose", "refund", "clarify", "summarize", "finish", "knowledge"]
    slots: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    status: Literal["pending", "running", "await_human", "done", "failed", "rejected"] = "pending"


class RefundProposal(BaseModel):
    proposal_id: str
    order_id: str
    amount: float
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)
    risk_level: Literal["low", "high"] = "low"
    status: Literal["pending", "approved", "rejected"] = "pending"


class Approval(BaseModel):
    proposal_id: str
    approver: str
    decision: Literal["approve", "reject"]
    comment: str = ""
    decided_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class ToolResult(BaseModel):
    ok: bool
    data: Any | None = None
    error: dict | None = None                 # {code, retryable, category, message}
    meta: dict = Field(default_factory=dict)  # latency_ms, row_count, tokens, access


class Verdict(BaseModel):
    ok: bool
    checks: list[dict] = Field(default_factory=list)
    retryable: bool = False
    reason: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class StepRecord(BaseModel):
    step: int
    observed: str = ""
    decision: str = ""
    tool: str | None = None
    result: Any | None = None
    verdict: dict | None = None
    state_delta: dict = Field(default_factory=dict)
    tokens: dict = Field(default_factory=dict)
    event: str = "step"   # step / human_gate / human_decision / final / error


class MemoryItem(BaseModel):
    memory_id: str
    layer: Literal["L1_work", "L2_event", "L3_semantic", "L4_pref"]
    content: str
    source: str
    confidence: float = 1.0
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    expires_at: str | None = None
    version: int = 1
