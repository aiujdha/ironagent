from __future__ import annotations

import json
import threading
from queue import Queue

from fastapi.testclient import TestClient
from websockets.sync.server import serve

from src.security_service.app import create_security_app
from src.security_service.control_ui_relay import (
    build_control_ui_relay_upstream_candidates,
    build_control_ui_relay_upstream_origin,
)
from src.security_service.launch_tickets import control_ui_relay_session_store, launch_ticket_store
from src.shared.config import get_settings


def _create_client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("IRON_AGENT_STORAGE_BACKEND", "json")
    monkeypatch.setenv("IRON_AGENT_ASSISTANTS_STORE_PATH", str(tmp_path / "assistants.json"))
    monkeypatch.setenv("IRON_AGENT_APPROVAL_STORE_PATH", str(tmp_path / "approvals.json"))
    monkeypatch.setenv("IRON_AGENT_ASSISTANTS_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("IRON_AGENT_ASSISTANTS_RUNTIME_ROOT", str(tmp_path / "runtimes"))
    monkeypatch.setenv("IRON_AGENT_USERS_STORE_PATH", str(tmp_path / "users.json"))
    monkeypatch.setenv("IRON_AGENT_USER_BINDINGS_STORE_PATH", str(tmp_path / "bindings.json"))
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
    monkeypatch.setenv("IRON_AGENT_CONTROL_UI_LAUNCH_TICKET_TTL_SECONDS", "90")
    get_settings.cache_clear()
    launch_ticket_store.clear()
    control_ui_relay_session_store.clear()
    return TestClient(create_security_app())


def _admin_headers() -> dict[str, str]:
    return {"X-IronAgent-Internal-Token": get_settings().internal_service_token}


class MockGatewayServer:
    def __init__(self) -> None:
        self.port = 0
        self.connect_frames: Queue[dict[str, object]] = Queue()
        self.forwarded_frames: Queue[dict[str, object]] = Queue()
        self.request_origins: Queue[str | None] = Queue()
        self._ready = threading.Event()
        self._server = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _handler(self, websocket) -> None:
        self.request_origins.put(websocket.request.headers.get("Origin"))
        websocket.send(
            json.dumps(
                {
                    "type": "event",
                    "event": "connect.challenge",
                    "payload": {"nonce": "upstream-nonce", "ts": 1},
                }
        )
            )
        connect_frame = json.loads(websocket.recv())
        self.connect_frames.put(connect_frame)
        websocket.send(
            json.dumps(
                {
                    "type": "res",
                    "id": connect_frame["id"],
                    "ok": True,
                    "payload": {
                        "type": "hello-ok",
                        "protocol": 3,
                        "features": {"methods": ["status"], "events": []},
                        "snapshot": {},
                    },
                }
        )
            )
        forwarded = json.loads(websocket.recv())
        self.forwarded_frames.put(forwarded)
        websocket.send(
            json.dumps(
                {
                    "type": "res",
                    "id": forwarded["id"],
                    "ok": True,
                    "payload": {"ok": True},
                }
        )
            )
        try:
            websocket.recv()
        except Exception:
            return

    def _run(self) -> None:
        with serve(self._handler, "127.0.0.1", 0) as server:
            self._server = server
            self.port = server.socket.getsockname()[1]
            self._ready.set()
            server.serve_forever()

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(timeout=5)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
        self._thread.join(timeout=5)


def test_control_ui_gateway_relay_bridges_launch_ticket_to_local_gateway(
    monkeypatch, tmp_path
) -> None:
    client = _create_client(monkeypatch, tmp_path)
    upstream = MockGatewayServer()
    upstream.start()
    try:
        create_response = client.post(
            "/api/v1/control-ui/launch-tickets",
            json={
                "gateway_url": f"ws://172.28.135.254:{upstream.port}",
                "gateway_token": "gateway-token",
                "session_key": "agent:demo-main:main",
                "control_ui_url": "http://testserver/chat",
            },
            headers=_admin_headers(),
        )
        assert create_response.status_code == 200, create_response.text
        ticket = create_response.json()["ticket"]

        resolve_response = client.post(
            "/api/v1/control-ui/launch-tickets/resolve",
            json={"ticket": ticket},
            headers={"Origin": "http://testserver"},
        )
        assert resolve_response.status_code == 200, resolve_response.text
        resolved = resolve_response.json()

        with client.websocket_connect("/api/v1/control-ui/gateway-relay/ws") as websocket:
            challenge = websocket.receive_json()
            assert challenge["event"] == "connect.challenge"

            websocket.send_json(
                {
                    "type": "req",
                    "id": "browser-connect-1",
                    "method": "connect",
                    "params": {
                        "minProtocol": 3,
                        "maxProtocol": 3,
                        "client": {
                            "id": "ironagent-control-ui",
                            "version": "control-ui",
                            "platform": "Win32",
                            "mode": "webchat",
                        },
                        "role": "operator",
                        "scopes": ["operator.read", "operator.write"],
                        "caps": ["tool-events"],
                        "auth": {"token": resolved["gateway_token"]},
                        "userAgent": "relay-test",
                        "locale": "zh-CN",
                    },
                }
            )
            hello = websocket.receive_json()
            assert hello["ok"] is True
            assert hello["payload"]["type"] == "hello-ok"

            websocket.send_json(
                {
                    "type": "req",
                    "id": "status-1",
                    "method": "status",
                    "params": {},
                }
            )
            status = websocket.receive_json()
            assert status["ok"] is True
            assert status["payload"] == {"ok": True}

        upstream_connect = upstream.connect_frames.get(timeout=5)
        assert upstream_connect["method"] == "connect"
        upstream_params = upstream_connect["params"]
        assert upstream_params["client"]["id"] == "ironagent-control-ui"
        assert upstream_params["client"]["mode"] == "webchat"
        assert upstream_params["auth"]["token"] == "gateway-token"
        assert "device" not in upstream_params
        assert upstream_params["scopes"] == ["operator.read", "operator.write"]
        assert upstream.request_origins.get(timeout=5) == f"http://172.28.135.254:{upstream.port}"

        forwarded = upstream.forwarded_frames.get(timeout=5)
        assert forwarded == {
            "type": "req",
            "id": "status-1",
            "method": "status",
            "params": {},
        }
    finally:
        upstream.stop()
        launch_ticket_store.clear()
        control_ui_relay_session_store.clear()
        get_settings.cache_clear()


def test_build_control_ui_relay_upstream_candidates_prefers_original_then_container_host() -> None:
    assert build_control_ui_relay_upstream_candidates("ws://172.28.135.254:2049/api") == [
        "ws://172.28.135.254:2049/api",
        "ws://host.docker.internal:2049/api",
        "ws://127.0.0.1:2049/api",
    ]


def test_build_control_ui_relay_upstream_origin_uses_gateway_host() -> None:
    assert (
        build_control_ui_relay_upstream_origin("ws://172.28.135.254:2049/api")
        == "http://172.28.135.254:2049"
    )
    assert (
        build_control_ui_relay_upstream_origin("wss://gateway.example.com/control")
        == "https://gateway.example.com"
    )
