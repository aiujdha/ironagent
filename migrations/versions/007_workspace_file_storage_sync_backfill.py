"""Idempotently backfill workspace file storage/sync columns.

Revision ID: 007_workspace_file_storage_sync_backfill
Revises: 006_repair_workspace_file_schema
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "007_workspace_file_storage_sync_backfill"
down_revision = "006_repair_workspace_file_schema"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = inspector.get_columns(table_name)
    return any(column["name"] == column_name for column in columns)


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    indexes = inspector.get_indexes(table_name)
    return any(index["name"] == index_name for index in indexes)


def upgrade() -> None:
    workspace_columns = [
        ("storage_backend", sa.String(length=32), "local", False),
        ("storage_key", sa.Text(), None, True),
        ("storage_url", sa.Text(), None, True),
        ("sync_status", sa.String(length=64), "synced", False),
        ("origin", sa.String(length=64), "manual_import", False),
        ("etag", sa.String(length=255), "", False),
        ("workspace_path", sa.Text(), None, True),
        ("last_synced_at", sa.DateTime(timezone=True), None, True),
        ("scan_fingerprint", sa.String(length=255), "", False),
    ]
    for name, type_, default, nullable in workspace_columns:
        if _has_column("workspace_files", name):
            continue
        kwargs = {"nullable": nullable}
        if default is not None:
            kwargs["server_default"] = default
        op.add_column("workspace_files", sa.Column(name, type_, **kwargs))
    if not _has_index("workspace_files", "ix_workspace_files_assistant_bucket_deleted"):
        op.create_index(
            "ix_workspace_files_assistant_bucket_deleted",
            "workspace_files",
            ["assistant_id", "bucket", "deleted"],
        )

    push_columns = [
        ("content_type", sa.String(length=255), "", False),
        ("storage_backend", sa.String(length=32), "local", False),
        ("storage_key", sa.Text(), None, True),
        ("storage_url", sa.Text(), None, True),
        ("staged_path", sa.Text(), None, True),
    ]
    for name, type_, default, nullable in push_columns:
        if _has_column("external_push_requests", name):
            continue
        kwargs = {"nullable": nullable}
        if default is not None:
            kwargs["server_default"] = default
        op.add_column("external_push_requests", sa.Column(name, type_, **kwargs))
    if not _has_index("external_push_requests", "ix_external_push_requests_assistant_created"):
        op.create_index(
            "ix_external_push_requests_assistant_created",
            "external_push_requests",
            ["assistant_id", "created_at"],
        )


def downgrade() -> None:
    pass
