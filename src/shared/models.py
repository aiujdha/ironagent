from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TimestampedModel(BaseModel):
    created_at: datetime = Field(default_factory=utc_now)


class AuditEvent(TimestampedModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    action: str
    detail: dict[str, Any] = Field(default_factory=dict)
