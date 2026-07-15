import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.shared.auth import internal_service_auth_headers, require_internal_service_token
from src.shared.config import Settings, get_settings


def test_settings_reads_iron_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IRON_AGENT_LOG_LEVEL", "DEBUG")

    assert Settings().log_level == "DEBUG"


def test_settings_prefers_ironagent_env_without_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IRONAGENT_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("IRON_AGENT_LOG_LEVEL", "DEBUG")

    assert Settings().log_level == "WARNING"


def test_settings_reads_iron_agent_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IRON_AGENT_RUNTIME_SOURCE_ROOT", "/tmp/ironagent-runtime")
    monkeypatch.setenv("IRON_AGENT_RUNTIME_CONTAINER_IMAGE", "ironagent-runtime:test")
    monkeypatch.setenv("IRON_AGENT_RUNTIME_HOOK_SERVICE_URL", "http://security-service:8000")
    monkeypatch.setenv("IRON_AGENT_RUNTIME_GATEWAY_HOST", "0.0.0.0")
    monkeypatch.setenv("IRON_AGENT_RUNTIME_HTTP_PROXY", "http://proxy.internal:7890")

    settings = Settings()

    assert str(settings.iron_agent_source_root) == "/tmp/ironagent-runtime"
    assert settings.iron_agent_container_image == "ironagent-runtime:test"
    assert settings.iron_agent_hook_service_url == "http://security-service:8000"
    assert settings.iron_agent_gateway_host == "0.0.0.0"
    assert settings.iron_agent_http_proxy == "http://proxy.internal:7890"


def test_settings_reads_ironagent_runtime_env_without_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IRONAGENT_RUNTIME_SOURCE_ROOT", "/tmp/ironagent-runtime")
    monkeypatch.setenv("IRONAGENT_RUNTIME_CONTAINER_IMAGE", "ironagent-runtime:test")
    monkeypatch.setenv("IRONAGENT_RUNTIME_HOOK_SERVICE_URL", "http://security-service:8000")
    monkeypatch.setenv("IRONAGENT_RUNTIME_GATEWAY_HOST", "0.0.0.0")

    settings = Settings()

    assert str(settings.iron_agent_source_root) == "/tmp/ironagent-runtime"
    assert settings.iron_agent_container_image == "ironagent-runtime:test"
    assert settings.iron_agent_hook_service_url == "http://security-service:8000"
    assert settings.iron_agent_gateway_host == "0.0.0.0"


def test_internal_service_auth_headers_writes_iron_agent_name() -> None:
    headers = internal_service_auth_headers(Settings(internal_service_token="secret"))

    assert headers["X-IronAgent-Internal-Token"] == "secret"
    assert list(headers) == ["X-IronAgent-Internal-Token"]


def test_require_internal_service_token_accepts_iron_agent_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IRON_AGENT_INTERNAL_SERVICE_TOKEN", "secret")
    get_settings.cache_clear()
    app = FastAPI()

    @app.get("/probe")
    def probe(_admin: None = Depends(require_internal_service_token)) -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)

    assert (
        client.get("/probe", headers={"X-IronAgent-Internal-Token": "secret"}).status_code == 200
    )
    get_settings.cache_clear()
