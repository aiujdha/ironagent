from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

HOOK_PROTOCOL_VERSION = "2026-04-03"

DecisionLiteral = Literal["allow", "deny", "pending_approval"]
RiskLevelLiteral = Literal["low", "medium", "high", "critical"]
HookNameLiteral = Literal["pre_execute", "post_execute"]


def utc_now() -> datetime:
    return datetime.now(UTC)


class HookEnvelope(BaseModel):
    protocol_version: str = HOOK_PROTOCOL_VERSION
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    source: str = "ironagent"
    hook_name: HookNameLiteral
    occurred_at: datetime = Field(default_factory=utc_now)


class AssistantContext(BaseModel):
    assistant_id: str | None = None
    name: str | None = None
    capability_profile: str = "onlyread"


class SessionContext(BaseModel):
    session_id: str | None = None
    run_id: str | None = None
    conversation_id: str | None = None
    channel: str | None = None
    user_id: str | None = None


class ToolContext(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    source: Literal["core", "plugin", "channel", "client"] | None = None
    optional: bool | None = None
    owner_only: bool | None = None


class RiskContext(BaseModel):
    level: RiskLevelLiteral = "medium"
    reason: str | None = None
    tags: list[str] = Field(default_factory=list)


class ApprovalContext(BaseModel):
    required: bool = True
    approval_id: str
    status: Literal["pending"] = "pending"
    expires_at: datetime


class ApprovalStatusResponse(BaseModel):
    approval_id: str
    tool_name: str
    assistant_id: str | None = None
    capability_profile: str | None = None
    approval_scope: Literal["user", "admin"] = "admin"
    reason: str | None = None
    target: str | None = None
    status: str
    resolution: Literal["allow", "deny"] | None = None
    requested_at: datetime
    expires_at: datetime
    resolved_at: datetime | None = None


class ApprovalResolveRequest(BaseModel):
    resolution: Literal["allow", "deny", "allow-once", "allow-always"]


class ControlUiLaunchTicketCreateRequest(BaseModel):
    assistant_id: str | None = None
    gateway_url: str
    gateway_token: str
    session_key: str = "main"
    control_ui_url: str | None = None


class ControlUiLaunchTicketCreateResponse(BaseModel):
    ticket: str
    expires_at: datetime


class ControlUiLaunchTicketResolveRequest(BaseModel):
    ticket: str


class ControlUiLaunchTicketResolveResponse(BaseModel):
    assistant_id: str | None = None
    gateway_url: str
    gateway_token: str
    admin_token: str | None = None
    session_key: str = "main"
    expires_at: datetime


class ExecutionResultContext(BaseModel):
    success: bool
    duration_ms: int | None = None
    error: str | None = None
    output_summary: dict[str, Any] = Field(default_factory=dict)


class PreExecuteHookRequest(BaseModel):
    envelope: HookEnvelope
    assistant: AssistantContext = Field(default_factory=AssistantContext)
    session: SessionContext = Field(default_factory=SessionContext)
    tool: ToolContext
    risk: RiskContext = Field(default_factory=RiskContext)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PreExecuteHookResponse(BaseModel):
    protocol_version: str = HOOK_PROTOCOL_VERSION
    request_id: str
    decision: DecisionLiteral
    reason: str
    capability_profile: str
    fail_closed: bool = True
    approval: ApprovalContext | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    tool_args: dict[str, Any] | None = None


class PostExecuteHookRequest(BaseModel):
    envelope: HookEnvelope
    assistant: AssistantContext = Field(default_factory=AssistantContext)
    session: SessionContext = Field(default_factory=SessionContext)
    tool: ToolContext
    result: ExecutionResultContext
    decision: DecisionLiteral | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PostExecuteHookResponse(BaseModel):
    protocol_version: str = HOOK_PROTOCOL_VERSION
    request_id: str
    status: Literal["ok"] = "ok"
