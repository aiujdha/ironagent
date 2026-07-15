"""Extend workspace file tables for blob storage and sync tracking.

Revision ID: 003_workspace_file_storage_sync
Revises: 002_user_bindings_and_files
"""

from alembic import op
import sqlalchemy as sa

revision = "003_workspace_file_storage_sync"
down_revision = "002_user_bindings_and_files"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    if not _has_column("workspace_files", "storage_backend"):
        op.add_column(
            "workspace_files",
            sa.Column("storage_backend", sa.String(length=32), nullable=False, server_default="local"),
        )
    if not _has_column("workspace_files", "storage_key"):
        op.add_column(
            "workspace_files",
            sa.Column("storage_key", sa.Text(), nullable=True),
        )
    if not _has_column("workspace_files", "storage_url"):
        op.add_column(
            "workspace_files",
            sa.Column("storage_url", sa.Text(), nullable=True),
        )
    if not _has_column("workspace_files", "sync_status"):
        op.add_column(
            "workspace_files",
            sa.Column("sync_status", sa.String(length=64), nullable=False, server_default="synced"),
        )
    if not _has_column("workspace_files", "origin"):
        op.add_column(
            "workspace_files",
            sa.Column("origin", sa.String(length=64), nullable=False, server_default="manual_import"),
        )
    if not _has_column("workspace_files", "etag"):
        op.add_column(
            "workspace_files",
            sa.Column("etag", sa.String(length=255), nullable=False, server_default=""),
        )
    if not _has_column("workspace_files", "workspace_path"):
        op.add_column(
            "workspace_files",
            sa.Column("workspace_path", sa.Text(), nullable=True),
        )
    if not _has_column("workspace_files", "last_synced_at"):
        op.add_column(
            "workspace_files",
            sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _has_column("workspace_files", "scan_fingerprint"):
        op.add_column(
            "workspace_files",
            sa.Column("scan_fingerprint", sa.String(length=255), nullable=False, server_default=""),
        )
    if not _has_index("workspace_files", "ix_workspace_files_assistant_bucket_deleted"):
        op.create_index(
            "ix_workspace_files_assistant_bucket_deleted",
            "workspace_files",
            ["assistant_id", "bucket", "deleted"],
        )

    if not _has_column("external_push_requests", "content_type"):
        op.add_column(
            "external_push_requests",
            sa.Column("content_type", sa.String(length=255), nullable=False, server_default=""),
        )
    if not _has_column("external_push_requests", "storage_backend"):
        op.add_column(
            "external_push_requests",
            sa.Column("storage_backend", sa.String(length=32), nullable=False, server_default="local"),
        )
    if not _has_column("external_push_requests", "storage_key"):
        op.add_column(
            "external_push_requests",
            sa.Column("storage_key", sa.Text(), nullable=True),
        )
    if not _has_column("external_push_requests", "storage_url"):
        op.add_column(
            "external_push_requests",
            sa.Column("storage_url", sa.Text(), nullable=True),
        )
    if not _has_column("external_push_requests", "staged_path"):
        op.add_column(
            "external_push_requests",
            sa.Column("staged_path", sa.Text(), nullable=True),
        )
    if not _has_index("external_push_requests", "ix_external_push_requests_assistant_created"):
        op.create_index(
            "ix_external_push_requests_assistant_created",
            "external_push_requests",
            ["assistant_id", "created_at"],
        )


def downgrade() -> None:
    op.drop_index("ix_external_push_requests_assistant_created", table_name="external_push_requests")
    op.drop_column("external_push_requests", "staged_path")
    op.drop_column("external_push_requests", "storage_url")
    op.drop_column("external_push_requests", "storage_key")
    op.drop_column("external_push_requests", "storage_backend")
    op.drop_column("external_push_requests", "content_type")

    op.drop_index("ix_workspace_files_assistant_bucket_deleted", table_name="workspace_files")
    op.drop_column("workspace_files", "scan_fingerprint")
    op.drop_column("workspace_files", "last_synced_at")
    op.drop_column("workspace_files", "workspace_path")
    op.drop_column("workspace_files", "etag")
    op.drop_column("workspace_files", "origin")
    op.drop_column("workspace_files", "sync_status")
    op.drop_column("workspace_files", "storage_url")
    op.drop_column("workspace_files", "storage_key")
    op.drop_column("workspace_files", "storage_backend")
