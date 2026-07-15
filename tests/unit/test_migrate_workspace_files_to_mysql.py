"""战役一 P3:正向迁移器 scripts/migrate_workspace_files_to_mysql.py 行为测试。

源 SQLite → aiosqlite 目标(代 MySQL),验证全量含软删迁移、对账、幂等重跑、dry-run。
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts.migrate_workspace_files_to_mysql import _count_target, migrate
from src.shared.database import Base
from src.shared.repositories import DatabaseWorkspaceFileRepository
from src.workspace_files.models import WorkspaceFile, WorkspaceFileEvent
from src.workspace_files.sqlite_repository import (
    SQLiteWorkspaceFileRepository,
    SQLiteWorkspaceFileStore,
)


def test_cli_disposes_database_engines_in_same_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.migrate_workspace_files_to_mysql as migrate_module

    loops: list[tuple[str, asyncio.AbstractEventLoop]] = []

    async def _fake_migrate(**_: object) -> dict[str, bool]:
        loops.append(("migrate", asyncio.get_running_loop()))
        return {"dry_run": True}

    async def _fake_dispose() -> None:
        loops.append(("dispose", asyncio.get_running_loop()))

    monkeypatch.setattr(sys, "argv", ["migrate_workspace_files_to_mysql.py", "--dry-run"])
    monkeypatch.setattr(migrate_module, "migrate", _fake_migrate)
    monkeypatch.setattr(migrate_module, "dispose_database_engines", _fake_dispose)

    migrate_module.main()

    assert [name for name, _ in loops] == ["migrate", "dispose"]
    assert loops[0][1] is loops[1][1]


def _file(
    file_id: str,
    *,
    deleted: bool = False,
    filename: str | None = None,
) -> WorkspaceFile:
    now = datetime.now(UTC)
    resolved_filename = filename or f"{file_id}.pdf"
    return WorkspaceFile(
        file_id=file_id,
        assistant_id="a1",
        bucket="persistent",
        filename=resolved_filename,
        relative_path=f"persistent/{resolved_filename.lstrip('/')}",
        created_at=now,
        updated_at=now,
        deleted=deleted,
    )


def _event(event_id: str, file_id: str) -> WorkspaceFileEvent:
    return WorkspaceFileEvent(
        event_id=event_id,
        file_id=file_id,
        assistant_id="a1",
        action="upload",
        user_id="emp001",
        detail={"k": "v"},
    )


async def _make_target(tmp_path: Path) -> async_sessionmaker:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'target.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


async def _seed_source(sqlite_path: Path) -> None:
    repo = SQLiteWorkspaceFileRepository(SQLiteWorkspaceFileStore(sqlite_path))
    await repo.asave_file(_file("f1"))
    await repo.asave_file(_file("f2"))
    await repo.asave_file(_file("f3", deleted=True))
    await repo.aappend_event(_event("e1", "f1"))
    await repo.aappend_event(_event("e2", "f2"))


@pytest.mark.asyncio
async def test_forward_migration_round_trip_and_idempotent(tmp_path: Path) -> None:
    src_path = tmp_path / "src.db"
    await _seed_source(src_path)
    target_factory = await _make_target(tmp_path)

    report = await migrate(sqlite_path=src_path, target_session_factory=target_factory)
    assert report["source_files"] == 3
    assert report["source_active"] == 2
    assert report["source_deleted"] == 1  # 软删行随迁,不丢
    assert report["source_events"] == 2
    assert report["target_files"] == 3
    assert report["target_events"] == 2
    assert report["files_ok"] is True
    assert report["events_ok"] is True

    # 字段保真:读回目标文件核对关键字段(证明迁的是正确数据,非只对数量)
    from src.shared.repositories import DatabaseWorkspaceFileRepository

    target_repo = DatabaseWorkspaceFileRepository(target_factory)
    migrated = await target_repo.aget_file("f1")
    assert migrated is not None
    assert migrated.filename == "f1.pdf"
    assert migrated.relative_path == "persistent/f1.pdf"
    assert migrated.bucket == "persistent"
    deleted_file = await target_repo.aget_file("f3")
    assert deleted_file is not None
    assert deleted_file.deleted is True  # 软删标记保真

    # 幂等重跑:目标计数不变(by file_id / event_id upsert)
    report2 = await migrate(sqlite_path=src_path, target_session_factory=target_factory)
    assert report2["target_files"] == 3
    assert report2["target_events"] == 2


@pytest.mark.asyncio
async def test_forward_migration_refuses_to_overwrite_existing_target_file(
    tmp_path: Path,
) -> None:
    src_path = tmp_path / "src.db"
    await _seed_source(src_path)
    target_factory = await _make_target(tmp_path)
    target_repo = DatabaseWorkspaceFileRepository(target_factory)
    await target_repo.asave_file(_file("f1", filename="mysql-newer.pdf"))

    with pytest.raises(ValueError, match="target file_id conflict"):
        await migrate(sqlite_path=src_path, target_session_factory=target_factory)

    existing = await target_repo.aget_file("f1")
    assert existing is not None
    assert existing.filename == "mysql-newer.pdf"
    assert await target_repo.aget_file("f2") is None


@pytest.mark.asyncio
async def test_forward_migration_dry_run_writes_nothing(tmp_path: Path) -> None:
    src_path = tmp_path / "src.db"
    await _seed_source(src_path)
    target_factory = await _make_target(tmp_path)

    report = await migrate(
        sqlite_path=src_path, target_session_factory=target_factory, dry_run=True
    )
    assert report["dry_run"] is True
    assert report["source_files"] == 3
    assert report["target_files_before"] == 0

    target_files, target_events = await _count_target(target_factory)
    assert target_files == 0  # dry-run 不写
    assert target_events == 0


@pytest.mark.asyncio
async def test_forward_migration_dry_run_does_not_mutate_source_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src_path = tmp_path / "src.db"
    await _seed_source(src_path)
    target_factory = await _make_target(tmp_path)

    def _fail_schema_init(self: SQLiteWorkspaceFileStore) -> None:
        raise AssertionError("dry-run should not initialize or migrate source SQLite schema")

    monkeypatch.setattr(SQLiteWorkspaceFileStore, "_ensure_schema", _fail_schema_init)

    report = await migrate(
        sqlite_path=src_path, target_session_factory=target_factory, dry_run=True
    )

    assert report["dry_run"] is True
    assert report["source_files"] == 3


@pytest.mark.asyncio
async def test_forward_migration_rejects_missing_explicit_sqlite_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_path = tmp_path / "missing-source.db"

    def _fail_target_factory() -> None:
        raise AssertionError("target session factory should not be built before source exists")

    monkeypatch.setattr(
        "scripts.migrate_workspace_files_to_mysql.get_session_factory",
        _fail_target_factory,
    )

    with pytest.raises(FileNotFoundError, match="source SQLite database does not exist"):
        await migrate(sqlite_path=missing_path)

    assert not missing_path.exists()


@pytest.mark.asyncio
async def test_forward_migration_rejects_normalized_active_path_conflicts(
    tmp_path: Path,
) -> None:
    src_path = tmp_path / "src.db"
    source_repo = SQLiteWorkspaceFileRepository(SQLiteWorkspaceFileStore(src_path))
    await source_repo.asave_file(_file("f1", filename="reports/summary.txt"))
    await source_repo.asave_file(_file("f2", filename="/reports/summary.txt"))
    target_factory = await _make_target(tmp_path)

    with pytest.raises(ValueError, match="normalized active path conflict"):
        await migrate(sqlite_path=src_path, target_session_factory=target_factory)

    target_files, target_events = await _count_target(target_factory)
    assert target_files == 0
    assert target_events == 0


@pytest.mark.asyncio
async def test_forward_migration_rejects_target_active_path_conflict(
    tmp_path: Path,
) -> None:
    from src.shared.repositories import DatabaseWorkspaceFileRepository

    src_path = tmp_path / "src.db"
    source_repo = SQLiteWorkspaceFileRepository(SQLiteWorkspaceFileStore(src_path))
    await source_repo.asave_file(_file("source-id", filename="shared/report.pdf"))
    await source_repo.aappend_event(_event("source-event", "source-id"))
    target_factory = await _make_target(tmp_path)
    target_repo = DatabaseWorkspaceFileRepository(target_factory)
    await target_repo.asave_file(_file("target-id", filename="/shared/report.pdf"))

    with pytest.raises(ValueError, match="target active path conflict"):
        await migrate(sqlite_path=src_path, target_session_factory=target_factory)

    assert await target_repo.aget_file("source-id") is None
    existing = await target_repo.aget_file("target-id")
    assert existing is not None
    assert existing.filename == "/shared/report.pdf"
    assert await target_repo.alist_events(file_id="source-id") == []


@pytest.mark.asyncio
async def test_forward_migration_validates_source_before_default_target_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src_path = tmp_path / "src.db"
    source_repo = SQLiteWorkspaceFileRepository(SQLiteWorkspaceFileStore(src_path))
    await source_repo.asave_file(_file("f1", filename="reports/summary.txt"))
    await source_repo.asave_file(_file("f2", filename="/reports/summary.txt"))

    def _fail_target_factory() -> None:
        raise AssertionError("target session factory should not be built before source preflight")

    monkeypatch.setattr(
        "scripts.migrate_workspace_files_to_mysql.get_session_factory",
        _fail_target_factory,
    )

    with pytest.raises(ValueError, match="normalized active path conflict"):
        await migrate(sqlite_path=src_path)
