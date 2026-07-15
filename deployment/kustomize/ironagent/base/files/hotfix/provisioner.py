from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from http.client import HTTPConnection, HTTPResponse
from pathlib import Path
from shutil import copy2, copytree, rmtree
from typing import Any
from urllib.parse import urlparse

from src.shared.config import Settings, get_settings
from src.shared.logging import get_logger
from src.shared.workspace_roots import (
    WorkspaceRootEntry,
    get_workspace_root,
    list_workspace_roots,
    normalize_workspace_slug,
)

from .models import AssistantIronAgentConfig, AssistantWorkspaceConfig, RuntimeStatusName


def utc_now() -> datetime:
    return datetime.now(UTC)


IRONAGENT_RUNTIME_UID = 1000
IRONAGENT_RUNTIME_GID = 1000
RUNTIME_COMMAND_TIMEOUT_SECONDS = 15
logger = get_logger("runtime.provisioner")


@dataclass(slots=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[list[str]], CommandResult]


@dataclass(slots=True)
class ProvisionResult:
    container_name: str
    container_id: str | None
    workspace_root: str
    workspace_root_id: str | None
    workspace_slug: str | None
    temp_dir: str
    persistent_dir: str
    state_root: str
    status: RuntimeStatusName
    image: str
    gateway_host: str | None = None
    gateway_port: int | None = None
    gateway_url: str | None = None
    gateway_token: str | None = None
    last_error: str = ""
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class RuntimeProbeResult:
    container_name: str
    container_exists: bool
    container_id: str | None = None
    container_state: str = "missing"
    health_state: str | None = None
    status: RuntimeStatusName = "failed"
    gateway_host: str | None = None
    gateway_port: int | None = None
    gateway_url: str | None = None
    last_error: str = ""


@dataclass(slots=True)
class DestroyRuntimeResult:
    container_name: str
    container_removed: bool = False
    workspace_removed: bool = False
    state_removed: bool = False


@dataclass(slots=True)
class WorkspaceLayout:
    root_id: str
    workspace_slug: str
    workspace_root: Path
    temp_dir: str = "temp"
    persistent_dir: str = "persistent"
    permissions_needed: bool = False


def default_command_runner(args: list[str]) -> CommandResult:
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            check=False,
            text=True,
            timeout=RUNTIME_COMMAND_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return CommandResult(returncode=127, stderr=f"command not found: {args[0]}")
    except subprocess.TimeoutExpired:
        return CommandResult(
            returncode=124,
            stderr=(
                f"command timed out after {RUNTIME_COMMAND_TIMEOUT_SECONDS}s: "
                + " ".join(args[:3])
            ),
        )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


class AssistantRuntimeProvisioner:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        command_runner: CommandRunner | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.command_runner = command_runner or default_command_runner

    def _docker_cli_missing(self, result: CommandResult) -> bool:
        return result.returncode == 127 and "command not found" in result.stderr.lower()

    def _docker_socket_available(self) -> bool:
        return Path(self.settings.docker_socket_path).exists()

    def _should_use_engine_api(self, result: CommandResult) -> bool:
        return self._docker_cli_missing(result) and self._docker_socket_available()

    def _engine_request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, str]:
        class UnixHTTPConnection(HTTPConnection):
            def __init__(self, socket_path: str) -> None:
                super().__init__("localhost")
                self._socket_path = socket_path

            def connect(self) -> None:  # type: ignore[override]
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.connect(self._socket_path)

        connection = UnixHTTPConnection(self.settings.docker_socket_path)
        body = None if payload is None else json.dumps(payload)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        try:
            connection.request(method, path, body=body, headers=headers)
            response: HTTPResponse = connection.getresponse()
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, raw
        except OSError as exc:
            return 599, f"{type(exc).__name__}: {exc}"
        finally:
            try:
                connection.close()
            except OSError:
                pass

    def _engine_inspect_container(self, container_name: str) -> dict[str, Any] | None:
        status_code, raw = self._engine_request("GET", f"/containers/{container_name}/json")
        if status_code == 404:
            return None
        if status_code < 200 or status_code >= 300:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def _engine_env_for_container(self, container_name: str) -> dict[str, str] | None:
        payload = self._engine_inspect_container(container_name)
        if payload is None:
            return None
        config = payload.get("Config")
        if not isinstance(config, dict):
            return None
        raw_env = config.get("Env")
        if not isinstance(raw_env, list):
            return None
        parsed: dict[str, str] = {}
        for item in raw_env:
            if not isinstance(item, str) or "=" not in item:
                continue
            key, value = item.split("=", 1)
            parsed[key] = value
        return parsed

    def _engine_start_container(self, container_name: str) -> CommandResult:
        status_code, raw = self._engine_request("POST", f"/containers/{container_name}/start")
        if status_code in {204, 304}:
            return CommandResult(returncode=0)
        return CommandResult(returncode=1, stderr=raw)

    def _engine_restart_container(self, container_name: str) -> CommandResult:
        status_code, raw = self._engine_request("POST", f"/containers/{container_name}/restart")
        if status_code == 204:
            return CommandResult(returncode=0)
        return CommandResult(returncode=1, stderr=raw)

    def _engine_stop_container(self, container_name: str) -> CommandResult:
        status_code, raw = self._engine_request("POST", f"/containers/{container_name}/stop?t=10")
        if status_code in {204, 304}:
            return CommandResult(returncode=0)
        return CommandResult(returncode=1, stderr=raw)

    def _engine_pause_container(self, container_name: str) -> CommandResult:
        status_code, raw = self._engine_request("POST", f"/containers/{container_name}/pause")
        if status_code in {204, 304}:
            return CommandResult(returncode=0)
        return CommandResult(returncode=1, stderr=raw)

    def _engine_unpause_container(self, container_name: str) -> CommandResult:
        status_code, raw = self._engine_request("POST", f"/containers/{container_name}/unpause")
        if status_code in {204, 304}:
            return CommandResult(returncode=0)
        return CommandResult(returncode=1, stderr=raw)

    def _engine_remove_container(self, container_name: str) -> CommandResult:
        status_code, raw = self._engine_request("DELETE", f"/containers/{container_name}?force=1")
        if status_code in {204, 404}:
            return CommandResult(returncode=0)
        return CommandResult(returncode=1, stderr=raw)

    def _engine_create_container(
        self,
        *,
        assistant_id: str,
        container_name: str,
        workspace_root: Path,
        state_root: Path,
    ) -> CommandResult:
        published_port = self._gateway_publish_port(assistant_id)
        gateway_launch_port = self._gateway_launch_port(published_port)
        env_values = [
            f"{key}={value}"
            for key, value in self._managed_container_env(
                assistant_id,
                published_port=published_port,
            ).items()
        ]
        cmd = [
            "node",
            "/app/ironagent.mjs",
            "gateway",
            "--bind",
            self.settings.ironagent_gateway_bind,
            "--port",
            str(gateway_launch_port),
            "--allow-unconfigured",
        ]
        host_config: dict[str, Any] = {
            "Init": True,
            "Binds": [
                f"{state_root}:{self.settings.ironagent_state_mount_path}",
                f"{workspace_root}:{self.settings.ironagent_workspace_mount_path}",
            ],
            "ExtraHosts": self._extra_hosts(),
        }
        payload: dict[str, Any] = {
            "Image": self.settings.ironagent_container_image,
            "Cmd": cmd,
            "Env": env_values,
            "Labels": {
                "ironagent.assistant_id": assistant_id,
                "ironagent.managed_by": "ironagent",
            },
            "HostConfig": host_config,
        }
        if published_port is None:
            payload["ExposedPorts"] = {f"{self.settings.ironagent_gateway_port}/tcp": {}}
            payload["HostConfig"]["PortBindings"] = {
                f"{self.settings.ironagent_gateway_port}/tcp": [
                    self._engine_gateway_port_binding(None)
                ]
            }
        else:
            payload["HostConfig"]["NetworkMode"] = "host"
        status_code, raw = self._engine_request(
            "POST",
            f"/containers/create?name={container_name}",
            payload=payload,
        )
        if status_code < 200 or status_code >= 300:
            return CommandResult(returncode=1, stderr=raw)
        try:
            created = json.loads(raw)
        except json.JSONDecodeError:
            return CommandResult(returncode=1, stderr=raw)
        container_id = created.get("Id")
        if not isinstance(container_id, str) or not container_id.strip():
            return CommandResult(returncode=1, stderr="missing container id from Docker Engine API")
        started = self._engine_start_container(container_name)
        if started.returncode != 0:
            return started
        return CommandResult(returncode=0, stdout=f"{container_id}\n")

    def _engine_port_endpoint(self, container_name: str) -> tuple[str, int] | None:
        payload = self._engine_inspect_container(container_name)
        if payload is None:
            return None
        network_settings = payload.get("NetworkSettings")
        if not isinstance(network_settings, dict):
            return None
        ports = network_settings.get("Ports")
        if not isinstance(ports, dict):
            return None
        mappings = ports.get(f"{self.settings.ironagent_gateway_port}/tcp")
        if not isinstance(mappings, list):
            return None
        fallback: tuple[str, int] | None = None
        for mapping in mappings:
            if not isinstance(mapping, dict):
                continue
            host = str(mapping.get("HostIp", "")).strip()
            port_raw = str(mapping.get("HostPort", "")).strip()
            if not host or not port_raw:
                continue
            try:
                port = int(port_raw)
            except ValueError:
                continue
            normalized_host = self._normalize_gateway_host(host)
            if host in {"127.0.0.1", "localhost"}:
                return (normalized_host, port)
            if fallback is None:
                fallback = (normalized_host, port)
        return fallback

    def ensure_runtime(
        self,
        *,
        assistant_id: str,
        assistant_name: str,
        model_config: AssistantIronAgentConfig | None = None,
        workspace_config: AssistantWorkspaceConfig | None = None,
    ) -> ProvisionResult:
        workspace_layout = self._ensure_workspace(
            assistant_id=assistant_id,
            assistant_name=assistant_name,
            workspace_config=workspace_config,
        )
        workspace_root = workspace_layout.workspace_root
        state_root_layout = self._ensure_state_root(assistant_id=assistant_id)
        state_root = state_root_layout.path
        gateway_token = self._gateway_token_for(assistant_id)
        config_changed = self._write_ironagent_config(
            state_root=state_root,
            gateway_token=gateway_token,
            model_config=model_config,
        )
        auth_changed = self._sync_host_ironagent_auth(
            state_root=state_root,
            model_config=model_config,
        )
        if workspace_layout.permissions_needed:
            self._ensure_runtime_mount_permissions(workspace_root)
        if state_root_layout.permissions_needed:
            self._ensure_runtime_mount_permissions(state_root)
        runtime_changed = config_changed or auth_changed
        container_name = self._container_name_for(assistant_id)
        inspect = self._inspect_container(container_name)
        if inspect is not None:
            container_id, status = inspect
            if self._container_requires_recreate(
                assistant_id=assistant_id,
                container_name=container_name,
                workspace_root=workspace_root,
                state_root=state_root,
            ):
                removed = self._remove_container(container_name)
                if removed.returncode != 0:
                    return ProvisionResult(
                        container_name=container_name,
                        container_id=container_id,
                        workspace_root=str(workspace_root),
                        workspace_root_id=workspace_layout.root_id,
                        workspace_slug=workspace_layout.workspace_slug,
                        temp_dir=workspace_layout.temp_dir,
                        persistent_dir=workspace_layout.persistent_dir,
                        state_root=str(state_root),
                        status="failed",
                        image=self.settings.ironagent_container_image,
                        gateway_token=gateway_token,
                        last_error=self._error_text(
                            removed,
                            "failed to recreate container",
                        ),
                        updated_at=utc_now(),
                    )
                created = self._create_container(
                    assistant_id=assistant_id,
                    container_name=container_name,
                    workspace_root=workspace_root,
                    state_root=state_root,
                )
                if created.returncode != 0:
                    return ProvisionResult(
                        container_name=container_name,
                        container_id=None,
                        workspace_root=str(workspace_root),
                        workspace_root_id=workspace_layout.root_id,
                        workspace_slug=workspace_layout.workspace_slug,
                        temp_dir=workspace_layout.temp_dir,
                        persistent_dir=workspace_layout.persistent_dir,
                        state_root=str(state_root),
                        status="failed",
                        image=self.settings.ironagent_container_image,
                        gateway_token=gateway_token,
                        last_error=self._error_text(
                            created,
                            "failed to recreate container",
                        ),
                        updated_at=utc_now(),
                    )
                gateway_host, gateway_port, gateway_url = self._resolve_gateway_endpoint(
                    container_name
                )
                return ProvisionResult(
                    container_name=container_name,
                    container_id=created.stdout.strip() or None,
                    workspace_root=str(workspace_root),
                    workspace_root_id=workspace_layout.root_id,
                    workspace_slug=workspace_layout.workspace_slug,
                    temp_dir=workspace_layout.temp_dir,
                    persistent_dir=workspace_layout.persistent_dir,
                    state_root=str(state_root),
                    status="running",
                    image=self.settings.ironagent_container_image,
                    gateway_host=gateway_host,
                    gateway_port=gateway_port,
                    gateway_url=gateway_url,
                    gateway_token=gateway_token,
                    updated_at=utc_now(),
                )
            if status != "running":
                started = self._start_container(container_name)
                if started.returncode != 0:
                    return ProvisionResult(
                        container_name=container_name,
                        container_id=container_id,
                        workspace_root=str(workspace_root),
                        workspace_root_id=workspace_layout.root_id,
                        workspace_slug=workspace_layout.workspace_slug,
                        temp_dir=workspace_layout.temp_dir,
                        persistent_dir=workspace_layout.persistent_dir,
                        state_root=str(state_root),
                        status="failed",
                        image=self.settings.ironagent_container_image,
                        gateway_token=gateway_token,
                        last_error=self._error_text(started, "failed to start container"),
                        updated_at=utc_now(),
                    )
            elif runtime_changed:
                restarted = self._restart_container(container_name)
                if restarted.returncode != 0:
                    return ProvisionResult(
                        container_name=container_name,
                        container_id=container_id,
                        workspace_root=str(workspace_root),
                        workspace_root_id=workspace_layout.root_id,
                        workspace_slug=workspace_layout.workspace_slug,
                        temp_dir=workspace_layout.temp_dir,
                        persistent_dir=workspace_layout.persistent_dir,
                        state_root=str(state_root),
                        status="failed",
                        image=self.settings.ironagent_container_image,
                        gateway_token=gateway_token,
                        last_error=self._error_text(
                            restarted,
                            "failed to restart container",
                        ),
                        updated_at=utc_now(),
                    )
            gateway_host, gateway_port, gateway_url = self._resolve_gateway_endpoint(
                container_name
            )
            return ProvisionResult(
                container_name=container_name,
                container_id=container_id,
                workspace_root=str(workspace_root),
                workspace_root_id=workspace_layout.root_id,
                workspace_slug=workspace_layout.workspace_slug,
                temp_dir=workspace_layout.temp_dir,
                persistent_dir=workspace_layout.persistent_dir,
                state_root=str(state_root),
                status="running",
                image=self.settings.ironagent_container_image,
                gateway_host=gateway_host,
                gateway_port=gateway_port,
                gateway_url=gateway_url,
                gateway_token=gateway_token,
                updated_at=utc_now(),
            )

        created = self._create_container(
            assistant_id=assistant_id,
            container_name=container_name,
            workspace_root=workspace_root,
            state_root=state_root,
        )
        if created.returncode != 0:
            return ProvisionResult(
                container_name=container_name,
                container_id=None,
                workspace_root=str(workspace_root),
                workspace_root_id=workspace_layout.root_id,
                workspace_slug=workspace_layout.workspace_slug,
                temp_dir=workspace_layout.temp_dir,
                persistent_dir=workspace_layout.persistent_dir,
                state_root=str(state_root),
                status="failed",
                image=self.settings.ironagent_container_image,
                gateway_token=gateway_token,
                last_error=self._error_text(created, "failed to create container"),
                updated_at=utc_now(),
            )

        gateway_host, gateway_port, gateway_url = self._resolve_gateway_endpoint(container_name)
        return ProvisionResult(
            container_name=container_name,
            container_id=created.stdout.strip() or None,
            workspace_root=str(workspace_root),
            workspace_root_id=workspace_layout.root_id,
            workspace_slug=workspace_layout.workspace_slug,
            temp_dir=workspace_layout.temp_dir,
            persistent_dir=workspace_layout.persistent_dir,
            state_root=str(state_root),
            status="running",
            image=self.settings.ironagent_container_image,
            gateway_host=gateway_host,
            gateway_port=gateway_port,
            gateway_url=gateway_url,
            gateway_token=gateway_token,
            updated_at=utc_now(),
        )

    def destroy_runtime(
        self,
        *,
        assistant_id: str,
        workspace_root: str | None = None,
        state_root: str | None = None,
    ) -> DestroyRuntimeResult:
        container_name = self._container_name_for(assistant_id)
        removed = self._remove_container(container_name)
        if removed.returncode != 0 and not self._container_is_missing(removed):
            raise ValueError(self._error_text(removed, "failed to remove container"))

        workspace_removed = self._remove_workspace_root(workspace_root)
        state_removed = self._remove_state_root(state_root)
        return DestroyRuntimeResult(
            container_name=container_name,
            container_removed=removed.returncode == 0,
            workspace_removed=workspace_removed,
            state_removed=state_removed,
        )

    def _container_name_for(self, assistant_id: str) -> str:
        safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in assistant_id.strip())
        safe = "-".join(part for part in safe.split("-") if part)
        return f"{self.settings.assistant_container_name_prefix}-{safe or 'assistant'}"

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
        resolved_container_name = container_name or self._container_name_for(assistant_id)
        skill_path = (
            f"{self.settings.ironagent_workspace_mount_path}/skills/{slug}/SKILL.md"
        )
        result = self.command_runner(
            [
                self.settings.docker_binary,
                "exec",
                resolved_container_name,
                "touch",
                skill_path,
            ]
        )
        return result.returncode == 0

    def restart_runtime(self, *, assistant_id: str) -> RuntimeProbeResult:
        container_name = self._container_name_for(assistant_id)
        inspected = self._inspect_container(container_name)
        if inspected is None:
            return RuntimeProbeResult(
                container_name=container_name,
                container_exists=False,
                container_state="missing",
                status="failed",
                last_error="container not found",
            )

        restarted = self._restart_container(container_name)
        if restarted.returncode != 0:
            return RuntimeProbeResult(
                container_name=container_name,
                container_exists=True,
                container_id=inspected[0],
                container_state=inspected[1],
                status="failed",
                last_error=self._error_text(restarted, "failed to restart container"),
            )

        latest_probe = self.probe_runtime(assistant_id=assistant_id)
        for _ in range(5):
            if latest_probe.status != "starting":
                return latest_probe
            time.sleep(0.5)
            latest_probe = self.probe_runtime(assistant_id=assistant_id)
        return latest_probe

    def start_runtime(self, *, assistant_id: str) -> RuntimeProbeResult:
        container_name = self._container_name_for(assistant_id)
        inspected = self._inspect_container(container_name)
        if inspected is None:
            return RuntimeProbeResult(
                container_name=container_name,
                container_exists=False,
                container_state="missing",
                status="failed",
                last_error="container not found",
            )

        container_id, container_state = inspected
        if container_state == "running":
            return self.probe_runtime(assistant_id=assistant_id)

        if container_state == "paused":
            started = self._unpause_container(container_name)
        else:
            started = self._start_container(container_name)

        if started.returncode != 0:
            return RuntimeProbeResult(
                container_name=container_name,
                container_exists=True,
                container_id=container_id,
                container_state=container_state,
                status="failed",
                last_error=self._error_text(started, "failed to start container"),
            )

        latest_probe = self.probe_runtime(assistant_id=assistant_id)
        for _ in range(5):
            if latest_probe.status not in {"starting", "paused"}:
                return latest_probe
            time.sleep(0.5)
            latest_probe = self.probe_runtime(assistant_id=assistant_id)
        return latest_probe

    def stop_runtime(self, *, assistant_id: str) -> RuntimeProbeResult:
        container_name = self._container_name_for(assistant_id)
        inspected = self._inspect_container(container_name)
        if inspected is None:
            return RuntimeProbeResult(
                container_name=container_name,
                container_exists=False,
                container_state="missing",
                status="failed",
                last_error="container not found",
            )

        container_id, container_state = inspected
        if container_state in {"exited", "dead"}:
            return self.probe_runtime(assistant_id=assistant_id)

        stopped = self._stop_container(container_name)
        if stopped.returncode != 0:
            return RuntimeProbeResult(
                container_name=container_name,
                container_exists=True,
                container_id=container_id,
                container_state=container_state,
                status="failed",
                last_error=self._error_text(stopped, "failed to stop container"),
            )
        return self.probe_runtime(assistant_id=assistant_id)

    def pause_runtime(self, *, assistant_id: str) -> RuntimeProbeResult:
        container_name = self._container_name_for(assistant_id)
        inspected = self._inspect_container(container_name)
        if inspected is None:
            return RuntimeProbeResult(
                container_name=container_name,
                container_exists=False,
                container_state="missing",
                status="failed",
                last_error="container not found",
            )

        container_id, container_state = inspected
        if container_state == "paused":
            return self.probe_runtime(assistant_id=assistant_id)
        if container_state != "running":
            return RuntimeProbeResult(
                container_name=container_name,
                container_exists=True,
                container_id=container_id,
                container_state=container_state,
                status="stopped" if container_state in {"exited", "dead"} else "failed",
                last_error="container must be running before pause",
            )

        paused = self._pause_container(container_name)
        if paused.returncode != 0:
            return RuntimeProbeResult(
                container_name=container_name,
                container_exists=True,
                container_id=container_id,
                container_state=container_state,
                status="failed",
                last_error=self._error_text(paused, "failed to pause container"),
            )
        return self.probe_runtime(assistant_id=assistant_id)

    def _extra_hosts(self) -> list[str]:
        raw = self.settings.ironagent_container_extra_host.strip()
        if not raw:
            return []
        return [raw]

    def _managed_container_env(
        self,
        assistant_id: str,
        *,
        published_port: int | None = None,
    ) -> dict[str, str]:
        managed = {
            "HOME": "/home/node",
            "TERM": "xterm-256color",
            "IRONAGENT_ASSISTANT_ID": assistant_id,
            "IRONAGENT_STATE_DIR": self.settings.ironagent_state_mount_path,
            "IRONAGENT_CONFIG_PATH": f"{self.settings.ironagent_state_mount_path}/ironagent.json",
        }
        if published_port is not None:
            managed["IRONAGENT_GATEWAY_PUBLISHED_PORT"] = str(published_port)
        if self.settings.ironagent_hooks_enabled:
            managed.update(
                {
                    "IRONAGENT_HOOKS_ENABLED": "true",
                    "IRONAGENT_SERVICE_URL": self.settings.ironagent_hook_service_url,
                    "IRONAGENT_FAIL_CLOSED": (
                        "true" if self.settings.ironagent_hook_fail_closed else "false"
                    ),
                    "IRONAGENT_HOOK_TIMEOUT_MS": str(self.settings.ironagent_hook_timeout_ms),
                    "IRONAGENT_APPROVAL_POLL_MS": str(self.settings.ironagent_approval_poll_ms),
                    "IRONAGENT_APPROVAL_MAX_WAIT_MS": str(
                        self.settings.ironagent_approval_max_wait_ms
                    ),
                }
            )
            if self.settings.ironagent_hook_api_token.strip():
                managed["IRONAGENT_API_TOKEN"] = self.settings.ironagent_hook_api_token.strip()
        managed.update(self._managed_proxy_env())
        return managed

    def _managed_proxy_env(self) -> dict[str, str]:
        proxy_env: dict[str, str] = {}
        http_proxy = self.settings.ironagent_http_proxy.strip()
        https_proxy = self.settings.ironagent_https_proxy.strip()
        configured_no_proxy = self.settings.ironagent_no_proxy.strip()
        no_proxy = (
            self._resolved_no_proxy_value()
            if http_proxy or https_proxy or configured_no_proxy
            else ""
        )
        if http_proxy:
            proxy_env["HTTP_PROXY"] = http_proxy
            proxy_env["http_proxy"] = http_proxy
        if https_proxy:
            proxy_env["HTTPS_PROXY"] = https_proxy
            proxy_env["https_proxy"] = https_proxy
        if no_proxy:
            proxy_env["NO_PROXY"] = no_proxy
            proxy_env["no_proxy"] = no_proxy
        return proxy_env

    def _resolved_no_proxy_value(self) -> str:
        configured_entries = [
            item.strip()
            for item in self.settings.ironagent_no_proxy.split(",")
            if item.strip()
        ]
        required_entries = ["127.0.0.1", "localhost", "::1", "host.docker.internal"]
        hook_host = self._hostname_from_url(self.settings.ironagent_hook_service_url)
        if hook_host:
            required_entries.append(hook_host)
        merged: list[str] = []
        seen: set[str] = set()
        for item in [*configured_entries, *required_entries]:
            normalized = item.lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            merged.append(item)
        return ",".join(merged)

    def _hostname_from_url(self, value: str) -> str | None:
        trimmed = value.strip()
        if not trimmed:
            return None
        try:
            parsed = urlparse(trimmed)
        except ValueError:
            return None
        return parsed.hostname.strip() if parsed.hostname else None

    def _ensure_workspace(
        self,
        *,
        assistant_id: str,
        assistant_name: str,
        workspace_config: AssistantWorkspaceConfig | None,
    ) -> WorkspaceLayout:
        workspace_root_entry = self._resolve_workspace_root(workspace_config)
        workspace_slug = normalize_workspace_slug(
            workspace_config.workspace_slug if workspace_config else "",
            fallback=assistant_id,
        )
        workspace_root = (workspace_root_entry.host_path / workspace_slug).resolve()
        permissions_needed = not workspace_root.exists()
        workspace_root.mkdir(parents=True, exist_ok=True)
        for child_name in ("temp", "persistent", "skills", "plugins"):
            child_path = workspace_root / child_name
            permissions_needed = permissions_needed or not child_path.exists()
            child_path.mkdir(parents=True, exist_ok=True)
        self._write_if_missing(
            workspace_root / "USER.md",
            f"# {assistant_name}\n\nThis workspace belongs to assistant `{assistant_id}`.\n",
        )
        self._write_if_missing(
            workspace_root / "AGENTS.md",
            (
                "# Local Assistant Guidance\n\n"
                "Place local instructions for this assistant here.\n"
            ),
        )
        self._seed_workspace_skills(workspace_root)
        return WorkspaceLayout(
            root_id=workspace_root_entry.id,
            workspace_slug=workspace_slug,
            workspace_root=workspace_root,
            permissions_needed=permissions_needed,
        )

    def _resolve_workspace_root(
        self,
        workspace_config: AssistantWorkspaceConfig | None,
    ) -> WorkspaceRootEntry:
        root_id = (
            workspace_config.root_id.strip()
            if workspace_config and workspace_config.root_id.strip()
            else self.settings.default_workspace_root_id.strip() or "default"
        )
        return get_workspace_root(root_id, self.settings)

    def _seed_workspace_skills(self, workspace_root: Path) -> None:
        source_root = self.settings.ironagent_source_root
        if source_root is None:
            return
        skills_root = source_root / "skills"
        if not skills_root.exists():
            return
        for skill_name in self._seed_workspace_skill_names():
            self._seed_workspace_skill(
                source_dir=skills_root / skill_name,
                target_dir=workspace_root / "skills" / skill_name,
            )

    def _seed_workspace_skill_names(self) -> list[str]:
        raw = self.settings.ironagent_seed_workspace_skills.strip()
        if not raw:
            return []
        return [item.strip() for item in raw.split(",") if item.strip()]

    def _seed_workspace_skill(self, *, source_dir: Path, target_dir: Path) -> None:
        if target_dir.exists():
            return
        skill_md = source_dir / "SKILL.md"
        if not skill_md.exists():
            return
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        copytree(source_dir, target_dir, copy_function=copy2)

    @dataclass(slots=True)
    class StateRootLayout:
        path: Path
        permissions_needed: bool = False

    def _ensure_state_root(self, *, assistant_id: str) -> StateRootLayout:
        state_root = (self.settings.assistants_runtime_root / assistant_id / "state").resolve()
        permissions_needed = not state_root.exists()
        state_root.mkdir(parents=True, exist_ok=True)
        return self.StateRootLayout(path=state_root, permissions_needed=permissions_needed)

    def _ensure_runtime_mount_permissions(self, root: Path) -> None:
        if os.name != "posix" or not root.exists():
            return

        def _apply(path: Path) -> None:
            try:
                os.chown(path, IRONAGENT_RUNTIME_UID, IRONAGENT_RUNTIME_GID)
            except OSError as exc:
                logger.warning("chown_skipped", path=str(path), error=str(exc))
            try:
                mode = 0o775 if path.is_dir() else 0o664
                os.chmod(path, mode)
            except OSError as exc:
                logger.warning("chmod_skipped", path=str(path), mode=oct(mode), error=str(exc))

        _apply(root)
        for current_root, dirnames, filenames in os.walk(root, followlinks=False):
            current_path = Path(current_root)
            for dirname in dirnames:
                path = current_path / dirname
                if path.is_symlink():
                    continue
                _apply(path)
            for filename in filenames:
                path = current_path / filename
                if path.is_symlink():
                    continue
                _apply(path)

    def _gateway_token_for(self, assistant_id: str) -> str:
        safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in assistant_id.strip())
        safe = "-".join(part for part in safe.split("-") if part)
        return f"ironagent-local-{safe or 'assistant'}"

    def _write_ironagent_config(
        self,
        *,
        state_root: Path,
        gateway_token: str,
        model_config: AssistantIronAgentConfig | None,
    ) -> bool:
        config_path = state_root / "ironagent.json"
        payload = self._read_config_payload(config_path)

        gateway = payload.setdefault("gateway", {})
        if not isinstance(gateway, dict):
            gateway = {}
            payload["gateway"] = gateway
        auth = gateway.setdefault("auth", {})
        if not isinstance(auth, dict):
            auth = {}
            gateway["auth"] = auth
        auth["mode"] = "token"
        auth["token"] = gateway_token
        control_ui = gateway.setdefault("controlUi", {})
        if not isinstance(control_ui, dict):
            control_ui = {}
            gateway["controlUi"] = control_ui
        control_ui["dangerouslyAllowHostHeaderOriginFallback"] = True
        control_ui["dangerouslyDisableDeviceAuth"] = True

        agents = payload.setdefault("agents", {})
        if not isinstance(agents, dict):
            agents = {}
            payload["agents"] = agents
        defaults = agents.setdefault("defaults", {})
        if not isinstance(defaults, dict):
            defaults = {}
            agents["defaults"] = defaults
        defaults["workspace"] = self.settings.ironagent_workspace_mount_path

        tools = payload.setdefault("tools", {})
        if not isinstance(tools, dict):
            tools = {}
            payload["tools"] = tools
        exec_settings = tools.setdefault("exec", {})
        if not isinstance(exec_settings, dict):
            exec_settings = {}
            tools["exec"] = exec_settings
        # IronAgent owns the approval decision for managed assistant containers, so
        # disable IronAgent's built-in host-exec approval prompts here.
        exec_settings["security"] = "full"
        exec_settings["ask"] = "off"

        effective_provider_id = self._provider_id_from_config(model_config)
        has_custom_provider_config = self._has_custom_provider_config(model_config)
        if model_config is not None and model_config.primary_model.strip():
            model_settings = defaults.setdefault("model", {})
            if not isinstance(model_settings, dict):
                model_settings = {}
                defaults["model"] = model_settings
            primary_model = self._effective_primary_model(model_config)
            model_settings["primary"] = primary_model

            # IronAgent's models.list and picker surfaces honor agents.defaults.models as
            # an allowlist. Keep managed assistants focused on the model IronAgent
            # explicitly configured so users do not see a misleading full catalog.
            allowed_models = defaults.setdefault("models", {})
            if not isinstance(allowed_models, dict):
                allowed_models = {}
                defaults["models"] = allowed_models
            allowed_models.clear()
            allowed_models[primary_model] = {}
        else:
            defaults.pop("models", None)

        if effective_provider_id and has_custom_provider_config:
            models = payload.setdefault("models", {})
            if not isinstance(models, dict):
                models = {}
                payload["models"] = models
            providers: dict[str, dict[str, object]] = {effective_provider_id: {}}
            models["providers"] = providers
            provider_settings = providers[effective_provider_id]
            provider_model_id = ""
            if model_config is not None and model_config.primary_model.strip():
                provider_model_id = self._effective_primary_model(model_config)
                if "/" in provider_model_id:
                    _, _, provider_model_id = provider_model_id.partition("/")
            if effective_provider_id == "ollama":
                # Force the native Ollama API so tool calling does not accidentally
                # fall back to an OpenAI-compatible /v1 endpoint.
                provider_settings["api"] = "ollama"
            elif effective_provider_id == "dashscope-coding":
                provider_settings["api"] = "openai-completions"
            if model_config is not None and model_config.provider_base_url.strip():
                provider_settings["baseUrl"] = model_config.provider_base_url.strip()
            if model_config is not None and model_config.provider_api_key.strip():
                provider_settings["apiKey"] = model_config.provider_api_key.strip()
            if effective_provider_id == "dashscope-coding" and provider_model_id:
                provider_settings["models"] = [
                    {
                        "id": provider_model_id,
                        "name": provider_model_id,
                        "reasoning": False,
                        "input": ["text"],
                        "cost": {
                            "input": 0,
                            "output": 0,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                        },
                        "contextWindow": 128000,
                        "maxTokens": 32000,
                    }
                ]
            else:
                provider_settings["models"] = []
        else:
            models = payload.get("models")
            if isinstance(models, dict):
                models.pop("providers", None)
                if not models:
                    payload.pop("models", None)

        self._merge_host_auth_profiles(
            payload=payload,
            model_config=model_config,
        )

        serialized = json.dumps(payload, indent=2, ensure_ascii=True) + "\n"
        if config_path.exists():
            current = config_path.read_text(encoding="utf-8")
            if current == serialized:
                return False
        config_path.write_text(serialized, encoding="utf-8")
        return True

    def _read_config_payload(self, config_path: Path) -> dict[str, object]:
        if not config_path.exists():
            return {}
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _provider_id_from_config(
        self,
        model_config: AssistantIronAgentConfig | None,
    ) -> str:
        if model_config is None:
            return ""
        if model_config.auth_mode != "provider_api_key":
            return ""
        if model_config.provider_id.strip():
            return model_config.provider_id.strip()
        if model_config.provider_base_url.strip() or model_config.provider_api_key.strip():
            if "/" in model_config.primary_model:
                provider_id, _, _ = model_config.primary_model.partition("/")
                return provider_id.strip()
        return ""

    def _effective_primary_model(
        self,
        model_config: AssistantIronAgentConfig,
    ) -> str:
        primary_model = model_config.primary_model.strip()
        if "/" in primary_model:
            return primary_model
        provider_id = self._provider_id_from_config(model_config)
        if provider_id:
            return f"{provider_id}/{primary_model}"
        return primary_model

    def _has_custom_provider_config(
        self,
        model_config: AssistantIronAgentConfig | None,
    ) -> bool:
        if model_config is None:
            return False
        if model_config.auth_mode != "provider_api_key":
            return False
        return bool(
            model_config.provider_id.strip()
            or model_config.provider_base_url.strip()
            or model_config.provider_api_key.strip()
        )

    def _merge_host_auth_profiles(
        self,
        *,
        payload: dict[str, object],
        model_config: AssistantIronAgentConfig | None,
    ) -> None:
        if not self._uses_host_openai_codex_auth(model_config):
            return
        host_config_path = self.settings.host_ironagent_root / "ironagent.json"
        host_payload = self._read_config_payload(host_config_path)
        host_auth = host_payload.get("auth")
        if not isinstance(host_auth, dict):
            return
        host_profiles = host_auth.get("profiles")
        if not isinstance(host_profiles, dict) or not host_profiles:
            return
        auth = payload.setdefault("auth", {})
        if not isinstance(auth, dict):
            auth = {}
            payload["auth"] = auth
        profiles = auth.setdefault("profiles", {})
        if not isinstance(profiles, dict):
            profiles = {}
            auth["profiles"] = profiles
        profiles.update(host_profiles)

    def _sync_host_ironagent_auth(
        self,
        *,
        state_root: Path,
        model_config: AssistantIronAgentConfig | None,
    ) -> bool:
        if not self._uses_host_openai_codex_auth(model_config):
            return False
        host_agent_root = (
            self.settings.host_ironagent_root
            / "agents"
            / self.settings.host_ironagent_agent_id
            / "agent"
        )
        target_agent_root = state_root / "agents" / self.settings.host_ironagent_agent_id / "agent"
        target_agent_root.mkdir(parents=True, exist_ok=True)
        changed = False
        for filename in ("auth-profiles.json", "models.json"):
            source = host_agent_root / filename
            if source.exists():
                changed = self._copy_if_changed(source, target_agent_root / filename) or changed
        return changed

    def _uses_host_openai_codex_auth(
        self,
        model_config: AssistantIronAgentConfig | None,
    ) -> bool:
        if model_config is None:
            return False
        if model_config.auth_mode != "host_ironagent":
            return False
        primary_model = model_config.primary_model.strip()
        return primary_model.startswith("openai-codex/")

    def _copy_if_changed(self, source: Path, target: Path) -> bool:
        if target.exists() and target.read_bytes() == source.read_bytes():
            return False
        copy2(source, target)
        return True

    def _write_if_missing(self, path: Path, content: str) -> None:
        if path.exists():
            return
        path.write_text(content, encoding="utf-8")

    def _remove_workspace_root(self, workspace_root: str | None) -> bool:
        if not workspace_root:
            return False
        managed_roots = [entry.host_path.resolve() for entry in list_workspace_roots(self.settings)]
        return self._remove_managed_directory(Path(workspace_root), managed_roots)

    def _remove_state_root(self, state_root: str | None) -> bool:
        if not state_root:
            return False
        target = Path(state_root).resolve()
        if target.name == "state":
            target = target.parent
        managed_root = self.settings.assistants_runtime_root.resolve()
        return self._remove_managed_directory(target, [managed_root])

    def _remove_managed_directory(
        self,
        target: Path,
        managed_roots: list[Path],
    ) -> bool:
        resolved_target = target.resolve()
        for managed_root in managed_roots:
            resolved_root = managed_root.resolve()
            if resolved_target == resolved_root:
                raise ValueError(f"refusing to remove managed root itself: {resolved_target}")
            if resolved_root not in resolved_target.parents:
                continue
            if not resolved_target.exists():
                return False
            rmtree(resolved_target)
            return True
        raise ValueError(f"refusing to remove unmanaged path: {resolved_target}")

    def _inspect_container(self, container_name: str) -> tuple[str | None, str] | None:
        result = self.command_runner(
            [
                self.settings.docker_binary,
                "container",
                "inspect",
                container_name,
                "--format",
                "{{.Id}}|{{.State.Status}}",
            ]
        )
        if self._should_use_engine_api(result):
            payload = self._engine_inspect_container(container_name)
            if payload is None:
                return None
            container_id = payload.get("Id")
            state = payload.get("State")
            status = state.get("Status") if isinstance(state, dict) else "unknown"
            return (
                container_id if isinstance(container_id, str) and container_id else None,
                status.strip() if isinstance(status, str) and status.strip() else "unknown",
            )
        if result.returncode != 0:
            return None
        raw = result.stdout.strip()
        if "|" not in raw:
            return (raw or None, "unknown")
        container_id, status = raw.split("|", 1)
        return (container_id or None, status.strip() or "unknown")

    def _start_container(self, container_name: str) -> CommandResult:
        result = self.command_runner([self.settings.docker_binary, "start", container_name])
        if self._should_use_engine_api(result):
            return self._engine_start_container(container_name)
        return result

    def _stop_container(self, container_name: str) -> CommandResult:
        result = self.command_runner([self.settings.docker_binary, "stop", container_name])
        if self._should_use_engine_api(result):
            return self._engine_stop_container(container_name)
        return result

    def _pause_container(self, container_name: str) -> CommandResult:
        result = self.command_runner([self.settings.docker_binary, "pause", container_name])
        if self._should_use_engine_api(result):
            return self._engine_pause_container(container_name)
        return result

    def _unpause_container(self, container_name: str) -> CommandResult:
        result = self.command_runner([self.settings.docker_binary, "unpause", container_name])
        if self._should_use_engine_api(result):
            return self._engine_unpause_container(container_name)
        return result

    def _restart_container(self, container_name: str) -> CommandResult:
        result = self.command_runner([self.settings.docker_binary, "restart", container_name])
        if self._should_use_engine_api(result):
            return self._engine_restart_container(container_name)
        return result

    def _remove_container(self, container_name: str) -> CommandResult:
        result = self.command_runner([self.settings.docker_binary, "rm", "-f", container_name])
        if self._should_use_engine_api(result):
            return self._engine_remove_container(container_name)
        return result

    def _container_is_missing(self, result: CommandResult) -> bool:
        text = f"{result.stderr}\n{result.stdout}".lower()
        return "no such container" in text or "no such object" in text

    def _inspect_container_env(self, container_name: str) -> dict[str, str] | None:
        result = self.command_runner(
            [
                self.settings.docker_binary,
                "container",
                "inspect",
                container_name,
                "--format",
                "{{json .Config.Env}}",
            ]
        )
        if self._should_use_engine_api(result):
            return self._engine_env_for_container(container_name)
        if result.returncode != 0:
            return None
        try:
            raw_env = json.loads(result.stdout.strip() or "[]")
        except json.JSONDecodeError:
            return None
        if not isinstance(raw_env, list):
            return None
        parsed: dict[str, str] = {}
        for item in raw_env:
            if not isinstance(item, str) or "=" not in item:
                continue
            key, value = item.split("=", 1)
            parsed[key] = value
        return parsed

    def _inspect_container_binds(self, container_name: str) -> list[str] | None:
        result = self.command_runner(
            [
                self.settings.docker_binary,
                "container",
                "inspect",
                container_name,
                "--format",
                "{{json .HostConfig.Binds}}",
            ]
        )
        if self._should_use_engine_api(result):
            payload = self._engine_inspect_container(container_name)
            if payload is None:
                return None
            host_config = payload.get("HostConfig")
            if not isinstance(host_config, dict):
                return None
            raw_binds = host_config.get("Binds")
            if not isinstance(raw_binds, list):
                return None
            return [item for item in raw_binds if isinstance(item, str)]
        if result.returncode != 0:
            return None
        try:
            raw_binds = json.loads(result.stdout.strip() or "[]")
        except json.JSONDecodeError:
            return None
        if not isinstance(raw_binds, list):
            return None
        return [item for item in raw_binds if isinstance(item, str)]

    def _inspect_container_extra_hosts(self, container_name: str) -> list[str] | None:
        result = self.command_runner(
            [
                self.settings.docker_binary,
                "container",
                "inspect",
                container_name,
                "--format",
                "{{json .HostConfig.ExtraHosts}}",
            ]
        )
        if self._should_use_engine_api(result):
            payload = self._engine_inspect_container(container_name)
            if payload is None:
                return None
            host_config = payload.get("HostConfig")
            if not isinstance(host_config, dict):
                return None
            raw_extra_hosts = host_config.get("ExtraHosts")
            if not isinstance(raw_extra_hosts, list):
                return None
            return [item for item in raw_extra_hosts if isinstance(item, str)]
        if result.returncode != 0:
            return None
        try:
            raw_extra_hosts = json.loads(result.stdout.strip() or "[]")
        except json.JSONDecodeError:
            return None
        if not isinstance(raw_extra_hosts, list):
            return None
        return [item for item in raw_extra_hosts if isinstance(item, str)]

    def _container_requires_recreate(
        self,
        *,
        assistant_id: str,
        container_name: str,
        workspace_root: Path,
        state_root: Path,
    ) -> bool:
        current_env = self._inspect_container_env(container_name)
        if current_env is None:
            return False
        expected_env = self._managed_container_env(assistant_id)
        for key, expected in expected_env.items():
            if current_env.get(key) != expected:
                return True
        for key in (
            "HTTP_PROXY",
            "http_proxy",
            "HTTPS_PROXY",
            "https_proxy",
            "NO_PROXY",
            "no_proxy",
        ):
            if key in current_env and key not in expected_env:
                return True
        if not self.settings.ironagent_hooks_enabled:
            for key in (
                "IRONAGENT_HOOKS_ENABLED",
                "IRONAGENT_SERVICE_URL",
                "IRONAGENT_FAIL_CLOSED",
                "IRONAGENT_HOOK_TIMEOUT_MS",
                "IRONAGENT_APPROVAL_POLL_MS",
                "IRONAGENT_APPROVAL_MAX_WAIT_MS",
                "IRONAGENT_API_TOKEN",
            ):
                if key in current_env:
                    return True
        current_binds = self._inspect_container_binds(container_name)
        if current_binds is None:
            return False
        expected_binds = {
            f"{state_root}:{self.settings.ironagent_state_mount_path}",
            f"{workspace_root}:{self.settings.ironagent_workspace_mount_path}",
        }
        if not expected_binds.issubset(set(current_binds)):
            return True
        expected_extra_hosts = set(self._extra_hosts())
        if not expected_extra_hosts:
            return False
        current_extra_hosts = self._inspect_container_extra_hosts(container_name)
        if current_extra_hosts is None:
            return False
        return not expected_extra_hosts.issubset(set(current_extra_hosts))

    def _create_container(
        self,
        *,
        assistant_id: str,
        container_name: str,
        workspace_root: Path,
        state_root: Path,
    ) -> CommandResult:
        published_port = self._gateway_publish_port(assistant_id)
        gateway_launch_port = self._gateway_launch_port(published_port)
        env_args: list[str] = []
        for key, value in self._managed_container_env(
            assistant_id,
            published_port=published_port,
        ).items():
            env_args.extend(["-e", f"{key}={value}"])
        extra_host_args: list[str] = []
        for entry in self._extra_hosts():
            extra_host_args.extend(["--add-host", entry])
        network_args = (
            ["--network", "host"]
            if published_port is not None
            else ["-p", self._gateway_port_binding_spec(None)]
        )
        result = self.command_runner(
            [
                self.settings.docker_binary,
                "run",
                "-d",
                "--init",
                "--name",
                container_name,
                "--label",
                f"ironagent.assistant_id={assistant_id}",
                "--label",
                "ironagent.managed_by=ironagent",
                *extra_host_args,
                *env_args,
                "-v",
                f"{state_root}:{self.settings.ironagent_state_mount_path}",
                "-v",
                f"{workspace_root}:{self.settings.ironagent_workspace_mount_path}",
                *network_args,
                self.settings.ironagent_container_image,
                "node",
                "/app/ironagent.mjs",
                "gateway",
                "--bind",
                self.settings.ironagent_gateway_bind,
                "--port",
                str(gateway_launch_port),
                "--allow-unconfigured",
            ]
        )
        if self._should_use_engine_api(result):
            return self._engine_create_container(
                assistant_id=assistant_id,
                container_name=container_name,
                workspace_root=workspace_root,
                state_root=state_root,
            )
        return result

    def _resolve_gateway_endpoint(
        self,
        container_name: str,
    ) -> tuple[str | None, int | None, str | None]:
        published_endpoint = self._published_gateway_endpoint_from_env(container_name)
        if published_endpoint is not None:
            host, port = published_endpoint
            return (host, port, f"http://{host}:{port}")
        for attempt in range(5):
            result = self.command_runner(
                [
                    self.settings.docker_binary,
                    "port",
                    container_name,
                    f"{self.settings.ironagent_gateway_port}/tcp",
                ]
            )
            if self._should_use_engine_api(result):
                endpoint = self._engine_port_endpoint(container_name)
                if endpoint is not None:
                    host, port = endpoint
                    return (host, port, f"http://{host}:{port}")
            elif result.returncode == 0:
                endpoint = self._parse_port_output(result.stdout)
                if endpoint is not None:
                    host, port = endpoint
                    return (host, port, f"http://{host}:{port}")
            if attempt < 4:
                time.sleep(0.2)
        return (None, None, None)

    def _published_gateway_endpoint_from_env(self, container_name: str) -> tuple[str, int] | None:
        if self._configured_gateway_publish_port_range() is None:
            return None
        env = self._inspect_container_env(container_name)
        if env is None:
            return None
        raw_port = env.get("IRONAGENT_GATEWAY_PUBLISHED_PORT", "").strip()
        if not raw_port:
            return None
        try:
            port = int(raw_port)
        except ValueError:
            return None
        host = self._normalize_gateway_host("0.0.0.0")
        return (host, port)

    def _parse_port_output(self, output: str) -> tuple[str, int] | None:
        fallback: tuple[str, int] | None = None
        for line in output.splitlines():
            trimmed = line.strip()
            if not trimmed:
                continue
            match = re.search(r"(?P<host>\[[^\]]+\]|[^:]+):(?P<port>\d+)$", trimmed)
            if not match:
                continue
            host = match.group("host").strip("[]")
            try:
                port = int(match.group("port"))
            except ValueError:
                continue
            normalized_host = self._normalize_gateway_host(host)
            if host in {"127.0.0.1", "localhost"}:
                return (normalized_host, port)
            if fallback is None:
                fallback = (normalized_host, port)
        return fallback

    def _gateway_port_binding_spec(self, published_port: int | None = None) -> str:
        publish_host = self._gateway_publish_host()
        if published_port is not None:
            if publish_host:
                return f"{publish_host}:{published_port}:{self.settings.ironagent_gateway_port}"
            return f"{published_port}:{self.settings.ironagent_gateway_port}"
        if publish_host:
            return f"{publish_host}::{self.settings.ironagent_gateway_port}"
        return f"::{self.settings.ironagent_gateway_port}"

    def _engine_gateway_port_binding(self, published_port: int | None = None) -> dict[str, str]:
        binding = {"HostPort": "" if published_port is None else str(published_port)}
        publish_host = self._gateway_publish_host()
        if publish_host:
            binding["HostIp"] = publish_host
        return binding

    def _gateway_publish_host(self) -> str:
        explicit_publish_host = self.settings.ironagent_gateway_publish_host.strip()
        if explicit_publish_host:
            if explicit_publish_host in {"0.0.0.0", "::", "[::]"}:
                return ""
            if explicit_publish_host == "localhost":
                return "127.0.0.1"
            return explicit_publish_host
        host = self.settings.ironagent_gateway_host.strip()
        if host in {"", "127.0.0.1", "localhost"}:
            # Publishing wildcard ports is more reliable on Docker Desktop than
            # an explicit loopback bind for dynamically created containers.
            return ""
        # In cloud deployments the advertised gateway host is often a public or
        # load-balanced address that does not exist on the node itself. Publish
        # on all local interfaces by default and let endpoint normalization keep
        # returning the configured public host to callers.
        return ""

    def _gateway_publish_port(self, assistant_id: str | None) -> int | None:
        if not assistant_id:
            return None
        configured_range = self._configured_gateway_publish_port_range()
        if configured_range is None:
            return None
        range_start, range_end = configured_range
        used_ports = self._managed_gateway_ports()
        range_size = range_end - range_start + 1
        seed = int.from_bytes(sha256(assistant_id.encode("utf-8")).digest()[:4], "big") % range_size
        for offset in range(range_size):
            candidate = range_start + ((seed + offset) % range_size)
            if candidate not in used_ports:
                return candidate
        raise RuntimeError(
            "no free published gateway ports available in configured range "
            f"{range_start}-{range_end}"
        )

    def _gateway_launch_port(self, published_port: int | None) -> int:
        if published_port is not None:
            return published_port
        return self.settings.ironagent_gateway_port

    def _configured_gateway_publish_port_range(self) -> tuple[int, int] | None:
        range_start = self.settings.ironagent_gateway_publish_port_start
        range_end = self.settings.ironagent_gateway_publish_port_end
        if range_start <= 0 and range_end <= 0:
            return None
        if range_start <= 0:
            range_start = range_end
        if range_end <= 0:
            range_end = range_start
        if range_start > range_end:
            range_start, range_end = range_end, range_start
        return (range_start, range_end)

    def _managed_gateway_ports(self) -> set[int]:
        result = self.command_runner(
            [
                self.settings.docker_binary,
                "ps",
                "-a",
                "--filter",
                "label=ironagent.managed_by=ironagent",
                "--format",
                "{{.Names}}",
            ]
        )
        if self._should_use_engine_api(result) or result.returncode != 0:
            return set()
        ports: set[int] = set()
        for container_name in result.stdout.splitlines():
            name = container_name.strip()
            if not name:
                continue
            port_result = self.command_runner(
                [
                    self.settings.docker_binary,
                    "port",
                    name,
                    f"{self.settings.ironagent_gateway_port}/tcp",
                ]
            )
            endpoint: tuple[str, int] | None = None
            if self._should_use_engine_api(port_result):
                endpoint = self._engine_port_endpoint(name)
            elif port_result.returncode == 0:
                endpoint = self._parse_port_output(port_result.stdout)
            if endpoint is not None:
                ports.add(endpoint[1])
                continue
            env = self._inspect_container_env(name)
            if env is None:
                continue
            raw_port = env.get("IRONAGENT_GATEWAY_PUBLISHED_PORT", "").strip()
            if not raw_port:
                continue
            try:
                ports.add(int(raw_port))
            except ValueError:
                continue
        return ports

    def _normalize_gateway_host(self, host: str) -> str:
        trimmed = host.strip()
        if trimmed in {"", "0.0.0.0", "::", "[::]"}:
            preferred = self.settings.ironagent_gateway_host.strip()
            if preferred in {"", "0.0.0.0", "::", "[::]", "localhost"}:
                return "127.0.0.1"
            return preferred
        if trimmed == "localhost":
            return "127.0.0.1"
        return trimmed

    def _error_text(self, result: CommandResult, fallback: str) -> str:
        return result.stderr.strip() or result.stdout.strip() or fallback

    def probe_runtime(self, *, assistant_id: str) -> RuntimeProbeResult:
        container_name = self._container_name_for(assistant_id)
        inspected = self._inspect_container_with_health(container_name)
        if inspected is None:
            return RuntimeProbeResult(
                container_name=container_name,
                container_exists=False,
                container_state="missing",
                status="failed",
                last_error="container not found",
            )

        container_id, container_state, health_state = inspected
        status: RuntimeStatusName = "failed"
        last_error = ""
        if container_state == "running":
            if health_state == "starting":
                status = "starting"
            elif health_state == "unhealthy":
                status = "failed"
                last_error = "container health is unhealthy"
            else:
                status = "running"
        elif container_state == "paused":
            status = "paused"
        elif container_state in {"created", "restarting"}:
            status = "starting"
        elif container_state in {"exited", "dead"}:
            status = "stopped"
            last_error = f"container state is {container_state}"
        else:
            status = "failed"
            last_error = f"container state is {container_state}"

        gateway_host: str | None = None
        gateway_port: int | None = None
        gateway_url: str | None = None
        if container_state == "running":
            gateway_host, gateway_port, gateway_url = self._resolve_gateway_endpoint(container_name)
            if gateway_url is None and not last_error:
                last_error = "gateway endpoint not published yet"

        return RuntimeProbeResult(
            container_name=container_name,
            container_exists=True,
            container_id=container_id,
            container_state=container_state,
            health_state=health_state,
            status=status,
            gateway_host=gateway_host,
            gateway_port=gateway_port,
            gateway_url=gateway_url,
            last_error=last_error,
        )

    def _inspect_container_with_health(
        self,
        container_name: str,
    ) -> tuple[str | None, str, str | None] | None:
        result = self.command_runner(
            [
                self.settings.docker_binary,
                "container",
                "inspect",
                container_name,
                "--format",
                "{{json .State}}|{{.Id}}",
            ]
        )
        if self._should_use_engine_api(result):
            payload = self._engine_inspect_container(container_name)
            if payload is None:
                return None
            container_id = payload.get("Id")
            state = payload.get("State")
            if not isinstance(state, dict):
                return (
                    container_id if isinstance(container_id, str) and container_id else None,
                    "unknown",
                    None,
                )
            container_state = state.get("Status")
            health = state.get("Health")
            health_state: str | None = None
            if isinstance(health, dict):
                raw_health_status = health.get("Status")
                if isinstance(raw_health_status, str) and raw_health_status.strip():
                    health_state = raw_health_status.strip()
            return (
                container_id if isinstance(container_id, str) and container_id else None,
                (
                    container_state.strip()
                    if isinstance(container_state, str) and container_state.strip()
                    else "unknown"
                ),
                health_state,
            )
        if result.returncode != 0:
            return None
        raw = result.stdout.strip()
        if not raw or "|" not in raw:
            return (None, "unknown", None)
        raw_state, _, raw_container_id = raw.partition("|")
        try:
            state = json.loads(raw_state)
        except json.JSONDecodeError:
            return (None, "unknown", None)
        if not isinstance(state, dict):
            return (None, "unknown", None)
        container_id = raw_container_id.strip()
        raw_container_state = state.get("Status")
        container_state = (
            raw_container_state.strip()
            if isinstance(raw_container_state, str) and raw_container_state.strip()
            else "unknown"
        )
        raw_health = state.get("Health")
        health_state = ""
        if isinstance(raw_health, dict):
            raw_health_status = raw_health.get("Status")
            if isinstance(raw_health_status, str):
                health_state = raw_health_status.strip()
        return (
            container_id or None,
            container_state or "unknown",
            health_state or None,
        )
