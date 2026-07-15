"""战役一 P2 PR-2:DatabaseWorkspaceSyncJobStore(MySQL/SQLAlchemy 后端)行为测试。

用 aiosqlite 验证异步实现 + active_idempotency_key 唯一索引去重(SQLite 上行为一致)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.async_utils import run_sync
from src.shared.config import get_settings
from src.shared.database import Base
from src.workspace_files.database_models import WorkspaceSyncJobRow
from src.workspace_files.sync_jobs import (
    DatabaseWorkspaceSyncJobStore,
    WorkspaceSyncJobStore,
    build_workspace_sync_job_store,
)


@asynccontextmanager
async def _db_store(tmp_path: Path) -> AsyncIterator[DatabaseWorkspaceSyncJobStore]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sync.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield DatabaseWorkspaceSyncJobStore(session_factory=session_factory)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_or_get_job_creates_pending(tmp_path: Path) -> None:
    async with _db_store(tmp_path) as store:
        job = await store.acreate_or_get_job("assistant-1")

        assert job.job_id
        assert job.status == "pending"
        assert job.assistant_id == "assistant-1"
        fetched = await store.aget_job(job.job_id)
        assert fetched is not None
        assert fetched.job_id == job.job_id


@pytest.mark.asyncio
async def test_create_or_get_job_dedups_active(tmp_path: Path) -> None:
    async with _db_store(tmp_path) as store:
        first = await store.acreate_or_get_job("assistant-1")
        pending_dup = await store.acreate_or_get_job("assistant-1")
        assert pending_dup.job_id == first.job_id

        running = await store.amark_running(first.job_id)
        assert running is not None
        assert running.status == "running"

        running_dup = await store.acreate_or_get_job("assistant-1")
        assert running_dup.job_id == first.job_id


@pytest.mark.asyncio
async def test_create_or_get_job_releases_stale_running_job(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'stale.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        store = DatabaseWorkspaceSyncJobStore(
            session_factory=session_factory,
            stale_running_after_seconds=60,
        )
        first = await store.acreate_or_get_job("assistant-1")
        running = await store.amark_running(first.job_id)
        assert running is not None
        old = datetime.now(UTC) - timedelta(minutes=10)
        async with session_factory() as session:
            await session.execute(
                update(WorkspaceSyncJobRow)
                .where(WorkspaceSyncJobRow.job_id == first.job_id)
                .values(updated_at=old)
            )
            await session.commit()

        second = await store.acreate_or_get_job("assistant-1")

        assert second.job_id != first.job_id
        assert second.status == "pending"
        stale = await store.aget_job(first.job_id)
        assert stale is not None
        assert stale.status == "failed"
        assert stale.error_code == "workspace_sync_stale_running"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_terminal_releases_active_idempotency_key(tmp_path: Path) -> None:
    async with _db_store(tmp_path) as store:
        first = await store.acreate_or_get_job("assistant-1")
        succeeded = await store.amark_succeeded(first.job_id, scanned_count=3, created_count=1)
        assert succeeded is not None
        assert succeeded.status == "succeeded"
        assert succeeded.scanned_count == 3
        assert succeeded.created_count == 1

        # 终态释放唯一约束后,同 scope 可创建新 job(不再去重到旧 job)
        second = await store.acreate_or_get_job("assistant-1")
        assert second.job_id != first.job_id
        assert second.status == "pending"


@pytest.mark.asyncio
async def test_claim_next_pending_claims_and_exhausts(tmp_path: Path) -> None:
    async with _db_store(tmp_path) as store:
        job = await store.acreate_or_get_job("assistant-1")

        claimed = await store.aclaim_next_pending(mode="index_only")
        assert claimed is not None
        assert claimed.job_id == job.job_id
        assert claimed.status == "running"

        again = await store.aclaim_next_pending(mode="index_only")
        assert again is None


@pytest.mark.asyncio
async def test_claim_orders_by_priority_then_created(tmp_path: Path) -> None:
    async with _db_store(tmp_path) as store:
        await store.acreate_or_get_job("assistant-low", priority=200)
        high = await store.acreate_or_get_job("assistant-high", priority=10)

        claimed = await store.aclaim_next_pending(mode="index_only")
        assert claimed is not None
        assert claimed.job_id == high.job_id  # priority 值越小越先


@pytest.mark.asyncio
async def test_claim_retries_next_pending_after_concurrent_cas_loss(tmp_path: Path) -> None:
    class RacingStore(DatabaseWorkspaceSyncJobStore):
        lost_once = False

        async def _claim_pending_row(
            self,
            session: AsyncSession,
            row: WorkspaceSyncJobRow,
            now: datetime,
        ) -> bool:
            if not self.lost_once:
                self.lost_once = True
                await session.execute(
                    update(WorkspaceSyncJobRow)
                    .where(WorkspaceSyncJobRow.job_id == row.job_id)
                    .values(status="running", started_at=now, updated_at=now)
                )
                return False
            return await super()._claim_pending_row(session, row, now)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        store = RacingStore(session_factory=session_factory)
        first = await store.acreate_or_get_job("assistant-1")
        second = await store.acreate_or_get_job("assistant-2")

        claimed = await store.aclaim_next_pending(mode="index_only")

        assert claimed is not None
        assert claimed.job_id == second.job_id
        first_after = await store.aget_job(first.job_id)
        assert first_after is not None
        assert first_after.status == "running"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_mark_failed_and_cancelled(tmp_path: Path) -> None:
    async with _db_store(tmp_path) as store:
        j1 = await store.acreate_or_get_job("a1")
        failed = await store.amark_failed(j1.job_id, error="boom")
        assert failed is not None
        assert failed.status == "failed"
        assert failed.error == "boom"

        j2 = await store.acreate_or_get_job("a2")
        cancelled = await store.amark_cancelled(j2.job_id)
        assert cancelled is not None
        assert cancelled.status == "cancelled"


@pytest.mark.asyncio
async def test_mark_running_noop_when_terminal(tmp_path: Path) -> None:
    async with _db_store(tmp_path) as store:
        job = await store.acreate_or_get_job("a1")
        await store.amark_succeeded(job.job_id)

        result = await store.amark_running(job.job_id)
        assert result is not None
        assert result.status == "succeeded"  # 非 pending 不改写


@pytest.mark.asyncio
async def test_get_missing_job_returns_none(tmp_path: Path) -> None:
    async with _db_store(tmp_path) as store:
        assert await store.aget_job("nonexistent") is None
        assert await store.amark_running("nonexistent") is None
        assert await store.amark_failed("nonexistent", error="x") is None


@pytest.mark.asyncio
async def test_datetime_fields_are_tz_aware(tmp_path: Path) -> None:
    """与 SQLite store 一致:时间字段恒 tz-aware(MySQL DATETIME 读回 naive 时补 UTC)。"""
    async with _db_store(tmp_path) as store:
        job = await store.acreate_or_get_job("assistant-1")
        fetched = await store.aget_job(job.job_id)
        assert fetched is not None
        assert fetched.created_at.tzinfo is not None
        assert fetched.updated_at.tzinfo is not None

        claimed = await store.aclaim_next_pending(mode="index_only")
        assert claimed is not None
        assert claimed.started_at is not None
        assert claimed.started_at.tzinfo is not None
        assert claimed.created_at.tzinfo is not None

        done = await store.amark_succeeded(job.job_id)
        assert done is not None
        assert done.finished_at is not None
        assert done.finished_at.tzinfo is not None
        assert done.created_at.tzinfo is not None  # 同对象内不混 aware/naive


@pytest.mark.asyncio
async def test_long_idempotency_key_reconcile_mode(tmp_path: Path) -> None:
    """reconcile/cos_import + 长 scope_slug 的 idempotency_key(~543 字符)完整持久化且去重。"""
    async with _db_store(tmp_path) as store:
        long_slug = "s" * 255

        first = await store.acreate_or_get_job(
            "a" * 255, scope_kind="capability", scope_slug=long_slug, mode="cos_import"
        )
        assert len(first.idempotency_key) > 512  # 未被截断

        dup = await store.acreate_or_get_job(
            "a" * 255, scope_kind="capability", scope_slug=long_slug, mode="cos_import"
        )
        assert dup.job_id == first.job_id  # 长 key 等值比较去重成功


def test_sync_interface_via_run_sync(tmp_path: Path) -> None:
    """同步接口经 run_sync 在后台 loop 执行异步实现;setup 同走 run_sync 保证同一 loop。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sj_sync.db'}")

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    run_sync(_setup())
    session_factory = async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    store = DatabaseWorkspaceSyncJobStore(session_factory=session_factory)
    try:
        job = store.create_or_get_job("assistant-1")
        assert job.status == "pending"
        fetched = store.get_job(job.job_id)
        assert fetched is not None
        assert fetched.job_id == job.job_id
        claimed = store.claim_next_pending(mode="index_only")
        assert claimed is not None
        assert claimed.status == "running"
    finally:
        run_sync(engine.dispose())


def test_build_workspace_sync_job_store_routes_by_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IRON_AGENT_WORKSPACE_SYNC_JOB_SQLITE_PATH", str(tmp_path / "sj.db"))
    monkeypatch.delenv("IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND", raising=False)
    monkeypatch.delenv("IRONAGENT_WORKSPACE_FILE_METADATA_BACKEND", raising=False)
    get_settings.cache_clear()
    assert isinstance(build_workspace_sync_job_store(), DatabaseWorkspaceSyncJobStore)

    monkeypatch.setenv("IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND", "sqlite")
    get_settings.cache_clear()
    assert isinstance(build_workspace_sync_job_store(), WorkspaceSyncJobStore)
    get_settings.cache_clear()
