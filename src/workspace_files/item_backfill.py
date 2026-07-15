"""Backfill assistant workspace item indexes from workspace file records."""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.workspace_files.item_index_writer import (
    WorkspaceItemIndexRecord,
    WorkspaceItemKind,
    utc_now,
)
from src.workspace_files.models import WorkspaceFile

CAPABILITY_BUCKETS: frozenset[str] = frozenset({"skills", "plugins"})
DEFAULT_BACKFILL_SYNC_STATUS = "stored"
DEFAULT_BACKFILL_RUNTIME_RELOAD_STATUS = "skipped"


@dataclass(slots=True)
class WorkspaceItemBackfillStats:
    scanned: int = 0
    created: int = 0
    updated: int = 0
    removed: int = 0
    skipped: int = 0
    errors: int = 0


@dataclass(slots=True)
class _BackfillGroup:
    assistant_id: str
    kind: WorkspaceItemKind
    slug: str
    files: list[WorkspaceFile] = field(default_factory=list)


async def backfill_workspace_item_index(
    *,
    file_repository: Any,
    item_index_store: Any,
    assistant_id: str | None = None,
    kind: WorkspaceItemKind | str | None = None,
    prune_missing: bool = False,
    now: datetime | None = None,
) -> WorkspaceItemBackfillStats:
    """Upsert capability item indexes from existing workspace file metadata.

    The helper reads only workspace file records and never scans COS or local
    assistant workspaces.
    """

    normalized_kind = _normalize_optional_kind(kind)
    timestamp = now or utc_now()
    stats = WorkspaceItemBackfillStats()
    files = await _alist_files(
        file_repository,
        assistant_id=_clean(assistant_id),
        kind=normalized_kind,
    )
    groups: dict[tuple[str, WorkspaceItemKind, str], _BackfillGroup] = {}
    for record in files:
        stats.scanned += 1
        candidate = _candidate_from_file(record, kind=normalized_kind)
        if candidate is None:
            stats.skipped += 1
            continue
        key = (candidate.assistant_id, candidate.kind, candidate.slug)
        group = groups.setdefault(
            key,
            _BackfillGroup(
                assistant_id=candidate.assistant_id,
                kind=candidate.kind,
                slug=candidate.slug,
            ),
        )
        group.files.append(record)

    for group in groups.values():
        try:
            existing = await _aget_existing(
                item_index_store,
                assistant_id=group.assistant_id,
                kind=group.kind,
                slug=group.slug,
            )
            index_record = _build_record(group, existing=existing, now=timestamp)
            await _aupsert_item(item_index_store, index_record)
        except Exception:
            stats.errors += 1
            continue
        if existing is None:
            stats.created += 1
        else:
            stats.updated += 1
    if prune_missing and _clean(assistant_id) is not None:
        try:
            removed = await _prune_missing_items(
                item_index_store,
                assistant_id=_clean(assistant_id) or "",
                kind=normalized_kind,
                active_keys=set(groups),
                now=timestamp,
            )
            stats.removed += removed
        except Exception:
            stats.errors += 1
    return stats


backfill_assistant_workspace_items = backfill_workspace_item_index


def _candidate_from_file(
    record: WorkspaceFile,
    *,
    kind: WorkspaceItemKind | None,
) -> _BackfillGroup | None:
    assistant_id = _clean(record.assistant_id)
    bucket = _clean(record.bucket)
    if assistant_id is None or bucket not in CAPABILITY_BUCKETS:
        return None
    normalized_bucket = _normalize_kind(bucket)
    if kind is not None and normalized_bucket != kind:
        return None
    if record.deleted:
        return None
    slug = _slug_from_relative_path(record.relative_path, bucket=normalized_bucket)
    if slug is None:
        return None
    return _BackfillGroup(assistant_id=assistant_id, kind=normalized_bucket, slug=slug)


def _slug_from_relative_path(
    relative_path: str,
    *,
    bucket: WorkspaceItemKind,
) -> str | None:
    parts = [part for part in str(relative_path).replace("\\", "/").strip("/").split("/") if part]
    if len(parts) < 2:
        return None
    if parts[0] == bucket:
        if len(parts) < 3:
            return None
        slug = parts[1]
    else:
        slug = parts[0]
    if slug in {".", "..", "skills", "plugins"} or slug.startswith("."):
        return None
    return slug


def _build_record(
    group: _BackfillGroup,
    *,
    existing: Any | None,
    now: datetime,
) -> WorkspaceItemIndexRecord:
    sync_status = _safe_existing_sync_status(existing) or DEFAULT_BACKFILL_SYNC_STATUS
    runtime_reload_status = (
        _safe_existing_runtime_reload_status(existing)
        or DEFAULT_BACKFILL_RUNTIME_RELOAD_STATUS
    )
    return WorkspaceItemIndexRecord(
        assistant_id=group.assistant_id,
        kind=group.kind,
        slug=group.slug,
        name=str(_field(existing, "name", "") or group.slug),
        version=str(_field(existing, "version", "") or ""),
        enabled=bool(_field(existing, "enabled", True)),
        install_path=str(_field(existing, "install_path", "") or f"{group.kind}/{group.slug}"),
        files_count=len(group.files),
        content_hash=str(_field(existing, "content_hash", "") or ""),
        source=str(_field(existing, "source", "") or "backfill"),
        sync_status=sync_status,
        runtime_reload_status=runtime_reload_status,
        runtime_reload_error=str(_field(existing, "runtime_reload_error", "") or ""),
        runtime_effective=runtime_reload_status != "failed",
        last_indexed_at=now,
        last_synced_at=_latest_datetime(
            file.last_synced_at for file in group.files if file.last_synced_at is not None
        ),
        last_error=str(_field(existing, "last_error", "") or ""),
        created_at=_field(existing, "created_at", now) or now,
        updated_at=now,
        deleted=False,
    )


def _safe_existing_sync_status(existing: Any | None) -> str:
    status = str(_field(existing, "sync_status", "") or "").strip()
    if status in {"missing", "missing_from_workspace", "deleted", "deleted_from_workspace"}:
        return ""
    return status


def _safe_existing_runtime_reload_status(existing: Any | None) -> str:
    status = str(_field(existing, "runtime_reload_status", "") or "").strip()
    if status in {"applied", "failed", "skipped"}:
        return status
    return ""


async def _alist_files(
    repository: Any,
    *,
    assistant_id: str | None,
    kind: WorkspaceItemKind | None,
) -> list[WorkspaceFile]:
    method = getattr(repository, "alist_files", None)
    if method is not None:
        return list(
            await _call_method(
                method,
                assistant_id=assistant_id,
                bucket=kind,
                include_deleted=True,
        )
            )
    method = getattr(repository, "list_files", None)
    if method is not None and assistant_id is not None:
        return list(
            await _call_method(
                method,
                assistant_id=assistant_id,
                bucket=kind,
                include_deleted=True,
        )
            )
    method = getattr(repository, "load_files", None)
    if method is None:
        raise TypeError("workspace file repository does not expose a file listing method")
    files = list(await _maybe_await(method()))
    if assistant_id is not None:
        files = [file for file in files if file.assistant_id == assistant_id]
    if kind is not None:
        files = [file for file in files if file.bucket == kind]
    return files


async def _aget_existing(
    store: Any,
    *,
    assistant_id: str,
    kind: WorkspaceItemKind,
    slug: str,
) -> Any | None:
    for name in (
        "aget_item",
        "aget_workspace_item",
        "aget",
        "get_item",
        "get_workspace_item",
        "get",
    ):
        method = getattr(store, name, None)
        if method is None:
            continue
        return await _call_method(
            method,
            assistant_id=assistant_id,
            kind=kind,
            slug=slug,
            include_deleted=True,
        )
    return None


async def _aupsert_item(store: Any, record: WorkspaceItemIndexRecord) -> Any:
    for name in ("aupsert_item", "aupsert_workspace_item", "asave_item", "aupsert", "asave"):
        method = getattr(store, name, None)
        if method is not None:
            return await _call_save_method(method, record)
    for name in ("upsert_item", "upsert_workspace_item", "save_item", "upsert", "save"):
        method = getattr(store, name, None)
        if method is not None:
            return await _call_save_method(method, record)
    raise TypeError("workspace item index store does not expose an upsert/save method")


async def _prune_missing_items(
    store: Any,
    *,
    assistant_id: str,
    kind: WorkspaceItemKind | None,
    active_keys: set[tuple[str, WorkspaceItemKind, str]],
    now: datetime,
) -> int:
    removed = 0
    for existing in await _alist_items(
        store,
        assistant_id=assistant_id,
        kind=kind,
        include_deleted=False,
    ):
        existing_kind = _normalize_kind(str(_field(existing, "kind", "")))
        key = (
            str(_field(existing, "assistant_id", "") or ""),
            existing_kind,
            str(_field(existing, "slug", "") or ""),
        )
        if key in active_keys:
            continue
        if _field(existing, "enabled", True) is False:
            continue
        record = _build_removed_record(existing, now=now)
        await _aupsert_item(store, record)
        removed += 1
    return removed


def _build_removed_record(existing: Any, *, now: datetime) -> WorkspaceItemIndexRecord:
    kind = _normalize_kind(str(_field(existing, "kind", "")))
    return WorkspaceItemIndexRecord(
        assistant_id=str(_field(existing, "assistant_id", "") or ""),
        kind=kind,
        slug=str(_field(existing, "slug", "") or ""),
        name=str(_field(existing, "name", "") or ""),
        version=str(_field(existing, "version", "") or ""),
        enabled=False,
        install_path=str(_field(existing, "install_path", "") or ""),
        files_count=0,
        content_hash=str(_field(existing, "content_hash", "") or ""),
        source=str(_field(existing, "source", "") or "backfill"),
        sync_status="deleted_from_workspace",
        runtime_reload_status=str(_field(existing, "runtime_reload_status", "") or "skipped"),
        runtime_reload_error=str(_field(existing, "runtime_reload_error", "") or ""),
        runtime_effective=False,
        last_indexed_at=now,
        last_synced_at=_field(existing, "last_synced_at", None),
        last_error=str(_field(existing, "last_error", "") or ""),
        created_at=_field(existing, "created_at", now) or now,
        updated_at=now,
        deleted=True,
    )


async def _alist_items(
    store: Any,
    *,
    assistant_id: str,
    kind: WorkspaceItemKind | None,
    include_deleted: bool,
) -> list[Any]:
    from src.workspace_files.item_index import WorkspaceItemIndexFilters

    records: list[Any] = []
    for name in ("alist_items", "list_items"):
        method = getattr(store, name, None)
        if method is None:
            continue
        page = 1
        while True:
            filters = WorkspaceItemIndexFilters(
                assistant_id=assistant_id,
                kind=kind,
                include_deleted=include_deleted,
                page=page,
                page_size=200,
            )
            result = await _maybe_await(method(filters))
            records.extend(list(_field(result, "records", []) or []))
            if not bool(_field(result, "has_next", False)):
                pagination = _field(result, "pagination", None)
                if not bool(_field(pagination, "has_next", False)):
                    break
            page += 1
        return records
    raise TypeError("workspace item index store does not expose a list_items method")


async def _call_save_method(method: Any, record: WorkspaceItemIndexRecord) -> Any:
    if _method_accepts_keyword(method, "record"):
        return await _maybe_await(method(record=record))
    if _method_accepts_keyword(method, "item"):
        return await _maybe_await(method(item=record))
    return await _maybe_await(method(record))


async def _call_method(method: Any, **kwargs: Any) -> Any:
    if _method_accepts_keyword(method, "assistant_id"):
        return await _maybe_await(method(**_supported_kwargs(method, kwargs)))
    if kwargs.get("assistant_id") is not None:
        assistant_id = kwargs["assistant_id"]
        remaining = {key: value for key, value in kwargs.items() if key != "assistant_id"}
        return await _maybe_await(method(assistant_id, **_supported_kwargs(method, remaining)))
    return await _maybe_await(method(**_supported_kwargs(method, kwargs)))


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _supported_kwargs(method: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return kwargs
    parameters = signature.parameters
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values()):
        return kwargs
    return {key: value for key, value in kwargs.items() if key in parameters}


def _method_accepts_keyword(method: Any, name: str) -> bool:
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return False
    parameters = signature.parameters
    return name in parameters or any(
        param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values()
    )


def _normalize_optional_kind(kind: WorkspaceItemKind | str | None) -> WorkspaceItemKind | None:
    cleaned = _clean(kind)
    if cleaned is None:
        return None
    return _normalize_kind(cleaned)


def _normalize_kind(kind: str) -> WorkspaceItemKind:
    if kind not in CAPABILITY_BUCKETS:
        raise ValueError("kind must be skills or plugins")
    return kind  # type: ignore[return-value]


def _latest_datetime(values: Iterable[datetime]) -> datetime | None:
    latest: datetime | None = None
    for value in values:
        if latest is None or value > latest:
            latest = value
    return latest


def _field(item: Any | None, name: str, default: Any) -> Any:
    if item is None:
        return default
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _clean(value: object | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None
