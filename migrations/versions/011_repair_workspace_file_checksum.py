"""Repair workspace file checksum defaults.

Revision ID: 011_repair_workspace_file_checksum
Revises: 010_repair_workspace_file_storage_provider
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "011_repair_workspace_file_checksum"
down_revision = "010_repair_workspace_file_storage_provider"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _set_checksum_default() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("workspace_files") as batch_op:
            batch_op.alter_column(
                "checksum",
                existing_type=sa.String(length=128),
                nullable=False,
                server_default="",
            )
        return
    op.alter_column(
        "workspace_files",
        "checksum",
        existing_type=sa.String(length=128),
        nullable=False,
        server_default="",
    )


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
    if _has_column("workspace_files", "checksum_sha256"):
        op.execute(
            sa.text(
                """
                UPDATE workspace_files
                SET checksum = checksum_sha256
                WHERE (checksum IS NULL OR checksum = '')
                  AND checksum_sha256 IS NOT NULL
                  AND checksum_sha256 != ''
                """
        )
            )
    op.execute(sa.text("UPDATE workspace_files SET checksum = '' WHERE checksum IS NULL"))
    _set_checksum_default()


def downgrade() -> None:
    pass
