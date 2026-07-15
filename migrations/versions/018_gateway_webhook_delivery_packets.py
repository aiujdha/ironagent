"""Persist callback request and response packets.

Revision ID: 018_gateway_webhook_delivery_packets
Revises: 017_gateway_queue_outbox_indexes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "018_gateway_webhook_delivery_packets"
down_revision: str | tuple[str, ...] | None = "017_gateway_queue_outbox_indexes"
branch_labels = None
depends_on = None


_LONG_TEXT = sa.Text().with_variant(mysql.LONGTEXT(), "mysql")


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
    table = "gateway_webhook_deliveries"
    _add_column_once(table, sa.Column("delivery_mode", sa.String(length=32), nullable=True))
    _add_column_once(table, sa.Column("request_packet", _LONG_TEXT, nullable=True))
    _add_column_once(table, sa.Column("response_observed", sa.Boolean(), nullable=True))
    _add_column_once(table, sa.Column("response_status_code", sa.Integer(), nullable=True))
    _add_column_once(table, sa.Column("response_status", sa.String(length=128), nullable=True))
    _add_column_once(table, sa.Column("response_content_type", sa.String(length=255), nullable=True))
    _add_column_once(table, sa.Column("response_content_length", sa.BigInteger(), nullable=True))
    _add_column_once(table, sa.Column("response_body", _LONG_TEXT, nullable=True))
    _add_column_once(table, sa.Column("response_body_bytes", sa.Integer(), nullable=True))
    _add_column_once(table, sa.Column("response_body_read_error", _LONG_TEXT, nullable=True))
    _add_column_once(table, sa.Column("response_body_truncated", sa.Boolean(), nullable=True))
    _add_column_once(table, sa.Column("response_packet", _LONG_TEXT, nullable=True))
    _add_column_once(table, sa.Column("duration_ms", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    table = "gateway_webhook_deliveries"
    for column_name in (
        "duration_ms",
        "response_packet",
        "response_body_truncated",
        "response_body_read_error",
        "response_body_bytes",
        "response_body",
        "response_content_length",
        "response_content_type",
        "response_status",
        "response_status_code",
        "response_observed",
        "request_packet",
        "delivery_mode",
    ):
        _drop_column_once(table, column_name)
