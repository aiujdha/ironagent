"""Backfill legacy workspace file storage schema to the current model.

Revision ID: 004_workspace_file_storage_metadata_legacy
Revises: 002_storage_repositories
"""

from alembic import op
import sqlalchemy as sa

revision = "004_workspace_file_storage_metadata_legacy"
down_revision = "002_storage_repositories"
branch_labels = None
depends_on = None


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return table_name in _inspector().get_table_names()


def _column_names(table_name: str) -> set[str]:
    return {column["name"] for column in _inspector().get_columns(table_name)}


def _has_column(table_name: str, column_name: str) -> bool:
    return column_name in _column_names(table_name)


def _has_index(table_name: str, index_name: str) -> bool:
    return any(index["name"] == index_name for index in _inspector().get_indexes(table_name))


def _set_not_null(table_name: str, column_name: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(column_name, existing_type=sa.Text(), nullable=False)
        return
    op.alter_column(table_name, column_name, existing_type=sa.Text(), nullable=False)


def _rename_column_if_needed(
    table_name: str,
    old_name: str,
    new_name: str,
    *,
    existing_type: sa.types.TypeEngine,
    existing_nullable: bool,
) -> None:
    columns = _column_names(table_name)
    if old_name not in columns or new_name in columns:
        return
    op.alter_column(
        table_name,
        old_name,
        new_column_name=new_name,
        existing_type=existing_type,
        existing_nullable=existing_nullable,
    )


def _add_required_text_column(table_name: str, column_name: str) -> None:
    if _has_column(table_name, column_name):
        return
    op.add_column(table_name, sa.Column(column_name, sa.Text(), nullable=True))
    op.execute(sa.text(f"UPDATE {table_name} SET {column_name} = '' WHERE {column_name} IS NULL"))
    _set_not_null(table_name, column_name)


def upgrade() -> None:
    if not _has_table("users"):
        op.create_table(
            "users",
            sa.Column("user_id", sa.String(255), primary_key=True),
            sa.Column("display_name", sa.String(255), server_default=""),
            sa.Column("employee_id", sa.String(255), server_default=""),
            sa.Column("department", sa.String(255), server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    if not _has_table("user_assistant_bindings"):
        op.create_table(
            "user_assistant_bindings",
            sa.Column("user_id", sa.String(255), nullable=False),
            sa.Column("assistant_id", sa.String(255), nullable=False),
            sa.Column("alias", sa.String(255), server_default=""),
            sa.Column("source_system", sa.String(255), server_default=""),
            sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("user_id", "assistant_id"),
        )
    if not _has_index("user_assistant_bindings", "ix_user_assistant_bindings_assistant_id"):
        op.create_index(
            "ix_user_assistant_bindings_assistant_id",
            "user_assistant_bindings",
            ["assistant_id"],
        )

    _rename_column_if_needed(
        "workspace_files",
        "storage_backend",
        "storage_provider",
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )
    _rename_column_if_needed(
        "workspace_files",
        "object_key",
        "storage_key",
        existing_type=sa.String(length=2048),
        existing_nullable=False,
    )
    _rename_column_if_needed(
        "workspace_files",
        "object_url",
        "storage_path",
        existing_type=sa.String(length=2048),
        existing_nullable=False,
    )
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
    op.drop_column("workspace_files", "source_file_id")
    op.drop_column("workspace_files", "file_role")
    op.alter_column(
        "workspace_files",
        "storage_path",
        new_column_name="object_url",
        existing_type=sa.String(length=2048),
        existing_nullable=False,
    )
    op.alter_column(
        "workspace_files",
        "storage_key",
        new_column_name="object_key",
        existing_type=sa.String(length=2048),
        existing_nullable=False,
    )
    op.alter_column(
        "workspace_files",
        "storage_provider",
        new_column_name="storage_backend",
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )

    op.drop_index("ix_user_assistant_bindings_assistant_id", table_name="user_assistant_bindings")
    op.drop_table("user_assistant_bindings")
    op.drop_table("users")
