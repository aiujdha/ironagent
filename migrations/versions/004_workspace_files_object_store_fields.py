"""Add object-store metadata columns to workspace_files.

Revision ID: 004_workspace_files_object_store_fields
Revises: 003_workspace_file_push_request_fields
"""

import sqlalchemy as sa
from alembic import op

revision = "004_workspace_files_object_store_fields"
down_revision = "003_workspace_file_push_request_fields"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


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
    if not _has_column("workspace_files", "direction"):
        op.add_column(
            "workspace_files",
            sa.Column("direction", sa.String(20), nullable=False, server_default="inbound"),
        )
    if not _has_column("workspace_files", "storage_backend"):
        op.add_column(
            "workspace_files",
            sa.Column("storage_backend", sa.String(20), nullable=False, server_default="local"),
        )
    if not _has_column("workspace_files", "object_key"):
        op.add_column(
            "workspace_files",
            sa.Column("object_key", sa.String(2048), nullable=False, server_default=""),
        )
    if not _has_column("workspace_files", "object_url"):
        op.add_column(
            "workspace_files",
            sa.Column("object_url", sa.String(4096), nullable=False, server_default=""),
        )
    if not _has_column("workspace_files", "checksum"):
        op.add_column(
            "workspace_files",
            sa.Column("checksum", sa.String(128), nullable=False, server_default=""),
        )
    if not _has_column("workspace_files", "sync_status"):
        op.add_column(
            "workspace_files",
            sa.Column(
                "sync_status",
                sa.String(64),
                nullable=False,
                server_default="synced_to_workspace",
            ),
        )
    _add_required_text_column("workspace_files", "last_error")


def downgrade() -> None:
    op.drop_column("workspace_files", "last_error")
    op.drop_column("workspace_files", "sync_status")
    op.drop_column("workspace_files", "checksum")
    op.drop_column("workspace_files", "object_url")
    op.drop_column("workspace_files", "object_key")
    op.drop_column("workspace_files", "storage_backend")
    op.drop_column("workspace_files", "direction")
