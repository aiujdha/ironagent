"""Manage collaboration repository tables in Alembic."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "022_collaboration_repository_tables"
down_revision: str | tuple[str, ...] | None = "021_repair_legacy_assistant_ironagent_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if not inspector.has_table("shared_workspace_objects"):
        op.create_table(
            "shared_workspace_objects",
            sa.Column("id", sa.String(length=64), primary_key=True, nullable=False),
            sa.Column("object_type", sa.String(length=32), nullable=False),
            sa.Column("filename", sa.String(length=255), nullable=False),
            sa.Column("content_type", sa.String(length=128), nullable=True),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("sha256", sa.String(length=64), nullable=False),
            sa.Column("storage_backend", sa.String(length=32), nullable=False),
            sa.Column("storage_bucket", sa.String(length=128), nullable=False),
            sa.Column("storage_region", sa.String(length=64), nullable=False),
            sa.Column("storage_endpoint", sa.String(length=255), nullable=False),
            sa.Column("storage_key", sa.String(length=1024), nullable=False),
            sa.Column("storage_etag", sa.String(length=128), nullable=False),
            sa.Column("storage_version_id", sa.String(length=255), nullable=False),
            sa.Column("source_assistant_id", sa.String(length=64), nullable=False),
            sa.Column("source_workspace_file_id", sa.String(length=64), nullable=True),
            sa.Column("parent_object_id", sa.String(length=64), nullable=True),
            sa.Column("derived_from_task_id", sa.String(length=64), nullable=True),
            sa.Column("visibility_scope", sa.String(length=32), nullable=False),
            sa.Column("access_mode", sa.String(length=32), nullable=False),
            sa.Column("retention_policy", sa.String(length=32), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("created_by", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("idempotency_key", sa.String(length=128), nullable=True),
            sa.Column("meta_json", sa.JSON(), nullable=False),
        )

    if not inspector.has_table("shared_object_permissions"):
        op.create_table(
            "shared_object_permissions",
            sa.Column("id", sa.String(length=64), primary_key=True, nullable=False),
            sa.Column("object_id", sa.String(length=64), nullable=False),
            sa.Column("grantee_type", sa.String(length=32), nullable=False),
            sa.Column("grantee_id", sa.String(length=64), nullable=False),
            sa.Column("permission_type", sa.String(length=32), nullable=False),
            sa.Column("created_by", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
        )

    if not inspector.has_table("collaboration_tasks"):
        op.create_table(
            "collaboration_tasks",
            sa.Column("id", sa.String(length=64), primary_key=True, nullable=False),
            sa.Column("root_task_id", sa.String(length=64), nullable=True),
            sa.Column("parent_task_id", sa.String(length=64), nullable=True),
            sa.Column("depth", sa.Integer(), nullable=False),
            sa.Column("max_depth", sa.Integer(), nullable=False),
            sa.Column("source_assistant_id", sa.String(length=64), nullable=False),
            sa.Column("target_assistant_id", sa.String(length=64), nullable=False),
            sa.Column("input_object_id", sa.String(length=64), nullable=False),
            sa.Column("instruction", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("approval_mode", sa.String(length=32), nullable=False),
            sa.Column("approval_id", sa.String(length=64), nullable=True),
            sa.Column("result_object_id", sa.String(length=64), nullable=True),
            sa.Column("result_workspace_file_id", sa.String(length=64), nullable=True),
            sa.Column("idempotency_key", sa.String(length=128), nullable=True),
            sa.Column("budget_limit", sa.Integer(), nullable=True),
            sa.Column("timeout_seconds", sa.Integer(), nullable=False),
            sa.Column("created_by", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("failed_reason_code", sa.String(length=64), nullable=True),
            sa.Column("failed_reason_text", sa.Text(), nullable=True),
            sa.Column("meta_json", sa.JSON(), nullable=False),
        )

    if not inspector.has_table("collaboration_claims"):
        op.create_table(
            "collaboration_claims",
            sa.Column("id", sa.String(length=64), primary_key=True, nullable=False),
            sa.Column("task_id", sa.String(length=64), nullable=False),
            sa.Column("assistant_id", sa.String(length=64), nullable=False),
            sa.Column("claim_token", sa.String(length=128), nullable=False),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
        )

    if not inspector.has_table("collaboration_events"):
        op.create_table(
            "collaboration_events",
            sa.Column("id", sa.String(length=64), primary_key=True, nullable=False),
            sa.Column("task_id", sa.String(length=64), nullable=False),
            sa.Column("event_type", sa.String(length=64), nullable=False),
            sa.Column("actor_type", sa.String(length=32), nullable=False),
            sa.Column("actor_id", sa.String(length=64), nullable=False),
            sa.Column("detail_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )

    _ensure_column(
        "shared_workspace_objects",
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
    )
    _ensure_column(
        "collaboration_tasks",
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
    )
    _fail_if_duplicate_non_null("shared_workspace_objects", "idempotency_key")
    _fail_if_duplicate_non_null("collaboration_tasks", "idempotency_key")
    _fail_if_duplicate_non_null("collaboration_claims", "claim_token")
    _ensure_index(
        "shared_workspace_objects",
        "uq_shared_workspace_objects_idempotency_key",
        ["idempotency_key"],
        unique=True,
    )
    _ensure_index(
        "shared_object_permissions",
        "ix_shared_object_permissions_object_id",
        ["object_id"],
    )
    _ensure_index("collaboration_tasks", "ix_collaboration_tasks_root_task_id", ["root_task_id"])
    _ensure_index(
        "collaboration_tasks",
        "ix_collaboration_tasks_source_assistant_id",
        ["source_assistant_id"],
    )
    _ensure_index(
        "collaboration_tasks",
        "ix_collaboration_tasks_target_assistant_id",
        ["target_assistant_id"],
    )
    _ensure_index("collaboration_tasks", "ix_collaboration_tasks_status", ["status"])
    _ensure_index(
        "collaboration_tasks",
        "uq_collaboration_tasks_idempotency_key",
        ["idempotency_key"],
        unique=True,
    )
    _ensure_index("collaboration_claims", "ix_collaboration_claims_task_id", ["task_id"])
    _ensure_index(
        "collaboration_claims",
        "uq_collaboration_claims_claim_token",
        ["claim_token"],
        unique=True,
    )
    _ensure_index("collaboration_events", "ix_collaboration_events_task_id", ["task_id"])


def downgrade() -> None:
    for table_name, index_name in (
        ("collaboration_events", "ix_collaboration_events_task_id"),
        ("collaboration_claims", "uq_collaboration_claims_claim_token"),
        ("collaboration_claims", "ix_collaboration_claims_task_id"),
        ("collaboration_tasks", "uq_collaboration_tasks_idempotency_key"),
        ("collaboration_tasks", "ix_collaboration_tasks_status"),
        ("collaboration_tasks", "ix_collaboration_tasks_target_assistant_id"),
        ("collaboration_tasks", "ix_collaboration_tasks_source_assistant_id"),
        ("collaboration_tasks", "ix_collaboration_tasks_root_task_id"),
        ("shared_object_permissions", "ix_shared_object_permissions_object_id"),
        ("shared_workspace_objects", "uq_shared_workspace_objects_idempotency_key"),
    ):
        _drop_index_if_exists(table_name, index_name)


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
    indexes = list(inspector.get_indexes(table_name))
    constraints = list(inspector.get_unique_constraints(table_name))
    existing = [*indexes, *constraints]
    named = next((item for item in existing if item.get("name") == index_name), None)
    if named is not None:
        if not _index_definition_matches(named, columns, unique):
            raise RuntimeError(
                f"existing index {index_name} on {table_name} has unexpected definition: "
                f"expected columns={columns} unique={unique}, got {named}"
            )
        return
    if any(_index_definition_matches(item, columns, unique) for item in existing):
        return
    op.create_index(index_name, table_name, columns, unique=unique)


def _ensure_column(table_name: str, column: sa.Column) -> None:
    inspector = inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return
    existing = {item["name"] for item in inspector.get_columns(table_name)}
    if column.name in existing:
        return
    op.add_column(table_name, column)


def _drop_index_if_exists(table_name: str, index_name: str) -> None:
    inspector = inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return
    existing = {item["name"] for item in inspector.get_indexes(table_name)}
    existing.update(item["name"] for item in inspector.get_unique_constraints(table_name))
    if index_name not in existing:
        return
    op.drop_index(index_name, table_name=table_name)


def _index_definition_matches(item: dict[str, object], columns: list[str], unique: bool) -> bool:
    column_names = item.get("column_names") or item.get("columns") or []
    if list(column_names) != columns:
        return False
    if unique:
        return bool(item.get("unique", True))
    return True


def _fail_if_duplicate_non_null(table_name: str, column_name: str) -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table(table_name):
        return
    existing_columns = {item["name"] for item in inspector.get_columns(table_name)}
    if column_name not in existing_columns:
        return
    duplicate = bind.execute(
        sa.text(
            f"""
            SELECT {column_name}, COUNT(*) AS duplicate_count
            FROM {table_name}
            WHERE {column_name} IS NOT NULL AND {column_name} <> ''
            GROUP BY {column_name}
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if duplicate is None:
        return
    raise RuntimeError(
        f"cannot create unique index for {table_name}.{column_name}: duplicate value exists"
    )
