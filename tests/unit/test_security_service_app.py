from types import SimpleNamespace

from fastapi.testclient import TestClient

import src.security_service.routes as security_routes
from src.assistants.models import AssistantRecord
from src.security_service.app import create_security_app
from src.shared.config import get_settings


def test_create_security_app_allows_control_ui_public_origin(monkeypatch) -> None:
    monkeypatch.setenv("IRON_AGENT_CONTROL_UI_PUBLIC_URL", "http://192.0.2.11:30789")
    monkeypatch.delenv("IRON_AGENT_CONTROL_UI_ALLOWED_ORIGINS_JSON", raising=False)
    get_settings.cache_clear()

    app = create_security_app()
    client = TestClient(app)
    response = client.get("/health", headers={"Origin": "http://192.0.2.11:30789"})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://192.0.2.11:30789"

    get_settings.cache_clear()


def test_lifespan_drains_approval_bypass_audit_tasks(monkeypatch) -> None:
    drained = False

    async def drain_approval_bypass_audit_tasks() -> None:
        nonlocal drained
        drained = True

    monkeypatch.setenv("IRON_AGENT_APPROVAL_MODE", "disabled")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "src.security_service.app.drain_approval_bypass_audit_tasks",
        drain_approval_bypass_audit_tasks,
        raising=False,
    )

    with TestClient(create_security_app()) as client:
        assert client.get("/health").status_code == 200

    assert drained is True
    get_settings.cache_clear()


def test_security_readyz_checks_workspace_metadata_and_collaboration(monkeypatch) -> None:
    monkeypatch.setenv("IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND", "sqlite")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "src.security_service.app.collaboration_service",
        SimpleNamespace(check_ready=lambda: {"status": "ok"}),
    )

    client = TestClient(create_security_app())
    response = client.get("/readyz")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["checks"]["workspaceFileMetadata"]["backend"] == "sqlite"
    assert payload["checks"]["collaboration"]["status"] == "ok"
    get_settings.cache_clear()


def test_control_ui_launch_ticket_rejects_deleted_assistant(monkeypatch) -> None:
    class StubAssistantService:
        async def aget_assistant(self, assistant_id: str):
            return SimpleNamespace(assistant_id=assistant_id, deleted=True)

    monkeypatch.setattr(security_routes, "AssistantService", StubAssistantService)

    client = TestClient(create_security_app())
    response = client.post(
        "/api/v1/control-ui/launch-tickets",
        headers={"X-IronAgent-Internal-Token": get_settings().internal_service_token},
        json={
            "assistant_id": "deleted-main",
            "gateway_url": "http://caller.example/gateway",
            "gateway_token": "caller-token",
            "session_key": "main",
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "assistant not found"


def test_control_ui_assistant_context_does_not_infer_deleted_assistant(monkeypatch) -> None:
    class StubAssistantService:
        def list_assistants(self):
            return [
                SimpleNamespace(
                    assistant_id="deleted-main",
                    deleted=True,
                    runtime=SimpleNamespace(status="running"),
                )
            ]

    class StubFileRepository:
        async def alist_files(self, **_kwargs):
            return [SimpleNamespace(assistant_id="deleted-main")]

    class StubWorkspaceFileService:
        file_repository = StubFileRepository()

    monkeypatch.setattr(security_routes, "AssistantService", StubAssistantService)
    app = create_security_app()
    app.dependency_overrides[security_routes.get_workspace_file_service] = lambda: (
        StubWorkspaceFileService()
    )
    client = TestClient(app)

    response = client.get("/api/v1/control-ui/assistant-context")

    assert response.status_code == 200
    assert response.json() == {"assistants": []}


def test_control_ui_assistant_context_does_not_seed_default_assistant_for_empty_mysql_repository(
    monkeypatch,
) -> None:
    class EmptyInitializedAssistantRepository:
        def __init__(self) -> None:
            self.records: dict[str, AssistantRecord] = {}

        def is_initialized(self) -> bool:
            return True

        def list_assistants(self) -> list[AssistantRecord]:
            return list(self.records.values())

        async def alist_assistants(self) -> list[AssistantRecord]:
            return self.list_assistants()

        def get_assistant(self, assistant_id: str) -> AssistantRecord | None:
            return self.records.get(assistant_id)

        async def aget_assistant(self, assistant_id: str) -> AssistantRecord | None:
            return self.get_assistant(assistant_id)

        def upsert_assistant(self, record: AssistantRecord) -> AssistantRecord:
            self.records[record.assistant_id] = record
            return record

        async def aupsert_assistant(self, record: AssistantRecord) -> AssistantRecord:
            return self.upsert_assistant(record)

        def delete_assistant(self, assistant_id: str) -> bool:
            return self.records.pop(assistant_id, None) is not None

        async def adelete_assistant(self, assistant_id: str) -> bool:
            return self.delete_assistant(assistant_id)

    class StubFileRepository:
        async def alist_files(self, **_kwargs):
            return []

    class StubWorkspaceFileService:
        file_repository = StubFileRepository()

    repository = EmptyInitializedAssistantRepository()
    monkeypatch.setenv("IRON_AGENT_STORAGE_BACKEND", "mysql")
    monkeypatch.delenv("IRON_AGENT_SEED_DEFAULT_ASSISTANTS", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(
        "src.assistants.service.build_assistant_repository",
        lambda: repository,
    )

    app = create_security_app()
    app.dependency_overrides[security_routes.get_workspace_file_service] = lambda: (
        StubWorkspaceFileService()
    )
    client = TestClient(app)

    response = client.get("/api/v1/control-ui/assistant-context")

    assert response.status_code == 200
    assert response.json() == {"assistants": []}
    assert repository.records == {}
    get_settings.cache_clear()
