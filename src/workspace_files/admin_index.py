from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from src.workspace_files.models import TargetBucket, WorkspaceFile
from src.workspace_files.repository import WorkspaceFileRepository, build_workspace_file_repository
from src.workspace_files.sqlite_repository import (
    SQLiteWorkspaceFileRepository,
    _workspace_file_from_row,
)

MAX_PAGE_SIZE = 200
CURRENT_SYNC_STATUSES = {"synced", "synced_to_workspace", "uploaded", "stored"}
SQLITE_FOLDER_SUMMARY_BATCH_SIZE = 500


@dataclass(frozen=True)
class WorkspaceFileIndexFilters:
    assistant_ids: Iterable[str] | None = None
    assistant_id: str | None = None
    bucket: TargetBucket | None = None
    source_type: str | None = None
    sync_status: str | None = None
    updated_after: datetime | None = None
    query: str | None = None
    page: int = 1
    page_size: int = 50
    view: str = "flat"
    parent: str = ""


@dataclass(frozen=True)
class WorkspaceFileIndexRecord:
    file_id: str
    assistant_id: str
    bucket: str
    filename: str
    relative_path: str
    source_type: str
    origin: str
    sync_status: str
    storage_status: str
    size_bytes: int
    created_at: datetime
    updated_at: datetime
    last_synced_at: datetime | None
    workspace_last_modified_at: datetime | None
    last_error: str
    stale: bool
    content_type: str = ""
    entry_type: str = "file"
    child_count: int = 0

    @classmethod
    def from_workspace_file(cls, item: WorkspaceFile) -> WorkspaceFileIndexRecord:
        return cls(
            file_id=item.file_id,
            assistant_id=item.assistant_id,
            bucket=item.bucket,
            filename=item.filename,
            relative_path=item.relative_path,
            source_type=item.source_type,
            origin=item.origin,
            sync_status=item.sync_status,
            storage_status=item.storage_status,
            size_bytes=item.size_bytes,
            created_at=item.created_at,
            updated_at=item.updated_at,
            last_synced_at=item.last_synced_at,
            workspace_last_modified_at=item.workspace_last_modified_at,
            last_error=item.last_error,
            stale=_is_stale_sync_status(item.sync_status),
            content_type=item.content_type,
        )


@dataclass(frozen=True)
class WorkspaceFileIndexPagination:
    page: int
    page_size: int
    total: int
    has_next: bool


@dataclass(frozen=True)
class WorkspaceFileIndexPage:
    records: list[WorkspaceFileIndexRecord]
    pagination: WorkspaceFileIndexPagination
    last_indexed_at: datetime | None
    sync_status_counts: dict[str, int] = field(default_factory=dict)
    stale_count: int = 0

    @property
    def total(self) -> int:
        return self.pagination.total

    @property
    def has_next(self) -> bool:
        return self.pagination.has_next


@dataclass(frozen=True)
class WorkspaceFolderIndexSummary:
    assistant_id: str
    buckets: list[str]
    files_count: int
    root_items_count: int
    last_indexed_at: datetime | None
    last_synced_at: datetime | None
    sync_status: str
    sync_error: str
    stale: bool


@dataclass(frozen=True)
class WorkspaceFolderIndexPage:
    summaries: list[WorkspaceFolderIndexSummary]
    last_indexed_at: datetime | None
    stale_count: int = 0
    source: str = "db"

    @property
    def total_files(self) -> int:
        return sum(item.files_count for item in self.summaries)

    @property
    def total_assistants(self) -> int:
        return len(self.summaries)


class _FallbackWorkspaceFileRepository(Protocol):
    async def alist_files(
        self,
        *,
        assistant_id: str | None = None,
        bucket: TargetBucket | None = None,
        include_deleted: bool = False,
        limit: int | None = None,
    ) -> list[WorkspaceFile]: ...


async def list_workspace_file_index_page(
    repository: WorkspaceFileRepository | _FallbackWorkspaceFileRepository,
    filters: WorkspaceFileIndexFilters | None = None,
) -> WorkspaceFileIndexPage:
    resolved = filters or WorkspaceFileIndexFilters()
    _validate_pagination(resolved)
    _validate_index_view(resolved)
    visible_assistant_ids = _normalize_assistant_ids(resolved.assistant_ids)
    assistant_id = _clean(resolved.assistant_id)
    if visible_assistant_ids is not None and assistant_id is not None:
        if assistant_id not in visible_assistant_ids:
            return _empty_page(resolved)

    if isinstance(repository, SQLiteWorkspaceFileRepository):
        return await _list_sqlite_index_page(
            repository,
            resolved,
            visible_assistant_ids=visible_assistant_ids,
            assistant_id=assistant_id,
        )
    return await _list_fallback_index_page(
        repository,
        resolved,
        visible_assistant_ids=visible_assistant_ids,
        assistant_id=assistant_id,
    )


async def list_workspace_files_page(
    *,
    assistant_ids: Iterable[str] | None = None,
    assistant_id: str | None = None,
    bucket: str | TargetBucket | None = None,
    source_type: str | None = None,
    sync_status: str | None = None,
    updated_after: str | datetime | None = None,
    query: str | None = None,
    page: int = 1,
    page_size: int = 50,
    view: str = "flat",
    parent: str | None = None,
    repository: WorkspaceFileRepository | _FallbackWorkspaceFileRepository | None = None,
    **_ignored: object,
) -> WorkspaceFileIndexPage:
    """Compatibility entrypoint for admin routes.

    The route layer intentionally depends on this narrow DB-only query surface
    instead of WorkspaceFileService, which may hydrate legacy object storage.
    """

    filters = WorkspaceFileIndexFilters(
        assistant_ids=assistant_ids,
        assistant_id=assistant_id,
        bucket=_normalize_bucket(bucket),
        source_type=source_type,
        sync_status=sync_status,
        updated_after=_normalize_updated_after(updated_after),
        query=query,
        page=page,
        page_size=page_size,
        view=view,
        parent=parent or "",
    )
    return await list_workspace_file_index_page(
        repository or build_workspace_file_repository(),
        filters,
    )


async def list_workspace_folder_summaries(
    repository: WorkspaceFileRepository | _FallbackWorkspaceFileRepository | None = None,
    *,
    assistant_ids: Iterable[str] | None = None,
) -> WorkspaceFolderIndexPage:
    """Return root-level assistant workspace summaries without returning files."""

    resolved_repository = repository or build_workspace_file_repository()
    visible_assistant_ids = _normalize_assistant_ids(assistant_ids) or set()
    if isinstance(resolved_repository, SQLiteWorkspaceFileRepository):
        return await asyncio.to_thread(
            _list_sqlite_folder_summaries_sync,
            resolved_repository,
            visible_assistant_ids,
        )
    return await _list_fallback_folder_summaries(
        resolved_repository,
        visible_assistant_ids,
    )


async def _list_sqlite_index_page(
    repository: SQLiteWorkspaceFileRepository,
    filters: WorkspaceFileIndexFilters,
    *,
    visible_assistant_ids: set[str] | None,
    assistant_id: str | None,
) -> WorkspaceFileIndexPage:
    if visible_assistant_ids == set():
        return _empty_page(filters)
    return await asyncio.to_thread(
        _list_sqlite_index_page_sync,
        repository,
        filters,
        visible_assistant_ids,
        assistant_id,
    )


def _list_sqlite_index_page_sync(
    repository: SQLiteWorkspaceFileRepository,
    filters: WorkspaceFileIndexFilters,
    visible_assistant_ids: set[str] | None,
    assistant_id: str | None,
) -> WorkspaceFileIndexPage:
    if _is_tree_view(filters):
        return _list_sqlite_tree_index_page_sync(
            repository,
            filters,
            visible_assistant_ids,
            assistant_id,
        )
    where_sql, params = _sqlite_where(filters, visible_assistant_ids, assistant_id)
    offset = (filters.page - 1) * filters.page_size
    rows_sql = f"""
        SELECT * FROM workspace_files
        {where_sql}
        ORDER BY updated_at DESC, file_id DESC
        LIMIT ? OFFSET ?
    """
    count_sql = f"SELECT COUNT(*) FROM workspace_files {where_sql}"
    max_sql = f"SELECT MAX(updated_at) FROM workspace_files {where_sql}"
    status_sql = f"""
        SELECT sync_status, COUNT(*) AS count
        FROM workspace_files
        {where_sql}
        GROUP BY sync_status
    """
    with repository.store.connect(read_only=True) as conn:
        total = int(conn.execute(count_sql, params).fetchone()[0])
        last_indexed_raw = conn.execute(max_sql, params).fetchone()[0]
        status_rows = conn.execute(status_sql, params).fetchall()
        rows = conn.execute(rows_sql, [*params, filters.page_size, offset]).fetchall()
    records = [
        WorkspaceFileIndexRecord.from_workspace_file(_workspace_file_from_row(row)) for row in rows
    ]
    sync_status_counts = {str(row["sync_status"] or ""): int(row["count"]) for row in status_rows}
    return WorkspaceFileIndexPage(
        records=records,
        pagination=WorkspaceFileIndexPagination(
            page=filters.page,
            page_size=filters.page_size,
            total=total,
            has_next=offset + len(records) < total,
        ),
        last_indexed_at=_parse_datetime(last_indexed_raw),
        sync_status_counts=sync_status_counts,
        stale_count=_count_stale(sync_status_counts),
    )


def _list_sqlite_tree_index_page_sync(
    repository: SQLiteWorkspaceFileRepository,
    filters: WorkspaceFileIndexFilters,
    visible_assistant_ids: set[str] | None,
    assistant_id: str | None,
) -> WorkspaceFileIndexPage:
    if assistant_id is None:
        return _empty_page(filters)
    where_sql, params = _sqlite_where(filters, visible_assistant_ids, assistant_id)
    rows_sql = f"""
        SELECT * FROM workspace_files
        {where_sql}
        ORDER BY relative_path ASC, file_id ASC
    """
    status_sql = f"""
        SELECT sync_status, COUNT(*) AS count
        FROM workspace_files
        {where_sql}
        GROUP BY sync_status
    """
    max_sql = f"SELECT MAX(updated_at) FROM workspace_files {where_sql}"
    with repository.store.connect(read_only=True) as conn:
        rows = conn.execute(rows_sql, params).fetchall()
        status_rows = conn.execute(status_sql, params).fetchall()
        last_indexed_raw = conn.execute(max_sql, params).fetchone()[0]
    records = [
        WorkspaceFileIndexRecord.from_workspace_file(_workspace_file_from_row(row)) for row in rows
    ]
    sync_status_counts = {str(row["sync_status"] or ""): int(row["count"]) for row in status_rows}
    return _tree_index_page_from_records(
        records,
        filters,
        last_indexed_at=_parse_datetime(last_indexed_raw),
        sync_status_counts=sync_status_counts,
    )


def _list_sqlite_folder_summaries_sync(
    repository: SQLiteWorkspaceFileRepository,
    assistant_ids: set[str],
) -> WorkspaceFolderIndexPage:
    ordered_ids = sorted(assistant_ids)
    if not ordered_ids:
        return WorkspaceFolderIndexPage(summaries=[], last_indexed_at=None)
    with repository.store.connect(read_only=True) as conn:
        rows = []
        for batch_ids in _chunks(ordered_ids, SQLITE_FOLDER_SUMMARY_BATCH_SIZE):
            placeholders = ", ".join("?" for _ in batch_ids)
            status_placeholders = ", ".join("?" for _ in CURRENT_SYNC_STATUSES)
            sql = f"""
                SELECT assistant_id,
                       COUNT(*) AS files_count,
                       GROUP_CONCAT(DISTINCT bucket) AS buckets,
                       MAX(updated_at) AS last_indexed_at,
                       MAX(last_synced_at) AS last_synced_at,
                       SUM(
                           CASE WHEN sync_status NOT IN ({status_placeholders})
                           THEN 1 ELSE 0 END
                       ) AS stale_count,
                       MAX(
                           CASE WHEN COALESCE(last_error, '') != ''
                           THEN last_error ELSE '' END
                       ) AS sync_error
                FROM workspace_files
                WHERE deleted = 0 AND assistant_id IN ({placeholders})
                GROUP BY assistant_id
            """
            params: list[object] = [*sorted(CURRENT_SYNC_STATUSES), *batch_ids]
            rows.extend(conn.execute(sql, params).fetchall())
    by_assistant = {str(row["assistant_id"]): row for row in rows}
    summaries: list[WorkspaceFolderIndexSummary] = []
    last_indexed_values: list[datetime] = []
    stale_total = 0
    for assistant_id in ordered_ids:
        row = by_assistant.get(assistant_id)
        if row is None:
            summaries.append(
                WorkspaceFolderIndexSummary(
                    assistant_id=assistant_id,
                    buckets=[],
                    files_count=0,
                    root_items_count=0,
                    last_indexed_at=None,
                    last_synced_at=None,
                    sync_status="empty",
                    sync_error="",
                    stale=False,
            )
                )
            continue
        buckets = sorted(
            bucket
            for bucket in str(row["buckets"] or "").split(",")
            if bucket
        )
        stale_count = int(row["stale_count"] or 0)
        stale_total += stale_count
        last_indexed_at = _parse_datetime(row["last_indexed_at"])
        if last_indexed_at is not None:
            last_indexed_values.append(last_indexed_at)
        summaries.append(
            WorkspaceFolderIndexSummary(
                assistant_id=assistant_id,
                buckets=buckets,
                files_count=int(row["files_count"] or 0),
                root_items_count=len(buckets),
                last_indexed_at=last_indexed_at,
                last_synced_at=_parse_datetime(row["last_synced_at"]),
                sync_status="unknown" if stale_count else "synced",
                sync_error=str(row["sync_error"] or ""),
                stale=stale_count > 0,
        )
            )
    return WorkspaceFolderIndexPage(
        summaries=summaries,
        last_indexed_at=max(last_indexed_values) if last_indexed_values else None,
        stale_count=stale_total,
    )


async def _list_fallback_folder_summaries(
    repository: _FallbackWorkspaceFileRepository,
    assistant_ids: set[str],
) -> WorkspaceFolderIndexPage:
    summaries: list[WorkspaceFolderIndexSummary] = []
    last_indexed_values: list[datetime] = []
    stale_total = 0
    for assistant_id in sorted(assistant_ids):
        items = await repository.alist_files(assistant_id=assistant_id, include_deleted=False)
        buckets = sorted({str(item.bucket) for item in items if item.bucket})
        last_indexed_at = max(
            (item.updated_at for item in items if item.updated_at),
            default=None,
        )
        if last_indexed_at is not None:
            last_indexed_values.append(last_indexed_at)
        stale_count = sum(1 for item in items if _is_stale_sync_status(item.sync_status))
        stale_total += stale_count
        summaries.append(
            WorkspaceFolderIndexSummary(
                assistant_id=assistant_id,
                buckets=buckets,
                files_count=len(items),
                root_items_count=len(buckets),
                last_indexed_at=last_indexed_at,
                last_synced_at=max(
                    (item.last_synced_at for item in items if item.last_synced_at),
                    default=None,
                ),
                sync_status="empty" if not items else ("unknown" if stale_count else "synced"),
                sync_error=next((item.last_error for item in items if item.last_error), ""),
                stale=stale_count > 0,
        )
            )
    return WorkspaceFolderIndexPage(
        summaries=summaries,
        last_indexed_at=max(last_indexed_values) if last_indexed_values else None,
        stale_count=stale_total,
    )


def _sqlite_where(
    filters: WorkspaceFileIndexFilters,
    visible_assistant_ids: set[str] | None,
    assistant_id: str | None,
) -> tuple[str, list[object]]:
    clauses = ["deleted = 0"]
    params: list[object] = []
    if assistant_id is not None:
        clauses.append("assistant_id = ?")
        params.append(assistant_id)
    elif visible_assistant_ids is not None:
        placeholders = ", ".join("?" for _ in visible_assistant_ids)
        clauses.append(f"assistant_id IN ({placeholders})")
        params.extend(sorted(visible_assistant_ids))
    if filters.bucket is not None:
        clauses.append("bucket = ?")
        params.append(filters.bucket)
    source_type = _clean(filters.source_type)
    if source_type is not None:
        clauses.append("source_type = ?")
        params.append(source_type)
    sync_status = _clean(filters.sync_status)
    if sync_status is not None:
        clauses.append("sync_status = ?")
        params.append(sync_status)
    if filters.updated_after is not None:
        clauses.append("updated_at > ?")
        params.append(filters.updated_after.isoformat())
    query = _clean(filters.query)
    if query is not None:
        pattern = f"%{_escape_like(query.lower())}%"
        clauses.append(
            "("
            "LOWER(filename) LIKE ? ESCAPE '\\' OR "
            "LOWER(relative_path) LIKE ? ESCAPE '\\' OR "
            "LOWER(file_id) LIKE ? ESCAPE '\\' OR "
            "LOWER(source_ref) LIKE ? ESCAPE '\\'"
            ")"
        )
        params.extend([pattern, pattern, pattern, pattern])
    return "WHERE " + " AND ".join(clauses), params


async def _list_fallback_index_page(
    repository: _FallbackWorkspaceFileRepository,
    filters: WorkspaceFileIndexFilters,
    *,
    visible_assistant_ids: set[str] | None,
    assistant_id: str | None,
) -> WorkspaceFileIndexPage:
    if visible_assistant_ids == set():
        return _empty_page(filters)
    items = await _fallback_load_files(
        repository,
        assistant_id=assistant_id,
        assistant_ids=visible_assistant_ids,
        bucket=filters.bucket,
    )
    filtered = [
        item
        for item in items
        if not item.deleted and _matches_filters(item, filters, visible_assistant_ids, assistant_id)
    ]
    if _is_tree_view(filters):
        if assistant_id is None:
            return _empty_page(filters)
        records = [WorkspaceFileIndexRecord.from_workspace_file(item) for item in filtered]
        sync_status_counts: dict[str, int] = {}
        for item in filtered:
            sync_status_counts[item.sync_status] = sync_status_counts.get(item.sync_status, 0) + 1
        return _tree_index_page_from_records(
            records,
            filters,
            last_indexed_at=max((item.updated_at for item in filtered), default=None),
            sync_status_counts=sync_status_counts,
        )
    filtered.sort(key=lambda item: (item.updated_at, item.file_id), reverse=True)
    offset = (filters.page - 1) * filters.page_size
    page_items = filtered[offset : offset + filters.page_size]
    records = [WorkspaceFileIndexRecord.from_workspace_file(item) for item in page_items]
    sync_status_counts: dict[str, int] = {}
    for item in filtered:
        sync_status_counts[item.sync_status] = sync_status_counts.get(item.sync_status, 0) + 1
    last_indexed_at = max((item.updated_at for item in filtered), default=None)
    return WorkspaceFileIndexPage(
        records=records,
        pagination=WorkspaceFileIndexPagination(
            page=filters.page,
            page_size=filters.page_size,
            total=len(filtered),
            has_next=offset + len(records) < len(filtered),
        ),
        last_indexed_at=last_indexed_at,
        sync_status_counts=sync_status_counts,
        stale_count=_count_stale(sync_status_counts),
    )


async def _fallback_load_files(
    repository: _FallbackWorkspaceFileRepository,
    *,
    assistant_id: str | None,
    assistant_ids: set[str] | None,
    bucket: TargetBucket | None,
) -> list[WorkspaceFile]:
    if assistant_id is not None:
        return await repository.alist_files(
            assistant_id=assistant_id,
            bucket=bucket,
            include_deleted=False,
        )
    if assistant_ids is not None:
        records: list[WorkspaceFile] = []
        for visible_assistant_id in sorted(assistant_ids):
            records.extend(
                await repository.alist_files(
                    assistant_id=visible_assistant_id,
                    bucket=bucket,
                    include_deleted=False,
            )
                )
        return records
    return await repository.alist_files(
        bucket=bucket,
        include_deleted=False,
    )


def _matches_filters(
    item: WorkspaceFile,
    filters: WorkspaceFileIndexFilters,
    visible_assistant_ids: set[str] | None,
    assistant_id: str | None,
) -> bool:
    if assistant_id is not None and item.assistant_id != assistant_id:
        return False
    if visible_assistant_ids is not None and item.assistant_id not in visible_assistant_ids:
        return False
    if filters.bucket is not None and item.bucket != filters.bucket:
        return False
    source_type = _clean(filters.source_type)
    if source_type is not None and item.source_type != source_type:
        return False
    sync_status = _clean(filters.sync_status)
    if sync_status is not None and item.sync_status != sync_status:
        return False
    if filters.updated_after is not None and item.updated_at <= filters.updated_after:
        return False
    query = _clean(filters.query)
    if query is not None:
        needle = query.lower()
        haystack = (
            item.filename.lower(),
            item.relative_path.lower(),
            item.file_id.lower(),
            item.source_ref.lower(),
        )
        return any(needle in value for value in haystack)
    return True


def _tree_index_page_from_records(
    records: list[WorkspaceFileIndexRecord],
    filters: WorkspaceFileIndexFilters,
    *,
    last_indexed_at: datetime | None,
    sync_status_counts: dict[str, int],
) -> WorkspaceFileIndexPage:
    parent_segments = _workspace_path_segments(filters.parent)
    folder_states: dict[str, dict[str, object]] = {}
    direct_files: list[WorkspaceFileIndexRecord] = []

    for record in records:
        segments = _workspace_path_segments(_record_relative_path(record))
        if not _matches_parent_segments(segments, parent_segments):
            continue
        remaining = segments[len(parent_segments) :]
        if not remaining:
            continue
        if len(remaining) > 1 or _is_directory_record(record):
            folder_path = "/".join([*parent_segments, remaining[0]])
            state = folder_states.get(folder_path)
            if state is None:
                state = {
                    "assistant_id": record.assistant_id,
                    "bucket": _bucket_from_segments(segments, record.bucket),
                    "filename": remaining[0],
                    "relative_path": folder_path,
                    "created_at": record.created_at,
                    "updated_at": record.updated_at,
                    "last_synced_at": record.last_synced_at,
                    "workspace_last_modified_at": record.workspace_last_modified_at,
                    "last_error": record.last_error,
                    "stale": record.stale,
                    "child_names": set(),
                    "child_count_hint": record.child_count,
                }
                folder_states[folder_path] = state
            _update_folder_state(state, record, remaining)
            continue
        direct_files.append(record)

    folders = [_folder_record_from_state(state) for state in folder_states.values()]
    folders.sort(key=lambda item: (item.filename.lower(), item.relative_path.lower()))
    direct_files.sort(
        key=lambda item: (
            item.filename.lower(),
            item.relative_path.lower(),
            item.file_id.lower(),
    )
        )
    tree_records = [*folders, *direct_files]
    offset = (filters.page - 1) * filters.page_size
    page_records = tree_records[offset : offset + filters.page_size]
    return WorkspaceFileIndexPage(
        records=page_records,
        pagination=WorkspaceFileIndexPagination(
            page=filters.page,
            page_size=filters.page_size,
            total=len(tree_records),
            has_next=offset + len(page_records) < len(tree_records),
        ),
        last_indexed_at=last_indexed_at,
        sync_status_counts=sync_status_counts,
        stale_count=_count_stale(sync_status_counts),
    )


def _update_folder_state(
    state: dict[str, object],
    record: WorkspaceFileIndexRecord,
    remaining: list[str],
) -> None:
    child_names = state["child_names"]
    if isinstance(child_names, set) and len(remaining) > 1:
        child_names.add(remaining[1])
    if record.updated_at > state["updated_at"]:
        state["updated_at"] = record.updated_at
    if record.created_at < state["created_at"]:
        state["created_at"] = record.created_at
    state["last_synced_at"] = _max_optional_datetime(
        state.get("last_synced_at"),
        record.last_synced_at,
    )
    state["workspace_last_modified_at"] = _max_optional_datetime(
        state.get("workspace_last_modified_at"),
        record.workspace_last_modified_at,
    )
    if record.last_error and not state.get("last_error"):
        state["last_error"] = record.last_error
    state["stale"] = bool(state.get("stale")) or record.stale
    state["child_count_hint"] = max(int(state.get("child_count_hint") or 0), record.child_count)


def _folder_record_from_state(state: dict[str, object]) -> WorkspaceFileIndexRecord:
    child_names = state.get("child_names")
    child_count = len(child_names) if isinstance(child_names, set) else 0
    child_count = max(child_count, int(state.get("child_count_hint") or 0))
    return WorkspaceFileIndexRecord(
        file_id=f"folder:{state['assistant_id']}:{state['relative_path']}",
        assistant_id=str(state["assistant_id"]),
        bucket=str(state["bucket"]),
        filename=str(state["filename"]),
        relative_path=str(state["relative_path"]),
        source_type="workspace_directory",
        origin="workspace_directory",
        sync_status="synced",
        storage_status="synced",
        size_bytes=0,
        created_at=state["created_at"],
        updated_at=state["updated_at"],
        last_synced_at=state.get("last_synced_at"),
        workspace_last_modified_at=state.get("workspace_last_modified_at"),
        last_error=str(state.get("last_error") or ""),
        stale=bool(state.get("stale")),
        content_type="application/x-directory",
        entry_type="folder",
        child_count=child_count,
    )


def _record_relative_path(record: WorkspaceFileIndexRecord) -> str:
    relative_path = _normalize_workspace_path(record.relative_path)
    bucket = _normalize_workspace_path(record.bucket)
    filename = _normalize_workspace_path(record.filename)
    if bucket and relative_path and relative_path != bucket and not relative_path.startswith(
        f"{bucket}/"
    ):
        return f"{bucket}/{relative_path}"
    if relative_path:
        return relative_path
    return f"{bucket}/{filename}" if bucket and filename else filename or bucket


def _workspace_path_segments(value: str | None) -> list[str]:
    normalized = _normalize_workspace_path(value)
    if not normalized:
        return []
    segments = normalized.split("/")
    if any(segment in {".", ".."} for segment in segments):
        raise ValueError("parent must not contain . or .. segments")
    return segments


def _normalize_workspace_path(value: str | None) -> str:
    cleaned = _clean(str(value)) if value is not None else None
    if cleaned is None:
        return ""
    cleaned = cleaned.replace("\\", "/")
    while "//" in cleaned:
        cleaned = cleaned.replace("//", "/")
    return cleaned.strip("/")


def _matches_parent_segments(segments: list[str], parent_segments: list[str]) -> bool:
    return (
        len(segments) > len(parent_segments)
        and segments[: len(parent_segments)] == parent_segments
    )


def _is_directory_record(record: WorkspaceFileIndexRecord) -> bool:
    return (
        record.entry_type == "folder"
        or record.source_type == "workspace_directory"
        or record.source_type == "cos_directory"
        or record.content_type == "application/x-directory"
    )


def _bucket_from_segments(segments: list[str], fallback: str) -> str:
    if segments and segments[0] in {"temp", "persistent", "skills", "plugins"}:
        return segments[0]
    return fallback


def _max_optional_datetime(left: object, right: datetime | None) -> datetime | None:
    left_dt = left if isinstance(left, datetime) else None
    if left_dt is None:
        return right
    if right is None:
        return left_dt
    return max(left_dt, right)


def _validate_index_view(filters: WorkspaceFileIndexFilters) -> None:
    if (_clean(filters.view) or "flat") not in {"flat", "tree"}:
        raise ValueError("view must be flat or tree")
    if _is_tree_view(filters):
        _workspace_path_segments(filters.parent)


def _is_tree_view(filters: WorkspaceFileIndexFilters) -> bool:
    return (_clean(filters.view) or "flat") == "tree"


def _validate_pagination(filters: WorkspaceFileIndexFilters) -> None:
    if filters.page < 1:
        raise ValueError("page must be >= 1")
    if filters.page_size < 1:
        raise ValueError("page_size must be >= 1")
    if filters.page_size > MAX_PAGE_SIZE:
        raise ValueError("page_size must be <= 200")


def _normalize_bucket(value: str | TargetBucket | None) -> TargetBucket | None:
    cleaned = _clean(value)
    if cleaned is None:
        return None
    if cleaned not in {"temp", "persistent", "skills", "plugins"}:
        raise ValueError("bucket must be temp, persistent, skills, or plugins")
    return cleaned  # type: ignore[return-value]


def _normalize_updated_after(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    cleaned = value.strip()
    return datetime.fromisoformat(cleaned) if cleaned else None


def _empty_page(filters: WorkspaceFileIndexFilters) -> WorkspaceFileIndexPage:
    return WorkspaceFileIndexPage(
        records=[],
        pagination=WorkspaceFileIndexPagination(
            page=filters.page,
            page_size=filters.page_size,
            total=0,
            has_next=False,
        ),
        last_indexed_at=None,
        sync_status_counts={},
        stale_count=0,
    )


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _normalize_assistant_ids(values: Iterable[str] | None) -> set[str] | None:
    if values is None:
        return None
    return {cleaned for value in values if (cleaned := _clean(value)) is not None}


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _is_stale_sync_status(sync_status: str) -> bool:
    return sync_status not in CURRENT_SYNC_STATUSES


def _count_stale(sync_status_counts: dict[str, int]) -> int:
    return sum(
        count
        for sync_status, count in sync_status_counts.items()
        if _is_stale_sync_status(sync_status)
    )
