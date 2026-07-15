import json
from types import SimpleNamespace

import pytest

from src.model_registry.service import ModelRegistryService
from src.runtime.models import AssistantIronAgentConfig
from src.shared.config import get_settings


def test_probe_host_openai_codex_model_reports_missing_auth(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IRON_AGENT_HOST_IRON_AGENT_ROOT", str(tmp_path / "host"))
    get_settings.cache_clear()
    try:
        service = ModelRegistryService(store_path=tmp_path / "registry.json")
        model = service.create_model(
            name="openai-codex/gpt-5.4",
            provider="host_iron_agent",
        )

        result = service.probe_model(model.model_id)
    finally:
        get_settings.cache_clear()

    assert result is not None
    assert result["ok"] is False
    assert result["status"] == "auth_missing"
    assert "auth-profiles.json" in result["message"]


def test_probe_host_openai_codex_model_reports_configured_auth(monkeypatch, tmp_path) -> None:
    host_root = tmp_path / "host"
    agent_root = host_root / "agents" / "main" / "agent"
    agent_root.mkdir(parents=True)
    (agent_root / "auth-profiles.json").write_text(
        json.dumps({"profiles": {"openai-codex:default": {"provider": "openai-codex"}}}),
        encoding="utf-8",
    )
    (agent_root / "models.json").write_text(
        json.dumps({"providers": {"openai-codex": {"api": "openai-codex-responses"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("IRON_AGENT_HOST_IRON_AGENT_ROOT", str(host_root))
    get_settings.cache_clear()
    try:
        service = ModelRegistryService(store_path=tmp_path / "registry.json")
        model = service.create_model(
            name="openai-codex/gpt-5.4",
            provider="host_iron_agent",
        )

        result = service.probe_model(model.model_id)
    finally:
        get_settings.cache_clear()

    assert result is not None
    assert result["ok"] is True
    assert result["status"] == "configured"


def test_create_model_rejects_placeholder_api_key(tmp_path) -> None:
    service = ModelRegistryService(store_path=tmp_path / "registry.json")
    with pytest.raises(ValueError, match="placeholder"):
        service.create_model(
            name="dashscope-coding/qwen3-coder-next",
            provider="dashscope-coding",
            url="https://coding.dashscope.aliyuncs.com/v1",
            api_key="***",
        )


def test_import_from_assistants_drops_placeholder_api_key(tmp_path) -> None:
    service = ModelRegistryService(store_path=tmp_path / "registry.json")
    record = SimpleNamespace(
        iron_agent_config=AssistantIronAgentConfig(
            primary_model="dashscope-coding/qwen3-coder-next",
            auth_mode="provider_api_key",
            provider_id="dashscope-coding",
            provider_base_url="https://coding.dashscope.aliyuncs.com/v1",
            provider_api_key="***",
        )
    )

    items = service.import_from_assistants([record])

    imported = next(m for m in items if m.provider == "dashscope-coding")
    # 占位符不能作为可用 key 导入注册表
    assert imported.api_key == ""
