"""Data models for workspace files, events, and external push requests."""

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

TargetBucket = Literal["temp", "persistent", "skills", "plugins"]
FileEventAction = Literal[
    "upload",
    "overwrite",
    "download",
    "delete",
    "push",
    "sync",
    "ingest",
    "scan_detected",
]
WorkspaceSyncStatus = Literal[
    "synced",
    "pending_approval",
    "missing_from_workspace",
    "deleted_from_workspace",
]
FileSyncStatus = Literal[
    "pending",
    "synced",
    "synced_to_workspace",
    "failed",
    "uploaded",
    "stored",
    "disabled",
    "deleted_from_workspace",
    "missing_from_workspace",
]
FileRole = Literal["input", "output", "result"]
FileDirection = Literal["inbound", "outbound"]


def utc_now() -> datetime:
    return datetime.now(UTC)


class WorkspaceFile(BaseModel):
    """Metadata record for a file stored in an assistant workspace."""

    file_id: str = Field(default_factory=lambda: str(uuid4()))
    assistant_id: str
    bucket: TargetBucket = "temp"
    filename: str
    relative_path: str
    direction: FileDirection = "inbound"
    file_role: FileRole = "input"
    source_file_id: str | None = None
    owner_user_id: str = ""
    size_bytes: int = 0
    content_type: str = ""
    uploader_user_id: str = ""
    source_type: str = ""
    source_ref: str = ""
    storage_provider: str = ""
    storage_key: str = ""
    storage_path: str = ""
    storage_status: FileSyncStatus = "pending"
    storage_error: str = ""
    storage_backend: str = ""
    object_key: str = ""
    object_url: str = ""
    checksum: str = ""
    sync_status: FileSyncStatus = "pending"
    last_error: str = ""
    workspace_last_modified_at: datetime | None = None
    last_synced_at: datetime | None = None
    scan_fingerprint: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    deleted: bool = False

    @model_validator(mode="before")
    @classmethod
    def _apply_legacy_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        if "storage_url" in payload and "object_url" not in payload:
            payload["object_url"] = payload["storage_url"]
        if "origin" in payload and "source_type" not in payload:
            payload["source_type"] = payload["origin"]
        if "workspace_path" in payload and "storage_path" not in payload:
            payload["storage_path"] = payload["workspace_path"]
        if "user_id" in payload and "owner_user_id" not in payload:
            payload["owner_user_id"] = payload["user_id"]
        return payload

    @property
    def checksum_sha256(self) -> str:
        return self.checksum

    @checksum_sha256.setter
    def checksum_sha256(self, value: str) -> None:
        self.checksum = value

    @property
    def sync_error(self) -> str:
        return self.last_error

    @sync_error.setter
    def sync_error(self, value: str) -> None:
        self.last_error = value

    @property
    def storage_url(self) -> str:
        return self.object_url

    @storage_url.setter
    def storage_url(self, value: str) -> None:
        self.object_url = value

    @property
    def origin(self) -> str:
        return self.source_type

    @origin.setter
    def origin(self, value: str) -> None:
        self.source_type = value

    @property
    def workspace_path(self) -> str:
        return self.storage_path

    @workspace_path.setter
    def workspace_path(self, value: str) -> None:
        self.storage_path = value


class WorkspaceFileEvent(BaseModel):
    """Audit trail entry for file operations."""

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    file_id: str
    assistant_id: str
    action: FileEventAction
    user_id: str = ""
    source_type: str = ""
    source_ref: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class ExternalPushRequest(BaseModel):
    """Tracks an external system pushing files into an assistant workspace."""

    push_id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str = ""
    assistant_id: str
    filename: str
    bucket: TargetBucket = "temp"
    content_type: str = ""
    storage_backend: str = ""
    storage_key: str = ""
    storage_url: str = ""
    source_type: str = ""
    source_ref: str = ""
    status: Literal["accepted", "rejected", "pending_approval"] = "accepted"
    approval_id: str | None = None
    staged_path: str | None = None
    error: str = ""
    created_at: datetime = Field(default_factory=utc_now)
