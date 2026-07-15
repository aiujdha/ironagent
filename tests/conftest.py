from pathlib import Path

import pytest
from sqlalchemy import create_engine

from src.security_service import routes as security_routes
from src.security_service.decision import SecurityDecisionEngine
from src.shared.config import get_settings
from src.shared.database import Base, reset_database_caches


@pytest.fixture()
def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = tmp_path / "test.sqlite3"
    monkeypatch.setenv("IRON_AGENT_STORAGE_BACKEND", "mysql")
    monkeypatch.setenv("IRON_AGENT_DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv(
        "IRON_AGENT_WORKSPACE_FILE_SQLITE_PATH",
        str(tmp_path / "workspace-files.sqlite3"),
    )
    monkeypatch.setenv(
        "IRON_AGENT_WORKSPACE_OBJECT_STORAGE_ROOT",
        str(tmp_path / "workspace-objects"),
    )
    get_settings.cache_clear()
    reset_database_caches()

    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    security_routes.engine = SecurityDecisionEngine()

    yield

    get_settings.cache_clear()
    reset_database_caches()
