"""Extend workspace file tables for object storage sync and approval payload staging.

Revision ID: 003_workspace_file_object_storage
Revises: 002_user_bindings_and_files
"""

from alembic import op
import sqlalchemy as sa

revision = "003_workspace_file_object_storage"
down_revision = "002_user_bindings_and_files"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def _set_not_null(table_name: str, column_name: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(column_name, existing_type=sa.Text(), nullable=False)
        return
    op.alter_column(table_name, column_name, existing_type=sa.Text(), nullable=False)


def _add_required_text_column(table_name: str, column_name: str) -> None:
    if _has_column(table_name, column_name):
        return
    op.add_column(table_name, sa.Column(column_name, sa.Text(), nullable=True))
    op.execute(sa.text(f"UPDATE {table_name} SET {column_name} = '' WHERE {column_name} IS NULL"))
    _set_not_null(table_name, column_name)


def upgrade() -> None:
    if not _has_column("workspace_files", "storage_backend"):
        op.add_column(
            "workspace_files",
            sa.Column("storage_backend", sa.String(length=32), nullable=False, server_default=""),
        )
    if not _has_column("workspace_files", "object_key"):
        op.add_column(
            "workspace_files",
            sa.Column("object_key", sa.String(length=1024), nullable=False, server_default=""),
        )
    _add_required_text_column("workspace_files", "object_url")
    if not _has_column("workspace_files", "checksum_sha256"):
        op.add_column(
            "workspace_files",
            sa.Column("checksum_sha256", sa.String(length=64), nullable=False, server_default=""),
        )
    if not _has_column("workspace_files", "sync_status"):
        op.add_column(
            "workspace_files",
            sa.Column(
                "sync_status",
                sa.String(length=32),
                nullable=False,
                server_default="pending",
            ),
        )
    _add_required_text_column("workspace_files", "sync_error")
    if not _has_column("workspace_files", "file_role"):
        op.add_column(
            "workspace_files",
            sa.Column("file_role", sa.String(length=32), nullable=False, server_default="input"),
        )
    if not _has_column("workspace_files", "workspace_last_modified_at"):
        op.add_column(
            "workspace_files",
            sa.Column("workspace_last_modified_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _has_column("workspace_files", "last_synced_at"):
        op.add_column(
            "workspace_files",
            sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        )

    if not _has_column("external_push_requests", "content_type"):
        op.add_column(
            "external_push_requests",
            sa.Column("content_type", sa.String(length=255), nullable=False, server_default=""),
        )
    if not _has_column("external_push_requests", "staged_path"):
        op.add_column(
            "external_push_requests",
            sa.Column("staged_path", sa.Text(), nullable=True),
        )
    if not _has_index("external_push_requests", "ix_external_push_requests_approval_id"):
        op.create_index(
            "ix_external_push_requests_approval_id",
            "external_push_requests",
            ["approval_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index("ix_external_push_requests_approval_id", table_name="external_push_requests")
    op.drop_column("external_push_requests", "staged_path")
    op.drop_column("external_push_requests", "content_type")

    op.drop_column("workspace_files", "last_synced_at")
    op.drop_column("workspace_files", "workspace_last_modified_at")
    op.drop_column("workspace_files", "file_role")
    op.drop_column("workspace_files", "sync_error")
    op.drop_column("workspace_files", "sync_status")
    op.drop_column("workspace_files", "checksum_sha256")
    op.drop_column("workspace_files", "object_url")
    op.drop_column("workspace_files", "object_key")
    op.drop_column("workspace_files", "storage_backend")
