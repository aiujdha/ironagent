"""Repair workspace file legacy columns left behind by older branches.

Revision ID: 012_repair_workspace_file_legacy_defaults
Revises: 011_repair_go_gateway_task_schema
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "012_repair_workspace_file_legacy_defaults"
down_revision = "011_repair_go_gateway_task_schema"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _has_table("workspace_files"):
        return

    if not _has_column("workspace_files", "checksum"):
        op.add_column(
            "workspace_files",
            sa.Column(
                "checksum",
                sa.String(length=128),
                nullable=False,
                server_default="",
            ),
        )

    has_storage_provider = _has_column("workspace_files", "storage_provider")
    if not has_storage_provider:
        op.add_column(
            "workspace_files",
            sa.Column(
                "storage_provider",
                sa.String(length=255),
                nullable=False,
                server_default="",
            ),
        )

    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        op.alter_column(
            "workspace_files",
            "storage_provider",
            existing_type=sa.String(length=255),
            nullable=False,
            server_default="",
        )
        op.alter_column(
            "workspace_files",
            "checksum",
            existing_type=sa.String(length=128),
            nullable=False,
            server_default="",
        )

    if not _has_table("external_push_requests"):
        return
    push_columns = [
        ("storage_backend", sa.String(length=32), "local", False),
        ("storage_key", sa.Text(), None, True),
        ("storage_url", sa.Text(), None, True),
    ]
    for name, type_, default, nullable in push_columns:
        if _has_column("external_push_requests", name):
            continue
        kwargs = {"nullable": nullable}
        if default is not None:
            kwargs["server_default"] = default
        op.add_column("external_push_requests", sa.Column(name, type_, **kwargs))


def downgrade() -> None:
    # Repair-only migration. Keep the legacy compatibility column intact.
    pass
