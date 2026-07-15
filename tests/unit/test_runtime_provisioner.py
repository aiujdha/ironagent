import json
import os
import subprocess
from pathlib import Path

import yaml

from src.assistants.service import AssistantService
from src.runtime.factory import build_runtime_provisioner
from src.runtime.kubernetes import K8sRuntimeProvisioner
from src.runtime.models import AssistantIronAgentConfig, AssistantWorkspaceConfig
from src.runtime.provisioner import (
    AssistantRuntimeProvisioner,
    CommandResult,
    RuntimeProbeResult,
    default_command_runner,
    is_placeholder_api_key,
)
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


class ProbeSequenceProvisioner(AssistantRuntimeProvisioner):
    def __init__(
        self,
        *,
        settings: Settings,
        inspect_result: tuple[str, str],
        probe_results: list[RuntimeProbeResult],
    ) -> None:
        super().__init__(
            settings=settings, command_runner=lambda _args: CommandResult(returncode=0)
        )
        self.inspect_result = inspect_result
        self.probe_results = probe_results
        self.unpause_calls: list[str] = []
        self.start_calls: list[str] = []
        self.restart_calls: list[str] = []

    def _inspect_container(self, container_name: str) -> tuple[str, str] | None:
        return self.inspect_result

    def _unpause_container(self, container_name: str) -> CommandResult:
        self.unpause_calls.append(container_name)
        return CommandResult(returncode=0)

    def _start_container(self, container_name: str) -> CommandResult:
        self.start_calls.append(container_name)
        return CommandResult(returncode=0)

    def _restart_container(self, container_name: str) -> CommandResult:
        self.restart_calls.append(container_name)
        return CommandResult(returncode=0)

    def probe_runtime(self, *, assistant_id: str) -> RuntimeProbeResult:
        if len(self.probe_results) > 1:
            return self.probe_results.pop(0)
        return self.probe_results[0]


def build_settings(tmp_path: Path) -> Settings:
    return Settings(
        assistants_workspace_root=tmp_path / "workspaces",
        default_workspace_root_id="default",
        assistants_runtime_root=tmp_path / "runtimes",
        host_iron_agent_root=tmp_path / "host-ironagent",
        iron_agent_source_root=tmp_path / "ironagent-main",
        host_iron_agent_agent_id="main",
        iron_agent_container_image="ironagent-runtime:test",
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


def write_seed_skill(source_root: Path, skill_name: str) -> None:
    skill_dir = source_root / "skills" / skill_name
    (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: seeded skill\n---\n",
        encoding="utf-8",
    )
    (skill_dir / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")


def test_default_workspace_metadata_backend_does_not_mount_sqlite(tmp_path) -> None:
    provisioner = AssistantRuntimeProvisioner(settings=build_settings(tmp_path))

    env = provisioner._managed_container_env("assistant-1")

    assert "IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND" not in env
    assert "IRON_AGENT_WORKSPACE_FILE_SQLITE_PATH" not in env
    assert provisioner._shared_workspace_metadata_binds() == []


def test_sqlite_workspace_metadata_backend_mounts_shared_sqlite(tmp_path) -> None:
    settings = build_settings(tmp_path).model_copy(
        update={"workspace_file_metadata_backend": "sqlite"}
    )
    provisioner = AssistantRuntimeProvisioner(settings=settings)

    env = provisioner._managed_container_env("assistant-1")

    assert env["IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND"] == "sqlite"
    assert env["IRON_AGENT_WORKSPACE_FILE_SQLITE_PATH"].endswith(
        f"/{settings.workspace_file_sqlite_path.name}"
    )
    assert provisioner._shared_workspace_metadata_binds()


def test_provisioner_copies_template_only_when_missing(tmp_path) -> None:
    provisioner = AssistantRuntimeProvisioner(settings=build_settings(tmp_path))
    source = tmp_path / "template.md"
    target = tmp_path / "workspace" / "template.md"
    target.parent.mkdir()
    source.write_text("seed template\n", encoding="utf-8")

    provisioner._copy_if_missing(source, target)
    source.write_text("updated template\n", encoding="utf-8")
    provisioner._copy_if_missing(source, target)

    assert target.read_text(encoding="utf-8") == "seed template\n"


def test_provisioner_replaces_default_agents_placeholder_with_template(tmp_path) -> None:
    settings = build_settings(tmp_path)
    template = settings.iron_agent_source_root / "docs" / "reference" / "templates" / "AGENTS.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# AGENTS.md - Your Workspace\n\n## Result Files\n\n"
        "- If one answer would produce three or more final result files, "
        "create a single `.zip` archive.\n",
        encoding="utf-8",
    )
    workspace_root = settings.assistants_workspace_root / "demo-main"
    workspace_root.mkdir(parents=True)
    (workspace_root / "AGENTS.md").write_text(
        "# Local Assistant Guidance\n\nPlace local instructions for this assistant here.\n",
        encoding="utf-8",
    )
    provisioner = AssistantRuntimeProvisioner(settings=settings)

    provisioner._ensure_workspace(
        assistant_id="demo-main",
        assistant_name="Demo Main",
        workspace_config=None,
    )

    agents_md = (workspace_root / "AGENTS.md").read_text(encoding="utf-8")
    assert "## Result Files" in agents_md
    assert "three or more final result files" in agents_md


def test_provisioner_preserves_custom_agents_guidance(tmp_path) -> None:
    settings = build_settings(tmp_path)
    template = settings.iron_agent_source_root / "docs" / "reference" / "templates" / "AGENTS.md"
    template.parent.mkdir(parents=True)
    template.write_text("# Template AGENTS\n", encoding="utf-8")
    workspace_root = settings.assistants_workspace_root / "demo-main"
    workspace_root.mkdir(parents=True)
    (workspace_root / "AGENTS.md").write_text("# Custom AGENTS\n", encoding="utf-8")
    provisioner = AssistantRuntimeProvisioner(settings=settings)

    provisioner._ensure_workspace(
        assistant_id="demo-main",
        assistant_name="Demo Main",
        workspace_config=None,
    )

    assert (workspace_root / "AGENTS.md").read_text(encoding="utf-8") == "# Custom AGENTS\n"


def test_default_command_runner_uses_common_docker_binary_when_path_misses(monkeypatch) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr("src.runtime.provisioner.shutil.which", lambda _command: None)
    monkeypatch.setattr(
        "src.runtime.provisioner.Path.exists",
        lambda self: str(self) == "/usr/local/bin/docker",
    )

    def fake_run(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr("src.runtime.provisioner.subprocess.run", fake_run)

    result = default_command_runner(["docker", "ps"])

    assert result.returncode == 0
    assert calls == [["/usr/local/bin/docker", "ps"]]


def test_provisioner_does_not_seed_workspace_skills_by_default(tmp_path) -> None:
    write_seed_skill(tmp_path / "ironagent-main", "office-docs")
    runner = SequenceRunner(
        [
            CommandResult(returncode=1, stderr="No such container"),
            CommandResult(returncode=0, stdout="container-123\n"),
            CommandResult(returncode=0, stdout="127.0.0.1:49153\n"),
        ]
    )
    provisioner = AssistantRuntimeProvisioner(
        settings=build_settings(tmp_path),
        command_runner=runner,
    )

    provisioner.ensure_runtime(
        assistant_id="demo-main",
        assistant_name="Demo Main",
    )

    workspace_root = tmp_path / "workspaces" / "demo-main"
    assert (workspace_root / "skills").exists()
    assert not (workspace_root / "skills" / "office-docs").exists()


def pod_payload(*, phase: str = "Running", pod_ip: str = "10.42.0.17") -> str:
    return json.dumps(
        {
            "metadata": {"uid": "pod-uid-123"},
            "status": {"phase": phase, "podIP": pod_ip},
        }
    )


def test_runtime_provisioner_factory_defaults_to_containerd(tmp_path) -> None:
    provisioner = build_runtime_provisioner(settings=build_settings(tmp_path))

    assert isinstance(provisioner, AssistantRuntimeProvisioner)
    assert not isinstance(provisioner, K8sRuntimeProvisioner)


def test_runtime_provisioner_factory_can_select_kubernetes(tmp_path) -> None:
    settings = build_settings(tmp_path).model_copy(update={"runtime_backend": "kubernetes"})

    provisioner = build_runtime_provisioner(settings=settings)

    assert isinstance(provisioner, K8sRuntimeProvisioner)


def test_k8s_provisioner_uses_iron_agent_pod_name_without_legacy_lookup(tmp_path) -> None:
    runner = SequenceRunner(
        [
            CommandResult(returncode=1, stderr="not found"),
            CommandResult(returncode=0, stdout="pod/ironagent-assistant-demo-main-pod\n"),
        ]
    )
    settings = build_settings(tmp_path).model_copy(
        update={
            "runtime_backend": "kubernetes",
            "assistant_container_name_prefix": "ironagent-assistant",
        }
    )
    provisioner = K8sRuntimeProvisioner(settings=settings, command_runner=runner)

    assert provisioner._operation_pod_name_for("demo-main") == (
        "ironagent-assistant-demo-main-pod"
    )
    assert runner.calls == []


def test_k8s_reload_workspace_capability_touches_iron_agent_marker(
    tmp_path,
) -> None:
    runner = SequenceRunner([CommandResult(returncode=0)])
    settings = build_settings(tmp_path).model_copy(update={"runtime_backend": "kubernetes"})
    provisioner = K8sRuntimeProvisioner(settings=settings, command_runner=runner)

    assert (
        provisioner.reload_workspace_capability(
            assistant_id="demo-main",
            kind="plugins",
            slug="spreadsheet-exporter",
        )
        is True
    )
    assert runner.calls == [
        [
            "kubectl",
            "-n",
            "ironagent",
            "exec",
            "ironagent-assistant-demo-main-pod",
            "--",
            "touch",
            "/workspace/.ironagent-capabilities-reload",
        ]
    ]


def test_k8s_provisioner_generates_single_pod_with_workspace_pvc_subpaths(tmp_path) -> None:
    runner = SequenceRunner(
        [
            CommandResult(
                returncode=0,
                stdout='pod "ironagent-assistant-demo-main-pod" deleted\n',
            ),
            CommandResult(returncode=0, stdout="secret/runtime configured\npod/runtime created\n"),
            CommandResult(returncode=0, stdout=pod_payload()),
        ]
    )
    settings = build_settings(tmp_path).model_copy(
        update={
            "runtime_backend": "kubernetes",
            "k8s_runtime_namespace": "ironagent",
            "k8s_runtime_workspace_pvc": "assistant-workspaces",
            "k8s_runtime_workspace_subpath_prefix": "assistants",
            "go_gateway_public_url": "http://gateway.example:30808",
            "iron_agent_hook_service_url": "http://security-service:8000",
        }
    )
    provisioner = K8sRuntimeProvisioner(settings=settings, command_runner=runner)
    provisioner._gateway_healthz_reachable = lambda _url: True

    result = provisioner.ensure_runtime(
        assistant_id="demo-main",
        assistant_name="Demo Main",
        capability_profile="dangerous",
        model_config=AssistantIronAgentConfig(
            primary_model="modelstudio/qwen-plus",
            provider_id="modelstudio",
            provider_base_url="https://dashscope.example/v1",
            provider_api_key="secret-key",
        ),
    )

    workspace_root = tmp_path / "workspaces" / "demo-main"
    state_root = workspace_root / ".ironagent"
    manifest_path = tmp_path / "runtimes" / "demo-main" / "k8s-runtime.yaml"
    assert result.status == "running"
    assert result.container_name == "ironagent-assistant-demo-main-pod"
    assert result.container_id == "pod-uid-123"
    assert result.gateway_url == (
        "http://ironagent-assistant-demo-main-svc.ironagent.svc.cluster.local:18789"
    )
    assert result.workspace_root == str(workspace_root.resolve())
    assert result.state_root == str(state_root.resolve())
    assert (workspace_root / "persistent").exists()
    assert (workspace_root / "temp").exists()
    assert (workspace_root / "IDENTITY.md").exists()
    assert (workspace_root / "SOUL.md").exists()
    assert (state_root / "ironagent.json").exists()
    config = json.loads((state_root / "ironagent.json").read_text(encoding="utf-8"))
    assert {"read", "write", "exec", "process", "subagents"} <= set(config["tools"]["allow"])
    assert "capability_profile" not in config["tools"]
    assert config["tools"]["exec"]["security"] == "full"
    assert config["tools"]["exec"]["ask"] == "off"
    assert runner.calls[0] == [
        "kubectl",
        "-n",
        "ironagent",
        "delete",
        "pod",
        "ironagent-assistant-demo-main-pod",
        "--ignore-not-found=true",
        "--wait=true",
        "--timeout=60s",
    ]
    assert runner.calls[1] == [
        "kubectl",
        "-n",
        "ironagent",
        "apply",
        "-f",
        str(manifest_path.resolve()),
    ]
    assert runner.calls[2] == [
        "kubectl",
        "-n",
        "ironagent",
        "get",
        "pod",
        "ironagent-assistant-demo-main-pod",
        "-o",
        "json",
    ]

    docs = list(yaml.safe_load_all(manifest_path.read_text(encoding="utf-8")))
    secret, service, pod = docs
    assert secret["kind"] == "Secret"
    assert secret["metadata"]["name"] == "ironagent-assistant-demo-main-secret"
    assert "secret-key" in secret["stringData"]["ironagent.json"]
    assert service["kind"] == "Service"
    assert service["metadata"]["name"] == "ironagent-assistant-demo-main-svc"
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["selector"]["ironagent.ironagent/assistant-id"] == "demo-main"
    assert service["spec"]["ports"][0]["targetPort"] == "http"
    assert pod["kind"] == "Pod"
    assert pod["metadata"]["name"] == "ironagent-assistant-demo-main-pod"
    assert pod["spec"]["hostname"].startswith("oc-")
    assert len(pod["spec"]["hostname"]) <= 15
    assert pod["spec"]["automountServiceAccountToken"] is False
    assert pod["spec"]["containers"][0]["workingDir"] == "/workspace"
    command = pod["spec"]["containers"][0]["command"]
    assert command[command.index("--bind") + 1] == "lan"
    volumes = pod["spec"]["volumes"]
    assert volumes[0]["persistentVolumeClaim"]["claimName"] == "assistant-workspaces"
    mounts = pod["spec"]["containers"][0]["volumeMounts"]
    env = {
        item["name"]: item["value"]
        for item in pod["spec"]["containers"][0]["env"]
        if "value" in item
    }
    assert env["IRON_AGENT_API_URL"] == "http://gateway.example:30808"
    assert env["IRON_AGENT_SERVICE_URL"] == "http://security-service:8000"
    assert env["IRON_AGENT_WORKSPACE_DIR"] == "/workspace"
    assert env["IRON_AGENT_BUNDLED_SKILLS_DIR"] == "/workspace/skills"
    assert env["IRON_AGENT_WORKSPACE_SKILLS_ONLY"] == "1"
    assert env["IRON_AGENT_WORKSPACE_PLUGINS_ONLY"] == "1"
    mdns_env = next(
        item
        for item in pod["spec"]["containers"][0]["env"]
        if item["name"] == "IRONAGENT_MDNS_HOSTNAME"
    )
    assert mdns_env["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.uid"
    assert mounts[0]["mountPath"] == "/workspace"
    assert mounts[0]["subPath"] == "assistants/demo-main"
    assert mounts[1]["mountPath"] == "/home/node/.ironagent"
    assert mounts[1]["subPath"] == "assistants/demo-main/.ironagent"
    init = pod["spec"]["initContainers"][0]
    assert init["image"] == "ironagent-runtime:test"
    assert init["imagePullPolicy"] == "IfNotPresent"
    assert init["env"] == [
        {"name": "WORKSPACE_SUBPATH", "value": "assistants/demo-main"},
        {"name": "STATE_DIR", "value": ".ironagent"},
    ]
    assert "chown -R 1000:1000" in init["command"][-1]


def test_k8s_manifest_quotes_numeric_like_string_values(tmp_path) -> None:
    settings = build_settings(tmp_path).model_copy(
        update={
            "runtime_backend": "kubernetes",
            "k8s_runtime_namespace": "ironagent",
            "k8s_runtime_workspace_pvc": "assistant-workspaces",
            "k8s_runtime_workspace_subpath_prefix": "assistants",
        }
    )
    provisioner = K8sRuntimeProvisioner(
        settings=settings,
        command_runner=lambda _args: CommandResult(returncode=0),
    )
    state_root = tmp_path / "workspaces" / "0099" / ".ironagent"
    state_root.mkdir(parents=True)
    (state_root / "ironagent.json").write_text("{}", encoding="utf-8")

    manifest_path = provisioner._write_runtime_manifest(
        assistant_id="0099",
        workspace_slug="0099",
        state_root=state_root,
    )
    manifest_text = manifest_path.read_text(encoding="utf-8")

    assert 'ironagent.ironagent/assistant-id: "0099"' in manifest_text
    assert 'value: "0099"' in manifest_text
    assert 'subPath: "assistants/0099"' in manifest_text
    docs = list(yaml.safe_load_all(manifest_text))
    secret, service, pod = docs
    assert secret["metadata"]["labels"]["ironagent.ironagent/assistant-id"] == "0099"
    assert service["metadata"]["labels"]["ironagent.ironagent/assistant-id"] == "0099"
    env = {
        item["name"]: item["value"]
        for item in pod["spec"]["containers"][0]["env"]
        if "value" in item
    }
    assert env["IRON_AGENT_ASSISTANT_ID"] == "0099"


def test_k8s_runtime_uses_short_hostname_for_long_assistant_ids(tmp_path) -> None:
    runner = SequenceRunner(
        [
            CommandResult(returncode=0, stdout="pod deleted\n"),
            CommandResult(returncode=0, stdout="secret/runtime configured\npod/runtime created\n"),
            CommandResult(
                returncode=0,
                stdout=json.dumps(
                    {
                        "metadata": {"uid": "pod-uid-long"},
                        "status": {
                            "phase": "Running",
                            "podIP": "10.42.0.18",
                            "containerStatuses": [
                                {
                                    "name": "ironagent-gateway",
                                    "ready": True,
                                    "state": {"running": {}},
                                }
                            ],
                        },
                    }
            ),
                ),
        ]
    )
    settings = build_settings(tmp_path).model_copy(
        update={
            "runtime_backend": "kubernetes",
            "k8s_runtime_namespace": "ironagent",
            "k8s_runtime_workspace_pvc": "assistant-workspaces",
            "k8s_runtime_workspace_subpath_prefix": "assistants",
        }
    )
    provisioner = K8sRuntimeProvisioner(settings=settings, command_runner=runner)
    provisioner._gateway_healthz_reachable = lambda _url: True

    provisioner.ensure_runtime(
        assistant_id="codex-e2e-delete-20260504023719",
        assistant_name="Long Hostname",
        model_config=AssistantIronAgentConfig(),
    )

    manifest_path = tmp_path / "runtimes" / "codex-e2e-delete-20260504023719" / "k8s-runtime.yaml"
    docs = list(yaml.safe_load_all(manifest_path.read_text(encoding="utf-8")))
    pod = docs[2]

    assert pod["metadata"]["name"] == "ironagent-assistant-codex-e2e-delete-20260504023719-pod"
    assert pod["spec"]["hostname"].startswith("oc-")
    assert len(pod["spec"]["hostname"]) <= 15


def test_k8s_ensure_replaces_existing_pod_before_applying_manifest(tmp_path) -> None:
    runner = SequenceRunner(
        [
            CommandResult(returncode=0, stdout="pod deleted\n"),
            CommandResult(returncode=0, stdout="secret/runtime configured\npod/runtime created\n"),
            CommandResult(returncode=0, stdout=pod_payload()),
        ]
    )
    settings = build_settings(tmp_path).model_copy(
        update={
            "runtime_backend": "kubernetes",
            "k8s_runtime_namespace": "ironagent",
        }
    )
    provisioner = K8sRuntimeProvisioner(settings=settings, command_runner=runner)
    provisioner._gateway_healthz_reachable = lambda _url: True

    result = provisioner.ensure_runtime(
        assistant_id="demo-main",
        assistant_name="Demo Main",
    )

    assert result.status == "running"
    assert runner.calls[0] == [
        "kubectl",
        "-n",
        "ironagent",
        "delete",
        "pod",
        "ironagent-assistant-demo-main-pod",
        "--ignore-not-found=true",
        "--wait=true",
        "--timeout=60s",
    ]
    assert runner.calls[1][0:4] == ["kubectl", "-n", "ironagent", "apply"]


def test_k8s_restart_deletes_pod_and_forces_reprovision(tmp_path) -> None:
    runner = SequenceRunner([CommandResult(returncode=0, stdout="pod deleted\n")])
    settings = build_settings(tmp_path).model_copy(
        update={
            "runtime_backend": "kubernetes",
            "k8s_runtime_namespace": "ironagent",
        }
    )
    provisioner = K8sRuntimeProvisioner(settings=settings, command_runner=runner)

    result = provisioner.restart_runtime(assistant_id="demo-main")

    assert result.status == "failed"
    assert result.container_exists is False
    assert result.last_error == "runtime pod deleted; reprovision required"
    assert runner.calls == [
        [
            "kubectl",
            "-n",
            "ironagent",
            "delete",
            "pod",
            "ironagent-assistant-demo-main-pod",
            "--ignore-not-found=true",
            "--wait=true",
            "--timeout=60s",
    ]
        ]


def test_k8s_probe_keeps_running_pod_starting_until_gateway_healthz(tmp_path) -> None:
    runner = SequenceRunner([CommandResult(returncode=0, stdout=pod_payload())])
    settings = build_settings(tmp_path).model_copy(update={"runtime_backend": "kubernetes"})
    provisioner = K8sRuntimeProvisioner(settings=settings, command_runner=runner)
    provisioner._gateway_healthz_reachable = lambda _url: False

    result = provisioner.probe_runtime(assistant_id="demo-main")

    assert result.container_exists is True
    assert result.container_state == "running"
    assert result.status == "starting"
    assert result.health_state == "starting"
    assert result.gateway_url == (
        "http://ironagent-assistant-demo-main-svc.ironagent.svc.cluster.local:18789"
    )
    assert result.last_error == "gateway healthz is not reachable"


def test_k8s_destroy_does_not_delete_workspace_by_default(tmp_path) -> None:
    workspace_root = tmp_path / "workspaces" / "demo-main"
    workspace_root.mkdir(parents=True)
    manifest_path = tmp_path / "runtimes" / "demo-main" / "k8s-runtime.yaml"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text("---\nkind: Pod\n", encoding="utf-8")
    runner = SequenceRunner([CommandResult(returncode=0, stdout="deleted\n")])
    settings = build_settings(tmp_path).model_copy(update={"runtime_backend": "kubernetes"})
    provisioner = K8sRuntimeProvisioner(settings=settings, command_runner=runner)

    result = provisioner.destroy_runtime(
        assistant_id="demo-main",
        workspace_root=str(workspace_root),
        state_root=str(workspace_root / ".ironagent"),
    )

    assert result.container_removed is True
    assert result.workspace_removed is False
    assert workspace_root.exists()
    assert runner.calls[0] == [
        "kubectl",
        "-n",
        "ironagent",
        "delete",
        "-f",
        str(manifest_path.resolve()),
        "--ignore-not-found=true",
        "--wait=false",
    ]


def test_k8s_destroy_without_manifest_deletes_named_pod_and_secret(tmp_path) -> None:
    workspace_root = tmp_path / "workspaces" / "demo-main"
    workspace_root.mkdir(parents=True)
    runner = SequenceRunner([CommandResult(returncode=0, stdout="deleted\n")])
    settings = build_settings(tmp_path).model_copy(update={"runtime_backend": "kubernetes"})
    provisioner = K8sRuntimeProvisioner(settings=settings, command_runner=runner)

    result = provisioner.destroy_runtime(
        assistant_id="demo-main",
        workspace_root=str(workspace_root),
        state_root=str(workspace_root / ".ironagent"),
    )

    assert result.container_removed is True
    assert result.workspace_removed is False
    assert runner.calls[0] == [
        "kubectl",
        "-n",
        "ironagent",
        "delete",
        "pod/ironagent-assistant-demo-main-pod",
        "secret/ironagent-assistant-demo-main-secret",
        "service/ironagent-assistant-demo-main-svc",
        "--ignore-not-found=true",
        "--wait=false",
    ]


def test_provisioner_creates_workspace_and_container(tmp_path) -> None:
    write_seed_skill(tmp_path / "ironagent-main", "office-docs")
    write_seed_skill(tmp_path / "ironagent-main", "local-pdf")
    runner = SequenceRunner(
        [
            CommandResult(returncode=1, stderr="No such container"),
            CommandResult(returncode=0, stdout="container-123\n"),
            CommandResult(returncode=0, stdout="127.0.0.1:49153\n"),
        ]
    )
    provisioner = AssistantRuntimeProvisioner(
        settings=build_settings(tmp_path).model_copy(
            update={"iron_agent_seed_workspace_skills": "office-docs,local-pdf"}
        ),
        command_runner=runner,
    )
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

    workspace_root = tmp_path / "workspaces" / "demo-main"
    state_root = tmp_path / "runtimes" / "demo-main" / "state"
    assert result.status == "running"
    assert result.container_id == "container-123"
    assert result.workspace_root == str(workspace_root.resolve())
    assert result.workspace_root_id == "default"
    assert result.workspace_slug == "demo-main"
    assert result.state_root == str(state_root.resolve())
    assert result.gateway_port == 49153
    assert result.gateway_url == "http://127.0.0.1:49153"
    assert result.gateway_token == "ironagent-local-demo-main"
    assert (workspace_root / "temp").exists()
    assert (workspace_root / "persistent").exists()
    assert (workspace_root / "plugins").exists()
    assert (workspace_root / "USER.md").exists()
    assert (workspace_root / "AGENTS.md").exists()
    assert (workspace_root / "IDENTITY.md").exists()
    assert (workspace_root / "SOUL.md").exists()
    assert (workspace_root / "skills" / "office-docs" / "SKILL.md").exists()
    assert (workspace_root / "skills" / "local-pdf" / "SKILL.md").exists()
    config = json.loads((state_root / "ironagent.json").read_text(encoding="utf-8"))
    assert config["gateway"]["controlUi"]["dangerouslyAllowHostHeaderOriginFallback"] is True
    assert config["gateway"]["controlUi"]["dangerouslyDisableDeviceAuth"] is True
    assert config["gateway"]["controlUi"]["allowedOrigins"] == [
        "http://127.0.0.1:18789",
        "http://localhost:18789",
        "http://127.0.0.1:19001",
        "http://localhost:19001",
    ]
    assert config["agents"]["defaults"]["model"]["primary"] == "modelstudio/qwen3-coder-next"
    auth_profiles = json.loads(
        (
            state_root / "agents" / "demo-main" / "agent" / "auth-profiles.json"
        ).read_text(encoding="utf-8")
    )
    assert auth_profiles["profiles"]["modelstudio:default"] == {
        "type": "api_key",
        "provider": "modelstudio",
        "key": "secret-key",
    }
    assert auth_profiles["order"]["modelstudio"] == ["modelstudio:default"]
    assert auth_profiles["lastGood"]["modelstudio"] == "modelstudio:default"
    assert config["agents"]["list"][0] == {
        "id": "demo-main",
        "name": "demo-main",
        "default": True,
        "workspace": "/workspace",
    }
    assert "capability_profile" not in config["tools"]
    assert "exec" not in config["tools"]
    assert config["models"]["providers"]["modelstudio"]["baseUrl"] == (
        "https://coding.dashscope.aliyuncs.com/v1"
    )
    assert config["models"]["providers"]["modelstudio"]["api"] == "openai-completions"
    assert config["models"]["providers"]["modelstudio"]["apiKey"] == "***"
    assert config["models"]["providers"]["modelstudio"]["authHeader"] is False
    assert config["models"]["providers"]["modelstudio"]["headers"] == {
        "Authorization": "secret-key",
    }
    assert config["models"]["providers"]["modelstudio"]["models"] == [
        {"id": "qwen3-coder-next", "name": "qwen3-coder-next"}
    ]
    assert "IRON_AGENT_HOOKS_ENABLED=true" in runner.calls[1]
    assert "IRON_AGENT_SERVICE_URL=http://host.docker.internal:8000" in runner.calls[1]
    assert "IRON_AGENT_FAIL_CLOSED=true" in runner.calls[1]
    assert "IRON_AGENT_HOOK_TIMEOUT_MS=5000" in runner.calls[1]
    assert "IRON_AGENT_APPROVAL_POLL_MS=1000" in runner.calls[1]
    assert "IRON_AGENT_APPROVAL_MAX_WAIT_MS=300000" in runner.calls[1]
    assert runner.calls[1][0:5] == ["docker", "run", "-d", "--init", "--name"]
    assert "--add-host" in runner.calls[1]
    assert "host.docker.internal:host-gateway" in runner.calls[1]
    assert runner.calls[1][-8:] == [
        "node",
        "/app/ironagent.mjs",
        "gateway",
        "--bind",
        "lan",
        "--port",
        "18789",
        "--allow-unconfigured",
    ]


def test_provisioner_uses_public_security_url_for_internal_hook_service(tmp_path) -> None:
    write_seed_skill(tmp_path / "ironagent-main", "office-docs")
    write_seed_skill(tmp_path / "ironagent-main", "local-pdf")
    runner = SequenceRunner(
        [
            CommandResult(returncode=1, stderr="No such container"),
            CommandResult(returncode=0, stdout="container-123\n"),
            CommandResult(returncode=0, stdout="127.0.0.1:49153\n"),
        ]
    )
    settings = build_settings(tmp_path).model_copy(
        update={
            "iron_agent_hook_service_url": "http://security-service:8000",
            "security_service_public_url": "http://192.0.2.11:30800",
            "iron_agent_no_proxy": "security-service",
        }
    )
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    provisioner.ensure_runtime(
        assistant_id="demo-main",
        assistant_name="Demo Main",
    )

    run_args = runner.calls[1]
    assert "IRON_AGENT_SERVICE_URL=http://192.0.2.11:30800" in run_args
    assert "IRON_AGENT_SERVICE_URL=http://security-service:8000" not in run_args
    expected_no_proxy = (
        "security-service,127.0.0.1,localhost,::1,host.docker.internal,192.0.2.11"
    )
    assert f"NO_PROXY={expected_no_proxy}" in run_args
    assert f"no_proxy={expected_no_proxy}" in run_args


def test_provisioner_uses_go_gateway_public_url_for_single_entry_runtime(tmp_path) -> None:
    write_seed_skill(tmp_path / "ironagent-main", "office-docs")
    write_seed_skill(tmp_path / "ironagent-main", "local-pdf")
    runner = SequenceRunner(
        [
            CommandResult(returncode=1, stderr="No such container"),
            CommandResult(returncode=0, stdout="container-123\n"),
            CommandResult(returncode=0, stdout="127.0.0.1:49153\n"),
        ]
    )
    settings = build_settings(tmp_path).model_copy(
        update={
            "go_gateway_public_url": "http://192.0.2.10:30808",
            "iron_agent_hook_service_url": "http://security-service:8000",
            "security_service_public_url": "",
            "iron_agent_no_proxy": "security-service",
        }
    )
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    provisioner.ensure_runtime(
        assistant_id="demo-main",
        assistant_name="Demo Main",
    )

    run_args = runner.calls[1]
    assert "IRON_AGENT_API_URL=http://192.0.2.10:30808" in run_args
    assert "IRON_AGENT_BUNDLED_SKILLS_DIR=/workspace/skills" in run_args
    assert "IRON_AGENT_SERVICE_URL=http://192.0.2.10:30808" in run_args
    assert "IRON_AGENT_SERVICE_URL=http://security-service:8000" not in run_args
    expected_no_proxy = (
        "security-service,127.0.0.1,localhost,::1,host.docker.internal,192.0.2.10"
    )
    assert f"NO_PROXY={expected_no_proxy}" in run_args
    assert f"no_proxy={expected_no_proxy}" in run_args


def test_provisioner_uses_configured_workspace_root_and_slug(tmp_path) -> None:
    custom_root = tmp_path / "managed-workspaces"
    settings = build_settings(tmp_path).model_copy(
        update={
            "allowed_workspace_roots_json": json.dumps(
                [
                    {
                        "id": "managed",
                        "label": "Managed",
                        "host_path": str(custom_root),
                    }
                ]
            ),
            "iron_agent_seed_workspace_skills": "office-docs,local-pdf",
        }
    )
    write_seed_skill(tmp_path / "ironagent-main", "office-docs")
    write_seed_skill(tmp_path / "ironagent-main", "local-pdf")
    runner = SequenceRunner(
        [
            CommandResult(returncode=1, stderr="No such container"),
            CommandResult(returncode=0, stdout="container-managed\n"),
            CommandResult(returncode=0, stdout="127.0.0.1:49170\n"),
        ]
    )
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    result = provisioner.ensure_runtime(
        assistant_id="demo-custom",
        assistant_name="Demo Custom",
        workspace_config=AssistantWorkspaceConfig(
            selection_mode="managed_root",
            root_id="managed",
            workspace_slug="team-space",
        ),
    )

    workspace_root = custom_root / "team-space"
    assert result.workspace_root == str(workspace_root.resolve())
    assert result.workspace_root_id == "managed"
    assert result.workspace_slug == "team-space"
    assert (workspace_root / "temp").exists()
    assert (workspace_root / "persistent").exists()
    assert (workspace_root / "plugins").exists()
    assert (workspace_root / "skills" / "office-docs" / "SKILL.md").exists()


def test_provisioner_reuses_existing_container_and_starts_if_needed(tmp_path) -> None:
    settings = build_settings(tmp_path)
    write_seed_skill(tmp_path / "ironagent-main", "office-docs")
    write_seed_skill(tmp_path / "ironagent-main", "local-pdf")
    workspace_root = (settings.assistants_workspace_root / "demo-main").resolve()
    state_root = (settings.assistants_runtime_root / "demo-main" / "state").resolve()
    runner = SequenceRunner(
        [
            CommandResult(returncode=0, stdout="container-456|exited\n"),
            CommandResult(
                returncode=0,
                stdout=(
                    '["HOME=/home/node","TERM=xterm-256color",'
                    '"IRON_AGENT_ASSISTANT_ID=demo-main",'
                    '"IRON_AGENT_RUNTIME_CONTAINER_IMAGE=ironagent-runtime:test",'
                    '"IRON_AGENT_STATE_DIR=/home/node/.ironagent",'
                    '"IRON_AGENT_CONFIG_PATH=/home/node/.ironagent/ironagent.json",'
                    '"IRON_AGENT_WORKSPACE_DIR=/workspace",'
                    '"IRON_AGENT_BUNDLED_SKILLS_DIR=/workspace/skills",'
                    '"IRON_AGENT_WORKSPACE_SKILLS_ONLY=1",'
                    '"IRON_AGENT_WORKSPACE_PLUGINS_ONLY=1",'
                    '"IRON_AGENT_API_URL=http://127.0.0.1:8000",'
                    '"IRON_AGENT_HOOKS_ENABLED=true",'
                    '"IRON_AGENT_SERVICE_URL=http://host.docker.internal:8000",'
                    '"IRON_AGENT_FAIL_CLOSED=true",'
                    '"IRON_AGENT_HOOK_TIMEOUT_MS=5000",'
                    '"IRON_AGENT_APPROVAL_POLL_MS=1000",'
                    '"IRON_AGENT_APPROVAL_MAX_WAIT_MS=300000"]\n'
            ),
                ),
            CommandResult(
                returncode=0,
                stdout=(f'["{state_root}:/home/node/.ironagent","{workspace_root}:/workspace"]\n'),
            ),
            CommandResult(returncode=0, stdout="/workspace\n"),
            CommandResult(returncode=0, stdout='{"18789/tcp":[{"HostIp":""}]}\n'),
            CommandResult(
                returncode=0,
                stdout='["host.docker.internal:host-gateway"]\n',
            ),
            CommandResult(returncode=0, stdout="container-456\n"),
            CommandResult(returncode=0, stdout="127.0.0.1:49154\n"),
        ]
    )
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)
    provisioner._gateway_healthz_reachable = lambda _url: True

    result = provisioner.ensure_runtime(
        assistant_id="demo-main",
        assistant_name="Demo Main",
    )

    assert result.status == "running"
    assert result.container_id == "container-456"
    assert result.gateway_port == 49154
    assert result.gateway_token == "ironagent-local-demo-main"
    assert runner.calls[6] == ["docker", "start", "ironagent-assistant-demo-main"]


def test_provisioner_marks_failure_when_container_creation_fails(tmp_path) -> None:
    write_seed_skill(tmp_path / "ironagent-main", "office-docs")
    write_seed_skill(tmp_path / "ironagent-main", "local-pdf")
    runner = SequenceRunner(
        [
            CommandResult(returncode=1, stderr="No such container"),
            CommandResult(returncode=125, stderr="image not found"),
        ]
    )
    provisioner = AssistantRuntimeProvisioner(
        settings=build_settings(tmp_path),
        command_runner=runner,
    )

    result = provisioner.ensure_runtime(
        assistant_id="demo-main",
        assistant_name="Demo Main",
    )

    assert result.status == "failed"
    assert result.gateway_token == "ironagent-local-demo-main"
    assert result.last_error == "image not found"


def test_provisioner_parses_first_published_gateway_port(tmp_path) -> None:
    settings = build_settings(tmp_path)
    write_seed_skill(tmp_path / "ironagent-main", "office-docs")
    write_seed_skill(tmp_path / "ironagent-main", "local-pdf")
    workspace_root = (settings.assistants_workspace_root / "demo-main").resolve()
    state_root = (settings.assistants_runtime_root / "demo-main" / "state").resolve()
    runner = SequenceRunner(
        [
            CommandResult(returncode=0, stdout="container-789|running\n"),
            CommandResult(
                returncode=0,
                stdout=(
                    '["HOME=/home/node","TERM=xterm-256color",'
                    '"IRON_AGENT_ASSISTANT_ID=demo-main",'
                    '"IRON_AGENT_RUNTIME_CONTAINER_IMAGE=ironagent-runtime:test",'
                    '"IRON_AGENT_STATE_DIR=/home/node/.ironagent",'
                    '"IRON_AGENT_CONFIG_PATH=/home/node/.ironagent/ironagent.json",'
                    '"IRON_AGENT_WORKSPACE_DIR=/workspace",'
                    '"IRON_AGENT_BUNDLED_SKILLS_DIR=/workspace/skills",'
                    '"IRON_AGENT_WORKSPACE_SKILLS_ONLY=1",'
                    '"IRON_AGENT_WORKSPACE_PLUGINS_ONLY=1",'
                    '"IRON_AGENT_API_URL=http://127.0.0.1:8000",'
                    '"IRON_AGENT_HOOKS_ENABLED=true",'
                    '"IRON_AGENT_SERVICE_URL=http://host.docker.internal:8000",'
                    '"IRON_AGENT_FAIL_CLOSED=true",'
                    '"IRON_AGENT_HOOK_TIMEOUT_MS=5000",'
                    '"IRON_AGENT_APPROVAL_POLL_MS=1000",'
                    '"IRON_AGENT_APPROVAL_MAX_WAIT_MS=300000"]\n'
            ),
                ),
            CommandResult(
                returncode=0,
                stdout=(f'["{state_root}:/home/node/.ironagent","{workspace_root}:/workspace"]\n'),
            ),
            CommandResult(returncode=0, stdout="/workspace\n"),
            CommandResult(returncode=0, stdout='{"18789/tcp":[{"HostIp":""}]}\n'),
            CommandResult(
                returncode=0,
                stdout='["host.docker.internal:host-gateway"]\n',
            ),
            CommandResult(returncode=0, stdout="container-789\n"),
            CommandResult(returncode=0, stdout="0.0.0.0:49155\n127.0.0.1:49156\n"),
        ]
    )
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    result = provisioner.ensure_runtime(
        assistant_id="demo-main",
        assistant_name="Demo Main",
    )

    assert result.gateway_host == "127.0.0.1"
    assert result.gateway_port == 49155
    assert result.gateway_url == "http://127.0.0.1:49155"
    assert runner.calls[6] == ["docker", "restart", "ironagent-assistant-demo-main"]


def test_provisioner_normalizes_wildcard_gateway_host_to_loopback(tmp_path) -> None:
    settings = build_settings(tmp_path)
    write_seed_skill(tmp_path / "ironagent-main", "office-docs")
    write_seed_skill(tmp_path / "ironagent-main", "local-pdf")
    runner = SequenceRunner(
        [
            CommandResult(returncode=1, stderr="No such container"),
            CommandResult(returncode=0, stdout="container-790\n"),
            CommandResult(returncode=0, stdout="0.0.0.0:49157\n"),
        ]
    )
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    result = provisioner.ensure_runtime(
        assistant_id="demo-main",
        assistant_name="Demo Main",
    )

    assert result.gateway_host == "127.0.0.1"
    assert result.gateway_port == 49157
    assert result.gateway_url == "http://127.0.0.1:49157"
    publish_index = runner.calls[1].index("-p")
    assert runner.calls[1][publish_index + 1] == "::18789"


def test_provisioner_prefers_public_control_ui_host_for_wildcard_gateway_bind(tmp_path) -> None:
    settings = build_settings(tmp_path).model_copy(
        update={
            "iron_agent_gateway_host": "0.0.0.0",
            "control_ui_public_url": "http://192.0.2.11:30789",
        }
    )
    write_seed_skill(tmp_path / "ironagent-main", "office-docs")
    write_seed_skill(tmp_path / "ironagent-main", "local-pdf")
    runner = SequenceRunner(
        [
            CommandResult(returncode=1, stderr="No such container"),
            CommandResult(returncode=0, stdout="container-791\n"),
            CommandResult(returncode=0, stdout="0.0.0.0:49158\n"),
        ]
    )
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    result = provisioner.ensure_runtime(
        assistant_id="demo-main",
        assistant_name="Demo Main",
    )

    assert result.gateway_host == "192.0.2.11"
    assert result.gateway_port == 49158
    assert result.gateway_url == "http://192.0.2.11:49158"
    publish_index = runner.calls[1].index("-p")
    assert runner.calls[1][publish_index + 1] == "0.0.0.0::18789"


def test_provisioner_uses_internal_gateway_publish_and_access_host(tmp_path) -> None:
    settings = build_settings(tmp_path).model_copy(
        update={
            "iron_agent_gateway_host": "0.0.0.0",
            "iron_agent_gateway_publish_host": "10.0.12.16",
            "iron_agent_gateway_access_host": "10.0.12.16",
            "control_ui_public_url": "http://192.0.2.10:30808",
        }
    )
    write_seed_skill(tmp_path / "ironagent-main", "office-docs")
    write_seed_skill(tmp_path / "ironagent-main", "local-pdf")
    runner = SequenceRunner(
        [
            CommandResult(returncode=1, stderr="No such container"),
            CommandResult(returncode=0, stdout="container-793\n"),
            CommandResult(returncode=0, stdout="10.0.12.16:49158\n"),
        ]
    )
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    result = provisioner.ensure_runtime(
        assistant_id="demo-main",
        assistant_name="Demo Main",
    )

    assert result.gateway_host == "10.0.12.16"
    assert result.gateway_port == 49158
    assert result.gateway_url == "http://10.0.12.16:49158"
    publish_index = runner.calls[1].index("-p")
    assert runner.calls[1][publish_index + 1] == "10.0.12.16::18789"


def test_provisioner_rewrites_loopback_port_output_to_public_control_ui_host(tmp_path) -> None:
    settings = build_settings(tmp_path).model_copy(
        update={
            "iron_agent_gateway_host": "0.0.0.0",
            "control_ui_public_url": "http://192.0.2.11:30789",
        }
    )
    write_seed_skill(tmp_path / "ironagent-main", "office-docs")
    write_seed_skill(tmp_path / "ironagent-main", "local-pdf")
    runner = SequenceRunner(
        [
            CommandResult(returncode=1, stderr="No such container"),
            CommandResult(returncode=0, stdout="container-792\n"),
            CommandResult(returncode=0, stdout="127.0.0.1:49159\n"),
        ]
    )
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    result = provisioner.ensure_runtime(
        assistant_id="demo-main",
        assistant_name="Demo Main",
    )

    assert result.gateway_host == "192.0.2.11"
    assert result.gateway_port == 49159
    assert result.gateway_url == "http://192.0.2.11:49159"


def test_provisioner_prefers_public_control_ui_host_over_default_loopback_setting(tmp_path) -> None:
    settings = build_settings(tmp_path).model_copy(
        update={"control_ui_public_url": "http://192.0.2.11:30789"}
    )
    write_seed_skill(tmp_path / "ironagent-main", "office-docs")
    write_seed_skill(tmp_path / "ironagent-main", "local-pdf")
    runner = SequenceRunner(
        [
            CommandResult(returncode=1, stderr="No such container"),
            CommandResult(returncode=0, stdout="container-793\n"),
            CommandResult(returncode=0, stdout="127.0.0.1:49160\n"),
        ]
    )
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    result = provisioner.ensure_runtime(
        assistant_id="demo-main",
        assistant_name="Demo Main",
    )

    assert result.gateway_host == "192.0.2.11"
    assert result.gateway_port == 49160
    assert result.gateway_url == "http://192.0.2.11:49160"


def test_provisioner_skips_hook_env_when_bridge_disabled(tmp_path) -> None:
    write_seed_skill(tmp_path / "ironagent-main", "office-docs")
    write_seed_skill(tmp_path / "ironagent-main", "local-pdf")
    runner = SequenceRunner(
        [
            CommandResult(returncode=1, stderr="No such container"),
            CommandResult(returncode=0, stdout="container-321\n"),
            CommandResult(returncode=0, stdout="127.0.0.1:49159\n"),
        ]
    )
    settings = build_settings(tmp_path).model_copy(update={"iron_agent_hooks_enabled": False})
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    provisioner.ensure_runtime(
        assistant_id="demo-main",
        assistant_name="Demo Main",
    )

    run_args = runner.calls[1]
    assert "IRON_AGENT_HOOKS_ENABLED=true" not in run_args
    assert not any(arg.startswith("IRON_AGENT_SERVICE_URL=") for arg in run_args)


def test_provisioner_injects_proxy_env_and_augments_no_proxy(tmp_path) -> None:
    write_seed_skill(tmp_path / "ironagent-main", "office-docs")
    write_seed_skill(tmp_path / "ironagent-main", "local-pdf")
    runner = SequenceRunner(
        [
            CommandResult(returncode=1, stderr="No such container"),
            CommandResult(returncode=0, stdout="container-proxy\n"),
            CommandResult(returncode=0, stdout="127.0.0.1:49160\n"),
        ]
    )
    settings = build_settings(tmp_path).model_copy(
        update={
            "iron_agent_http_proxy": "http://host.docker.internal:7890",
            "iron_agent_https_proxy": "http://host.docker.internal:7890",
            "iron_agent_no_proxy": "internal.example",
        }
    )
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    provisioner.ensure_runtime(
        assistant_id="demo-main",
        assistant_name="Demo Main",
    )

    run_args = runner.calls[1]
    assert "HTTP_PROXY=http://host.docker.internal:7890" in run_args
    assert "http_proxy=http://host.docker.internal:7890" in run_args
    assert "HTTPS_PROXY=http://host.docker.internal:7890" in run_args
    assert "https_proxy=http://host.docker.internal:7890" in run_args
    expected_no_proxy = "internal.example,127.0.0.1,localhost,::1,host.docker.internal"
    assert f"NO_PROXY={expected_no_proxy}" in run_args
    assert f"no_proxy={expected_no_proxy}" in run_args


def test_provisioner_recreates_container_when_proxy_env_is_removed(tmp_path) -> None:
    settings = build_settings(tmp_path)
    write_seed_skill(tmp_path / "ironagent-main", "office-docs")
    write_seed_skill(tmp_path / "ironagent-main", "local-pdf")
    workspace_root = (settings.assistants_workspace_root / "demo-main").resolve()
    state_root = (settings.assistants_runtime_root / "demo-main" / "state").resolve()
    runner = SequenceRunner(
        [
            CommandResult(
                returncode=0,
                stdout=(
                    '["HOME=/home/node","TERM=xterm-256color",'
                    '"IRON_AGENT_ASSISTANT_ID=demo-main",'
                    '"IRON_AGENT_RUNTIME_CONTAINER_IMAGE=ironagent-runtime:test",'
                    '"IRON_AGENT_STATE_DIR=/home/node/.ironagent",'
                    '"IRON_AGENT_CONFIG_PATH=/home/node/.ironagent/ironagent.json",'
                    '"IRON_AGENT_WORKSPACE_DIR=/workspace",'
                    '"IRON_AGENT_BUNDLED_SKILLS_DIR=/workspace/skills",'
                    '"IRON_AGENT_WORKSPACE_SKILLS_ONLY=1",'
                    '"IRON_AGENT_WORKSPACE_PLUGINS_ONLY=1",'
                    '"IRON_AGENT_API_URL=http://127.0.0.1:8000",'
                    '"IRON_AGENT_HOOKS_ENABLED=true",'
                    '"IRON_AGENT_SERVICE_URL=http://host.docker.internal:8000",'
                    '"IRON_AGENT_FAIL_CLOSED=true",'
                    '"IRON_AGENT_HOOK_TIMEOUT_MS=5000",'
                    '"IRON_AGENT_APPROVAL_POLL_MS=1000",'
                    '"IRON_AGENT_APPROVAL_MAX_WAIT_MS=300000",'
                    '"HTTP_PROXY=http://host.docker.internal:7890",'
                    '"http_proxy=http://host.docker.internal:7890"]\n'
            ),
                ),
            CommandResult(
                returncode=0,
                stdout=(
                    f'["{state_root}:/home/node/.ironagent",'
                    f'"{workspace_root}:/home/node/.ironagent/workspace"]\n'
            ),
                ),
            CommandResult(
                returncode=0,
                stdout='["host.docker.internal:host-gateway"]\n',
            ),
        ]
    )
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    should_recreate = provisioner._container_requires_recreate(
        assistant_id="demo-main",
        container_name="ironagent-assistant-demo-main",
        workspace_root=workspace_root,
        state_root=state_root,
    )

    assert should_recreate is True


def test_provisioner_recreates_container_when_gateway_publish_host_changed(tmp_path) -> None:
    settings = build_settings(tmp_path).model_copy(
        update={
            "iron_agent_gateway_publish_host": "10.0.12.16",
            "iron_agent_gateway_access_host": "10.0.12.16",
        }
    )
    workspace_root = (settings.assistants_workspace_root / "demo-main").resolve()
    state_root = (settings.assistants_runtime_root / "demo-main" / "state").resolve()
    runner = SequenceRunner(
        [
            CommandResult(
                returncode=0,
                stdout=(
                    '["HOME=/home/node","TERM=xterm-256color",'
                    '"IRON_AGENT_ASSISTANT_ID=demo-main",'
                    '"IRON_AGENT_RUNTIME_CONTAINER_IMAGE=ironagent-runtime:test",'
                    '"IRON_AGENT_STATE_DIR=/home/node/.ironagent",'
                    '"IRON_AGENT_CONFIG_PATH=/home/node/.ironagent/ironagent.json",'
                    '"IRON_AGENT_WORKSPACE_DIR=/workspace",'
                    '"IRON_AGENT_BUNDLED_SKILLS_DIR=/workspace/skills",'
                    '"IRON_AGENT_WORKSPACE_SKILLS_ONLY=1",'
                    '"IRON_AGENT_WORKSPACE_PLUGINS_ONLY=1",'
                    '"IRON_AGENT_API_URL=http://127.0.0.1:8000",'
                    '"IRON_AGENT_HOOKS_ENABLED=true",'
                    '"IRON_AGENT_SERVICE_URL=http://host.docker.internal:8000",'
                    '"IRON_AGENT_FAIL_CLOSED=true",'
                    '"IRON_AGENT_HOOK_TIMEOUT_MS=5000",'
                    '"IRON_AGENT_APPROVAL_POLL_MS=1000",'
                    '"IRON_AGENT_APPROVAL_MAX_WAIT_MS=300000"]\n'
            ),
                ),
            CommandResult(
                returncode=0,
                stdout=(f'["{state_root}:/home/node/.ironagent","{workspace_root}:/workspace"]\n'),
            ),
            CommandResult(returncode=0, stdout="/workspace\n"),
            CommandResult(returncode=0, stdout='{"18789/tcp":[{"HostIp":"0.0.0.0"}]}\n'),
        ]
    )
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    should_recreate = provisioner._container_requires_recreate(
        assistant_id="demo-main",
        container_name="ironagent-assistant-demo-main",
        workspace_root=workspace_root,
        state_root=state_root,
    )

    assert should_recreate is True


def test_provisioner_recreates_container_when_working_dir_drifted(tmp_path) -> None:
    settings = build_settings(tmp_path)
    workspace_root = (settings.assistants_workspace_root / "demo-main").resolve()
    state_root = (settings.assistants_runtime_root / "demo-main" / "state").resolve()
    runner = SequenceRunner(
        [
            CommandResult(
                returncode=0,
                stdout=(
                    '["HOME=/home/node","TERM=xterm-256color",'
                    '"IRON_AGENT_ASSISTANT_ID=demo-main",'
                    '"IRON_AGENT_RUNTIME_CONTAINER_IMAGE=ironagent-runtime:test",'
                    '"IRON_AGENT_STATE_DIR=/home/node/.ironagent",'
                    '"IRON_AGENT_CONFIG_PATH=/home/node/.ironagent/ironagent.json",'
                    '"IRON_AGENT_WORKSPACE_DIR=/workspace",'
                    '"IRON_AGENT_BUNDLED_SKILLS_DIR=/workspace/skills",'
                    '"IRON_AGENT_WORKSPACE_SKILLS_ONLY=1",'
                    '"IRON_AGENT_WORKSPACE_PLUGINS_ONLY=1",'
                    '"IRON_AGENT_API_URL=http://127.0.0.1:8000",'
                    '"IRON_AGENT_HOOKS_ENABLED=true",'
                    '"IRON_AGENT_SERVICE_URL=http://host.docker.internal:8000",'
                    '"IRON_AGENT_FAIL_CLOSED=true",'
                    '"IRON_AGENT_HOOK_TIMEOUT_MS=5000",'
                    '"IRON_AGENT_APPROVAL_POLL_MS=1000",'
                    '"IRON_AGENT_APPROVAL_MAX_WAIT_MS=300000"]\n'
            ),
                ),
            CommandResult(
                returncode=0,
                stdout=(f'["{state_root}:/home/node/.ironagent","{workspace_root}:/workspace"]\n'),
            ),
            CommandResult(returncode=0, stdout="/app\n"),
        ]
    )
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    should_recreate = provisioner._container_requires_recreate(
        assistant_id="demo-main",
        container_name="ironagent-assistant-demo-main",
        workspace_root=workspace_root,
        state_root=state_root,
    )

    assert should_recreate is True


def test_provisioner_preserves_existing_config_while_updating_managed_fields(tmp_path) -> None:
    settings = build_settings(tmp_path)
    write_seed_skill(tmp_path / "ironagent-main", "office-docs")
    write_seed_skill(tmp_path / "ironagent-main", "local-pdf")
    workspace_root = (settings.assistants_workspace_root / "demo-main").resolve()
    state_root = (settings.assistants_runtime_root / "demo-main" / "state").resolve()
    runner = SequenceRunner(
        [
            CommandResult(returncode=0, stdout="container-999|running\n"),
            CommandResult(
                returncode=0,
                stdout=(
                    '["HOME=/home/node","TERM=xterm-256color",'
                    '"IRON_AGENT_ASSISTANT_ID=demo-main",'
                    '"IRON_AGENT_RUNTIME_CONTAINER_IMAGE=ironagent-runtime:test",'
                    '"IRON_AGENT_STATE_DIR=/home/node/.ironagent",'
                    '"IRON_AGENT_CONFIG_PATH=/home/node/.ironagent/ironagent.json",'
                    '"IRON_AGENT_WORKSPACE_DIR=/workspace",'
                    '"IRON_AGENT_BUNDLED_SKILLS_DIR=/workspace/skills",'
                    '"IRON_AGENT_WORKSPACE_SKILLS_ONLY=1",'
                    '"IRON_AGENT_WORKSPACE_PLUGINS_ONLY=1",'
                    '"IRON_AGENT_API_URL=http://127.0.0.1:8000",'
                    '"IRON_AGENT_HOOKS_ENABLED=true",'
                    '"IRON_AGENT_SERVICE_URL=http://host.docker.internal:8000",'
                    '"IRON_AGENT_FAIL_CLOSED=true",'
                    '"IRON_AGENT_HOOK_TIMEOUT_MS=5000",'
                    '"IRON_AGENT_APPROVAL_POLL_MS=1000",'
                    '"IRON_AGENT_APPROVAL_MAX_WAIT_MS=300000"]\n'
            ),
                ),
            CommandResult(
                returncode=0,
                stdout=(f'["{state_root}:/home/node/.ironagent","{workspace_root}:/workspace"]\n'),
            ),
            CommandResult(returncode=0, stdout="/workspace\n"),
            CommandResult(returncode=0, stdout='{"18789/tcp":[{"HostIp":""}]}\n'),
            CommandResult(
                returncode=0,
                stdout='["host.docker.internal:host-gateway"]\n',
            ),
            CommandResult(returncode=0, stdout="container-999\n"),
            CommandResult(returncode=0, stdout="127.0.0.1:49157\n"),
        ]
    )
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "ironagent.json").write_text(
        json.dumps(
            {
                "logging": {"level": "DEBUG"},
                "agents": {
                    "defaults": {
                        "model": {"primary": "old/model"},
                        "skillsFilter": "all skills",
                    }
                },
                "tools": {
                    "capability_profile": "dangerous",
                    "allow": ["read", "write", "exec"],
                    "exec": {"security": "full", "ask": "off"},
                },
            }
        ),
        encoding="utf-8",
    )
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    provisioner.ensure_runtime(
        assistant_id="demo-main",
        assistant_name="Demo Main",
        model_config=AssistantIronAgentConfig(
            primary_model="modelstudio/qwen3-coder-next",
            provider_id="modelstudio",
            provider_api_key="secret-key",
        ),
    )

    config = json.loads((state_root / "ironagent.json").read_text(encoding="utf-8"))
    assert config["logging"]["level"] == "DEBUG"
    assert config["agents"]["defaults"]["skillsFilter"] == "all skills"
    assert config["agents"]["defaults"]["workspace"] == "/workspace"
    assert config["agents"]["defaults"]["model"]["primary"] == "modelstudio/qwen3-coder-next"
    assert "capability_profile" not in config["tools"]
    assert "exec" not in config["tools"]
    assert runner.calls[6] == ["docker", "restart", "ironagent-assistant-demo-main"]


def test_provisioner_fails_closed_when_provider_api_key_missing(tmp_path) -> None:
    runner = SequenceRunner([])
    settings = build_settings(tmp_path)
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    result = provisioner.ensure_runtime(
        assistant_id="demo-main",
        assistant_name="Demo Main",
        model_config=AssistantIronAgentConfig(
            primary_model="modelstudio/qwen3-coder-next",
            auth_mode="provider_api_key",
            provider_id="modelstudio",
            provider_base_url="https://coding.dashscope.aliyuncs.com/v1",
            provider_api_key="",
        ),
    )

    assert result.status == "failed"
    assert "provider_api_key" in result.last_error
    # Refuse to write a half-formed config; no container command issued.
    assert not (
        settings.assistants_runtime_root / "demo-main" / "state" / "ironagent.json"
    ).exists()
    assert runner.calls == []


def test_provisioner_fails_closed_when_provider_id_unresolvable(tmp_path) -> None:
    runner = SequenceRunner([])
    settings = build_settings(tmp_path)
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    result = provisioner.ensure_runtime(
        assistant_id="demo-main",
        assistant_name="Demo Main",
        model_config=AssistantIronAgentConfig(
            primary_model="bare-model-no-slash",
            auth_mode="provider_api_key",
            provider_id="",
            provider_base_url="https://example.invalid/v1",
            provider_api_key="secret-key",
        ),
    )

    assert result.status == "failed"
    assert "provider id" in result.last_error
    assert runner.calls == []


def test_generate_then_recover_roundtrip_preserves_real_provider_key(tmp_path) -> None:
    """真实路线:provisioner 动态生成 ironagent.json,再经
    _model_config_from_runtime_state 恢复时,必须还原 headers.Authorization 里的
    真实 key,而不是 apiKey 字段的 "***" 占位符(占位符回流会污染存储配置)。"""
    settings = build_settings(tmp_path)
    provisioner = AssistantRuntimeProvisioner(settings=settings)
    state_root = settings.assistants_runtime_root / "demo" / "state"
    state_root.mkdir(parents=True)
    real_key = "sk-real-secret-12345"
    provisioner._write_iron_agent_config(
        assistant_id="demo",
        state_root=state_root,
        gateway_token="ironagent-local-demo",
        capability_profile="onlyread",
        model_config=AssistantIronAgentConfig(
            primary_model="dashscope-coding/qwen3-coder-next",
            auth_mode="provider_api_key",
            provider_id="dashscope-coding",
            provider_base_url="https://coding.dashscope.aliyuncs.com/v1",
            provider_api_key=real_key,
        ),
    )

    payload = json.loads((state_root / "ironagent.json").read_text(encoding="utf-8"))
    provider_block = payload["models"]["providers"]["dashscope-coding"]
    # 生成侧:apiKey 是占位符,真实 key 在 headers.Authorization
    assert provider_block["apiKey"] == "***"
    assert provider_block["headers"]["Authorization"] == real_key

    # 恢复侧:还原真实 key,而不是占位符
    service = AssistantService(
        store_path=tmp_path / "assistants.json",
        provisioner=provisioner,
    )
    recovered = service._model_config_from_runtime_state(payload)
    assert recovered.provider_api_key == real_key
    assert recovered.provider_id == "dashscope-coding"
    assert recovered.provider_base_url == "https://coding.dashscope.aliyuncs.com/v1"


def test_is_placeholder_api_key_detects_runtime_and_ui_placeholders() -> None:
    assert is_placeholder_api_key("***") is True
    assert is_placeholder_api_key("******") is True
    assert is_placeholder_api_key("  ***  ") is True
    assert is_placeholder_api_key("sk-real-secret") is False
    assert is_placeholder_api_key("") is False
    assert is_placeholder_api_key(None) is False


def test_provisioner_fails_closed_when_provider_api_key_is_placeholder(tmp_path) -> None:
    runner = SequenceRunner([])
    settings = build_settings(tmp_path)
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    result = provisioner.ensure_runtime(
        assistant_id="demo-main",
        assistant_name="Demo Main",
        model_config=AssistantIronAgentConfig(
            primary_model="dashscope-coding/qwen3-coder-next",
            auth_mode="provider_api_key",
            provider_id="dashscope-coding",
            provider_base_url="https://coding.dashscope.aliyuncs.com/v1",
            provider_api_key="***",  # 占位符,不是真实凭据
        ),
    )

    assert result.status == "failed"
    assert "placeholder" in result.last_error
    assert runner.calls == []


def test_provisioner_syncs_host_openai_codex_auth_without_creating_provider_override(
    tmp_path,
) -> None:
    write_seed_skill(tmp_path / "ironagent-main", "office-docs")
    write_seed_skill(tmp_path / "ironagent-main", "local-pdf")
    runner = SequenceRunner(
        [
            CommandResult(returncode=1, stderr="No such container"),
            CommandResult(returncode=0, stdout="container-234\n"),
            CommandResult(returncode=0, stdout="127.0.0.1:49158\n"),
        ]
    )
    settings = build_settings(tmp_path)
    host_root = settings.host_iron_agent_root
    host_agent_root = host_root / "agents" / settings.host_iron_agent_agent_id / "agent"
    host_agent_root.mkdir(parents=True, exist_ok=True)
    (host_root / "ironagent.json").write_text(
        json.dumps(
            {
                "auth": {
                    "profiles": {
                        "openai-codex:default": {
                            "provider": "openai-codex",
                            "mode": "oauth",
            }
                    }
                }
                        }
        ),
        encoding="utf-8",
    )
    (host_agent_root / "auth-profiles.json").write_text(
        json.dumps({"profiles": {"openai-codex:default": {"provider": "openai-codex"}}}),
        encoding="utf-8",
    )
    (host_agent_root / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "openai-codex": {
                        "baseUrl": "https://chatgpt.com/backend-api",
                        "api": "openai-codex-responses",
                }
            }
                    }
        ),
        encoding="utf-8",
    )
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    provisioner.ensure_runtime(
        assistant_id="demo-main",
        assistant_name="Demo Main",
        model_config=AssistantIronAgentConfig(primary_model="openai-codex/gpt-5.4"),
    )

    state_root = settings.assistants_runtime_root / "demo-main" / "state"
    config = json.loads((state_root / "ironagent.json").read_text(encoding="utf-8"))
    assert config["agents"]["defaults"]["model"]["primary"] == "openai-codex/gpt-5.4"
    assert config["auth"]["profiles"]["openai-codex:default"]["provider"] == "openai-codex"
    assert "capability_profile" not in config["tools"]
    assert "exec" not in config["tools"]
    assert "models" not in config or "providers" not in config.get("models", {})
    assert (
        state_root / "agents" / settings.host_iron_agent_agent_id / "agent" / "auth-profiles.json"
    ).exists()
    assert (
        state_root / "agents" / settings.host_iron_agent_agent_id / "agent" / "models.json"
    ).exists()


def test_host_openai_codex_auth_sync_preserves_newer_runtime_profile(tmp_path) -> None:
    settings = build_settings(tmp_path)
    host_agent_root = (
        settings.host_iron_agent_root / "agents" / settings.host_iron_agent_agent_id / "agent"
    )
    target_agent_root = (
        settings.assistants_runtime_root
        / "demo-main"
        / "state"
        / "agents"
        / settings.host_iron_agent_agent_id
        / "agent"
    )
    host_agent_root.mkdir(parents=True)
    target_agent_root.mkdir(parents=True)
    source = host_agent_root / "auth-profiles.json"
    target = target_agent_root / "auth-profiles.json"
    source.write_text(
        json.dumps({"profiles": {"openai-codex:default": {"refresh": "old"}}}),
        encoding="utf-8",
    )
    target.write_text(
        json.dumps({"profiles": {"openai-codex:default": {"refresh": "rotated"}}}),
        encoding="utf-8",
    )
    os.utime(source, (100, 100))
    os.utime(target, (200, 200))
    provisioner = AssistantRuntimeProvisioner(settings=settings)

    changed = provisioner._sync_host_iron_agent_auth(
        state_root=settings.assistants_runtime_root / "demo-main" / "state",
        model_config=AssistantIronAgentConfig(primary_model="openai-codex/gpt-5.4"),
    )

    assert changed is False
    assert json.loads(target.read_text(encoding="utf-8"))["profiles"]["openai-codex:default"][
        "refresh"
    ] == "rotated"


def test_provisioner_skips_host_auth_sync_when_assistant_uses_provider_api_mode(tmp_path) -> None:
    write_seed_skill(tmp_path / "ironagent-main", "office-docs")
    write_seed_skill(tmp_path / "ironagent-main", "local-pdf")
    runner = SequenceRunner(
        [
            CommandResult(returncode=1, stderr="No such container"),
            CommandResult(returncode=0, stdout="container-235\n"),
            CommandResult(returncode=0, stdout="127.0.0.1:49158\n"),
        ]
    )
    settings = build_settings(tmp_path)
    host_root = settings.host_iron_agent_root
    host_agent_root = host_root / "agents" / settings.host_iron_agent_agent_id / "agent"
    host_agent_root.mkdir(parents=True, exist_ok=True)
    (host_root / "ironagent.json").write_text(
        json.dumps(
            {
                "auth": {
                    "profiles": {
                        "openai-codex:default": {
                            "provider": "openai-codex",
                            "mode": "oauth",
                    }
            }
                }
                        }
        ),
        encoding="utf-8",
    )
    (host_agent_root / "auth-profiles.json").write_text(
        json.dumps({"profiles": {"openai-codex:default": {"provider": "openai-codex"}}}),
        encoding="utf-8",
    )
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    provisioner.ensure_runtime(
        assistant_id="demo-main",
        assistant_name="Demo Main",
        model_config=AssistantIronAgentConfig(
            primary_model="openai-codex/gpt-5.4",
            auth_mode="provider_api_key",
            provider_id="openai-compatible",
            provider_base_url="https://example.invalid/v1",
            provider_api_key="secret-key",
        ),
    )

    state_root = settings.assistants_runtime_root / "demo-main" / "state"
    config = json.loads((state_root / "ironagent.json").read_text(encoding="utf-8"))
    assert config["models"]["providers"]["openai-compatible"]["api"] == "openai-completions"
    assert "auth" not in config or "profiles" not in config.get("auth", {})
    assert config["models"]["providers"]["openai-compatible"]["apiKey"] == "***"
    assert config["models"]["providers"]["openai-compatible"]["authHeader"] is False
    assert config["models"]["providers"]["openai-compatible"]["headers"] == {
        "Authorization": "secret-key",
    }
    assert config["models"]["providers"]["openai-compatible"]["models"] == [
        {"id": "gpt-5.4", "name": "gpt-5.4"}
    ]
    auth_profiles = json.loads(
        (
            state_root / "agents" / "demo-main" / "agent" / "auth-profiles.json"
        ).read_text(encoding="utf-8")
    )
    assert auth_profiles["profiles"]["openai-compatible:default"] == {
        "type": "api_key",
        "provider": "openai-compatible",
        "key": "secret-key",
    }
    assert not (
        state_root / "agents" / settings.host_iron_agent_agent_id / "agent" / "auth-profiles.json"
    ).exists()


def test_provider_api_key_spdb_deepseek_keeps_raw_authorization_header(
    tmp_path,
) -> None:
    runner = SequenceRunner(
        [
            CommandResult(returncode=1, stderr="No such container"),
            CommandResult(returncode=0, stdout="container-spdb\n"),
            CommandResult(returncode=0, stdout="127.0.0.1:49160\n"),
        ]
    )
    settings = build_settings(tmp_path)
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    provisioner.ensure_runtime(
        assistant_id="demo-spdb",
        assistant_name="Demo SPDB",
        model_config=AssistantIronAgentConfig(
            primary_model="deepseek-v4-flash",
            auth_mode="provider_api_key",
            provider_id="spdb-deepseek",
            provider_base_url="http://msmpllm.spdb.com:8080/ltx026/v1",
            provider_api_key="drun-test-token",
        ),
    )

    state_root = settings.assistants_runtime_root / "demo-spdb" / "state"
    config = json.loads((state_root / "ironagent.json").read_text(encoding="utf-8"))
    assert config["agents"]["defaults"]["model"]["primary"] == (
        "spdb-deepseek/deepseek-v4-flash"
    )
    spdb_provider = config["models"]["providers"]["spdb-deepseek"]
    assert spdb_provider["api"] == "openai-completions"
    assert spdb_provider["baseUrl"] == "http://msmpllm.spdb.com:8080/ltx026/v1"
    assert spdb_provider["apiKey"] == "***"
    assert spdb_provider["authHeader"] is False
    assert spdb_provider["headers"] == {"Authorization": "drun-test-token"}
    assert spdb_provider["models"] == [
        {"id": "deepseek-v4-flash", "name": "deepseek-v4-flash"}
    ]


def test_provider_api_key_dashscope_keeps_raw_authorization_header(
    tmp_path,
) -> None:
    runner = SequenceRunner(
        [
            CommandResult(returncode=1, stderr="No such container"),
            CommandResult(returncode=0, stdout="container-dashscope\n"),
            CommandResult(returncode=0, stdout="127.0.0.1:49161\n"),
        ]
    )
    settings = build_settings(tmp_path)
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    provisioner.ensure_runtime(
        assistant_id="demo-dashscope",
        assistant_name="Demo DashScope",
        model_config=AssistantIronAgentConfig(
            primary_model="dashscope-coding/qwen3-coder-next",
            auth_mode="provider_api_key",
            provider_id="dashscope-coding",
            provider_base_url="https://coding.dashscope.aliyuncs.com/v1",
            provider_api_key="sk-test-token",
        ),
    )

    state_root = settings.assistants_runtime_root / "demo-dashscope" / "state"
    config = json.loads((state_root / "ironagent.json").read_text(encoding="utf-8"))
    dashscope_provider = config["models"]["providers"]["dashscope-coding"]
    assert dashscope_provider["api"] == "openai-completions"
    assert dashscope_provider["apiKey"] == "***"
    assert dashscope_provider["authHeader"] is False
    assert dashscope_provider["headers"] == {"Authorization": "sk-test-token"}


def test_provider_api_key_minimax_keeps_raw_authorization_header(
    tmp_path,
) -> None:
    runner = SequenceRunner(
        [
            CommandResult(returncode=1, stderr="No such container"),
            CommandResult(returncode=0, stdout="container-minimax\n"),
            CommandResult(returncode=0, stdout="127.0.0.1:49162\n"),
        ]
    )
    settings = build_settings(tmp_path)
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    provisioner.ensure_runtime(
        assistant_id="demo-minimax",
        assistant_name="Demo MiniMax",
        model_config=AssistantIronAgentConfig(
            primary_model="minimax-m2.7",
            auth_mode="provider_api_key",
            provider_id="minimax",
            provider_base_url="http://21.65.80.131:8012/v1",
            provider_api_key="sk-test-token",
        ),
    )

    state_root = settings.assistants_runtime_root / "demo-minimax" / "state"
    config = json.loads((state_root / "ironagent.json").read_text(encoding="utf-8"))
    assert config["agents"]["defaults"]["model"]["primary"] == "minimax/minimax-m2.7"
    minimax_provider = config["models"]["providers"]["minimax"]
    assert minimax_provider["api"] == "openai-completions"
    assert minimax_provider["baseUrl"] == "http://21.65.80.131:8012/v1"
    assert minimax_provider["apiKey"] == "***"
    assert minimax_provider["authHeader"] is False
    assert minimax_provider["headers"] == {"Authorization": "sk-test-token"}
    assert minimax_provider["models"] == [
        {"id": "minimax-m2.7", "name": "minimax-m2.7"}
    ]


def test_provisioner_writes_native_ollama_api_for_ollama_provider(tmp_path) -> None:
    runner = SequenceRunner(
        [
            CommandResult(returncode=1, stderr="No such container"),
            CommandResult(returncode=0, stdout="container-ollama\n"),
            CommandResult(returncode=0, stdout="127.0.0.1:49159\n"),
        ]
    )
    settings = build_settings(tmp_path)
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    provisioner.ensure_runtime(
        assistant_id="demo-ollama",
        assistant_name="Demo Ollama",
        model_config=AssistantIronAgentConfig(
            primary_model="ollama/qwen2.5:1.5b",
            provider_id="ollama",
            provider_base_url="http://host.docker.internal:11434",
            provider_api_key="ollama-local",
        ),
    )

    state_root = settings.assistants_runtime_root / "demo-ollama" / "state"
    config = json.loads((state_root / "ironagent.json").read_text(encoding="utf-8"))
    ollama_provider = config["models"]["providers"]["ollama"]
    assert ollama_provider["api"] == "ollama"
    assert ollama_provider["baseUrl"] == "http://host.docker.internal:11434"
    assert ollama_provider["apiKey"] == "ollama-local"


def test_provisioner_skips_restart_when_running_container_config_is_unchanged(tmp_path) -> None:
    settings = build_settings(tmp_path)
    workspace_root = (settings.assistants_workspace_root / "demo-main").resolve()
    state_root = (settings.assistants_runtime_root / "demo-main" / "state").resolve()
    runner = SequenceRunner(
        [
            CommandResult(returncode=0, stdout="container-abc|running\n"),
            CommandResult(
                returncode=0,
                stdout=(
                    '["HOME=/home/node","TERM=xterm-256color",'
                    '"IRON_AGENT_ASSISTANT_ID=demo-main",'
                    '"IRON_AGENT_RUNTIME_CONTAINER_IMAGE=ironagent-runtime:test",'
                    '"IRON_AGENT_STATE_DIR=/home/node/.ironagent",'
                    '"IRON_AGENT_CONFIG_PATH=/home/node/.ironagent/ironagent.json",'
                    '"IRON_AGENT_WORKSPACE_DIR=/workspace",'
                    '"IRON_AGENT_BUNDLED_SKILLS_DIR=/workspace/skills",'
                    '"IRON_AGENT_WORKSPACE_SKILLS_ONLY=1",'
                    '"IRON_AGENT_WORKSPACE_PLUGINS_ONLY=1",'
                    '"IRON_AGENT_API_URL=http://127.0.0.1:8000",'
                    '"IRON_AGENT_HOOKS_ENABLED=true",'
                    '"IRON_AGENT_SERVICE_URL=http://host.docker.internal:8000",'
                    '"IRON_AGENT_FAIL_CLOSED=true",'
                    '"IRON_AGENT_HOOK_TIMEOUT_MS=5000",'
                    '"IRON_AGENT_APPROVAL_POLL_MS=1000",'
                    '"IRON_AGENT_APPROVAL_MAX_WAIT_MS=300000"]\n'
            ),
                ),
            CommandResult(
                returncode=0,
                stdout=(f'["{state_root}:/home/node/.ironagent","{workspace_root}:/workspace"]\n'),
            ),
            CommandResult(returncode=0, stdout="/workspace\n"),
            CommandResult(returncode=0, stdout='{"18789/tcp":[{"HostIp":""}]}\n'),
            CommandResult(
                returncode=0,
                stdout='["host.docker.internal:host-gateway"]\n',
            ),
            CommandResult(returncode=0, stdout="127.0.0.1:49159\n"),
        ]
    )
    state_root.mkdir(parents=True, exist_ok=True)
    stable_config = {
        "gateway": {
            "auth": {
                "mode": "token",
                "token": "ironagent-local-demo-main",
            },
            "controlUi": {
                "allowedOrigins": [
                    "http://127.0.0.1:18789",
                    "http://localhost:18789",
                    "http://127.0.0.1:19001",
                    "http://localhost:19001",
                ],
                "dangerouslyAllowHostHeaderOriginFallback": True,
                "dangerouslyDisableDeviceAuth": True,
        },
            },
        "agents": {
            "defaults": {
                "workspace": "/workspace",
                "model": {"primary": "openai-codex/gpt-5.4"},
                "models": {"openai-codex/gpt-5.4": {}},
            },
            "list": [
                {
                    "id": "demo-main",
                    "name": "demo-main",
                    "default": True,
                    "workspace": "/workspace",
                }
            ],
        },
        "tools": {
            "allow": [
                "memory_get",
                "memory_search",
                "read",
                "session_status",
                "sessions_history",
                "sessions_list",
                "web_fetch",
                "web_search",
            ],
        },
        "auth": {
            "profiles": {
                "openai-codex:default": {
                    "provider": "openai-codex",
                    "mode": "oauth",
                }
            }
        },
    }
    (state_root / "ironagent.json").write_text(
        json.dumps(stable_config, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    host_agent_root = (
        settings.host_iron_agent_root / "agents" / settings.host_iron_agent_agent_id / "agent"
    )
    host_agent_root.mkdir(parents=True, exist_ok=True)
    (settings.host_iron_agent_root / "ironagent.json").write_text(
        json.dumps(
            {
                "auth": {
                    "profiles": {
                        "openai-codex:default": {
                            "provider": "openai-codex",
                            "mode": "oauth",
                }
            }
                    }
                        }
        ),
        encoding="utf-8",
    )
    target_agent_root = state_root / "agents" / settings.host_iron_agent_agent_id / "agent"
    target_agent_root.mkdir(parents=True, exist_ok=True)
    for filename, payload in (
        (
            "auth-profiles.json",
            {"profiles": {"openai-codex:default": {"provider": "openai-codex"}}},
        ),
        (
            "models.json",
            {"providers": {"openai-codex": {"baseUrl": "https://chatgpt.com/backend-api"}}},
        ),
    ):
        source = host_agent_root / filename
        source.write_text(json.dumps(payload), encoding="utf-8")
        (target_agent_root / filename).write_text(json.dumps(payload), encoding="utf-8")
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    result = provisioner.ensure_runtime(
        assistant_id="demo-main",
        assistant_name="Demo Main",
        model_config=AssistantIronAgentConfig(primary_model="openai-codex/gpt-5.4"),
    )

    assert result.gateway_port == 49159
    assert len(runner.calls) == 7
    assert runner.calls[6][0:2] == ["docker", "port"]


def test_docker_provisioner_writes_work_no_exec_without_exec_settings(tmp_path) -> None:
    runner = SequenceRunner(
        [
            CommandResult(returncode=1, stderr="No such container"),
            CommandResult(returncode=0, stdout="container-work\n"),
            CommandResult(returncode=0, stdout="127.0.0.1:49164\n"),
        ]
    )
    settings = build_settings(tmp_path)
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)
    provisioner._gateway_healthz_reachable = lambda _url: True

    result = provisioner.ensure_runtime(
        assistant_id="demo-work",
        assistant_name="Demo Work",
        capability_profile="work_no_exec",
    )

    state_root = settings.assistants_runtime_root / "demo-work" / "state"
    config = json.loads((state_root / "ironagent.json").read_text(encoding="utf-8"))
    allowed_tools = set(config["tools"]["allow"])
    assert result.status == "running"
    assert "capability_profile" not in config["tools"]
    assert {"read", "write", "edit", "apply_patch"} <= allowed_tools
    assert {
        "exec",
        "process",
        "code_execution",
        "cron",
        "sessions_send",
        "sessions_spawn",
        "sessions_history",
        "subagents",
    }.isdisjoint(allowed_tools)
    assert "exec" not in config["tools"]


def test_k8s_provisioner_writes_work_no_exec_without_exec_settings(tmp_path) -> None:
    runner = SequenceRunner(
        [
            CommandResult(returncode=0, stdout="pod deleted\n"),
            CommandResult(returncode=0, stdout="secret/runtime configured\npod/runtime created\n"),
            CommandResult(returncode=0, stdout=pod_payload()),
        ]
    )
    settings = build_settings(tmp_path).model_copy(
        update={
            "runtime_backend": "kubernetes",
            "k8s_runtime_namespace": "ironagent",
            "k8s_runtime_workspace_pvc": "assistant-workspaces",
            "k8s_runtime_workspace_subpath_prefix": "assistants",
        }
    )
    provisioner = K8sRuntimeProvisioner(settings=settings, command_runner=runner)
    provisioner._gateway_healthz_reachable = lambda _url: True

    result = provisioner.ensure_runtime(
        assistant_id="demo-work",
        assistant_name="Demo Work",
        capability_profile="work_no_exec",
    )

    state_root = settings.assistants_workspace_root / "demo-work" / ".ironagent"
    config = json.loads((state_root / "ironagent.json").read_text(encoding="utf-8"))
    allowed_tools = set(config["tools"]["allow"])
    assert result.status == "running"
    assert "capability_profile" not in config["tools"]
    assert {"read", "write", "edit", "apply_patch"} <= allowed_tools
    assert "exec" not in config["tools"]
    assert {
        "exec",
        "process",
        "code_execution",
        "cron",
        "sessions_send",
        "sessions_spawn",
        "sessions_history",
        "subagents",
    }.isdisjoint(allowed_tools)


def test_provisioner_recreates_existing_container_when_managed_env_drifted(tmp_path) -> None:
    runner = SequenceRunner(
        [
            CommandResult(returncode=0, stdout="container-654|running\n"),
            CommandResult(
                returncode=0,
                stdout=(
                    '["HOME=/home/node","TERM=xterm-256color",'
                    '"IRON_AGENT_ASSISTANT_ID=demo-main",'
                    '"IRON_AGENT_RUNTIME_CONTAINER_IMAGE=ironagent-runtime:test",'
                    '"IRON_AGENT_STATE_DIR=/home/node/.ironagent",'
                    '"IRON_AGENT_CONFIG_PATH=/home/node/.ironagent/ironagent.json",'
                    '"IRON_AGENT_WORKSPACE_DIR=/workspace",'
                    '"IRON_AGENT_BUNDLED_SKILLS_DIR=/workspace/skills",'
                    '"IRON_AGENT_WORKSPACE_SKILLS_ONLY=1",'
                    '"IRON_AGENT_WORKSPACE_PLUGINS_ONLY=1"]\n'
            ),
                ),
            CommandResult(returncode=0, stdout="container-654\n"),
            CommandResult(returncode=0, stdout="container-777\n"),
            CommandResult(returncode=0, stdout="127.0.0.1:49160\n"),
        ]
    )
    provisioner = AssistantRuntimeProvisioner(
        settings=build_settings(tmp_path),
        command_runner=runner,
    )
    provisioner._gateway_healthz_reachable = lambda _url: True

    result = provisioner.ensure_runtime(
        assistant_id="demo-main",
        assistant_name="Demo Main",
    )

    assert result.status == "running"
    assert result.container_id == "container-777"
    assert runner.calls[2] == ["docker", "rm", "-f", "ironagent-assistant-demo-main"]
    assert runner.calls[3][0:5] == ["docker", "run", "-d", "--init", "--name"]
    assert runner.calls[3][runner.calls[3].index("-w") + 1] == "/workspace"
    assert "--add-host" in runner.calls[3]
    assert "host.docker.internal:host-gateway" in runner.calls[3]


def test_provisioner_falls_back_to_docker_engine_api_when_cli_missing(
    tmp_path,
    monkeypatch,
) -> None:
    runner = SequenceRunner(
        [
            CommandResult(returncode=127, stderr="command not found: docker"),
            CommandResult(returncode=127, stderr="command not found: docker"),
            CommandResult(returncode=127, stderr="command not found: docker"),
        ]
    )
    settings = build_settings(tmp_path).model_copy(
        update={"docker_socket_path": "/tmp/docker.sock"}
    )
    Path(settings.docker_socket_path).write_text("", encoding="utf-8")
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    monkeypatch.setattr(provisioner, "_engine_inspect_container", lambda name: None)
    monkeypatch.setattr(
        provisioner,
        "_engine_create_container",
        lambda **kwargs: CommandResult(returncode=0, stdout="engine-container-1\n"),
    )
    monkeypatch.setattr(
        provisioner,
        "_engine_port_endpoint",
        lambda name: ("127.0.0.1", 49200),
    )
    monkeypatch.setattr(provisioner, "_gateway_healthz_reachable", lambda _url: True)

    result = provisioner.ensure_runtime(
        assistant_id="engine-main",
        assistant_name="Engine Main",
    )

    assert result.status == "running"
    assert result.container_id == "engine-container-1"
    assert result.gateway_url == "http://127.0.0.1:49200"


def test_engine_port_endpoint_prefers_public_control_ui_host_for_loopback_bindings(
    tmp_path,
) -> None:
    settings = build_settings(tmp_path).model_copy(
        update={"control_ui_public_url": "http://192.0.2.11:30789"}
    )
    provisioner = AssistantRuntimeProvisioner(settings=settings)
    provisioner._engine_inspect_container = lambda _name: {
        "NetworkSettings": {
            "Ports": {
                "18789/tcp": [
                    {
                        "HostIp": "127.0.0.1",
                        "HostPort": "49160",
                    }
                ]
        }
    }
            }

    assert provisioner._engine_port_endpoint("assistant-demo") == ("192.0.2.11", 49160)


def test_engine_request_returns_error_payload_when_socket_connect_resets(
    tmp_path,
    monkeypatch,
) -> None:
    settings = build_settings(tmp_path).model_copy(
        update={"docker_socket_path": str(tmp_path / "docker.sock")}
    )
    Path(settings.docker_socket_path).write_text("", encoding="utf-8")
    provisioner = AssistantRuntimeProvisioner(settings=settings)

    class ResettingSocket:
        def connect(self, _path: str) -> None:
            raise ConnectionResetError("socket reset by peer")

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "src.runtime.provisioner.socket.socket",
        lambda *_args, **_kwargs: ResettingSocket(),
    )

    status_code, raw = provisioner._engine_request("GET", "/containers/demo/json")

    assert status_code == 599
    assert "ConnectionResetError" in raw
    assert "socket reset by peer" in raw


def test_provisioner_destroy_runtime_removes_managed_paths(tmp_path) -> None:
    settings = build_settings(tmp_path)
    workspace_root = (settings.assistants_workspace_root / "demo-main").resolve()
    state_root = (settings.assistants_runtime_root / "demo-main" / "state").resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    state_root.mkdir(parents=True, exist_ok=True)
    (workspace_root / "persistent").mkdir(parents=True, exist_ok=True)
    (workspace_root / "persistent" / "note.txt").write_text("keep", encoding="utf-8")
    (state_root / "ironagent.json").write_text("{}", encoding="utf-8")
    runner = SequenceRunner([CommandResult(returncode=0, stdout="demo-main\n")])
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    result = provisioner.destroy_runtime(
        assistant_id="demo-main",
        workspace_root=str(workspace_root),
        state_root=str(state_root),
    )

    assert result.container_name == "ironagent-assistant-demo-main"
    assert result.container_removed is True
    assert result.workspace_removed is True
    assert result.state_removed is True
    assert not workspace_root.exists()
    assert not state_root.parent.exists()
    assert runner.calls == [["docker", "rm", "-f", "ironagent-assistant-demo-main"]]


def test_provisioner_falls_back_to_existing_legacy_container_name(tmp_path) -> None:
    settings = build_settings(tmp_path).model_copy(
        update={"assistant_container_name_prefix": "ironagent-assistant"}
    )
    provisioner = AssistantRuntimeProvisioner(settings=settings)
    inspected_names: list[str] = []

    def inspect_container(container_name: str) -> tuple[str, str] | None:
        inspected_names.append(container_name)
        if container_name == "ironagent-assistant-demo-main":
            return ("legacy-container-1", "running")
        return None

    provisioner._inspect_container = inspect_container  # type: ignore[method-assign]

    container_name, inspected = provisioner._inspect_existing_container("demo-main")

    assert container_name == "ironagent-assistant-demo-main"
    assert inspected == ("legacy-container-1", "running")
    assert inspected_names == [
        "ironagent-assistant-demo-main",
    ]


def test_provisioner_reload_workspace_capability_touches_new_and_legacy_markers(
    tmp_path,
) -> None:
    runner = SequenceRunner([CommandResult(returncode=0)])
    provisioner = AssistantRuntimeProvisioner(
        settings=build_settings(tmp_path),
        command_runner=runner,
    )

    assert (
        provisioner.reload_workspace_capability(
            assistant_id="demo-main",
            kind="skills",
            slug="spreadsheet-exporter",
        )
        is True
    )
    assert runner.calls == [
        [
            "docker",
            "exec",
            "ironagent-assistant-demo-main",
            "touch",
            "/workspace/.ironagent-capabilities-reload",
        ]
    ]


def test_provisioner_reload_workspace_capability_uses_engine_api_without_docker_cli(
    tmp_path,
) -> None:
    socket_path = tmp_path / "docker.sock"
    socket_path.touch()
    runner = SequenceRunner([CommandResult(returncode=127, stderr="command not found: docker")])
    provisioner = AssistantRuntimeProvisioner(
        settings=build_settings(tmp_path).model_copy(update={"docker_socket_path": socket_path}),
        command_runner=runner,
    )
    engine_calls: list[tuple[str, list[str]]] = []

    def fake_engine_exec(container_name: str, cmd: list[str]) -> CommandResult:
        engine_calls.append((container_name, cmd))
        return CommandResult(returncode=0)

    provisioner._engine_exec_container = fake_engine_exec  # type: ignore[method-assign]

    assert (
        provisioner.reload_workspace_capability(
            assistant_id="demo-main",
            kind="skills",
            slug="spreadsheet-exporter",
        )
        is True
    )
    assert engine_calls == [
        (
            "ironagent-assistant-demo-main",
            ["touch", "/workspace/.ironagent-capabilities-reload"],
        )
    ]


def test_provisioner_destroy_runtime_refuses_unmanaged_workspace_path(tmp_path) -> None:
    settings = build_settings(tmp_path)
    unmanaged_root = (tmp_path / "outside" / "demo-main").resolve()
    unmanaged_root.mkdir(parents=True, exist_ok=True)
    runner = SequenceRunner([CommandResult(returncode=1, stderr="No such container: demo-main")])
    provisioner = AssistantRuntimeProvisioner(settings=settings, command_runner=runner)

    try:
        provisioner.destroy_runtime(
            assistant_id="demo-main",
            workspace_root=str(unmanaged_root),
            state_root=None,
        )
    except ValueError as exc:
        assert "refusing to remove unmanaged path" in str(exc)
    else:
        raise AssertionError("expected unmanaged workspace deletion to fail")

    assert unmanaged_root.exists()


def test_start_runtime_waits_past_transient_unhealthy_probe(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.runtime.provisioner.time.sleep", lambda _seconds: None)
    provisioner = ProbeSequenceProvisioner(
        settings=build_settings(tmp_path),
        inspect_result=("container-123", "paused"),
        probe_results=[
            RuntimeProbeResult(
                container_name="ironagent-assistant-demo-main",
                container_exists=True,
                container_id="container-123",
                container_state="running",
                health_state="unhealthy",
                status="failed",
                last_error="container health is unhealthy",
            ),
            RuntimeProbeResult(
                container_name="ironagent-assistant-demo-main",
                container_exists=True,
                container_id="container-123",
                container_state="running",
                health_state="starting",
                status="starting",
            ),
            RuntimeProbeResult(
                container_name="ironagent-assistant-demo-main",
                container_exists=True,
                container_id="container-123",
                container_state="running",
                health_state="healthy",
                status="running",
                gateway_host="127.0.0.1",
                gateway_port=49190,
                gateway_url="http://127.0.0.1:49190",
            ),
        ],
    )

    result = provisioner.start_runtime(assistant_id="demo-main")

    assert result.status == "running"
    assert result.gateway_url == "http://127.0.0.1:49190"
    assert provisioner.unpause_calls == ["ironagent-assistant-demo-main"]
    assert provisioner.start_calls == []


def test_wait_for_runtime_probe_returns_latest_probe_after_transient_timeout(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("src.runtime.provisioner.time.sleep", lambda _seconds: None)
    provisioner = ProbeSequenceProvisioner(
        settings=build_settings(tmp_path),
        inspect_result=("container-123", "running"),
        probe_results=[
            RuntimeProbeResult(
                container_name="ironagent-assistant-demo-main",
                container_exists=True,
                container_id="container-123",
                container_state="running",
                health_state="starting",
                status="starting",
                last_error="gateway healthz is not reachable",
            ),
        ],
    )

    result = provisioner._wait_for_runtime_probe(
        assistant_id="demo-main",
        transient_statuses={"starting"},
        attempts=2,
        interval_seconds=0,
    )

    assert result.status == "starting"
    assert result.container_name == "ironagent-assistant-demo-main"


def test_gateway_health_probe_falls_back_to_legacy_health_endpoint(
    tmp_path,
    monkeypatch,
) -> None:
    statuses = [404, 404, 200]
    requests: list[tuple[str, str]] = []

    class FakeResponse:
        def __init__(self, status: int) -> None:
            self.status = status

        def read(self) -> bytes:
            return b""

    class FakeConnection:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            assert host == "127.0.0.1"
            assert port == 49190
            assert timeout == 1.0

        def request(self, method: str, path: str) -> None:
            requests.append((method, path))

        def getresponse(self) -> FakeResponse:
            return FakeResponse(statuses.pop(0))

        def close(self) -> None:
            pass

    provisioner = AssistantRuntimeProvisioner(
        settings=build_settings(tmp_path),
        command_runner=lambda _args: CommandResult(returncode=0),
    )
    monkeypatch.setattr("src.runtime.provisioner.HTTPConnection", FakeConnection)

    assert provisioner._gateway_healthz_reachable("http://127.0.0.1:49190")
    assert requests == [
        ("GET", "/healthz"),
        ("GET", "/api/v1/health"),
        ("GET", "/health"),
    ]


def test_gateway_health_probe_does_not_fallback_when_healthz_is_unhealthy(
    tmp_path,
    monkeypatch,
) -> None:
    requests: list[tuple[str, str]] = []

    class FakeResponse:
        status = 503

        def read(self) -> bytes:
            return b""

    class FakeConnection:
        def __init__(self, _host: str, _port: int, timeout: float) -> None:
            pass

        def request(self, method: str, path: str) -> None:
            requests.append((method, path))

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            pass

    provisioner = AssistantRuntimeProvisioner(
        settings=build_settings(tmp_path),
        command_runner=lambda _args: CommandResult(returncode=0),
    )
    monkeypatch.setattr("src.runtime.provisioner.HTTPConnection", FakeConnection)

    assert not provisioner._gateway_healthz_reachable("http://127.0.0.1:49190")
    assert requests == [("GET", "/healthz")]


def test_probe_runtime_treats_starting_health_as_running_when_gateway_healthz_ready(
    tmp_path,
    monkeypatch,
) -> None:
    provisioner = AssistantRuntimeProvisioner(
        settings=build_settings(tmp_path),
        command_runner=lambda _args: CommandResult(returncode=0),
    )
    monkeypatch.setattr(
        provisioner,
        "_inspect_container_with_health",
        lambda _name: ("container-123", "running", "starting"),
    )
    monkeypatch.setattr(
        provisioner,
        "_resolve_gateway_endpoint",
        lambda _name: ("127.0.0.1", 49190, "http://127.0.0.1:49190"),
    )
    monkeypatch.setattr(
        provisioner,
        "_gateway_healthz_reachable",
        lambda url: url == "http://127.0.0.1:49190",
    )

    result = provisioner.probe_runtime(assistant_id="demo-main")

    assert result.status == "running"
    assert result.health_state == "starting"
    assert result.gateway_url == "http://127.0.0.1:49190"


def test_probe_runtime_keeps_running_container_starting_until_gateway_healthz_ready(
    tmp_path,
    monkeypatch,
) -> None:
    provisioner = AssistantRuntimeProvisioner(
        settings=build_settings(tmp_path),
        command_runner=lambda _args: CommandResult(returncode=0),
    )
    monkeypatch.setattr(
        provisioner,
        "_inspect_container_with_health",
        lambda _name: ("container-123", "running", None),
    )
    monkeypatch.setattr(
        provisioner,
        "_resolve_gateway_endpoint",
        lambda _name: ("127.0.0.1", 49190, "http://127.0.0.1:49190"),
    )
    monkeypatch.setattr(provisioner, "_gateway_healthz_reachable", lambda _url: False)

    result = provisioner.probe_runtime(assistant_id="demo-main")

    assert result.status == "starting"
    assert result.gateway_url == "http://127.0.0.1:49190"
    assert result.last_error == "gateway healthz is not reachable"


def test_probe_runtime_reports_recent_openai_codex_auth_failure(
    tmp_path,
    monkeypatch,
) -> None:
    provisioner = AssistantRuntimeProvisioner(
        settings=build_settings(tmp_path),
        command_runner=lambda _args: CommandResult(returncode=0),
    )
    monkeypatch.setattr(
        provisioner,
        "_inspect_container_with_health",
        lambda _name: ("container-123", "running", None),
    )
    monkeypatch.setattr(
        provisioner,
        "_resolve_gateway_endpoint",
        lambda _name: ("127.0.0.1", 49190, "http://127.0.0.1:49190"),
    )
    monkeypatch.setattr(provisioner, "_gateway_healthz_reachable", lambda _url: True)
    monkeypatch.setattr(
        provisioner,
        "_recent_runtime_auth_error",
        lambda _name: "OpenAI Codex OAuth token refresh failed",
    )

    result = provisioner.probe_runtime(assistant_id="demo-main")

    assert result.status == "running"
    assert result.last_error == "OpenAI Codex OAuth token refresh failed"


def test_recent_runtime_auth_error_detects_refresh_token_reused_log(tmp_path) -> None:
    runner = SequenceRunner(
        [
            CommandResult(
                returncode=0,
                stderr="Token refresh failed: 401 code=refresh_token_reused",
            )
        ]
    )
    provisioner = AssistantRuntimeProvisioner(
        settings=build_settings(tmp_path),
        command_runner=runner,
    )

    message = provisioner._recent_runtime_auth_error("ironagent-assistant-demo-main")

    assert "OpenAI Codex OAuth token refresh failed" in message
    assert runner.calls == [
        ["docker", "logs", "--tail", "120", "ironagent-assistant-demo-main"]
    ]


def test_start_runtime_restarts_paused_container_when_health_stays_unhealthy(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("src.runtime.provisioner.time.sleep", lambda _seconds: None)
    unhealthy_probe = RuntimeProbeResult(
        container_name="ironagent-assistant-demo-main",
        container_exists=True,
        container_id="container-123",
        container_state="running",
        health_state="unhealthy",
        status="failed",
        last_error="container health is unhealthy",
    )
    provisioner = ProbeSequenceProvisioner(
        settings=build_settings(tmp_path),
        inspect_result=("container-123", "paused"),
        probe_results=[
            unhealthy_probe,
            RuntimeProbeResult(
                container_name="ironagent-assistant-demo-main",
                container_exists=True,
                container_id="container-123",
                container_state="running",
                health_state="healthy",
                status="running",
                gateway_host="127.0.0.1",
                gateway_port=49191,
                gateway_url="http://127.0.0.1:49191",
            ),
        ],
    )

    result = provisioner.start_runtime(assistant_id="demo-main")

    assert result.status == "running"
    assert result.gateway_url == "http://127.0.0.1:49191"
    assert provisioner.unpause_calls == ["ironagent-assistant-demo-main"]
    assert provisioner.restart_calls == []


def test_provisioner_logs_permission_fix_failures(tmp_path, monkeypatch) -> None:
    provisioner = AssistantRuntimeProvisioner(settings=build_settings(tmp_path))
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    child = root / "child.txt"
    child.write_text("content", encoding="utf-8")

    warnings: list[dict[str, object]] = []

    class FakeLogger:
        def warning(self, event: str, **kwargs: object) -> None:
            warnings.append({"event": event, **kwargs})

    def fail_chown(path: Path, uid: int, gid: int) -> None:
        raise OSError("chown denied")

    def fail_chmod(path: Path, mode: int) -> None:
        raise OSError("chmod denied")

    monkeypatch.setattr("src.runtime.provisioner.logger", FakeLogger())
    monkeypatch.setattr("src.runtime.provisioner.os.name", "posix")
    monkeypatch.setattr("src.runtime.provisioner.os.chown", fail_chown)
    monkeypatch.setattr("src.runtime.provisioner.os.chmod", fail_chmod)

    provisioner._ensure_runtime_mount_permissions(root)

    assert any(item["event"] == "chown_skipped" for item in warnings)
    assert any(item["event"] == "chmod_skipped" for item in warnings)
