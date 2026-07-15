from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

MarketStatus = Literal["pending", "approved", "rejected"]
StorageBackend = Literal["local", "qiniu", "cos"]


def utc_now() -> datetime:
    return datetime.now(UTC)


class SkillRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    slug: str
    version: str
    description: str = ""
    author: str = ""
    tags: str = ""
    package_path: str | None = None
    local_archive_path: str | None = None
    manifest: dict[str, Any] = Field(default_factory=dict)
    status: MarketStatus = "pending"
    install_count: int = 0
    storage_backend: StorageBackend = "local"
    created_at: datetime = Field(default_factory=utc_now)
    reviewed_at: datetime | None = None
