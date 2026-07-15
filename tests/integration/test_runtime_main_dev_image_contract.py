import json
from pathlib import Path

from src.runtime.kubernetes import K8sRuntimeProvisioner
from src.runtime.models import AssistantIronAgentConfig
from src.runtime.provisioner import AssistantRuntimeProvisioner, CommandResult
from src.shared.config import Settings


class SequenceRunner:
    def __init__(self, results: list[CommandResult]) -> None:
        self.results = results
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> CommandResult:
        self.calls.append(args)
        if not self.results:
            raise AssertionError("unexpected command call")
        return self.results.pop(0)


def _main_dev_image() -> str:
    return "ironagent-main-dev:mock"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        assistants_workspace_root=tmp_path / "workspaces",
        default_workspace_root_id="default",
        assistants_runtime_root=tmp_path / "runtimes",
        host_iron_agent_root=tmp_path / "host-ironagent",
        iron_agent_source_root=tmp_path / "ironagent-main",
        host_iron_agent_agent_id="main",
        iron_agent_container_image=_main_dev_image(),
        iron_agent_state_mount_path="/home/node/.ironagent",
        iron_agent_workspace_mount_path="/workspace",
        assistant_container_name_prefix="ironagent-assistant",
        iron_agent_gateway_host="127.0.0.1",
        iron_agent_gateway_port=18789,
        iron_agent_gateway_bind="lan",
        iron_agent_hooks_enabled=True,
        iron_agent_hook_service_url="http://host.docker.internal:8000",
        iron_agent_hook_fail_closed=True,
        iron_agent_hook_timeout_ms=5000,
        iron_agent_approval_poll_ms=1000,
        iron_agent_approval_max_wait_ms=300000,
        iron_agent_http_proxy="",
        iron_agent_https_proxy="",
        iron_agent_no_proxy="",
        docker_binary="docker",
    )


def _env_from_docker_run(args: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for index, item in enumerate(args[:-1]):
        if item != "-e":
            continue
        key, _, value = args[index + 1].partition("=")
        env[key] = value
    return env


def test_docker_runtime_uses_iron_agent_contract_for_main_dev_image_mock(tmp_path: Path) -> None:
    runner = SequenceRunner(
        [
            CommandResult(returncode=1, stderr="No such container"),
            CommandResult(returncode=0, stdout="container-main-dev\n"),
            CommandResult(returncode=0, stdout="127.0.0.1:49190\n"),
        ]
    )
    settings = _settings(tmp_path)
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)
    provisioner._gateway_healthz_reachable = lambda _url: True

    result = provisioner.ensure_runtime(
        assistant_id="demo-main",
        assistant_name="Demo Main",
        model_config=AssistantIronAgentConfig(
            primary_model="modelstudio/qwen3-coder-next",
            provider_id="modelstudio",
            provider_base_url="https://coding.dashscope.aliyuncs.com/v1",
            provider_api_key="secret-key",
        ),
    )

    assert result.status == "running"
    assert result.image == settings.iron_agent_container_image
    assert result.gateway_url == "http://127.0.0.1:49190"

    run_args = runner.calls[1]
    image_index = run_args.index(settings.iron_agent_container_image)
    assert run_args[image_index + 1 :] == [
        "node",
        "/app/ironagent.mjs",
        "gateway",
        "--bind",
        "lan",
        "--port",
        "18789",
        "--allow-unconfigured",
    ]
    assert f"{tmp_path / 'runtimes' / 'demo-main' / 'state'}:/home/node/.ironagent" in run_args
    assert f"{tmp_path / 'workspaces' / 'demo-main'}:/workspace" in run_args

    env = _env_from_docker_run(run_args)
    assert env["IRON_AGENT_RUNTIME_CONTAINER_IMAGE"] == settings.iron_agent_container_image
    assert env["IRON_AGENT_CONFIG_PATH"] == "/home/node/.ironagent/ironagent.json"
    assert env["IRON_AGENT_WORKSPACE_DIR"] == "/workspace"
    assert env["IRON_AGENT_BUNDLED_SKILLS_DIR"] == "/workspace/skills"
    assert env["IRON_AGENT_HOOKS_ENABLED"] == "true"
    assert env["IRON_AGENT_SERVICE_URL"] == "http://host.docker.internal:8000"

    state_config = tmp_path / "runtimes" / "demo-main" / "state" / "ironagent.json"
    payload = json.loads(state_config.read_text(encoding="utf-8"))
    assert payload["gateway"]["auth"] == {
        "mode": "token",
        "token": "ironagent-local-demo-main",
    }
    assert payload["agents"]["defaults"]["workspace"] == "/workspace"
    assert payload["agents"]["defaults"]["model"]["primary"] == "modelstudio/qwen3-coder-next"
    assert payload["agents"]["list"][0] == {
        "id": "demo-main",
        "name": "demo-main",
        "default": True,
        "workspace": "/workspace",
    }
    assert "read" in payload["tools"]["allow"]
    assert "process" not in payload["tools"]["allow"]
    assert "code_execution" not in payload["tools"]["allow"]
    assert "exec" not in payload["tools"]
    assert payload["models"]["providers"]["modelstudio"]["api"] == "openai-completions"
    assert payload["models"]["providers"]["modelstudio"]["apiKey"] == "***"
    assert payload["models"]["providers"]["modelstudio"]["authHeader"] is False
    assert payload["models"]["providers"]["modelstudio"]["headers"] == {
        "Authorization": "secret-key",
    }
    assert payload["models"]["providers"]["modelstudio"]["models"] == [
        {"id": "qwen3-coder-next", "name": "qwen3-coder-next"}
    ]


def test_k8s_runtime_manifest_uses_iron_agent_contract_for_main_dev_image_mock(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path).model_copy(
        update={
            "runtime_backend": "kubernetes",
            "k8s_runtime_init_image": "",
            "k8s_runtime_namespace": "ironagent",
            "k8s_runtime_workspace_pvc": "assistant-workspaces",
            "k8s_runtime_workspace_subpath_prefix": "assistants",
            "k8s_runtime_state_dir": ".ironagent",
        }
    )
    provisioner = K8sRuntimeProvisioner(
        settings=settings,
        command_runner=SequenceRunner([]),
    )

    runtime_config = json.dumps(
        {
            "gateway": {"auth": {"mode": "token", "token": "runtime-token"}},
            "agents": {
                "defaults": {"workspace": "/workspace"},
                "list": [
                    {
                        "id": "demo-main",
                        "name": "Demo Main",
                        "default": True,
                        "workspace": "/workspace",
                    }
                ],
            },
        }
    )
    secret, service, pod = provisioner.build_runtime_manifest(
        assistant_id="demo-main",
        workspace_slug="demo-main",
        iron_agent_config=runtime_config,
    )

    assert secret["stringData"]["ironagent.json"] == runtime_config
    assert service["kind"] == "Service"
    assert service["metadata"]["name"] == "ironagent-assistant-demo-main-svc"
    assert service["spec"]["ports"][0]["targetPort"] == "http"
    assert pod["metadata"]["name"] == "ironagent-assistant-demo-main-pod"

    init_container = pod["spec"]["initContainers"][0]
    runtime_container = pod["spec"]["containers"][0]
    assert init_container["image"] == settings.iron_agent_container_image
    assert runtime_container["image"] == settings.iron_agent_container_image
    assert runtime_container["command"] == [
        "node",
        "/app/ironagent.mjs",
        "gateway",
        "--bind",
        "lan",
        "--port",
        "18789",
        "--allow-unconfigured",
    ]
    assert runtime_container["workingDir"] == "/workspace"

    env = {
        item["name"]: item["value"]
        for item in runtime_container["env"]
        if "value" in item
    }
    assert env["IRON_AGENT_RUNTIME_CONTAINER_IMAGE"] == settings.iron_agent_container_image
    assert env["IRON_AGENT_CONFIG_PATH"] == "/home/node/.ironagent/ironagent.json"
    assert env["IRON_AGENT_WORKSPACE_DIR"] == "/workspace"
    assert env["IRON_AGENT_BUNDLED_SKILLS_DIR"] == "/workspace/skills"

    mounts = {
        item["mountPath"]: item["subPath"]
        for item in runtime_container["volumeMounts"]
        if "subPath" in item
    }
    assert mounts["/workspace"] == "assistants/demo-main"
    assert mounts["/home/node/.ironagent"] == "assistants/demo-main/.ironagent"
