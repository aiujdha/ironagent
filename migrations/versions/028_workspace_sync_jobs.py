"""Create workspace_sync_jobs table for the MySQL metadata backend.

Revision ID: 028_workspace_sync_jobs
Revises: 027_gateway_queue_capacity_wait_state
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "028_workspace_sync_jobs"
down_revision: str | tuple[str, ...] | None = "027_gateway_queue_capacity_wait_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if not inspector.has_table("workspace_sync_jobs"):
        op.create_table(
            "workspace_sync_jobs",
            sa.Column("job_id", sa.String(length=36), primary_key=True, nullable=False),
            sa.Column("idempotency_key", sa.String(length=1024), nullable=False),
            # 活跃态(pending/running)= sha256(idempotency_key),终态置 NULL 释放唯一约束。
            # 替代 SQLite 侧的部分唯一索引(WHERE status IN ...),MySQL 不支持部分索引。
            sa.Column("active_idempotency_key", sa.String(length=64), nullable=True),
            sa.Column("assistant_id", sa.String(length=255), nullable=False),
            sa.Column("scope_kind", sa.String(length=64), nullable=False),
            sa.Column("scope_slug", sa.String(length=255), nullable=False),
            sa.Column("mode", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("priority", sa.Integer(), nullable=False),
            sa.Column("requested_by", sa.String(length=255), nullable=False),
            sa.Column("requested_from", sa.String(length=255), nullable=False),
            sa.Column("scanned_count", sa.Integer(), nullable=False),
            sa.Column("created_count", sa.Integer(), nullable=False),
            sa.Column("updated_count", sa.Integer(), nullable=False),
            sa.Column("missing_count", sa.Integer(), nullable=False),
            sa.Column("deleted_count", sa.Integer(), nullable=False),
            sa.Column("orphan_count", sa.Integer(), nullable=False),
            sa.Column("error_code", sa.String(length=64), nullable=False),
            sa.Column("error", sa.Text(), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    existing_indexes = {
        index["name"]
        for index in (
            inspector.get_indexes("workspace_sync_jobs")
            if inspector.has_table("workspace_sync_jobs")
            else []
        )
    }

    if "uq_workspace_sync_jobs_active_idempotency" not in existing_indexes:
        op.create_index(
            "uq_workspace_sync_jobs_active_idempotency",
            "workspace_sync_jobs",
            ["active_idempotency_key"],
            unique=True,
        )
    if "ix_workspace_sync_jobs_status_priority_created" not in existing_indexes:
        op.create_index(
            "ix_workspace_sync_jobs_status_priority_created",
            "workspace_sync_jobs",
            ["status", "priority", "created_at"],
        )
    if "ix_workspace_sync_jobs_assistant_status_created" not in existing_indexes:
        op.create_index(
            "ix_workspace_sync_jobs_assistant_status_created",
            "workspace_sync_jobs",
            ["assistant_id", "status", "created_at"],
        )
    if "ix_workspace_sync_jobs_mode_status_created" not in existing_indexes:
        op.create_index(
            "ix_workspace_sync_jobs_mode_status_created",
            "workspace_sync_jobs",
            ["mode", "status", "created_at"],
        )
    if "ix_workspace_sync_jobs_scope_status_created" not in existing_indexes:
        op.create_index(
            "ix_workspace_sync_jobs_scope_status_created",
            "workspace_sync_jobs",
            ["scope_kind", "scope_slug", "status", "created_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if inspector.has_table("workspace_sync_jobs"):
        op.drop_table("workspace_sync_jobs")
