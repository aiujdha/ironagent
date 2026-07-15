"""Add indexes for Go Gateway assistant queue backpressure.

Revision ID: 026_gateway_assistant_queue_backpressure_indexes
Revises: 025_workspace_files_active_path_key
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "026_gateway_assistant_queue_backpressure_indexes"
down_revision: str | tuple[str, ...] | None = "025_workspace_files_active_path_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _has_index(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def _create_index_once(table_name: str, index_name: str, columns: list[str]) -> None:
    if _has_table(table_name) and not _has_index(table_name, index_name):
        op.create_index(index_name, table_name, columns)


def _drop_index_once(table_name: str, index_name: str) -> None:
    if _has_table(table_name) and _has_index(table_name, index_name):
        op.drop_index(index_name, table_name=table_name)


def upgrade() -> None:
    _create_index_once(
        "gateway_tasks",
        "ix_gateway_tasks_target_assistant_updated",
        ["target_assistant_id", "updated_at"],
    )
    _create_index_once(
        "gateway_task_queue",
        "ix_gateway_task_queue_status_task",
        ["status", "task_id"],
    )


def downgrade() -> None:
    _drop_index_once("gateway_task_queue", "ix_gateway_task_queue_status_task")
    _drop_index_once("gateway_tasks", "ix_gateway_tasks_target_assistant_updated")
