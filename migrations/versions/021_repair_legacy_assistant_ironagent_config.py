"""Repair legacy assistant iron_agent_config column.

Revision ID: 021_repair_legacy_assistant_ironagent_config
Revises: 020_workspace_file_owner_user_id
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "021_repair_legacy_assistant_ironagent_config"
down_revision: str | tuple[str, ...] | None = "020_workspace_file_owner_user_id"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    table = "assistants"
    column = "iron_agent_config"
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if not _has_table(table) or not _has_column(table, column):
        return
    op.alter_column(table, column, existing_type=sa.JSON(), nullable=True)


def downgrade() -> None:
    # Repair-only migration. Do not restore the legacy NOT NULL constraint.
    pass
