"""Copy workspace_files and workspace_file_events from SQLite(-on-SFS) WAL to in-cluster MySQL.

战役一 P4 切换前置:把 workspace 文件元数据从 SFS 上的 SQLite 正向迁回 in-cluster MySQL。

特性:
- 幂等可重跑:默认只补缺失 file_id / event_id;若目标已有不同内容则拒绝覆盖。
- 全量含软删:alist_files(include_deleted=True, limit=None),不丢 deleted 行。
- 迁移后源/目标 COUNT 对账(workspace_files 含/不含 deleted 两口径 + events 总数)。
- --dry-run 只读对账、不写。

反方向脚本见 scripts/migrate_workspace_files_to_sqlite.py。
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
from collections import defaultdict
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from src.shared.database import dispose_database_engines, get_session_factory
from src.shared.repositories import (
    _apply_workspace_file_event_to_row,
    _apply_workspace_file_to_row,
    _workspace_file_from_row,
)
from src.shared.repositories import (
    _workspace_file_event_from_row as _mysql_workspace_file_event_from_row,
)
from src.workspace_files.database_models import (
    WorkspaceFileEventRow,
    WorkspaceFileRow,
    workspace_file_active_path_key,
)
from src.workspace_files.models import WorkspaceFile, WorkspaceFileEvent
from src.workspace_files.sqlite_repository import (
    SQLiteWorkspaceFileRepository,
    SQLiteWorkspaceFileStore,
    _workspace_file_event_from_row,
)

ALLOWED_EXISTING_FILE_DIFF_FIELDS = frozenset({"created_at", "updated_at", "sync_status"})
ALLOWED_EXISTING_EVENT_DIFF_FIELDS = frozenset({"created_at"})


def _read_sqlite_events(store: SQLiteWorkspaceFileStore) -> list[WorkspaceFileEvent]:
    with store.connect(read_only=True) as conn:
        rows: list[sqlite3.Row] = conn.execute(
            "SELECT * FROM workspace_file_events ORDER BY created_at ASC, event_id ASC"
        ).fetchall()
    return [_workspace_file_event_from_row(row) for row in rows]


def _normalized_model_dump(model: WorkspaceFile | WorkspaceFileEvent) -> dict:
    return model.model_dump(mode="json")


def _diff_models(
    source: WorkspaceFile | WorkspaceFileEvent,
    target: WorkspaceFile | WorkspaceFileEvent,
    *,
    ignored_fields: frozenset[str] = frozenset(),
) -> list[str]:
    source_dump = _normalized_model_dump(source)
    target_dump = _normalized_model_dump(target)
    return sorted(
        field
        for field, source_value in source_dump.items()
        if field not in ignored_fields and target_dump.get(field) != source_value
    )


def _find_normalized_active_path_conflicts(
    files: list[WorkspaceFile],
) -> dict[str, list[WorkspaceFile]]:
    by_key: dict[str, list[WorkspaceFile]] = defaultdict(list)
    for record in files:
        active_path_key = workspace_file_active_path_key(
            record.assistant_id,
            record.bucket,
            record.filename,
            deleted=record.deleted,
        )
        if active_path_key is not None:
            by_key[active_path_key].append(record)
    return {key: records for key, records in by_key.items() if len(records) > 1}


def _format_active_path_conflicts(
    conflicts: dict[str, list[WorkspaceFile]],
    *,
    max_groups: int = 5,
    max_records_per_group: int = 3,
) -> str:
    groups: list[str] = []
    for index, records in enumerate(conflicts.values()):
        if index >= max_groups:
            break
        examples = ", ".join(
            f"{record.file_id}:{record.assistant_id}/{record.bucket}/{record.filename!r}"
            for record in records[:max_records_per_group]
        )
        groups.append(f"[{examples}]")
    suffix = "" if len(conflicts) <= max_groups else f"; +{len(conflicts) - max_groups} more"
    return "; ".join(groups) + suffix


def _reject_normalized_active_path_conflicts(files: list[WorkspaceFile]) -> None:
    conflicts = _find_normalized_active_path_conflicts(files)
    if conflicts:
        raise ValueError(
            "normalized active path conflict in source SQLite; "
            "resolve duplicate active assistant/bucket/path rows before migration: "
            f"{_format_active_path_conflicts(conflicts)}"
        )


def _resolve_source_sqlite_path(sqlite_path: Path | None) -> Path | None:
    if sqlite_path is None:
        return None
    source_path = sqlite_path.expanduser()
    if not source_path.is_file():
        raise FileNotFoundError(f"source SQLite database does not exist: {source_path}")
    return source_path


def _build_source_store(sqlite_path: Path | None) -> SQLiteWorkspaceFileStore:
    store = SQLiteWorkspaceFileStore(
        _resolve_source_sqlite_path(sqlite_path),
        ensure_schema=False,
    )
    if not store.db_path.is_file():
        raise FileNotFoundError(f"source SQLite database does not exist: {store.db_path}")
    return store


async def _save_file_by_id_strict(
    session_factory: async_sessionmaker[AsyncSession],
    record: WorkspaceFile,
    *,
    overwrite_existing: bool = False,
) -> None:
    active_path_key = workspace_file_active_path_key(
        record.assistant_id,
        str(record.bucket),
        record.filename,
        deleted=record.deleted,
    )
    async with session_factory() as session:
        async with session.begin():
            row = await session.get(WorkspaceFileRow, record.file_id)
            if active_path_key is not None:
                conflict = await session.scalar(
                    select(WorkspaceFileRow)
                    .where(
                        WorkspaceFileRow.active_path_key == active_path_key,
                        WorkspaceFileRow.deleted.is_(False),
                        WorkspaceFileRow.file_id != record.file_id,
                    )
                    .limit(1)
                )
                if conflict is not None:
                    raise ValueError(
                        "target active path conflict in MySQL; refusing to attach "
                        f"source file_id={record.file_id} to existing "
                        f"file_id={conflict.file_id} "
                        f"({record.assistant_id}/{record.bucket}/{record.filename!r})"
                    )
            if row is not None and not overwrite_existing:
                diffs = _diff_models(
                    record,
                    _workspace_file_from_row(row),
                    ignored_fields=ALLOWED_EXISTING_FILE_DIFF_FIELDS,
                )
                if diffs:
                    raise ValueError(
                        "target file_id conflict in MySQL; refusing to overwrite "
                        f"file_id={record.file_id} ({record.assistant_id}/"
                        f"{record.bucket}/{record.filename!r}); differing_fields="
                        f"{','.join(diffs[:12])}"
                    )
                return
            if row is None:
                row = WorkspaceFileRow(file_id=record.file_id)
                session.add(row)
            _apply_workspace_file_to_row(row, record)


async def _append_event_by_id_strict(
    session_factory: async_sessionmaker[AsyncSession],
    event: WorkspaceFileEvent,
    *,
    overwrite_existing: bool = False,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            row = await session.get(WorkspaceFileEventRow, event.event_id)
            if row is not None and not overwrite_existing:
                diffs = _diff_models(
                    event,
                    _mysql_workspace_file_event_from_row(row),
                    ignored_fields=ALLOWED_EXISTING_EVENT_DIFF_FIELDS,
                )
                if diffs:
                    raise ValueError(
                        "target event_id conflict in MySQL; refusing to overwrite "
                        f"event_id={event.event_id} file_id={event.file_id}; "
                        f"differing_fields={','.join(diffs[:12])}"
                    )
                return
            if row is None:
                row = WorkspaceFileEventRow(event_id=event.event_id)
                session.add(row)
            _apply_workspace_file_event_to_row(row, event)


async def _count_target(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int]:
    async with session_factory() as session:
        files = await session.scalar(select(func.count()).select_from(WorkspaceFileRow))
        events = await session.scalar(select(func.count()).select_from(WorkspaceFileEventRow))
    return int(files or 0), int(events or 0)


async def _count_target_ids(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    file_ids: set[str],
    event_ids: set[str],
) -> tuple[int, int]:
    async with session_factory() as session:
        files = await _count_existing_ids(
            session,
            WorkspaceFileRow,
            WorkspaceFileRow.file_id,
            file_ids,
        )
        events = await _count_existing_ids(
            session,
            WorkspaceFileEventRow,
            WorkspaceFileEventRow.event_id,
            event_ids,
        )
    return files, events


async def _count_existing_ids(
    session: AsyncSession,
    row_model: type,
    id_column: ColumnElement[str],
    ids: set[str],
) -> int:
    if not ids:
        return 0
    total = 0
    id_list = sorted(ids)
    for start in range(0, len(id_list), 500):
        chunk = id_list[start : start + 500]
        count = await session.scalar(
            select(func.count()).select_from(row_model).where(id_column.in_(chunk))
        )
        total += int(count or 0)
    return total


async def migrate(
    *,
    sqlite_path: Path | None = None,
    target_session_factory: async_sessionmaker[AsyncSession] | None = None,
    dry_run: bool = False,
    batch_log: int = 500,
    overwrite_existing: bool = False,
) -> dict[str, int | bool]:
    store = _build_source_store(sqlite_path)
    print(f"source sqlite: {store.db_path}", flush=True)

    files = await SQLiteWorkspaceFileRepository(store).alist_files(
        include_deleted=True, limit=None
    )
    events = await asyncio.to_thread(_read_sqlite_events, store)
    active = sum(1 for record in files if not record.deleted)
    deleted = len(files) - active
    print(
        f"source: files={len(files)} (active={active}, deleted={deleted}), events={len(events)}",
        flush=True,
    )
    _reject_normalized_active_path_conflicts(files)

    session_factory = target_session_factory or get_session_factory()
    target_files_before, target_events_before = await _count_target(session_factory)

    if dry_run:
        print(
            "[dry-run] target before: "
            f"files={target_files_before}, events={target_events_before}; "
            f"would migrate files={len(files)}, events={len(events)}",
            flush=True,
        )
        return {
            "dry_run": True,
            "source_files": len(files),
            "source_active": active,
            "source_deleted": deleted,
            "source_events": len(events),
            "target_files_before": target_files_before,
            "target_events_before": target_events_before,
        }

    print("writing workspace_files to MySQL...", flush=True)
    for index, record in enumerate(files, start=1):
        await _save_file_by_id_strict(
            session_factory,
            record,
            overwrite_existing=overwrite_existing,
        )
        if index % batch_log == 0:
            print(f"  files {index}/{len(files)}", flush=True)
    print("writing workspace_file_events to MySQL...", flush=True)
    for index, event in enumerate(events, start=1):
        await _append_event_by_id_strict(
            session_factory,
            event,
            overwrite_existing=overwrite_existing,
        )
        if index % batch_log == 0:
            print(f"  events {index}/{len(events)}", flush=True)

    target_files_after, target_events_after = await _count_target(session_factory)
    source_file_ids = {record.file_id for record in files}
    source_event_ids = {event.event_id for event in events}
    migrated_source_files, migrated_source_events = await _count_target_ids(
        session_factory,
        file_ids=source_file_ids,
        event_ids=source_event_ids,
    )
    files_ok = migrated_source_files == len(source_file_ids)
    events_ok = migrated_source_events == len(source_event_ids)
    print(
        "reconciliation: "
        f"source_files={len(files)} target_files={target_files_after} "
        f"source_file_ids_present={migrated_source_files} ok={files_ok} | "
        f"source_events={len(events)} target_events={target_events_after} "
        f"source_event_ids_present={migrated_source_events} ok={events_ok}",
        flush=True,
    )
    return {
        "dry_run": False,
        "source_files": len(files),
        "source_active": active,
        "source_deleted": deleted,
        "source_events": len(events),
        "target_files": target_files_after,
        "target_events": target_events_after,
        "source_file_ids_present": migrated_source_files,
        "source_event_ids_present": migrated_source_events,
        "files_ok": files_ok,
        "events_ok": events_ok,
    }


async def _run(args: argparse.Namespace) -> dict[str, int | bool]:
    try:
        return await migrate(
            sqlite_path=args.sqlite_path,
            dry_run=args.dry_run,
            overwrite_existing=args.overwrite_existing,
        )
    finally:
        await dispose_database_engines()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy workspace_files and workspace_file_events from SQLite WAL to MySQL.",
    )
    parser.add_argument(
        "--sqlite-path",
        type=Path,
        default=None,
        help="Source SQLite database path. Defaults to IRON_AGENT_WORKSPACE_FILE_SQLITE_PATH.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only read source and report target counts; do not write.",
    )
    parser.add_argument(
        "--overwrite-existing",
        action="store_true",
        help=(
            "Dangerous: overwrite existing MySQL rows with SQLite values. "
            "Default refuses conflicting existing file_id/event_id rows."
        ),
    )
    args = parser.parse_args()

    try:
        report = asyncio.run(_run(args))
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", flush=True)
        raise SystemExit(2) from exc

    if report.get("dry_run"):
        print("dry-run complete (no rows written)")
        return
    ok = report["files_ok"] and report["events_ok"]
    print(
        "workspace file metadata forward migration complete: "
        f"files={report['source_files']} events={report['source_events']} "
        f"reconciled={'yes' if ok else 'NO — target below source, investigate'}"
    )
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
