import asyncio
import threading

import pytest

from src.shared import database
from src.shared.config import get_settings


def test_database_engine_uses_url_cache_across_threads(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IRON_AGENT_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'db.sqlite3'}")
    get_settings.cache_clear()
    database.reset_database_caches()

    factories = []

    def _load_factory() -> None:
        factories.append(database.get_session_factory())

    threads = [threading.Thread(target=_load_factory) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len({id(factory) for factory in factories}) == 1
    assert len(database._engine_cache) == 1


def test_database_engine_uses_separate_cache_per_event_loop(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IRON_AGENT_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'db.sqlite3'}")
    get_settings.cache_clear()
    database.reset_database_caches()

    async def _load_factory():
        return database.get_session_factory()

    first = asyncio.run(_load_factory())
    second = asyncio.run(_load_factory())

    assert first is not second
    assert len(database._engine_cache) == 2


def test_mysql_engine_options_read_pool_settings(monkeypatch) -> None:
    calls = []

    class FakeEngine:
        async def dispose(self) -> None:
            pass

    def fake_create_async_engine(url, **kwargs):
        calls.append((url, kwargs))
        return FakeEngine()

    monkeypatch.setenv(
        "IRON_AGENT_DATABASE_URL",
        "mysql+pymysql://ironagent:ironagent@mysql:3306/ironagent?charset=utf8mb4",
    )
    monkeypatch.setenv("IRON_AGENT_DB_POOL_SIZE", "7")
    monkeypatch.setenv("IRON_AGENT_DB_MAX_OVERFLOW", "2")
    monkeypatch.setenv("IRON_AGENT_DB_POOL_TIMEOUT_SECONDS", "9")
    monkeypatch.setenv("IRON_AGENT_DB_POOL_RECYCLE_SECONDS", "120")
    monkeypatch.setenv("IRON_AGENT_DB_CONNECT_TIMEOUT_SECONDS", "4")
    monkeypatch.setattr(database, "create_async_engine", fake_create_async_engine)
    get_settings.cache_clear()
    database.reset_database_caches()

    database.get_database_engine()

    assert calls == [
        (
            "mysql+aiomysql://ironagent:ironagent@mysql:3306/ironagent?charset=utf8mb4",
            {
                "pool_pre_ping": True,
                "future": True,
                "pool_size": 7,
                "max_overflow": 2,
                "pool_timeout": 9,
                "pool_recycle": 120,
                "connect_args": {"connect_timeout": 4},
            },
        )
    ]


def test_sqlite_engine_does_not_use_mysql_pool_options(tmp_path, monkeypatch) -> None:
    calls = []

    class FakeEngine:
        async def dispose(self) -> None:
            pass

    def fake_create_async_engine(url, **kwargs):
        calls.append((url, kwargs))
        return FakeEngine()

    monkeypatch.setenv("IRON_AGENT_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'db.sqlite3'}")
    monkeypatch.setattr(database, "create_async_engine", fake_create_async_engine)
    get_settings.cache_clear()
    database.reset_database_caches()

    database.get_database_engine()

    assert calls[0][0].startswith("sqlite+aiosqlite:///")
    assert calls[0][1]["pool_pre_ping"] is True
    assert calls[0][1]["future"] is True
    assert calls[0][1]["connect_args"] == {"timeout": get_settings().db_pool_timeout_seconds}
    assert "pool_size" not in calls[0][1]
    assert "max_overflow" not in calls[0][1]


@pytest.mark.asyncio
async def test_dispose_database_engines_clears_caches(monkeypatch) -> None:
    disposed = []

    class FakeEngine:
        async def dispose(self) -> None:
            disposed.append("engine")

    def fake_create_async_engine(url, **kwargs):
        return FakeEngine()

    monkeypatch.setenv(
        "IRON_AGENT_DATABASE_URL",
        "mysql+pymysql://ironagent:ironagent@mysql:3306/ironagent?charset=utf8mb4",
    )
    monkeypatch.setattr(database, "create_async_engine", fake_create_async_engine)
    get_settings.cache_clear()
    database.reset_database_caches()

    database.get_session_factory()

    assert database._engine_cache
    assert database._session_factory_cache

    await database.dispose_database_engines()

    assert disposed == ["engine"]
    assert database._engine_cache == {}
    assert database._session_factory_cache == {}
