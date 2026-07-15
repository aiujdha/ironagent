import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.assistants.models import AssistantRecord
from src.assistants.service import AssistantService
from src.runtime.models import AssistantIronAgentConfig, AssistantWorkspaceConfig
from src.runtime.provisioner import DestroyRuntimeResult, ProvisionResult, RuntimeProbeResult
from src.shared.config import get_settings
from src.shared.json_store import write_json_object


@pytest.fixture(autouse=True)
def isolated_assistant_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IRON_AGENT_ASSISTANTS_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("IRON_AGENT_ASSISTANTS_RUNTIME_ROOT", str(tmp_path / "runtimes"))
    monkeypatch.setenv("IRON_AGENT_DEFAULT_WORKSPACE_ROOT_ID", "default")
    get_settings.cache_clear()

    yield

    get_settings.cache_clear()


class FakeProvisioner:
    def __init__(
        self,
        result: ProvisionResult,
        probe_result: RuntimeProbeResult | None = None,
    ) -> None:
        self.result = result
        self.calls: list[
            tuple[
                str,
                AssistantIronAgentConfig | None,
                AssistantWorkspaceConfig | None,
            ]
        ] = []
        self.probe_calls: list[str] = []
        self.destroy_calls: list[tuple[str, str | None, str | None]] = []
        self.probe_overrides: dict[str, RuntimeProbeResult] = {}
        self.probe_result = probe_result or RuntimeProbeResult(
            container_name=result.container_name,
            container_exists=result.container_id is not None,
            container_id=result.container_id,
            container_state="running" if result.status == "running" else "exited",
            status=result.status,
            gateway_host=result.gateway_host,
            gateway_port=result.gateway_port,
            gateway_url=result.gateway_url,
            last_error=result.last_error,
        )
        self.restart_result = self.probe_result
        self.destroy_result = DestroyRuntimeResult(container_name=result.container_name)

    def ensure_runtime(
        self,
        *,
        assistant_id: str,
        assistant_name: str,
        capability_profile: str = "onlyread",
        model_config: AssistantIronAgentConfig | None = None,
        workspace_config: AssistantWorkspaceConfig | None = None,
    ) -> ProvisionResult:
        self.calls.append(
            (
                assistant_id,
                assistant_name,
                capability_profile,
                model_config,
                workspace_config,
        )
            )
        return self.result

    def probe_runtime(self, *, assistant_id: str) -> RuntimeProbeResult:
        self.probe_calls.append(assistant_id)
        return self.probe_overrides.get(assistant_id, self.probe_result)

    def restart_runtime(self, *, assistant_id: str) -> RuntimeProbeResult:
        return self.restart_result

    def destroy_runtime(
        self,
        *,
        assistant_id: str,
        workspace_root: str | None = None,
        state_root: str | None = None,
    ) -> DestroyRuntimeResult:
        self.destroy_calls.append((assistant_id, workspace_root, state_root))
        return self.destroy_result


class FakeInitializedAssistantRepository:
    def __init__(self) -> None:
        self.records: dict[str, AssistantRecord] = {}

    def is_initialized(self) -> bool:
        return True

    async def alist_assistants(self) -> list[AssistantRecord]:
        return list(self.records.values())

    def list_assistants(self) -> list[AssistantRecord]:
        return list(self.records.values())

    async def aget_assistant(self, assistant_id: str) -> AssistantRecord | None:
        return self.records.get(assistant_id)

    def get_assistant(self, assistant_id: str) -> AssistantRecord | None:
        return self.records.get(assistant_id)

    async def aupsert_assistant(self, record: AssistantRecord) -> AssistantRecord:
        self.records[record.assistant_id] = record
        return record

    def upsert_assistant(self, record: AssistantRecord) -> AssistantRecord:
        self.records[record.assistant_id] = record
        return record

    async def adelete_assistant(self, assistant_id: str) -> bool:
        return self.records.pop(assistant_id, None) is not None

    def delete_assistant(self, assistant_id: str) -> bool:
        return self.records.pop(assistant_id, None) is not None


def test_assistant_service_seeds_default_assistant(tmp_path) -> None:
    service = AssistantService(store_path=tmp_path / "assistants.json")

    assistants = service.list_assistants()

    assert len(assistants) == 1
    assert assistants[0].assistant_id == "demo-main"
    assert assistants[0].capability_profile == "onlyread"


def test_assistant_service_does_not_seed_default_assistant_for_initialized_empty_repository_by_default(
    monkeypatch,
) -> None:
    monkeypatch.delenv("IRON_AGENT_SEED_DEFAULT_ASSISTANTS", raising=False)
    get_settings.cache_clear()
    repository = FakeInitializedAssistantRepository()
    service = AssistantService(repository=repository)

    assistants = service.list_assistants()

    assert assistants == []
    assert service.get_assistant("demo-main") is None
    get_settings.cache_clear()


def test_assistant_service_can_seed_default_assistant_for_initialized_empty_repository(
    monkeypatch,
) -> None:
    monkeypatch.setenv("IRON_AGENT_SEED_DEFAULT_ASSISTANTS", "true")
    get_settings.cache_clear()
    repository = FakeInitializedAssistantRepository()
    service = AssistantService(repository=repository)

    assistants = service.list_assistants()

    assert len(assistants) == 1
    assert assistants[0].assistant_id == "demo-main"
    assert service.get_assistant("demo-main") is not None
    get_settings.cache_clear()


def test_assistant_service_honors_storage_backend_when_env_store_path_exists(
    monkeypatch,
    tmp_path,
) -> None:
    repository = FakeInitializedAssistantRepository()
    monkeypatch.setenv("IRON_AGENT_STORAGE_BACKEND", "mysql")
    monkeypatch.setenv("IRON_AGENT_ASSISTANTS_STORE_PATH", str(tmp_path / "assistants.json"))
    get_settings.cache_clear()
    monkeypatch.setattr("src.assistants.service.build_assistant_repository", lambda: repository)

    service = AssistantService(provisioner=object())

    assert service.repository is repository
    get_settings.cache_clear()


def test_assistant_service_skips_runtime_state_recovery_for_database_repository(
    monkeypatch,
    tmp_path,
) -> None:
    runtime_root = tmp_path / "runtimes"
    state_root = runtime_root / "orphan" / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "ironagent.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("IRON_AGENT_ASSISTANTS_RUNTIME_ROOT", str(runtime_root))
    get_settings.cache_clear()

    repository = FakeInitializedAssistantRepository()
    repository.upsert_assistant(
        AssistantRecord(assistant_id="db-only", name="DB Only")
    )
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-db-only",
            container_id="container-db-only",
            workspace_root=str(tmp_path / "workspaces" / "db-only"),
            workspace_root_id="default",
            workspace_slug="db-only",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(runtime_root / "db-only" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_token="ironagent-local-db-only",
            updated_at=datetime.now(UTC),
    )
        )
    service = AssistantService(repository=repository, provisioner=provisioner)

    assistants = service.list_assistants()

    assert [item.assistant_id for item in assistants] == ["db-only"]
    assert provisioner.probe_calls == []


def test_assistant_service_recovers_missing_runtime_record_from_local_state(
    tmp_path,
    monkeypatch,
) -> None:
    runtime_root = tmp_path / "runtimes"
    workspace_root = tmp_path / "workspaces"
    state_root = runtime_root / "001" / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    (workspace_root / "001").mkdir(parents=True, exist_ok=True)
    (state_root / "ironagent.json").write_text(
        json.dumps(
            {
                "gateway": {"auth": {"token": "ironagent-local-001"}},
                "agents": {
                    "defaults": {"model": {"primary": "modelstudio/qwen3-coder-next"}},
                    "list": [{"id": "001", "name": "001"}],
                },
                "tools": {
                    "allow": [
                        "read",
                        "write",
                        "edit",
                        "apply_patch",
                        "exec",
                        "process",
                        "subagents",
                    ]
                },
                "models": {
                    "providers": {
                        "modelstudio": {
                            "baseUrl": "https://coding.dashscope.aliyuncs.com/v1",
                            "apiKey": "secret-key",
                    }
                        }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("IRON_AGENT_ASSISTANTS_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setenv("IRON_AGENT_ASSISTANTS_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("IRON_AGENT_DEFAULT_WORKSPACE_ROOT_ID", "default")
    get_settings.cache_clear()

    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-demo-main",
            container_id="container-demo",
            workspace_root=str(workspace_root / "demo-main"),
            workspace_root_id="default",
            workspace_slug="demo-main",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(runtime_root / "demo-main" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_host="127.0.0.1",
            gateway_port=49153,
            gateway_url="http://127.0.0.1:49153",
            gateway_token="ironagent-local-demo-main",
            updated_at=datetime.now(UTC),
    )
        )
    provisioner.probe_overrides["001"] = RuntimeProbeResult(
        container_name="ironagent-assistant-001",
        container_exists=True,
        container_id="container-001",
        container_state="running",
        status="running",
        gateway_host="172.28.135.254",
        gateway_port=3221,
        gateway_url="http://172.28.135.254:3221",
    )

    service = AssistantService(store_path=tmp_path / "assistants.json", provisioner=provisioner)

    try:
        recovered = service.get_assistant("001")

        assert recovered is not None
        assert recovered.name == "001"
        assert recovered.capability_profile == "dangerous"
        assert recovered.runtime.container_name == "ironagent-assistant-001"
        assert recovered.runtime.gateway_url == "http://172.28.135.254:3221"
        assert recovered.runtime.workspace_root == str((workspace_root / "001").resolve())
        assert recovered.iron_agent_config.primary_model == "modelstudio/qwen3-coder-next"
        assert recovered.iron_agent_config.provider_id == "modelstudio"
        assert recovered.iron_agent_config.provider_base_url == "https://coding.dashscope.aliyuncs.com/v1"
        assert recovered.iron_agent_config.provider_api_key == "secret-key"
    finally:
        get_settings.cache_clear()


def test_assistant_service_prefers_explicit_runtime_capability_profile(tmp_path) -> None:
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-demo-main",
            container_id="container-demo",
            workspace_root=str(tmp_path / "workspaces" / "demo-main"),
            workspace_root_id="default",
            workspace_slug="demo-main",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "demo-main" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_host="127.0.0.1",
            gateway_port=49153,
            gateway_url="http://127.0.0.1:49153",
            gateway_token="ironagent-local-demo-main",
            updated_at=datetime.now(UTC),
        )
    )
    service = AssistantService(store_path=tmp_path / "assistants.json", provisioner=provisioner)

    profile = service._capability_profile_from_runtime_state(
        {
            "tools": {
                "capability_profile": "work_no_exec",
                "allow": ["read", "write", "edit", "apply_patch"],
            }
        }
    )

    assert profile == "work_no_exec"


def test_model_config_from_runtime_state_recovers_key_from_headers_not_placeholder(
    tmp_path,
) -> None:
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-demo",
            container_id="container-x",
            workspace_root=str(tmp_path / "workspaces" / "demo"),
            workspace_root_id="default",
            workspace_slug="demo",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "demo" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_host="127.0.0.1",
            gateway_port=49153,
            gateway_url="http://127.0.0.1:49153",
            gateway_token="ironagent-local-demo",
            updated_at=datetime.now(UTC),
    )
        )
    service = AssistantService(store_path=tmp_path / "assistants.json", provisioner=provisioner)

    config = service._model_config_from_runtime_state(
        {
            "agents": {"defaults": {"model": {"primary": "dashscope-coding/qwen3-coder-next"}}},
            "models": {
                "providers": {
                    "dashscope-coding": {
                        "baseUrl": "https://coding.dashscope.aliyuncs.com/v1",
                        "apiKey": "***",
                        "authHeader": False,
                        "headers": {"Authorization": "sk-real-secret"},
                    }
                }
            },
        }
    )

    # 真实 key 在 headers.Authorization;占位符 "***" 绝不能被当作凭据恢复。
    assert config.provider_api_key == "sk-real-secret"
    assert config.provider_id == "dashscope-coding"
    assert config.provider_base_url == "https://coding.dashscope.aliyuncs.com/v1"


def test_model_config_from_runtime_state_drops_placeholder_without_header(tmp_path) -> None:
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-demo2",
            container_id="container-y",
            workspace_root=str(tmp_path / "workspaces" / "demo2"),
            workspace_root_id="default",
            workspace_slug="demo2",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "demo2" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_host="127.0.0.1",
            gateway_port=49154,
            gateway_url="http://127.0.0.1:49154",
            gateway_token="ironagent-local-demo2",
            updated_at=datetime.now(UTC),
    )
        )
    service = AssistantService(store_path=tmp_path / "assistants.json", provisioner=provisioner)

    config = service._model_config_from_runtime_state(
        {
            "agents": {"defaults": {"model": {"primary": "dashscope-coding/qwen3-coder-next"}}},
            "models": {
                "providers": {
                    "dashscope-coding": {
                        "baseUrl": "https://coding.dashscope.aliyuncs.com/v1",
                        "apiKey": "***",
                    }
                }
            },
        }
    )

    # 占位符没有对应的真实 key → 留空,绝不把 "***" 回写进存储配置。
    assert config.provider_api_key == ""


def test_assistant_service_upserts_and_persists_runtime_metadata(tmp_path) -> None:
    store_path = tmp_path / "assistants.json"
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-1",
            container_id="container-123",
            workspace_root=str(tmp_path / "workspaces" / "assistant-1"),
            workspace_root_id="default",
            workspace_slug="assistant-1",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "assistant-1" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_host="127.0.0.1",
            gateway_port=49153,
            gateway_url="http://127.0.0.1:49153",
            gateway_token="ironagent-local-assistant-1",
            updated_at=datetime.now(UTC),
    )
        )
    service = AssistantService(store_path=store_path, provisioner=provisioner)

    saved = service.upsert_assistant(
        assistant_id="assistant-1",
        name="Demo Writer",
        capability_profile="onlywrite",
        notes="demo",
        model_config=AssistantIronAgentConfig(
            primary_model="modelstudio/qwen3-coder-next",
            provider_id="modelstudio",
            provider_base_url="https://coding.dashscope.aliyuncs.com/v1",
            provider_api_key="secret-key",
        ),
        workspace_config=AssistantWorkspaceConfig(
            root_id="default",
            workspace_slug="assistant-1",
        ),
    )

    reloaded = AssistantService(store_path=store_path, provisioner=provisioner).get_assistant(
        "assistant-1"
    )

    assert saved.assistant_id == "assistant-1"
    assert saved.runtime.status == "running"
    assert saved.runtime.container_name == "ironagent-assistant-assistant-1"
    assert reloaded is not None
    assert reloaded.name == "Demo Writer"
    assert reloaded.capability_profile == "onlywrite"
    assert reloaded.iron_agent_config.primary_model == "modelstudio/qwen3-coder-next"
    assert reloaded.iron_agent_config.auth_mode == "provider_api_key"
    assert reloaded.iron_agent_config.provider_id == "modelstudio"
    assert reloaded.iron_agent_config.provider_base_url == "https://coding.dashscope.aliyuncs.com/v1"
    assert reloaded.iron_agent_config.provider_api_key == "secret-key"
    assert reloaded.workspace_config.root_id == "default"
    assert reloaded.workspace_config.workspace_slug == "assistant-1"
    assert reloaded.runtime.workspace_root == str(tmp_path / "workspaces" / "assistant-1")
    assert reloaded.runtime.workspace_root_id == "default"
    assert reloaded.runtime.workspace_slug == "assistant-1"
    assert reloaded.runtime.state_root == str(tmp_path / "runtimes" / "assistant-1" / "state")
    assert reloaded.runtime.gateway_url == "http://127.0.0.1:49153"
    assert reloaded.runtime.gateway_token == "ironagent-local-assistant-1"
    assert provisioner.calls == [
        (
            "assistant-1",
            "Demo Writer",
            "onlywrite",
            AssistantIronAgentConfig(
                primary_model="modelstudio/qwen3-coder-next",
                provider_id="modelstudio",
                provider_base_url="https://coding.dashscope.aliyuncs.com/v1",
                provider_api_key="secret-key",
            ),
            AssistantWorkspaceConfig(
                root_id="default",
                workspace_slug="assistant-1",
            ),
        )
    ]


def test_assistant_service_persists_failed_runtime_state(tmp_path) -> None:
    store_path = tmp_path / "assistants.json"
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-2",
            container_id=None,
            workspace_root=str(tmp_path / "workspaces" / "assistant-2"),
            workspace_root_id="default",
            workspace_slug="assistant-2",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "assistant-2" / "state"),
            status="failed",
            image="ironagent-runtime:dev",
            gateway_token="ironagent-local-assistant-2",
            last_error="docker unavailable",
            updated_at=datetime.now(UTC),
    )
        )
    service = AssistantService(store_path=store_path, provisioner=provisioner)

    saved = service.upsert_assistant(
        assistant_id="assistant-2",
        name="Failed Runtime",
        capability_profile="onlyread",
    )

    assert saved.runtime.status == "failed"
    assert saved.runtime.gateway_token == "ironagent-local-assistant-2"
    assert saved.runtime.last_error == "docker unavailable"


def test_restart_assistant_refreshes_recoverable_failed_probe(tmp_path) -> None:
    store_path = tmp_path / "assistants.json"
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-3",
            container_id="container-123",
            workspace_root=str(tmp_path / "workspaces" / "assistant-3"),
            workspace_root_id="default",
            workspace_slug="assistant-3",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "assistant-3" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_host="127.0.0.1",
            gateway_port=49153,
            gateway_url="http://127.0.0.1:49153",
            gateway_token="ironagent-local-assistant-3",
            updated_at=datetime.now(UTC),
    )
        )
    provisioner.restart_result = RuntimeProbeResult(
        container_name="ironagent-assistant-assistant-3",
        container_exists=True,
        container_id="container-123",
        container_state="running",
        status="failed",
        last_error="gateway endpoint not published yet",
    )
    provisioner.probe_overrides["assistant-3"] = RuntimeProbeResult(
        container_name="ironagent-assistant-assistant-3",
        container_exists=True,
        container_id="container-123",
        container_state="running",
        status="running",
        gateway_host="127.0.0.1",
        gateway_port=49153,
        gateway_url="http://127.0.0.1:49153",
    )
    service = AssistantService(store_path=store_path, provisioner=provisioner)
    service.upsert_assistant(
        assistant_id="assistant-3",
        name="Restartable",
        capability_profile="onlyread",
    )

    restarted = service.restart_assistant("assistant-3")

    assert restarted is not None
    assert restarted.runtime.status == "running"
    assert provisioner.probe_calls == ["assistant-3"]


def test_mark_assistant_restarting_updates_state_without_runtime_restart(tmp_path) -> None:
    store_path = tmp_path / "assistants.json"
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-restart",
            container_id="container-restart",
            workspace_root=str(tmp_path / "workspaces" / "assistant-restart"),
            workspace_root_id="default",
            workspace_slug="assistant-restart",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "assistant-restart" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_host="127.0.0.1",
            gateway_port=49155,
            gateway_url="http://127.0.0.1:49155",
            gateway_token="ironagent-local-assistant-restart",
            updated_at=datetime.now(UTC),
    )
        )
    service = AssistantService(store_path=store_path, provisioner=provisioner)
    service.upsert_assistant(
        assistant_id="assistant-restart",
        name="Restarting",
        capability_profile="onlyread",
    )

    marked = service.mark_assistant_restarting("assistant-restart")

    assert marked is not None
    assert marked.runtime.status == "restarting"
    assert marked.runtime.desired_state == "running"
    assert marked.runtime.observed_state == "restarting"
    assert marked.runtime.sync_state == "pending"
    assert marked.runtime.gateway_url == "http://127.0.0.1:49155"
    assert [call[0] for call in provisioner.calls] == ["assistant-restart"]


def test_restart_assistant_reprovisions_when_runtime_manifest_is_missing(tmp_path) -> None:
    store_path = tmp_path / "assistants.json"
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-4",
            container_id="container-456",
            workspace_root=str(tmp_path / "workspaces" / "assistant-4"),
            workspace_root_id="default",
            workspace_slug="assistant-4",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "assistant-4" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_host="127.0.0.1",
            gateway_port=49156,
            gateway_url="http://127.0.0.1:49156",
            gateway_token="ironagent-local-assistant-4",
            updated_at=datetime.now(UTC),
    )
        )
    provisioner.restart_result = RuntimeProbeResult(
        container_name="ironagent-assistant-assistant-4",
        container_exists=False,
        container_state="missing",
        status="failed",
        last_error="runtime manifest not found",
    )
    service = AssistantService(store_path=store_path, provisioner=provisioner)
    service.upsert_assistant(
        assistant_id="assistant-4",
        name="Restartable Without Manifest",
        capability_profile="onlyread",
    )

    restarted = service.restart_assistant("assistant-4")

    assert restarted is not None
    assert restarted.runtime.status == "running"
    assert [call[0] for call in provisioner.calls] == ["assistant-4", "assistant-4"]


def test_restart_assistant_reprovisions_when_k8s_pod_was_deleted(tmp_path) -> None:
    store_path = tmp_path / "assistants.json"
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-5",
            container_id="container-567",
            workspace_root=str(tmp_path / "workspaces" / "assistant-5"),
            workspace_root_id="default",
            workspace_slug="assistant-5",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "assistant-5" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_host="127.0.0.1",
            gateway_port=49157,
            gateway_url="http://127.0.0.1:49157",
            gateway_token="ironagent-local-assistant-5",
            updated_at=datetime.now(UTC),
    )
        )
    provisioner.restart_result = RuntimeProbeResult(
        container_name="ironagent-assistant-assistant-5",
        container_exists=False,
        container_state="missing",
        status="failed",
        last_error="runtime pod deleted; reprovision required",
    )
    service = AssistantService(store_path=store_path, provisioner=provisioner)
    service.upsert_assistant(
        assistant_id="assistant-5",
        name="K8s Restartable",
        capability_profile="onlyread",
    )

    restarted = service.restart_assistant("assistant-5")

    assert restarted is not None
    assert restarted.runtime.status == "running"
    assert [call[0] for call in provisioner.calls] == ["assistant-5", "assistant-5"]


def test_assistant_service_preserves_existing_api_key_when_form_leaves_it_blank(tmp_path) -> None:
    store_path = tmp_path / "assistants.json"
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-3",
            container_id="container-789",
            workspace_root=str(tmp_path / "workspaces" / "assistant-3"),
            workspace_root_id="default",
            workspace_slug="assistant-3",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "assistant-3" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_host="127.0.0.1",
            gateway_port=49155,
            gateway_url="http://127.0.0.1:49155",
            gateway_token="ironagent-local-assistant-3",
            updated_at=datetime.now(UTC),
    )
        )
    service = AssistantService(store_path=store_path, provisioner=provisioner)

    service.upsert_assistant(
        assistant_id="assistant-3",
        name="Demo Analyst",
        capability_profile="onlyread",
        model_config=AssistantIronAgentConfig(
            primary_model="modelstudio/qwen3-coder-next",
            provider_id="modelstudio",
            provider_base_url="https://coding.dashscope.aliyuncs.com/v1",
            provider_api_key="secret-key",
        ),
    )
    provisioner.calls.clear()

    updated = service.upsert_assistant(
        assistant_id="assistant-3",
        name="Demo Analyst",
        capability_profile="onlyread",
        model_config=AssistantIronAgentConfig(
            primary_model="modelstudio/qwen3-coder-next",
            provider_id="modelstudio",
            provider_base_url="https://coding.dashscope.aliyuncs.com/v1",
            provider_api_key="",
        ),
    )

    assert updated.iron_agent_config.provider_api_key == "secret-key"
    assert updated.iron_agent_config.auth_mode == "provider_api_key"
    assert provisioner.calls == [
        (
            "assistant-3",
            "Demo Analyst",
            "onlyread",
            AssistantIronAgentConfig(
                primary_model="modelstudio/qwen3-coder-next",
                provider_id="modelstudio",
                provider_base_url="https://coding.dashscope.aliyuncs.com/v1",
                provider_api_key="secret-key",
            ),
            AssistantWorkspaceConfig(
                root_id="default",
                workspace_slug="assistant-3",
            ),
        )
    ]


def test_assistant_service_rotating_only_the_api_key_keeps_provider_id_and_base_url(
    tmp_path,
) -> None:
    store_path = tmp_path / "assistants.json"
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-rot",
            container_id="container-rot",
            workspace_root=str(tmp_path / "workspaces" / "assistant-rot"),
            workspace_root_id="default",
            workspace_slug="assistant-rot",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "assistant-rot" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_host="127.0.0.1",
            gateway_port=49156,
            gateway_url="http://127.0.0.1:49156",
            gateway_token="ironagent-local-assistant-rot",
            updated_at=datetime.now(UTC),
    )
        )
    service = AssistantService(store_path=store_path, provisioner=provisioner)

    service.upsert_assistant(
        assistant_id="assistant-rot",
        name="Rotate Key",
        capability_profile="onlyread",
        model_config=AssistantIronAgentConfig(
            primary_model="modelstudio/qwen3-coder-next",
            provider_id="modelstudio",
            provider_base_url="https://coding.dashscope.aliyuncs.com/v1",
            provider_api_key="old-key",
        ),
    )

    # Caller rotates only the key; provider_id / base_url left blank must NOT
    # clear the stored provider routing.
    updated = service.upsert_assistant(
        assistant_id="assistant-rot",
        name="Rotate Key",
        capability_profile="onlyread",
        model_config=AssistantIronAgentConfig(
            primary_model="modelstudio/qwen3-coder-next",
            auth_mode="provider_api_key",
            provider_id="",
            provider_base_url="",
            provider_api_key="new-key",
        ),
    )

    assert updated.iron_agent_config.provider_api_key == "new-key"
    assert updated.iron_agent_config.provider_id == "modelstudio"
    assert updated.iron_agent_config.provider_base_url == (
        "https://coding.dashscope.aliyuncs.com/v1"
    )
    assert updated.iron_agent_config.auth_mode == "provider_api_key"


def test_assistant_service_masked_api_key_placeholder_keeps_stored_secret(tmp_path) -> None:
    store_path = tmp_path / "assistants.json"
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-mask",
            container_id="container-mask",
            workspace_root=str(tmp_path / "workspaces" / "assistant-mask"),
            workspace_root_id="default",
            workspace_slug="assistant-mask",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "assistant-mask" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_host="127.0.0.1",
            gateway_port=49157,
            gateway_url="http://127.0.0.1:49157",
            gateway_token="ironagent-local-assistant-mask",
            updated_at=datetime.now(UTC),
    )
        )
    service = AssistantService(store_path=store_path, provisioner=provisioner)

    service.upsert_assistant(
        assistant_id="assistant-mask",
        name="Masked Key",
        capability_profile="onlyread",
        model_config=AssistantIronAgentConfig(
            primary_model="modelstudio/qwen3-coder-next",
            provider_id="modelstudio",
            provider_base_url="https://coding.dashscope.aliyuncs.com/v1",
            provider_api_key="real-secret",
        ),
    )

    # The UI echoes back "******" for the configured key; it must not be stored
    # as the real secret.
    updated = service.upsert_assistant(
        assistant_id="assistant-mask",
        name="Masked Key",
        capability_profile="onlyread",
        model_config=AssistantIronAgentConfig(
            primary_model="modelstudio/qwen3-coder-next",
            auth_mode="provider_api_key",
            provider_id="modelstudio",
            provider_base_url="https://coding.dashscope.aliyuncs.com/v1",
            provider_api_key="******",
        ),
    )

    assert updated.iron_agent_config.provider_api_key == "real-secret"


def test_assistant_service_falls_back_to_configured_workspace_when_runtime_path_is_stale(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_root = tmp_path / "workspaces" / "demo-assistant"
    state_root = tmp_path / "runtimes" / "demo-assistant" / "state"
    workspace_root.mkdir(parents=True, exist_ok=True)
    state_root.mkdir(parents=True, exist_ok=True)
    store_path = tmp_path / "assistants.json"
    assistant = {
        "assistant_id": "demo-assistant",
        "name": "Demo Assistant",
        "capability_profile": "onlyread",
        "workspace_config": {
            "root_id": "default",
            "workspace_slug": "demo-assistant",
        },
        "runtime": {
            "workspace_root": "/home/oldjohn/project/ironagent/data/workspaces/demo-assistant",
        },
    }
    write_json_object(store_path, {"assistants": [assistant]})
    monkeypatch.setattr(
        "src.assistants.service.get_settings",
        lambda: get_settings().model_copy(
            update={"assistants_workspace_root": tmp_path / "workspaces"}
        ),
    )
    service = AssistantService(store_path=store_path)
    assistant = service.get_assistant("demo-assistant")
    assert assistant is not None

    resolved = service._resolve_workspace_root_path(assistant)

    assert resolved == str(workspace_root.resolve())


def test_assistant_service_recomputes_managed_workspace_when_runtime_metadata_is_stale(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace_root = tmp_path / "workspaces" / "demo-assistant"
    state_root = tmp_path / "runtimes" / "demo-assistant" / "state"
    workspace_root.mkdir(parents=True, exist_ok=True)
    state_root.mkdir(parents=True, exist_ok=True)
    store_path = tmp_path / "assistants.json"
    assistant = {
        "assistant_id": "demo-assistant",
        "name": "Demo Assistant",
        "capability_profile": "onlyread",
        "workspace_config": {
            "root_id": "default",
            "workspace_slug": "demo-assistant",
        },
        "runtime": {
            "workspace_root": (
                "/Users/hufangyu/Desktop/ironagent-main/data/workspaces/demo-assistant"
            ),
            "workspace_root_id": "default",
            "workspace_slug": "demo-assistant",
            "state_root": (
                "/Users/hufangyu/Desktop/ironagent-main/data/runtimes/demo-assistant/state"
            ),
        },
    }
    write_json_object(store_path, {"assistants": [assistant]})
    monkeypatch.setattr(
        "src.assistants.service.get_settings",
        lambda: get_settings().model_copy(
            update={
                "assistants_workspace_root": tmp_path / "workspaces",
                "assistants_runtime_root": tmp_path / "runtimes",
            }
        ),
    )
    service = AssistantService(store_path=store_path)
    assistant = service.get_assistant("demo-assistant")
    assert assistant is not None

    resolved_workspace = service._resolve_workspace_root_path(assistant)
    resolved_state = service._resolve_state_root_path(assistant)

    assert resolved_workspace == str(workspace_root.resolve())
    assert resolved_state == str(state_root.resolve())


def test_assistant_service_clears_stale_api_key_when_switching_to_builtin_provider(
    tmp_path,
) -> None:
    store_path = tmp_path / "assistants.json"
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-4",
            container_id="container-999",
            workspace_root=str(tmp_path / "workspaces" / "assistant-4"),
            workspace_root_id="default",
            workspace_slug="assistant-4",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "assistant-4" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_host="127.0.0.1",
            gateway_port=49160,
            gateway_url="http://127.0.0.1:49160",
            gateway_token="ironagent-local-assistant-4",
            updated_at=datetime.now(UTC),
    )
        )
    service = AssistantService(store_path=store_path, provisioner=provisioner)

    service.upsert_assistant(
        assistant_id="assistant-4",
        name="Demo Switcher",
        capability_profile="dangerous",
        model_config=AssistantIronAgentConfig(
            primary_model="modelstudio/qwen3-coder-next",
            provider_id="modelstudio",
            provider_base_url="https://coding.dashscope.aliyuncs.com/v1",
            provider_api_key="secret-key",
        ),
    )
    provisioner.calls.clear()

    updated = service.upsert_assistant(
        assistant_id="assistant-4",
        name="Demo Switcher",
        capability_profile="dangerous",
        model_config=AssistantIronAgentConfig(
            primary_model="openai-codex/gpt-5.4",
            provider_id="",
            provider_base_url="",
            provider_api_key="",
        ),
    )

    assert updated.iron_agent_config.primary_model == "openai-codex/gpt-5.4"
    assert updated.iron_agent_config.auth_mode == "host_iron_agent"
    assert updated.iron_agent_config.provider_id == ""
    assert updated.iron_agent_config.provider_base_url == ""
    assert updated.iron_agent_config.provider_api_key == ""


def test_assistant_service_clears_lingering_openai_codex_api_key(tmp_path) -> None:
    store_path = tmp_path / "assistants.json"
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-5",
            container_id="container-111",
            workspace_root=str(tmp_path / "workspaces" / "assistant-5"),
            workspace_root_id="default",
            workspace_slug="assistant-5",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "assistant-5" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_host="127.0.0.1",
            gateway_port=49161,
            gateway_url="http://127.0.0.1:49161",
            gateway_token="ironagent-local-assistant-5",
            updated_at=datetime.now(UTC),
    )
        )
    service = AssistantService(store_path=store_path, provisioner=provisioner)

    service.upsert_assistant(
        assistant_id="assistant-5",
        name="Demo Codex",
        capability_profile="dangerous",
        model_config=AssistantIronAgentConfig(
            primary_model="openai-codex/gpt-5.4",
            provider_id="",
            provider_base_url="",
            provider_api_key="stale-key",
        ),
    )
    provisioner.calls.clear()

    updated = service.upsert_assistant(
        assistant_id="assistant-5",
        name="Demo Codex",
        capability_profile="dangerous",
        model_config=AssistantIronAgentConfig(
            primary_model="openai-codex/gpt-5.4",
            provider_id="",
            provider_base_url="",
            provider_api_key="",
        ),
    )

    assert updated.iron_agent_config.provider_api_key == ""
    assert updated.iron_agent_config.auth_mode == "host_iron_agent"


def test_assistant_config_infers_provider_api_key_mode_for_legacy_provider_records() -> None:
    config = AssistantIronAgentConfig.model_validate(
        {
            "primary_model": "modelstudio/qwen3-coder-next",
            "provider_id": "modelstudio",
            "provider_base_url": "https://coding.dashscope.aliyuncs.com/v1",
            "provider_api_key": "secret-key",
        }
    )

    assert config.auth_mode == "provider_api_key"


def test_assistant_service_switching_to_host_auth_clears_provider_fields(tmp_path) -> None:
    store_path = tmp_path / "assistants.json"
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-6",
            container_id="container-112",
            workspace_root=str(tmp_path / "workspaces" / "assistant-6"),
            workspace_root_id="default",
            workspace_slug="assistant-6",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "assistant-6" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_host="127.0.0.1",
            gateway_port=49162,
            gateway_url="http://127.0.0.1:49162",
            gateway_token="ironagent-local-assistant-6",
            updated_at=datetime.now(UTC),
    )
        )
    service = AssistantService(store_path=store_path, provisioner=provisioner)

    service.upsert_assistant(
        assistant_id="assistant-6",
        name="Demo Host Auth",
        capability_profile="dangerous",
        model_config=AssistantIronAgentConfig(
            primary_model="modelstudio/qwen3-coder-next",
            provider_id="modelstudio",
            provider_base_url="https://coding.dashscope.aliyuncs.com/v1",
            provider_api_key="secret-key",
        ),
    )

    updated = service.upsert_assistant(
        assistant_id="assistant-6",
        name="Demo Host Auth",
        capability_profile="dangerous",
        model_config=AssistantIronAgentConfig(
            primary_model="openai-codex/gpt-5.4",
            auth_mode="host_iron_agent",
        ),
    )

    assert updated.iron_agent_config.primary_model == "openai-codex/gpt-5.4"
    assert updated.iron_agent_config.auth_mode == "host_iron_agent"
    assert updated.iron_agent_config.provider_id == ""
    assert updated.iron_agent_config.provider_base_url == ""
    assert updated.iron_agent_config.provider_api_key == ""


def test_assistant_service_defaults_workspace_slug_to_assistant_id(tmp_path) -> None:
    store_path = tmp_path / "assistants.json"
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-7",
            container_id="container-113",
            workspace_root=str(tmp_path / "workspaces" / "assistant-7"),
            workspace_root_id="default",
            workspace_slug="assistant-7",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "assistant-7" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_host="127.0.0.1",
            gateway_port=49163,
            gateway_url="http://127.0.0.1:49163",
            gateway_token="ironagent-local-assistant-7",
            updated_at=datetime.now(UTC),
    )
        )
    service = AssistantService(store_path=store_path, provisioner=provisioner)

    updated = service.upsert_assistant(
        assistant_id="assistant-7",
        name="Workspace Defaults",
        capability_profile="onlyread",
        workspace_config=AssistantWorkspaceConfig(
            root_id="default",
            workspace_slug="",
        ),
    )

    assert updated.workspace_config.workspace_slug == "assistant-7"


def test_assistant_service_accepts_custom_workspace_root_and_slug(tmp_path, monkeypatch) -> None:
    store_path = tmp_path / "assistants.json"
    custom_root = tmp_path / "managed-workspaces"
    monkeypatch.setenv(
        "IRON_AGENT_ALLOWED_WORKSPACE_ROOTS_JSON",
        f'[{{"id":"managed","label":"Managed","host_path":"{custom_root}"}}]',
    )
    get_settings.cache_clear()
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-8",
            container_id="container-114",
            workspace_root=str(custom_root / "workspace-a"),
            workspace_root_id="managed",
            workspace_slug="workspace-a",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "assistant-8" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_host="127.0.0.1",
            gateway_port=49164,
            gateway_url="http://127.0.0.1:49164",
            gateway_token="ironagent-local-assistant-8",
            updated_at=datetime.now(UTC),
    )
        )
    service = AssistantService(store_path=store_path, provisioner=provisioner)

    updated = service.upsert_assistant(
        assistant_id="assistant-8",
        name="Workspace Custom",
        capability_profile="dangerous",
        workspace_config=AssistantWorkspaceConfig(
            selection_mode="managed_root",
            root_id="managed",
            workspace_slug="workspace-a",
        ),
    )

    assert updated.workspace_config.root_id == "managed"
    assert updated.workspace_config.workspace_slug == "workspace-a"
    assert provisioner.calls[-1][4] == AssistantWorkspaceConfig(
        selection_mode="managed_root",
        root_id="managed",
        workspace_slug="workspace-a",
    )
    get_settings.cache_clear()


def test_assistant_service_probes_runtime_states(tmp_path) -> None:
    store_path = tmp_path / "assistants.json"
    store_path.write_text(
        json.dumps(
            {
                "assistants": [
                    {
                        "assistant_id": "assistant-probe",
                        "name": "Assistant Probe",
                        "capability_profile": "dangerous",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-probe",
            container_id="container-probe",
            workspace_root=str(tmp_path / "workspaces" / "assistant-probe"),
            workspace_root_id="default",
            workspace_slug="assistant-probe",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "assistant-probe" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_host="127.0.0.1",
            gateway_port=49170,
            gateway_url="http://127.0.0.1:49170",
            gateway_token="ironagent-local-assistant-probe",
            updated_at=datetime.now(UTC),
    )
        )
    provisioner.probe_overrides["assistant-probe"] = RuntimeProbeResult(
        container_name="ironagent-assistant-assistant-probe",
        container_exists=True,
        container_id="container-probe",
        container_state="running",
        health_state="healthy",
        status="running",
        gateway_host="127.0.0.1",
        gateway_port=49170,
        gateway_url="http://127.0.0.1:49170",
    )
    service = AssistantService(store_path=store_path, provisioner=provisioner)

    probes = service.probe_runtime_states()

    assert "assistant-probe" in probes
    assert probes["assistant-probe"].status == "running"
    assert probes["assistant-probe"].gateway_url == "http://127.0.0.1:49170"
    assert provisioner.probe_calls == ["assistant-probe"]


def test_sync_runtime_views_fills_k8s_workspace_metadata(tmp_path, monkeypatch) -> None:
    workspace_root = tmp_path / "workspaces"
    store_path = tmp_path / "assistants.json"
    store_path.write_text(
        json.dumps(
            {
                "assistants": [
                    {
                        "assistant_id": "assistant-k8s",
                        "name": "Assistant K8s",
                        "capability_profile": "chat",
                        "runtime": {
                            "container_name": "ironagent-assistant-assistant-k8s-pod",
                            "status": "starting",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("IRON_AGENT_RUNTIME_BACKEND", "kubernetes")
    monkeypatch.setenv("IRON_AGENT_ASSISTANTS_WORKSPACE_ROOT", str(workspace_root))
    get_settings.cache_clear()
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-k8s-pod",
            container_id="pod-uid",
            workspace_root=str(workspace_root / "assistant-k8s"),
            workspace_root_id="default",
            workspace_slug="assistant-k8s",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(workspace_root / "assistant-k8s" / ".ironagent"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_host="10.244.144.10",
            gateway_port=18789,
            gateway_url="http://10.244.144.10:18789",
            gateway_token="ironagent-local-assistant-k8s",
            updated_at=datetime.now(UTC),
    )
        )
    provisioner.probe_overrides["assistant-k8s"] = RuntimeProbeResult(
        container_name="ironagent-assistant-assistant-k8s-pod",
        container_exists=True,
        container_id="pod-uid",
        container_state="running",
        status="running",
        gateway_host="10.244.144.10",
        gateway_port=18789,
        gateway_url="http://10.244.144.10:18789",
    )
    service = AssistantService(store_path=store_path, provisioner=provisioner)

    records, _, updated_count = service.sync_runtime_views()
    updated = records[0]

    assert updated_count == 1
    assert updated.runtime.status == "running"
    assert updated.runtime.workspace_root == str((workspace_root / "assistant-k8s").resolve())
    assert updated.runtime.workspace_root_id == "default"
    assert updated.runtime.workspace_slug == "assistant-k8s"
    assert updated.runtime.state_root == str(
        (workspace_root / "assistant-k8s" / ".ironagent").resolve()
    )
    assert updated.runtime.gateway_token == "ironagent-local-assistant-k8s"
    get_settings.cache_clear()


def test_sync_runtime_views_marks_failed_probe_as_degraded_observed_state(tmp_path) -> None:
    store_path = tmp_path / "assistants.json"
    store_path.write_text(
        json.dumps(
            {
                "assistants": [
                    {
                        "assistant_id": "assistant-degraded",
                        "name": "Assistant Degraded",
                        "capability_profile": "onlyread",
                        "runtime": {
                            "container_name": "ironagent-assistant-assistant-degraded",
                            "container_id": "container-degraded",
                            "status": "running",
                            "desired_state": "running",
                            "observed_state": "running",
                            "sync_state": "current",
                            "gateway_url": "http://127.0.0.1:49180",
                            "gateway_token": "ironagent-local-assistant-degraded",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-degraded",
            container_id="container-degraded",
            workspace_root=str(tmp_path / "workspaces" / "assistant-degraded"),
            workspace_root_id="default",
            workspace_slug="assistant-degraded",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "assistant-degraded" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_url="http://127.0.0.1:49180",
            gateway_token="ironagent-local-assistant-degraded",
            updated_at=datetime.now(UTC),
    )
        )
    provisioner.probe_overrides["assistant-degraded"] = RuntimeProbeResult(
        container_name="ironagent-assistant-assistant-degraded",
        container_exists=False,
        container_state="missing",
        status="failed",
        last_error="container not found",
    )
    service = AssistantService(store_path=store_path, provisioner=provisioner)

    records, _, updated_count = service.sync_runtime_views()
    updated = records[0]

    assert updated_count == 1
    assert updated.runtime.status == "failed"
    assert updated.runtime.desired_state == "running"
    assert updated.runtime.observed_state == "failed"
    assert updated.runtime.sync_state == "degraded"
    assert updated.runtime.last_runtime_error == "container not found"
    assert updated.runtime.last_observed_at is not None
    assert updated.runtime.last_reconciled_at is not None


def test_sync_runtime_views_does_not_overwrite_pending_with_equivalent_stale_probe(
    tmp_path,
) -> None:
    observed_at = "2026-01-01T00:00:00+00:00"
    store_path = tmp_path / "assistants.json"
    store_path.write_text(
        json.dumps(
            {
                "assistants": [
                    {
                        "assistant_id": "assistant-pending",
                        "name": "Assistant Pending",
                        "capability_profile": "onlyread",
                        "runtime": {
                            "container_name": "ironagent-assistant-assistant-pending",
                            "container_id": "container-pending",
                            "status": "starting",
                            "desired_state": "running",
                            "observed_state": "running",
                            "sync_state": "pending",
                            "gateway_host": "127.0.0.1",
                            "gateway_port": 49181,
                            "gateway_url": "http://127.0.0.1:49181",
                            "gateway_token": "ironagent-local-assistant-pending",
                            "last_observed_at": observed_at,
                            "last_reconciled_at": observed_at,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-pending",
            container_id="container-pending",
            workspace_root=str(tmp_path / "workspaces" / "assistant-pending"),
            workspace_root_id="default",
            workspace_slug="assistant-pending",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "assistant-pending" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_host="127.0.0.1",
            gateway_port=49181,
            gateway_url="http://127.0.0.1:49181",
            gateway_token="ironagent-local-assistant-pending",
            updated_at=datetime.now(UTC),
    )
        )
    provisioner.probe_overrides["assistant-pending"] = RuntimeProbeResult(
        container_name="ironagent-assistant-assistant-pending",
        container_exists=True,
        container_id="container-pending",
        container_state="running",
        status="running",
        gateway_host="127.0.0.1",
        gateway_port=49181,
        gateway_url="http://127.0.0.1:49181",
    )
    service = AssistantService(store_path=store_path, provisioner=provisioner)

    records, _, updated_count = service.sync_runtime_views()
    updated = records[0]

    assert updated_count == 0
    assert updated.runtime.status == "starting"
    assert updated.runtime.desired_state == "running"
    assert updated.runtime.observed_state == "running"
    assert updated.runtime.sync_state == "pending"
    assert updated.runtime.last_observed_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert updated.runtime.last_reconciled_at == datetime(2026, 1, 1, tzinfo=UTC)


def test_sync_runtime_views_preserves_deleted_assistant_when_probe_finds_container(
    tmp_path,
) -> None:
    deleted_at = "2026-01-01T00:00:00+00:00"
    store_path = tmp_path / "assistants.json"
    store_path.write_text(
        json.dumps(
            {
                "assistants": [
                    {
                        "assistant_id": "assistant-deleted",
                        "name": "Assistant Deleted",
                        "capability_profile": "onlyread",
                        "deleted_at": deleted_at,
                        "runtime": {
                            "container_name": "ironagent-assistant-assistant-deleted",
                            "container_id": None,
                            "status": "stopped",
                            "desired_state": "stopped",
                            "observed_state": "stopped",
                            "sync_state": "current",
                            "gateway_token": None,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-deleted",
            container_id="container-deleted",
            workspace_root=str(tmp_path / "workspaces" / "assistant-deleted"),
            workspace_root_id="default",
            workspace_slug="assistant-deleted",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "assistant-deleted" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_url="http://127.0.0.1:49182",
            gateway_token="ironagent-local-assistant-deleted",
            updated_at=datetime.now(UTC),
    )
        )
    provisioner.probe_overrides["assistant-deleted"] = RuntimeProbeResult(
        container_name="ironagent-assistant-assistant-deleted",
        container_exists=True,
        container_id="container-deleted",
        container_state="running",
        status="running",
        gateway_url="http://127.0.0.1:49182",
    )
    service = AssistantService(store_path=store_path, provisioner=provisioner)

    records, _, updated_count = service.sync_runtime_views()
    updated = records[0]

    assert updated_count == 0
    assert updated.deleted_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert updated.runtime.status == "stopped"
    assert updated.runtime.desired_state == "stopped"
    assert updated.runtime.observed_state == "stopped"
    assert updated.runtime.sync_state == "current"
    assert provisioner.probe_calls == []


def test_sync_runtime_views_empty_target_list_does_not_probe_all(tmp_path) -> None:
    store_path = tmp_path / "assistants.json"
    store_path.write_text(
        json.dumps(
            {
                "assistants": [
                    {
                        "assistant_id": "assistant-active",
                        "name": "Assistant Active",
                        "capability_profile": "chat",
                        "runtime": {
                            "container_name": "ironagent-assistant-assistant-active",
                            "status": "starting",
                            "desired_state": "running",
                            "observed_state": "starting",
                            "sync_state": "pending",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-active",
            container_id="container-active",
            workspace_root=str(tmp_path / "workspaces" / "assistant-active"),
            workspace_root_id="default",
            workspace_slug="assistant-active",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "assistant-active" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_token="ironagent-local-assistant-active",
            updated_at=datetime.now(UTC),
    )
        )
    service = AssistantService(store_path=store_path, provisioner=provisioner)

    records, probes, updated_count = service.sync_runtime_views([])

    assert records == []
    assert probes == {}
    assert updated_count == 0
    assert provisioner.probe_calls == []


def test_reconcile_runtime_states_skips_deleted_assistants_before_probe(
    tmp_path,
) -> None:
    deleted_at = "2026-01-01T00:00:00+00:00"
    store_path = tmp_path / "assistants.json"
    store_path.write_text(
        json.dumps(
            {
                "assistants": [
                    {
                        "assistant_id": "assistant-active",
                        "name": "Assistant Active",
                        "capability_profile": "chat",
                        "runtime": {
                            "container_name": "ironagent-assistant-assistant-active",
                            "container_id": "container-active",
                            "status": "running",
                            "image": "ironagent-runtime:dev",
                            "gateway_url": "http://127.0.0.1:49183",
                    },
                        },
                    {
                        "assistant_id": "assistant-deleted",
                        "name": "Assistant Deleted",
                        "capability_profile": "chat",
                        "deleted_at": deleted_at,
                        "runtime": {
                            "container_name": "ironagent-assistant-assistant-deleted",
                            "container_id": None,
                            "status": "stopped",
                            "desired_state": "stopped",
                            "observed_state": "stopped",
                            "sync_state": "current",
                            "last_error": "assistant soft deleted",
                    },
                        },
                ]
            }
        ),
        encoding="utf-8",
    )
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-active",
            container_id="container-active",
            workspace_root=str(tmp_path / "workspaces" / "assistant-active"),
            workspace_root_id="default",
            workspace_slug="assistant-active",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "assistant-active" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_url="http://127.0.0.1:49183",
            gateway_token="ironagent-local-assistant-active",
            updated_at=datetime.now(UTC),
    )
        )
    provisioner.probe_overrides["assistant-active"] = RuntimeProbeResult(
        container_name="ironagent-assistant-assistant-active",
        container_exists=True,
        container_id="container-active",
        container_state="running",
        status="running",
        gateway_url="http://127.0.0.1:49183",
    )
    provisioner.probe_overrides["assistant-deleted"] = RuntimeProbeResult(
        container_name="ironagent-assistant-assistant-deleted",
        container_exists=False,
        status="failed",
        last_error="pod not found",
    )
    service = AssistantService(store_path=store_path, provisioner=provisioner)

    repaired, skipped = service.repair_out_of_sync_runtimes()

    assert (repaired, skipped) == (0, 2)
    assert provisioner.probe_calls == ["assistant-active"]


def test_assistant_service_repairs_out_of_sync_runtimes(tmp_path) -> None:
    store_path = tmp_path / "assistants.json"
    store_path.write_text(
        json.dumps(
            {
                "assistants": [
                    {
                        "assistant_id": "assistant-repair",
                        "name": "Assistant Repair",
                        "capability_profile": "dangerous",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-repair",
            container_id="container-repair",
            workspace_root=str(tmp_path / "workspaces" / "assistant-repair"),
            workspace_root_id="default",
            workspace_slug="assistant-repair",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "assistant-repair" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_host="127.0.0.1",
            gateway_port=49171,
            gateway_url="http://127.0.0.1:49171",
            gateway_token="ironagent-local-assistant-repair",
            updated_at=datetime.now(UTC),
    )
        )
    provisioner.probe_overrides["assistant-repair"] = RuntimeProbeResult(
        container_name="ironagent-assistant-assistant-repair",
        container_exists=False,
        status="failed",
        last_error="container not found",
    )
    service = AssistantService(store_path=store_path, provisioner=provisioner)

    repaired, skipped = service.repair_out_of_sync_runtimes()
    updated = service.get_assistant("assistant-repair")

    assert repaired == 1
    assert skipped == 0
    assert len(provisioner.calls) == 1
    assert updated is not None
    assert updated.runtime.status == "running"
    assert updated.runtime.gateway_url == "http://127.0.0.1:49171"


def test_assistant_service_repairs_runtime_image_drift(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IRON_AGENT_RUNTIME_CONTAINER_IMAGE", "ironagent-runtime:new")
    get_settings.cache_clear()
    store_path = tmp_path / "assistants.json"
    store_path.write_text(
        json.dumps(
            {
                "assistants": [
                    {
                        "assistant_id": "assistant-image-drift",
                        "name": "Assistant Image Drift",
                        "capability_profile": "chat",
                        "runtime": {
                            "container_name": "ironagent-assistant-assistant-image-drift",
                            "container_id": "old-container",
                            "status": "running",
                            "image": "ironagent-runtime:old",
                            "gateway_url": "http://127.0.0.1:49172",
                            "gateway_token": "ironagent-local-assistant-image-drift",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-image-drift",
            container_id="new-container",
            workspace_root=str(tmp_path / "workspaces" / "assistant-image-drift"),
            workspace_root_id="default",
            workspace_slug="assistant-image-drift",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "assistant-image-drift" / "state"),
            status="running",
            image="ironagent-runtime:new",
            gateway_host="127.0.0.1",
            gateway_port=49172,
            gateway_url="http://127.0.0.1:49172",
            gateway_token="ironagent-local-assistant-image-drift",
            updated_at=datetime.now(UTC),
    )
        )
    provisioner.probe_overrides["assistant-image-drift"] = RuntimeProbeResult(
        container_name="ironagent-assistant-assistant-image-drift",
        container_exists=True,
        container_id="old-container",
        container_state="running",
        status="running",
        gateway_host="127.0.0.1",
        gateway_port=49172,
        gateway_url="http://127.0.0.1:49172",
    )
    service = AssistantService(store_path=store_path, provisioner=provisioner)

    repaired, skipped = service.repair_out_of_sync_runtimes()
    updated = service.get_assistant("assistant-image-drift")

    assert (repaired, skipped) == (1, 0)
    assert len(provisioner.calls) == 1
    assert updated is not None
    assert updated.runtime.container_id == "new-container"
    assert updated.runtime.image == "ironagent-runtime:new"


def test_assistant_service_reconcile_runtime_states_aliases_repair(tmp_path) -> None:
    store_path = tmp_path / "assistants.json"
    store_path.write_text(
        json.dumps(
            {
                "assistants": [
                    {
                        "assistant_id": "assistant-reconcile",
                        "name": "Assistant Reconcile",
                        "capability_profile": "chat",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-reconcile",
            container_id="container-reconcile",
            workspace_root=str(tmp_path / "workspaces" / "assistant-reconcile"),
            workspace_root_id="default",
            workspace_slug="assistant-reconcile",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(tmp_path / "runtimes" / "assistant-reconcile" / "state"),
            status="running",
            image="ironagent-runtime:dev",
            gateway_host="127.0.0.1",
            gateway_port=49172,
            gateway_url="http://127.0.0.1:49172",
            gateway_token="ironagent-local-assistant-reconcile",
            updated_at=datetime.now(UTC),
    )
        )
    provisioner.probe_overrides["assistant-reconcile"] = RuntimeProbeResult(
        container_name="ironagent-assistant-assistant-reconcile",
        container_exists=False,
        status="failed",
        last_error="container not found",
    )
    service = AssistantService(store_path=store_path, provisioner=provisioner)

    repaired, skipped = service.reconcile_runtime_states()
    updated = service.get_assistant("assistant-reconcile")

    assert (repaired, skipped) == (1, 0)
    assert updated is not None
    assert updated.runtime.status == "running"


def test_assistant_service_soft_deletes_and_preserves_workspace_state(tmp_path) -> None:
    store_path = tmp_path / "assistants.json"
    workspace_root = tmp_path / "workspaces" / "assistant-delete"
    state_root = tmp_path / "runtimes" / "assistant-delete" / "state"
    (workspace_root / "temp").mkdir(parents=True, exist_ok=True)
    (workspace_root / "temp" / "input.txt").write_text("keep me", encoding="utf-8")
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "ironagent.json").write_text("{}", encoding="utf-8")
    store_path.write_text(
        json.dumps(
            {
                "assistants": [
                    {
                        "assistant_id": "assistant-delete",
                        "name": "Assistant Delete",
                        "capability_profile": "dangerous",
                        "runtime": {
                            "container_name": "ironagent-assistant-assistant-delete",
                            "workspace_root": str(workspace_root),
                            "workspace_root_id": "default",
                            "workspace_slug": "assistant-delete",
                            "state_root": str(state_root),
                            "status": "running",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    provisioner = FakeProvisioner(
        ProvisionResult(
            container_name="ironagent-assistant-assistant-delete",
            container_id="container-delete",
            workspace_root=str(workspace_root),
            workspace_root_id="default",
            workspace_slug="assistant-delete",
            temp_dir="temp",
            persistent_dir="persistent",
            state_root=str(state_root),
            status="running",
            image="ironagent-runtime:dev",
            gateway_host="127.0.0.1",
            gateway_port=49172,
            gateway_url="http://127.0.0.1:49172",
            gateway_token="ironagent-local-assistant-delete",
            updated_at=datetime.now(UTC),
    )
        )
    provisioner.destroy_result = DestroyRuntimeResult(
        container_name="ironagent-assistant-assistant-delete",
        container_removed=True,
        workspace_removed=False,
        state_removed=False,
    )
    service = AssistantService(store_path=store_path, provisioner=provisioner)

    deleted = service.delete_assistant("assistant-delete")
    assistants = service.list_assistants()

    assert deleted is not None
    assert deleted.assistant_id == "assistant-delete"
    assert deleted.container_removed is True
    assert deleted.workspace_removed is False
    assert deleted.state_removed is False
    assert deleted.soft_deleted is True
    assert deleted.runtime_disabled is True
    assert provisioner.destroy_calls == [("assistant-delete", None, None)]
    assert len(assistants) == 1
    assert assistants[0].assistant_id == "assistant-delete"
    assert assistants[0].deleted_at is not None
    assert assistants[0].runtime.status == "stopped"
    assert assistants[0].runtime.gateway_url is None
    assert (workspace_root / "temp" / "input.txt").read_text(encoding="utf-8") == "keep me"
    assert (state_root / "ironagent.json").exists()


def test_assistant_service_lists_workspace_items_with_disabled_entries(tmp_path) -> None:
    store_path = tmp_path / "assistants.json"
    workspace_root = tmp_path / "workspaces" / "assistant-items"
    state_root = tmp_path / "runtimes" / "assistant-items" / "state"
    (workspace_root / "skills" / "office-docs").mkdir(parents=True, exist_ok=True)
    (workspace_root / "skills" / "office-docs" / "SKILL.md").write_text(
        "# Office Docs\n",
        encoding="utf-8",
    )
    (workspace_root / "skills" / "empty-install").mkdir(parents=True, exist_ok=True)
    (workspace_root / ".ironagent-disabled" / "plugins" / "office-plugin").mkdir(
        parents=True,
        exist_ok=True,
    )
    disabled_plugin_manifest = (
        workspace_root
        / ".ironagent-disabled"
        / "plugins"
        / "office-plugin"
        / "manifest.json"
    )
    disabled_plugin_manifest.write_text(
        json.dumps({"name": "Office Plugin", "version": "1.2.3"}),
        encoding="utf-8",
    )
    (workspace_root / "plugins" / "workspace-plugin").mkdir(parents=True, exist_ok=True)
    (workspace_root / "plugins" / "workspace-plugin" / "ironagent.plugin.json").write_text(
        json.dumps({"name": "Workspace Plugin", "version": "2.0.0"}),
        encoding="utf-8",
    )
    store_path.write_text(
        json.dumps(
            {
                "assistants": [
                    {
                        "assistant_id": "assistant-items",
                        "name": "Assistant Items",
                        "capability_profile": "dangerous",
                        "runtime": {
                            "container_name": "ironagent-assistant-assistant-items",
                            "workspace_root": str(workspace_root),
                            "workspace_root_id": "default",
                            "workspace_slug": "assistant-items",
                            "state_root": str(state_root),
                            "status": "running",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    service = AssistantService(store_path=store_path)

    items = service.list_workspace_items("assistant-items")

    assert [(item.kind, item.slug, item.enabled) for item in items] == [
        ("plugins", "office-plugin", False),
        ("plugins", "workspace-plugin", True),
        ("skills", "office-docs", True),
    ]
    assert "empty-install" not in {item.slug for item in items}
    disabled_plugin = next(item for item in items if item.slug == "office-plugin")
    assert disabled_plugin.name == "Office Plugin"
    assert disabled_plugin.version == "1.2.3"
    workspace_plugin = next(item for item in items if item.slug == "workspace-plugin")
    assert workspace_plugin.name == "Workspace Plugin"
    assert workspace_plugin.version == "2.0.0"


def test_assistant_service_can_disable_and_enable_workspace_item(tmp_path) -> None:
    store_path = tmp_path / "assistants.json"
    workspace_root = tmp_path / "workspaces" / "assistant-toggle"
    state_root = tmp_path / "runtimes" / "assistant-toggle" / "state"
    skill_root = workspace_root / "skills" / "office-docs"
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "SKILL.md").write_text("# Office Docs\n", encoding="utf-8")
    store_path.write_text(
        json.dumps(
            {
                "assistants": [
                    {
                        "assistant_id": "assistant-toggle",
                        "name": "Assistant Toggle",
                        "capability_profile": "dangerous",
                        "runtime": {
                            "container_name": "ironagent-assistant-assistant-toggle",
                            "workspace_root": str(workspace_root),
                            "workspace_root_id": "default",
                            "workspace_slug": "assistant-toggle",
                            "state_root": str(state_root),
                            "status": "running",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    service = AssistantService(store_path=store_path)

    disabled = service.set_workspace_item_enabled(
        assistant_id="assistant-toggle",
        kind="skills",
        slug="office-docs",
        enabled=False,
    )
    assert disabled.enabled is False
    assert Path(disabled.install_path).exists()
    assert not (workspace_root / "skills" / "office-docs").exists()
    assert (workspace_root / ".ironagent-disabled" / "skills" / "office-docs").exists()

    enabled = service.set_workspace_item_enabled(
        assistant_id="assistant-toggle",
        kind="skills",
        slug="office-docs",
        enabled=True,
    )
    assert enabled.enabled is True
    assert (workspace_root / "skills" / "office-docs").exists()
    assert not (workspace_root / ".ironagent-disabled" / "skills" / "office-docs").exists()


def test_assistant_service_can_reuse_workspace_item_to_another_assistant(tmp_path) -> None:
    store_path = tmp_path / "assistants.json"
    source_workspace = tmp_path / "workspaces" / "assistant-source"
    target_workspace = tmp_path / "workspaces" / "assistant-target"
    source_skill_root = source_workspace / "skills" / "office-docs"
    source_skill_root.mkdir(parents=True, exist_ok=True)
    (source_skill_root / "SKILL.md").write_text("# Office Docs\n", encoding="utf-8")
    (source_skill_root / "manifest.json").write_text(
        json.dumps({"name": "Office Docs", "version": "9.9.9"}),
        encoding="utf-8",
    )
    store_path.write_text(
        json.dumps(
            {
                "assistants": [
                    {
                        "assistant_id": "assistant-source",
                        "name": "Assistant Source",
                        "capability_profile": "dangerous",
                        "runtime": {
                            "container_name": "ironagent-assistant-assistant-source",
                            "workspace_root": str(source_workspace),
                            "workspace_root_id": "default",
                            "workspace_slug": "assistant-source",
                            "state_root": str(tmp_path / "runtimes" / "assistant-source" / "state"),
                            "status": "running",
                    },
                        },
                    {
                        "assistant_id": "assistant-target",
                        "name": "Assistant Target",
                        "capability_profile": "dangerous",
                        "runtime": {
                            "container_name": "ironagent-assistant-assistant-target",
                            "workspace_root": str(target_workspace),
                            "workspace_root_id": "default",
                            "workspace_slug": "assistant-target",
                            "state_root": str(tmp_path / "runtimes" / "assistant-target" / "state"),
                            "status": "running",
                    },
                        },
                ]
            }
        ),
        encoding="utf-8",
    )
    service = AssistantService(store_path=store_path)

    reused = service.reuse_workspace_item(
        source_assistant_id="assistant-source",
        target_assistant_id="assistant-target",
        kind="skills",
        slug="office-docs",
    )

    assert reused.source_assistant_id == "assistant-source"
    assert reused.target_assistant_id == "assistant-target"
    assert reused.kind == "skills"
    assert reused.slug == "office-docs"
    copied_manifest = target_workspace / "skills" / "office-docs" / "manifest.json"
    assert copied_manifest.exists()
    assert json.loads(copied_manifest.read_text(encoding="utf-8"))["version"] == "9.9.9"


def test_assistant_service_reuse_replaces_disabled_copy_in_target(tmp_path) -> None:
    store_path = tmp_path / "assistants.json"
    source_workspace = tmp_path / "workspaces" / "assistant-source"
    target_workspace = tmp_path / "workspaces" / "assistant-target"
    source_skill_root = source_workspace / "skills" / "office-docs"
    source_skill_root.mkdir(parents=True, exist_ok=True)
    (source_skill_root / "manifest.json").write_text(
        json.dumps({"name": "Office Docs", "version": "9.9.9"}),
        encoding="utf-8",
    )
    disabled_target_root = (
        target_workspace / ".ironagent-disabled" / "skills" / "office-docs"
    )
    disabled_target_root.mkdir(parents=True, exist_ok=True)
    (disabled_target_root / "manifest.json").write_text(
        json.dumps({"name": "Old Office Docs", "version": "1.0.0"}),
        encoding="utf-8",
    )
    store_path.write_text(
        json.dumps(
            {
                "assistants": [
                    {
                        "assistant_id": "assistant-source",
                        "name": "Assistant Source",
                        "capability_profile": "dangerous",
                        "runtime": {
                            "container_name": "ironagent-assistant-assistant-source",
                            "workspace_root": str(source_workspace),
                            "workspace_root_id": "default",
                            "workspace_slug": "assistant-source",
                            "state_root": str(tmp_path / "runtimes" / "assistant-source" / "state"),
                            "status": "running",
                    },
                        },
                    {
                        "assistant_id": "assistant-target",
                        "name": "Assistant Target",
                        "capability_profile": "dangerous",
                        "runtime": {
                            "container_name": "ironagent-assistant-assistant-target",
                            "workspace_root": str(target_workspace),
                            "workspace_root_id": "default",
                            "workspace_slug": "assistant-target",
                            "state_root": str(tmp_path / "runtimes" / "assistant-target" / "state"),
                            "status": "running",
                    },
                        },
                ]
            }
        ),
        encoding="utf-8",
    )
    service = AssistantService(store_path=store_path)

    service.reuse_workspace_item(
        source_assistant_id="assistant-source",
        target_assistant_id="assistant-target",
        kind="skills",
        slug="office-docs",
    )

    enabled_manifest = target_workspace / "skills" / "office-docs" / "manifest.json"
    assert enabled_manifest.exists()
    assert json.loads(enabled_manifest.read_text(encoding="utf-8"))["version"] == "9.9.9"
    assert not disabled_target_root.exists()
