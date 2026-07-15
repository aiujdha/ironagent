"""Add Go Gateway runtime instance leases.

Revision ID: 023_gateway_runtime_instances
Revises: 022_collaboration_repository_tables
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "023_gateway_runtime_instances"
down_revision: str | tuple[str, ...] | None = "022_collaboration_repository_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())

    if not inspector.has_table("gateway_runtime_instances"):
        op.create_table(
            "gateway_runtime_instances",
            sa.Column("runtime_id", sa.String(length=191), primary_key=True, nullable=False),
            sa.Column("assistant_id", sa.String(length=191), nullable=False),
            sa.Column("pod_name", sa.String(length=191), nullable=True),
            sa.Column("internal_base_url", sa.String(length=1024), nullable=True),
            sa.Column("gateway_url", sa.String(length=1024), nullable=True),
            sa.Column("status", sa.String(length=64), nullable=False),
            sa.Column("capacity", sa.Integer(), nullable=False),
            sa.Column("in_flight", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("capabilities_json", sa.JSON(), nullable=True),
            sa.Column("drain_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    _ensure_index("gateway_runtime_instances", "ix_gateway_runtime_instances_assistant_id", ["assistant_id"])
    _ensure_index("gateway_runtime_instances", "ix_gateway_runtime_instances_status", ["status"])
    _ensure_index(
        "gateway_runtime_instances",
        "ix_gateway_runtime_instances_claim",
        ["assistant_id", "status", "drain_mode", "in_flight", "capacity"],
    )
    _ensure_index(
        "gateway_runtime_instances",
        "ix_gateway_runtime_instances_last_heartbeat_at",
        ["last_heartbeat_at"],
    )

    if not inspector.has_table("gateway_runtime_leases"):
        op.create_table(
            "gateway_runtime_leases",
            sa.Column("lease_id", sa.String(length=191), primary_key=True, nullable=False),
            sa.Column("runtime_id", sa.String(length=191), nullable=False),
            sa.Column("assistant_id", sa.String(length=191), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    _ensure_index("gateway_runtime_leases", "ix_gateway_runtime_leases_runtime_id", ["runtime_id"])
    _ensure_index("gateway_runtime_leases", "ix_gateway_runtime_leases_assistant_id", ["assistant_id"])
    _ensure_index("gateway_runtime_leases", "ix_gateway_runtime_leases_status", ["status"])
    _ensure_index(
        "gateway_runtime_leases",
        "ix_gateway_runtime_leases_active_expiry",
        ["status", "lease_expires_at"],
    )

    _backfill_runtime_instances()


def downgrade() -> None:
    _drop_index_if_exists("gateway_runtime_leases", "ix_gateway_runtime_leases_active_expiry")
    _drop_index_if_exists("gateway_runtime_leases", "ix_gateway_runtime_leases_status")
    _drop_index_if_exists("gateway_runtime_leases", "ix_gateway_runtime_leases_assistant_id")
    _drop_index_if_exists("gateway_runtime_leases", "ix_gateway_runtime_leases_runtime_id")
    if inspect(op.get_bind()).has_table("gateway_runtime_leases"):
        op.drop_table("gateway_runtime_leases")

    _drop_index_if_exists(
        "gateway_runtime_instances",
        "ix_gateway_runtime_instances_last_heartbeat_at",
    )
    _drop_index_if_exists("gateway_runtime_instances", "ix_gateway_runtime_instances_claim")
    _drop_index_if_exists("gateway_runtime_instances", "ix_gateway_runtime_instances_status")
    _drop_index_if_exists("gateway_runtime_instances", "ix_gateway_runtime_instances_assistant_id")
    if inspect(op.get_bind()).has_table("gateway_runtime_instances"):
        op.drop_table("gateway_runtime_instances")


def _backfill_runtime_instances() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("gateway_runtimes") or not inspector.has_table("gateway_runtime_instances"):
        return

    runtime_columns = {column["name"] for column in inspector.get_columns("gateway_runtimes")}
    required = {
        "assistant_id",
        "status",
        "capacity",
        "in_flight",
        "capabilities_json",
        "drain_mode",
        "last_heartbeat_at",
        "last_error",
        "version",
        "updated_at",
    }
    if not required.issubset(runtime_columns):
        return

    optional_columns = {
        "runtime_id": "runtime_id",
        "pod_name": "pod_name",
        "internal_base_url": "internal_base_url",
        "gateway_url": "gateway_url",
    }
    select_columns = ["assistant_id"]
    select_columns.extend(
        column for column in optional_columns.values() if column in runtime_columns
    )
    select_columns.extend(sorted(required - {"assistant_id"}))
    rows = bind.execute(
        sa.text(f"SELECT {', '.join(select_columns)} FROM gateway_runtimes")
    ).mappings()

    for row in rows:
        assistant_id = (row.get("assistant_id") or "").strip()
        if not assistant_id:
            continue
        runtime_id = (row.get("runtime_id") or "").strip() or assistant_id
        exists = bind.execute(
            sa.text(
                "SELECT 1 FROM gateway_runtime_instances WHERE runtime_id = :runtime_id LIMIT 1"
            ),
            {"runtime_id": runtime_id},
        ).first()
        if exists:
            continue
        bind.execute(
            sa.text(
                """
                INSERT INTO gateway_runtime_instances (
                    runtime_id, assistant_id, pod_name, internal_base_url, gateway_url,
                    status, capacity, in_flight, capabilities_json, drain_mode,
                    last_heartbeat_at, last_error, version, updated_at
                ) VALUES (
                    :runtime_id, :assistant_id, :pod_name, :internal_base_url, :gateway_url,
                    :status, :capacity, :in_flight, :capabilities_json, :drain_mode,
                    :last_heartbeat_at, :last_error, :version, :updated_at
                )
                """
            ),
            {
                "runtime_id": runtime_id,
                "assistant_id": assistant_id,
                "pod_name": row.get("pod_name"),
                "internal_base_url": row.get("internal_base_url"),
                "gateway_url": row.get("gateway_url"),
                "status": row.get("status"),
                "capacity": row.get("capacity"),
                "in_flight": row.get("in_flight"),
                "capabilities_json": row.get("capabilities_json"),
                "drain_mode": row.get("drain_mode"),
                "last_heartbeat_at": row.get("last_heartbeat_at"),
                "last_error": row.get("last_error"),
                "version": row.get("version"),
                "updated_at": row.get("updated_at"),
            },
        )


def _ensure_index(
    table_name: str,
    index_name: str,
    columns: list[str],
    *,
    unique: bool = False,
) -> None:
    inspector = inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return
    existing = list(inspector.get_indexes(table_name))
    existing.extend(inspector.get_unique_constraints(table_name))
    if any(item.get("name") == index_name for item in existing):
        return
    op.create_index(index_name, table_name, columns, unique=unique)


def _drop_index_if_exists(table_name: str, index_name: str) -> None:
    inspector = inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return
    existing = {item["name"] for item in inspector.get_indexes(table_name)}
    existing.update(item["name"] for item in inspector.get_unique_constraints(table_name))
    if index_name in existing:
        op.drop_index(index_name, table_name=table_name)
