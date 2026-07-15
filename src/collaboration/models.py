from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


SharedObjectStatusName = Literal["active", "expired", "soft_deleted", "deleted"]
SharedObjectVisibilityScope = Literal["task_only", "shared_persistent"]
SharedObjectAccessMode = Literal["read_only"]
SharedObjectRetentionPolicy = Literal["temp_shared", "task_bound", "persistent_shared"]
SharedObjectPermissionStatusName = Literal["active", "expired", "revoked"]
SharedObjectPermissionType = Literal["read"]
SharedObjectGranteeType = Literal["assistant", "task"]
CollaborationApprovalMode = Literal["none", "user_required", "admin_required"]
CollaborationTaskStatusName = Literal[
    "pending_approval",
    "pending_claim",
    "claimed",
    "running",
    "completed",
    "failed",
    "expired",
    "cancelled",
    "rejected",
]
CollaborationClaimStatusName = Literal["active", "released", "expired"]
CollaborationActorType = Literal["user", "assistant", "system"]


class SharedWorkspaceObject(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    object_type: str = "document"
    filename: str
    content_type: str | None = None
    size_bytes: int = 0
    sha256: str = ""
    storage_backend: str = "cos"
    storage_bucket: str = ""
    storage_region: str = ""
    storage_endpoint: str = ""
    storage_key: str = ""
    storage_etag: str = ""
    storage_version_id: str = ""
    source_assistant_id: str
    source_workspace_file_id: str | None = None
    parent_object_id: str | None = None
    derived_from_task_id: str | None = None
    visibility_scope: SharedObjectVisibilityScope = "task_only"
    access_mode: SharedObjectAccessMode = "read_only"
    retention_policy: SharedObjectRetentionPolicy = "task_bound"
    expires_at: datetime | None = None
    last_accessed_at: datetime | None = None
    status: SharedObjectStatusName = "active"
    created_by: str = "system"
    created_at: datetime = Field(default_factory=utc_now)
    deleted_at: datetime | None = None
    idempotency_key: str | None = None
    meta_json: dict[str, Any] = Field(default_factory=dict)


class SharedObjectPermission(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    object_id: str
    grantee_type: SharedObjectGranteeType
    grantee_id: str
    permission_type: SharedObjectPermissionType = "read"
    created_by: str = "system"
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None
    status: SharedObjectPermissionStatusName = "active"


class CollaborationTask(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    root_task_id: str | None = None
    parent_task_id: str | None = None
    depth: int = 0
    max_depth: int = 0
    source_assistant_id: str
    target_assistant_id: str
    input_object_id: str
    instruction: str
    status: CollaborationTaskStatusName = "pending_claim"
    approval_mode: CollaborationApprovalMode = "none"
    approval_id: str | None = None
    result_object_id: str | None = None
    result_workspace_file_id: str | None = None
    idempotency_key: str | None = None
    budget_limit: int | None = None
    timeout_seconds: int = 600
    created_by: str = "system"
    created_at: datetime = Field(default_factory=utc_now)
    accepted_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failed_at: datetime | None = None
    failed_reason_code: str | None = None
    failed_reason_text: str | None = None
    meta_json: dict[str, Any] = Field(default_factory=dict)


class CollaborationClaim(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    assistant_id: str
    claim_token: str = Field(default_factory=lambda: str(uuid4()))
    claimed_at: datetime = Field(default_factory=utc_now)
    lease_expires_at: datetime
    status: CollaborationClaimStatusName = "active"


class CollaborationEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    event_type: str
    actor_type: CollaborationActorType = "system"
    actor_id: str = "system"
    detail_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class CollaborationState(BaseModel):
    objects: list[SharedWorkspaceObject] = Field(default_factory=list)
    permissions: list[SharedObjectPermission] = Field(default_factory=list)
    tasks: list[CollaborationTask] = Field(default_factory=list)
    claims: list[CollaborationClaim] = Field(default_factory=list)
    events: list[CollaborationEvent] = Field(default_factory=list)
