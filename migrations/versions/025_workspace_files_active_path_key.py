"""Add active workspace file path key.

Revision ID: 025_workspace_files_active_path_key
Revises: 024_gateway_conversation_routes
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "025_workspace_files_active_path_key"
down_revision: str | tuple[str, ...] | None = "024_gateway_conversation_routes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    if not inspector.has_table("workspace_files"):
        return
    if not _has_column("workspace_files", "active_path_key"):
        op.add_column(
            "workspace_files",
            sa.Column("active_path_key", sa.String(length=64), nullable=True),
        )
    _deduplicate_active_paths()
    _backfill_active_path_keys()
    _ensure_index(
        "workspace_files",
        "uq_workspace_files_active_path",
        ["active_path_key"],
        unique=True,
    )


def downgrade() -> None:
    _drop_index_if_exists("workspace_files", "uq_workspace_files_active_path")
    if _has_column("workspace_files", "active_path_key"):
        op.drop_column("workspace_files", "active_path_key")


def _deduplicate_active_paths() -> None:
    bind = op.get_bind()
    # 按【归一化路径键】(= _active_path_key / 唯一索引口径)去重,而非按原始 filename。
    # 读所有活跃行(updated_at DESC, file_id DESC),每个归一化 key 只留首条(最新),余者标 deleted。
    # 故原始不同但归一化后相同的活跃行('a.txt' vs '/a.txt'、'dir\\b.txt' vs 'dir/b.txt')也归
    # 同组、压成单行,避免随后 CREATE UNIQUE INDEX 撞同一 active_path_key(1062)。
    rows = bind.execute(
        sa.text(
            """
            SELECT file_id, assistant_id, bucket, filename
            FROM workspace_files
            WHERE COALESCE(deleted, 0) = 0
            ORDER BY updated_at DESC, file_id DESC
            """
        )
    ).mappings().all()
    now = datetime.now(UTC)
    seen_keys: set[str] = set()
    for row in rows:
        key = _active_path_key(
            str(row["assistant_id"] or ""),
            str(row["bucket"] or ""),
            str(row["filename"] or ""),
            deleted=False,
        )
        if key is None:
            # 归一化后为空 -> active_path_key 将为 NULL,不进唯一索引,无需去重。
            continue
        if key not in seen_keys:
            seen_keys.add(key)
            continue
        # 同一归一化路径已保留更新的一行,这一行作为重复被标记 deleted。
        bind.execute(
            sa.text(
                """
                UPDATE workspace_files
                SET
                    deleted = 1,
                    active_path_key = NULL,
                    sync_status = 'deleted_from_workspace',
                    storage_status = CASE
                        WHEN COALESCE(storage_status, '') IN ('', 'pending')
                        THEN 'deleted_from_workspace'
                        ELSE storage_status
                    END,
                    last_error = CASE
                        WHEN COALESCE(last_error, '') = ''
                        THEN 'duplicate active workspace file row superseded during active path migration'
                        ELSE last_error
                    END,
                    updated_at = :updated_at
                WHERE file_id = :file_id
                """
            ),
            {"file_id": row["file_id"], "updated_at": now},
        )


def _backfill_active_path_keys() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT file_id, assistant_id, bucket, filename, deleted
            FROM workspace_files
            """
        )
    ).mappings().all()
    for row in rows:
        bind.execute(
            sa.text(
                """
                UPDATE workspace_files
                SET active_path_key = :active_path_key
                WHERE file_id = :file_id
                """
            ),
            {
                "file_id": row["file_id"],
                "active_path_key": _active_path_key(
                    str(row["assistant_id"] or ""),
                    str(row["bucket"] or ""),
                    str(row["filename"] or ""),
                    deleted=bool(row["deleted"]),
                ),
            },
        )


def _active_path_key(
    assistant_id: str,
    bucket: str,
    filename: str,
    *,
    deleted: bool = False,
) -> str | None:
    if deleted:
        return None
    assistant_id = assistant_id.strip()
    bucket = bucket.strip()
    filename = filename.replace("\\", "/").strip().lstrip("/")
    if not assistant_id or not bucket or not filename:
        return None
    return hashlib.sha256("\0".join((assistant_id, bucket, filename)).encode("utf-8")).hexdigest()


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return False
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


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
