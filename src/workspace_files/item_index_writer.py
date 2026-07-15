from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

WorkspaceItemKind = Literal["skills", "plugins"]
RuntimeReloadStatus = Literal["applied", "failed", "skipped"]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class WorkspaceItemIndexRecord:
    assistant_id: str
    kind: WorkspaceItemKind
    slug: str
    name: str = ""
    version: str = ""
    enabled: bool = True
    install_path: str = ""
    files_count: int = 0
    content_hash: str = ""
    source: str = "market"
    sync_status: str = "synced"
    runtime_reload_status: str = "skipped"
    runtime_reload_error: str = ""
    runtime_effective: bool = True
    last_indexed_at: datetime | None = None
    last_synced_at: datetime | None = None
    last_error: str = ""
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    deleted: bool = False


class WorkspaceItemIndexStore(Protocol):
    async def aget_item(
        self,
        *,
        assistant_id: str,
        kind: WorkspaceItemKind,
        slug: str,
        include_deleted: bool = False,
    ) -> Any | None: ...

    async def aupsert_item(self, record: WorkspaceItemIndexRecord) -> Any: ...


async def record_workspace_item_installed(
    *,
    store: WorkspaceItemIndexStore | Any | None = None,
    repository: WorkspaceItemIndexStore | Any | None = None,
    assistant_id: str,
    kind: WorkspaceItemKind,
    slug: str,
    name: str = "",
    version: str = "",
    install_path: str = "",
    enabled: bool = True,
    files_count: int = 0,
    content_hash: str = "",
    source: str = "market",
    sync_status: str = "synced",
    runtime_reload_status: RuntimeReloadStatus = "skipped",
    runtime_reload_error: str = "",
    last_indexed_at: datetime | None = None,
    last_synced_at: datetime | None = None,
    last_error: str = "",
    now: datetime | None = None,
) -> WorkspaceItemIndexRecord:
    resolved_store = _require_store(store, repository)
    existing = await _get_existing(resolved_store, assistant_id=assistant_id, kind=kind, slug=slug)
    record = _build_record(
        existing,
        assistant_id=assistant_id,
        kind=kind,
        slug=slug,
        name=name,
        version=version,
        enabled=enabled,
        install_path=install_path,
        files_count=files_count,
        content_hash=content_hash,
        source=source,
        sync_status=sync_status,
        runtime_reload_status=runtime_reload_status,
        runtime_reload_error=runtime_reload_error,
        runtime_effective=_runtime_effective(runtime_reload_status),
        last_indexed_at=last_indexed_at,
        last_synced_at=last_synced_at,
        last_error=last_error,
        deleted=False,
        now=now or utc_now(),
    )
    return await _save_record(resolved_store, record)


async def record_workspace_item_reused(
    *,
    store: WorkspaceItemIndexStore | Any | None = None,
    repository: WorkspaceItemIndexStore | Any | None = None,
    source_assistant_id: str = "",
    target_assistant_id: str | None = None,
    assistant_id: str | None = None,
    kind: WorkspaceItemKind,
    slug: str,
    name: str = "",
    version: str = "",
    install_path: str = "",
    files_count: int = 0,
    content_hash: str = "",
    sync_status: str = "synced",
    runtime_reload_status: RuntimeReloadStatus = "skipped",
    runtime_reload_error: str = "",
    last_indexed_at: datetime | None = None,
    last_synced_at: datetime | None = None,
    last_error: str = "",
    now: datetime | None = None,
) -> WorkspaceItemIndexRecord:
    del source_assistant_id
    resolved_assistant_id = target_assistant_id or assistant_id
    if resolved_assistant_id is None:
        raise ValueError("target_assistant_id or assistant_id is required")
    return await record_workspace_item_installed(
        store=store,
        repository=repository,
        assistant_id=resolved_assistant_id,
        kind=kind,
        slug=slug,
        name=name,
        version=version,
        install_path=install_path,
        enabled=True,
        files_count=files_count,
        content_hash=content_hash,
        source="reuse",
        sync_status=sync_status,
        runtime_reload_status=runtime_reload_status,
        runtime_reload_error=runtime_reload_error,
        last_indexed_at=last_indexed_at,
        last_synced_at=last_synced_at,
        last_error=last_error,
        now=now,
    )


async def record_workspace_item_toggle(
    *,
    store: WorkspaceItemIndexStore | Any | None = None,
    repository: WorkspaceItemIndexStore | Any | None = None,
    assistant_id: str,
    kind: WorkspaceItemKind,
    slug: str,
    enabled: bool,
    runtime_reload_status: RuntimeReloadStatus = "skipped",
    runtime_reload_error: str = "",
    last_error: str = "",
    now: datetime | None = None,
) -> WorkspaceItemIndexRecord:
    resolved_store = _require_store(store, repository)
    existing = await _get_existing(resolved_store, assistant_id=assistant_id, kind=kind, slug=slug)
    record = _build_record(
        existing,
        assistant_id=assistant_id,
        kind=kind,
        slug=slug,
        enabled=enabled,
        runtime_reload_status=runtime_reload_status,
        runtime_reload_error=runtime_reload_error,
        runtime_effective=_runtime_effective(runtime_reload_status),
        last_error=last_error,
        deleted=False,
        now=now or utc_now(),
    )
    return await _save_record(resolved_store, record)


async def record_workspace_item_removed(
    *,
    store: WorkspaceItemIndexStore | Any | None = None,
    repository: WorkspaceItemIndexStore | Any | None = None,
    assistant_id: str,
    kind: WorkspaceItemKind,
    slug: str,
    runtime_reload_status: RuntimeReloadStatus = "skipped",
    runtime_reload_error: str = "",
    last_error: str = "",
    now: datetime | None = None,
) -> WorkspaceItemIndexRecord:
    resolved_store = _require_store(store, repository)
    existing = await _get_existing(resolved_store, assistant_id=assistant_id, kind=kind, slug=slug)
    record = _build_record(
        existing,
        assistant_id=assistant_id,
        kind=kind,
        slug=slug,
        enabled=False,
        sync_status="deleted",
        runtime_reload_status=runtime_reload_status,
        runtime_reload_error=runtime_reload_error,
        runtime_effective=_runtime_effective(runtime_reload_status),
        last_error=last_error,
        deleted=True,
        now=now or utc_now(),
    )
    return await _save_record(resolved_store, record)


async def record_workspace_item_reload_result(
    *,
    store: WorkspaceItemIndexStore | Any | None = None,
    repository: WorkspaceItemIndexStore | Any | None = None,
    assistant_id: str,
    kind: WorkspaceItemKind,
    slug: str,
    runtime_reload_status: RuntimeReloadStatus,
    runtime_reload_error: str = "",
    now: datetime | None = None,
) -> WorkspaceItemIndexRecord:
    resolved_store = _require_store(store, repository)
    existing = await _get_existing(resolved_store, assistant_id=assistant_id, kind=kind, slug=slug)
    record = _build_record(
        existing,
        assistant_id=assistant_id,
        kind=kind,
        slug=slug,
        runtime_reload_status=runtime_reload_status,
        runtime_reload_error=runtime_reload_error,
        runtime_effective=_runtime_effective(runtime_reload_status),
        now=now or utc_now(),
    )
    return await _save_record(resolved_store, record)


def _require_store(store: Any | None, repository: Any | None) -> Any:
    resolved = store or repository
    if resolved is None:
        raise ValueError("store or repository is required")
    return resolved


async def _get_existing(
    store: Any,
    *,
    assistant_id: str,
    kind: WorkspaceItemKind,
    slug: str,
) -> Any | None:
    normalized_assistant_id = _clean_required(assistant_id, "assistant_id")
    normalized_kind = _normalize_kind(kind)
    normalized_slug = _clean_required(slug, "slug")
    for name in ("aget_item", "aget_workspace_item", "aget", "get_item", "get_workspace_item", "get"):
        method = getattr(store, name, None)
        if method is None:
            continue
        return await _call_method(
            method,
            assistant_id=normalized_assistant_id,
            kind=normalized_kind,
            slug=normalized_slug,
            include_deleted=True,
        )
    return None


async def _save_record(store: Any, record: WorkspaceItemIndexRecord) -> WorkspaceItemIndexRecord:
    for name in ("aupsert_item", "aupsert_workspace_item", "asave_item", "aupsert", "asave"):
        method = getattr(store, name, None)
        if method is None:
            continue
        saved = await _call_save_method(method, record)
        return _coerce_record(saved, record)
    for name in ("upsert_item", "upsert_workspace_item", "save_item", "upsert", "save"):
        method = getattr(store, name, None)
        if method is None:
            continue
        saved = await _call_save_method(method, record)
        return _coerce_record(saved, record)
    raise TypeError("workspace item index store does not expose an upsert/save method")


async def _call_save_method(method: Any, record: WorkspaceItemIndexRecord) -> Any:
    if _method_accepts_keyword(method, "record"):
        return await _maybe_await(method(record=record))
    if _method_accepts_keyword(method, "item"):
        return await _maybe_await(method(item=record))
    if _method_accepts_keyword(method, "entry"):
        return await _maybe_await(method(entry=record))
    return await _maybe_await(method(record))


async def _call_method(method: Any, **kwargs: Any) -> Any:
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


def _build_record(
    existing: Any | None,
    *,
    assistant_id: str,
    kind: WorkspaceItemKind,
    slug: str,
    now: datetime,
    **updates: Any,
) -> WorkspaceItemIndexRecord:
    normalized_assistant_id = _clean_required(assistant_id, "assistant_id")
    normalized_kind = _normalize_kind(kind)
    normalized_slug = _clean_required(slug, "slug")
    return WorkspaceItemIndexRecord(
        assistant_id=normalized_assistant_id,
        kind=normalized_kind,
        slug=normalized_slug,
        name=str(updates.get("name", _field(existing, "name", "")) or ""),
        version=str(updates.get("version", _field(existing, "version", "")) or ""),
        enabled=bool(updates.get("enabled", _field(existing, "enabled", True))),
        install_path=str(updates.get("install_path", _field(existing, "install_path", "")) or ""),
        files_count=int(updates.get("files_count", _field(existing, "files_count", 0)) or 0),
        content_hash=str(updates.get("content_hash", _field(existing, "content_hash", "")) or ""),
        source=str(updates.get("source", _field(existing, "source", "market")) or "market"),
        sync_status=str(updates.get("sync_status", _field(existing, "sync_status", "synced")) or ""),
        runtime_reload_status=str(
            updates.get(
                "runtime_reload_status",
                _field(existing, "runtime_reload_status", "skipped"),
            )
            or "skipped"
        ),
        runtime_reload_error=str(
            updates.get(
                "runtime_reload_error",
                _field(existing, "runtime_reload_error", ""),
            )
            or ""
        ),
        runtime_effective=bool(
            updates.get("runtime_effective", _field(existing, "runtime_effective", True))
        ),
        last_indexed_at=updates.get(
            "last_indexed_at",
            _field(existing, "last_indexed_at", None),
        ),
        last_synced_at=updates.get("last_synced_at", _field(existing, "last_synced_at", None)),
        last_error=str(updates.get("last_error", _field(existing, "last_error", "")) or ""),
        created_at=_field(existing, "created_at", now) or now,
        updated_at=now,
        deleted=bool(updates.get("deleted", _field(existing, "deleted", False))),
    )


def _coerce_record(value: Any, fallback: WorkspaceItemIndexRecord) -> WorkspaceItemIndexRecord:
    if value is None:
        return fallback
    if isinstance(value, WorkspaceItemIndexRecord):
        return value
    return _build_record(
        value,
        assistant_id=_field(value, "assistant_id", fallback.assistant_id),
        kind=_field(value, "kind", fallback.kind),
        slug=_field(value, "slug", fallback.slug),
        now=_field(value, "updated_at", fallback.updated_at) or fallback.updated_at,
        name=_field(value, "name", fallback.name),
        version=_field(value, "version", fallback.version),
        enabled=_field(value, "enabled", fallback.enabled),
        install_path=_field(value, "install_path", fallback.install_path),
        files_count=_field(value, "files_count", fallback.files_count),
        content_hash=_field(value, "content_hash", fallback.content_hash),
        source=_field(value, "source", fallback.source),
        sync_status=_field(value, "sync_status", fallback.sync_status),
        runtime_reload_status=_field(
            value,
            "runtime_reload_status",
            fallback.runtime_reload_status,
        ),
        runtime_reload_error=_field(value, "runtime_reload_error", fallback.runtime_reload_error),
        runtime_effective=_field(value, "runtime_effective", fallback.runtime_effective),
        last_indexed_at=_field(value, "last_indexed_at", fallback.last_indexed_at),
        last_synced_at=_field(value, "last_synced_at", fallback.last_synced_at),
        last_error=_field(value, "last_error", fallback.last_error),
        deleted=_field(value, "deleted", fallback.deleted),
    )


def _field(item: Any | None, name: str, default: Any) -> Any:
    if item is None:
        return default
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _clean_required(value: str, field_name: str) -> str:
    cleaned = str(value).strip()
    if not cleaned:
        raise ValueError(f"{field_name} is required")
    return cleaned


def _normalize_kind(kind: WorkspaceItemKind | str) -> WorkspaceItemKind:
    cleaned = str(kind).strip()
    if cleaned not in {"skills", "plugins"}:
        raise ValueError("kind must be skills or plugins")
    return cleaned  # type: ignore[return-value]


def _runtime_effective(status: str) -> bool:
    return status != "failed"
