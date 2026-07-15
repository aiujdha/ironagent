from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.market import routes as market_routes
from src.market.service import InstallResult
from src.shared.config import get_settings


def test_skill_install_route_records_reload_failure_audit(monkeypatch) -> None:
    audit_events: list[dict[str, object]] = []

    class StubMarketService:
        def get_skill(self, skill_id: str):
            return SimpleNamespace(
                id=skill_id,
                slug="office-docs",
                version="2.0.0",
            )

        async def ainstall_skill(self, *, record_id: str, assistant_id: str):
            return InstallResult(
                assistant_id=assistant_id,
                install_path="/workspace/demo/skills/office-docs",
                install_count=2,
                sync_status="failed",
                sync_error="runtime reload failed: runtime command failed",
                runtime_reload_status="failed",
                runtime_reload_error="runtime command failed",
                action="upgrade",
                previous_version="1.0.0",
            )

    class StubAuditRepository:
        async def aappend_event(self, payload):
            audit_events.append(payload)

    monkeypatch.setattr(market_routes, "service", StubMarketService())
    monkeypatch.setattr(
        market_routes,
        "build_audit_repository",
        lambda: StubAuditRepository(),
    )

    app = FastAPI()
    app.include_router(market_routes.router)
    client = TestClient(app)

    response = client.post(
        "/api/v1/market/skills/skill-2/install",
        data={"assistant_id": "demo-assistant"},
        headers={
            "X-IronAgent-Internal-Token": get_settings().internal_service_token,
            "X-Admin-Operator": "operator-1",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "capability_upgrade"
    assert body["runtime_reload_status"] == "failed"
    assert body["runtime_effective"] is False
    assert audit_events[0]["operator"]["operator_id"] == "operator-1"
    assert audit_events[0]["capability"] == {
        "kind": "skills",
        "id": "skill-2",
        "slug": "office-docs",
        "version": "2.0.0",
    }
    assert audit_events[0]["metadata"]["previous_version"] == "1.0.0"
    assert audit_events[0]["metadata"]["error_code"] == "RUNTIME_RELOAD_FAILED"


def test_skill_install_route_keeps_success_when_audit_log_fails(monkeypatch) -> None:
    class StubMarketService:
        def get_skill(self, skill_id: str):
            return SimpleNamespace(
                id=skill_id,
                slug="office-docs",
                version="2.0.0",
            )

        async def ainstall_skill(self, *, record_id: str, assistant_id: str):
            return InstallResult(
                assistant_id=assistant_id,
                install_path="/workspace/demo/skills/office-docs",
                install_count=1,
                runtime_reload_status="applied",
                action="install",
            )

    class FailingAuditRepository:
        async def aappend_event(self, payload):
            raise RuntimeError("audit database unavailable")

    monkeypatch.setattr(market_routes, "service", StubMarketService())
    monkeypatch.setattr(
        market_routes,
        "build_audit_repository",
        lambda: FailingAuditRepository(),
    )

    app = FastAPI()
    app.include_router(market_routes.router)
    client = TestClient(app)

    response = client.post(
        "/api/v1/market/skills/skill-1/install",
        data={"assistant_id": "demo-assistant"},
        headers={
            "X-IronAgent-Internal-Token": get_settings().internal_service_token,
            "X-Admin-Operator": "operator-1",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "capability_install"
    assert body["runtime_reload_status"] == "applied"
    assert body["runtime_effective"] is True
    assert body["warnings"] == [
        "audit log failed: RuntimeError: audit database unavailable"
    ]
