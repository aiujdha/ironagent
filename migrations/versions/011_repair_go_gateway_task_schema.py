"""Repair Go Gateway task schema for environments stamped with legacy tables.

Revision ID: 011_repair_go_gateway_task_schema
Revises: 010_go_gateway_task_store
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "011_repair_go_gateway_task_schema"
down_revision = "010_go_gateway_task_store"
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


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if not _has_column(table_name, column.name):
        op.add_column(table_name, column)


def _create_index_if_missing(table_name: str, index_name: str, columns: list[str]) -> None:
    if not _has_index(table_name, index_name):
        op.create_index(index_name, table_name, columns)


def upgrade() -> None:
    if not _has_table("gateway_tasks"):
        return

    _add_column_if_missing(
        "gateway_tasks",
        sa.Column("client_id", sa.String(length=191), nullable=False, server_default="default"),
    )
    _add_column_if_missing(
        "gateway_tasks",
        sa.Column("conversation_id", sa.String(length=191), nullable=True),
    )
    _add_column_if_missing(
        "gateway_tasks",
        sa.Column("employee_no", sa.String(length=191), nullable=True),
    )
    _add_column_if_missing(
        "gateway_tasks",
        sa.Column("source_assistant_id", sa.String(length=191), nullable=True),
    )
    _add_column_if_missing(
        "gateway_tasks",
        sa.Column("target_assistant_id", sa.String(length=191), nullable=True),
    )
    _add_column_if_missing(
        "gateway_tasks",
        sa.Column("southbound_task_id", sa.String(length=191), nullable=True),
    )
    _add_column_if_missing(
        "gateway_tasks",
        sa.Column("approval_id", sa.String(length=191), nullable=True),
    )
    _add_column_if_missing(
        "gateway_tasks",
        sa.Column("status", sa.String(length=64), nullable=False, server_default="accepted"),
    )
    _add_column_if_missing(
        "gateway_tasks",
        sa.Column("stage", sa.String(length=64), nullable=True),
    )
    _add_column_if_missing(
        "gateway_tasks",
        sa.Column("fallback_query_url", sa.String(length=512), nullable=True),
    )
    _add_column_if_missing(
        "gateway_tasks",
        sa.Column("callback_url", sa.String(length=1024), nullable=True),
    )
    _add_column_if_missing(
        "gateway_tasks",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    _add_column_if_missing(
        "gateway_tasks",
        sa.Column("failed_reason_code", sa.String(length=191), nullable=True),
    )
    _add_column_if_missing(
        "gateway_tasks",
        sa.Column("failed_reason_text", sa.Text(), nullable=True),
    )
    _add_column_if_missing(
        "gateway_tasks",
        sa.Column("metadata_json", sa.JSON(), nullable=True),
    )

    bind = op.get_bind()
    if bind.dialect.name != "sqlite" and _has_column("gateway_tasks", "user_id"):
        op.alter_column(
            "gateway_tasks",
            "user_id",
            existing_type=sa.String(length=255),
            nullable=True,
        )

    _create_index_if_missing("gateway_tasks", "ix_gateway_tasks_client_id", ["client_id"])
    _create_index_if_missing("gateway_tasks", "ix_gateway_tasks_status", ["status"])
    _create_index_if_missing("gateway_tasks", "ix_gateway_tasks_approval_id", ["approval_id"])
    _create_index_if_missing(
        "gateway_tasks",
        "ix_gateway_tasks_southbound_task_id",
        ["southbound_task_id"],
    )
    _create_index_if_missing("gateway_tasks", "ix_gateway_tasks_updated_at", ["updated_at"])


def downgrade() -> None:
    # This is a repair-only migration. Dropping repaired columns could destroy task history
    # in environments that already accepted traffic, so rollback is intentionally no-op.
    pass
