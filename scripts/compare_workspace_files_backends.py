"""P3 影子读:逐行比对 workspace_files / events 在 SQLite vs MySQL 两后端读出的结果。

战役一切闸(P4)前的信心校验:确认 MySQL(迁移器灌入)与现役 SQLite 读出的数据一致。

⚠️ 两后端读路径本就有 8 个 file 字段的 cross-repo 读语义差异(非数据错,经 file:line 核实),默认加白名单
   不报警;其余字段不一致 = 真实数据差异(或迁移丢数),报警并以退出码 1 阻断。
   白名单(File):sync_status / direction / storage_provider / checksum /
   storage_status / storage_path / object_url / storage_error;datetime 归一 UTC 再比。
   `--show-known` 可把白名单差异也打印出来核对。

用法:
  python -m scripts.compare_workspace_files_backends   # 默认 SQLite 源 vs 默认 MySQL
  python -m scripts.compare_workspace_files_backends --sqlite-path /path/workspace.db --show-known
  python -m scripts.compare_workspace_files_backends --strict-extra-mysql
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.shared.database import dispose_database_engines, get_session_factory
from src.shared.repositories import _workspace_file_event_from_row as _mysql_event_from_row
from src.shared.repositories import _workspace_file_from_row as _mysql_file_from_row
from src.workspace_files.database_models import WorkspaceFileEventRow, WorkspaceFileRow
from src.workspace_files.models import WorkspaceFile, WorkspaceFileEvent
from src.workspace_files.sqlite_repository import (
    SQLiteWorkspaceFileRepository,
    SQLiteWorkspaceFileStore,
)
from src.workspace_files.sqlite_repository import (
    _workspace_file_event_from_row as _sqlite_event_from_row,
)

# 两后端读路径产出不同值的字段(cross-repo 读语义,非数据错;详见 cutover 分析)。
WHITELIST_FILE_FIELDS = frozenset(
    {
        "sync_status",  # MySQL _normalize_sync_status 归一 vs SQLite 原始透传
        "direction",  # MySQL 空时按 file_role 推导 vs SQLite 透传
        "storage_provider",  # SQLite 恒按 backend 派生(表无该列)vs MySQL 优先存储列
        "checksum",  # MySQL checksum_sha256 or checksum vs SQLite 仅 checksum_sha256
        "storage_status",  # SQLite 空时 fallback sync_status vs MySQL 直读
        "storage_path",  # SQLite 空时 fallback object_url vs MySQL 直读
        "object_url",  # 受 storage_path fallback 传染
        "storage_error",  # SQLite 空时 fallback last_error vs MySQL 直读
    }
)


def _norm(value: object) -> object:
    """datetime 归一到 aware-UTC(MySQL DATETIME 读回 naive,SQLite 读回 aware,按瞬时比)。"""
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return value


def _diff_model(
    source: WorkspaceFile | WorkspaceFileEvent,
    target: WorkspaceFile | WorkspaceFileEvent,
    *,
    whitelist: frozenset[str],
    show_known: bool,
) -> list[str]:
    diffs: list[str] = []
    for field in sorted(type(source).model_fields):
        is_known = field in whitelist
        if is_known and not show_known:
            continue
        sv, tv = getattr(source, field), getattr(target, field)
        if _norm(sv) != _norm(tv):
            tag = "[known] " if is_known else ""
            diffs.append(f"{tag}{field}: sqlite={sv!r} mysql={tv!r}")
    return diffs


def _compare_sets(
    source: list,
    target: list,
    *,
    key: Callable[[Any], str],
    whitelist: frozenset[str],
    show_known: bool,
) -> dict:
    src_by = {key(item): item for item in source}
    tgt_by = {key(item): item for item in target}
    missing = sorted(set(src_by) - set(tgt_by))  # 在 SQLite、不在 MySQL = 迁移丢数
    extra = sorted(set(tgt_by) - set(src_by))  # 在 MySQL、不在 SQLite
    real_mismatches: list[tuple[str, list[str]]] = []
    for shared_key in sorted(set(src_by) & set(tgt_by)):
        diffs = _diff_model(
            src_by[shared_key], tgt_by[shared_key], whitelist=whitelist, show_known=show_known
        )
        if diffs:
            real_mismatches.append((shared_key, diffs))
    return {
        "source_count": len(source),
        "target_count": len(target),
        "missing_in_mysql": missing,
        "extra_in_mysql": extra,
        "value_mismatches": real_mismatches,
    }


def _read_sqlite_events(store: SQLiteWorkspaceFileStore) -> list[WorkspaceFileEvent]:
    with store.connect(read_only=True) as conn:
        rows: list[sqlite3.Row] = conn.execute(
            "SELECT * FROM workspace_file_events ORDER BY created_at ASC, event_id ASC"
        ).fetchall()
    return [_sqlite_event_from_row(row) for row in rows]


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


async def _load_mysql(
    session_factory: async_sessionmaker[AsyncSession],
    row_model: type,
    converter: Callable[[Any], Any],
) -> list:
    async with session_factory() as session:
        rows: list[Any] = list((await session.execute(select(row_model))).scalars().all())
    return [converter(row) for row in rows]


async def compare(
    *,
    sqlite_path: Path | None = None,
    target_session_factory: async_sessionmaker[AsyncSession] | None = None,
    show_known: bool = False,
) -> dict:
    store = _build_source_store(sqlite_path)

    sqlite_files = await SQLiteWorkspaceFileRepository(store).alist_files(
        include_deleted=True, limit=None
    )
    sqlite_events = await asyncio.to_thread(_read_sqlite_events, store)

    session_factory = target_session_factory or get_session_factory()
    mysql_files = await _load_mysql(session_factory, WorkspaceFileRow, _mysql_file_from_row)
    mysql_events = await _load_mysql(session_factory, WorkspaceFileEventRow, _mysql_event_from_row)

    files = _compare_sets(
        sqlite_files,
        mysql_files,
        key=lambda f: f.file_id,
        whitelist=WHITELIST_FILE_FIELDS,
        show_known=show_known,
    )
    events = _compare_sets(
        sqlite_events,
        mysql_events,
        key=lambda e: e.event_id,
        whitelist=frozenset(),  # events 无硬白名单(detail 正常 dict 比对一致;datetime 已归一)
        show_known=show_known,
    )
    return {"files": files, "events": events}


def _print_report(
    name: str,
    report: dict,
    *,
    sample: int,
    allow_extra_mysql: bool = True,
) -> bool:
    missing, extra, mismatches = (
        report["missing_in_mysql"],
        report["extra_in_mysql"],
        report["value_mismatches"],
    )
    blocking_extra = extra if not allow_extra_mysql else []
    clean = not missing and not blocking_extra and not mismatches
    print(
        f"[{name}] sqlite={report['source_count']} mysql={report['target_count']} "
        f"missing_in_mysql={len(missing)} extra_in_mysql={len(extra)} "
        f"value_mismatches={len(mismatches)} "
        f"extra_policy={'allowed' if allow_extra_mysql else 'strict'} "
        f"-> {'OK' if clean else 'DIFF'}",
        flush=True,
    )
    for key in missing[:sample]:
        print(f"  MISSING (迁移丢数): {key}", flush=True)
    for key in extra[:sample]:
        label = "EXTRA  (MySQL 新写入/多出)" if allow_extra_mysql else "EXTRA  (MySQL 多出)"
        print(f"  {label}: {key}", flush=True)
    for key, diffs in mismatches[:sample]:
        print(f"  MISMATCH {key}:", flush=True)
        for line in diffs:
            print(f"    {line}", flush=True)
    return clean


async def _run(args: argparse.Namespace) -> dict:
    try:
        return await compare(sqlite_path=args.sqlite_path, show_known=args.show_known)
    finally:
        await dispose_database_engines()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Shadow-read compare workspace_files/events: SQLite backend vs MySQL backend.",
    )
    parser.add_argument("--sqlite-path", type=Path, default=None, help="Source SQLite path.")
    parser.add_argument(
        "--show-known",
        action="store_true",
        help="Also print the 8 known file-field read-semantic differences (default hidden).",
    )
    parser.add_argument(
        "--strict-extra-mysql",
        action="store_true",
        help=(
            "Treat MySQL-only rows as blocking differences. Default allows them so online "
            "cutover writes do not fail shadow compare."
        ),
    )
    parser.add_argument("--sample", type=int, default=20, help="Max sample lines per category.")
    args = parser.parse_args()

    try:
        report = asyncio.run(_run(args))
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", flush=True)
        raise SystemExit(2) from exc

    allow_extra_mysql = not args.strict_extra_mysql
    files_ok = _print_report(
        "files",
        report["files"],
        sample=args.sample,
        allow_extra_mysql=allow_extra_mysql,
    )
    events_ok = _print_report(
        "events",
        report["events"],
        sample=args.sample,
        allow_extra_mysql=allow_extra_mysql,
    )
    if files_ok and events_ok:
        print(
            "shadow-read OK: no blocking differences "
            "(known read-semantic diffs whitelisted; MySQL-only rows allowed by default)"
        )
        return
    print("shadow-read FOUND DIFFERENCES — investigate before cutover", flush=True)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
