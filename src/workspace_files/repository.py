from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from src.shared.async_utils import run_sync
from src.shared.config import (
    StorageBackendName,
    get_settings,
    resolve_workspace_file_metadata_backend,
)
from src.shared.json_store import read_json_object, write_json_object
from src.workspace_files.database_models import (
    ExternalPushRequestRow,
    WorkspaceFileEventRow,
    WorkspaceFileRow,
    workspace_file_active_path_key,
)
from src.workspace_files.models import (
    ExternalPushRequest,
    TargetBucket,
    WorkspaceFile,
    WorkspaceFileEvent,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class WorkspaceFileRepository(Protocol):
    async def alist_files(
        self,
        *,
        assistant_id: str | None = None,
        bucket: TargetBucket | None = None,
        include_deleted: bool = False,
        limit: int | None = None,
    ) -> list[WorkspaceFile]: ...

    async def alist_recent_files(
        self,
        *,
        assistant_id: str | None = None,
        bucket: TargetBucket | None = None,
        include_deleted: bool = False,
        limit: int = 100,
    ) -> list[WorkspaceFile]: ...

    async def aget_file(self, file_id: str) -> WorkspaceFile | None: ...

    async def aget_active_file_by_path(
        self,
        *,
        assistant_id: str,
        bucket: TargetBucket,
        filename: str,
    ) -> WorkspaceFile | None: ...

    async def asave_file(self, record: WorkspaceFile) -> WorkspaceFile: ...


class WorkspaceFileEventRepository(Protocol):
    async def aappend_event(self, event: WorkspaceFileEvent) -> WorkspaceFileEvent: ...

    async def alist_events(
        self,
        *,
        assistant_id: str | None = None,
        file_id: str | None = None,
        limit: int = 100,
    ) -> list[WorkspaceFileEvent]: ...


class ExternalPushRequestRepository(Protocol):
    async def alist_push_requests(
        self,
        *,
        assistant_id: str | None = None,
        limit: int = 100,
    ) -> list[ExternalPushRequest]: ...

    async def aget_push_request_by_approval(
        self,
        approval_id: str,
    ) -> ExternalPushRequest | None: ...

    async def asave_push_request(self, request: ExternalPushRequest) -> ExternalPushRequest: ...


class JsonWorkspaceFileRepository:
    def __init__(
        self,
        store_path: Path | None = None,
        *,
        files_store: Path | None = None,
        events_store: Path | None = None,
        push_store: Path | None = None,
    ) -> None:
        resolved_store = store_path or files_store
        if resolved_store is None:
            raise ValueError("store_path or files_store is required")
        self.store_path = resolved_store
        self._event_repo = (
            JsonWorkspaceFileEventRepository(events_store) if events_store is not None else None
        )
        self._push_repo = (
            JsonExternalPushRequestRepository(push_store) if push_store is not None else None
        )

    async def alist_files(
        self,
        *,
        assistant_id: str | None = None,
        bucket: TargetBucket | None = None,
        include_deleted: bool = False,
        limit: int | None = None,
    ) -> list[WorkspaceFile]:
        items = self._load_files()
        if assistant_id is not None:
            items = [item for item in items if item.assistant_id == assistant_id]
        if bucket is not None:
            items = [item for item in items if item.bucket == bucket]
        if not include_deleted:
            items = [item for item in items if not item.deleted]
        if limit is not None:
            items = sorted(
                items,
                key=lambda item: (item.updated_at, item.file_id),
                reverse=True,
            )[:limit]
        return items

    async def alist_recent_files(
        self,
        *,
        assistant_id: str | None = None,
        bucket: TargetBucket | None = None,
        include_deleted: bool = False,
        limit: int = 100,
    ) -> list[WorkspaceFile]:
        items = await self.alist_files(
            assistant_id=assistant_id,
            bucket=bucket,
            include_deleted=include_deleted,
        )
        return sorted(items, key=lambda record: record.updated_at, reverse=True)[:limit]

    async def aget_file(self, file_id: str) -> WorkspaceFile | None:
        return next((item for item in self._load_files() if item.file_id == file_id), None)

    async def aget_active_file_by_path(
        self,
        *,
        assistant_id: str,
        bucket: TargetBucket,
        filename: str,
    ) -> WorkspaceFile | None:
        return next(
            (
                item
                for item in self._load_files()
                if item.assistant_id == assistant_id
                and item.bucket == bucket
                and item.filename == filename
                and not item.deleted
            ),
            None,
        )

    async def asave_file(self, record: WorkspaceFile) -> WorkspaceFile:
        items = self._load_files()
        replaced = False
        next_items: list[WorkspaceFile] = []
        for existing in items:
            if existing.file_id == record.file_id:
                next_items.append(record)
                replaced = True
            else:
                next_items.append(existing)
        if not replaced:
            next_items.append(record)
        self._write_files(next_items)
        return record

    def _load_files(self) -> list[WorkspaceFile]:
        payload = read_json_object(self.store_path, None)
        if payload is None:
            return []
        items = payload.get("files", []) if isinstance(payload, dict) else []
        return [WorkspaceFile.model_validate(item) for item in items if isinstance(item, dict)]

    def _write_files(self, records: Sequence[WorkspaceFile]) -> None:
        write_json_object(
            self.store_path,
            {"files": [item.model_dump(mode="json") for item in records]},
        )

    def load_files(self) -> list[WorkspaceFile]:
        return self._load_files()

    def write_files(self, records: Sequence[WorkspaceFile]) -> None:
        self._write_files(records)

    def list_files(
        self,
        *,
        assistant_id: str | None = None,
        bucket: TargetBucket | None = None,
        include_deleted: bool = False,
    ) -> list[WorkspaceFile]:
        return run_sync(
            self.alist_files(
                assistant_id=assistant_id,
                bucket=bucket,
                include_deleted=include_deleted,
        )
            )

    def save_file(self, record: WorkspaceFile) -> WorkspaceFile:
        return run_sync(self.asave_file(record))

    def append_event(self, event: WorkspaceFileEvent) -> WorkspaceFileEvent:
        if self._event_repo is None:
            raise AttributeError("events_store is not configured for this repository")
        return run_sync(self._event_repo.aappend_event(event))

    def list_events(
        self,
        *,
        assistant_id: str | None = None,
        file_id: str | None = None,
        limit: int = 100,
    ) -> list[WorkspaceFileEvent]:
        if self._event_repo is None:
            raise AttributeError("events_store is not configured for this repository")
        return run_sync(
            self._event_repo.alist_events(
                assistant_id=assistant_id,
                file_id=file_id,
                limit=limit,
        )
            )

    def save_push_request(self, request: ExternalPushRequest) -> ExternalPushRequest:
        if self._push_repo is None:
            raise AttributeError("push_store is not configured for this repository")
        return run_sync(self._push_repo.asave_push_request(request))

    def list_push_requests(
        self,
        *,
        assistant_id: str | None = None,
        limit: int = 100,
    ) -> list[ExternalPushRequest]:
        if self._push_repo is None:
            raise AttributeError("push_store is not configured for this repository")
        return run_sync(self._push_repo.alist_push_requests(assistant_id=assistant_id, limit=limit))


class JsonWorkspaceFileEventRepository:
    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path

    async def aappend_event(self, event: WorkspaceFileEvent) -> WorkspaceFileEvent:
        items = self._load_events()
        items.append(event)
        write_json_object(
            self.store_path,
            {"events": [item.model_dump(mode="json") for item in items]},
        )
        return event

    async def alist_events(
        self,
        *,
        assistant_id: str | None = None,
        file_id: str | None = None,
        limit: int = 100,
    ) -> list[WorkspaceFileEvent]:
        items = self._load_events()
        if assistant_id is not None:
            items = [item for item in items if item.assistant_id == assistant_id]
        if file_id is not None:
            items = [item for item in items if item.file_id == file_id]
        return list(reversed(items[-limit:]))

    def _load_events(self) -> list[WorkspaceFileEvent]:
        payload = read_json_object(self.store_path, None)
        if payload is None:
            return []
        items = payload.get("events", []) if isinstance(payload, dict) else []
        return [WorkspaceFileEvent.model_validate(item) for item in items if isinstance(item, dict)]


class JsonExternalPushRequestRepository:
    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path

    async def alist_push_requests(
        self,
        *,
        assistant_id: str | None = None,
        limit: int = 100,
    ) -> list[ExternalPushRequest]:
        items = self._load_requests()
        if assistant_id is not None:
            items = [item for item in items if item.assistant_id == assistant_id]
        return list(reversed(items[-limit:]))

    async def aget_push_request_by_approval(
        self,
        approval_id: str,
    ) -> ExternalPushRequest | None:
        items = self._load_requests()
        return next((item for item in reversed(items) if item.approval_id == approval_id), None)

    async def asave_push_request(self, request: ExternalPushRequest) -> ExternalPushRequest:
        items = self._load_requests()
        replaced = False
        next_items: list[ExternalPushRequest] = []
        for existing in items:
            if existing.push_id == request.push_id:
                next_items.append(request)
                replaced = True
            else:
                next_items.append(existing)
        if not replaced:
            next_items.append(request)
        write_json_object(
            self.store_path,
            {"push_requests": [item.model_dump(mode="json") for item in next_items]},
        )
        return request

    def _load_requests(self) -> list[ExternalPushRequest]:
        payload = read_json_object(self.store_path, None)
        if payload is None:
            return []
        items = payload.get("push_requests", []) if isinstance(payload, dict) else []
        return [
            ExternalPushRequest.model_validate(item) for item in items if isinstance(item, dict)
        ]


def _resolve_metadata_backend(backend: str | None) -> str:
    return resolve_workspace_file_metadata_backend(backend)


def build_workspace_file_repository(
    *,
    backend: str | None = None,
) -> WorkspaceFileRepository:
    if _resolve_metadata_backend(backend) == "mysql":
        # 模块内 DatabaseWorkspaceFileRepository(行 564)是 shared 实现的惰性代理
        return DatabaseWorkspaceFileRepository()
    from src.workspace_files.sqlite_repository import SQLiteWorkspaceFileRepository

    return SQLiteWorkspaceFileRepository()


def build_workspace_file_event_repository(
    *,
    backend: str | None = None,
) -> WorkspaceFileEventRepository:
    # DatabaseWorkspaceFileEventRepository 是同一代理的别名（统一实现文件+事件协议）
    if _resolve_metadata_backend(backend) == "mysql":
        return DatabaseWorkspaceFileEventRepository()
    from src.workspace_files.sqlite_repository import SQLiteWorkspaceFileEventRepository

    return SQLiteWorkspaceFileEventRepository()


def build_external_push_request_repository(
    *,
    backend: StorageBackendName | None = None,
) -> ExternalPushRequestRepository:
    if _resolve_metadata_backend(backend) == "mysql":
        return DatabaseExternalPushRequestRepository()
    settings = get_settings()
    return JsonExternalPushRequestRepository(settings.workspace_push_requests_store_path)


def _workspace_file_from_row(row: WorkspaceFileRow) -> WorkspaceFile:
    storage_backend = row.storage_backend
    if storage_backend == "disk":
        storage_backend = "local"
    object_key = row.object_key or row.storage_key
    storage_path = row.storage_path or row.object_url
    object_url = row.object_url or storage_path
    checksum = row.checksum_sha256 or row.checksum
    last_error = row.last_error or row.sync_error or row.storage_error
    direction = row.direction
    storage_status = row.storage_status or row.sync_status
    sync_status = row.sync_status or storage_status
    storage_provider = row.storage_provider or _storage_provider_for_backend(storage_backend)
    return WorkspaceFile.model_validate(
        {
            "file_id": row.file_id,
            "assistant_id": row.assistant_id,
            "bucket": row.bucket,
            "filename": row.filename,
            "relative_path": row.relative_path,
            "direction": direction,
            "size_bytes": row.size_bytes,
            "content_type": row.content_type,
            "owner_user_id": row.owner_user_id,
            "uploader_user_id": row.uploader_user_id,
            "source_type": row.source_type,
            "source_ref": row.source_ref,
            "file_role": row.file_role,
            "source_file_id": row.source_file_id,
            "storage_provider": storage_provider,
            "storage_key": row.storage_key or object_key,
            "storage_path": storage_path,
            "storage_status": storage_status,
            "storage_error": row.storage_error or last_error,
            "storage_backend": storage_backend,
            "object_key": object_key,
            "object_url": object_url,
            "checksum": checksum,
            "scan_fingerprint": getattr(row, "scan_fingerprint", ""),
            "sync_status": sync_status,
            "last_error": last_error,
            "workspace_last_modified_at": None,
            "last_synced_at": row.last_synced_at,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "deleted": row.deleted,
        }
    )


def _apply_workspace_file_to_row(row: WorkspaceFileRow, record: WorkspaceFile) -> None:
    row.assistant_id = record.assistant_id
    row.bucket = record.bucket
    row.filename = record.filename
    row.relative_path = record.relative_path
    row.direction = record.direction
    row.size_bytes = record.size_bytes
    row.content_type = record.content_type
    row.owner_user_id = record.owner_user_id
    row.uploader_user_id = record.uploader_user_id
    row.source_type = record.source_type
    row.source_ref = record.source_ref
    row.active_path_key = workspace_file_active_path_key(
        record.assistant_id,
        str(record.bucket),
        record.filename,
        deleted=record.deleted,
    )
    row.file_role = record.file_role
    row.source_file_id = record.source_file_id
    row.storage_provider = record.storage_provider or _storage_provider_for_backend(
        record.storage_backend
    )
    row.storage_key = record.storage_key or record.object_key
    row.storage_path = record.storage_path or record.relative_path
    row.storage_status = record.storage_status or record.sync_status
    row.storage_error = record.storage_error or record.last_error
    row.storage_backend = record.storage_backend
    row.object_key = record.object_key or record.storage_key
    row.object_url = record.object_url or record.storage_path
    row.checksum = record.checksum
    row.checksum_sha256 = record.checksum
    if hasattr(row, "scan_fingerprint"):
        row.scan_fingerprint = record.scan_fingerprint
    row.sync_status = record.sync_status or record.storage_status
    row.last_error = record.last_error or record.storage_error
    row.sync_error = record.last_error or record.storage_error
    row.last_synced_at = record.last_synced_at
    row.created_at = record.created_at
    row.updated_at = record.updated_at
    row.deleted = record.deleted


def _storage_provider_for_backend(storage_backend: str) -> str:
    if storage_backend in {"cos", "obs"}:
        return storage_backend
    return "local"


def _workspace_file_event_from_row(row: WorkspaceFileEventRow) -> WorkspaceFileEvent:
    return WorkspaceFileEvent.model_validate(
        {
            "event_id": row.event_id,
            "file_id": row.file_id,
            "assistant_id": row.assistant_id,
            "action": row.action,
            "user_id": row.user_id,
            "source_type": row.source_type,
            "source_ref": row.source_ref,
            "detail": row.detail,
            "created_at": row.created_at,
        }
    )


def _apply_workspace_file_event_to_row(
    row: WorkspaceFileEventRow,
    event: WorkspaceFileEvent,
) -> None:
    row.file_id = event.file_id
    row.assistant_id = event.assistant_id
    row.action = event.action
    row.user_id = event.user_id
    row.source_type = event.source_type
    row.source_ref = event.source_ref
    row.detail = event.detail
    row.created_at = event.created_at


def _push_request_from_row(row: ExternalPushRequestRow) -> ExternalPushRequest:
    return ExternalPushRequest.model_validate(
        {
            "push_id": row.push_id,
            "user_id": row.user_id,
            "assistant_id": row.assistant_id,
            "filename": row.filename,
            "bucket": row.bucket,
            "content_type": row.content_type,
            # Keep compatibility with older database models that do not persist
            # object-storage metadata on external push requests.
            "storage_backend": getattr(row, "storage_backend", ""),
            "storage_key": getattr(row, "storage_key", ""),
            "storage_url": getattr(row, "storage_url", ""),
            "source_type": row.source_type,
            "source_ref": row.source_ref,
            "status": row.status,
            "approval_id": row.approval_id,
            "staged_path": row.staged_path,
            "error": row.error,
            "created_at": row.created_at,
        }
    )


def _apply_push_request_to_row(
    row: ExternalPushRequestRow,
    request: ExternalPushRequest,
) -> None:
    row.user_id = request.user_id
    row.assistant_id = request.assistant_id
    row.filename = request.filename
    row.bucket = request.bucket
    row.content_type = request.content_type
    if hasattr(row, "storage_backend"):
        row.storage_backend = request.storage_backend
    if hasattr(row, "storage_key"):
        row.storage_key = request.storage_key
    if hasattr(row, "storage_url"):
        row.storage_url = request.storage_url
    row.source_type = request.source_type
    row.source_ref = request.source_ref
    row.status = request.status
    row.approval_id = request.approval_id
    row.staged_path = request.staged_path
    row.error = request.error
    row.created_at = request.created_at


class DatabaseWorkspaceFileRepository:
    """Backward-compatible lazy proxy for the shared database repository."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        from src.shared.repositories import DatabaseWorkspaceFileRepository as Impl

        self._impl = Impl(*args, **kwargs)

    def __getattr__(self, name: str) -> object:
        return getattr(self._impl, name)


DatabaseWorkspaceFileEventRepository = DatabaseWorkspaceFileRepository
DatabaseExternalPushRequestRepository = DatabaseWorkspaceFileRepository
