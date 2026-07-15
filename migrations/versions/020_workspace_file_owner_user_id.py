"""Add workspace file owner user field.

Revision ID: 020_workspace_file_owner_user_id
Revises: 019_gateway_task_progress_policy
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "020_workspace_file_owner_user_id"
down_revision: str | tuple[str, ...] | None = "019_gateway_task_progress_policy"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    table = "workspace_files"
    if not _has_table(table):
        return
    if not _has_column(table, "owner_user_id"):
        op.add_column(
            table,
            sa.Column(
                "owner_user_id",
                sa.String(length=255),
                nullable=False,
                server_default="",
            ),
        )
    if not _has_index(table, "ix_workspace_files_owner_user_id"):
        op.create_index("ix_workspace_files_owner_user_id", table, ["owner_user_id"])


def downgrade() -> None:
    table = "workspace_files"
    if not _has_table(table):
        return
    if _has_index(table, "ix_workspace_files_owner_user_id"):
        op.drop_index("ix_workspace_files_owner_user_id", table_name=table)
    if _has_column(table, "owner_user_id"):
        op.drop_column(table, "owner_user_id")
