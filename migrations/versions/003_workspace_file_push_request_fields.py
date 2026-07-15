"""Align external_push_requests columns with current workspace file models.

Revision ID: 003_workspace_file_push_request_fields
Revises: 002_user_bindings_and_files
"""

import sqlalchemy as sa
from alembic import op

revision = "003_workspace_file_push_request_fields"
down_revision = "002_user_bindings_and_files"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _has_column("external_push_requests", "content_type"):
        op.add_column(
            "external_push_requests",
            sa.Column("content_type", sa.String(255), nullable=False, server_default=""),
        )
    if not _has_column("external_push_requests", "staged_path"):
        op.add_column(
            "external_push_requests",
            sa.Column("staged_path", sa.String(2048), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("external_push_requests", "staged_path")
    op.drop_column("external_push_requests", "content_type")
