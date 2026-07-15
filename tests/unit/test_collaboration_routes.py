import pytest
from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from src.collaboration import routes
from src.collaboration.routes import _run_call


@pytest.mark.asyncio
async def test_run_call_maps_repository_errors_to_503() -> None:
    def fail() -> None:
        raise SQLAlchemyError("database unavailable")

    with pytest.raises(HTTPException) as exc:
        await _run_call("test_operation", fail)

    assert exc.value.status_code == 503
    assert exc.value.detail == "collaboration repository unavailable"


@pytest.mark.asyncio
async def test_run_call_does_not_mask_generic_runtime_errors() -> None:
    def fail() -> None:
        raise RuntimeError("programmer bug")

    with pytest.raises(RuntimeError, match="programmer bug"):
        await _run_call("test_operation", fail)


@pytest.mark.asyncio
async def test_collaboration_readyz_returns_repository_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReadyService:
        def check_ready(self) -> dict[str, object]:
            return {"status": "ok", "tables_checked": 5}

    monkeypatch.setattr(routes, "service", ReadyService())

    assert await routes.collaboration_readyz() == {"status": "ok", "tables_checked": 5}
