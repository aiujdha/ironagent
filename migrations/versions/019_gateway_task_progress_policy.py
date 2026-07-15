"""Track Go Gateway task queue progress policy.

Revision ID: 019_gateway_task_progress_policy
Revises: 018_gateway_webhook_delivery_packets
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "019_gateway_task_progress_policy"
down_revision: str | tuple[str, ...] | None = "018_gateway_webhook_delivery_packets"
branch_labels = None
depends_on = None


_LONG_TEXT = sa.Text().with_variant(mysql.LONGTEXT(), "mysql")
_JSON = sa.JSON().with_variant(mysql.JSON(), "mysql")


def _has_table(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def _add_column_once(table_name: str, column: sa.Column) -> None:
    if _has_table(table_name) and not _has_column(table_name, column.name):
        op.add_column(table_name, column)


def _drop_column_once(table_name: str, column_name: str) -> None:
    if _has_table(table_name) and _has_column(table_name, column_name):
        op.drop_column(table_name, column_name)


def _create_index_once(table_name: str, index_name: str, columns: list[str]) -> None:
    if _has_table(table_name) and not _has_index(table_name, index_name):
        op.create_index(index_name, table_name, columns)


def _drop_index_once(table_name: str, index_name: str) -> None:
    if _has_table(table_name) and _has_index(table_name, index_name):
        op.drop_index(index_name, table_name=table_name)


def upgrade() -> None:
    table = "gateway_task_queue"
    _add_column_once(table, sa.Column("attempt_token", sa.String(length=191), nullable=True))
    _add_column_once(table, sa.Column("attempt_started_at", sa.DateTime(timezone=True), nullable=True))
    _add_column_once(table, sa.Column("last_progress_at", sa.DateTime(timezone=True), nullable=True))
    _add_column_once(table, sa.Column("last_progress_status", sa.String(length=64), nullable=True))
    _add_column_once(table, sa.Column("last_progress_stage", sa.String(length=64), nullable=True))
    _add_column_once(table, sa.Column("last_progress_detail", _LONG_TEXT, nullable=True))
    _add_column_once(table, sa.Column("terminal_reason_code", sa.String(length=191), nullable=True))
    _add_column_once(table, sa.Column("terminal_reason_text", _LONG_TEXT, nullable=True))
    _add_column_once(table, sa.Column("terminal_detail_json", _JSON, nullable=True))

    _create_index_once(table, "ix_gateway_task_queue_progress_stale", ["status", "last_progress_at"])
    _create_index_once(table, "ix_gateway_task_queue_attempt_token", ["attempt_token"])
    _create_index_once(table, "ix_gateway_task_queue_terminal_reason", ["terminal_reason_code"])


def downgrade() -> None:
    table = "gateway_task_queue"
    _drop_index_once(table, "ix_gateway_task_queue_terminal_reason")
    _drop_index_once(table, "ix_gateway_task_queue_attempt_token")
    _drop_index_once(table, "ix_gateway_task_queue_progress_stale")
    for column_name in (
        "terminal_detail_json",
        "terminal_reason_text",
        "terminal_reason_code",
        "last_progress_detail",
        "last_progress_stage",
        "last_progress_status",
        "last_progress_at",
        "attempt_started_at",
        "attempt_token",
    ):
        _drop_column_once(table, column_name)
