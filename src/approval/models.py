from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(UTC)


DEFAULT_APPROVAL_TIMEOUT_MINUTES = 15


def approval_timeout_delta() -> timedelta:
    from src.shared.config import get_settings

    minutes = get_settings().approval_timeout_minutes
    if minutes < 1:
        minutes = DEFAULT_APPROVAL_TIMEOUT_MINUTES
    return timedelta(minutes=minutes)


@dataclass(slots=True)
class ApprovalRequest:
    tool_name: str
    assistant_id: str | None = None
    capability_profile: str | None = None
    approval_scope: Literal["user", "admin"] = "admin"
    reason: str | None = None
    target: str | None = None
    status: str = "pending"
    resolution: Literal["allow", "deny"] | None = None
    id: str = field(default_factory=lambda: str(uuid4()))
    requested_at: datetime = field(default_factory=utc_now)
    expires_at: datetime = field(default_factory=lambda: utc_now() + approval_timeout_delta())
    resolved_at: datetime | None = None
    gateway_url: str | None = None  # 保存 Gateway URL 用于通知
