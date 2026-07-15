from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.database import Base
from src.workspace_files.models import utc_now


class WorkspaceFileRow(Base):
    __tablename__ = "workspace_files"
    __table_args__ = (
        Index("ix_workspace_files_assistant_bucket", "assistant_id", "bucket"),
        Index("uq_workspace_files_active_path", "active_path_key", unique=True),
        Index("ix_workspace_files_owner_user_id", "owner_user_id"),
        Index("ix_workspace_files_source_file_id", "source_file_id"),
        Index("ix_workspace_files_storage_status", "storage_status"),
    )

    file_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    assistant_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    bucket: Mapped[str] = mapped_column(String(20), nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_role: Mapped[str] = mapped_column(String(20), nullable=False, default="input")
    source_file_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    owner_user_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    uploader_user_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    source_type: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    source_ref: Mapped[str] = mapped_column(Text, nullable=False, default="")
    active_path_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    storage_provider: Mapped[str] = mapped_column(String(64), nullable=False, default="local")
    storage_backend: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    storage_key: Mapped[str | None] = mapped_column(Text, nullable=True, default="")
    storage_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    storage_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    storage_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    object_key: Mapped[str] = mapped_column(Text, nullable=False, default="")
    object_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    checksum: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    scan_fingerprint: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    sync_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    sync_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    direction: Mapped[str] = mapped_column(String(20), nullable=False, default="inbound")
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    workspace_last_modified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


def workspace_file_active_path_key(
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
    payload = "\0".join((assistant_id, bucket, filename)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class WorkspaceFileEventRow(Base):
    __tablename__ = "workspace_file_events"
    __table_args__ = (
        Index("ix_workspace_file_events_assistant_created", "assistant_id", "created_at"),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    file_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    assistant_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    source_type: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    source_ref: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        index=True,
    )


class ExternalPushRequestRow(Base):
    __tablename__ = "external_push_requests"
    __table_args__ = (
        Index("ix_external_push_requests_approval_id", "approval_id"),
        Index("ix_external_push_requests_assistant_created", "assistant_id", "created_at"),
    )

    push_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    assistant_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(1024), nullable=False)
    bucket: Mapped[str] = mapped_column(String(20), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    storage_backend: Mapped[str] = mapped_column(String(32), nullable=False, default="local")
    storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    source_ref: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    approval_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    staged_path: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# 活跃态(pending/running)的 sync job 才参与去重唯一约束;终态置 NULL 释放约束,
# 模拟 SQLite 侧 `WHERE status IN ('pending','running')` 的部分唯一索引(MySQL 不支持部分索引)。
ACTIVE_SYNC_JOB_STATUSES = ("pending", "running")


def workspace_sync_job_active_idempotency_key(idempotency_key: str, status: str) -> str | None:
    """活跃态返回 idempotency_key 的 sha256(压到 64 字符),终态返回 None。

    照 workspace_file_active_path_key 的 NULL-释放范式:唯一索引只约束非 NULL 行,
    故同一 (assistant,scope,mode) 同时至多一个 pending/running job。
    """
    if status not in ACTIVE_SYNC_JOB_STATUSES:
        return None
    payload = idempotency_key.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class WorkspaceSyncJobRow(Base):
    __tablename__ = "workspace_sync_jobs"
    __table_args__ = (
        Index(
            "uq_workspace_sync_jobs_active_idempotency",
            "active_idempotency_key",
            unique=True,
        ),
        Index(
            "ix_workspace_sync_jobs_status_priority_created",
            "status",
            "priority",
            "created_at",
        ),
        Index(
            "ix_workspace_sync_jobs_assistant_status_created",
            "assistant_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_workspace_sync_jobs_mode_status_created",
            "mode",
            "status",
            "created_at",
        ),
        Index(
            "ix_workspace_sync_jobs_scope_status_created",
            "scope_kind",
            "scope_slug",
            "status",
            "created_at",
        ),
    )

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 长度覆盖 json([assistant_id≤255, scope_kind, scope_slug≤255, mode]) 的上限(实测 ~543)
    idempotency_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    active_idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    assistant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    scope_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_slug: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    mode: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    requested_from: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    scanned_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deleted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    orphan_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
