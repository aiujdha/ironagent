"""Add role column to users.

Revision ID: 008_users_role_column
Revises: 007_workspace_file_storage_sync_backfill
"""

import sqlalchemy as sa
from alembic import op

revision = "008_users_role_column"
down_revision = "007_workspace_file_storage_sync_backfill"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    if _has_column("users", "role"):
        return
    op.add_column(
        "users",
        sa.Column("role", sa.String(length=64), nullable=False, server_default="user"),
    )


def downgrade() -> None:
    if _has_column("users", "role"):
        op.drop_column("users", "role")
