"""Readiness checks for workspace file metadata storage."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from alembic.script import ScriptDirectory
from alembic.script.revision import ResolutionError
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.shared.async_utils import run_sync
from src.shared.config import (
    WorkspaceFileMetadataBackend,
    resolve_workspace_file_metadata_backend,
)
from src.shared.database import get_session_factory
from src.workspace_files.database_models import (
    WorkspaceFileEventRow,
    WorkspaceFileRow,
    WorkspaceSyncJobRow,
)

REQUIRED_WORKSPACE_FILE_ALEMBIC_REVISION = "029_workspace_file_long_path_columns"
REQUIRED_WORKSPACE_FILE_MODELS = (
    WorkspaceFileRow,
    WorkspaceFileEventRow,
    WorkspaceSyncJobRow,
)


def _revision_descends_from(
    script: ScriptDirectory,
    revision: str,
    required_revision: str,
) -> bool:
    pending = [revision]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        if current == required_revision:
            return True
        try:
            script_revision = script.get_revision(current)
        except ResolutionError:
            continue
        down_revision = script_revision.down_revision
        if down_revision is None:
            continue
        if isinstance(down_revision, str):
            pending.append(down_revision)
        else:
            pending.extend(down_revision)
    return False


def _alembic_revision_satisfies_required(revisions: Iterable[str]) -> bool:
    revision_ids = {revision.strip() for revision in revisions if revision and revision.strip()}
    if REQUIRED_WORKSPACE_FILE_ALEMBIC_REVISION in revision_ids:
        return True
    if not revision_ids:
        return False
    script_location = Path(__file__).resolve().parents[2] / "migrations"
    try:
        script = ScriptDirectory(str(script_location))
    except Exception:
        return False
    return any(
        _revision_descends_from(script, revision, REQUIRED_WORKSPACE_FILE_ALEMBIC_REVISION)
        for revision in revision_ids
    )


def _check_workspace_schema(sync_connection: Any) -> dict[str, Any]:
    sync_connection.execute(text("SELECT 1"))
    inspector = inspect(sync_connection)
    missing_tables: list[str] = []
    missing_columns: dict[str, list[str]] = {}
    for model in REQUIRED_WORKSPACE_FILE_MODELS:
        table = model.__table__
        if not inspector.has_table(table.name):
            missing_tables.append(table.name)
            continue
        existing_columns = {column["name"] for column in inspector.get_columns(table.name)}
        expected_columns = set(table.columns.keys())
        table_missing = sorted(expected_columns - existing_columns)
        if table_missing:
            missing_columns[table.name] = table_missing
    if missing_tables or missing_columns:
        raise RuntimeError(
            "workspace file metadata schema is not ready: "
            f"missing_tables={missing_tables}, missing_columns={missing_columns}"
        )
    if not inspector.has_table("alembic_version"):
        raise RuntimeError("workspace file metadata schema is not ready: alembic_version missing")
    revisions = [
        str(row[0])
        for row in sync_connection.execute(text("SELECT version_num FROM alembic_version")).all()
    ]
    if not _alembic_revision_satisfies_required(revisions):
        raise RuntimeError(
            "workspace file metadata schema is not ready: "
            f"alembic_revision={revisions}, "
            f"required={REQUIRED_WORKSPACE_FILE_ALEMBIC_REVISION}"
        )
    return {
        "status": "ok",
        "backend": "mysql",
        "tables_checked": len(REQUIRED_WORKSPACE_FILE_MODELS),
        "alembic_revision": ",".join(revisions),
    }


async def acheck_workspace_file_metadata_readiness(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    backend: WorkspaceFileMetadataBackend | str | None = None,
) -> dict[str, Any]:
    resolved_backend = resolve_workspace_file_metadata_backend(backend)
    if resolved_backend != "mysql":
        return {
            "status": "ok",
            "backend": resolved_backend,
            "skipped": True,
        }
    factory = session_factory or get_session_factory()
    async with factory() as session:
        connection = await session.connection()
        return await connection.run_sync(_check_workspace_schema)


def check_workspace_file_metadata_readiness(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    backend: WorkspaceFileMetadataBackend | str | None = None,
) -> dict[str, Any]:
    return run_sync(
        acheck_workspace_file_metadata_readiness(
            session_factory=session_factory,
            backend=backend,
        )
    )
