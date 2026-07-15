from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from sqlalchemy import select

from src.shared.database import get_database_engine, get_session_factory
from src.workspace_files.database_models import WorkspaceFileEventRow
from src.workspace_files.repository import (
    DatabaseWorkspaceFileRepository,
    _workspace_file_event_from_row,
)
from src.workspace_files.sqlite_repository import (
    SQLiteWorkspaceFileStore,
    copy_events_to_sqlite,
    copy_files_to_sqlite,
)


async def _load_mysql_workspace_events() -> list:
    async with get_session_factory()() as session:
        result = await session.execute(
            select(WorkspaceFileEventRow).order_by(
                WorkspaceFileEventRow.created_at.asc(),
                WorkspaceFileEventRow.event_id.asc(),
        )
            )
        return [_workspace_file_event_from_row(row) for row in result.scalars().all()]


async def migrate(*, sqlite_path: Path | None = None) -> tuple[int, int]:
    store = SQLiteWorkspaceFileStore(sqlite_path)
    print(f"target sqlite: {store.db_path}", flush=True)
    print("loading workspace_files from MySQL...", flush=True)
    mysql_files = await DatabaseWorkspaceFileRepository().alist_files(
        include_deleted=True,
        limit=None,
    )
    print(f"loaded workspace_files: {len(mysql_files)}", flush=True)
    print("loading workspace_file_events from MySQL...", flush=True)
    mysql_events = await _load_mysql_workspace_events()
    print(f"loaded workspace_file_events: {len(mysql_events)}", flush=True)
    print("writing workspace_files to SQLite...", flush=True)
    file_count = await asyncio.to_thread(copy_files_to_sqlite, mysql_files, store=store)
    print("writing workspace_file_events to SQLite...", flush=True)
    event_count = await asyncio.to_thread(copy_events_to_sqlite, mysql_events, store=store)
    await get_database_engine().dispose()
    return file_count, event_count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy workspace_files and workspace_file_events from MySQL to SQLite WAL.",
    )
    parser.add_argument(
        "--sqlite-path",
        type=Path,
        default=None,
        help="Target SQLite database path. Defaults to IRON_AGENT_WORKSPACE_FILE_SQLITE_PATH.",
    )
    args = parser.parse_args()

    file_count, event_count = asyncio.run(migrate(sqlite_path=args.sqlite_path))
    print(
        "workspace file metadata migration complete: "
        f"files={file_count}, events={event_count}"
    )


if __name__ == "__main__":
    main()
