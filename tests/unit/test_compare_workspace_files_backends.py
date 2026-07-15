"""P3 影子读比对工具 scripts/compare_workspace_files_backends.py 测试。

验证:① 迁移后比对干净(已知 cross-repo 读语义差异如 sync_status 被白名单隐藏)
      ② --show-known 能把白名单差异打印出来核对
      ③ 真实丢数(MySQL 缺行)被检出。
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts.compare_workspace_files_backends import _print_report, compare
from scripts.migrate_workspace_files_to_mysql import migrate
from src.shared.database import Base
from src.workspace_files.database_models import WorkspaceFileRow
from src.workspace_files.models import WorkspaceFile, WorkspaceFileEvent
from src.workspace_files.sqlite_repository import (
    SQLiteWorkspaceFileRepository,
    SQLiteWorkspaceFileStore,
)


def test_cli_disposes_database_engines_in_same_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.compare_workspace_files_backends as compare_module

    loops: list[tuple[str, asyncio.AbstractEventLoop]] = []
    clean_report = {
        "source_count": 0,
        "target_count": 0,
        "missing_in_mysql": [],
        "extra_in_mysql": [],
        "value_mismatches": [],
    }

    async def _fake_compare(**_: object) -> dict[str, dict[str, object]]:
        loops.append(("compare", asyncio.get_running_loop()))
        return {"files": clean_report, "events": clean_report}

    async def _fake_dispose() -> None:
        loops.append(("dispose", asyncio.get_running_loop()))

    monkeypatch.setattr(sys, "argv", ["compare_workspace_files_backends.py"])
    monkeypatch.setattr(compare_module, "compare", _fake_compare)
    monkeypatch.setattr(compare_module, "dispose_database_engines", _fake_dispose)

    compare_module.main()

    assert [name for name, _ in loops] == ["compare", "dispose"]
    assert loops[0][1] is loops[1][1]


def _file(file_id: str, *, sync_status: str = "uploaded") -> WorkspaceFile:
    now = datetime.now(UTC)
    return WorkspaceFile(
        file_id=file_id,
        assistant_id="a1",
        bucket="persistent",
        filename=f"{file_id}.pdf",
        relative_path=f"persistent/{file_id}.pdf",
        sync_status=sync_status,
        created_at=now,
        updated_at=now,
    )


def _event(event_id: str, file_id: str) -> WorkspaceFileEvent:
    return WorkspaceFileEvent(
        event_id=event_id, file_id=file_id, assistant_id="a1", action="upload", user_id="emp001"
    )


async def _make_target(tmp_path: Path) -> async_sessionmaker:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'target.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


async def _seed_source(sqlite_path: Path) -> None:
    repo = SQLiteWorkspaceFileRepository(SQLiteWorkspaceFileStore(sqlite_path))
    # f_legacy 的 sync_status="synced" 在 MySQL 读路径会归一化 —— 应被白名单隐藏
    await repo.asave_file(_file("f_legacy", sync_status="synced"))
    await repo.asave_file(_file("f_normal"))
    await repo.aappend_event(_event("e1", "f_legacy"))


@pytest.mark.asyncio
async def test_shadow_read_clean_after_migration(tmp_path: Path) -> None:
    src_path = tmp_path / "src.db"
    await _seed_source(src_path)
    target_factory = await _make_target(tmp_path)
    await migrate(sqlite_path=src_path, target_session_factory=target_factory)

    # 默认:已知差异(sync_status 归一化)被白名单隐藏 → 比对干净
    report = await compare(sqlite_path=src_path, target_session_factory=target_factory)
    assert report["files"]["missing_in_mysql"] == []
    assert report["files"]["extra_in_mysql"] == []
    assert report["files"]["value_mismatches"] == []
    assert report["events"]["missing_in_mysql"] == []
    assert report["events"]["value_mismatches"] == []
    assert report["files"]["source_count"] == 2
    assert report["files"]["target_count"] == 2


@pytest.mark.asyncio
async def test_show_known_surfaces_sync_status_divergence(tmp_path: Path) -> None:
    src_path = tmp_path / "src.db"
    await _seed_source(src_path)
    target_factory = await _make_target(tmp_path)
    await migrate(sqlite_path=src_path, target_session_factory=target_factory)

    report = await compare(
        sqlite_path=src_path, target_session_factory=target_factory, show_known=True
    )
    # f_legacy 的 sync_status:SQLite "synced" vs MySQL 归一化 "synced_to_workspace"
    mismatches = dict(report["files"]["value_mismatches"])
    assert "f_legacy" in mismatches
    assert any("sync_status" in line for line in mismatches["f_legacy"])
    assert all("[known]" in line for line in mismatches["f_legacy"])  # 仅白名单字段,无真实差异


@pytest.mark.asyncio
async def test_detects_missing_row_as_data_loss(tmp_path: Path) -> None:
    src_path = tmp_path / "src.db"
    await _seed_source(src_path)
    target_factory = await _make_target(tmp_path)
    await migrate(sqlite_path=src_path, target_session_factory=target_factory)

    # 模拟迁移丢一行
    async with target_factory() as session:
        row = await session.get(WorkspaceFileRow, "f_normal")
        await session.delete(row)
        await session.commit()

    report = await compare(sqlite_path=src_path, target_session_factory=target_factory)
    assert report["files"]["missing_in_mysql"] == ["f_normal"]


@pytest.mark.asyncio
async def test_online_compare_allows_mysql_only_rows(tmp_path: Path) -> None:
    from src.shared.repositories import DatabaseWorkspaceFileRepository

    src_path = tmp_path / "src.db"
    await _seed_source(src_path)
    target_factory = await _make_target(tmp_path)
    await migrate(sqlite_path=src_path, target_session_factory=target_factory)
    target_repo = DatabaseWorkspaceFileRepository(target_factory)
    await target_repo.asave_file(_file("f_live_mysql"))

    report = await compare(sqlite_path=src_path, target_session_factory=target_factory)

    assert report["files"]["missing_in_mysql"] == []
    assert report["files"]["extra_in_mysql"] == ["f_live_mysql"]
    assert _print_report(
        "files",
        report["files"],
        sample=20,
        allow_extra_mysql=True,
    )


@pytest.mark.asyncio
async def test_compare_rejects_missing_explicit_sqlite_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_path = tmp_path / "missing-source.db"

    def _fail_target_factory() -> None:
        raise AssertionError("target session factory should not be built before source exists")

    monkeypatch.setattr(
        "scripts.compare_workspace_files_backends.get_session_factory",
        _fail_target_factory,
    )

    with pytest.raises(FileNotFoundError, match="source SQLite database does not exist"):
        await compare(sqlite_path=missing_path)

    assert not missing_path.exists()
