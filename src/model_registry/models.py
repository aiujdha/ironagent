from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class ModelRegistryRecord(BaseModel):
    model_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    provider: str
    url: str = ""
    api_key: str = ""
    status: str = "active"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
