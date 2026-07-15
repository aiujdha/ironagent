"""Repair workspace file schema for environments stamped with legacy merge revisions.

Revision ID: 006_repair_workspace_file_schema
Revises: 005_merge_workspace_file_heads
"""

import sqlalchemy as sa
from alembic import op

revision = "006_repair_workspace_file_schema"
down_revision = "005_merge_workspace_file_heads"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table_name)}


def _set_not_null(table_name: str, column_name: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(column_name, existing_type=sa.Text(), nullable=False)
        return
    op.alter_column(
        table_name,
        column_name,
        existing_type=sa.Text(),
        nullable=False,
    )


def upgrade() -> None:
    workspace_file_columns = _column_names("workspace_files")
    if "direction" not in workspace_file_columns:
        op.add_column(
            "workspace_files",
            sa.Column("direction", sa.String(20), nullable=False, server_default="inbound"),
        )
    if "checksum" not in workspace_file_columns:
        op.add_column(
            "workspace_files",
            sa.Column("checksum", sa.String(128), nullable=False, server_default=""),
        )
    if "sync_status" not in workspace_file_columns:
        op.add_column(
            "workspace_files",
            sa.Column(
                "sync_status",
                sa.String(64),
                nullable=False,
                server_default="synced_to_workspace",
            ),
        )
    if "last_error" not in workspace_file_columns:
        op.add_column(
            "workspace_files",
            sa.Column("last_error", sa.Text(), nullable=True),
        )
        op.execute("UPDATE workspace_files SET last_error = '' WHERE last_error IS NULL")
        _set_not_null("workspace_files", "last_error")


def downgrade() -> None:
    workspace_file_columns = _column_names("workspace_files")
    if "last_error" in workspace_file_columns:
        op.drop_column("workspace_files", "last_error")
    if "sync_status" in workspace_file_columns:
        op.drop_column("workspace_files", "sync_status")
    if "checksum" in workspace_file_columns:
        op.drop_column("workspace_files", "checksum")
    if "direction" in workspace_file_columns:
        op.drop_column("workspace_files", "direction")
