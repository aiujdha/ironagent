from fastapi.testclient import TestClient

from src.admin.control_ui_launch import mint_control_ui_launch_ticket
from src.assistants.models import AssistantRecord
from src.runtime.models import AssistantRuntimeRecord
from src.runtime.provisioner import RuntimeProbeResult
from src.security_service.app import create_security_app
from src.security_service.launch_tickets import control_ui_relay_session_store, launch_ticket_store
from src.shared.config import get_settings
from src.shared.repositories import JsonAuditRepository


def _create_client(monkeypatch, tmp_path, *, ttl_seconds: int = 90) -> TestClient:
    monkeypatch.setenv("IRON_AGENT_STORAGE_BACKEND", "json")
    monkeypatch.setenv("IRON_AGENT_ASSISTANTS_STORE_PATH", str(tmp_path / "assistants.json"))
    monkeypatch.setenv("IRON_AGENT_APPROVAL_STORE_PATH", str(tmp_path / "approvals.json"))
    monkeypatch.setenv("IRON_AGENT_ASSISTANTS_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("IRON_AGENT_USERS_STORE_PATH", str(tmp_path / "users.json"))
    monkeypatch.setenv("IRON_AGENT_USER_BINDINGS_STORE_PATH", str(tmp_path / "bindings.json"))
    monkeypatch.setenv("IRON_AGENT_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv(
        "IRON_AGENT_WORKSPACE_FILES_STORE_PATH",
        str(tmp_path / "workspace-files.json"),
    )
    monkeypatch.setenv(
        "IRON_AGENT_WORKSPACE_FILE_EVENTS_STORE_PATH",
        str(tmp_path / "workspace-events.json"),
    )
    monkeypatch.setenv(
        "IRON_AGENT_WORKSPACE_PUSH_REQUESTS_STORE_PATH",
        str(tmp_path / "workspace-push.json"),
    )
    monkeypatch.setenv("IRON_AGENT_CONTROL_UI_LAUNCH_TICKET_TTL_SECONDS", str(ttl_seconds))
    get_settings.cache_clear()
    launch_ticket_store.clear()
    control_ui_relay_session_store.clear()
    return TestClient(create_security_app())


def _admin_headers() -> dict[str, str]:
    return {"X-IronAgent-Internal-Token": get_settings().internal_service_token}


def _audit_events(tmp_path) -> list[dict]:
    return JsonAuditRepository(tmp_path / "audit.jsonl").list_recent(limit=20)


def test_control_ui_launch_ticket_minter_uses_internal_service_auth(
    monkeypatch,
    tmp_path,
) -> None:
    _create_client(monkeypatch, tmp_path)
    monkeypatch.setenv("IRON_AGENT_CONTROL_UI_PUBLIC_URL", "http://127.0.0.1:64685/chat")
    get_settings.cache_clear()
    captured: dict[str, dict] = {}

    class StubResponse:
        status_code = 200
        text = ""

        def json(self) -> dict[str, str]:
            return {"ticket": "ticket-1", "expires_at": "2026-05-18T00:00:00Z"}

    class StubClient:
        def __init__(self, *, timeout: float) -> None:
            assert timeout == 3.0

        def __enter__(self) -> "StubClient":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, url: str, *, headers: dict[str, str], json: dict) -> StubResponse:
            captured["request"] = {"url": url, "headers": headers, "json": json}
            return StubResponse()

    monkeypatch.setattr("src.admin.control_ui_launch.httpx.Client", StubClient)

    ticket = mint_control_ui_launch_ticket(
        assistant_id="demo-main",
        gateway_url="ws://127.0.0.1:60599",
        gateway_token="gateway-token",
    )

    assert ticket is not None
    assert captured["request"]["headers"] == _admin_headers()
    assert set(captured["request"]["json"]) == {
        "assistant_id",
        "gateway_url",
        "gateway_token",
        "session_key",
        "control_ui_url",
    }
    assert captured["request"]["json"]["gateway_token"] == "gateway-token"
    get_settings.cache_clear()


def test_control_ui_launch_ticket_can_be_issued_and_consumed(monkeypatch, tmp_path) -> None:
    client = _create_client(monkeypatch, tmp_path, ttl_seconds=90)
    try:
        create_response = client.post(
            "/api/v1/control-ui/launch-tickets",
            json={
                "assistant_id": "demo-main",
                "gateway_url": "ws://127.0.0.1:60599",
                "gateway_token": "gateway-token",
                "session_key": "agent:main:main",
                "control_ui_url": "http://127.0.0.1:64685/chat",
            },
            headers=_admin_headers(),
        )
        assert create_response.status_code == 200, create_response.text
        ticket = create_response.json()["ticket"]

        resolve_response = client.post(
            "/api/v1/control-ui/launch-tickets/resolve",
            json={"ticket": ticket},
            headers={"Origin": "http://127.0.0.1:64685"},
        )
        assert resolve_response.status_code == 200, resolve_response.text
        payload = resolve_response.json()
        assert payload["assistant_id"] == "demo-main"
        assert payload["gateway_url"] == "ws://testserver/api/v1/control-ui/gateway-relay/ws"
        assert payload["gateway_token"] != "gateway-token"
        assert payload["admin_token"] == "gateway-token"
        assert set(payload) == {
            "assistant_id",
            "gateway_url",
            "gateway_token",
            "admin_token",
            "session_key",
            "expires_at",
        }
        assert payload["session_key"] == "agent:main:main"
        assert payload["expires_at"] == create_response.json()["expires_at"]

        replay_response = client.post(
            "/api/v1/control-ui/launch-tickets/resolve",
            json={"ticket": ticket},
            headers={"Origin": "http://127.0.0.1:64685"},
        )
        assert replay_response.status_code == 404
        operations = {event["operation_type"] for event in _audit_events(tmp_path)}
        assert {
            "control_session.create",
            "control_session.consume",
            "control_session.reject",
        }.issubset(operations)
    finally:
        launch_ticket_store.clear()
        control_ui_relay_session_store.clear()
        get_settings.cache_clear()


def test_control_ui_launch_ticket_admin_resolve_returns_runtime_gateway(
    monkeypatch,
    tmp_path,
) -> None:
    client = _create_client(monkeypatch, tmp_path, ttl_seconds=90)

    class StubAssistantService:
        async def aget_assistant(self, assistant_id: str) -> AssistantRecord | None:
            if assistant_id != "demo-main":
                return None
            return AssistantRecord(
                assistant_id="demo-main",
                name="Demo Main",
                runtime=AssistantRuntimeRecord(
                    status="running",
                    gateway_url="ws://127.0.0.1:60599",
                    gateway_token="gateway-token",
                ),
            )

        async def aprobe_runtime_states(
            self,
            assistants: list[AssistantRecord] | None = None,
        ) -> dict[str, RuntimeProbeResult]:
            return {}

    monkeypatch.setattr("src.security_service.routes.AssistantService", StubAssistantService)

    try:
        create_response = client.post(
            "/api/v1/control-ui/launch-tickets",
            json={
                "assistant_id": "demo-main",
                "gateway_url": "ws://127.0.0.1:60599",
                "gateway_token": "gateway-token",
                "session_key": "agent:main:main",
                "control_ui_url": "http://127.0.0.1:64685/chat",
            },
            headers=_admin_headers(),
        )
        assert create_response.status_code == 200, create_response.text
        ticket = create_response.json()["ticket"]

        resolve_response = client.post(
            "/api/v1/control-ui/launch-tickets/resolve",
            json={"ticket": ticket},
            headers={
                **_admin_headers(),
                "Origin": "http://127.0.0.1:64685",
            },
        )
        assert resolve_response.status_code == 200, resolve_response.text
        payload = resolve_response.json()
        assert payload["assistant_id"] == "demo-main"
        assert payload["gateway_url"] == "ws://127.0.0.1:60599"
        assert payload["gateway_token"] == "gateway-token"
        assert payload["admin_token"] == "gateway-token"
    finally:
        launch_ticket_store.clear()
        control_ui_relay_session_store.clear()
        get_settings.cache_clear()


def test_control_ui_launch_ticket_rejects_origin_mismatch(monkeypatch, tmp_path) -> None:
    client = _create_client(monkeypatch, tmp_path, ttl_seconds=90)
    try:
        create_response = client.post(
            "/api/v1/control-ui/launch-tickets",
            json={
                "assistant_id": "demo-main",
                "gateway_url": "ws://127.0.0.1:60599",
                "gateway_token": "gateway-token",
                "control_ui_url": "https://spdbpanda.example/chat",
            },
            headers=_admin_headers(),
        )
        assert create_response.status_code == 200, create_response.text
        ticket = create_response.json()["ticket"]

        resolve_response = client.post(
            "/api/v1/control-ui/launch-tickets/resolve",
            json={"ticket": ticket},
            headers={"Origin": "https://other.example"},
        )
        assert resolve_response.status_code == 403
        assert resolve_response.json()["detail"] == "launch ticket origin mismatch"
        reject_events = [
            event for event in _audit_events(tmp_path)
            if event.get("operation_type") == "control_session.reject"
        ]
        assert reject_events[-1]["result"]["error_code"] == "origin_mismatch"
    finally:
        launch_ticket_store.clear()
        control_ui_relay_session_store.clear()
        get_settings.cache_clear()


def test_control_ui_launch_ticket_expires(monkeypatch, tmp_path) -> None:
    client = _create_client(monkeypatch, tmp_path, ttl_seconds=0)
    try:
        create_response = client.post(
            "/api/v1/control-ui/launch-tickets",
            json={
                "assistant_id": "demo-main",
                "gateway_url": "ws://127.0.0.1:60599",
                "gateway_token": "gateway-token",
                "control_ui_url": "https://spdbpanda.example/chat",
            },
            headers=_admin_headers(),
        )
        assert create_response.status_code == 200, create_response.text
        ticket = create_response.json()["ticket"]

        resolve_response = client.post(
            "/api/v1/control-ui/launch-tickets/resolve",
            json={"ticket": ticket},
            headers={"Origin": "https://spdbpanda.example"},
        )
        assert resolve_response.status_code == 410
        assert resolve_response.json()["detail"] == "launch ticket expired"
        expire_events = [
            event for event in _audit_events(tmp_path)
            if event.get("operation_type") == "control_session.expire"
        ]
        assert expire_events[-1]["result"]["error_code"] == "expired"
    finally:
        launch_ticket_store.clear()
        control_ui_relay_session_store.clear()
        get_settings.cache_clear()


def test_control_ui_launch_ticket_refreshes_stale_gateway_url_without_overriding_token(
    monkeypatch,
    tmp_path,
) -> None:
    client = _create_client(monkeypatch, tmp_path, ttl_seconds=90)

    class StubAssistantService:
        async def aget_assistant(self, assistant_id: str) -> AssistantRecord | None:
            if assistant_id != "001":
                return None
            return AssistantRecord(
                assistant_id="001",
                name="Assistant 001",
                runtime=AssistantRuntimeRecord(
                    status="running",
                    gateway_url="http://172.28.135.254:3221",
                    gateway_token="stale-record-token",
                ),
            )

        async def aprobe_runtime_states(
            self,
            assistants: list[AssistantRecord] | None = None,
        ) -> dict[str, RuntimeProbeResult]:
            return {
                "001": RuntimeProbeResult(
                    container_name="ironagent-assistant-001",
                    container_exists=True,
                    container_id="container-001",
                    container_state="running",
                    health_state="healthy",
                    status="running",
                    gateway_host="172.28.135.254",
                    gateway_port=3221,
                    gateway_url="http://172.28.135.254:3221",
                )
            }

    monkeypatch.setattr("src.security_service.routes.AssistantService", StubAssistantService)

    try:
        create_response = client.post(
            "/api/v1/control-ui/launch-tickets",
            json={
                "assistant_id": "001",
                "gateway_url": "http://172.28.135.254:2049",
                "gateway_token": "request-gateway-token",
                "session_key": "main",
                "control_ui_url": "http://127.0.0.1:64685/chat",
            },
            headers=_admin_headers(),
        )
        assert create_response.status_code == 200, create_response.text
        ticket = create_response.json()["ticket"]

        resolve_response = client.post(
            "/api/v1/control-ui/launch-tickets/resolve",
            json={"ticket": ticket},
            headers={"Origin": "http://127.0.0.1:64685"},
        )
        assert resolve_response.status_code == 200, resolve_response.text
        payload = resolve_response.json()

        relay_session = control_ui_relay_session_store.resolve(payload["gateway_token"])
        assert relay_session.assistant_id == "001"
        assert relay_session.gateway_url == "http://172.28.135.254:3221"
        assert relay_session.gateway_token == "request-gateway-token"
        assert payload["admin_token"] == "request-gateway-token"
    finally:
        launch_ticket_store.clear()
        control_ui_relay_session_store.clear()
        get_settings.cache_clear()
