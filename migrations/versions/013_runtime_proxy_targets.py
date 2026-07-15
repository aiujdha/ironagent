"""Add runtime proxy target URLs for Go Gateway.

Revision ID: 013_runtime_proxy_targets
Revises: 012_repair_workspace_file_legacy_defaults
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "013_runtime_proxy_targets"
down_revision = "012_repair_workspace_file_legacy_defaults"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if not _has_column(table_name, column.name):
        op.add_column(table_name, column)


def upgrade() -> None:
    if _has_table("gateway_runtimes"):
        _add_column_if_missing(
            "gateway_runtimes",
            sa.Column("internal_base_url", sa.String(length=1024), nullable=True),
        )
        _add_column_if_missing(
            "gateway_runtimes",
            sa.Column("gateway_url", sa.String(length=1024), nullable=True),
        )

    if _has_table("gateway_runtime_heartbeats"):
        _add_column_if_missing(
            "gateway_runtime_heartbeats",
            sa.Column("internal_base_url", sa.String(length=1024), nullable=True),
        )
        _add_column_if_missing(
            "gateway_runtime_heartbeats",
            sa.Column("gateway_url", sa.String(length=1024), nullable=True),
        )


def downgrade() -> None:
    if _has_table("gateway_runtime_heartbeats"):
        if _has_column("gateway_runtime_heartbeats", "gateway_url"):
            op.drop_column("gateway_runtime_heartbeats", "gateway_url")
        if _has_column("gateway_runtime_heartbeats", "internal_base_url"):
            op.drop_column("gateway_runtime_heartbeats", "internal_base_url")

    if _has_table("gateway_runtimes"):
        if _has_column("gateway_runtimes", "gateway_url"):
            op.drop_column("gateway_runtimes", "gateway_url")
        if _has_column("gateway_runtimes", "internal_base_url"):
            op.drop_column("gateway_runtimes", "internal_base_url")
