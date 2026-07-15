"""Workspace sync job persistence and runner helpers."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from sqlalchemy import CursorResult, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.shared.async_utils import run_sync
from src.shared.config import get_settings, resolve_workspace_file_metadata_backend
from src.shared.database import get_session_factory
from src.shared.logging import get_logger
from src.workspace_files.database_models import (
    WorkspaceSyncJobRow,
    workspace_sync_job_active_idempotency_key,
)
from src.workspace_files.models import utc_now
from src.workspace_files.sync_service import SYNC_BUCKETS, WorkspaceSyncService

log = get_logger(__name__)

WorkspaceSyncJobStatus = Literal[
    "pending",
    "running",
    "succeeded",
    "failed",
    "cancelled",
]
WorkspaceSyncJobMode = Literal["index_only", "reconcile", "cos_import"]

ACTIVE_JOB_STATUSES: tuple[str, ...] = ("pending", "running")
TERMINAL_JOB_STATUSES: tuple[str, ...] = ("succeeded", "failed", "cancelled")
DEFAULT_WORKSPACE_SYNC_JOBS_DB = Path("./data/workspace_sync_jobs.db")
MAINTENANCE_JOB_SCOPE_KINDS: tuple[str, ...] = (
    "assistant",
    "bucket",
    "capability",
    "all_visible",
)


class UnsupportedWorkspaceSyncJob(ValueError):
    """Raised when Phase 1 does not support the requested sync job."""


@dataclass(slots=True)
class WorkspaceSyncJobCreate:
    assistant_id: str
    scope_kind: str = "assistant"
    scope_slug: str = ""
    mode: WorkspaceSyncJobMode = "index_only"
    requested_by: str = "admin-web"
    requested_from: str = ""
    priority: int = 100


@dataclass(slots=True)
class WorkspaceSyncJob:
    job_id: str
    idempotency_key: str
    assistant_id: str
    scope_kind: str
    scope_slug: str
    mode: str
    status: WorkspaceSyncJobStatus
    priority: int
    requested_by: str
    requested_from: str
    scanned_count: int
    created_count: int
    updated_count: int
    missing_count: int
    deleted_count: int
    orphan_count: int
    error_code: str
    error: str
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class WorkspaceSyncJobStore:
    _schema_lock = threading.Lock()
    _initialized_paths: set[Path] = set()

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or get_settings().workspace_sync_job_sqlite_path
        self._ensure_schema()

    def create_or_get_job(
        self,
        assistant_id: str,
        *,
        scope_kind: str = "assistant",
        scope_slug: str = "",
        mode: str = "index_only",
        requested_by: str = "admin-web",
        requested_from: str = "",
        priority: int = 100,
    ) -> WorkspaceSyncJob:
        create = WorkspaceSyncJobCreate(
            assistant_id=assistant_id,
            scope_kind=scope_kind,
            scope_slug=scope_slug,
            mode=_validate_mode(mode),
            requested_by=requested_by,
            requested_from=requested_from,
            priority=priority,
        )
        _validate_scope(create.mode, create.scope_kind, create.scope_slug)
        idempotency_key = _build_idempotency_key(create)

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._get_active_job_row(conn, idempotency_key)
            if row is not None:
                return _job_from_row(row)

            now = utc_now()
            job_id = str(uuid4())
            conn.execute(
                """
                INSERT INTO workspace_sync_jobs (
                    job_id, idempotency_key, assistant_id, scope_kind, scope_slug,
                    mode, status, priority, requested_by, requested_from,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    idempotency_key,
                    create.assistant_id,
                    create.scope_kind,
                    create.scope_slug,
                    create.mode,
                    create.priority,
                    create.requested_by,
                    create.requested_from,
                    _to_db_time(now),
                    _to_db_time(now),
                ),
            )
            row = self._get_job_row(conn, job_id)
            if row is None:  # pragma: no cover - inserted row should be readable
                raise RuntimeError("created workspace sync job could not be read")
            return _job_from_row(row)

    def get_job(self, job_id: str) -> WorkspaceSyncJob | None:
        with self.connect(read_only=True) as conn:
            row = self._get_job_row(conn, job_id)
        return _job_from_row(row) if row is not None else None

    def mark_running(self, job_id: str) -> WorkspaceSyncJob | None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._get_job_row(conn, job_id)
            if row is None:
                return None
            job = _job_from_row(row)
            if job.status == "pending":
                started_at = job.started_at or now
                conn.execute(
                    """
                    UPDATE workspace_sync_jobs
                    SET status = 'running', started_at = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (_to_db_time(started_at), _to_db_time(now), job_id),
                )
                row = self._get_job_row(conn, job_id)
        return _job_from_row(row) if row is not None else None

    def claim_next_pending(
        self,
        *,
        mode: str = "index_only",
    ) -> WorkspaceSyncJob | None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM workspace_sync_jobs
                WHERE status = 'pending' AND mode = ?
                ORDER BY priority ASC, created_at ASC
                LIMIT 1
                """,
                (mode,),
            ).fetchone()
            if row is None:
                return None
            job_id = row["job_id"]
            cursor = conn.execute(
                """
                UPDATE workspace_sync_jobs
                SET status = 'running', started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE job_id = ? AND status = 'pending'
                """,
                (_to_db_time(now), _to_db_time(now), job_id),
            )
            if cursor.rowcount != 1:
                return None
            row = self._get_job_row(conn, job_id)
        return _job_from_row(row) if row is not None else None

    def mark_succeeded(
        self,
        job_id: str,
        *,
        scanned_count: int = 0,
        created_count: int = 0,
        updated_count: int = 0,
        missing_count: int = 0,
        deleted_count: int = 0,
        orphan_count: int = 0,
    ) -> WorkspaceSyncJob | None:
        return self._mark_terminal(
            job_id,
            status="succeeded",
            scanned_count=scanned_count,
            created_count=created_count,
            updated_count=updated_count,
            missing_count=missing_count,
            deleted_count=deleted_count,
            orphan_count=orphan_count,
        )

    def mark_failed(
        self,
        job_id: str,
        *,
        error: str,
        error_code: str = "workspace_sync_failed",
    ) -> WorkspaceSyncJob | None:
        return self._mark_terminal(
            job_id,
            status="failed",
            error=error,
            error_code=error_code,
        )

    def mark_cancelled(self, job_id: str, *, error: str = "") -> WorkspaceSyncJob | None:
        return self._mark_terminal(job_id, status="cancelled", error=error)

    def connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            uri = f"file:{self.db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        self._configure_connection(conn, read_only=read_only)
        return conn

    def _ensure_schema(self) -> None:
        resolved = self.db_path.resolve()
        with self._schema_lock:
            if resolved in self._initialized_paths:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as conn:
                conn.executescript(_SCHEMA_SQL)
            self._initialized_paths.add(resolved)

    def _configure_connection(self, conn: sqlite3.Connection, *, read_only: bool) -> None:
        conn.execute("PRAGMA busy_timeout=30000")
        if not read_only:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")

    def _get_active_job_row(
        self,
        conn: sqlite3.Connection,
        idempotency_key: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT * FROM workspace_sync_jobs
            WHERE idempotency_key = ?
              AND status IN ('pending', 'running')
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (idempotency_key,),
        ).fetchone()

    def _get_job_row(self, conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM workspace_sync_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()

    def _mark_terminal(
        self,
        job_id: str,
        *,
        status: WorkspaceSyncJobStatus,
        scanned_count: int = 0,
        created_count: int = 0,
        updated_count: int = 0,
        missing_count: int = 0,
        deleted_count: int = 0,
        orphan_count: int = 0,
        error_code: str = "",
        error: str = "",
    ) -> WorkspaceSyncJob | None:
        if status not in TERMINAL_JOB_STATUSES:
            raise ValueError(f"unsupported terminal workspace sync status: {status}")
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._get_job_row(conn, job_id)
            if row is None:
                return None
            conn.execute(
                """
                UPDATE workspace_sync_jobs
                SET status = ?, scanned_count = ?, created_count = ?,
                    updated_count = ?, missing_count = ?, deleted_count = ?,
                    orphan_count = ?, error_code = ?, error = ?,
                    finished_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (
                    status,
                    scanned_count,
                    created_count,
                    updated_count,
                    missing_count,
                    deleted_count,
                    orphan_count,
                    error_code,
                    error,
                    _to_db_time(now),
                    _to_db_time(now),
                    job_id,
                ),
            )
            row = self._get_job_row(conn, job_id)
        return _job_from_row(row) if row is not None else None


def _ensure_aware(value: datetime | None) -> datetime | None:
    """MySQL DATETIME 无时区,读回为 naive;补 UTC 使其与 SQLite store(恒 tz-aware)产出一致。"""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _job_from_orm(row: WorkspaceSyncJobRow) -> WorkspaceSyncJob:
    return WorkspaceSyncJob(
        job_id=row.job_id,
        idempotency_key=row.idempotency_key,
        assistant_id=row.assistant_id,
        scope_kind=row.scope_kind,
        scope_slug=row.scope_slug,
        mode=row.mode,
        status=row.status,  # type: ignore[arg-type]
        priority=row.priority,
        requested_by=row.requested_by,
        requested_from=row.requested_from,
        scanned_count=row.scanned_count,
        created_count=row.created_count,
        updated_count=row.updated_count,
        missing_count=row.missing_count,
        deleted_count=row.deleted_count,
        orphan_count=row.orphan_count,
        error_code=row.error_code,
        error=row.error,
        started_at=_ensure_aware(row.started_at),
        finished_at=_ensure_aware(row.finished_at),
        created_at=_ensure_aware(row.created_at) or utc_now(),
        updated_at=_ensure_aware(row.updated_at) or utc_now(),
    )


class DatabaseWorkspaceSyncJobStore:
    """MySQL/SQLAlchemy 后端 sync job store,镜像 WorkspaceSyncJobStore 的同步接口。

    同步方法经 run_sync 包装异步实现(与 DatabaseWorkspaceFileRepository 一致)。
    活跃去重(同一 assistant+scope+mode 至多一个 pending/running)靠
    active_idempotency_key 唯一索引:活跃态=sha256(idempotency_key),终态置 NULL 释放约束。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        *,
        stale_running_after_seconds: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._stale_running_after_seconds = stale_running_after_seconds

    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory or get_session_factory()

    def stale_running_after_seconds(self) -> int:
        if self._stale_running_after_seconds is not None:
            return self._stale_running_after_seconds
        return get_settings().workspace_sync_job_stale_after_seconds

    def create_or_get_job(
        self,
        assistant_id: str,
        *,
        scope_kind: str = "assistant",
        scope_slug: str = "",
        mode: str = "index_only",
        requested_by: str = "admin-web",
        requested_from: str = "",
        priority: int = 100,
    ) -> WorkspaceSyncJob:
        return run_sync(
            self.acreate_or_get_job(
                assistant_id,
                scope_kind=scope_kind,
                scope_slug=scope_slug,
                mode=mode,
                requested_by=requested_by,
                requested_from=requested_from,
                priority=priority,
            )
        )

    def get_job(self, job_id: str) -> WorkspaceSyncJob | None:
        return run_sync(self.aget_job(job_id))

    def mark_running(self, job_id: str) -> WorkspaceSyncJob | None:
        return run_sync(self.amark_running(job_id))

    def claim_next_pending(self, *, mode: str = "index_only") -> WorkspaceSyncJob | None:
        return run_sync(self.aclaim_next_pending(mode=mode))

    def mark_succeeded(
        self,
        job_id: str,
        *,
        scanned_count: int = 0,
        created_count: int = 0,
        updated_count: int = 0,
        missing_count: int = 0,
        deleted_count: int = 0,
        orphan_count: int = 0,
    ) -> WorkspaceSyncJob | None:
        return run_sync(
            self.amark_terminal(
                job_id,
                status="succeeded",
                scanned_count=scanned_count,
                created_count=created_count,
                updated_count=updated_count,
                missing_count=missing_count,
                deleted_count=deleted_count,
                orphan_count=orphan_count,
            )
        )

    def mark_failed(
        self,
        job_id: str,
        *,
        error: str,
        error_code: str = "workspace_sync_failed",
    ) -> WorkspaceSyncJob | None:
        return run_sync(
            self.amark_terminal(job_id, status="failed", error=error, error_code=error_code)
        )

    def mark_cancelled(self, job_id: str, *, error: str = "") -> WorkspaceSyncJob | None:
        return run_sync(self.amark_terminal(job_id, status="cancelled", error=error))

    async def acreate_or_get_job(
        self,
        assistant_id: str,
        *,
        scope_kind: str = "assistant",
        scope_slug: str = "",
        mode: str = "index_only",
        requested_by: str = "admin-web",
        requested_from: str = "",
        priority: int = 100,
    ) -> WorkspaceSyncJob:
        create = WorkspaceSyncJobCreate(
            assistant_id=assistant_id,
            scope_kind=scope_kind,
            scope_slug=scope_slug,
            mode=_validate_mode(mode),
            requested_by=requested_by,
            requested_from=requested_from,
            priority=priority,
        )
        _validate_scope(create.mode, create.scope_kind, create.scope_slug)
        idempotency_key = _build_idempotency_key(create)
        active_key = workspace_sync_job_active_idempotency_key(idempotency_key, "pending")
        async with self.session_factory()() as session:
            now = utc_now()
            await self._release_stale_running_rows(
                session,
                now,
                idempotency_key=idempotency_key,
                active_key=active_key,
            )
            existing = await self._get_active_row(session, idempotency_key, active_key)
            if existing is not None:
                return _job_from_orm(existing)
            row = WorkspaceSyncJobRow(
                job_id=str(uuid4()),
                idempotency_key=idempotency_key,
                active_idempotency_key=active_key,
                assistant_id=create.assistant_id,
                scope_kind=create.scope_kind,
                scope_slug=create.scope_slug,
                mode=create.mode,
                status="pending",
                priority=create.priority,
                requested_by=create.requested_by,
                requested_from=create.requested_from,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                # 并发对手已插入相同活跃 job;回滚后重查返回它(镜像 asave_file 的去重范式)
                await session.rollback()
                existing = await self._get_active_row(session, idempotency_key, active_key)
                if existing is not None:
                    return _job_from_orm(existing)
                raise
            return _job_from_orm(row)

    async def aget_job(self, job_id: str) -> WorkspaceSyncJob | None:
        async with self.session_factory()() as session:
            row = await session.get(WorkspaceSyncJobRow, job_id)
            return _job_from_orm(row) if row is not None else None

    async def amark_running(self, job_id: str) -> WorkspaceSyncJob | None:
        async with self.session_factory()() as session:
            async with session.begin():
                row = await session.get(WorkspaceSyncJobRow, job_id)
                if row is None:
                    return None
                if row.status == "pending":
                    now = utc_now()
                    row.status = "running"
                    row.started_at = row.started_at or now
                    row.updated_at = now
            return _job_from_orm(row)

    async def aclaim_next_pending(self, *, mode: str = "index_only") -> WorkspaceSyncJob | None:
        async with self.session_factory()() as session:
            while True:
                async with session.begin():
                    await self._release_stale_running_rows(
                        session,
                        utc_now(),
                        mode=mode,
                    )
                    row = await self._next_pending_row(session, mode=mode)
                    if row is None:
                        return None
                    now = utc_now()
                    claimed = await self._claim_pending_row(session, row, now)
                    if not claimed:
                        continue
                    await session.refresh(row)
                    return _job_from_orm(row)

    async def amark_terminal(
        self,
        job_id: str,
        *,
        status: WorkspaceSyncJobStatus,
        scanned_count: int = 0,
        created_count: int = 0,
        updated_count: int = 0,
        missing_count: int = 0,
        deleted_count: int = 0,
        orphan_count: int = 0,
        error_code: str = "",
        error: str = "",
    ) -> WorkspaceSyncJob | None:
        if status not in TERMINAL_JOB_STATUSES:
            raise ValueError(f"unsupported terminal workspace sync status: {status}")
        async with self.session_factory()() as session:
            async with session.begin():
                row = await session.get(WorkspaceSyncJobRow, job_id)
                if row is None:
                    return None
                now = utc_now()
                row.status = status
                row.scanned_count = scanned_count
                row.created_count = created_count
                row.updated_count = updated_count
                row.missing_count = missing_count
                row.deleted_count = deleted_count
                row.orphan_count = orphan_count
                row.error_code = error_code
                row.error = error
                row.finished_at = now
                row.updated_at = now
                # 终态释放活跃唯一约束,允许同 scope 再次创建
                row.active_idempotency_key = None
            return _job_from_orm(row)

    async def amark_succeeded(
        self,
        job_id: str,
        *,
        scanned_count: int = 0,
        created_count: int = 0,
        updated_count: int = 0,
        missing_count: int = 0,
        deleted_count: int = 0,
        orphan_count: int = 0,
    ) -> WorkspaceSyncJob | None:
        return await self.amark_terminal(
            job_id,
            status="succeeded",
            scanned_count=scanned_count,
            created_count=created_count,
            updated_count=updated_count,
            missing_count=missing_count,
            deleted_count=deleted_count,
            orphan_count=orphan_count,
        )

    async def amark_failed(
        self,
        job_id: str,
        *,
        error: str,
        error_code: str = "workspace_sync_failed",
    ) -> WorkspaceSyncJob | None:
        return await self.amark_terminal(
            job_id, status="failed", error=error, error_code=error_code
        )

    async def amark_cancelled(self, job_id: str, *, error: str = "") -> WorkspaceSyncJob | None:
        return await self.amark_terminal(job_id, status="cancelled", error=error)

    async def _get_active_row(
        self,
        session: AsyncSession,
        idempotency_key: str,
        active_key: str | None,
    ) -> WorkspaceSyncJobRow | None:
        if active_key is None:
            return None
        return await session.scalar(
            select(WorkspaceSyncJobRow)
            .where(
                WorkspaceSyncJobRow.active_idempotency_key == active_key,
                WorkspaceSyncJobRow.idempotency_key == idempotency_key,
            )
            .order_by(WorkspaceSyncJobRow.created_at.asc())
            .limit(1)
        )

    async def _release_stale_running_rows(
        self,
        session: AsyncSession,
        now: datetime,
        *,
        idempotency_key: str | None = None,
        active_key: str | None = None,
        mode: str | None = None,
    ) -> None:
        stale_after_seconds = self.stale_running_after_seconds()
        if stale_after_seconds <= 0:
            return
        cutoff = now - timedelta(seconds=stale_after_seconds)
        conditions = [
            WorkspaceSyncJobRow.status == "running",
            WorkspaceSyncJobRow.updated_at < cutoff,
        ]
        if idempotency_key is not None:
            conditions.append(WorkspaceSyncJobRow.idempotency_key == idempotency_key)
        if active_key is not None:
            conditions.append(WorkspaceSyncJobRow.active_idempotency_key == active_key)
        if mode is not None:
            conditions.append(WorkspaceSyncJobRow.mode == mode)
        await session.execute(
            update(WorkspaceSyncJobRow)
            .where(*conditions)
            .values(
                status="failed",
                active_idempotency_key=None,
                error_code="workspace_sync_stale_running",
                error=(
                    "workspace sync job was running past "
                    f"{stale_after_seconds}s without heartbeat"
                ),
                finished_at=now,
                updated_at=now,
            )
        )

    async def _next_pending_row(
        self,
        session: AsyncSession,
        *,
        mode: str,
    ) -> WorkspaceSyncJobRow | None:
        return await session.scalar(
            select(WorkspaceSyncJobRow)
            .where(
                WorkspaceSyncJobRow.status == "pending",
                WorkspaceSyncJobRow.mode == mode,
            )
            .order_by(
                WorkspaceSyncJobRow.priority.asc(),
                WorkspaceSyncJobRow.created_at.asc(),
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )

    async def _claim_pending_row(
        self,
        session: AsyncSession,
        row: WorkspaceSyncJobRow,
        now: datetime,
    ) -> bool:
        # 条件 UPDATE + rowcount 乐观认领:并发对手已认领则 rowcount=0,调用方重取下一条。
        result = await session.execute(
            update(WorkspaceSyncJobRow)
            .where(
                WorkspaceSyncJobRow.job_id == row.job_id,
                WorkspaceSyncJobRow.status == "pending",
            )
            .values(
                status="running",
                started_at=row.started_at or now,
                updated_at=now,
            )
        )
        return cast(CursorResult[Any], result).rowcount == 1


def build_workspace_sync_job_store() -> WorkspaceSyncJobStore | DatabaseWorkspaceSyncJobStore:
    """按 workspace 文件元数据后端 flag 路由 sync job store(与文件元数据同一开关、同批切换)。"""
    if resolve_workspace_file_metadata_backend() == "mysql":
        return DatabaseWorkspaceSyncJobStore()
    return WorkspaceSyncJobStore()


WorkspaceSyncJobStoreBackend = WorkspaceSyncJobStore | DatabaseWorkspaceSyncJobStore


class WorkspaceSyncJobService:
    def __init__(self, *, store: WorkspaceSyncJobStoreBackend | None = None) -> None:
        self.store = store or build_workspace_sync_job_store()

    def create_or_get_job(
        self,
        assistant_id: str,
        *,
        scope_kind: str = "assistant",
        scope_slug: str = "",
        mode: str = "index_only",
        requested_by: str = "admin-web",
        requested_from: str = "",
        priority: int = 100,
    ) -> WorkspaceSyncJob:
        return self.store.create_or_get_job(
            assistant_id,
            scope_kind=scope_kind,
            scope_slug=scope_slug,
            mode=mode,
            requested_by=requested_by,
            requested_from=requested_from,
            priority=_normalize_priority(priority),
        )

    def create_or_reuse_job(
        self,
        assistant_id: str,
        *,
        scope_kind: str = "assistant",
        scope_slug: str = "",
        mode: str = "index_only",
        requested_by: str = "admin-web",
        requested_from: str = "",
        priority: int | str = 100,
        reason: str = "",
        idempotency_key: str | None = None,
    ) -> WorkspaceSyncJob:
        del reason, idempotency_key
        return self.create_or_get_job(
            assistant_id,
            scope_kind=scope_kind,
            scope_slug=scope_slug,
            mode=mode,
            requested_by=requested_by,
            requested_from=requested_from,
            priority=_normalize_priority(priority),
        )

    def get_job(self, job_id: str) -> WorkspaceSyncJob | None:
        return self.store.get_job(job_id)

    async def _astore(
        self, sync_name: str, async_name: str, *args: object, **kwargs: object
    ) -> WorkspaceSyncJob | None:
        """从 async 上下文调用 store:有原生异步方法则 await,否则 to_thread 跑同步方法。

        避免对 DatabaseWorkspaceSyncJobStore 在运行 loop 内触发 run_sync(会 raise);
        对 SQLite store 则把阻塞调用挪到线程,不卡事件循环。
        """
        async_method = getattr(self.store, async_name, None)
        if async_method is not None:
            return await async_method(*args, **kwargs)
        sync_method = getattr(self.store, sync_name)
        return await asyncio.to_thread(lambda: sync_method(*args, **kwargs))

    async def run_index_only_job(
        self,
        job_id: str,
        *,
        sync_service_factory: Callable[[], WorkspaceSyncService] | None = None,
    ) -> WorkspaceSyncJob | None:
        job = await self._astore("mark_running", "amark_running", job_id)
        if job is None:
            return None
        if job.status in TERMINAL_JOB_STATUSES:
            return job
        return await self._run_claimed_index_only_job(
            job,
            sync_service_factory=sync_service_factory,
        )

    async def run_next_pending_index_only_job(
        self,
        *,
        sync_service_factory: Callable[[], WorkspaceSyncService] | None = None,
    ) -> WorkspaceSyncJob | None:
        job = await self._astore(
            "claim_next_pending", "aclaim_next_pending", mode="index_only"
        )
        if job is None:
            return None
        return await self._run_claimed_index_only_job(
            job,
            sync_service_factory=sync_service_factory,
        )

    async def _run_claimed_index_only_job(
        self,
        job: WorkspaceSyncJob,
        *,
        sync_service_factory: Callable[[], WorkspaceSyncService] | None = None,
    ) -> WorkspaceSyncJob | None:
        if job.mode != "index_only":
            return await self._astore(
                "mark_failed",
                "amark_failed",
                job.job_id,
                error=f"unsupported workspace sync runner for mode: {job.mode}",
                error_code="unsupported_runner",
            )

        try:
            sync_service = (
                sync_service_factory() if sync_service_factory else WorkspaceSyncService()
            )
            stats = await sync_service.async_sync_assistant_workspace_scopes(
                job.assistant_id,
                _scopes_for_job(job),
                mark_missing=True,
            )
            if getattr(stats, "metadata_degraded", False):
                return await self._astore(
                    "mark_failed",
                    "amark_failed",
                    job.job_id,
                    error="workspace sync metadata repository degraded",
                    error_code="workspace_sync_metadata_degraded",
                )
            await _backfill_workspace_items_for_job(job.assistant_id, sync_service)
            return await self._astore(
                "mark_succeeded",
                "amark_succeeded",
                job.job_id,
                scanned_count=stats.scanned_files,
                created_count=stats.created,
                updated_count=stats.updated,
                missing_count=stats.drifted,
                deleted_count=stats.removed,
            )
        except Exception as exc:  # pragma: no cover - exact failures are integration-owned
            log.warning(
                "workspace_sync_job_failed",
                job_id=job.job_id,
                assistant_id=job.assistant_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return await self._astore(
                "mark_failed",
                "amark_failed",
                job.job_id,
                error=f"{type(exc).__name__}: {exc}",
            )


def create_workspace_sync_job(
    assistant_id: str,
    *,
    scope_kind: str = "assistant",
    scope_slug: str = "",
    mode: str = "index_only",
    requested_by: str = "admin-web",
    requested_from: str = "",
    priority: int = 100,
    store: WorkspaceSyncJobStoreBackend | None = None,
) -> WorkspaceSyncJob:
    service = WorkspaceSyncJobService(store=store)
    return service.create_or_get_job(
        assistant_id,
        scope_kind=scope_kind,
        scope_slug=scope_slug,
        mode=mode,
        requested_by=requested_by,
        requested_from=requested_from,
        priority=priority,
    )


async def run_index_only_job(
    job_id: str,
    *,
    sync_service_factory: Callable[[], WorkspaceSyncService] | None = None,
    store: WorkspaceSyncJobStoreBackend | None = None,
) -> WorkspaceSyncJob | None:
    service = WorkspaceSyncJobService(store=store)
    return await service.run_index_only_job(
        job_id,
        sync_service_factory=sync_service_factory,
    )


async def run_next_pending_index_only_job(
    *,
    sync_service_factory: Callable[[], WorkspaceSyncService] | None = None,
    store: WorkspaceSyncJobStoreBackend | None = None,
) -> WorkspaceSyncJob | None:
    service = WorkspaceSyncJobService(store=store)
    return await service.run_next_pending_index_only_job(
        sync_service_factory=sync_service_factory,
    )


def _validate_mode(mode: str) -> WorkspaceSyncJobMode:
    if mode == "index_only":
        return "index_only"
    if mode == "reconcile":
        return "reconcile"
    if mode == "cos_import":
        return "cos_import"
    raise UnsupportedWorkspaceSyncJob(f"unsupported workspace sync mode for Phase 1: {mode}")


def _normalize_priority(value: int | str) -> int:
    if isinstance(value, int):
        return value
    cleaned = value.strip().lower()
    if cleaned == "high":
        return 50
    if cleaned == "low":
        return 150
    if cleaned in {"", "normal"}:
        return 100
    return int(cleaned)


def _validate_scope(mode: str, scope_kind: str, scope_slug: str) -> None:
    if mode == "index_only":
        if scope_kind == "assistant" and not scope_slug:
            return
        raise UnsupportedWorkspaceSyncJob(
            "Phase 1 only supports assistant-scoped index_only workspace sync jobs"
        )
    if mode in {"reconcile", "cos_import"} and scope_kind in MAINTENANCE_JOB_SCOPE_KINDS:
        if scope_kind == "all_visible" and scope_slug:
            raise UnsupportedWorkspaceSyncJob("all_visible workspace sync scope cannot have slug")
        return
    raise UnsupportedWorkspaceSyncJob(f"unsupported workspace sync scope: {scope_kind}")


def _build_idempotency_key(create: WorkspaceSyncJobCreate) -> str:
    return json.dumps(
        [
            create.assistant_id,
            create.scope_kind,
            create.scope_slug,
            create.mode,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _scopes_for_job(job: WorkspaceSyncJob) -> list[tuple[str, str]]:
    _validate_scope(job.mode, job.scope_kind, job.scope_slug)
    return [(bucket, "") for bucket in SYNC_BUCKETS]


async def _backfill_workspace_items_for_job(
    assistant_id: str,
    sync_service: WorkspaceSyncService,
) -> None:
    repository = getattr(sync_service, "repository", None)
    if repository is None:
        return
    from src.workspace_files.item_backfill import backfill_workspace_item_index
    from src.workspace_files.item_index import WorkspaceItemIndexStore

    await backfill_workspace_item_index(
        file_repository=repository,
        item_index_store=WorkspaceItemIndexStore(),
        assistant_id=assistant_id,
        prune_missing=True,
    )


def _to_db_time(value: datetime) -> str:
    return value.isoformat()


def _from_db_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _job_from_row(row: sqlite3.Row) -> WorkspaceSyncJob:
    return WorkspaceSyncJob(
        job_id=row["job_id"],
        idempotency_key=row["idempotency_key"],
        assistant_id=row["assistant_id"],
        scope_kind=row["scope_kind"],
        scope_slug=row["scope_slug"],
        mode=row["mode"],
        status=row["status"],
        priority=row["priority"],
        requested_by=row["requested_by"],
        requested_from=row["requested_from"],
        scanned_count=row["scanned_count"],
        created_count=row["created_count"],
        updated_count=row["updated_count"],
        missing_count=row["missing_count"],
        deleted_count=row["deleted_count"],
        orphan_count=row["orphan_count"],
        error_code=row["error_code"],
        error=row["error"],
        started_at=_from_db_time(row["started_at"]),
        finished_at=_from_db_time(row["finished_at"]),
        created_at=_from_db_time(row["created_at"]) or utc_now(),
        updated_at=_from_db_time(row["updated_at"]) or utc_now(),
    )


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workspace_sync_jobs (
    job_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    assistant_id TEXT NOT NULL,
    scope_kind TEXT NOT NULL,
    scope_slug TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    requested_by TEXT NOT NULL DEFAULT '',
    requested_from TEXT NOT NULL DEFAULT '',
    scanned_count INTEGER NOT NULL DEFAULT 0,
    created_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    missing_count INTEGER NOT NULL DEFAULT 0,
    deleted_count INTEGER NOT NULL DEFAULT 0,
    orphan_count INTEGER NOT NULL DEFAULT 0,
    error_code TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_sync_jobs_active_idempotency
ON workspace_sync_jobs(idempotency_key)
WHERE status IN ('pending', 'running');

CREATE INDEX IF NOT EXISTS idx_workspace_sync_jobs_status_priority_created
ON workspace_sync_jobs(status, priority, created_at);

CREATE INDEX IF NOT EXISTS idx_workspace_sync_jobs_assistant_status_created
ON workspace_sync_jobs(assistant_id, status, created_at);

CREATE INDEX IF NOT EXISTS idx_workspace_sync_jobs_mode_status_created
ON workspace_sync_jobs(mode, status, created_at);

CREATE INDEX IF NOT EXISTS idx_workspace_sync_jobs_scope_status_created
ON workspace_sync_jobs(scope_kind, scope_slug, status, created_at);
"""
