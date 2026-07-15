"""Add Go Gateway conversation route bindings.

Revision ID: 024_gateway_conversation_routes
Revises: 023_gateway_runtime_instances
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "024_gateway_conversation_routes"
down_revision: str | tuple[str, ...] | None = "023_gateway_runtime_instances"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    if not inspector.has_table("gateway_conversation_routes"):
        op.create_table(
            "gateway_conversation_routes",
            sa.Column("client_id", sa.String(length=191), primary_key=True, nullable=False),
            sa.Column("user_id", sa.String(length=191), primary_key=True, nullable=False),
            sa.Column("conversation_id", sa.String(length=191), primary_key=True, nullable=False),
            sa.Column("assistant_id", sa.String(length=191), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    _ensure_index(
        "gateway_conversation_routes",
        "ix_gateway_conversation_routes_assistant_id",
        ["assistant_id"],
    )
    _ensure_index(
        "gateway_conversation_routes",
        "ix_gateway_conversation_routes_updated_at",
        ["updated_at"],
    )


def downgrade() -> None:
    _drop_index_if_exists("gateway_conversation_routes", "ix_gateway_conversation_routes_updated_at")
    _drop_index_if_exists("gateway_conversation_routes", "ix_gateway_conversation_routes_assistant_id")
    if inspect(op.get_bind()).has_table("gateway_conversation_routes"):
        op.drop_table("gateway_conversation_routes")


def _ensure_index(table_name: str, index_name: str, columns: list[str]) -> None:
    inspector = inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return
    existing = list(inspector.get_indexes(table_name))
    existing.extend(inspector.get_unique_constraints(table_name))
    if any(item.get("name") == index_name for item in existing):
        return
    op.create_index(index_name, table_name, columns)


def _drop_index_if_exists(table_name: str, index_name: str) -> None:
    inspector = inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return
    existing = {item["name"] for item in inspector.get_indexes(table_name)}
    existing.update(item["name"] for item in inspector.get_unique_constraints(table_name))
    if index_name in existing:
        op.drop_index(index_name, table_name=table_name)
