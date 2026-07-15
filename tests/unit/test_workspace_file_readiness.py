from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.shared.database import Base
from src.workspace_files.readiness import acheck_workspace_file_metadata_readiness


async def _make_target(tmp_path: Path) -> async_sessionmaker:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ready.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("CREATE TABLE alembic_version (version_num varchar(64))"))
    return async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.mark.asyncio
async def test_workspace_file_readiness_requires_029_revision(tmp_path: Path) -> None:
    session_factory = await _make_target(tmp_path)
    async with session_factory() as session:
        await session.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:version)"),
            {"version": "028_workspace_sync_jobs"},
        )
        await session.commit()

    with pytest.raises(RuntimeError, match="required=029_workspace_file_long_path_columns"):
        await acheck_workspace_file_metadata_readiness(
            session_factory=session_factory,
            backend="mysql",
        )


@pytest.mark.asyncio
async def test_workspace_file_readiness_accepts_latest_revision(tmp_path: Path) -> None:
    session_factory = await _make_target(tmp_path)
    async with session_factory() as session:
        await session.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:version)"),
            {"version": "029_workspace_file_long_path_columns"},
        )
        await session.commit()

    result = await acheck_workspace_file_metadata_readiness(
        session_factory=session_factory,
        backend="mysql",
    )

    assert result["status"] == "ok"
    assert result["backend"] == "mysql"
    assert result["alembic_revision"] == "029_workspace_file_long_path_columns"
