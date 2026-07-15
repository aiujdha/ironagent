"""Service layer for workspace file upload, sync, download, and delete."""

from __future__ import annotations

import hashlib
import posixpath
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from shutil import rmtree
from time import monotonic
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from src.approval.service import ApprovalService
from src.assistants.service import AssistantService
from src.shared.async_utils import run_sync
from src.shared.config import approval_mode_disabled, get_settings
from src.shared.cos_network import (
    configure_cos_sdk_logging,
    cos_config_proxies,
    ensure_cos_no_proxy,
)
from src.shared.logging import get_logger
from src.shared.object_storage import WorkspaceObjectStorage, download_cos_url_bytes
from src.users.service import UserService
from src.workspace_files.models import (
    ExternalPushRequest,
    TargetBucket,
    WorkspaceFile,
    WorkspaceFileEvent,
    utc_now,
)
from src.workspace_files.repository import (
    ExternalPushRequestRepository,
    JsonExternalPushRequestRepository,
    JsonWorkspaceFileEventRepository,
    JsonWorkspaceFileRepository,
    WorkspaceFileEventRepository,
    WorkspaceFileRepository,
    build_external_push_request_repository,
    build_workspace_file_event_repository,
    build_workspace_file_repository,
)
from src.workspace_files.storage_keys import (
    build_assistant_storage_scope,
    build_workspace_object_key,
)
from src.workspace_files.workspace_writer import WorkspaceWriter

log = get_logger(__name__)

FORBIDDEN_BASENAMES = {name.upper() for name in ("AGENTS.md", "USER.md", ".env")}
_PATH_TRAVERSAL_RE = re.compile(r"(^|[\\/])\.\.($|[\\/])")
WORKSPACE_STORAGE_MARKER_FILENAME = ".keep"
WORKSPACE_STORAGE_MARKER_CONTENT_TYPE = "application/x-directory"
WORKSPACE_STORAGE_MARKER_BUCKETS: tuple[TargetBucket, ...] = ("persistent", "temp")
WORKSPACE_SCAN_BUCKETS: tuple[TargetBucket, ...] = ("temp", "persistent", "skills", "plugins")
WORKSPACE_FILE_UPLOAD_BUCKETS: frozenset[TargetBucket] = frozenset({"temp", "persistent"})
WORKSPACE_FILE_READ_ONLY_BUCKETS: frozenset[TargetBucket] = frozenset({"skills", "plugins"})
WORKSPACE_RESULT_BUCKETS: tuple[TargetBucket, ...] = ("temp", "persistent")
_FILE_SYNC_STATUS_RANK = {
    "synced": 5,
    "synced_to_workspace": 4,
    "uploaded": 3,
    "stored": 3,
    "pending": 2,
    "failed": 1,
}


def _is_workspace_storage_marker_name(filename: str) -> bool:
    normalized = filename.replace("\\", "/").strip().lstrip("/")
    return normalized in {WORKSPACE_STORAGE_MARKER_FILENAME, ".folder"}


def _preferred_active_workspace_file(left: WorkspaceFile, right: WorkspaceFile) -> WorkspaceFile:
    left_updated = left.updated_at or left.created_at
    right_updated = right.updated_at or right.created_at
    left_rank = (
        _FILE_SYNC_STATUS_RANK.get(left.sync_status, 0),
        left_updated,
        left.created_at,
        left.file_id,
    )
    right_rank = (
        _FILE_SYNC_STATUS_RANK.get(right.sync_status, 0),
        right_updated,
        right.created_at,
        right.file_id,
    )
    return left if left_rank >= right_rank else right


def _deduplicate_active_workspace_files(items: list[WorkspaceFile]) -> list[WorkspaceFile]:
    selected: dict[tuple[str, str, str], WorkspaceFile] = {}
    for item in items:
        key = (item.assistant_id, item.bucket, item.filename)
        existing = selected.get(key)
        selected[key] = (
            item if existing is None else _preferred_active_workspace_file(existing, item)
        )
    return [
        item
        for item in items
        if selected.get((item.assistant_id, item.bucket, item.filename)) is item
    ]


def _normalize_workspace_scan_path(value: str | None) -> str:
    normalized = (value or "").replace("\\", "/").strip().strip("/")
    if not normalized:
        return ""
    if normalized.startswith("workspace/"):
        normalized = normalized[len("workspace/") :]
    if _PATH_TRAVERSAL_RE.search(normalized):
        raise FileSecurityError("path traversal is not allowed")
    normalized = posixpath.normpath(normalized)
    if normalized in {"", "."}:
        return ""
    if normalized == ".." or normalized.startswith("../"):
        raise FileSecurityError("path traversal is not allowed")
    if normalized.startswith("/") or (len(normalized) >= 2 and normalized[1] == ":"):
        raise FileSecurityError("absolute paths are not allowed")
    return normalized.strip("/")


def _bucket_relative_result_prefix(
    result_path_prefix: str | None,
    bucket: TargetBucket,
) -> str | None:
    normalized = _normalize_workspace_scan_path(result_path_prefix)
    if not normalized:
        return ""
    for known_bucket in WORKSPACE_SCAN_BUCKETS:
        if normalized == known_bucket:
            return "" if known_bucket == bucket else None
        bucket_prefix = f"{known_bucket}/"
        if normalized.startswith(bucket_prefix):
            if known_bucket != bucket:
                return None
            return normalized[len(bucket_prefix) :].strip("/")
    return normalized


def _matches_bucket_relative_prefix(filename: str, prefix: str) -> bool:
    normalized_filename = _normalize_workspace_scan_path(filename)
    normalized_prefix = _normalize_workspace_scan_path(prefix)
    if not normalized_prefix:
        return True
    return normalized_filename == normalized_prefix or normalized_filename.startswith(
        normalized_prefix + "/"
    )


def _validate_result_scan_scope(
    result_path_prefix: str,
    bucket: TargetBucket | None,
) -> None:
    if not result_path_prefix:
        return
    if bucket is not None and bucket not in WORKSPACE_RESULT_BUCKETS:
        raise FileSecurityError("result sync bucket must be temp or persistent")
    normalized = _normalize_workspace_scan_path(result_path_prefix)
    if not normalized:
        return
    if normalized in WORKSPACE_SCAN_BUCKETS:
        raise FileSecurityError("result_path_prefix must include task directory")
    first_component = normalized.split("/", 1)[0]
    if (
        first_component in WORKSPACE_SCAN_BUCKETS
        and first_component not in WORKSPACE_RESULT_BUCKETS
    ):
        raise FileSecurityError("result sync prefix must be under temp or persistent")

    buckets: tuple[TargetBucket, ...] = (
        (bucket,) if bucket is not None else WORKSPACE_RESULT_BUCKETS
    )
    matching_prefixes = [
        prefix
        for current_bucket in buckets
        if (prefix := _bucket_relative_result_prefix(normalized, current_bucket)) is not None
    ]
    if not matching_prefixes:
        raise FileSecurityError("result_path_prefix does not match result bucket")
    if not any(len(prefix.split("/")) >= 2 for prefix in matching_prefixes if prefix):
        raise FileSecurityError("result_path_prefix must include task directory")


def _is_unbound_workspace_scan_ref(value: str | None) -> bool:
    return (value or "").strip() in {"", "workspace_scan", "workspace-scan"}


@dataclass(slots=True)
class WorkspaceScanSummary:
    assistants_scanned: int = 0
    files_scanned: int = 0
    files_synced: int = 0
    files_deleted: int = 0
    errors: list[str] | None = None
    records: list[WorkspaceFile] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []
        if self.records is None:
            self.records = []

    def __iter__(self):
        return iter(self.records or [])

    def __len__(self) -> int:
        return len(self.records or [])

    def __getitem__(self, index: int) -> WorkspaceFile:
        return (self.records or [])[index]


@dataclass(slots=True)
class WorkspaceFileDownloadResult:
    record: WorkspaceFile
    content: bytes
    download_source: str

    def __iter__(self):
        yield self.record
        yield self.content


class _LegacyFileRecordsAdapter:
    def __init__(self, repository: WorkspaceFileRepository) -> None:
        self._repository = repository

    def load_files(self) -> list[WorkspaceFile]:
        if hasattr(self._repository, "load_files"):
            return list(self._repository.load_files())
        return run_sync(
            self._repository.alist_files(
                assistant_id=None,
                bucket=None,
                include_deleted=True,
            )
        )

    def write_files(self, files: list[WorkspaceFile]) -> None:
        if hasattr(self._repository, "write_files"):
            self._repository.write_files(files)
            return
        for item in files:
            run_sync(self._repository.asave_file(item))

    def list_files(
        self,
        assistant_id: str,
        *,
        bucket: TargetBucket | None = None,
        include_deleted: bool = False,
    ) -> list[WorkspaceFile]:
        return run_sync(
            self._repository.alist_files(
                assistant_id=assistant_id,
                bucket=bucket,
                include_deleted=include_deleted,
            )
        )

    def get_file(self, file_id: str) -> WorkspaceFile | None:
        return run_sync(self._repository.aget_file(file_id))


class _LegacyObjectStorageGatewayAdapter:
    provider = "cos"

    def __init__(self, storage: WorkspaceObjectStorage) -> None:
        self._storage = storage

    def is_enabled(self) -> bool:
        if hasattr(self._storage, "is_enabled"):
            return bool(self._storage.is_enabled())
        backend = getattr(self._storage, "backend", "")
        if backend:
            return backend != "local"
        return True

    def download_bytes(self, *, key: str) -> tuple[bool, bytes | str, str]:
        try:
            content = self._storage.get_bytes(key=key)
        except TypeError:
            content = self._storage.get_bytes(key)
        if content is None:
            return False, "object not found", "application/octet-stream"
        return True, content, "application/octet-stream"


class _LegacyRepositoryFacade:
    def __init__(
        self,
        file_repository: WorkspaceFileRepository,
        event_repository: WorkspaceFileEventRepository,
    ) -> None:
        self._file_repository = file_repository
        self._event_repository = event_repository

    def list_files(
        self,
        assistant_id: str,
        *,
        bucket: TargetBucket | None = None,
        include_deleted: bool = False,
    ) -> list[WorkspaceFile]:
        return run_sync(
            self._file_repository.alist_files(
                assistant_id=assistant_id,
                bucket=bucket,
                include_deleted=include_deleted,
            )
        )

    def get_file(self, file_id: str) -> WorkspaceFile | None:
        return run_sync(self._file_repository.aget_file(file_id))

    def save_file(self, record: WorkspaceFile) -> WorkspaceFile:
        return run_sync(self._file_repository.asave_file(record))

    def append_event(self, event: WorkspaceFileEvent) -> WorkspaceFileEvent:
        return run_sync(self._event_repository.aappend_event(event))


class _DisabledObjectStorage:
    backend = "cos"
    prefix = "workspace-files"

    def __init__(self) -> None:
        self.local_root = get_settings().workspace_object_storage_root

    def is_enabled(self) -> bool:
        return False

    def put_bytes(self, *, key: str, content: bytes, content_type: str = ""):
        object_key = key.strip().lstrip("/")
        if self.prefix and not object_key.startswith(f"{self.prefix}/"):
            object_key = f"{self.prefix}/{object_key}"
        path = self.local_root / object_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

        class Stored:
            backend = "cos"

            def __init__(self, object_key: str, path: Path) -> None:
                self.key = object_key
                self.url = str(path.resolve())

        return Stored(object_key, path)

    def get_bytes(self, key: str | None = None, *, key_: str | None = None) -> bytes | None:
        object_key = (key_ or key or "").strip().lstrip("/")
        if not object_key:
            return None
        path = self.local_root / object_key
        if not path.exists():
            return None
        return path.read_bytes()

    def delete(self, key: str | None = None, *, key_: str | None = None) -> None:
        object_key = (key_ or key or "").strip().lstrip("/")
        if not object_key:
            return None
        path = self.local_root / object_key
        path.unlink(missing_ok=True)
        return None


class FileSecurityError(Exception):
    pass


class WorkspaceFileService:
    _cos_capability_package_cache: dict[
        tuple[str, str, str, str, str],
        tuple[float, list[WorkspaceFile]],
    ] = {}
    _cos_capability_package_cache_ttl_seconds = 120.0

    def __init__(
        self,
        files_store: Path | None = None,
        events_store: Path | None = None,
        push_store: Path | None = None,
        assistant_service: AssistantService | None = None,
        user_service: UserService | None = None,
        approval_service: ApprovalService | None = None,
        file_repository: WorkspaceFileRepository | None = None,
        event_repository: WorkspaceFileEventRepository | None = None,
        push_repository: ExternalPushRequestRepository | None = None,
        object_storage: WorkspaceObjectStorage | None = None,
        repository: WorkspaceFileRepository | None = None,
        object_store: WorkspaceObjectStorage | None = None,
        blob_storage: WorkspaceObjectStorage | None = None,
    ) -> None:
        settings = get_settings()
        self.files_store = files_store or settings.workspace_files_store_path
        self.events_store = events_store or settings.workspace_file_events_store_path
        self.push_store = push_store or settings.workspace_push_requests_store_path
        self.assistant_svc = assistant_service or AssistantService()
        self.user_svc = user_service or UserService()
        self.approval_svc = approval_service or ApprovalService()
        resolved_file_repository = file_repository or repository
        resolved_object_storage = object_storage or object_store or blob_storage
        self.legacy_object_storage_import_enabled = (
            resolved_file_repository is None
            and object_storage is None
            and object_store is None
            and blob_storage is None
            and files_store is None
            and events_store is None
            and push_store is None
        )
        if resolved_object_storage is None:
            if any(path is not None for path in (files_store, events_store, push_store)):
                resolved_object_storage = _DisabledObjectStorage()
            else:
                resolved_object_storage = WorkspaceObjectStorage()
        if resolved_file_repository is not None:
            self.file_repository = resolved_file_repository
        elif files_store is not None:
            self.file_repository = JsonWorkspaceFileRepository(self.files_store)
        else:
            self.file_repository = build_workspace_file_repository()
        if event_repository is None and hasattr(self.file_repository, "aappend_event"):
            event_repository = self.file_repository
        if event_repository is not None:
            self.event_repository = event_repository
        elif events_store is not None:
            self.event_repository = JsonWorkspaceFileEventRepository(self.events_store)
        else:
            self.event_repository = build_workspace_file_event_repository()
        if push_repository is None and all(
            hasattr(self.file_repository, method)
            for method in ("aget_push_request_by_approval", "asave_push_request")
        ):
            push_repository = self.file_repository
        if push_repository is not None:
            self.push_repository = push_repository
        elif push_store is not None:
            self.push_repository = JsonExternalPushRequestRepository(self.push_store)
        else:
            self.push_repository = build_external_push_request_repository()
        self.object_storage = resolved_object_storage
        self.repository = _LegacyRepositoryFacade(
            self.file_repository,
            self.event_repository,
        )
        self.file_records = _LegacyFileRecordsAdapter(self.file_repository)
        self.object_storage_gateway = _LegacyObjectStorageGatewayAdapter(self.object_storage)
        self.workspace_writer = WorkspaceWriter(
            lambda assistant_id: self._workspace_dir(assistant_id)
        )

    @staticmethod
    def validate_filename(filename: str) -> None:
        if not filename or not filename.strip():
            raise FileSecurityError("filename must not be empty")
        normalized = filename.replace("\\", "/")
        basename = posixpath.basename(normalized)
        if basename.upper() in FORBIDDEN_BASENAMES:
            raise FileSecurityError(f"writing to {basename!r} is forbidden")
        if _PATH_TRAVERSAL_RE.search(filename):
            raise FileSecurityError("path traversal is not allowed")
        if filename.startswith("/") or (len(filename) >= 2 and filename[1] == ":"):
            raise FileSecurityError("absolute paths are not allowed")

    @staticmethod
    def compose_upload_filename(*, filename: str, target_path: str = "") -> str:
        normalized_target = target_path.replace("\\", "/").strip().strip("/")
        raw_filename = filename.replace("\\", "/")
        if (
            _PATH_TRAVERSAL_RE.search(filename)
            or raw_filename.startswith("/")
            or (len(filename) >= 2 and filename[1] == ":")
        ):
            WorkspaceFileService.validate_filename(filename)
        basename = posixpath.basename(filename.replace("\\", "/"))
        resolved = basename or filename
        if not normalized_target:
            WorkspaceFileService.validate_filename(resolved)
            return resolved
        composed = f"{normalized_target}/{resolved}"
        WorkspaceFileService.validate_filename(composed)
        return composed

    def resolve_assistant_id(
        self,
        *,
        assistant_id: str | None = None,
        user_id: str | None = None,
    ) -> str:
        if assistant_id:
            return assistant_id.strip()
        if user_id:
            resolved = self.user_svc.resolve_primary_assistant(user_id.strip())
            if resolved is None:
                raise ValueError(f"no assistant bound to user {user_id!r}")
            return resolved
        raise ValueError("either assistant_id or user_id must be provided")

    def _workspace_dir(self, assistant_id: str) -> Path:
        assistant = self.assistant_svc.get_assistant(assistant_id)
        if assistant is not None:
            try:
                return self.assistant_svc.resolve_assistant_workspace_root(assistant_id)
            except ValueError:
                log.warning(
                    "workspace_dir_resolve_failed",
                    assistant_id=assistant_id,
                )
        return get_settings().assistants_workspace_root / assistant_id

    def _bucket_dir(self, assistant_id: str, bucket: TargetBucket) -> Path:
        return self._workspace_dir(assistant_id) / bucket

    def _pending_payload_dir(self) -> Path:
        return self.push_store.parent / "pending_payloads"

    def _stage_pending_payload(self, *, content: bytes, filename: str) -> str:
        suffix = Path(filename).suffix
        staged_path = self._pending_payload_dir() / f"{uuid4()}{suffix}"
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        staged_path.write_bytes(content)
        return str(staged_path)

    def _cleanup_staged_payload(self, staged_path: str | None) -> None:
        if not staged_path:
            return
        path = Path(staged_path)
        if path.exists():
            path.unlink()

    def _object_key(self, *, assistant_id: str, bucket: TargetBucket, filename: str) -> str:
        assistant = self.assistant_svc.get_assistant(assistant_id)
        if bucket in {"skills", "plugins"}:
            scope = build_assistant_storage_scope(
                assistant_id=assistant_id,
                assistant_name="",
            )
            normalized_name = filename.replace("\\", "/").lstrip("/")
            return f"{scope}/{bucket}/{normalized_name}"
        return build_workspace_object_key(
            assistant_id=assistant_id,
            assistant_name=assistant.name if assistant is not None else "",
            bucket=bucket,
            relative_name=filename,
        )

    def _checksum_sha256(self, content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def _workspace_mtime(self, path: Path) -> datetime:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)

    def _assistant_exists(self, assistant_id: str) -> None:
        assistant = self.assistant_svc.get_assistant(assistant_id)
        if assistant is None or assistant.deleted:
            raise ValueError(f"assistant {assistant_id!r} does not exist")

    def _ensure_upload_bucket(self, bucket: TargetBucket) -> None:
        if bucket not in WORKSPACE_FILE_UPLOAD_BUCKETS:
            raise FileSecurityError(
                f"workspace file upload only supports temp or persistent; {bucket} is read-only"
            )

    def _ensure_file_view_mutable(self, record: WorkspaceFile, *, operation: str) -> None:
        if record.bucket in WORKSPACE_FILE_READ_ONLY_BUCKETS:
            raise FileSecurityError(
                f"{record.bucket} is read-only in workspace file views; "
                f"use capability management to {operation} packages"
            )

    def _record_workspace_refresh_event(
        self,
        record: WorkspaceFile,
        *,
        user_id: str = "",
        reason: str,
    ) -> None:
        run_sync(
            self.event_repository.aappend_event(
                WorkspaceFileEvent(
                    file_id=record.file_id,
                    assistant_id=record.assistant_id,
                    action="sync",
                    user_id=user_id,
                    source_type=record.source_type,
                    source_ref=record.source_ref,
                    detail={
                        "filename": record.filename,
                        "bucket": record.bucket,
                        "reason": reason,
                        "workspace_refresh_requested": True,
                        "workspace_reload_requested": True,
                        "sync_status": record.sync_status,
                    },
                )
            )
        )

    def _build_cos_client(self) -> Any | None:
        settings = get_settings()
        if not (
            settings.cos_secret_id.strip()
            and settings.cos_secret_key.strip()
            and settings.cos_bucket.strip()
            and settings.cos_region.strip()
        ):
            return None
        try:
            configure_cos_sdk_logging()
            from qcloud_cos import CosConfig, CosS3Client
        except ModuleNotFoundError:
            return None
        parsed = urlparse(settings.cos_endpoint.strip())
        endpoint_host = parsed.netloc or settings.cos_endpoint.strip()
        kwargs: dict[str, object] = {
            "Region": settings.cos_region.strip(),
            "SecretId": settings.cos_secret_id.strip(),
            "SecretKey": settings.cos_secret_key.strip(),
            "Token": settings.cos_session_token.strip() or None,
            "Scheme": parsed.scheme or "https",
            "Proxies": cos_config_proxies(),
        }
        if endpoint_host and not endpoint_host.startswith(f"{settings.cos_bucket.strip()}."):
            kwargs["Endpoint"] = endpoint_host
        ensure_cos_no_proxy(
            endpoint=settings.cos_endpoint,
            bucket=settings.cos_bucket,
            region=settings.cos_region,
        )
        return CosS3Client(CosConfig(**kwargs))

    def _object_storage_url(self, object_key: str) -> str:
        settings = get_settings()
        endpoint = settings.cos_endpoint.strip().rstrip("/")
        if not endpoint:
            endpoint = f"https://{settings.cos_bucket.strip()}.cos.{settings.cos_region.strip()}.myqcloud.com"
        return f"{endpoint}/{object_key.lstrip('/')}"

    def _coerce_datetime(self, raw: Any) -> datetime:
        if isinstance(raw, datetime):
            return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return utc_now()
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        return utc_now()

    def _assistant_storage_scope_candidates(self, assistant_id: str) -> list[str]:
        assistant = self.assistant_svc.get_assistant(assistant_id)
        assistant_name = assistant.name if assistant is not None else ""
        workspace_slug = (
            assistant.workspace_config.workspace_slug
            if assistant is not None and assistant.workspace_config is not None
            else ""
        )
        candidates = [
            workspace_slug,
            build_assistant_storage_scope(
                assistant_id=assistant_id,
                assistant_name="",
            ),
            assistant_id,
            build_assistant_storage_scope(
                assistant_id=assistant_id,
                assistant_name=assistant_name,
            ),
            build_assistant_storage_scope(
                assistant_id=assistant_name,
                assistant_name="",
            )
            if assistant_name.strip()
            else "",
        ]
        scopes: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            normalized = str(candidate).strip().strip("/")
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            scopes.append(normalized)
        return scopes

    def _is_workspace_object_storage_key(self, object_key: str) -> bool:
        settings = get_settings()
        normalized_key = object_key.strip().strip("/")
        if not normalized_key:
            return False
        candidates = [
            settings.workspace_object_storage_prefix.strip().strip("/"),
            "workspace-files",
        ]
        cos_prefix = settings.cos_prefix.strip().strip("/")
        if cos_prefix:
            candidates.append(f"{cos_prefix}/workspace-files")
        for candidate in candidates:
            prefix = candidate.strip().strip("/")
            if not prefix:
                continue
            if normalized_key.startswith(f"{prefix}/") or f"/{prefix}/" in normalized_key:
                return True
        return False

    def _legacy_object_storage_prefixes(self, assistant_id: str) -> list[str]:
        settings = get_settings()
        workspace_prefix = settings.workspace_object_storage_prefix.strip().strip("/")
        cos_prefix = settings.cos_prefix.strip().strip("/")
        scopes = self._assistant_storage_scope_candidates(assistant_id)
        candidates: list[str] = []
        for scope in scopes:
            if workspace_prefix:
                candidates.append(f"{workspace_prefix}/{scope}/")
            candidates.append(f"workspace-files/{scope}/")
        for scope in scopes:
            candidates.append(f"{scope}/")
        prefixes: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            trimmed = candidate.strip().lstrip("/")
            if not trimmed:
                continue
            values = [trimmed]
            if cos_prefix and trimmed != cos_prefix and not trimmed.startswith(f"{cos_prefix}/"):
                values.append(f"{cos_prefix}/{trimmed}")
            for value in values:
                normalized = value.strip().strip("/")
                if not normalized:
                    continue
                prefix = f"{normalized}/"
                if prefix in seen:
                    continue
                seen.add(prefix)
                prefixes.append(prefix)
        return prefixes

    def _legacy_object_storage_suffix_markers(self, assistant_id: str) -> list[str]:
        settings = get_settings()
        workspace_prefix = settings.workspace_object_storage_prefix.strip().strip("/")
        workspace_leaf = (
            workspace_prefix.rsplit("/", 1)[-1] if workspace_prefix else "workspace-files"
        )
        scopes = self._assistant_storage_scope_candidates(assistant_id)
        markers: list[str] = []
        seen: set[str] = set()
        for scope in scopes:
            for marker in (
                f"{workspace_prefix}/{scope}/" if workspace_prefix else "",
                f"{workspace_leaf}/{scope}/" if workspace_leaf else "",
                f"workspace-files/{scope}/",
            ):
                normalized = marker.strip().strip("/")
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                markers.append(f"{normalized}/")
        return markers

    def _capability_folder_prefixes_from_scopes(
        self,
        scopes: Iterable[str],
        bucket: TargetBucket,
    ) -> list[str]:
        settings = get_settings()
        workspace_prefix = settings.workspace_object_storage_prefix.strip().strip("/")
        cos_prefix = settings.cos_prefix.strip().strip("/")
        base_prefix = workspace_prefix or "workspace-files"
        candidates: list[str] = []
        for scope in scopes:
            scope = str(scope).strip().strip("/")
            if not scope:
                continue
            if cos_prefix:
                if base_prefix == cos_prefix or base_prefix.startswith(f"{cos_prefix}/"):
                    candidates.append(f"{base_prefix}/{scope}/{bucket}/")
                else:
                    candidates.append(f"{cos_prefix}/{base_prefix}/{scope}/{bucket}/")
                continue
            if workspace_prefix:
                candidates.append(f"{workspace_prefix}/{scope}/{bucket}/")
            candidates.append(f"workspace-files/{scope}/{bucket}/")
        prefixes: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            normalized = candidate.strip().strip("/")
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            prefixes.append(f"{normalized}/")
        return prefixes

    def _primary_capability_folder_prefixes(
        self,
        assistant_id: str,
        bucket: TargetBucket,
    ) -> list[str]:
        assistant = self.assistant_svc.get_assistant(assistant_id)
        workspace_slug = (
            assistant.workspace_config.workspace_slug
            if assistant is not None and assistant.workspace_config is not None
            else ""
        )
        primary_scopes: list[str] = []
        seen: set[str] = set()
        for candidate in (
            workspace_slug,
            build_assistant_storage_scope(assistant_id=assistant_id, assistant_name=""),
            assistant_id,
        ):
            normalized = str(candidate).strip().strip("/")
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            primary_scopes.append(normalized)
        return self._capability_folder_prefixes_from_scopes(primary_scopes, bucket)

    def _capability_folder_prefixes(self, assistant_id: str, bucket: TargetBucket) -> list[str]:
        return self._capability_folder_prefixes_from_scopes(
            self._assistant_storage_scope_candidates(assistant_id),
            bucket,
        )

    def _cos_capability_cache_key(self, assistant_id: str) -> tuple[str, str, str, str, str]:
        settings = get_settings()
        assistant = self.assistant_svc.get_assistant(assistant_id)
        workspace_slug = (
            assistant.workspace_config.workspace_slug
            if assistant is not None and assistant.workspace_config is not None
            else ""
        )
        return (
            settings.cos_bucket.strip(),
            settings.workspace_object_storage_prefix.strip().strip("/"),
            settings.cos_prefix.strip().strip("/"),
            workspace_slug.strip().strip("/"),
            assistant_id.strip(),
        )

    def _remember_cos_capability_record(
        self,
        records: list[WorkspaceFile],
        seen: set[tuple[str, str]],
        *,
        assistant_id: str,
        bucket: TargetBucket,
        slug: str,
        object_key: str,
        modified_at: datetime | None = None,
    ) -> None:
        if not slug or slug.startswith(".") or (bucket, slug) in seen:
            return
        seen.add((bucket, slug))
        timestamp = modified_at or utc_now()
        records.append(
            WorkspaceFile(
                assistant_id=assistant_id,
                bucket=bucket,
                filename=f"{slug}/",
                relative_path=f"{bucket}/{slug}/",
                direction="inbound",
                file_role="input",
                size_bytes=0,
                content_type=WORKSPACE_STORAGE_MARKER_CONTENT_TYPE,
                source_type="manual_import",
                source_ref="cos_capability_folder",
                storage_provider="cos",
                storage_key=object_key,
                storage_status="synced",
                storage_backend="cos",
                object_key=object_key,
                object_url=self._object_storage_url(object_key),
                sync_status="synced",
                last_synced_at=timestamp,
                created_at=timestamp,
                updated_at=timestamp,
                deleted=False,
            )
        )

    def invalidate_cos_capability_package_cache(self, assistant_id: str | None = None) -> None:
        if not assistant_id:
            self._cos_capability_package_cache.clear()
            return
        normalized_id = assistant_id.strip()
        for key in list(self._cos_capability_package_cache):
            if key[3] == normalized_id or key[4] == normalized_id:
                self._cos_capability_package_cache.pop(key, None)

    def list_cos_capability_package_records(
        self,
        assistant_id: str,
        *,
        force_refresh: bool = False,
    ) -> list[WorkspaceFile]:
        cache_key = self._cos_capability_cache_key(assistant_id)
        if not force_refresh:
            cached = self._cos_capability_package_cache.get(cache_key)
            if cached is not None:
                cached_at, cached_records = cached
                if monotonic() - cached_at <= self._cos_capability_package_cache_ttl_seconds:
                    return list(cached_records)

        client = self._build_cos_client()
        if client is None:
            return []
        settings = get_settings()
        records: list[WorkspaceFile] = []
        seen: set[tuple[str, str]] = set()

        def scan_prefixes(bucket: TargetBucket, prefixes: list[str]) -> int:
            before = len([record for record in records if record.bucket == bucket])
            for prefix in prefixes:
                marker = ""
                while True:
                    kwargs: dict[str, object] = {
                        "Bucket": settings.cos_bucket.strip(),
                        "Prefix": prefix,
                        "MaxKeys": 1000,
                    }
                    if marker:
                        kwargs["Marker"] = marker
                    response = client.list_objects(**kwargs)
                    for item in response.get("CommonPrefixes") or []:
                        folder_key = str(item.get("Prefix", "")).strip().lstrip("/")
                        if not folder_key.startswith(prefix):
                            continue
                        slug = folder_key[len(prefix) :].strip("/").split("/", 1)[0]
                        self._remember_cos_capability_record(
                            records,
                            seen,
                            assistant_id=assistant_id,
                            bucket=bucket,
                            slug=slug,
                            object_key=folder_key,
                        )
                    for entry in response.get("Contents") or []:
                        object_key = str(entry.get("Key", "")).strip().lstrip("/")
                        if not object_key.startswith(prefix):
                            continue
                        relative = object_key[len(prefix) :].strip("/")
                        slug = relative.split("/", 1)[0] if relative else ""
                        if (
                            not slug
                            or slug.startswith(".")
                            or _is_workspace_storage_marker_name(slug)
                        ):
                            continue
                        self._remember_cos_capability_record(
                            records,
                            seen,
                            assistant_id=assistant_id,
                            bucket=bucket,
                            slug=slug,
                            object_key=object_key,
                            modified_at=self._coerce_datetime(entry.get("LastModified")),
                        )
                    if str(response.get("IsTruncated", "")).lower() != "true":
                        break
                    marker = str(response.get("NextMarker", "")).strip()
                    if not marker:
                        contents = response.get("Contents") or []
                        if contents:
                            marker = str(contents[-1].get("Key", "")).strip()
                    if not marker:
                        break
            after = len([record for record in records if record.bucket == bucket])
            return after - before

        for bucket in ("skills", "plugins"):
            found = scan_prefixes(
                bucket,
                self._primary_capability_folder_prefixes(assistant_id, bucket),
            )
            if found == 0:
                fallback_prefixes = [
                    prefix
                    for prefix in self._capability_folder_prefixes(assistant_id, bucket)
                    if prefix not in self._primary_capability_folder_prefixes(assistant_id, bucket)
                ]
                scan_prefixes(bucket, fallback_prefixes)
        if records:
            self._cos_capability_package_cache[cache_key] = (monotonic(), list(records))
        else:
            self._cos_capability_package_cache.pop(cache_key, None)
        return records

    def _delete_cos_capability_objects(
        self,
        *,
        assistant_id: str,
        bucket: TargetBucket,
        slug: str,
    ) -> int:
        client = self._build_cos_client()
        if client is None:
            return 0
        settings = get_settings()
        deleted = 0
        seen_keys: set[str] = set()
        delete_batch: list[str] = []

        def flush_delete_batch() -> None:
            nonlocal deleted
            if not delete_batch:
                return
            objects = [{"Key": key} for key in delete_batch]
            try:
                if hasattr(client, "delete_objects"):
                    client.delete_objects(
                        Bucket=settings.cos_bucket.strip(),
                        Delete={"Object": objects, "Quiet": "true"},
                    )
                else:
                    for object_key in delete_batch:
                        client.delete_object(Bucket=settings.cos_bucket.strip(), Key=object_key)
                deleted += len(delete_batch)
            except Exception as exc:
                log.warning(
                    "capability_object_batch_delete_failed",
                    assistant_id=assistant_id,
                    bucket=bucket,
                    slug=slug,
                    count=len(delete_batch),
                    error=f"{type(exc).__name__}: {exc}",
                )
                for object_key in delete_batch:
                    try:
                        client.delete_object(Bucket=settings.cos_bucket.strip(), Key=object_key)
                        deleted += 1
                    except Exception as item_exc:
                        log.warning(
                            "capability_object_delete_failed",
                            assistant_id=assistant_id,
                            bucket=bucket,
                            slug=slug,
                            object_key=object_key,
                            error=f"{type(item_exc).__name__}: {item_exc}",
                        )
            finally:
                delete_batch.clear()

        for prefix in self._capability_folder_prefixes(assistant_id, bucket):
            package_prefix = f"{prefix}{slug.strip('/')}/"
            marker = ""
            while True:
                kwargs: dict[str, object] = {
                    "Bucket": settings.cos_bucket.strip(),
                    "Prefix": package_prefix,
                    "MaxKeys": 1000,
                }
                if marker:
                    kwargs["Marker"] = marker
                try:
                    response = client.list_objects(**kwargs)
                except Exception as exc:
                    log.warning(
                        "capability_object_list_failed",
                        assistant_id=assistant_id,
                        bucket=bucket,
                        slug=slug,
                        prefix=package_prefix,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    break
                for entry in response.get("Contents") or []:
                    object_key = str(entry.get("Key", "")).strip().lstrip("/")
                    if not object_key or object_key in seen_keys:
                        continue
                    seen_keys.add(object_key)
                    delete_batch.append(object_key)
                    if len(delete_batch) >= 1000:
                        flush_delete_batch()
                if str(response.get("IsTruncated", "")).lower() != "true":
                    break
                marker = str(response.get("NextMarker", "")).strip()
                if not marker:
                    break
            flush_delete_batch()
        return deleted

    def _discover_legacy_object_storage_records(self, assistant_id: str) -> list[WorkspaceFile]:
        client = self._build_cos_client()
        if client is None:
            return []
        settings = get_settings()
        allowed_buckets = {"temp", "persistent", "skills", "plugins"}
        records: list[WorkspaceFile] = []
        seen_keys: set[str] = set()
        seen_directories: set[str] = set()

        def add_directory(*, bucket: str, directory_name: str, object_key: str) -> None:
            normalized_directory = directory_name.strip("/")
            if bucket not in allowed_buckets:
                return
            directory_key = (
                f"{bucket}/{normalized_directory}/" if normalized_directory else f"{bucket}/"
            )
            if directory_key in seen_directories:
                return
            seen_directories.add(directory_key)
            now = utc_now()
            records.append(
                WorkspaceFile(
                    file_id=f"cosdir:{assistant_id}:{directory_key}",
                    assistant_id=assistant_id,
                    bucket=bucket,  # type: ignore[arg-type]
                    filename=f"{normalized_directory}/" if normalized_directory else "",
                    relative_path=directory_key,
                    direction="inbound",
                    file_role="input",
                    size_bytes=0,
                    content_type=WORKSPACE_STORAGE_MARKER_CONTENT_TYPE,
                    source_type="cos_directory",
                    source_ref="cos_prefix",
                    storage_provider="cos",
                    storage_key=object_key,
                    storage_path="",
                    storage_status="synced",
                    storage_backend="cos",
                    object_key=object_key,
                    object_url=self._object_storage_url(object_key),
                    sync_status="synced",
                    last_synced_at=now,
                    created_at=now,
                    updated_at=now,
                    deleted=False,
                )
            )

        def add_directory_ancestors(*, bucket: str, filename: str, object_key: str) -> None:
            normalized = filename.strip("/")
            if not normalized:
                return
            parts = [part for part in normalized.split("/") if part]
            if not parts:
                return
            if filename.endswith("/"):
                max_depth = len(parts)
            else:
                max_depth = max(len(parts) - 1, 0)
            for depth in range(1, max_depth + 1):
                add_directory(
                    bucket=bucket,
                    directory_name="/".join(parts[:depth]),
                    object_key=object_key,
                )

        def add_entry(entry: Any, *, relative: str, object_key: str) -> None:
            if not object_key or object_key in seen_keys:
                return
            normalized_relative = relative.lstrip("/")
            if not normalized_relative:
                return
            if "/" not in normalized_relative:
                bucket = normalized_relative.strip("/")
                filename = ""
            else:
                bucket, filename = normalized_relative.split("/", 1)
            if bucket not in allowed_buckets:
                return
            if not filename:
                add_directory(bucket=bucket, directory_name="", object_key=object_key)
                seen_keys.add(object_key)
                return
            add_directory(bucket=bucket, directory_name="", object_key=object_key)
            add_directory_ancestors(bucket=bucket, filename=filename, object_key=object_key)
            if _is_workspace_storage_marker_name(filename):
                seen_keys.add(object_key)
                return
            if filename.endswith("/"):
                seen_keys.add(object_key)
                return
            seen_keys.add(object_key)
            modified_at = self._coerce_datetime(entry.get("LastModified"))
            records.append(
                WorkspaceFile(
                    assistant_id=assistant_id,
                    bucket=bucket,
                    filename=filename,
                    relative_path=f"{bucket}/{filename}",
                    direction="inbound",
                    file_role="input",
                    size_bytes=int(entry.get("Size", 0) or 0),
                    content_type=_guess_content_type(filename),
                    source_type="manual_import",
                    source_ref="legacy_object_storage",
                    storage_provider="cos",
                    storage_key=object_key,
                    storage_path="",
                    storage_status="synced",
                    storage_backend="cos",
                    object_key=object_key,
                    object_url=self._object_storage_url(object_key),
                    sync_status="synced",
                    last_synced_at=modified_at,
                    created_at=modified_at,
                    updated_at=modified_at,
                    deleted=False,
                )
            )

        def scan_prefix(prefix: str, *, suffix_markers: list[str] | None = None) -> None:
            marker = ""
            while True:
                kwargs: dict[str, object] = {
                    "Bucket": settings.cos_bucket.strip(),
                    "Prefix": prefix,
                    "MaxKeys": 1000,
                }
                if marker:
                    kwargs["Marker"] = marker
                response = client.list_objects(**kwargs)
                for item in response.get("CommonPrefixes") or []:
                    folder_key = str(item.get("Prefix", "")).strip().lstrip("/")
                    if not folder_key:
                        continue
                    if suffix_markers:
                        for suffix_marker in suffix_markers:
                            marker_index = folder_key.find(suffix_marker)
                            if marker_index < 0:
                                continue
                            add_entry(
                                item,
                                relative=folder_key[marker_index + len(suffix_marker) :],
                                object_key=folder_key,
                            )
                            break
                        continue
                    relative = (
                        folder_key[len(prefix) :].lstrip("/")
                        if folder_key.startswith(prefix)
                        else ""
                    )
                    add_entry(item, relative=relative, object_key=folder_key)
                contents = response.get("Contents") or []
                for entry in contents:
                    object_key = str(entry.get("Key", "")).strip().lstrip("/")
                    if not object_key or object_key in seen_keys:
                        continue
                    if suffix_markers:
                        for suffix_marker in suffix_markers:
                            marker_index = object_key.find(suffix_marker)
                            if marker_index < 0:
                                continue
                            add_entry(
                                entry,
                                relative=object_key[marker_index + len(suffix_marker) :],
                                object_key=object_key,
                            )
                            break
                        continue
                    relative = (
                        object_key[len(prefix) :].lstrip("/")
                        if object_key.startswith(prefix)
                        else ""
                    )
                    add_entry(entry, relative=relative, object_key=object_key)
                if str(response.get("IsTruncated", "")).lower() != "true":
                    break
                marker = str(response.get("NextMarker", "")).strip()
                if not marker and contents:
                    marker = str(contents[-1].get("Key", "")).strip()
                if not marker:
                    break

        for prefix in self._legacy_object_storage_prefixes(assistant_id):
            scan_prefix(prefix)
        return records

    def list_cos_workspace_records(self, assistant_id: str) -> list[WorkspaceFile]:
        """List assistant workspace records directly from COS without using local metadata."""
        return self._discover_legacy_object_storage_records(assistant_id)

    def list_cos_workspace_summary_records(self, assistant_id: str) -> list[WorkspaceFile]:
        """List only the first two COS workspace directory levels for fast UI summaries."""
        client = self._build_cos_client()
        if client is None:
            return []
        settings = get_settings()
        allowed_buckets = {"temp", "persistent", "skills", "plugins"}
        records: list[WorkspaceFile] = []
        seen: set[str] = set()

        def add_directory(bucket: str, name: str, object_key: str) -> None:
            if bucket not in allowed_buckets:
                return
            normalized_name = name.strip("/")
            relative_path = f"{bucket}/{normalized_name}/" if normalized_name else f"{bucket}/"
            if relative_path in seen:
                return
            seen.add(relative_path)
            now = utc_now()
            records.append(
                WorkspaceFile(
                    file_id=f"cosdir:{assistant_id}:{relative_path}",
                    assistant_id=assistant_id,
                    bucket=bucket,  # type: ignore[arg-type]
                    filename=f"{normalized_name}/" if normalized_name else "",
                    relative_path=relative_path,
                    size_bytes=0,
                    content_type=WORKSPACE_STORAGE_MARKER_CONTENT_TYPE,
                    source_type="cos_directory",
                    source_ref="cos_summary_prefix",
                    storage_provider="cos",
                    storage_key=object_key,
                    storage_backend="cos",
                    object_key=object_key,
                    object_url=self._object_storage_url(object_key),
                    storage_status="synced",
                    sync_status="synced",
                    created_at=now,
                    updated_at=now,
                    last_synced_at=now,
                )
            )

        def add_file(bucket: str, filename: str, entry: Any, object_key: str) -> None:
            if (
                bucket not in allowed_buckets
                or not filename
                or _is_workspace_storage_marker_name(filename)
            ):
                return
            relative_path = f"{bucket}/{filename}"
            if relative_path in seen:
                return
            seen.add(relative_path)
            modified_at = self._coerce_datetime(entry.get("LastModified"))
            records.append(
                WorkspaceFile(
                    assistant_id=assistant_id,
                    bucket=bucket,  # type: ignore[arg-type]
                    filename=filename,
                    relative_path=relative_path,
                    size_bytes=int(entry.get("Size", 0) or 0),
                    content_type=_guess_content_type(filename),
                    source_type="manual_import",
                    source_ref="cos_summary",
                    storage_provider="cos",
                    storage_key=object_key,
                    storage_backend="cos",
                    object_key=object_key,
                    object_url=self._object_storage_url(object_key),
                    storage_status="synced",
                    sync_status="synced",
                    created_at=modified_at,
                    updated_at=modified_at,
                    last_synced_at=modified_at,
                )
            )

        def list_once(prefix: str) -> dict[str, Any]:
            return client.list_objects(
                Bucket=settings.cos_bucket.strip(),
                Prefix=prefix,
                Delimiter="/",
                MaxKeys=1000,
            )

        for root_prefix in self._legacy_object_storage_prefixes(assistant_id):
            records_before_root = len(records)
            try:
                root_response = list_once(root_prefix)
            except Exception as exc:
                log.warning(
                    "cos_workspace_summary_root_list_failed",
                    assistant_id=assistant_id,
                    prefix=root_prefix,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue

            bucket_prefixes: dict[str, str] = {}
            for item in root_response.get("CommonPrefixes") or []:
                folder_key = str(item.get("Prefix", "")).strip().lstrip("/")
                relative = (
                    folder_key[len(root_prefix) :].strip("/")
                    if folder_key.startswith(root_prefix)
                    else ""
                )
                bucket = relative.split("/", 1)[0] if relative else ""
                if bucket in allowed_buckets:
                    add_directory(bucket, "", folder_key)
                    bucket_prefixes[bucket] = folder_key
            for entry in root_response.get("Contents") or []:
                object_key = str(entry.get("Key", "")).strip().lstrip("/")
                relative = (
                    object_key[len(root_prefix) :].strip("/")
                    if object_key.startswith(root_prefix)
                    else ""
                )
                bucket = relative.split("/", 1)[0] if relative else ""
                if bucket in allowed_buckets and "/" not in relative:
                    add_directory(bucket, "", object_key)
                    bucket_prefixes.setdefault(bucket, f"{root_prefix}{bucket}/")

            for bucket, bucket_prefix in bucket_prefixes.items():
                try:
                    bucket_response = list_once(bucket_prefix)
                except Exception as exc:
                    log.warning(
                        "cos_workspace_summary_bucket_list_failed",
                        assistant_id=assistant_id,
                        bucket=bucket,
                        prefix=bucket_prefix,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    continue
                for item in bucket_response.get("CommonPrefixes") or []:
                    folder_key = str(item.get("Prefix", "")).strip().lstrip("/")
                    child = folder_key[len(bucket_prefix) :].strip("/").split("/", 1)[0]
                    if child:
                        add_directory(bucket, child, folder_key)
                for entry in bucket_response.get("Contents") or []:
                    object_key = str(entry.get("Key", "")).strip().lstrip("/")
                    filename = (
                        object_key[len(bucket_prefix) :].strip("/")
                        if object_key.startswith(bucket_prefix)
                        else ""
                    )
                    if filename and "/" not in filename:
                        add_file(bucket, filename, entry, object_key)
            if len(records) > records_before_root:
                break
        return records

    def _hydrate_legacy_object_storage_records(self, assistant_id: str) -> None:
        discovered = self._discover_legacy_object_storage_records(assistant_id)
        if not discovered:
            return
        existing = {
            (item.bucket, item.filename): item
            for item in run_sync(
                self.file_repository.alist_files(
                    assistant_id=assistant_id,
                    include_deleted=True,
                )
            )
        }
        for imported in discovered:
            key = (imported.bucket, imported.filename)
            current = existing.get(key)
            if current is None:
                run_sync(self.file_repository.asave_file(imported))
                existing[key] = imported
                continue
            updated = False
            if current.deleted:
                continue
            imported_key = imported.storage_key or imported.object_key
            current_key = current.storage_key or current.object_key
            prefer_imported_key = (
                imported.bucket in {"skills", "plugins"}
                and imported_key != current_key
                and self._is_workspace_object_storage_key(imported_key)
                and not self._is_workspace_object_storage_key(current_key)
            )
            if not current.storage_key or prefer_imported_key:
                current.storage_key = imported.storage_key
                current.object_key = imported.object_key
                current.object_url = imported.object_url
                current.storage_backend = "cos"
                current.storage_provider = "cos"
                current.storage_status = "synced"
                current.sync_status = "synced"
                updated = True
            if updated:
                current.updated_at = utc_now()
                current.last_synced_at = imported.last_synced_at or utc_now()
                run_sync(self.file_repository.asave_file(current))

    def _put_object_bytes(self, *, key: str, content: bytes, content_type: str):
        try:
            return self.object_storage.put_bytes(
                key=key,
                content=content,
                content_type=content_type,
            )
        except TypeError:
            return self.object_storage.put_bytes(key=key, content=content)

    def _get_object_bytes(self, *, key: str) -> bytes | None:
        try:
            return self.object_storage.get_bytes(key=key)
        except TypeError:
            return self.object_storage.get_bytes(key)

    def _delete_object_bytes(self, *, key: str) -> None:
        try:
            self.object_storage.delete(key=key)
        except TypeError:
            self.object_storage.delete(key)

    def ensure_workspace_storage_prefixes(self, assistant_id: str) -> list[str]:
        """Create object-storage markers for the assistant workspace buckets."""
        resolved_id = assistant_id.strip()
        self._assistant_exists(resolved_id)
        created_keys: list[str] = []
        for bucket in WORKSPACE_STORAGE_MARKER_BUCKETS:
            object_key = self._object_key(
                assistant_id=resolved_id,
                bucket=bucket,
                filename=WORKSPACE_STORAGE_MARKER_FILENAME,
            )
            try:
                stored = self._put_object_bytes(
                    key=object_key,
                    content=b"",
                    content_type=WORKSPACE_STORAGE_MARKER_CONTENT_TYPE,
                )
            except Exception as exc:
                log.warning(
                    "workspace_storage_prefix_create_failed",
                    assistant_id=resolved_id,
                    bucket=bucket,
                    object_key=object_key,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            created_keys.append(getattr(stored, "key", object_key))
        return created_keys

    def _sync_blob(
        self,
        *,
        record: WorkspaceFile,
        content: bytes,
    ) -> WorkspaceFile:
        if not self.object_storage_gateway.is_enabled():
            record.storage_provider = getattr(self.object_storage_gateway, "provider", "cos")
            record.storage_key = ""
            record.storage_path = ""
            record.storage_status = "disabled"
            record.storage_error = "object storage is not configured"
            record.storage_backend = getattr(self.object_storage, "backend", "cos")
            record.object_key = ""
            record.object_url = ""
            record.checksum_sha256 = self._checksum_sha256(content)
            record.sync_status = "synced_to_workspace"
            record.sync_error = ""
            record.updated_at = utc_now()
            return record
        stored = self._put_object_bytes(
            key=self._object_key(
                assistant_id=record.assistant_id,
                bucket=record.bucket,
                filename=record.filename,
            ),
            content=content,
            content_type=record.content_type,
        )
        now = utc_now()
        sync_status = "synced"
        if self.file_repository.__class__.__module__ == "src.shared.repositories":
            sync_status = "synced_to_workspace"
        storage_key = stored.key
        storage_url = stored.url
        prefix = (
            str(
                getattr(
                    self.object_storage,
                    "prefix",
                    get_settings().workspace_object_storage_prefix,
                )
            )
            .strip()
            .strip("/")
        )
        if prefix and storage_key and not storage_key.startswith(f"{prefix}/"):
            storage_key = f"{prefix}/{storage_key.lstrip('/')}"
            if stored.url.endswith(stored.key):
                storage_url = f"{stored.url[: -len(stored.key)]}{storage_key}"
        record.storage_provider = stored.backend
        record.storage_key = storage_key
        record.storage_path = storage_url
        record.storage_status = "synced"
        record.storage_error = ""
        record.storage_backend = stored.backend
        record.object_key = storage_key
        record.object_url = storage_url
        record.checksum_sha256 = self._checksum_sha256(content)
        record.sync_status = sync_status
        record.sync_error = ""
        record.last_synced_at = now
        record.updated_at = now
        return record

    def _mark_sync_failed(self, *, record: WorkspaceFile, error: str) -> WorkspaceFile:
        record.storage_status = "failed"
        record.storage_error = error
        record.sync_status = "failed"
        record.sync_error = error
        record.updated_at = utc_now()
        return record

    def _write_workspace_copy(
        self,
        *,
        assistant_id: str,
        bucket: TargetBucket,
        filename: str,
        content: bytes,
    ) -> Path:
        destination = self._bucket_dir(assistant_id, bucket) / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return destination

    def _capability_profile_for_assistant(self, assistant_id: str) -> str | None:
        assistant = self.assistant_svc.get_assistant(assistant_id)
        if assistant is None:
            return None
        return str(assistant.capability_profile or "").strip() or None

    def upload_file(
        self,
        *,
        assistant_id: str | None = None,
        user_id: str | None = None,
        bucket: TargetBucket = "temp",
        filename: str,
        content: bytes,
        content_type: str = "",
        overwrite: bool = False,
        target_path: str = "",
        source_type: str = "",
        source_ref: str = "",
        owner_user_id: str = "",
        uploader_user_id: str = "",
    ) -> WorkspaceFile | ExternalPushRequest:
        resolved_id = self.resolve_assistant_id(assistant_id=assistant_id, user_id=user_id)
        self._ensure_upload_bucket(bucket)
        filename = self.compose_upload_filename(filename=filename, target_path=target_path)
        self._assistant_exists(resolved_id)

        existing = run_sync(
            self.file_repository.aget_active_file_by_path(
                assistant_id=resolved_id,
                bucket=bucket,
                filename=filename,
            )
        )
        destination = self._write_workspace_copy(
            assistant_id=resolved_id,
            bucket=bucket,
            filename=filename,
            content=content,
        )
        now = utc_now()
        record = existing or WorkspaceFile(
            assistant_id=resolved_id,
            bucket=bucket,
            filename=filename,
            relative_path=f"{bucket}/{filename}",
            created_at=now,
            updated_at=now,
            file_role="input",
        )
        effective_owner_user_id = (owner_user_id or (user_id or "")).strip()
        effective_uploader_user_id = (uploader_user_id or (user_id or "")).strip()
        record.size_bytes = len(content)
        record.content_type = content_type
        record.owner_user_id = effective_owner_user_id or record.owner_user_id
        record.uploader_user_id = effective_uploader_user_id
        record.source_type = source_type
        record.source_ref = source_ref
        record.direction = "inbound"
        record.file_role = "input"
        record.deleted = False
        record.workspace_last_modified_at = self._workspace_mtime(destination)
        try:
            record = self._sync_blob(record=record, content=content)
        except Exception as exc:
            record = self._mark_sync_failed(record=record, error=f"{type(exc).__name__}: {exc}")
            log.warning(
                "file_object_sync_failed",
                assistant_id=resolved_id,
                filename=filename,
                error=record.sync_error,
            )
        run_sync(self.file_repository.asave_file(record))

        action = "overwrite" if existing is not None else "upload"
        run_sync(
            self.event_repository.aappend_event(
                WorkspaceFileEvent(
                    file_id=record.file_id,
                    assistant_id=resolved_id,
                    action=action,
                    user_id=effective_uploader_user_id,
                    source_type=source_type,
                    source_ref=source_ref,
                    detail={
                        "filename": filename,
                        "bucket": bucket,
                        "size": len(content),
                        "storage_backend": record.storage_backend,
                        "sync_status": record.sync_status,
                    },
                )
            )
        )

        run_sync(
            self.push_repository.asave_push_request(
                ExternalPushRequest(
                    user_id=effective_uploader_user_id,
                    assistant_id=resolved_id,
                    filename=filename,
                    bucket=bucket,
                    content_type=content_type,
                    source_type=source_type,
                    source_ref=source_ref,
                    status="accepted",
                )
            )
        )

        log.info(
            "file_uploaded",
            assistant_id=resolved_id,
            filename=filename,
            bucket=bucket,
            size=len(content),
            sync_status=record.sync_status,
        )
        self._record_workspace_refresh_event(
            record,
            user_id=effective_uploader_user_id,
            reason="file_upload",
        )
        return record

    def list_files(
        self,
        assistant_id: str,
        *,
        bucket: TargetBucket | None = None,
        direction: str | None = None,
        file_role: str | None = None,
        include_deleted: bool = False,
    ) -> list[WorkspaceFile]:
        if self.legacy_object_storage_import_enabled and bucket in {
            None,
            "temp",
            "persistent",
            "skills",
            "plugins",
        }:
            try:
                self._hydrate_legacy_object_storage_records(assistant_id)
            except Exception as exc:
                log.warning(
                    "legacy_object_storage_import_failed",
                    assistant_id=assistant_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
        items = run_sync(
            self.file_repository.alist_files(
                assistant_id=assistant_id,
                bucket=bucket,
                include_deleted=include_deleted,
            )
        )
        if direction is not None:
            items = [item for item in items if item.direction == direction]
        if file_role is not None:
            items = [item for item in items if item.file_role == file_role]
        if not include_deleted:
            items = _deduplicate_active_workspace_files(items)
        return items

    def delete_capability_package(
        self,
        *,
        assistant_id: str,
        bucket: TargetBucket,
        slug: str,
        user_id: str = "admin-web",
    ) -> int:
        if bucket not in {"skills", "plugins"}:
            raise ValueError("capability package bucket must be skills or plugins")
        package_slug = slug.strip().strip("/\\")
        if not package_slug:
            raise ValueError("capability package slug is required")

        package_dir = self._bucket_dir(assistant_id, bucket) / package_slug
        if package_dir.exists():
            if package_dir.is_dir():
                rmtree(package_dir)
            else:
                package_dir.unlink()

        cos_deleted_count = self._delete_cos_capability_objects(
            assistant_id=assistant_id,
            bucket=bucket,
            slug=package_slug,
        )
        self.invalidate_cos_capability_package_cache(assistant_id)

        try:
            records = run_sync(
                self.file_repository.alist_files(
                    assistant_id=assistant_id,
                    bucket=bucket,
                    include_deleted=True,
                )
            )
        except Exception as exc:
            log.warning(
                "capability_package_metadata_read_failed",
                assistant_id=assistant_id,
                bucket=bucket,
                slug=package_slug,
                cos_deleted_count=cos_deleted_count,
                error=f"{type(exc).__name__}: {exc}",
            )
            return cos_deleted_count
        deleted_count = 0
        for record in records:
            if record.deleted:
                continue
            normalized = record.filename.replace("\\", "/").lstrip("/")
            record_slug = normalized.split("/", 1)[0] if normalized else ""
            if record_slug != package_slug:
                continue
            try:
                self._delete_record(
                    record,
                    user_id=user_id,
                    reason="capability_package_uninstall",
                )
                deleted_count += 1
            except Exception as exc:
                log.warning(
                    "capability_package_metadata_delete_failed",
                    assistant_id=assistant_id,
                    bucket=bucket,
                    slug=package_slug,
                    file_id=record.file_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
        return deleted_count + cos_deleted_count

    def list_files_for_user(
        self,
        user_id: str,
        *,
        bucket: TargetBucket | None = None,
        direction: str | None = None,
        file_role: str | None = None,
        include_deleted: bool = False,
    ) -> list[WorkspaceFile]:
        items = run_sync(
            self.file_repository.alist_files(
                assistant_id=None,
                bucket=bucket,
                include_deleted=include_deleted,
            )
        )
        items = [item for item in items if item.owner_user_id == user_id]
        if direction is not None:
            items = [item for item in items if item.direction == direction]
        if file_role is not None:
            items = [item for item in items if item.file_role == file_role]
        if not include_deleted:
            items = _deduplicate_active_workspace_files(items)
        return sorted(items, key=lambda item: item.updated_at, reverse=True)

    def get_file(self, file_id: str) -> WorkspaceFile | None:
        return run_sync(self.file_repository.aget_file(file_id))

    def _download_from_object_storage(self, record: WorkspaceFile) -> bytes | None:
        object_key = record.object_key or record.storage_key
        if object_key:
            gateway = getattr(self, "object_storage_gateway", None)
            if gateway is not None and hasattr(gateway, "download_bytes"):
                try:
                    success, result, _content_type = gateway.download_bytes(key=object_key)
                    if success and isinstance(result, bytes):
                        return result
                except Exception as exc:
                    log.warning(
                        "file_object_download_failed",
                        file_id=record.file_id,
                        object_key=object_key,
                        error=f"{type(exc).__name__}: {exc}",
                    )
            try:
                content = self._get_object_bytes(key=object_key)
                if content is not None:
                    return content
            except Exception as exc:
                log.warning(
                    "file_object_download_failed",
                    file_id=record.file_id,
                    object_key=object_key,
                    error=f"{type(exc).__name__}: {exc}",
                )
        seen_urls: set[str] = set()
        for object_url in (record.object_url, record.storage_path):
            trimmed = object_url.strip()
            if not trimmed or trimmed in seen_urls:
                continue
            seen_urls.add(trimmed)
            try:
                content = download_cos_url_bytes(trimmed)
                if content is not None:
                    return content
            except Exception as exc:
                log.warning(
                    "file_object_url_download_failed",
                    file_id=record.file_id,
                    object_url=trimmed,
                    error=f"{type(exc).__name__}: {exc}",
                )
        return None

    def sync_file(self, file_id: str) -> WorkspaceFile | None:
        record = self.get_file(file_id)
        if record is None or record.deleted:
            return None
        workspace_path = self._bucket_dir(record.assistant_id, record.bucket) / record.filename
        if not workspace_path.exists():
            return record
        content = workspace_path.read_bytes()
        record.size_bytes = len(content)
        record.workspace_last_modified_at = self._workspace_mtime(workspace_path)
        try:
            record = self._sync_blob(record=record, content=content)
        except Exception as exc:
            record = self._mark_sync_failed(record=record, error=f"{type(exc).__name__}: {exc}")
        run_sync(self.file_repository.asave_file(record))
        return record

    def download_file(self, file_id: str) -> WorkspaceFileDownloadResult | None:
        record = self.get_file(file_id)
        if record is None or record.deleted:
            return None
        workspace_path = self._bucket_dir(record.assistant_id, record.bucket) / record.filename
        download_source = "workspace"
        if not workspace_path.is_file():
            return None
        content = workspace_path.read_bytes()

        run_sync(
            self.event_repository.aappend_event(
                WorkspaceFileEvent(
                    file_id=record.file_id,
                    assistant_id=record.assistant_id,
                    action="download",
                    detail={
                        "filename": record.filename,
                        "bucket": record.bucket,
                        "source": "workspace",
                        "download_source": download_source,
                    },
                )
            )
        )
        return WorkspaceFileDownloadResult(
            record=record,
            content=content,
            download_source=download_source,
        )

    def _delete_record(
        self,
        record: WorkspaceFile,
        *,
        user_id: str = "",
        approval_id: str | None = None,
        reason: str = "",
    ) -> WorkspaceFile:
        record.deleted = True
        record.updated_at = utc_now()
        run_sync(self.file_repository.asave_file(record))

        workspace_path = self._bucket_dir(record.assistant_id, record.bucket) / record.filename
        if workspace_path.exists():
            workspace_path.unlink()
        object_key = record.object_key or record.storage_key
        if object_key:
            try:
                self._delete_object_bytes(key=object_key)
            except Exception as exc:
                log.warning(
                    "file_object_delete_failed",
                    file_id=record.file_id,
                    object_key=object_key,
                    error=f"{type(exc).__name__}: {exc}",
                )

        detail: dict[str, str] = {"filename": record.filename, "bucket": record.bucket}
        if approval_id:
            detail["approval_id"] = approval_id
        if reason:
            detail["reason"] = reason
        run_sync(
            self.event_repository.aappend_event(
                WorkspaceFileEvent(
                    file_id=record.file_id,
                    assistant_id=record.assistant_id,
                    action="delete",
                    user_id=user_id,
                    detail=detail,
                )
            )
        )
        return record

    def _reject_push_request(self, push_request: ExternalPushRequest, *, error: str = "") -> None:
        push_request.status = "rejected"
        if error:
            push_request.error = error
        self._cleanup_staged_payload(push_request.staged_path)
        push_request.staged_path = None
        run_sync(self.push_repository.asave_push_request(push_request))

    def _apply_delete_approval_resolution(self, approval_id: str) -> WorkspaceFile | None:
        push_request = run_sync(self.push_repository.aget_push_request_by_approval(approval_id))
        approval = self.approval_svc.get_request(approval_id)
        if approval is None:
            return None
        if approval.resolution == "deny":
            if push_request is not None and push_request.status == "pending_approval":
                self._reject_push_request(push_request)
            return None
        if approval.resolution != "allow":
            return None
        if push_request is None:
            target = (approval.target or "").strip().replace("\\", "/")
            bucket = "persistent"
            filename = target
            if "/" in target:
                maybe_bucket, maybe_filename = target.split("/", 1)
                if maybe_bucket in {"persistent", "temp", "skills", "plugins"} and maybe_filename:
                    bucket = maybe_bucket
                    filename = maybe_filename
            if not approval.assistant_id or not filename:
                return None
            record = run_sync(
                self.file_repository.aget_active_file_by_path(
                    assistant_id=approval.assistant_id,
                    bucket=bucket,
                    filename=filename,
                )
            )
            if record is None:
                return None
            return self._delete_record(
                record,
                approval_id=approval_id,
                reason="approval_resolution_fallback",
            )
        if push_request.status != "pending_approval":
            files = self.list_files(push_request.assistant_id, include_deleted=True)
            return next(
                (
                    item
                    for item in files
                    if item.filename == push_request.filename and item.bucket == push_request.bucket
                ),
                None,
            )
        record = run_sync(
            self.file_repository.aget_active_file_by_path(
                assistant_id=push_request.assistant_id,
                bucket=push_request.bucket,
                filename=push_request.filename,
            )
        )
        if record is None:
            push_request.status = "accepted"
            run_sync(self.push_repository.asave_push_request(push_request))
            return None
        deleted = self._delete_record(
            record,
            user_id=push_request.user_id,
            approval_id=approval_id,
            reason="approval_resolution",
        )
        push_request.status = "accepted"
        run_sync(self.push_repository.asave_push_request(push_request))
        return deleted

    def _apply_overwrite_approval_resolution(self, approval_id: str) -> WorkspaceFile | None:
        push_request = run_sync(self.push_repository.aget_push_request_by_approval(approval_id))
        if push_request is None:
            return None
        approval = self.approval_svc.get_request(approval_id)
        if approval is None:
            return None
        if approval.resolution == "deny":
            if push_request.status == "pending_approval":
                self._reject_push_request(push_request)
            return None
        if approval.resolution != "allow":
            return None
        if push_request.status != "pending_approval":
            return run_sync(
                self.file_repository.aget_active_file_by_path(
                    assistant_id=push_request.assistant_id,
                    bucket=push_request.bucket,
                    filename=push_request.filename,
                )
            )
        staged_path = Path(push_request.staged_path) if push_request.staged_path else None
        if staged_path is None or not staged_path.exists():
            self._reject_push_request(push_request, error="staged overwrite payload is missing")
            return None
        record = run_sync(
            self.file_repository.aget_active_file_by_path(
                assistant_id=push_request.assistant_id,
                bucket=push_request.bucket,
                filename=push_request.filename,
            )
        )
        if record is None:
            self._reject_push_request(push_request, error="target file is missing at approval time")
            return None
        content = staged_path.read_bytes()
        destination = self._write_workspace_copy(
            assistant_id=record.assistant_id,
            bucket=record.bucket,
            filename=record.filename,
            content=content,
        )
        record.size_bytes = len(content)
        record.content_type = push_request.content_type
        record.owner_user_id = record.owner_user_id or push_request.user_id
        record.uploader_user_id = push_request.user_id
        record.source_type = push_request.source_type
        record.source_ref = push_request.source_ref
        record.deleted = False
        record.workspace_last_modified_at = self._workspace_mtime(destination)
        try:
            record = self._sync_blob(record=record, content=content)
        except Exception as exc:
            record = self._mark_sync_failed(record=record, error=f"{type(exc).__name__}: {exc}")
        run_sync(self.file_repository.asave_file(record))
        run_sync(
            self.event_repository.aappend_event(
                WorkspaceFileEvent(
                    file_id=record.file_id,
                    assistant_id=record.assistant_id,
                    action="overwrite",
                    user_id=push_request.user_id,
                    source_type=push_request.source_type,
                    source_ref=push_request.source_ref,
                    detail={
                        "filename": record.filename,
                        "bucket": record.bucket,
                        "size": len(content),
                        "approval_id": approval_id,
                        "sync_status": record.sync_status,
                    },
                )
            )
        )
        self._cleanup_staged_payload(push_request.staged_path)
        push_request.staged_path = None
        push_request.status = "accepted"
        push_request.error = ""
        run_sync(self.push_repository.asave_push_request(push_request))
        self._record_workspace_refresh_event(
            record,
            user_id=push_request.user_id,
            reason="file_overwrite_approval",
        )
        return record

    def apply_approval_resolution(self, approval_id: str) -> WorkspaceFile | None:
        approval = self.approval_svc.get_request(approval_id)
        if approval is None:
            return None
        if approval.tool_name == "workspace_file_delete":
            return self._apply_delete_approval_resolution(approval_id)
        if approval.tool_name == "workspace_file_overwrite":
            return self._apply_overwrite_approval_resolution(approval_id)
        return None

    def apply_resolved_pending_push_requests(self, *, limit: int = 100) -> int:
        applied = 0
        for push_request in self.list_push_requests(limit=limit):
            if push_request.status != "pending_approval" or not push_request.approval_id:
                continue
            approval = self.approval_svc.get_request(push_request.approval_id)
            if approval is None or approval.resolution not in {"allow", "deny"}:
                continue
            self.apply_approval_resolution(push_request.approval_id)
            applied += 1
        return applied

    def delete_file(
        self,
        file_id: str,
        *,
        user_id: str = "",
    ) -> WorkspaceFile | ExternalPushRequest | None:
        record = self.get_file(file_id)
        if record is None or record.deleted:
            return None
        self._ensure_file_view_mutable(record, operation="delete")
        if record.bucket == "persistent":
            if approval_mode_disabled():
                return self._delete_record(
                    record,
                    user_id=user_id,
                    reason="approval_mode_disabled",
                )
            approval = self.approval_svc.create_request(
                "workspace_file_delete",
                assistant_id=record.assistant_id,
                capability_profile=self._capability_profile_for_assistant(record.assistant_id),
                approval_scope="user",
                reason=f"Delete persistent file: {record.filename}",
                target=record.filename,
            )
            push_request = ExternalPushRequest(
                user_id=user_id,
                assistant_id=record.assistant_id,
                filename=record.filename,
                bucket=record.bucket,
                status="pending_approval",
                approval_id=approval.id,
            )
            run_sync(self.push_repository.asave_push_request(push_request))
            return push_request
        return self._delete_record(record, user_id=user_id)

    def delete_assistant_files(
        self,
        assistant_id: str,
        *,
        user_id: str = "",
        reason: str = "assistant_deleted",
    ) -> int:
        deleted_count = 0
        for record in self.list_files(assistant_id=assistant_id, include_deleted=False):
            self._delete_record(record, user_id=user_id, reason=reason)
            deleted_count += 1
        return deleted_count

    def list_events(
        self,
        *,
        assistant_id: str | None = None,
        file_id: str | None = None,
        limit: int = 100,
    ) -> list[WorkspaceFileEvent]:
        items = run_sync(
            self.event_repository.alist_events(
                assistant_id=assistant_id,
                file_id=file_id,
                limit=limit,
            )
        )
        return sorted(
            items,
            key=lambda item: (item.created_at, _event_priority(item.action)),
            reverse=True,
        )

    def list_push_requests(
        self,
        *,
        assistant_id: str | None = None,
        limit: int = 100,
    ) -> list[ExternalPushRequest]:
        return run_sync(
            self.push_repository.alist_push_requests(assistant_id=assistant_id, limit=limit)
        )

    def _iter_bucket_files(self, bucket_dir: Path) -> Iterable[tuple[str, Path]]:
        if not bucket_dir.exists():
            return []
        files: list[tuple[str, Path]] = []
        for path in sorted(bucket_dir.rglob("*")):
            if not path.is_file():
                continue
            relative_name = path.relative_to(bucket_dir).as_posix()
            if _is_workspace_storage_marker_name(relative_name):
                continue
            files.append((relative_name, path))
        return files

    def scan_workspace_results(
        self,
        assistant_id: str | None = None,
        *,
        bucket: TargetBucket | None = None,
        user_id: str | None = None,
        changed_after: datetime | None = None,
        result_path_prefix: str | None = None,
        result_source_ref: str | None = None,
    ) -> WorkspaceScanSummary:
        summary = WorkspaceScanSummary()
        resolved_user_id = (user_id or "").strip()
        resolved_changed_after = self._normalize_scan_changed_after(changed_after)
        resolved_result_path_prefix = _normalize_workspace_scan_path(result_path_prefix)
        _validate_result_scan_scope(resolved_result_path_prefix, bucket)
        resolved_result_source_ref = (result_source_ref or "").strip()
        log.info(
            "workspace_result_scan_started",
            assistant_id=assistant_id,
            bucket=bucket,
            user_id=resolved_user_id,
            changed_after=(
                resolved_changed_after.isoformat() if resolved_changed_after is not None else None
            ),
            result_path_prefix=resolved_result_path_prefix,
            result_source_ref=resolved_result_source_ref,
            task_scoped=bool(resolved_result_path_prefix),
        )
        assistants = (
            [self.assistant_svc.get_assistant(assistant_id)]
            if assistant_id
            else self.assistant_svc.list_assistants()
        )
        for assistant in assistants:
            if assistant is None:
                continue
            if getattr(assistant, "deleted", False):
                continue
            summary.assistants_scanned += 1
            buckets: list[TargetBucket] = (
                [bucket]
                if bucket is not None
                else list(
                    WORKSPACE_RESULT_BUCKETS
                    if resolved_result_path_prefix
                    else WORKSPACE_SCAN_BUCKETS
                )
            )
            for current_bucket in buckets:
                summary.files_scanned += self._scan_single_bucket(
                    assistant_id=assistant.assistant_id,
                    bucket=current_bucket,
                    user_id=resolved_user_id,
                    changed_after=resolved_changed_after,
                    result_path_prefix=resolved_result_path_prefix,
                    result_source_ref=resolved_result_source_ref,
                    summary=summary,
                )
            if any(current_bucket in {"skills", "plugins"} for current_bucket in buckets):
                self._backfill_workspace_items_from_files(
                    assistant_id=assistant.assistant_id,
                    kind=bucket if bucket in {"skills", "plugins"} else None,
                )
        log.info(
            "workspace_result_scan_completed",
            assistant_id=assistant_id,
            bucket=bucket,
            user_id=resolved_user_id,
            result_path_prefix=resolved_result_path_prefix,
            result_source_ref=resolved_result_source_ref,
            assistants_scanned=summary.assistants_scanned,
            files_scanned=summary.files_scanned,
            files_synced=summary.files_synced,
            files_deleted=summary.files_deleted,
            records_count=len(summary.records),
            errors_count=len(summary.errors),
            record_file_ids=_scan_log_file_ids(summary.records),
            record_file_ids_truncated=len(summary.records) > 20,
        )
        return summary

    def sync_workspace_outputs(self, assistant_id: str) -> list[WorkspaceFile]:
        assistant = self.assistant_svc.get_assistant(assistant_id)
        if getattr(assistant, "deleted", False):
            return []
        if assistant is None and not self._workspace_dir(assistant_id).exists():
            return []
        summary = WorkspaceScanSummary(assistants_scanned=1)
        for current_bucket in ("temp", "persistent"):
            summary.files_scanned += self._scan_single_bucket(
                assistant_id=assistant_id,
                bucket=current_bucket,
                summary=summary,
            )
        synced: list[WorkspaceFile] = []
        for record in summary:
            synced.append(record)
        return synced

    def _backfill_workspace_items_from_files(
        self,
        *,
        assistant_id: str,
        kind: TargetBucket | None = None,
    ) -> None:
        from src.workspace_files.item_backfill import backfill_workspace_item_index
        from src.workspace_files.item_index import WorkspaceItemIndexStore

        run_sync(
            backfill_workspace_item_index(
                file_repository=self.file_repository,
                item_index_store=WorkspaceItemIndexStore(),
                assistant_id=assistant_id,
                kind=kind if kind in {"skills", "plugins"} else None,
                prune_missing=True,
            )
        )

    def _scan_single_bucket(
        self,
        *,
        assistant_id: str,
        bucket: TargetBucket,
        user_id: str = "",
        changed_after: datetime | None = None,
        result_path_prefix: str = "",
        result_source_ref: str = "",
        summary: WorkspaceScanSummary,
    ) -> int:
        bucket_dir = self._bucket_dir(assistant_id, bucket)
        bucket_result_prefix = _bucket_relative_result_prefix(result_path_prefix, bucket)
        if bucket_result_prefix is None:
            log.info(
                "workspace_result_scan_bucket_skipped",
                assistant_id=assistant_id,
                bucket=bucket,
                reason="prefix_not_in_bucket",
                user_id=user_id,
                result_path_prefix=result_path_prefix,
                result_source_ref=result_source_ref,
            )
            return 0
        seen_filenames: set[str] = set()
        scanned = 0
        synced_before = summary.files_synced
        deleted_before = summary.files_deleted
        errors_before = len(summary.errors)
        conflicts = 0
        unchanged = 0
        filtered_by_changed_after = 0
        skipped_inbound = 0
        capability_bucket = bucket in {"skills", "plugins"}
        task_scoped_result = bool(result_path_prefix)
        for filename, path in self._iter_bucket_files(bucket_dir):
            if bucket_result_prefix and not _matches_bucket_relative_prefix(
                filename,
                bucket_result_prefix,
            ):
                continue
            scanned += 1
            seen_filenames.add(filename)
            try:
                self.validate_filename(filename)
                existing = run_sync(
                    self.file_repository.aget_active_file_by_path(
                        assistant_id=assistant_id,
                        bucket=bucket,
                        filename=filename,
                    )
                )
                if (
                    not capability_bucket
                    and existing is not None
                    and existing.direction == "inbound"
                    and existing.file_role != "result"
                    and existing.source_type not in {"assistant_result", "assistant_output"}
                ):
                    skipped_inbound += 1
                    continue
                if not capability_bucket and existing is not None and task_scoped_result:
                    conflict_reasons: list[str] = []
                    if user_id:
                        if existing.owner_user_id and existing.owner_user_id != user_id:
                            conflict_reasons.append(
                                f"owner_user_id={existing.owner_user_id!r}"
                            )
                        if existing.uploader_user_id and existing.uploader_user_id != user_id:
                            conflict_reasons.append(
                                f"uploader_user_id={existing.uploader_user_id!r}"
                            )
                    if (
                        result_source_ref
                        and not _is_unbound_workspace_scan_ref(existing.source_ref)
                        and existing.source_ref != result_source_ref
                    ):
                        conflict_reasons.append(f"source_ref={existing.source_ref!r}")
                    if conflict_reasons:
                        conflicts += 1
                        summary.errors.append(
                            f"{assistant_id}:{bucket}:{filename}: existing result binding mismatch "
                            f"for task-scoped sync ({', '.join(conflict_reasons)})"
                        )
                        continue
                stat = path.stat()
                mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
                if changed_after is not None and mtime < changed_after:
                    filtered_by_changed_after += 1
                    continue
                if (
                    existing is not None
                    and existing.size_bytes == stat.st_size
                    and existing.workspace_last_modified_at == mtime
                    and existing.sync_status in {"synced", "synced_to_workspace"}
                ):
                    metadata_changed = False
                    if not capability_bucket:
                        if task_scoped_result and user_id:
                            if existing.uploader_user_id != user_id:
                                existing.uploader_user_id = user_id
                                metadata_changed = True
                            if existing.owner_user_id != user_id:
                                existing.owner_user_id = user_id
                                metadata_changed = True
                        elif (
                            user_id
                            and (not existing.uploader_user_id or not existing.owner_user_id)
                            and changed_after is not None
                            and mtime >= changed_after
                        ):
                            if not existing.uploader_user_id:
                                existing.uploader_user_id = user_id
                                metadata_changed = True
                            if not existing.owner_user_id:
                                existing.owner_user_id = user_id
                                metadata_changed = True
                        if (
                            task_scoped_result
                            and result_source_ref
                            and _is_unbound_workspace_scan_ref(existing.source_ref)
                        ):
                            existing.source_ref = result_source_ref
                            metadata_changed = True
                        if existing.source_type != "assistant_result":
                            existing.source_type = "assistant_result"
                            metadata_changed = True
                        if existing.direction != "outbound":
                            existing.direction = "outbound"
                            metadata_changed = True
                        if existing.file_role != "result":
                            existing.file_role = "result"
                            metadata_changed = True
                    if metadata_changed:
                        existing.updated_at = datetime.now(tz=UTC)
                        existing = run_sync(self.file_repository.asave_file(existing))
                        summary.records.append(existing)
                        summary.files_synced += 1
                    else:
                        unchanged += 1
                    continue
                content = path.read_bytes()
                record = existing or WorkspaceFile(
                    assistant_id=assistant_id,
                    bucket=bucket,
                    filename=filename,
                    relative_path=f"{bucket}/{filename}",
                    direction="inbound" if capability_bucket else "outbound",
                    file_role="input" if capability_bucket else "result",
                    source_type="capability_package" if capability_bucket else "assistant_result",
                    source_ref=(
                        "workspace_scan"
                        if capability_bucket
                        else (result_source_ref if task_scoped_result else "") or "workspace_scan"
                    ),
                    source_file_id=(
                        None
                        if capability_bucket
                        else self._pick_source_file_id(
                            assistant_id=assistant_id,
                            bucket=bucket,
                        )
                    ),
                )
                if capability_bucket:
                    record.direction = "inbound"
                    record.file_role = "input"
                    record.source_type = "capability_package"
                    record.source_ref = record.source_ref or "workspace_scan"
                    record.source_file_id = None
                else:
                    record.source_type = "assistant_result"
                    if task_scoped_result and result_source_ref:
                        record.source_ref = result_source_ref
                    if not record.source_ref:
                        record.source_ref = "workspace_scan"
                    record.direction = "outbound"
                    record.file_role = "result"
                if not capability_bucket and not record.source_file_id:
                    record.source_file_id = self._pick_source_file_id(
                        assistant_id=assistant_id,
                        bucket=bucket,
                    )
                if not capability_bucket and not record.owner_user_id and record.source_file_id:
                    record.owner_user_id = self._owner_for_source_file_id(record.source_file_id)
                if not capability_bucket and user_id:
                    record.uploader_user_id = user_id
                    record.owner_user_id = (
                        user_id if task_scoped_result else record.owner_user_id or user_id
                    )
                record.size_bytes = len(content)
                record.content_type = record.content_type or _guess_content_type(filename)
                record.deleted = False
                record.workspace_last_modified_at = mtime
                try:
                    record = self._sync_blob(record=record, content=content)
                except Exception as exc:
                    record = self._mark_sync_failed(
                        record=record,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    summary.errors.append(record.sync_error)
                record = run_sync(self.file_repository.asave_file(record))
                summary.records.append(record)
                run_sync(
                    self.event_repository.aappend_event(
                        WorkspaceFileEvent(
                            file_id=record.file_id,
                            assistant_id=assistant_id,
                            action=self._scan_event_action(),
                            source_type=record.source_type,
                            source_ref=record.source_ref,
                            detail={
                                "filename": filename,
                                "bucket": bucket,
                                "size": len(content),
                                "kind": "created" if existing is None else "updated",
                                "detected_change": "created" if existing is None else "updated",
                                "source_file_id": record.source_file_id,
                                "sync_status": record.sync_status,
                            },
                        )
                    )
                )
                summary.files_synced += 1
            except Exception as exc:
                summary.errors.append(
                    f"{assistant_id}:{bucket}:{filename}: {type(exc).__name__}: {exc}"
                )

        existing_records = self.list_files(assistant_id, bucket=bucket, include_deleted=False)
        for record in existing_records:
            if bucket_result_prefix and not _matches_bucket_relative_prefix(
                record.filename,
                bucket_result_prefix,
            ):
                continue
            if record.filename in seen_filenames:
                continue
            if capability_bucket:
                if record.source_type != "capability_package":
                    continue
            elif (
                record.direction != "outbound"
                and record.file_role != "result"
                and record.source_type
                not in {
                    "assistant_result",
                    "assistant_output",
                    "workspace_scan",
                    "workspace-scan",
                }
            ):
                continue
            self._delete_record(record, reason="workspace_scan_missing")
            summary.files_deleted += 1
        log.info(
            "workspace_result_scan_bucket_completed",
            assistant_id=assistant_id,
            bucket=bucket,
            user_id=user_id,
            result_path_prefix=result_path_prefix,
            bucket_result_prefix=bucket_result_prefix,
            result_source_ref=result_source_ref,
            task_scoped=task_scoped_result,
            scanned=scanned,
            synced=summary.files_synced - synced_before,
            deleted=summary.files_deleted - deleted_before,
            errors=len(summary.errors) - errors_before,
            conflicts=conflicts,
            unchanged=unchanged,
            filtered_by_changed_after=filtered_by_changed_after,
            skipped_inbound=skipped_inbound,
        )
        return scanned

    def _normalize_scan_changed_after(self, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _pick_source_file_id(self, *, assistant_id: str, bucket: TargetBucket) -> str | None:
        candidates = self.list_files(
            assistant_id,
            direction="inbound",
            include_deleted=False,
        )
        if not candidates:
            return None
        same_bucket = [item for item in candidates if item.bucket == bucket]
        ordered = same_bucket or candidates
        ordered.sort(key=lambda item: (item.updated_at, item.created_at), reverse=True)
        return ordered[0].file_id

    def _owner_for_source_file_id(self, source_file_id: str | None) -> str:
        if not source_file_id:
            return ""
        source = self.get_file(source_file_id)
        if source is None or source.deleted:
            return ""
        return source.owner_user_id

    def _scan_event_action(self) -> str:
        # Keep both legacy JSON flows and newer DB-backed flows working while
        # the workspace scan contract is shared by two generations of tests/UI.
        if isinstance(self.file_repository, JsonWorkspaceFileRepository):
            return "ingest"
        return "scan_detected"


def _scan_log_file_ids(records: list[WorkspaceFile]) -> list[str]:
    return [record.file_id for record in records[:20]]


def _guess_content_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".txt":
        return "text/plain"
    if suffix == ".json":
        return "application/json"
    if suffix == ".pdf":
        return "application/pdf"
    if suffix == ".csv":
        return "text/csv"
    return "application/octet-stream"


def _event_priority(action: str) -> int:
    priorities = {
        "download": 5,
        "delete": 4,
        "overwrite": 3,
        "scan_detected": 2,
        "ingest": 2,
        "sync": 2,
        "push": 1,
        "upload": 0,
    }
    return priorities.get(action, 0)
