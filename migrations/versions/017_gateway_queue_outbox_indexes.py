"""Add claim-path indexes for Go Gateway queue and outbox.

Revision ID: 017_gateway_queue_outbox_indexes
Revises: 016_assistant_soft_delete
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "017_gateway_queue_outbox_indexes"
down_revision: str | tuple[str, ...] | None = "016_assistant_soft_delete"
branch_labels = None
depends_on = None


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
        "gateway_task_queue",
        "ix_gateway_task_queue_claim",
        ["status", "next_attempt_at", "created_at"],
    )
    _create_index_once(
        "gateway_task_queue",
        "ix_gateway_task_queue_stuck",
        ["status", "locked_until"],
    )
    _create_index_once(
        "gateway_task_queue",
        "ix_gateway_task_queue_claimed",
        ["locked_by", "status", "created_at"],
    )
    _create_index_once(
        "gateway_event_outbox",
        "ix_gateway_event_outbox_claim",
        ["status", "next_attempt_at", "created_at", "event_seq"],
    )
    _create_index_once(
        "gateway_event_outbox",
        "ix_gateway_event_outbox_stuck",
        ["status", "locked_until"],
    )
    _create_index_once(
        "gateway_event_outbox",
        "ix_gateway_event_outbox_claim_order",
        ["status", "created_at", "event_seq"],
    )


def downgrade() -> None:
    _drop_index_once("gateway_event_outbox", "ix_gateway_event_outbox_claim_order")
    _drop_index_once("gateway_event_outbox", "ix_gateway_event_outbox_stuck")
    _drop_index_once("gateway_event_outbox", "ix_gateway_event_outbox_claim")
    _drop_index_once("gateway_task_queue", "ix_gateway_task_queue_claimed")
    _drop_index_once("gateway_task_queue", "ix_gateway_task_queue_stuck")
    _drop_index_once("gateway_task_queue", "ix_gateway_task_queue_claim")
