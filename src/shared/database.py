import asyncio
import threading
from typing import Any

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from src.shared.config import get_settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


_engine_lock = threading.RLock()
_DatabaseCacheKey = tuple[str, asyncio.AbstractEventLoop | str]

_engine_cache: dict[_DatabaseCacheKey, AsyncEngine] = {}
_session_factory_cache: dict[_DatabaseCacheKey, async_sessionmaker[AsyncSession]] = {}


def get_database_url() -> str:
    return get_settings().database_url


def get_async_database_url() -> str:
    database_url = get_database_url().strip()
    replacements = (
        ("mysql+pymysql://", "mysql+aiomysql://"),
        ("mysql://", "mysql+aiomysql://"),
        ("postgresql+psycopg2://", "postgresql+asyncpg://"),
        ("postgresql://", "postgresql+asyncpg://"),
        ("sqlite+pysqlite://", "sqlite+aiosqlite://"),
        ("sqlite://", "sqlite+aiosqlite://"),
    )
    for prefix, replacement in replacements:
        if database_url.startswith(prefix):
            return replacement + database_url[len(prefix) :]
    return database_url


def _database_cache_key() -> _DatabaseCacheKey:
    async_database_url = get_async_database_url()
    try:
        loop_key: asyncio.AbstractEventLoop | str = asyncio.get_running_loop()
    except RuntimeError:
        loop_key = "no-running-loop"
    return (async_database_url, loop_key)


def _engine_options(async_database_url: str) -> dict[str, Any]:
    settings = get_settings()
    options: dict[str, Any] = {
        "pool_pre_ping": True,
        "future": True,
    }
    if async_database_url.startswith("mysql+"):
        options.update(
            {
                "pool_size": settings.db_pool_size,
                "max_overflow": settings.db_max_overflow,
                "pool_timeout": settings.db_pool_timeout_seconds,
                "pool_recycle": settings.db_pool_recycle_seconds,
                "connect_args": {
                    "connect_timeout": settings.db_connect_timeout_seconds,
                },
            }
        )
    elif async_database_url.startswith("sqlite+"):
        options["connect_args"] = {
            "timeout": settings.db_pool_timeout_seconds,
        }
    return options


def get_database_engine() -> AsyncEngine:
    cache_key = _database_cache_key()
    async_database_url = cache_key[0]
    with _engine_lock:
        engine = _engine_cache.get(cache_key)
        if engine is None:
            engine = create_async_engine(
                async_database_url,
                **_engine_options(async_database_url),
            )
            _engine_cache[cache_key] = engine
        return engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    cache_key = _database_cache_key()
    with _engine_lock:
        factory = _session_factory_cache.get(cache_key)
        if factory is None:
            factory = async_sessionmaker(
                bind=get_database_engine(),
                autoflush=False,
                expire_on_commit=False,
            )
            _session_factory_cache[cache_key] = factory
        return factory


def new_session() -> AsyncSession:
    return get_session_factory()()


async def dispose_database_engines() -> None:
    with _engine_lock:
        engines = list(_engine_cache.values())
        _engine_cache.clear()
        _session_factory_cache.clear()
    for engine in engines:
        await engine.dispose()


def reset_database_caches() -> None:
    with _engine_lock:
        engines = list(_engine_cache.values())
        _engine_cache.clear()
        _session_factory_cache.clear()

    async def _dispose_all() -> None:
        for engine in engines:
            await engine.dispose()

    if not engines:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_dispose_all())
        return
    loop.create_task(_dispose_all())
