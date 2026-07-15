"""Add workspace file storage metadata columns.

Revision ID: 003_workspace_file_storage_metadata
Revises: 002_user_bindings_and_files
"""

from alembic import op
import sqlalchemy as sa

revision = "003_workspace_file_storage_metadata"
down_revision = "002_user_bindings_and_files"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def _set_not_null(table_name: str, column_name: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(column_name, existing_type=sa.Text(), nullable=False)
        return
    op.alter_column(table_name, column_name, existing_type=sa.Text(), nullable=False)


def _add_required_text_column(table_name: str, column_name: str) -> None:
    if _has_column(table_name, column_name):
        return
    op.add_column(table_name, sa.Column(column_name, sa.Text(), nullable=True))
    op.execute(sa.text(f"UPDATE {table_name} SET {column_name} = '' WHERE {column_name} IS NULL"))
    _set_not_null(table_name, column_name)


def upgrade() -> None:
    if not _has_column("workspace_files", "file_role"):
        op.add_column(
            "workspace_files",
            sa.Column("file_role", sa.String(length=20), nullable=False, server_default="input"),
        )
    if not _has_column("workspace_files", "source_file_id"):
        op.add_column(
            "workspace_files",
            sa.Column("source_file_id", sa.String(length=36), nullable=True),
        )
    if not _has_column("workspace_files", "storage_provider"):
        op.add_column(
            "workspace_files",
            sa.Column("storage_provider", sa.String(length=64), nullable=False, server_default=""),
        )
    if not _has_column("workspace_files", "storage_key"):
        op.add_column(
            "workspace_files",
            sa.Column("storage_key", sa.String(length=1024), nullable=False, server_default=""),
        )
    if not _has_column("workspace_files", "storage_path"):
        op.add_column(
            "workspace_files",
            sa.Column("storage_path", sa.String(length=2048), nullable=False, server_default=""),
        )
    if not _has_column("workspace_files", "storage_status"):
        op.add_column(
            "workspace_files",
            sa.Column(
                "storage_status",
                sa.String(length=32),
                nullable=False,
                server_default="pending",
            ),
        )
    _add_required_text_column("workspace_files", "storage_error")
    if not _has_index("workspace_files", "ix_workspace_files_source_file_id"):
        op.create_index("ix_workspace_files_source_file_id", "workspace_files", ["source_file_id"])
    if not _has_index("workspace_files", "ix_workspace_files_storage_status"):
        op.create_index("ix_workspace_files_storage_status", "workspace_files", ["storage_status"])
    if not _has_index("workspace_files", "ix_workspace_files_assistant_bucket"):
        op.create_index("ix_workspace_files_assistant_bucket", "workspace_files", ["assistant_id", "bucket"])

    if not _has_column("external_push_requests", "content_type"):
        op.add_column(
            "external_push_requests",
            sa.Column("content_type", sa.String(length=255), nullable=False, server_default=""),
        )
    if not _has_column("external_push_requests", "staged_path"):
        op.add_column(
            "external_push_requests",
            sa.Column("staged_path", sa.String(length=2048), nullable=True),
        )
    if not _has_index("external_push_requests", "ix_external_push_requests_approval_id"):
        op.create_index("ix_external_push_requests_approval_id", "external_push_requests", ["approval_id"])
    if not _has_index("external_push_requests", "ix_external_push_requests_assistant_created"):
        op.create_index(
            "ix_external_push_requests_assistant_created",
            "external_push_requests",
            ["assistant_id", "created_at"],
        )
    if not _has_index("workspace_file_events", "ix_workspace_file_events_assistant_created"):
        op.create_index(
            "ix_workspace_file_events_assistant_created",
            "workspace_file_events",
            ["assistant_id", "created_at"],
        )


def downgrade() -> None:
    op.drop_index("ix_workspace_file_events_assistant_created", table_name="workspace_file_events")
    op.drop_index("ix_external_push_requests_assistant_created", table_name="external_push_requests")
    op.drop_index("ix_external_push_requests_approval_id", table_name="external_push_requests")
    op.drop_column("external_push_requests", "staged_path")
    op.drop_column("external_push_requests", "content_type")

    op.drop_index("ix_workspace_files_assistant_bucket", table_name="workspace_files")
    op.drop_index("ix_workspace_files_storage_status", table_name="workspace_files")
    op.drop_index("ix_workspace_files_source_file_id", table_name="workspace_files")
    op.drop_column("workspace_files", "storage_error")
    op.drop_column("workspace_files", "storage_status")
    op.drop_column("workspace_files", "storage_path")
    op.drop_column("workspace_files", "storage_key")
    op.drop_column("workspace_files", "storage_provider")
    op.drop_column("workspace_files", "source_file_id")
    op.drop_column("workspace_files", "file_role")
