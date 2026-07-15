"""Repair workspace file storage_provider defaults.

Revision ID: 010_repair_workspace_file_storage_provider
Revises: 009_merge_workspace_file_heads
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "010_repair_workspace_file_storage_provider"
down_revision = "009_merge_workspace_file_heads"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _set_storage_provider_default() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("workspace_files") as batch_op:
            batch_op.alter_column(
                "storage_provider",
                existing_type=sa.String(length=64),
                nullable=False,
                server_default="local",
            )
        return
    op.alter_column(
        "workspace_files",
        "storage_provider",
        existing_type=sa.String(length=64),
        nullable=False,
        server_default="local",
    )


def upgrade() -> None:
    if not _has_table("workspace_files"):
        return
    if not _has_column("workspace_files", "storage_provider"):
        op.add_column(
            "workspace_files",
            sa.Column(
                "storage_provider",
                sa.String(length=64),
                nullable=False,
                server_default="local",
            ),
        )
    op.execute(
        sa.text(
            """
            UPDATE workspace_files
            SET storage_provider = CASE
                WHEN storage_backend IN ('cos', 'obs') THEN storage_backend
                ELSE 'local'
            END
            WHERE storage_provider IS NULL OR storage_provider = ''
            """
    )
        )
    _set_storage_provider_default()


def downgrade() -> None:
    pass
