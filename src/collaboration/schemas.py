from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.collaboration.models import (
    CollaborationApprovalMode,
    CollaborationClaim,
    CollaborationTask,
    SharedObjectGranteeType,
    SharedObjectRetentionPolicy,
    SharedObjectStatusName,
    SharedObjectVisibilityScope,
    SharedWorkspaceObject,
)


class SharedObjectCreateRequest(BaseModel):
    source_assistant_id: str = Field(min_length=1, max_length=64)
    filename: str = Field(min_length=1, max_length=255)
    object_type: str = Field(default="document", min_length=1, max_length=32)
    content_type: str | None = Field(default=None, max_length=128)
    size_bytes: int = Field(default=0, ge=0)
    sha256: str = Field(default="", max_length=64)
    source_workspace_file_id: str | None = Field(default=None, max_length=64)
    parent_object_id: str | None = Field(default=None, max_length=64)
    derived_from_task_id: str | None = Field(default=None, max_length=64)
    visibility_scope: SharedObjectVisibilityScope = "task_only"
    retention_policy: SharedObjectRetentionPolicy = "task_bound"
    expires_at: datetime | None = None
    created_by: str = Field(default="system", min_length=1, max_length=64)
    storage_bucket: str | None = Field(default=None, max_length=128)
    storage_region: str | None = Field(default=None, max_length=64)
    storage_endpoint: str | None = Field(default=None, max_length=255)
    storage_key: str | None = Field(default=None, max_length=1024)
    idempotency_key: str | None = Field(default=None, max_length=128)
    meta_json: dict[str, Any] = Field(default_factory=dict)


class SharedObjectGrantRequest(BaseModel):
    grantee_type: SharedObjectGranteeType
    grantee_id: str
    created_by: str = "system"
    expires_at: datetime | None = None


class CollaborationTaskCreateRequest(BaseModel):
    source_assistant_id: str = Field(min_length=1, max_length=64)
    target_assistant_id: str = Field(min_length=1, max_length=64)
    input_object_id: str = Field(min_length=1, max_length=64)
    instruction: str = Field(min_length=1)
    created_by: str = Field(default="system", min_length=1, max_length=64)
    approval_mode: CollaborationApprovalMode = "none"
    idempotency_key: str | None = Field(default=None, max_length=128)
    timeout_seconds: int = Field(default=600, gt=0)
    budget_limit: int | None = Field(default=None, ge=0)
    parent_task_id: str | None = Field(default=None, max_length=64)
    max_depth: int = Field(default=0, ge=0)


class CollaborationTaskClaimRequest(BaseModel):
    assistant_id: str


class CollaborationTaskStartRequest(BaseModel):
    assistant_id: str
    claim_token: str


class CollaborationTaskClaimRenewRequest(BaseModel):
    assistant_id: str
    claim_token: str


class CollaborationTaskCompleteRequest(BaseModel):
    assistant_id: str
    claim_token: str
    result_object_id: str | None = None
    result_workspace_file_id: str | None = None
    result_workspace_file_ids: list[str] | None = None


class CollaborationTaskFailRequest(BaseModel):
    assistant_id: str
    claim_token: str
    failed_reason_code: str
    failed_reason_text: str | None = None


class CollaborationTaskCancelRequest(BaseModel):
    actor_id: str = "system"
    reason: str | None = None


class CollaborationClaimResponse(BaseModel):
    task: CollaborationTask
    claim: CollaborationClaim


class CollaborationCleanupResponse(BaseModel):
    expired_objects: int
    expired_tasks: int
    expired_permissions: int
    expired_claims: int


class SharedObjectListResponse(BaseModel):
    items: list[SharedWorkspaceObject] = Field(default_factory=list)
    total: int = 0


class CollaborationTaskListResponse(BaseModel):
    items: list[CollaborationTask] = Field(default_factory=list)
    total: int = 0


class SharedObjectStatusFilter(BaseModel):
    status: SharedObjectStatusName | None = None


class CollaborationTaskStatusFilter(BaseModel):
    status: Literal[
        "pending_approval",
        "pending_claim",
        "claimed",
        "running",
        "completed",
        "failed",
        "expired",
        "cancelled",
        "rejected",
    ] | None = None
