from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from src.approval.service import ApprovalService
from src.collaboration.models import (
    CollaborationActorType,
    CollaborationClaim,
    CollaborationEvent,
    CollaborationState,
    CollaborationTask,
    SharedObjectPermission,
    SharedWorkspaceObject,
)
from src.collaboration.repository import SqlAlchemyCollaborationRepository
from src.shared.config import approval_mode_disabled, get_settings


@dataclass(slots=True)
class CollaborationObjectStorageDefaults:
    backend: str = "cos"
    bucket: str = ""
    region: str = ""
    endpoint: str = ""
    prefix: str = "collaboration/"


@dataclass(slots=True)
class CollaborationCleanupResult:
    expired_objects: int = 0
    expired_tasks: int = 0
    expired_permissions: int = 0
    expired_claims: int = 0


def utc_now() -> datetime:
    return datetime.now(UTC)


RETRYABLE_TASK_FAILURE_CODES = {
    "runtime_lookup_failed",
    "runtime_route_lookup_failed",
    "runtime_gateway_unavailable",
    "runtime_execution_failed",
    "runtime_result_object_failed",
    "gateway_task_stale_progress",
}


class CollaborationService:
    def __init__(
        self,
        approval_service: ApprovalService | None = None,
        storage_defaults: CollaborationObjectStorageDefaults | None = None,
        database_url: str | None = None,
        repository: SqlAlchemyCollaborationRepository | None = None,
        claim_lease_seconds: int | None = None,
    ) -> None:
        settings = get_settings()
        self.approval_service = approval_service or ApprovalService()
        self.storage_defaults = storage_defaults or CollaborationObjectStorageDefaults(
            backend=settings.collaboration_object_storage_backend,
            bucket=settings.collaboration_object_storage_bucket,
            region=settings.collaboration_object_storage_region,
            endpoint=settings.collaboration_object_storage_endpoint,
            prefix=settings.collaboration_object_storage_prefix,
        )
        self.repository = repository or SqlAlchemyCollaborationRepository(
            database_url or settings.database_url
        )
        self._state_lock = threading.RLock()
        self.claim_lease_seconds = (
            claim_lease_seconds
            if claim_lease_seconds is not None
            else settings.collaboration_claim_lease_seconds
        )

    def ensure_ready(self) -> None:
        self.repository._ensure_ready()

    def check_ready(self) -> dict[str, Any]:
        return self.repository.check_ready()

    def dispose(self) -> None:
        self.repository.dispose()

    def _load_state(self, *, sync_approvals: bool = True) -> CollaborationState:
        if not sync_approvals:
            return self.repository.load_state()
        with self._state_lock:
            state = self.repository.load_state()
            if self._sync_approval_statuses(state):
                self._write_state(state)
            return state

    def _write_state(self, state: CollaborationState) -> None:
        self.repository.save_state(state)

    def _append_event(
        self,
        state: CollaborationState,
        *,
        task_id: str,
        event_type: str,
        actor_type: CollaborationActorType = "system",
        actor_id: str = "system",
        detail_json: dict[str, object] | None = None,
    ) -> None:
        state.events.append(
            CollaborationEvent(
                task_id=task_id,
                event_type=event_type,
                actor_type=actor_type,
                actor_id=actor_id,
                detail_json=detail_json or {},
            )
        )

    def _find_object(
        self,
        state: CollaborationState,
        object_id: str,
    ) -> SharedWorkspaceObject | None:
        return next((item for item in state.objects if item.id == object_id), None)

    def _find_task(self, state: CollaborationState, task_id: str) -> CollaborationTask | None:
        return next((item for item in state.tasks if item.id == task_id), None)

    def _reset_retryable_task_for_new_attempt(
        self,
        state: CollaborationState,
        task: CollaborationTask,
        *,
        reason: str = "retryable_failure",
    ) -> None:
        task.status = "pending_claim"
        task.accepted_at = None
        task.started_at = None
        task.completed_at = None
        task.failed_at = None
        task.failed_reason_code = None
        task.failed_reason_text = None
        task.result_object_id = None
        task.result_workspace_file_id = None
        self._append_event(
            state,
            task_id=task.id,
            event_type="retry_scheduled",
            actor_type="system",
            actor_id="go-gateway",
            detail_json={"idempotency_key": task.idempotency_key, "reason": reason},
        )

    def _active_claim_for_task(
        self, state: CollaborationState, task_id: str
    ) -> CollaborationClaim | None:
        now = utc_now()
        for claim in state.claims:
            if claim.task_id != task_id or claim.status != "active":
                continue
            if claim.lease_expires_at <= now:
                claim.status = "expired"
                continue
            return claim
        return None

    def _grant_permission_if_missing(
        self,
        state: CollaborationState,
        *,
        object_id: str,
        grantee_type: str,
        grantee_id: str,
        created_by: str,
        expires_at: datetime | None = None,
    ) -> SharedObjectPermission:
        existing = next(
            (
                item
                for item in state.permissions
                if item.object_id == object_id
                and item.grantee_type == grantee_type
                and item.grantee_id == grantee_id
                and item.permission_type == "read"
                and item.status == "active"
            ),
            None,
        )
        if existing is not None:
            return existing
        permission = SharedObjectPermission(
            object_id=object_id,
            grantee_type=grantee_type,
            grantee_id=grantee_id,
            created_by=created_by,
            expires_at=expires_at,
        )
        state.permissions.append(permission)
        return permission

    def _has_active_permission(
        self,
        state: CollaborationState,
        *,
        object_id: str,
        assistant_id: str | None = None,
        task_id: str | None = None,
    ) -> bool:
        now = utc_now()
        for permission in state.permissions:
            if permission.object_id != object_id or permission.status != "active":
                continue
            if permission.expires_at is not None and permission.expires_at <= now:
                continue
            if (
                assistant_id
                and permission.grantee_type == "assistant"
                and permission.grantee_id == assistant_id
            ):
                return True
            if task_id and permission.grantee_type == "task" and permission.grantee_id == task_id:
                return True
        return False

    def _default_expires_at(self, retention_policy: str, now: datetime) -> datetime | None:
        if retention_policy == "temp_shared":
            return now + timedelta(hours=24)
        if retention_policy == "task_bound":
            return now + timedelta(days=7)
        return None

    def _safe_filename(self, filename: str) -> str:
        candidate = filename.strip().replace("\\", "/").split("/")[-1]
        normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", candidate).strip("-.")
        if len(normalized) > 255:
            normalized = normalized[:255].strip("-.")
        return normalized or "object.bin"

    def _require_text(self, value: str, *, field_name: str, max_length: int) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError(f"{field_name} is required")
        if len(trimmed) > max_length:
            raise ValueError(f"{field_name} must be at most {max_length} characters")
        return trimmed

    def _optional_text(
        self,
        value: str | None,
        *,
        field_name: str,
        max_length: int,
    ) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            return None
        if len(trimmed) > max_length:
            raise ValueError(f"{field_name} must be at most {max_length} characters")
        return trimmed

    def _build_storage_key(self, object_id: str, filename: str) -> str:
        prefix = self.storage_defaults.prefix.strip().strip("/")
        base = self._safe_filename(filename)
        if prefix:
            return f"{prefix}/{object_id}/{base}"
        return f"{object_id}/{base}"

    def _sync_approval_statuses(self, state: CollaborationState) -> bool:
        changed = False
        for task in state.tasks:
            if task.status != "pending_approval" or not task.approval_id:
                continue
            approval = self.approval_service.get_request(task.approval_id)
            if approval is None:
                continue
            if approval.status == "approved":
                task.status = "pending_claim"
                self._grant_permission_if_missing(
                    state,
                    object_id=task.input_object_id,
                    grantee_type="assistant",
                    grantee_id=task.target_assistant_id,
                    created_by="system",
                )
                self._append_event(
                    state,
                    task_id=task.id,
                    event_type="approved",
                    detail_json={"approval_id": task.approval_id},
                )
                changed = True
            elif approval.status == "denied":
                task.status = "rejected"
                task.failed_at = utc_now()
                task.failed_reason_code = "approval_denied"
                task.failed_reason_text = "collaboration approval denied"
                self._append_event(
                    state,
                    task_id=task.id,
                    event_type="rejected",
                    detail_json={"approval_id": task.approval_id},
                )
                changed = True
        return changed

    def create_shared_object(
        self,
        *,
        source_assistant_id: str,
        filename: str,
        created_by: str = "system",
        object_type: str = "document",
        content_type: str | None = None,
        size_bytes: int = 0,
        sha256: str = "",
        source_workspace_file_id: str | None = None,
        parent_object_id: str | None = None,
        derived_from_task_id: str | None = None,
        visibility_scope: str = "task_only",
        retention_policy: str = "task_bound",
        expires_at: datetime | None = None,
        storage_bucket: str | None = None,
        storage_region: str | None = None,
        storage_endpoint: str | None = None,
        storage_key: str | None = None,
        idempotency_key: str | None = None,
        meta_json: dict[str, Any] | None = None,
    ) -> SharedWorkspaceObject:
        if size_bytes < 0:
            raise ValueError("size_bytes must be non-negative")
        safe_filename = self._safe_filename(filename)
        source_assistant_id = self._require_text(
            source_assistant_id,
            field_name="source_assistant_id",
            max_length=64,
        )
        object_type = self._require_text(object_type, field_name="object_type", max_length=32)
        content_type = self._optional_text(
            content_type,
            field_name="content_type",
            max_length=128,
        )
        sha256 = self._optional_text(sha256, field_name="sha256", max_length=64) or ""
        source_workspace_file_id = self._optional_text(
            source_workspace_file_id,
            field_name="source_workspace_file_id",
            max_length=64,
        )
        parent_object_id = self._optional_text(
            parent_object_id,
            field_name="parent_object_id",
            max_length=64,
        )
        derived_from_task_id = self._optional_text(
            derived_from_task_id,
            field_name="derived_from_task_id",
            max_length=64,
        )
        created_by = self._require_text(created_by, field_name="created_by", max_length=64)
        storage_bucket = (
            self._optional_text(
                storage_bucket or self.storage_defaults.bucket,
                field_name="storage_bucket",
                max_length=128,
            )
            or ""
        )
        storage_region = (
            self._optional_text(
                storage_region or self.storage_defaults.region,
                field_name="storage_region",
                max_length=64,
            )
            or ""
        )
        storage_endpoint = (
            self._optional_text(
                storage_endpoint or self.storage_defaults.endpoint,
                field_name="storage_endpoint",
                max_length=255,
            )
            or ""
        )
        storage_key = self._optional_text(storage_key, field_name="storage_key", max_length=1024)
        idempotency_key = self._optional_text(
            idempotency_key,
            field_name="idempotency_key",
            max_length=128,
        )
        meta = dict(meta_json or {})
        if idempotency_key:
            meta["idempotency_key"] = idempotency_key

        direct_create_shared_object = getattr(self.repository, "create_shared_object_direct", None)
        if callable(direct_create_shared_object):
            now = utc_now()
            item = SharedWorkspaceObject(
                filename=safe_filename,
                object_type=object_type,
                content_type=content_type,
                size_bytes=size_bytes,
                sha256=sha256,
                source_assistant_id=source_assistant_id,
                source_workspace_file_id=source_workspace_file_id,
                parent_object_id=parent_object_id,
                derived_from_task_id=derived_from_task_id,
                visibility_scope=visibility_scope,
                retention_policy=retention_policy,
                expires_at=expires_at or self._default_expires_at(retention_policy, now),
                created_by=created_by,
                storage_backend=self.storage_defaults.backend,
                storage_bucket=storage_bucket,
                storage_region=storage_region,
                storage_endpoint=storage_endpoint,
                idempotency_key=idempotency_key,
                meta_json=meta,
            )
            item.storage_key = (
                storage_key or self._build_storage_key(item.id, item.filename)
            ).strip()
            permission = SharedObjectPermission(
                object_id=item.id,
                grantee_type="assistant",
                grantee_id=item.source_assistant_id,
                created_by=created_by,
            )
            return direct_create_shared_object(
                item=item,
                source_permission=permission,
            )

        with self._state_lock:
            state = self._load_state(sync_approvals=False)
            if idempotency_key:
                existing = next(
                    (
                        item
                        for item in state.objects
                        if item.idempotency_key == idempotency_key
                        or item.meta_json.get("idempotency_key") == idempotency_key
                    ),
                    None,
                )
                if existing is not None:
                    return existing
            now = utc_now()
            item = SharedWorkspaceObject(
                filename=safe_filename,
                object_type=object_type,
                content_type=content_type,
                size_bytes=size_bytes,
                sha256=sha256,
                source_assistant_id=source_assistant_id,
                source_workspace_file_id=source_workspace_file_id,
                parent_object_id=parent_object_id,
                derived_from_task_id=derived_from_task_id,
                visibility_scope=visibility_scope,
                retention_policy=retention_policy,
                expires_at=expires_at or self._default_expires_at(retention_policy, now),
                created_by=created_by,
                storage_backend=self.storage_defaults.backend,
                storage_bucket=storage_bucket,
                storage_region=storage_region,
                storage_endpoint=storage_endpoint,
                idempotency_key=idempotency_key,
                meta_json=meta,
            )
            item.storage_key = (
                storage_key or self._build_storage_key(item.id, item.filename)
            ).strip()
            state.objects.append(item)
            self._grant_permission_if_missing(
                state,
                object_id=item.id,
                grantee_type="assistant",
                grantee_id=item.source_assistant_id,
                created_by=created_by,
            )
            self._write_state(state)
            return item

    def list_shared_objects(
        self,
        *,
        assistant_id: str | None = None,
        status: str | None = None,
    ) -> list[SharedWorkspaceObject]:
        state = self._load_state()
        items = state.objects
        if status is not None:
            items = [item for item in items if item.status == status]
        if assistant_id is None:
            return items
        return [
            item
            for item in items
            if item.source_assistant_id == assistant_id
            or self._has_active_permission(state, object_id=item.id, assistant_id=assistant_id)
        ]

    def get_shared_object(
        self,
        object_id: str,
        *,
        assistant_id: str | None = None,
        task_id: str | None = None,
    ) -> SharedWorkspaceObject:
        with self._state_lock:
            state = self._load_state()
            item = self._find_object(state, object_id.strip())
            if item is None:
                raise ValueError("shared object not found")
            if item.status not in {"active", "soft_deleted"}:
                raise ValueError("shared object is not accessible")
            if assistant_id or task_id:
                if not self._has_active_permission(
                    state,
                    object_id=item.id,
                    assistant_id=assistant_id,
                    task_id=task_id,
                ):
                    raise ValueError("assistant is not authorized to read shared object")
            item.last_accessed_at = utc_now()
            self._write_state(state)
            return item

    def grant_shared_object_permission(
        self,
        *,
        object_id: str,
        grantee_type: str,
        grantee_id: str,
        created_by: str = "system",
        expires_at: datetime | None = None,
    ) -> SharedObjectPermission:
        with self._state_lock:
            state = self._load_state(sync_approvals=False)
            item = self._find_object(state, object_id.strip())
            if item is None:
                raise ValueError("shared object not found")
            permission = self._grant_permission_if_missing(
                state,
                object_id=item.id,
                grantee_type=grantee_type,
                grantee_id=grantee_id.strip(),
                created_by=created_by,
                expires_at=expires_at,
            )
            self._write_state(state)
            return permission

    def create_task(
        self,
        *,
        source_assistant_id: str,
        target_assistant_id: str,
        input_object_id: str,
        instruction: str,
        created_by: str = "system",
        approval_mode: str = "none",
        idempotency_key: str | None = None,
        timeout_seconds: int = 600,
        budget_limit: int | None = None,
        parent_task_id: str | None = None,
        max_depth: int = 0,
    ) -> CollaborationTask:
        if not instruction.strip():
            raise ValueError("instruction is required")
        if source_assistant_id.strip() == target_assistant_id.strip():
            raise ValueError("source and target assistant must be different")
        if parent_task_id:
            raise ValueError("nested collaboration tasks are disabled in phase 1")

        direct_create_task = getattr(self.repository, "create_task_direct", None)
        if approval_mode == "none" and callable(direct_create_task):
            task = CollaborationTask(
                source_assistant_id=source_assistant_id.strip(),
                target_assistant_id=target_assistant_id.strip(),
                input_object_id=input_object_id.strip(),
                instruction=instruction.strip(),
                status="pending_claim",
                approval_mode=approval_mode,
                idempotency_key=idempotency_key,
                timeout_seconds=timeout_seconds,
                budget_limit=budget_limit,
                created_by=created_by,
                max_depth=max_depth,
            )
            task.root_task_id = task.id
            target_permission = SharedObjectPermission(
                object_id=task.input_object_id,
                grantee_type="assistant",
                grantee_id=task.target_assistant_id,
                created_by=created_by,
            )
            return direct_create_task(task=task, target_permission=target_permission)

        with self._state_lock:
            state = self._load_state()
            item = self._find_object(state, input_object_id.strip())
            if item is None:
                raise ValueError("input shared object not found")
            if item.status != "active":
                raise ValueError("input shared object is not active")
            if not self._has_active_permission(
                state,
                object_id=item.id,
                assistant_id=source_assistant_id.strip(),
            ):
                raise ValueError("source assistant is not authorized to share this object")
            if idempotency_key:
                existing = next(
                    (task for task in state.tasks if task.idempotency_key == idempotency_key),
                    None,
                )
                if existing is not None:
                    if (
                        existing.status == "failed"
                        and existing.failed_reason_code in RETRYABLE_TASK_FAILURE_CODES
                    ):
                        self._reset_retryable_task_for_new_attempt(
                            state,
                            existing,
                            reason="retryable_failure",
                        )
                        self._write_state(state)
                    elif (
                        existing.status in {"claimed", "running"}
                        and self._active_claim_for_task(state, existing.id) is None
                    ):
                        self._reset_retryable_task_for_new_attempt(
                            state,
                            existing,
                            reason="claim_expired",
                        )
                        self._write_state(state)
                    return existing

            status = "pending_claim"
            approval_id: str | None = None
            if approval_mode != "none" and not approval_mode_disabled():
                approval = self.approval_service.create_request(
                    "collaboration.share",
                    assistant_id=source_assistant_id.strip(),
                    approval_scope="user" if approval_mode == "user_required" else "admin",
                    reason="cross-assistant collaboration requires approval",
                    target=f"{item.id}:{target_assistant_id.strip()}",
                )
                approval_id = approval.id
                status = "pending_approval"

            task = CollaborationTask(
                source_assistant_id=source_assistant_id.strip(),
                target_assistant_id=target_assistant_id.strip(),
                input_object_id=item.id,
                instruction=instruction.strip(),
                status=status,
                approval_mode=approval_mode,
                approval_id=approval_id,
                idempotency_key=idempotency_key,
                timeout_seconds=timeout_seconds,
                budget_limit=budget_limit,
                created_by=created_by,
                max_depth=max_depth,
            )
            task.root_task_id = task.id
            state.tasks.append(task)
            self._append_event(
                state,
                task_id=task.id,
                event_type="created",
                actor_type="assistant",
                actor_id=source_assistant_id.strip(),
                detail_json={
                    "input_object_id": item.id,
                    "target_assistant_id": task.target_assistant_id,
                },
            )
            if approval_id:
                self._append_event(
                    state,
                    task_id=task.id,
                    event_type="approval_requested",
                    detail_json={"approval_id": approval_id},
                )
            else:
                self._grant_permission_if_missing(
                    state,
                    object_id=item.id,
                    grantee_type="assistant",
                    grantee_id=task.target_assistant_id,
                    created_by=created_by,
                )
            self._write_state(state)
            return task

    def list_tasks(
        self,
        *,
        assistant_id: str | None = None,
        status: str | None = None,
        source_assistant_id: str | None = None,
    ) -> list[CollaborationTask]:
        state = self._load_state()
        tasks = state.tasks
        if assistant_id is not None:
            tasks = [item for item in tasks if item.target_assistant_id == assistant_id]
        if source_assistant_id is not None:
            tasks = [item for item in tasks if item.source_assistant_id == source_assistant_id]
        if status is not None:
            tasks = [item for item in tasks if item.status == status]
        return tasks

    def get_task(self, task_id: str) -> CollaborationTask:
        direct_get_task = getattr(self.repository, "get_task", None)
        if callable(direct_get_task):
            task = direct_get_task(task_id.strip())
            if task.status != "pending_approval":
                return task

        state = self._load_state()
        task = self._find_task(state, task_id.strip())
        if task is None:
            raise ValueError("collaboration task not found")
        return task

    def claim_task(self, *, task_id: str, assistant_id: str) -> CollaborationClaim:
        direct_claim_task = getattr(self.repository, "claim_task", None)
        if callable(direct_claim_task):
            return direct_claim_task(
                task_id=task_id,
                assistant_id=assistant_id,
                claim_lease_seconds=self.claim_lease_seconds,
            )

        with self._state_lock:
            state = self._load_state()
            task = self._find_task(state, task_id.strip())
            if task is None:
                raise ValueError("collaboration task not found")
            if task.target_assistant_id != assistant_id.strip():
                raise ValueError("assistant is not authorized to claim this task")
            active_claim = self._active_claim_for_task(state, task.id)
            if active_claim is not None:
                if (
                    task.status in {"claimed", "running"}
                    and active_claim.assistant_id == assistant_id.strip()
                ):
                    active_claim.lease_expires_at = utc_now() + timedelta(
                        seconds=self.claim_lease_seconds
                    )
                    self._write_state(state)
                    return active_claim
                raise ValueError("collaboration task is already claimed")
            if task.status != "pending_claim":
                if not (
                    task.status == "claimed"
                    and task.started_at is None
                    and task.completed_at is None
                    and task.failed_at is None
                ):
                    raise ValueError("collaboration task is not claimable")
            claim = CollaborationClaim(
                task_id=task.id,
                assistant_id=assistant_id.strip(),
                lease_expires_at=utc_now() + timedelta(seconds=self.claim_lease_seconds),
            )
            state.claims.append(claim)
            task.status = "claimed"
            task.accepted_at = utc_now()
            self._append_event(
                state,
                task_id=task.id,
                event_type="claimed",
                actor_type="assistant",
                actor_id=assistant_id.strip(),
                detail_json={"claim_id": claim.id},
            )
            self._write_state(state)
            return claim

    def _require_active_claim(
        self,
        state: CollaborationState,
        *,
        task_id: str,
        assistant_id: str,
        claim_token: str,
    ) -> tuple[CollaborationTask, CollaborationClaim]:
        task = self._find_task(state, task_id.strip())
        if task is None:
            raise ValueError("collaboration task not found")
        if task.status in {"completed", "cancelled", "expired", "failed", "rejected"}:
            raise ValueError("collaboration task can no longer accept execution updates")
        claim = self._active_claim_for_task(state, task.id)
        if claim is None:
            raise ValueError("active claim not found")
        if claim.assistant_id != assistant_id.strip():
            raise ValueError("assistant is not authorized to use this claim")
        if claim.claim_token != claim_token.strip():
            raise ValueError("claim token is invalid")
        return task, claim

    def renew_claim(
        self,
        *,
        task_id: str,
        assistant_id: str,
        claim_token: str,
    ) -> CollaborationClaim:
        direct_renew_claim = getattr(self.repository, "renew_claim", None)
        if callable(direct_renew_claim):
            return direct_renew_claim(
                task_id=task_id,
                assistant_id=assistant_id,
                claim_token=claim_token,
                claim_lease_seconds=self.claim_lease_seconds,
            )

        with self._state_lock:
            state = self._load_state()
            task, claim = self._require_active_claim(
                state,
                task_id=task_id,
                assistant_id=assistant_id,
                claim_token=claim_token,
            )
            if task.status not in {"claimed", "running"}:
                raise ValueError("collaboration task claim cannot be renewed")
            claim.lease_expires_at = utc_now() + timedelta(seconds=self.claim_lease_seconds)
            self._write_state(state)
            return claim

    def start_task(self, *, task_id: str, assistant_id: str, claim_token: str) -> CollaborationTask:
        direct_start_task = getattr(self.repository, "start_task", None)
        if callable(direct_start_task):
            return direct_start_task(
                task_id=task_id,
                assistant_id=assistant_id,
                claim_token=claim_token,
            )

        with self._state_lock:
            state = self._load_state()
            task, _claim = self._require_active_claim(
                state,
                task_id=task_id,
                assistant_id=assistant_id,
                claim_token=claim_token,
            )
            if task.status not in {"claimed", "running"}:
                raise ValueError("collaboration task cannot be started")
            if task.status != "running":
                task.status = "running"
                task.started_at = utc_now()
                self._append_event(
                    state,
                    task_id=task.id,
                    event_type="started",
                    actor_type="assistant",
                    actor_id=assistant_id.strip(),
                )
                self._write_state(state)
            return task

    def complete_task(
        self,
        *,
        task_id: str,
        assistant_id: str,
        claim_token: str,
        result_object_id: str | None = None,
        result_workspace_file_id: str | None = None,
        result_workspace_file_ids: list[str] | None = None,
    ) -> CollaborationTask:
        direct_complete_task = getattr(self.repository, "complete_task", None)
        if callable(direct_complete_task):
            return direct_complete_task(
                task_id=task_id,
                assistant_id=assistant_id,
                claim_token=claim_token,
                result_object_id=result_object_id,
                result_workspace_file_id=result_workspace_file_id,
                result_workspace_file_ids=result_workspace_file_ids,
            )

        with self._state_lock:
            state = self._load_state()
            task, claim = self._require_active_claim(
                state,
                task_id=task_id,
                assistant_id=assistant_id,
                claim_token=claim_token,
            )
            if task.status not in {"claimed", "running"}:
                raise ValueError("collaboration task cannot be completed")
            normalized_file_ids = self._normalize_result_workspace_file_ids(
                result_workspace_file_id,
                result_workspace_file_ids,
            )
            if normalized_file_ids and result_workspace_file_id is None:
                result_workspace_file_id = normalized_file_ids[0]
            if result_object_id is None and result_workspace_file_id is None:
                raise ValueError("completion requires a result object or workspace file")
            if result_object_id is not None and self._find_object(state, result_object_id) is None:
                raise ValueError("result shared object not found")
            task.status = "completed"
            task.completed_at = utc_now()
            task.result_object_id = result_object_id
            task.result_workspace_file_id = result_workspace_file_id
            if normalized_file_ids:
                task.meta_json["runtime_result_workspace_file_ids"] = normalized_file_ids
                task.meta_json["runtime_result_file_count"] = len(normalized_file_ids)
            else:
                task.meta_json.pop("runtime_result_workspace_file_ids", None)
                task.meta_json["runtime_result_file_count"] = 0
            claim.status = "released"
            self._append_event(
                state,
                task_id=task.id,
                event_type="completed",
                actor_type="assistant",
                actor_id=assistant_id.strip(),
                detail_json={
                    "result_object_id": result_object_id,
                    "result_workspace_file_id": result_workspace_file_id,
                    "result_workspace_file_ids": normalized_file_ids,
                },
            )
            self._write_state(state)
            return task

    @staticmethod
    def _normalize_result_workspace_file_ids(
        primary: str | None,
        file_ids: list[str] | None,
    ) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in [primary, *(file_ids or [])]:
            normalized = str(value or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            out.append(normalized)
        return out

    def fail_task(
        self,
        *,
        task_id: str,
        assistant_id: str,
        claim_token: str,
        failed_reason_code: str,
        failed_reason_text: str | None = None,
    ) -> CollaborationTask:
        direct_fail_task = getattr(self.repository, "fail_task", None)
        if callable(direct_fail_task):
            return direct_fail_task(
                task_id=task_id,
                assistant_id=assistant_id,
                claim_token=claim_token,
                failed_reason_code=failed_reason_code,
                failed_reason_text=failed_reason_text,
            )

        with self._state_lock:
            state = self._load_state()
            task, claim = self._require_active_claim(
                state,
                task_id=task_id,
                assistant_id=assistant_id,
                claim_token=claim_token,
            )
            if task.status not in {"claimed", "running"}:
                raise ValueError("collaboration task cannot be failed")
            task.status = "failed"
            task.failed_at = utc_now()
            task.failed_reason_code = failed_reason_code.strip()
            task.failed_reason_text = (failed_reason_text or "").strip() or None
            claim.status = "released"
            self._append_event(
                state,
                task_id=task.id,
                event_type="failed",
                actor_type="assistant",
                actor_id=assistant_id.strip(),
                detail_json={
                    "failed_reason_code": task.failed_reason_code,
                    "failed_reason_text": task.failed_reason_text,
                },
            )
            self._write_state(state)
            return task

    def cancel_task(
        self, *, task_id: str, actor_id: str = "system", reason: str | None = None
    ) -> CollaborationTask:
        direct_cancel_task = getattr(self.repository, "cancel_task", None)
        if callable(direct_cancel_task):
            return direct_cancel_task(task_id=task_id, actor_id=actor_id, reason=reason)

        with self._state_lock:
            state = self._load_state()
            task = self._find_task(state, task_id.strip())
            if task is None:
                raise ValueError("collaboration task not found")
            if task.status in {"completed", "cancelled", "failed", "expired", "rejected"}:
                raise ValueError("collaboration task can no longer be cancelled")
            task.status = "cancelled"
            task.failed_reason_code = "cancelled"
            task.failed_reason_text = (reason or "").strip() or None
            task.failed_at = utc_now()
            active_claim = self._active_claim_for_task(state, task.id)
            if active_claim is not None:
                active_claim.status = "released"
            self._append_event(
                state,
                task_id=task.id,
                event_type="cancelled",
                actor_type="system",
                actor_id=actor_id,
                detail_json={"reason": task.failed_reason_text},
            )
            self._write_state(state)
            return task

    def cleanup_expired_objects(self) -> CollaborationCleanupResult:
        with self._state_lock:
            state = self._load_state()
            now = utc_now()
            result = CollaborationCleanupResult()
            running_object_ids = {
                task.input_object_id for task in state.tasks if task.status == "running"
            }
            expired_object_ids: set[str] = set()
            for item in state.objects:
                if item.status != "active" or item.expires_at is None or item.expires_at > now:
                    continue
                if item.id in running_object_ids:
                    continue
                item.status = "expired"
                expired_object_ids.add(item.id)
                result.expired_objects += 1
            for permission in state.permissions:
                if permission.status != "active":
                    continue
                if permission.object_id in expired_object_ids:
                    permission.status = "expired"
                    result.expired_permissions += 1
            for task in state.tasks:
                if task.input_object_id not in expired_object_ids:
                    continue
                if task.status not in {"pending_approval", "pending_claim", "claimed"}:
                    continue
                task.status = "expired"
                task.failed_at = now
                task.failed_reason_code = "object_expired"
                task.failed_reason_text = "input shared object expired before execution"
                result.expired_tasks += 1
                self._append_event(
                    state,
                    task_id=task.id,
                    event_type="expired",
                    actor_type="system",
                    actor_id="system",
                    detail_json={"input_object_id": task.input_object_id},
                )
            for claim in state.claims:
                if claim.status != "active":
                    continue
                if claim.lease_expires_at <= now or any(
                    task.id == claim.task_id and task.status == "expired" for task in state.tasks
                ):
                    claim.status = "expired"
                    result.expired_claims += 1
            self._write_state(state)
            return result
