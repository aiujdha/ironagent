"""Widen workspace file path metadata columns.

Revision ID: 029_workspace_file_long_path_columns
Revises: 028_workspace_sync_jobs
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "029_workspace_file_long_path_columns"
down_revision: str | tuple[str, ...] | None = "028_workspace_sync_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TEXT_COLUMNS: tuple[tuple[str, sa.TypeEngine, bool], ...] = (
    ("filename", sa.String(length=1024), False),
    ("relative_path", sa.String(length=2048), False),
    ("source_ref", sa.String(length=1024), False),
    ("storage_key", sa.Text(), True),
    ("storage_path", sa.String(length=2048), False),
    ("object_key", sa.String(length=2048), False),
    ("object_url", sa.String(length=4096), False),
)
_EMPTY_STRING_IF_NULL = frozenset(("source_ref", "storage_path", "object_key", "object_url"))


def _backfill_empty_string_columns(
    bind: sa.engine.Connection,
    existing_columns: set[str],
) -> None:
    for column_name in sorted(_EMPTY_STRING_IF_NULL & existing_columns):
        bind.execute(
            sa.text(f"UPDATE workspace_files SET {column_name} = '' WHERE {column_name} IS NULL")
        )


def _mysql_modify_clause(column_name: str, nullable: bool) -> str:
    nullability = "NULL" if nullable else "NOT NULL"
    return f"MODIFY `{column_name}` TEXT {nullability}"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("workspace_files"):
        return
    if bind.dialect.name == "sqlite":
        return

    existing_columns = {column["name"] for column in inspector.get_columns("workspace_files")}
    _backfill_empty_string_columns(bind, existing_columns)
    if bind.dialect.name == "mysql":
        clauses = [
            _mysql_modify_clause(column_name, nullable)
            for column_name, _, nullable in _TEXT_COLUMNS
            if column_name in existing_columns
        ]
        if clauses:
            op.execute(sa.text("ALTER TABLE workspace_files " + ", ".join(clauses)))
        return

    for column_name, existing_type, nullable in _TEXT_COLUMNS:
        if column_name not in existing_columns:
            continue
        op.alter_column(
            "workspace_files",
            column_name,
            existing_type=existing_type,
            type_=sa.Text(),
            nullable=nullable,
            existing_nullable=nullable,
            server_default=None,
        )


def downgrade() -> None:
    # Do not shrink back to varchar: production may already contain long legacy paths.
    pass
