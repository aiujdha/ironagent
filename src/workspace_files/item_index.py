"""SQLite-backed assistant workspace item index."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast

from src.shared.config import get_settings
from src.workspace_files.item_index_writer import (
    WorkspaceItemIndexRecord,
    WorkspaceItemKind,
    utc_now,
)

MAX_PAGE_SIZE = 200
DEFAULT_WORKSPACE_ITEM_INDEX_DB = Path("./data/workspace_item_index.db")
CURRENT_ITEM_SYNC_STATUSES = {"synced", "stored", "uploaded"}


@dataclass(frozen=True)
class WorkspaceItemIndexFilters:
    assistant_ids: Iterable[str] | None = None
    assistant_id: str | None = None
    kind: str | None = None
    slug: str | None = None
    enabled: bool | None = None
    sync_status: str | None = None
    source: str | None = None
    query: str | None = None
    include_deleted: bool = False
    page: int = 1
    page_size: int = 50


@dataclass(frozen=True)
class WorkspaceItemIndexPagination:
    page: int
    page_size: int
    total: int
    has_next: bool


@dataclass(frozen=True)
class WorkspaceItemIndexStatus:
    source: str = "db"
    sync_status: str = "unknown"
    sync_error: str = ""
    stale: bool = False
    last_indexed_at: datetime | None = None
    stale_count: int = 0


@dataclass(frozen=True)
class WorkspaceItemIndexPage:
    records: list[WorkspaceItemIndexRecord]
    pagination: WorkspaceItemIndexPagination
    last_indexed_at: datetime | None
    sync_status_counts: dict[str, int] = field(default_factory=dict)
    stale_count: int = 0

    @property
    def total(self) -> int:
        return self.pagination.total

    @property
    def has_next(self) -> bool:
        return self.pagination.has_next

    @property
    def index_status(self) -> WorkspaceItemIndexStatus:
        if self.total == 0:
            sync_status = "empty"
        elif self.stale_count:
            sync_status = "stale"
        else:
            sync_status = "synced"
        return WorkspaceItemIndexStatus(
            sync_status=sync_status,
            stale=bool(self.stale_count),
            last_indexed_at=self.last_indexed_at,
            stale_count=self.stale_count,
        )


class WorkspaceItemIndexStore:
    _schema_lock = threading.Lock()
    _initialized_paths: set[Path] = set()

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or get_settings().workspace_item_index_sqlite_path
        self._ensure_schema()

    async def aget_item(
        self,
        *,
        assistant_id: str,
        kind: WorkspaceItemKind,
        slug: str,
        include_deleted: bool = False,
    ) -> WorkspaceItemIndexRecord | None:
        return self.get_item(
            assistant_id=assistant_id,
            kind=kind,
            slug=slug,
            include_deleted=include_deleted,
        )

    def get_item(
        self,
        *,
        assistant_id: str,
        kind: WorkspaceItemKind | str,
        slug: str,
        include_deleted: bool = False,
    ) -> WorkspaceItemIndexRecord | None:
        normalized_assistant_id = _clean_required(assistant_id, "assistant_id")
        normalized_kind = _normalize_kind(kind)
        normalized_slug = _clean_required(slug, "slug")
        clauses = ["assistant_id = ?", "kind = ?", "slug = ?"]
        params: list[object] = [normalized_assistant_id, normalized_kind, normalized_slug]
        if not include_deleted:
            clauses.append("deleted = 0")
        with self.connect(read_only=True) as conn:
            row = conn.execute(
                f"""
                SELECT * FROM assistant_workspace_items
                WHERE {' AND '.join(clauses)}
                LIMIT 1
                """,
                params,
            ).fetchone()
        return _record_from_row(row) if row is not None else None

    async def aupsert_item(
        self,
        record: WorkspaceItemIndexRecord,
    ) -> WorkspaceItemIndexRecord:
        return self.upsert_item(record)

    def upsert_item(self, record: WorkspaceItemIndexRecord) -> WorkspaceItemIndexRecord:
        now = utc_now()
        created_at = record.created_at or now
        updated_at = record.updated_at or now
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO assistant_workspace_items (
                    assistant_id, kind, slug, name, version, enabled, install_path,
                    files_count, content_hash, source, sync_status,
                    runtime_reload_status, runtime_reload_error, runtime_effective,
                    last_indexed_at, last_synced_at, last_error, deleted,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(assistant_id, kind, slug) DO UPDATE SET
                    name = excluded.name,
                    version = excluded.version,
                    enabled = excluded.enabled,
                    install_path = excluded.install_path,
                    files_count = excluded.files_count,
                    content_hash = excluded.content_hash,
                    source = excluded.source,
                    sync_status = excluded.sync_status,
                    runtime_reload_status = excluded.runtime_reload_status,
                    runtime_reload_error = excluded.runtime_reload_error,
                    runtime_effective = excluded.runtime_effective,
                    last_indexed_at = excluded.last_indexed_at,
                    last_synced_at = excluded.last_synced_at,
                    last_error = excluded.last_error,
                    deleted = excluded.deleted,
                    updated_at = excluded.updated_at
                """,
                _record_params(record, created_at=created_at, updated_at=updated_at),
            )
            row = conn.execute(
                """
                SELECT * FROM assistant_workspace_items
                WHERE assistant_id = ? AND kind = ? AND slug = ?
                """,
                (record.assistant_id, record.kind, record.slug),
            ).fetchone()
        if row is None:  # pragma: no cover - inserted row should be readable
            raise RuntimeError("workspace item index row could not be read")
        return _record_from_row(row)

    def list_items(
        self,
        filters: WorkspaceItemIndexFilters | None = None,
    ) -> WorkspaceItemIndexPage:
        resolved = filters or WorkspaceItemIndexFilters()
        _validate_pagination(resolved)
        visible_assistant_ids = _normalize_assistant_ids(resolved.assistant_ids)
        assistant_id = _clean(resolved.assistant_id)
        if visible_assistant_ids is not None and assistant_id is not None:
            if assistant_id not in visible_assistant_ids:
                return _empty_page(resolved)
        if visible_assistant_ids == set():
            return _empty_page(resolved)

        where_sql, params = _where_sql(
            resolved,
            visible_assistant_ids=visible_assistant_ids,
            assistant_id=assistant_id,
        )
        offset = (resolved.page - 1) * resolved.page_size
        count_sql = f"SELECT COUNT(*) FROM assistant_workspace_items {where_sql}"
        max_sql = f"SELECT MAX(updated_at) FROM assistant_workspace_items {where_sql}"
        status_sql = f"""
            SELECT sync_status, COUNT(*) AS count
            FROM assistant_workspace_items
            {where_sql}
            GROUP BY sync_status
        """
        rows_sql = f"""
            SELECT *
            FROM assistant_workspace_items
            {where_sql}
            ORDER BY updated_at DESC, kind ASC, slug ASC
            LIMIT ? OFFSET ?
        """
        with self.connect(read_only=True) as conn:
            total = int(conn.execute(count_sql, params).fetchone()[0])
            last_indexed_raw = conn.execute(max_sql, params).fetchone()[0]
            status_rows = conn.execute(status_sql, params).fetchall()
            rows = conn.execute(rows_sql, [*params, resolved.page_size, offset]).fetchall()

        records = [_record_from_row(row) for row in rows]
        sync_status_counts = {str(row["sync_status"] or ""): int(row["count"]) for row in status_rows}
        return WorkspaceItemIndexPage(
            records=records,
            pagination=WorkspaceItemIndexPagination(
                page=resolved.page,
                page_size=resolved.page_size,
                total=total,
                has_next=offset + len(records) < total,
            ),
            last_indexed_at=_parse_datetime(last_indexed_raw),
            sync_status_counts=sync_status_counts,
            stale_count=_count_stale(sync_status_counts),
        )

    async def alist_items(
        self,
        filters: WorkspaceItemIndexFilters | None = None,
    ) -> WorkspaceItemIndexPage:
        return self.list_items(filters)

    def connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            uri = f"file:{self.db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        self._configure_connection(conn, read_only=read_only)
        return conn

    def _ensure_schema(self) -> None:
        resolved = self.db_path.resolve()
        with self._schema_lock:
            if resolved in self._initialized_paths:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as conn:
                conn.executescript(_SCHEMA_SQL)
            self._initialized_paths.add(resolved)

    def _configure_connection(self, conn: sqlite3.Connection, *, read_only: bool) -> None:
        conn.execute("PRAGMA busy_timeout=30000")
        if not read_only:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")


async def list_workspace_item_index_page_async(
    filters: WorkspaceItemIndexFilters | None = None,
    *,
    store: WorkspaceItemIndexStore | None = None,
    **kwargs: object,
) -> WorkspaceItemIndexPage:
    return list_workspace_item_index_page(filters, store=store, **kwargs)


def list_workspace_item_index_page(
    filters: WorkspaceItemIndexFilters | None = None,
    *,
    assistant_id: str | None = None,
    assistant_ids: Iterable[str] | None = None,
    kind: str | None = None,
    slug: str | None = None,
    enabled: bool | None = None,
    sync_status: str | None = None,
    source: str | None = None,
    query: str | None = None,
    include_deleted: bool | None = None,
    page: int | None = None,
    page_size: int | None = None,
    store: WorkspaceItemIndexStore | None = None,
    workspace_file_repository: object | None = None,
    workspace_file_store: object | None = None,
    **_ignored: object,
) -> WorkspaceItemIndexPage:
    resolved = _merge_filters(
        filters,
        assistant_id=assistant_id,
        assistant_ids=assistant_ids,
        kind=kind,
        slug=slug,
        enabled=enabled,
        sync_status=sync_status,
        source=source,
        query=query,
        include_deleted=include_deleted,
        page=page,
        page_size=page_size,
    )
    page_result = (store or WorkspaceItemIndexStore()).list_items(resolved)
    return _with_workspace_file_aggregates(
        page_result,
        workspace_file_repository=workspace_file_repository,
        workspace_file_store=workspace_file_store,
    )


list_workspace_items_page = list_workspace_item_index_page
list_assistant_workspace_items_page = list_workspace_item_index_page
query_workspace_items_page = list_workspace_item_index_page


def _merge_filters(
    filters: WorkspaceItemIndexFilters | None,
    *,
    assistant_id: str | None,
    assistant_ids: Iterable[str] | None,
    kind: str | None,
    slug: str | None,
    enabled: bool | None,
    sync_status: str | None,
    source: str | None,
    query: str | None,
    include_deleted: bool | None,
    page: int | None,
    page_size: int | None,
) -> WorkspaceItemIndexFilters:
    base = filters or WorkspaceItemIndexFilters()
    return WorkspaceItemIndexFilters(
        assistant_ids=assistant_ids if assistant_ids is not None else base.assistant_ids,
        assistant_id=assistant_id if assistant_id is not None else base.assistant_id,
        kind=kind if kind is not None else base.kind,
        slug=slug if slug is not None else base.slug,
        enabled=enabled if enabled is not None else base.enabled,
        sync_status=sync_status if sync_status is not None else base.sync_status,
        source=source if source is not None else base.source,
        query=query if query is not None else base.query,
        include_deleted=(
            include_deleted if include_deleted is not None else base.include_deleted
        ),
        page=page if page is not None else base.page,
        page_size=page_size if page_size is not None else base.page_size,
    )


def _where_sql(
    filters: WorkspaceItemIndexFilters,
    *,
    visible_assistant_ids: set[str] | None,
    assistant_id: str | None,
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if not filters.include_deleted:
        clauses.append("deleted = 0")
    if assistant_id is not None:
        clauses.append("assistant_id = ?")
        params.append(assistant_id)
    elif visible_assistant_ids is not None:
        placeholders = ", ".join("?" for _ in visible_assistant_ids)
        clauses.append(f"assistant_id IN ({placeholders})")
        params.extend(sorted(visible_assistant_ids))
    kind = _clean(filters.kind)
    if kind is not None:
        clauses.append("kind = ?")
        params.append(_normalize_kind(kind))
    slug = _clean(filters.slug)
    if slug is not None:
        clauses.append("slug = ?")
        params.append(slug)
    if filters.enabled is not None:
        clauses.append("enabled = ?")
        params.append(1 if filters.enabled else 0)
    sync_status = _clean(filters.sync_status)
    if sync_status is not None:
        clauses.append("sync_status = ?")
        params.append(sync_status)
    source = _clean(filters.source)
    if source is not None:
        clauses.append("source = ?")
        params.append(source)
    query = _clean(filters.query)
    if query is not None:
        pattern = f"%{_escape_like(query.lower())}%"
        clauses.append(
            "("
            "LOWER(slug) LIKE ? ESCAPE '\\' OR "
            "LOWER(name) LIKE ? ESCAPE '\\' OR "
            "LOWER(install_path) LIKE ? ESCAPE '\\' OR "
            "LOWER(content_hash) LIKE ? ESCAPE '\\'"
            ")"
        )
        params.extend([pattern, pattern, pattern, pattern])
    if not clauses:
        return "", params
    return "WHERE " + " AND ".join(clauses), params


def _record_params(
    record: WorkspaceItemIndexRecord,
    *,
    created_at: datetime,
    updated_at: datetime,
) -> tuple[object, ...]:
    return (
        _clean_required(record.assistant_id, "assistant_id"),
        _normalize_kind(record.kind),
        _clean_required(record.slug, "slug"),
        record.name,
        record.version,
        1 if record.enabled else 0,
        record.install_path,
        int(record.files_count),
        record.content_hash,
        record.source,
        record.sync_status,
        record.runtime_reload_status,
        record.runtime_reload_error,
        1 if record.runtime_effective else 0,
        _to_db_time(record.last_indexed_at),
        _to_db_time(record.last_synced_at),
        record.last_error,
        1 if record.deleted else 0,
        _to_db_time(created_at),
        _to_db_time(updated_at),
    )


def _record_from_row(row: sqlite3.Row) -> WorkspaceItemIndexRecord:
    return WorkspaceItemIndexRecord(
        assistant_id=row["assistant_id"],
        kind=row["kind"],
        slug=row["slug"],
        name=row["name"],
        version=row["version"],
        enabled=bool(row["enabled"]),
        install_path=row["install_path"],
        files_count=int(row["files_count"] or 0),
        content_hash=row["content_hash"],
        source=row["source"],
        sync_status=row["sync_status"],
        runtime_reload_status=row["runtime_reload_status"],
        runtime_reload_error=row["runtime_reload_error"],
        runtime_effective=bool(row["runtime_effective"]),
        last_indexed_at=_parse_datetime(row["last_indexed_at"]),
        last_synced_at=_parse_datetime(row["last_synced_at"]),
        last_error=row["last_error"],
        deleted=bool(row["deleted"]),
        created_at=_parse_datetime(row["created_at"]) or utc_now(),
        updated_at=_parse_datetime(row["updated_at"]) or utc_now(),
    )


@dataclass(frozen=True)
class _WorkspaceFileAggregate:
    files_count: int
    last_indexed_at: datetime | None
    last_synced_at: datetime | None


class _SQLiteWorkspaceFileStoreLike(Protocol):
    def connect(self, *, read_only: bool = False) -> sqlite3.Connection: ...


def _with_workspace_file_aggregates(
    page: WorkspaceItemIndexPage,
    *,
    workspace_file_repository: object | None,
    workspace_file_store: object | None,
) -> WorkspaceItemIndexPage:
    workspace_store = _resolve_workspace_file_store(
        workspace_file_repository=workspace_file_repository,
        workspace_file_store=workspace_file_store,
    )
    if workspace_store is None or not page.records:
        return page

    aggregates = _load_workspace_file_aggregates(
        page.records,
        workspace_file_store=workspace_store,
    )
    if not aggregates:
        return page

    records = [
        replace(
            record,
            files_count=aggregate.files_count,
            last_indexed_at=aggregate.last_indexed_at,
            last_synced_at=aggregate.last_synced_at,
        )
        if (aggregate := aggregates.get((record.assistant_id, record.kind, record.slug)))
        is not None
        else record
        for record in page.records
    ]
    return WorkspaceItemIndexPage(
        records=records,
        pagination=page.pagination,
        last_indexed_at=page.last_indexed_at,
        sync_status_counts=page.sync_status_counts,
        stale_count=page.stale_count,
    )


def _resolve_workspace_file_store(
    *,
    workspace_file_repository: object | None,
    workspace_file_store: object | None,
) -> _SQLiteWorkspaceFileStoreLike | None:
    for candidate in (workspace_file_store, workspace_file_repository):
        if candidate is None:
            continue
        if callable(getattr(candidate, "connect", None)):
            return cast(_SQLiteWorkspaceFileStoreLike, candidate)
        store = getattr(candidate, "store", None)
        if store is not None and callable(getattr(store, "connect", None)):
            return cast(_SQLiteWorkspaceFileStoreLike, store)
    return None


def _load_workspace_file_aggregates(
    records: list[WorkspaceItemIndexRecord],
    *,
    workspace_file_store: _SQLiteWorkspaceFileStoreLike,
) -> dict[tuple[str, str, str], _WorkspaceFileAggregate]:
    aggregates: dict[tuple[str, str, str], _WorkspaceFileAggregate] = {}
    with workspace_file_store.connect(read_only=True) as conn:
        for record in records:
            prefix = f"{record.kind}/{record.slug}"
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS files_count,
                    MAX(updated_at) AS last_indexed_at,
                    MAX(last_synced_at) AS last_synced_at
                FROM workspace_files
                WHERE assistant_id = ?
                    AND bucket = ?
                    AND deleted = 0
                    AND (
                        relative_path = ?
                        OR relative_path LIKE ? ESCAPE '\\'
                    )
                """,
                (
                    record.assistant_id,
                    record.kind,
                    prefix,
                    f"{_escape_like(prefix)}/%",
                ),
            ).fetchone()
            aggregates[(record.assistant_id, record.kind, record.slug)] = (
                _WorkspaceFileAggregate(
                    files_count=int(row["files_count"] or 0),
                    last_indexed_at=_parse_datetime(row["last_indexed_at"]),
                    last_synced_at=_parse_datetime(row["last_synced_at"]),
            )
                )
    return aggregates


def _validate_pagination(filters: WorkspaceItemIndexFilters) -> None:
    if filters.page < 1:
        raise ValueError("page must be >= 1")
    if filters.page_size < 1:
        raise ValueError("page_size must be >= 1")
    if filters.page_size > MAX_PAGE_SIZE:
        raise ValueError("page_size must be <= 200")


def _normalize_assistant_ids(values: Iterable[str] | None) -> set[str] | None:
    if values is None:
        return None
    return {cleaned for value in values if (cleaned := _clean(value)) is not None}


def _normalize_kind(kind: WorkspaceItemKind | str) -> WorkspaceItemKind:
    cleaned = str(kind).strip()
    if cleaned not in {"skills", "plugins"}:
        raise ValueError("kind must be skills or plugins")
    return cleaned  # type: ignore[return-value]


def _clean_required(value: str, field_name: str) -> str:
    cleaned = str(value).strip()
    if not cleaned:
        raise ValueError(f"{field_name} is required")
    return cleaned


def _clean(value: object | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _to_db_time(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    raw = str(value).strip()
    return datetime.fromisoformat(raw) if raw else None


def _is_stale_sync_status(sync_status: str) -> bool:
    return sync_status not in CURRENT_ITEM_SYNC_STATUSES


def _count_stale(sync_status_counts: dict[str, int]) -> int:
    return sum(
        count
        for sync_status, count in sync_status_counts.items()
        if _is_stale_sync_status(sync_status)
    )


def _empty_page(filters: WorkspaceItemIndexFilters) -> WorkspaceItemIndexPage:
    return WorkspaceItemIndexPage(
        records=[],
        pagination=WorkspaceItemIndexPagination(
            page=filters.page,
            page_size=filters.page_size,
            total=0,
            has_next=False,
        ),
        last_indexed_at=None,
        sync_status_counts={},
        stale_count=0,
    )


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS assistant_workspace_items (
    assistant_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    slug TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    install_path TEXT NOT NULL DEFAULT '',
    files_count INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'market',
    sync_status TEXT NOT NULL DEFAULT 'synced',
    runtime_reload_status TEXT NOT NULL DEFAULT 'skipped',
    runtime_reload_error TEXT NOT NULL DEFAULT '',
    runtime_effective INTEGER NOT NULL DEFAULT 1,
    last_indexed_at TEXT,
    last_synced_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    deleted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (assistant_id, kind, slug)
);

CREATE INDEX IF NOT EXISTS idx_assistant_workspace_items_assistant_kind_deleted_updated
ON assistant_workspace_items(assistant_id, kind, deleted, updated_at);

CREATE INDEX IF NOT EXISTS idx_assistant_workspace_items_deleted_updated
ON assistant_workspace_items(deleted, updated_at);

CREATE INDEX IF NOT EXISTS idx_assistant_workspace_items_sync_status
ON assistant_workspace_items(sync_status);

CREATE INDEX IF NOT EXISTS idx_assistant_workspace_items_content_hash
ON assistant_workspace_items(content_hash);
"""
