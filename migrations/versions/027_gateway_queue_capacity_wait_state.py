"""Store Go Gateway capacity-wait state on queue rows.

Revision ID: 027_gateway_queue_capacity_wait_state
Revises: 026_gateway_assistant_queue_backpressure_indexes
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "027_gateway_queue_capacity_wait_state"
down_revision: str | tuple[str, ...] | None = "026_gateway_assistant_queue_backpressure_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _add_column_once(table_name: str, column: sa.Column) -> None:
    if _has_table(table_name) and not _has_column(table_name, column.name):
        op.add_column(table_name, column)


def _drop_column_once(table_name: str, column_name: str) -> None:
    if _has_table(table_name) and _has_column(table_name, column_name):
        op.drop_column(table_name, column_name)


def upgrade() -> None:
    table = "gateway_task_queue"
    _add_column_once(
        table,
        sa.Column(
            "capacity_wait_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    _add_column_once(table, sa.Column("capacity_wait_first_at", sa.DateTime(timezone=True), nullable=True))
    _add_column_once(table, sa.Column("capacity_wait_last_at", sa.DateTime(timezone=True), nullable=True))
    _add_column_once(
        table,
        sa.Column("capacity_wait_last_callback_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    table = "gateway_task_queue"
    _drop_column_once(table, "capacity_wait_last_callback_at")
    _drop_column_once(table, "capacity_wait_last_at")
    _drop_column_once(table, "capacity_wait_first_at")
    _drop_column_once(table, "capacity_wait_count")
