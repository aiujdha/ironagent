from __future__ import annotations

import json
from hashlib import sha1
from pathlib import Path
from typing import Any

import yaml

from src.shared.config import Settings, get_settings
from src.shared.logging import get_logger
from src.shared.workspace_roots import normalize_workspace_slug

from .models import AssistantIronAgentConfig, AssistantWorkspaceConfig, RuntimeStatusName
from .provisioner import (
    AssistantRuntimeProvisioner,
    CommandResult,
    CommandRunner,
    DestroyRuntimeResult,
    ProvisionResult,
    RuntimeProbeResult,
    utc_now,
)

log = get_logger(__name__)


class _QuotedString(str):
    pass


class _K8sManifestDumper(yaml.SafeDumper):
    pass


def _quoted_string_representer(
    dumper: yaml.SafeDumper,
    data: _QuotedString,
) -> yaml.nodes.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style='"')


_K8sManifestDumper.add_representer(_QuotedString, _quoted_string_representer)


def _quote_manifest_strings(value: Any) -> Any:
    if isinstance(value, str):
        return _QuotedString(value)
    if isinstance(value, list):
        return [_quote_manifest_strings(item) for item in value]
    if isinstance(value, dict):
        return {key: _quote_manifest_strings(item) for key, item in value.items()}
    return value


class K8sRuntimeProvisioner(AssistantRuntimeProvisioner):
    """Provision one assistant runtime as one Kubernetes Pod with PVC subPath mounts."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        command_runner: CommandRunner | None = None,
    ) -> None:
        super().__init__(settings=settings or get_settings(), command_runner=command_runner)

    def ensure_runtime(
        self,
        *,
        assistant_id: str,
        assistant_name: str,
        capability_profile: str = "onlyread",
        model_config: AssistantIronAgentConfig | None = None,
        workspace_config: AssistantWorkspaceConfig | None = None,
    ) -> ProvisionResult:
        log.info(
            "K8s助手运行时创建开始",
            assistant_id=assistant_id,
            assistant_name=assistant_name,
            namespace=self._namespace(),
            image=self.settings.iron_agent_container_image,
            workspace_pvc=self.settings.k8s_runtime_workspace_pvc,
        )
        workspace_layout = self._ensure_workspace(
            assistant_id=assistant_id,
            assistant_name=assistant_name,
            workspace_config=workspace_config,
        )
        workspace_root = workspace_layout.workspace_root
        state_root = self._ensure_k8s_state_root(workspace_root)
        gateway_token = self._gateway_token_for(assistant_id)
        try:
            self._write_iron_agent_config(
                assistant_id=assistant_id,
                state_root=state_root,
                gateway_token=gateway_token,
                capability_profile=capability_profile,
                model_config=model_config,
            )
        except ValueError as exc:
            log.warning(
                "K8s助手运行时配置写入失败",
                assistant_id=assistant_id,
                pod_name=self._pod_name_for(assistant_id),
                workspace_slug=workspace_layout.workspace_slug,
                error=str(exc),
            )
            return ProvisionResult(
                container_name=self._pod_name_for(assistant_id),
                container_id=None,
                workspace_root=str(workspace_root),
                workspace_root_id=workspace_layout.root_id,
                workspace_slug=workspace_layout.workspace_slug,
                temp_dir=workspace_layout.temp_dir,
                persistent_dir=workspace_layout.persistent_dir,
                state_root=str(state_root),
                status="failed",
                image=self.settings.iron_agent_container_image,
                gateway_token=gateway_token,
                last_error=str(exc),
                updated_at=utc_now(),
            )
        if workspace_layout.permissions_needed:
            self._ensure_runtime_mount_permissions(workspace_root)
        self._ensure_runtime_mount_permissions(state_root)

        manifest_path = self._write_runtime_manifest(
            assistant_id=assistant_id,
            workspace_slug=workspace_layout.workspace_slug,
            state_root=state_root,
        )
        deleted = self._delete_runtime_pod(assistant_id, wait=True)
        if deleted.returncode != 0:
            log.warning(
                "K8s助手运行时替换失败",
                assistant_id=assistant_id,
                pod_name=self._pod_name_for(assistant_id),
                workspace_root=str(workspace_root),
                workspace_slug=workspace_layout.workspace_slug,
                error=self._error_text(deleted, "failed to replace runtime pod"),
            )
            return ProvisionResult(
                container_name=self._pod_name_for(assistant_id),
                container_id=None,
                workspace_root=str(workspace_root),
                workspace_root_id=workspace_layout.root_id,
                workspace_slug=workspace_layout.workspace_slug,
                temp_dir=workspace_layout.temp_dir,
                persistent_dir=workspace_layout.persistent_dir,
                state_root=str(state_root),
                status="failed",
                image=self.settings.iron_agent_container_image,
                gateway_token=gateway_token,
                last_error=self._error_text(deleted, "failed to replace runtime pod"),
                updated_at=utc_now(),
            )
        applied = self.command_runner(
            [
                self.settings.k8s_runtime_kubectl_binary,
                "-n",
                self._namespace(),
                "apply",
                "-f",
                str(manifest_path),
            ]
        )
        if applied.returncode != 0:
            log.warning(
                "K8s助手运行时清单应用失败",
                assistant_id=assistant_id,
                pod_name=self._pod_name_for(assistant_id),
                manifest_path=str(manifest_path),
                workspace_root=str(workspace_root),
                workspace_slug=workspace_layout.workspace_slug,
                error=self._error_text(applied, "failed to apply runtime pod manifest"),
            )
            return ProvisionResult(
                container_name=self._pod_name_for(assistant_id),
                container_id=None,
                workspace_root=str(workspace_root),
                workspace_root_id=workspace_layout.root_id,
                workspace_slug=workspace_layout.workspace_slug,
                temp_dir=workspace_layout.temp_dir,
                persistent_dir=workspace_layout.persistent_dir,
                state_root=str(state_root),
                status="failed",
                image=self.settings.iron_agent_container_image,
                gateway_token=gateway_token,
                last_error=self._error_text(applied, "failed to apply runtime pod manifest"),
                updated_at=utc_now(),
            )

        probe = self._wait_for_runtime_probe(
            assistant_id=assistant_id,
            transient_statuses={"starting"},
            attempts=60,
            interval_seconds=0.5,
        )
        status = probe.status if probe.container_exists else "starting"
        log.info(
            "K8s助手运行时创建完成",
            assistant_id=assistant_id,
            pod_name=probe.container_name,
            pod_uid=probe.container_id,
            workspace_root=str(workspace_root),
            workspace_slug=workspace_layout.workspace_slug,
            temp_dir=workspace_layout.temp_dir,
            persistent_dir=workspace_layout.persistent_dir,
            status=status,
            health_state=probe.health_state,
            gateway_url=probe.gateway_url,
            last_error=probe.last_error,
        )
        return ProvisionResult(
            container_name=probe.container_name,
            container_id=probe.container_id,
            workspace_root=str(workspace_root),
            workspace_root_id=workspace_layout.root_id,
            workspace_slug=workspace_layout.workspace_slug,
            temp_dir=workspace_layout.temp_dir,
            persistent_dir=workspace_layout.persistent_dir,
            state_root=str(state_root),
            status=status,
            image=self.settings.iron_agent_container_image,
            gateway_host=probe.gateway_host,
            gateway_port=probe.gateway_port,
            gateway_url=probe.gateway_url,
            gateway_token=gateway_token,
            last_error=probe.last_error,
            updated_at=utc_now(),
        )

    def destroy_runtime(
        self,
        *,
        assistant_id: str,
        workspace_root: str | None = None,
        state_root: str | None = None,
    ) -> DestroyRuntimeResult:
        log.info(
            "K8s助手运行时销毁开始",
            assistant_id=assistant_id,
            pod_name=self._pod_name_for(assistant_id),
            workspace_root=workspace_root,
            delete_workspace=self.settings.k8s_runtime_delete_workspace_on_destroy,
        )
        manifest_path = self._manifest_path(assistant_id)
        if manifest_path.exists():
            removed = self.command_runner(
                [
                    self.settings.k8s_runtime_kubectl_binary,
                    "-n",
                    self._namespace(),
                    "delete",
                    "-f",
                    str(manifest_path),
                    "--ignore-not-found=true",
                    "--wait=false",
                ]
            )
        else:
            pod_names = self._pod_name_candidates_for(assistant_id)
            secret_names = self._secret_name_candidates_for(assistant_id)
            service_names = self._service_name_candidates_for(assistant_id)
            removed = self.command_runner(
                [
                    self.settings.k8s_runtime_kubectl_binary,
                    "-n",
                    self._namespace(),
                    "delete",
                    *(f"pod/{pod_name}" for pod_name in pod_names),
                    *(f"secret/{secret_name}" for secret_name in secret_names),
                    *(f"service/{service_name}" for service_name in service_names),
                    "--ignore-not-found=true",
                    "--wait=false",
                ]
            )
        if removed.returncode != 0:
            log.warning(
                "K8s助手运行时销毁失败",
                assistant_id=assistant_id,
                pod_name=self._pod_name_for(assistant_id),
                error=self._error_text(removed, "failed to delete runtime pod"),
            )
            raise ValueError(self._error_text(removed, "failed to delete runtime pod"))

        workspace_removed = False
        if self.settings.k8s_runtime_delete_workspace_on_destroy:
            workspace_removed = self._remove_workspace_root(workspace_root)
        # In the Kubernetes backend the state directory is stored inside the
        # assistant workspace subPath, so workspace cleanup covers it.
        state_removed = workspace_removed if state_root else False
        log.info(
            "K8s助手运行时销毁完成",
            assistant_id=assistant_id,
            pod_name=self._pod_name_for(assistant_id),
            workspace_removed=workspace_removed,
            state_removed=state_removed,
        )
        return DestroyRuntimeResult(
            container_name=self._pod_name_for(assistant_id),
            container_removed=True,
            workspace_removed=workspace_removed,
            state_removed=state_removed,
        )

    def restart_runtime(self, *, assistant_id: str) -> RuntimeProbeResult:
        pod_name = self._pod_name_for(assistant_id)
        log.info("K8s助手运行时重启开始", assistant_id=assistant_id, pod_name=pod_name)
        deleted = self._delete_runtime_pod(assistant_id, wait=True)
        if deleted.returncode != 0:
            log.warning(
                "K8s助手运行时重启失败",
                assistant_id=assistant_id,
                pod_name=pod_name,
                error=self._error_text(deleted, "failed to delete runtime pod"),
            )
            return RuntimeProbeResult(
                container_name=pod_name,
                container_exists=True,
                container_state="unknown",
                status="failed",
                last_error=self._error_text(deleted, "failed to delete runtime pod"),
            )
        return RuntimeProbeResult(
            container_name=pod_name,
            container_exists=False,
            container_state="missing",
            status="failed",
            last_error="runtime pod deleted; reprovision required",
        )

    def start_runtime(self, *, assistant_id: str) -> RuntimeProbeResult:
        manifest_path = self._manifest_path(assistant_id)
        log.info(
            "K8s助手运行时启动开始",
            assistant_id=assistant_id,
            pod_name=self._pod_name_for(assistant_id),
            manifest_path=str(manifest_path),
        )
        if not manifest_path.exists():
            log.warning(
                "K8s助手运行时启动失败",
                assistant_id=assistant_id,
                pod_name=self._pod_name_for(assistant_id),
                manifest_path=str(manifest_path),
                reason="manifest_not_found",
            )
            return RuntimeProbeResult(
                container_name=self._pod_name_for(assistant_id),
                container_exists=False,
                container_state="missing",
                status="failed",
                last_error="runtime manifest not found",
            )
        applied = self.command_runner(
            [
                self.settings.k8s_runtime_kubectl_binary,
                "-n",
                self._namespace(),
                "apply",
                "-f",
                str(manifest_path),
            ]
        )
        if applied.returncode != 0:
            log.warning(
                "K8s助手运行时启动失败",
                assistant_id=assistant_id,
                pod_name=self._pod_name_for(assistant_id),
                manifest_path=str(manifest_path),
                error=self._error_text(applied, "failed to apply runtime pod manifest"),
            )
            return RuntimeProbeResult(
                container_name=self._pod_name_for(assistant_id),
                container_exists=False,
                container_state="missing",
                status="failed",
                last_error=self._error_text(applied, "failed to apply runtime pod manifest"),
            )
        probe = self._wait_for_runtime_probe(
            assistant_id=assistant_id,
            transient_statuses={"starting"},
            attempts=60,
            interval_seconds=0.5,
        )
        log.info(
            "K8s助手运行时启动完成",
            assistant_id=assistant_id,
            pod_name=probe.container_name,
            status=probe.status,
            health_state=probe.health_state,
            gateway_url=probe.gateway_url,
            last_error=probe.last_error,
        )
        return probe

    def stop_runtime(self, *, assistant_id: str) -> RuntimeProbeResult:
        pod_name = self._operation_pod_name_for(assistant_id)
        log.info("K8s助手运行时停止开始", assistant_id=assistant_id, pod_name=pod_name)
        stopped = self.command_runner(
            [
                self.settings.k8s_runtime_kubectl_binary,
                "-n",
                self._namespace(),
                "delete",
                "pod",
                pod_name,
                "--ignore-not-found=true",
            ]
        )
        if stopped.returncode != 0:
            log.warning(
                "K8s助手运行时停止失败",
                assistant_id=assistant_id,
                pod_name=pod_name,
                error=self._error_text(stopped, "failed to stop runtime pod"),
            )
            return RuntimeProbeResult(
                container_name=pod_name,
                container_exists=True,
                container_state="unknown",
                status="failed",
                last_error=self._error_text(stopped, "failed to stop runtime pod"),
            )
        return RuntimeProbeResult(
            container_name=pod_name,
            container_exists=False,
            container_state="missing",
            status="stopped",
        )

    def pause_runtime(self, *, assistant_id: str) -> RuntimeProbeResult:
        return RuntimeProbeResult(
            container_name=self._pod_name_for(assistant_id),
            container_exists=True,
            container_state="running",
            status="failed",
            last_error="pause is unsupported for kubernetes runtime",
        )

    def reload_workspace_capability(
        self,
        *,
        assistant_id: str,
        kind: str,
        slug: str,
        container_name: str | None = None,
    ) -> bool:
        if kind not in {"skills", "plugins"}:
            return False
        normalized_slug = normalize_workspace_slug(slug, fallback="").strip()
        if not normalized_slug:
            return False
        pod_name = container_name or self._operation_pod_name_for(assistant_id)
        reload_marker_path = (
            f"{self.settings.iron_agent_workspace_mount_path}/.ironagent-capabilities-reload"
        )
        result = self.command_runner(
            [
                self.settings.k8s_runtime_kubectl_binary,
                "-n",
                self._namespace(),
                "exec",
                pod_name,
                "--",
                "touch",
                reload_marker_path,
            ]
        )
        ok = result.returncode == 0
        if ok:
            log.info(
                "K8s助手运行时能力重载触发完成",
                assistant_id=assistant_id,
                pod_name=pod_name,
                kind=kind,
                slug=normalized_slug,
            )
        else:
            log.warning(
                "K8s助手运行时能力重载触发失败",
                assistant_id=assistant_id,
                pod_name=pod_name,
                kind=kind,
                slug=normalized_slug,
                error=self._error_text(result, "failed to reload runtime capability"),
            )
        return ok

    def notify_workspace_skill_changed(
        self,
        *,
        assistant_id: str,
        skill_slug: str,
        container_name: str | None = None,
    ) -> bool:
        slug = normalize_workspace_slug(skill_slug, fallback="").strip()
        if not slug:
            return False
        pod_name = container_name or self._operation_pod_name_for(assistant_id)
        skill_path = f"{self.settings.iron_agent_workspace_mount_path}/skills/{slug}/SKILL.md"
        result = self.command_runner(
            [
                self.settings.k8s_runtime_kubectl_binary,
                "-n",
                self._namespace(),
                "exec",
                pod_name,
                "--",
                "touch",
                skill_path,
            ]
        )
        ok = result.returncode == 0
        if ok:
            log.info(
                "K8s助手运行时技能文件变更通知完成",
                assistant_id=assistant_id,
                pod_name=pod_name,
                skill_slug=slug,
            )
        else:
            log.warning(
                "K8s助手运行时技能文件变更通知失败",
                assistant_id=assistant_id,
                pod_name=pod_name,
                skill_slug=slug,
                error=self._error_text(result, "failed to touch skill file"),
            )
        return ok

    def _managed_hook_service_url(self) -> str:
        configured_url = self.settings.iron_agent_hook_service_url.strip()
        if configured_url:
            return configured_url
        internal_url = self.settings.security_service_internal_url.strip()
        return internal_url or "http://security-service:8000"

    def probe_runtime(self, *, assistant_id: str) -> RuntimeProbeResult:
        pod_name = self._operation_pod_name_for(assistant_id)
        log.info("K8s助手运行时探活开始", assistant_id=assistant_id, pod_name=pod_name)
        result = self.command_runner(
            [
                self.settings.k8s_runtime_kubectl_binary,
                "-n",
                self._namespace(),
                "get",
                "pod",
                pod_name,
                "-o",
                "json",
            ]
        )
        if result.returncode != 0:
            log.warning(
                "K8s助手运行时探活失败",
                assistant_id=assistant_id,
                pod_name=pod_name,
                reason="pod_not_found",
                error=self._error_text(result, "runtime pod not found"),
            )
            return RuntimeProbeResult(
                container_name=pod_name,
                container_exists=False,
                container_state="missing",
                status="failed",
                last_error=self._error_text(result, "runtime pod not found"),
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            log.warning(
                "K8s助手运行时探活失败",
                assistant_id=assistant_id,
                pod_name=pod_name,
                reason="invalid_kubectl_pod_json",
            )
            return RuntimeProbeResult(
                container_name=pod_name,
                container_exists=True,
                container_state="unknown",
                status="failed",
                last_error="invalid kubectl pod json",
            )
        metadata = payload.get("metadata") if isinstance(payload, dict) else {}
        status = payload.get("status") if isinstance(payload, dict) else {}
        metadata = metadata if isinstance(metadata, dict) else {}
        status = status if isinstance(status, dict) else {}
        phase = str(status.get("phase", "")).strip() or "Unknown"
        pod_ip = str(status.get("podIP", "")).strip()
        runtime_status = self._runtime_status_from_phase(phase)
        service_host = self._service_host_for(assistant_id)
        gateway_url = (
            f"http://{service_host}:{self.settings.iron_agent_gateway_port}"
            if runtime_status == "running"
            else None
        )
        health_state: str | None = None
        last_error = ""
        if runtime_status == "running":
            if gateway_url and self._gateway_healthz_reachable(gateway_url):
                health_state = "healthy"
            else:
                runtime_status = "starting"
                health_state = "starting"
                last_error = (
                    "gateway healthz is not reachable"
                    if gateway_url
                    else "gateway endpoint not published yet"
                )
        probe = RuntimeProbeResult(
            container_name=pod_name,
            container_exists=True,
            container_id=str(metadata.get("uid", "")).strip() or None,
            container_state=phase.lower(),
            health_state=health_state,
            status=runtime_status,
            gateway_host=service_host if runtime_status == "running" else None,
            gateway_port=self.settings.iron_agent_gateway_port if pod_ip else None,
            gateway_url=gateway_url,
            last_error=last_error,
        )
        log.info(
            "K8s助手运行时探活完成",
            assistant_id=assistant_id,
            pod_name=pod_name,
            pod_uid=probe.container_id,
            pod_phase=phase,
            pod_ip=pod_ip,
            status=probe.status,
            health_state=probe.health_state,
            gateway_url=probe.gateway_url,
            last_error=probe.last_error,
        )
        return probe

    def build_runtime_manifest(
        self,
        *,
        assistant_id: str,
        workspace_slug: str,
        iron_agent_config: str,
    ) -> list[dict[str, Any]]:
        pod_name = self._pod_name_for(assistant_id)
        secret_name = self._secret_name_for(assistant_id)
        service_name = self._service_name_for(assistant_id)
        namespace = self._namespace()
        workspace_subpath = self._workspace_subpath(workspace_slug)
        state_subpath = f"{workspace_subpath}/{self._state_dir_name()}"
        init_image = self.settings.k8s_runtime_init_image or self.settings.iron_agent_container_image
        init_script = "\n".join(
            [
                "set -eu",
                'workspace_dir="/workspace-root/${WORKSPACE_SUBPATH}"',
                'mkdir -p "$workspace_dir/temp" "$workspace_dir/persistent"',
                'mkdir -p "$workspace_dir/skills" "$workspace_dir/plugins"',
                'mkdir -p "$workspace_dir/${STATE_DIR}"',
                'cp /runtime-config/ironagent.json "$workspace_dir/${STATE_DIR}/ironagent.json"',
                'chown -R 1000:1000 "$workspace_dir"',
                'chmod -R u+rwX,g+rwX "$workspace_dir"',
            ]
        )
        pod_spec: dict[str, Any] = {
            "restartPolicy": "Always",
            "hostname": self._pod_hostname_for(assistant_id),
            "automountServiceAccountToken": False,
            "securityContext": {
                "fsGroup": 1000,
                "fsGroupChangePolicy": "OnRootMismatch",
            },
            "initContainers": [
                {
                    "name": "init-workspace",
                    "image": init_image,
                    "imagePullPolicy": self.settings.k8s_runtime_image_pull_policy,
                    "command": ["sh", "-lc", init_script],
                    "env": [
                        {"name": "WORKSPACE_SUBPATH", "value": workspace_subpath},
                        {"name": "STATE_DIR", "value": self._state_dir_name()},
                    ],
                    "volumeMounts": [
                        {
                            "name": "workspace",
                            "mountPath": "/workspace-root",
                        },
                        {
                            "name": "runtime-config",
                            "mountPath": "/runtime-config",
                            "readOnly": True,
                        },
                    ],
                }
            ],
            "containers": [
                {
                    "name": "ironagent-gateway",
                    "image": self.settings.iron_agent_container_image,
                    "imagePullPolicy": self.settings.k8s_runtime_image_pull_policy,
                    "workingDir": self.settings.iron_agent_workspace_mount_path,
                    "command": [
                        "node",
                        "/app/ironagent.mjs",
                        "gateway",
                        "--bind",
                        self.settings.iron_agent_gateway_bind,
                        "--port",
                        str(self.settings.iron_agent_gateway_port),
                        "--allow-unconfigured",
                    ],
                    "env": self._runtime_env(assistant_id),
                    "ports": [
                        {
                            "name": "http",
                            "containerPort": self.settings.iron_agent_gateway_port,
                        }
                    ],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "runAsUser": 1000,
                        "runAsGroup": 1000,
                    },
                    "resources": self._runtime_resources(),
                    "volumeMounts": [
                        {
                            "name": "workspace",
                            "mountPath": self.settings.iron_agent_workspace_mount_path,
                            "subPath": workspace_subpath,
                        },
                        {
                            "name": "workspace",
                            "mountPath": self.settings.iron_agent_state_mount_path,
                            "subPath": state_subpath,
                        },
                    ],
                }
            ],
            "volumes": [
                {
                    "name": "workspace",
                    "persistentVolumeClaim": {
                        "claimName": self.settings.k8s_runtime_workspace_pvc,
                },
                    },
                {
                    "name": "runtime-config",
                    "secret": {
                        "secretName": secret_name,
                },
                    },
            ],
        }
        service_account = self.settings.k8s_runtime_service_account_name.strip()
        if service_account:
            pod_spec["serviceAccountName"] = service_account
        return [
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": secret_name,
                    "namespace": namespace,
                    "labels": self._labels(assistant_id),
                },
                "type": "Opaque",
                "stringData": {
                    "ironagent.json": iron_agent_config,
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {
                    "name": service_name,
                    "namespace": namespace,
                    "labels": self._labels(assistant_id),
                },
                "spec": {
                    "type": "ClusterIP",
                    "selector": self._labels(assistant_id),
                    "ports": [
                        {
                            "name": "http",
                            "port": self.settings.iron_agent_gateway_port,
                            "targetPort": "http",
                        }
                    ],
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": pod_name,
                    "namespace": namespace,
                    "labels": self._labels(assistant_id),
                },
                "spec": pod_spec,
            },
        ]

    def _write_runtime_manifest(
        self,
        *,
        assistant_id: str,
        workspace_slug: str,
        state_root: Path,
    ) -> Path:
        config_path = state_root / "ironagent.json"
        manifest = self.build_runtime_manifest(
            assistant_id=assistant_id,
            workspace_slug=workspace_slug,
            iron_agent_config=config_path.read_text(encoding="utf-8"),
        )
        manifest_path = self._manifest_path(assistant_id)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            yaml.dump_all(
                _quote_manifest_strings(manifest),
                Dumper=_K8sManifestDumper,
                sort_keys=False,
                explicit_start=True,
            ),
            encoding="utf-8",
        )
        log.info(
            "K8s助手运行时清单已生成",
            assistant_id=assistant_id,
            manifest_path=str(manifest_path),
            workspace_slug=workspace_slug,
        )
        return manifest_path

    def _delete_runtime_pod(self, assistant_id: str, *, wait: bool) -> CommandResult:
        args = [
            self.settings.k8s_runtime_kubectl_binary,
            "-n",
            self._namespace(),
            "delete",
            "pod",
            *self._pod_name_candidates_for(assistant_id),
            "--ignore-not-found=true",
        ]
        if wait:
            args.extend(["--wait=true", "--timeout=60s"])
        return self.command_runner(args)

    def _manifest_path(self, assistant_id: str) -> Path:
        return (self.settings.assistants_runtime_root / assistant_id / "k8s-runtime.yaml").resolve()

    def _ensure_k8s_state_root(self, workspace_root: Path) -> Path:
        state_root = (workspace_root / self._state_dir_name()).resolve()
        state_root.mkdir(parents=True, exist_ok=True)
        return state_root

    def _runtime_env(self, assistant_id: str) -> list[dict[str, Any]]:
        env: list[dict[str, Any]] = [
            {"name": key, "value": value}
            for key, value in sorted(self._managed_container_env(assistant_id).items())
        ]
        env.append(
            {
                "name": "IRONAGENT_MDNS_HOSTNAME",
                "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
            }
        )
        return env

    def _runtime_resources(self) -> dict[str, dict[str, str]]:
        resources = {
            "requests": {
                "cpu": self.settings.k8s_runtime_cpu_request,
                "memory": self.settings.k8s_runtime_memory_request,
            },
            "limits": {
                "cpu": self.settings.k8s_runtime_cpu_limit,
                "memory": self.settings.k8s_runtime_memory_limit,
            },
        }
        return {
            section: {key: value for key, value in values.items() if value.strip()}
            for section, values in resources.items()
        }

    def _workspace_subpath(self, workspace_slug: str) -> str:
        prefix = self.settings.k8s_runtime_workspace_subpath_prefix.strip().strip("/")
        safe_slug = normalize_workspace_slug(workspace_slug, fallback="assistant")
        return f"{prefix}/{safe_slug}" if prefix else safe_slug

    def _state_dir_name(self) -> str:
        return self.settings.k8s_runtime_state_dir.strip().strip("/") or ".ironagent"

    def _namespace(self) -> str:
        return self.settings.k8s_runtime_namespace.strip() or "ironagent"

    def _labels(self, assistant_id: str) -> dict[str, str]:
        return {
            "app": "ironagent-assistant-runtime",
            "ironagent.ironagent/assistant-id": self._label_value(assistant_id),
            "ironagent.ironagent/runtime-backend": "kubernetes",
        }

    def _pod_name_for(self, assistant_id: str) -> str:
        return self._k8s_name("pod", assistant_id)

    def _legacy_pod_name_for(self, assistant_id: str) -> str:
        return self._k8s_name_with_prefix("ironagent-assistant", "pod", assistant_id)

    def _pod_name_candidates_for(self, assistant_id: str) -> tuple[str, ...]:
        primary = self._pod_name_for(assistant_id)
        if self.settings.assistant_container_name_prefix.strip() in {"", "ironagent-assistant"}:
            legacy = self._legacy_pod_name_for(assistant_id)
            if legacy != primary:
                return (primary, legacy)
        return (primary,)

    def _operation_pod_name_for(self, assistant_id: str) -> str:
        candidates = self._pod_name_candidates_for(assistant_id)
        if len(candidates) == 1:
            return candidates[0]
        for pod_name in candidates:
            result = self.command_runner(
                [
                    self.settings.k8s_runtime_kubectl_binary,
                    "-n",
                    self._namespace(),
                    "get",
                    "pod",
                    pod_name,
                    "-o",
                    "name",
                ]
            )
            if result.returncode == 0:
                return pod_name
        return candidates[0]

    def _pod_hostname_for(self, assistant_id: str) -> str:
        # IronAgent's mDNS service appends a display suffix to the hostname, so
        # keep the runtime hostname well below the Kubernetes 63-byte limit.
        digest = sha1(assistant_id.encode("utf-8")).hexdigest()[:12]
        return f"oc-{digest}"

    def _secret_name_for(self, assistant_id: str) -> str:
        return self._k8s_name("secret", assistant_id)

    def _legacy_secret_name_for(self, assistant_id: str) -> str:
        return self._k8s_name_with_prefix("ironagent-assistant", "secret", assistant_id)

    def _secret_name_candidates_for(self, assistant_id: str) -> tuple[str, ...]:
        primary = self._secret_name_for(assistant_id)
        if self.settings.assistant_container_name_prefix.strip() in {"", "ironagent-assistant"}:
            legacy = self._legacy_secret_name_for(assistant_id)
            if legacy != primary:
                return (primary, legacy)
        return (primary,)

    def _service_name_for(self, assistant_id: str) -> str:
        return self._k8s_name("svc", assistant_id)

    def _legacy_service_name_for(self, assistant_id: str) -> str:
        return self._k8s_name_with_prefix("ironagent-assistant", "svc", assistant_id)

    def _service_name_candidates_for(self, assistant_id: str) -> tuple[str, ...]:
        primary = self._service_name_for(assistant_id)
        if self.settings.assistant_container_name_prefix.strip() in {"", "ironagent-assistant"}:
            legacy = self._legacy_service_name_for(assistant_id)
            if legacy != primary:
                return (primary, legacy)
        return (primary,)

    def _service_host_for(self, assistant_id: str) -> str:
        return f"{self._service_name_for(assistant_id)}.{self._namespace()}.svc.cluster.local"

    def _k8s_name(self, suffix: str, assistant_id: str) -> str:
        prefix = self.settings.assistant_container_name_prefix.strip() or "ironagent-assistant"
        return self._k8s_name_with_prefix(prefix, suffix, assistant_id)

    def _k8s_name_with_prefix(
        self,
        prefix: str,
        suffix: str,
        assistant_id: str,
    ) -> str:
        raw = f"{prefix}-{assistant_id}-{suffix}"
        safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in raw)
        safe = "-".join(part for part in safe.split("-") if part)
        if len(safe) > 63:
            digest = sha1(safe.encode("utf-8")).hexdigest()[:8]
            safe = f"{safe[:54].strip('-')}-{digest}"
        safe = safe[:63].strip("-")
        return safe or f"ironagent-assistant-{suffix}"

    def _label_value(self, value: str) -> str:
        safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in value.strip())
        safe = "-".join(part for part in safe.split("-") if part)
        return safe[:63].strip("-") or "assistant"

    def _runtime_status_from_phase(self, phase: str) -> RuntimeStatusName:
        normalized = phase.strip().lower()
        if normalized == "running":
            return "running"
        if normalized == "pending":
            return "starting"
        if normalized == "succeeded":
            return "stopped"
        return "failed"
