"""Add users, user_assistant_bindings, workspace_files, workspace_file_events, external_push_requests tables.

Revision ID: 002_user_bindings_and_files
Revises: 001_initial
"""

from alembic import op
import sqlalchemy as sa

revision = "002_user_bindings_and_files"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("user_id", sa.String(255), primary_key=True),
        sa.Column("display_name", sa.String(255), server_default=""),
        sa.Column("employee_id", sa.String(255), server_default=""),
        sa.Column("department", sa.String(255), server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "user_assistant_bindings",
        sa.Column("user_id", sa.String(255), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("assistant_id", sa.String(255), nullable=False),
        sa.Column("alias", sa.String(255), server_default=""),
        sa.Column("source_system", sa.String(255), server_default=""),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "assistant_id"),
    )
    op.create_index(
        "ix_user_assistant_bindings_assistant_id",
        "user_assistant_bindings",
        ["assistant_id"],
    )

    op.create_table(
        "workspace_files",
        sa.Column("file_id", sa.String(36), primary_key=True),
        sa.Column("assistant_id", sa.String(255), nullable=False),
        sa.Column("bucket", sa.String(20), nullable=False),
        sa.Column("filename", sa.String(1024), nullable=False),
        sa.Column("relative_path", sa.String(2048), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, server_default="0"),
        sa.Column("content_type", sa.String(255), server_default=""),
        sa.Column("uploader_user_id", sa.String(255), server_default=""),
        sa.Column("source_type", sa.String(255), server_default=""),
        sa.Column("source_ref", sa.String(1024), server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted", sa.Boolean, server_default=sa.false()),
    )
    op.create_index(
        "ix_workspace_files_assistant_id",
        "workspace_files",
        ["assistant_id"],
    )

    op.create_table(
        "workspace_file_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("file_id", sa.String(36), nullable=False),
        sa.Column("assistant_id", sa.String(255), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("user_id", sa.String(255), server_default=""),
        sa.Column("source_type", sa.String(255), server_default=""),
        sa.Column("source_ref", sa.String(1024), server_default=""),
        sa.Column("detail", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_workspace_file_events_file_id",
        "workspace_file_events",
        ["file_id"],
    )
    op.create_index(
        "ix_workspace_file_events_assistant_id",
        "workspace_file_events",
        ["assistant_id"],
    )

    op.create_table(
        "external_push_requests",
        sa.Column("push_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(255), server_default=""),
        sa.Column("assistant_id", sa.String(255), nullable=False),
        sa.Column("filename", sa.String(1024), nullable=False),
        sa.Column("bucket", sa.String(20), nullable=False),
        sa.Column("source_type", sa.String(255), server_default=""),
        sa.Column("source_ref", sa.String(1024), server_default=""),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("approval_id", sa.String(36), nullable=True),
        sa.Column("error", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_external_push_requests_assistant_id",
        "external_push_requests",
        ["assistant_id"],
    )


def downgrade() -> None:
    op.drop_table("external_push_requests")
    op.drop_table("workspace_file_events")
    op.drop_table("workspace_files")
    op.drop_table("user_assistant_bindings")
    op.drop_table("users")
