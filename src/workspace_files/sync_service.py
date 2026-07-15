"""Scan assistant workspaces and sync result files back into storage metadata."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from src.assistants.service import AssistantService
from src.shared.config import get_settings
from src.shared.logging import get_logger
from src.shared.object_storage import WorkspaceObjectStorage
from src.workspace_files.models import WorkspaceFile, WorkspaceFileEvent, utc_now
from src.workspace_files.repository import WorkspaceFileRepository, build_workspace_file_repository
from src.workspace_files.storage_keys import (
    build_assistant_storage_scope,
    build_workspace_object_key,
)

log = get_logger(__name__)
SYNC_BUCKETS: tuple[str, ...] = ("temp", "persistent", "skills", "plugins")
CAPABILITY_BUCKETS: frozenset[str] = frozenset({"skills", "plugins"})
WorkspaceSyncScope = tuple[str, str]


@dataclass(slots=True)
class WorkspaceSyncStats:
    scanned_files: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    removed: int = 0
    drifted: int = 0
    metadata_degraded: bool = False


@dataclass(slots=True)
class _StoredBlobResult:
    backend: str
    key: str
    url: str


class _WorkspaceObjectStorageAdapter:
    def __init__(self) -> None:
        self._storage = WorkspaceObjectStorage()

    def is_enabled(self) -> bool:
        return True

    def put_bytes(self, *, key: str, content: bytes) -> _StoredBlobResult:
        stored = self._storage.put_bytes(key=key, content=content)
        return _StoredBlobResult(
            backend=stored.backend,
            key=stored.key,
            url=stored.url,
        )

    def delete(self, *, key: str) -> None:
        self._storage.delete(key=key)


class WorkspaceSyncService:
    def __init__(
        self,
        *,
        assistant_service: AssistantService | None = None,
        repository: WorkspaceFileRepository | None = None,
        blob_storage: object | None = None,
    ) -> None:
        self.assistant_svc = assistant_service or AssistantService()
        self.repository = repository or build_workspace_file_repository()
        self.blob_storage = blob_storage or self._default_blob_storage()

    async def _alist_files(
        self,
        *,
        assistant_id: str,
        include_deleted: bool,
    ) -> list[WorkspaceFile]:
        if hasattr(self.repository, "alist_files"):
            return await self.repository.alist_files(
                assistant_id=assistant_id,
                include_deleted=include_deleted,
            )
        return await asyncio.to_thread(
            self.repository.list_files,
            assistant_id,
            include_deleted=include_deleted,
        )

    async def _asave_file(self, record: WorkspaceFile) -> WorkspaceFile:
        if hasattr(self.repository, "asave_file"):
            return await self.repository.asave_file(record)
        return await asyncio.to_thread(self.repository.save_file, record)

    async def _aappend_event(self, event: WorkspaceFileEvent) -> WorkspaceFileEvent:
        if hasattr(self.repository, "aappend_event"):
            return await self.repository.aappend_event(event)
        return await asyncio.to_thread(self.repository.append_event, event)

    def _default_blob_storage(self) -> _WorkspaceObjectStorageAdapter:
        return _WorkspaceObjectStorageAdapter()

    def _storage_key_for(
        self,
        *,
        assistant_id: str,
        assistant_name: str,
        bucket: str,
        relative_name: str,
    ) -> str:
        if bucket in {"skills", "plugins"}:
            scope = build_assistant_storage_scope(
                assistant_id=assistant_id,
                assistant_name="",
            )
            normalized_name = relative_name.replace("\\", "/").lstrip("/")
            return f"{scope}/{bucket}/{normalized_name}"
        return build_workspace_object_key(
            assistant_id=assistant_id,
            assistant_name=assistant_name,
            bucket=bucket,
            relative_name=relative_name,
        )

    def _fingerprint_for(self, path: Path) -> str:
        stat = path.stat()
        return f"{stat.st_mtime_ns}:{stat.st_size}"

    def _is_capability_bucket(self, bucket: str) -> bool:
        return bucket in CAPABILITY_BUCKETS

    def _source_type_for_bucket(self, bucket: str) -> str:
        if self._is_capability_bucket(bucket):
            return "capability_package"
        return "assistant_output"

    def _apply_workspace_metadata(self, record: WorkspaceFile, bucket: str) -> None:
        if self._is_capability_bucket(bucket):
            record.direction = "inbound"
            record.file_role = "input"
            record.source_type = "capability_package"
            record.source_ref = "workspace_scan"
            record.source_file_id = None
            return
        record.source_type = "assistant_output"
        record.source_ref = "workspace_scan"

    def _existing_file_map(
        self,
        records: list[WorkspaceFile],
    ) -> dict[tuple[str, str], WorkspaceFile]:
        selected: dict[tuple[str, str], WorkspaceFile] = {}
        for record in records:
            key = (record.bucket, record.filename)
            current = selected.get(key)
            if current is None:
                selected[key] = record
                continue
            if current.deleted and not record.deleted:
                selected[key] = record
                continue
            if current.deleted == record.deleted and record.updated_at > current.updated_at:
                selected[key] = record
        return selected

    def _delete_blob(self, key: str) -> None:
        if not key or self.blob_storage is None:
            return
        delete = getattr(self.blob_storage, "delete", None)
        if not callable(delete):
            return
        try:
            delete(key=key)
        except TypeError:
            delete(key)

    async def _aworkspace_root_for(self, assistant_id: str) -> Path:
        assistant = await self.assistant_svc.aget_assistant(assistant_id)
        if assistant is None or getattr(assistant, "deleted", False):
            raise ValueError(f"assistant {assistant_id!r} not found")
        runtime_root = (assistant.runtime.workspace_root or "").strip()
        if runtime_root:
            candidate = Path(runtime_root)
            if candidate.exists():
                return candidate
        return await self.assistant_svc.aresolve_assistant_workspace_root(assistant_id)

    async def _aiter_workspace_files(
        self,
        assistant_id: str,
        scopes: list[WorkspaceSyncScope] | None = None,
    ) -> list[tuple[str, Path, str]]:
        root = await self._aworkspace_root_for(assistant_id)
        items: list[tuple[str, Path, str]] = []
        seen: set[tuple[str, str]] = set()
        bucket_scopes = scopes or [(bucket, "") for bucket in SYNC_BUCKETS]
        for bucket, prefix in bucket_scopes:
            bucket_dir = root / bucket
            if not bucket_dir.exists():
                continue
            scope_prefix = prefix.replace("\\", "/").strip("/")
            scan_root = bucket_dir / scope_prefix if scope_prefix else bucket_dir
            if not scan_root.exists():
                continue
            for path in sorted(scan_root.rglob("*")):
                if not path.is_file():
                    continue
                relative_name = path.relative_to(bucket_dir).as_posix()
                if relative_name.startswith("."):
                    continue
                key = (bucket, relative_name)
                if key in seen:
                    continue
                seen.add(key)
                items.append((bucket, path, relative_name))
        return items

    async def _amark_missing_records(
        self,
        *,
        assistant_id: str,
        existing: dict[tuple[str, str], WorkspaceFile],
        seen_keys: set[tuple[str, str]],
        scopes: list[WorkspaceSyncScope] | None,
        stats: WorkspaceSyncStats,
    ) -> None:
        now = utc_now()
        for key, record in existing.items():
            if key in seen_keys or record.deleted:
                continue
            if not self._record_matches_scopes(key, scopes):
                continue

            should_delete = (
                record.direction == "outbound"
                or record.file_role == "result"
                or record.origin == "assistant_output"
                or record.source_type
                in {
                    "assistant_output",
                    "assistant_result",
                    "capability_package",
                    "workspace_scan",
                    "workspace-scan",
                }
            )
            record.updated_at = now
            record.workspace_path = ""
            record.last_synced_at = now
            record.scan_fingerprint = ""
            if should_delete:
                self._delete_blob(record.object_key or record.storage_key)
                record.deleted = True
                record.storage_status = "deleted_from_workspace"
                record.sync_status = "deleted_from_workspace"
                stats.removed += 1
                action = "delete"
            else:
                record.sync_status = "missing_from_workspace"
                stats.drifted += 1
                action = "overwrite"

            record = await self._asave_file(record)
            await self._aappend_event(
                WorkspaceFileEvent(
                    file_id=record.file_id,
                    assistant_id=assistant_id,
                    action=action,
                    user_id="",
                    source_type="workspace_scan",
                    source_ref="workspace_missing",
                    detail={
                        "filename": record.filename,
                        "bucket": record.bucket,
                        "sync_status": record.sync_status,
                    },
            )
                )

    def _record_matches_scopes(
        self,
        key: tuple[str, str],
        scopes: list[WorkspaceSyncScope] | None,
    ) -> bool:
        if scopes is None:
            return True
        bucket, filename = key
        normalized_filename = filename.replace("\\", "/").strip("/")
        for scope_bucket, scope_prefix in scopes:
            if bucket != scope_bucket:
                continue
            normalized_prefix = scope_prefix.replace("\\", "/").strip("/")
            if not normalized_prefix:
                return True
            if (
                normalized_filename == normalized_prefix
                or normalized_filename.startswith(f"{normalized_prefix}/")
            ):
                return True
        return False

    async def _async_sync_assistant_workspace(
        self,
        assistant_id: str,
        *,
        scopes: list[WorkspaceSyncScope] | None = None,
        mark_missing: bool,
    ) -> WorkspaceSyncStats:
        stats = WorkspaceSyncStats()
        assistant = await self.assistant_svc.aget_assistant(assistant_id)
        assistant_name = assistant.name if assistant is not None else ""
        try:
            existing_files = await self._alist_files(
                assistant_id=assistant_id,
                include_deleted=True,
            )
        except SQLAlchemyError as exc:
            stats.metadata_degraded = True
            existing_files = []
            log.warning(
                "workspace_sync_metadata_read_failed",
                assistant_id=assistant_id,
                error=f"{type(exc).__name__}: {exc}",
            )
        existing = self._existing_file_map(existing_files)
        seen_keys: set[tuple[str, str]] = set()
        for bucket, path, relative_name in await self._aiter_workspace_files(
            assistant_id,
            scopes=scopes,
        ):
            stats.scanned_files += 1
            seen_keys.add((bucket, relative_name))
            fingerprint = self._fingerprint_for(path)
            matched = existing.get((bucket, relative_name))
            if (
                matched is not None
                and matched.scan_fingerprint == fingerprint
                and not matched.deleted
            ):
                stats.skipped += 1
                continue

            content = path.read_bytes()
            blob = None
            if self.blob_storage is not None:
                blob = self.blob_storage.put_bytes(
                    key=self._storage_key_for(
                        assistant_id=assistant_id,
                        assistant_name=assistant_name,
                        bucket=bucket,
                        relative_name=relative_name,
                    ),
                    content=content,
                )

            now = utc_now()
            if matched is None or matched.deleted:
                source_type = self._source_type_for_bucket(bucket)
                record = WorkspaceFile(
                    assistant_id=assistant_id,
                    bucket=bucket,
                    filename=relative_name,
                    relative_path=f"{bucket}/{relative_name}",
                    direction=("inbound" if self._is_capability_bucket(bucket) else "outbound"),
                    file_role="input" if self._is_capability_bucket(bucket) else "result",
                    size_bytes=len(content),
                    content_type=matched.content_type if matched is not None else "",
                    uploader_user_id="",
                    source_type=source_type,
                    source_ref="workspace_scan",
                    storage_backend=blob.backend if blob is not None else "local",
                    storage_key=blob.key if blob is not None else "",
                    storage_url=blob.url if blob is not None else "",
                    sync_status="synced",
                    origin=source_type,
                    workspace_path=str(path),
                    last_synced_at=now,
                    scan_fingerprint=fingerprint,
                    created_at=matched.created_at if matched is not None else now,
                    updated_at=now,
                    deleted=False,
                )
                stats.created += 1
                action = "upload"
            else:
                matched.size_bytes = len(content)
                self._apply_workspace_metadata(matched, bucket)
                matched.storage_backend = (
                    blob.backend if blob is not None else matched.storage_backend
                )
                matched.storage_key = blob.key if blob is not None else matched.storage_key
                matched.storage_url = blob.url if blob is not None else matched.storage_url
                matched.sync_status = "synced"
                matched.origin = self._source_type_for_bucket(bucket)
                matched.workspace_path = str(path)
                matched.last_synced_at = now
                matched.scan_fingerprint = fingerprint
                matched.updated_at = now
                matched.deleted = False
                record = matched
                stats.updated += 1
                action = "overwrite"

            try:
                record = await self._asave_file(record)
                await self._aappend_event(
                    WorkspaceFileEvent(
                        file_id=record.file_id,
                        assistant_id=assistant_id,
                        action=action,
                        user_id="",
                        source_type=record.source_type,
                        source_ref="workspace_scan",
                        detail={
                            "filename": record.filename,
                            "bucket": record.bucket,
                            "size": record.size_bytes,
                            "workspace_path": record.workspace_path,
                        },
                )
                    )
            except SQLAlchemyError as exc:
                stats.metadata_degraded = True
                log.warning(
                    "workspace_sync_metadata_write_failed",
                    assistant_id=assistant_id,
                    bucket=bucket,
                    filename=relative_name,
                    storage_key=record.object_key or record.storage_key,
                    error=f"{type(exc).__name__}: {exc}",
                )
        if mark_missing:
            if stats.metadata_degraded:
                log.warning(
                    "workspace_sync_missing_mark_skipped",
                    assistant_id=assistant_id,
                    reason="metadata repository unavailable",
                )
            else:
                await self._amark_missing_records(
                    assistant_id=assistant_id,
                    existing=existing,
                    seen_keys=seen_keys,
                    scopes=scopes,
                    stats=stats,
                )
        return stats

    async def async_sync_assistant_workspace(self, assistant_id: str) -> WorkspaceSyncStats:
        return await self._async_sync_assistant_workspace(
            assistant_id,
            mark_missing=True,
        )

    async def async_sync_assistant_workspace_scopes(
        self,
        assistant_id: str,
        scopes: list[WorkspaceSyncScope],
        *,
        mark_missing: bool = False,
    ) -> WorkspaceSyncStats:
        return await self._async_sync_assistant_workspace(
            assistant_id,
            scopes=scopes,
            mark_missing=mark_missing,
        )

    def sync_assistant_workspace(self, assistant_id: str) -> WorkspaceSyncStats:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.async_sync_assistant_workspace(assistant_id))
        raise RuntimeError(
            "sync_assistant_workspace cannot be called while an event loop is already running; "
            "use async_sync_assistant_workspace instead"
        )

    def sync_assistant_workspace_scopes(
        self,
        assistant_id: str,
        scopes: list[WorkspaceSyncScope],
        *,
        mark_missing: bool = False,
    ) -> WorkspaceSyncStats:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.async_sync_assistant_workspace_scopes(
                    assistant_id,
                    scopes,
                    mark_missing=mark_missing,
            )
                )
        raise RuntimeError(
            "sync_assistant_workspace_scopes cannot be called while an event loop is already "
            "running; use async_sync_assistant_workspace_scopes instead"
        )

    def scan_all_assistants(self) -> dict[str, WorkspaceSyncStats]:
        results: dict[str, WorkspaceSyncStats] = {}
        for assistant in self.assistant_svc.list_assistants():
            if getattr(assistant, "deleted", False):
                continue
            try:
                results[assistant.assistant_id] = self.sync_assistant_workspace(
                    assistant.assistant_id
                )
            except Exception as exc:  # pragma: no cover - safety net for background loop
                log.warning(
                    "workspace_sync_failed",
                    assistant_id=assistant.assistant_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
        return results

    async def run_forever(self) -> None:
        interval = max(1, get_settings().workspace_scan_interval_seconds)
        log.info(
            "workspace_scan_loop_started",
            interval_seconds=interval,
        )
        while True:
            try:
                summary = await asyncio.to_thread(self.scan_all_assistants)
                if summary:
                    log.info(
                        "workspace_scan_completed",
                        assistants=len(summary),
                        scanned_files=sum(item.scanned_files for item in summary.values()),
                        created=sum(item.created for item in summary.values()),
                        updated=sum(item.updated for item in summary.values()),
                        skipped=sum(item.skipped for item in summary.values()),
                        removed=sum(item.removed for item in summary.values()),
                        drifted=sum(item.drifted for item in summary.values()),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - background safety net
                log.warning(
                    "workspace_scan_loop_failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            await asyncio.sleep(interval)
