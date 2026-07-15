from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.client import HTTPConnection, HTTPResponse
from pathlib import Path
from shutil import copy2, copytree, rmtree
from typing import Any
from urllib.parse import urlparse

from src.security.capability import load_capability_profile
from src.security.config_loader import capability_file
from src.shared.config import Settings, get_settings, resolve_workspace_file_metadata_backend
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


IRON_AGENT_RUNTIME_UID = 1000
IRON_AGENT_RUNTIME_GID = 1000
RUNTIME_COMMAND_TIMEOUT_SECONDS = 15
DEFAULT_AGENTS_PLACEHOLDER = (
    "# Local Assistant Guidance\n\nPlace local instructions for this assistant here.\n"
)
# Placeholder written into ironagent.json's provider `apiKey` for
# openai-completions providers — the real key lives in headers.Authorization.
# Never recover this value as a real credential when reading config back.
RUNTIME_API_KEY_PLACEHOLDER = "***"
# The admin UI echoes this back in place of a configured-but-hidden key.
UI_MASKED_API_KEY = "******"
logger = get_logger("runtime.provisioner")


def is_placeholder_api_key(value: str | None) -> bool:
    """True when the value is a non-credential placeholder, not a real key.

    Covers the runtime apiKey placeholder ("***", which can leak back into stored
    config via runtime-state recovery) and the admin UI mask ("******"). Such a
    value must never be written into ironagent.json or persisted as a credential.
    """
    return (value or "").strip() in {RUNTIME_API_KEY_PLACEHOLDER, UI_MASKED_API_KEY}

WILDCARD_GATEWAY_HOSTS = {"", "0.0.0.0", "::", "[::]"}
OPENAI_CODEX_AUTH_FAILURE_MARKERS = (
    "oauth token refresh failed for openai-codex",
    "refresh_token_reused",
    "invalid_grant",
    "token refresh failed: 401",
)
OPENAI_CODEX_AUTH_FAILURE_MESSAGE = (
    "OpenAI Codex OAuth token refresh failed; re-authenticate the host IronAgent account"
)


@dataclass(slots=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[list[str]], CommandResult]

COMMON_RUNTIME_BINARY_PATHS = {
    "docker": ("/usr/local/bin/docker", "/opt/homebrew/bin/docker"),
}


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


def _resolve_runtime_binary(command: str) -> str:
    if os.path.sep in command:
        return command
    resolved = shutil.which(command)
    if resolved:
        return resolved
    for candidate in COMMON_RUNTIME_BINARY_PATHS.get(command, ()):
        if Path(candidate).exists():
            return candidate
    return command


def default_command_runner(args: list[str]) -> CommandResult:
    resolved_args = [_resolve_runtime_binary(args[0]), *args[1:]] if args else args
    try:
        completed = subprocess.run(
            resolved_args,
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
                f"command timed out after {RUNTIME_COMMAND_TIMEOUT_SECONDS}s: " + " ".join(args[:3])
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

    def _engine_exec_container(self, container_name: str, cmd: list[str]) -> CommandResult:
        status_code, raw = self._engine_request(
            "POST",
            f"/containers/{container_name}/exec",
            payload={
                "AttachStdout": True,
                "AttachStderr": True,
                "Cmd": cmd,
            },
        )
        if status_code < 200 or status_code >= 300:
            return CommandResult(returncode=1, stderr=raw)
        try:
            created = json.loads(raw)
        except json.JSONDecodeError:
            return CommandResult(returncode=1, stderr=raw)
        exec_id = created.get("Id")
        if not isinstance(exec_id, str) or not exec_id.strip():
            return CommandResult(returncode=1, stderr="missing exec id from Docker Engine API")

        status_code, output = self._engine_request(
            "POST",
            f"/exec/{exec_id}/start",
            payload={"Detach": False, "Tty": False},
        )
        if status_code < 200 or status_code >= 300:
            return CommandResult(returncode=1, stderr=output)

        deadline = time.monotonic() + RUNTIME_COMMAND_TIMEOUT_SECONDS
        inspect_raw = ""
        while time.monotonic() < deadline:
            status_code, inspect_raw = self._engine_request("GET", f"/exec/{exec_id}/json")
            if status_code < 200 or status_code >= 300:
                return CommandResult(returncode=1, stdout=output, stderr=inspect_raw)
            try:
                inspected = json.loads(inspect_raw)
            except json.JSONDecodeError:
                return CommandResult(returncode=1, stdout=output, stderr=inspect_raw)
            if not bool(inspected.get("Running")):
                exit_code = inspected.get("ExitCode")
                return CommandResult(
                    returncode=exit_code if isinstance(exit_code, int) else 1,
                    stdout=output,
                )
            time.sleep(0.1)
        return CommandResult(returncode=124, stdout=output, stderr="docker exec timed out")

    def _engine_container_logs(self, container_name: str, *, tail: int = 120) -> CommandResult:
        status_code, raw = self._engine_request(
            "GET",
            f"/containers/{container_name}/logs?stdout=1&stderr=1&tail={tail}",
        )
        if 200 <= status_code < 300:
            return CommandResult(returncode=0, stdout=raw)
        return CommandResult(returncode=1, stderr=raw)

    def _engine_create_container(
        self,
        *,
        assistant_id: str,
        container_name: str,
        workspace_root: Path,
        state_root: Path,
    ) -> CommandResult:
        env_values = [
            f"{key}={value}" for key, value in self._managed_container_env(assistant_id).items()
        ]
        binds = [
            f"{state_root}:{self.settings.iron_agent_state_mount_path}",
            f"{workspace_root}:{self.settings.iron_agent_workspace_mount_path}",
            *self._shared_workspace_metadata_binds(),
        ]
        cmd = [
            "node",
            "/app/ironagent.mjs",
            "gateway",
            "--bind",
            self.settings.iron_agent_gateway_bind,
            "--port",
            str(self.settings.iron_agent_gateway_port),
            "--allow-unconfigured",
        ]
        payload: dict[str, Any] = {
            "Image": self.settings.iron_agent_container_image,
            "Cmd": cmd,
            "Env": env_values,
            "WorkingDir": self.settings.iron_agent_workspace_mount_path,
            "ExposedPorts": {f"{self.settings.iron_agent_gateway_port}/tcp": {}},
            "Labels": {
                "ironagent.assistant_id": assistant_id,
                "ironagent.managed_by": "ironagent",
            },
            "HostConfig": {
                "Init": True,
                "Binds": binds,
                "ExtraHosts": self._extra_hosts(),
                "PortBindings": {
                    f"{self.settings.iron_agent_gateway_port}/tcp": [
                        self._engine_gateway_port_binding()
                    ]
            },
                },
        }
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
        mappings = ports.get(f"{self.settings.iron_agent_gateway_port}/tcp")
        if not isinstance(mappings, list):
            return None
        candidates: list[tuple[str, int]] = []
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
            candidates.append((host, port))
        return self._select_gateway_endpoint(candidates)

    def ensure_runtime(
        self,
        *,
        assistant_id: str,
        assistant_name: str,
        capability_profile: str = "onlyread",
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
        try:
            config_changed = self._write_iron_agent_config(
                assistant_id=assistant_id,
                state_root=state_root,
                gateway_token=gateway_token,
                capability_profile=capability_profile,
                model_config=model_config,
            )
        except ValueError as exc:
            return ProvisionResult(
                container_name=self._container_name_for(assistant_id),
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
        auth_changed = self._sync_host_iron_agent_auth(
            state_root=state_root,
            model_config=model_config,
        )
        provider_auth_changed = self._sync_provider_api_key_auth(
            assistant_id=assistant_id,
            state_root=state_root,
            model_config=model_config,
        )
        shared_workspace_metadata_host_dir = self._shared_workspace_metadata_host_dir()
        if workspace_layout.permissions_needed:
            self._ensure_runtime_mount_permissions(workspace_root)
        if state_root_layout.permissions_needed:
            self._ensure_runtime_mount_permissions(state_root)
        if shared_workspace_metadata_host_dir is not None:
            self._ensure_runtime_mount_permissions(shared_workspace_metadata_host_dir)
        runtime_changed = config_changed or auth_changed or provider_auth_changed
        container_name, inspect = self._inspect_existing_container(assistant_id)
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
                        image=self.settings.iron_agent_container_image,
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
                        image=self.settings.iron_agent_container_image,
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
                status, last_error = self._gateway_runtime_status(gateway_url)
                return ProvisionResult(
                    container_name=container_name,
                    container_id=created.stdout.strip() or None,
                    workspace_root=str(workspace_root),
                    workspace_root_id=workspace_layout.root_id,
                    workspace_slug=workspace_layout.workspace_slug,
                    temp_dir=workspace_layout.temp_dir,
                    persistent_dir=workspace_layout.persistent_dir,
                    state_root=str(state_root),
                    status=status,
                    image=self.settings.iron_agent_container_image,
                    gateway_host=gateway_host,
                    gateway_port=gateway_port,
                    gateway_url=gateway_url,
                    gateway_token=gateway_token,
                    last_error=last_error,
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
                        image=self.settings.iron_agent_container_image,
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
                        image=self.settings.iron_agent_container_image,
                        gateway_token=gateway_token,
                        last_error=self._error_text(
                            restarted,
                            "failed to restart container",
                        ),
                        updated_at=utc_now(),
                    )
            gateway_host, gateway_port, gateway_url = self._resolve_gateway_endpoint(container_name)
            status, last_error = self._gateway_runtime_status(gateway_url)
            return ProvisionResult(
                container_name=container_name,
                container_id=container_id,
                workspace_root=str(workspace_root),
                workspace_root_id=workspace_layout.root_id,
                workspace_slug=workspace_layout.workspace_slug,
                temp_dir=workspace_layout.temp_dir,
                persistent_dir=workspace_layout.persistent_dir,
                state_root=str(state_root),
                status=status,
                image=self.settings.iron_agent_container_image,
                gateway_host=gateway_host,
                gateway_port=gateway_port,
                gateway_url=gateway_url,
                gateway_token=gateway_token,
                last_error=last_error,
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
                image=self.settings.iron_agent_container_image,
                gateway_token=gateway_token,
                last_error=self._error_text(created, "failed to create container"),
                updated_at=utc_now(),
            )

        gateway_host, gateway_port, gateway_url = self._resolve_gateway_endpoint(container_name)
        status, last_error = self._gateway_runtime_status(gateway_url)
        return ProvisionResult(
            container_name=container_name,
            container_id=created.stdout.strip() or None,
            workspace_root=str(workspace_root),
            workspace_root_id=workspace_layout.root_id,
            workspace_slug=workspace_layout.workspace_slug,
            temp_dir=workspace_layout.temp_dir,
            persistent_dir=workspace_layout.persistent_dir,
            state_root=str(state_root),
            status=status,
            image=self.settings.iron_agent_container_image,
            gateway_host=gateway_host,
            gateway_port=gateway_port,
            gateway_url=gateway_url,
            gateway_token=gateway_token,
            last_error=last_error,
            updated_at=utc_now(),
        )

    def destroy_runtime(
        self,
        *,
        assistant_id: str,
        workspace_root: str | None = None,
        state_root: str | None = None,
    ) -> DestroyRuntimeResult:
        removed_container_name = self._container_name_for(assistant_id)
        container_removed = False
        for container_name in self._container_name_candidates_for(assistant_id):
            removed = self._remove_container(container_name)
            if removed.returncode == 0:
                removed_container_name = container_name
                container_removed = True
                continue
            if not self._container_is_missing(removed):
                raise ValueError(self._error_text(removed, "failed to remove container"))

        workspace_removed = self._remove_workspace_root(workspace_root)
        state_removed = self._remove_state_root(state_root)
        return DestroyRuntimeResult(
            container_name=removed_container_name,
            container_removed=container_removed,
            workspace_removed=workspace_removed,
            state_removed=state_removed,
        )

    def _container_name_for(self, assistant_id: str) -> str:
        prefix = self.settings.assistant_container_name_prefix.strip()
        return self._container_name_for_prefix(
            prefix or "ironagent-assistant",
            assistant_id,
        )

    def _container_name_for_prefix(self, prefix: str, assistant_id: str) -> str:
        safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in assistant_id.strip())
        safe = "-".join(part for part in safe.split("-") if part)
        return f"{prefix}-{safe or 'assistant'}"

    def _legacy_container_name_for(self, assistant_id: str) -> str:
        return self._container_name_for_prefix("ironagent-assistant", assistant_id)

    def _container_name_candidates_for(self, assistant_id: str) -> tuple[str, ...]:
        primary = self._container_name_for(assistant_id)
        if self.settings.assistant_container_name_prefix.strip() in {"", "ironagent-assistant"}:
            legacy = self._legacy_container_name_for(assistant_id)
            if legacy != primary:
                return (primary, legacy)
        return (primary,)

    def _inspect_existing_container(
        self,
        assistant_id: str,
    ) -> tuple[str, tuple[str | None, str] | None]:
        candidates = self._container_name_candidates_for(assistant_id)
        for container_name in candidates:
            inspected = self._inspect_container(container_name)
            if inspected is not None:
                return container_name, inspected
        return candidates[0], None

    def _inspect_existing_container_with_health(
        self,
        assistant_id: str,
    ) -> tuple[str, tuple[str | None, str, str | None] | None]:
        candidates = self._container_name_candidates_for(assistant_id)
        for container_name in candidates:
            inspected = self._inspect_container_with_health(container_name)
            if inspected is not None:
                return container_name, inspected
        return candidates[0], None

    def _operation_container_name_for(self, assistant_id: str) -> str:
        candidates = self._container_name_candidates_for(assistant_id)
        if len(candidates) == 1:
            return candidates[0]
        return self._inspect_existing_container(assistant_id)[0]

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
        resolved_container_name = container_name or self._operation_container_name_for(assistant_id)
        reload_marker_paths = [
            f"{self.settings.iron_agent_workspace_mount_path}/.ironagent-capabilities-reload",
        ]
        result = self.command_runner(
            [
                self.settings.docker_binary,
                "exec",
                resolved_container_name,
                "touch",
                *reload_marker_paths,
            ]
        )
        if self._should_use_engine_api(result):
            result = self._engine_exec_container(
                resolved_container_name,
                ["touch", *reload_marker_paths],
            )
        return result.returncode == 0

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
        resolved_container_name = container_name or self._operation_container_name_for(assistant_id)
        skill_path = f"{self.settings.iron_agent_workspace_mount_path}/skills/{slug}/SKILL.md"
        result = self.command_runner(
            [
                self.settings.docker_binary,
                "exec",
                resolved_container_name,
                "touch",
                skill_path,
            ]
        )
        if self._should_use_engine_api(result):
            result = self._engine_exec_container(
                resolved_container_name,
                ["touch", skill_path],
            )
        return result.returncode == 0

    def restart_runtime(self, *, assistant_id: str) -> RuntimeProbeResult:
        container_name, inspected = self._inspect_existing_container(assistant_id)
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

        return self._wait_for_runtime_probe(
            assistant_id=assistant_id,
            transient_statuses={"starting"},
            tolerate_unhealthy=True,
        )

    def start_runtime(self, *, assistant_id: str) -> RuntimeProbeResult:
        container_name, inspected = self._inspect_existing_container(assistant_id)
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

        latest_probe = self._wait_for_runtime_probe(
            assistant_id=assistant_id,
            transient_statuses={"starting", "paused"},
            tolerate_unhealthy=True,
        )
        if (
            latest_probe.status != "failed"
            or latest_probe.last_error != "container health is unhealthy"
        ):
            return latest_probe

        restarted = self._restart_container(container_name)
        if restarted.returncode != 0:
            return RuntimeProbeResult(
                container_name=container_name,
                container_exists=True,
                container_id=container_id,
                container_state=container_state,
                status="failed",
                last_error=self._error_text(
                    restarted,
                    "failed to restart unhealthy container",
                ),
            )
        return self._wait_for_runtime_probe(
            assistant_id=assistant_id,
            transient_statuses={"starting"},
            tolerate_unhealthy=True,
        )

    def stop_runtime(self, *, assistant_id: str) -> RuntimeProbeResult:
        container_name, inspected = self._inspect_existing_container(assistant_id)
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
        container_name, inspected = self._inspect_existing_container(assistant_id)
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
        raw = self.settings.iron_agent_container_extra_host.strip()
        if not raw:
            return []
        return [raw]

    def _managed_container_env(self, assistant_id: str) -> dict[str, str]:
        api_url = self._managed_iron_agent_api_url()
        managed = {
            "HOME": "/home/node",
            "TERM": "xterm-256color",
            "IRON_AGENT_ASSISTANT_ID": assistant_id,
            "IRON_AGENT_RUNTIME_CONTAINER_IMAGE": self.settings.iron_agent_container_image,
            "IRON_AGENT_STATE_DIR": self.settings.iron_agent_state_mount_path,
            "IRON_AGENT_CONFIG_PATH": f"{self.settings.iron_agent_state_mount_path}/ironagent.json",
            "IRON_AGENT_WORKSPACE_DIR": self.settings.iron_agent_workspace_mount_path,
            "IRON_AGENT_BUNDLED_SKILLS_DIR": (
                f"{self.settings.iron_agent_workspace_mount_path.rstrip('/')}/skills"
            ),
            "IRON_AGENT_WORKSPACE_SKILLS_ONLY": "1",
            "IRON_AGENT_WORKSPACE_PLUGINS_ONLY": "1",
            # IronAgent API configuration for built-in Control UI
            "IRON_AGENT_API_URL": api_url,
        }
        shared_workspace_sqlite_path = self._shared_workspace_metadata_container_db_path()
        if shared_workspace_sqlite_path:
            managed.update(
                {
                    "IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND": "sqlite",
                    "IRON_AGENT_WORKSPACE_FILE_SQLITE_PATH": shared_workspace_sqlite_path,
                }
            )
        if self.settings.iron_agent_hooks_enabled:
            managed.update(
                {
                    "IRON_AGENT_HOOKS_ENABLED": "true",
                    "IRON_AGENT_SERVICE_URL": self._managed_hook_service_url(),
                    "IRON_AGENT_FAIL_CLOSED": (
                        "true" if self.settings.iron_agent_hook_fail_closed else "false"
                    ),
                    "IRON_AGENT_HOOK_TIMEOUT_MS": str(self.settings.iron_agent_hook_timeout_ms),
                    "IRON_AGENT_APPROVAL_POLL_MS": str(self.settings.iron_agent_approval_poll_ms),
                    "IRON_AGENT_APPROVAL_MAX_WAIT_MS": str(
                        self.settings.iron_agent_approval_max_wait_ms
                    ),
                }
            )
            if self.settings.iron_agent_hook_api_token.strip():
                managed["IRON_AGENT_API_TOKEN"] = self.settings.iron_agent_hook_api_token.strip()
        managed.update(self._managed_proxy_env())
        managed.update(
            {
                key.replace("IRON_AGENT_", "IRON_AGENT_", 1): value
                for key, value in managed.items()
                if key.startswith("IRON_AGENT_")
            }
        )
        return managed

    def _shared_workspace_metadata_host_dir(self) -> Path | None:
        if resolve_workspace_file_metadata_backend(settings=self.settings) != "sqlite":
            return None
        sqlite_path = self.settings.workspace_file_sqlite_path.resolve()
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite_path.parent

    def _shared_workspace_metadata_container_dir(self) -> str | None:
        if resolve_workspace_file_metadata_backend(settings=self.settings) != "sqlite":
            return None
        return self.settings.iron_agent_workspace_file_sqlite_mount_path.rstrip("/")

    def _shared_workspace_metadata_container_db_path(self) -> str | None:
        container_dir = self._shared_workspace_metadata_container_dir()
        if not container_dir:
            return None
        return f"{container_dir}/{self.settings.workspace_file_sqlite_path.name}"

    def _shared_workspace_metadata_binds(self) -> list[str]:
        host_dir = self._shared_workspace_metadata_host_dir()
        container_dir = self._shared_workspace_metadata_container_dir()
        if host_dir is None or not container_dir:
            return []
        return [f"{host_dir}:{container_dir}"]

    def _managed_hook_service_url(self) -> str:
        configured_url = self.settings.iron_agent_hook_service_url.strip()
        public_url = self._managed_iron_agent_api_url().rstrip("/")
        # Managed assistant containers run outside Kubernetes service DNS, so a
        # cluster-internal hook URL must be translated to the public service URL.
        if public_url and (
            public_url != "http://127.0.0.1:8000"
            and (not configured_url or self._is_internal_service_url(configured_url))
        ):
            return public_url
        return configured_url

    def _managed_iron_agent_api_url(self) -> str:
        for value in (
            self.settings.go_gateway_public_url,
            self.settings.security_service_public_url,
        ):
            normalized = value.strip().rstrip("/")
            if normalized:
                return normalized
        return "http://127.0.0.1:8000"

    def _is_internal_service_url(self, value: str) -> bool:
        host = self._hostname_from_url(value)
        if not host:
            return False
        normalized = host.lower().strip(".")
        return (
            normalized in {"security-service", "admin"}
            or normalized.endswith(".svc")
            or normalized.endswith(".cluster.local")
        )

    def _managed_proxy_env(self) -> dict[str, str]:
        proxy_env: dict[str, str] = {}
        http_proxy = self.settings.iron_agent_http_proxy.strip()
        https_proxy = self.settings.iron_agent_https_proxy.strip()
        configured_no_proxy = self.settings.iron_agent_no_proxy.strip()
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
            item.strip() for item in self.settings.iron_agent_no_proxy.split(",") if item.strip()
        ]
        required_entries = ["127.0.0.1", "localhost", "::1", "host.docker.internal"]
        for hook_host in [
            self._hostname_from_url(self.settings.iron_agent_hook_service_url),
            self._hostname_from_url(self._managed_hook_service_url()),
        ]:
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
            DEFAULT_AGENTS_PLACEHOLDER,
        )
        self._seed_workspace_templates(
            workspace_root,
            assistant_id=assistant_id,
            assistant_name=assistant_name,
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
        source_root = self.settings.iron_agent_source_root
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
        raw = self.settings.iron_agent_seed_workspace_skills.strip()
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

    def _seed_workspace_templates(
        self,
        workspace_root: Path,
        *,
        assistant_id: str,
        assistant_name: str,
    ) -> None:
        source_root = self.settings.iron_agent_source_root
        templates_root = (
            source_root / "docs" / "reference" / "templates" if source_root is not None else None
        )
        if templates_root is not None and templates_root.exists():
            for template_path in sorted(templates_root.glob("*.md")):
                target_path = workspace_root / template_path.name
                if template_path.name == "AGENTS.md":
                    self._copy_if_missing_or_placeholder(
                        template_path,
                        target_path,
                        placeholder=DEFAULT_AGENTS_PLACEHOLDER,
                    )
                    continue
                self._copy_if_missing(template_path, target_path)

        fallback_templates = {
            "IDENTITY.md": (
                f"# {assistant_name}\n\n"
                f"Assistant ID: `{assistant_id}`\n\n"
                "Describe this assistant's identity, role, and collaboration style here.\n"
            ),
            "SOUL.md": (
                f"# {assistant_name} Soul\n\n"
                "This file records the durable operating principles for "
                f"assistant `{assistant_id}`.\n"
            ),
        }
        for filename, content in fallback_templates.items():
            self._write_if_missing(workspace_root / filename, content)

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
                os.chown(path, IRON_AGENT_RUNTIME_UID, IRON_AGENT_RUNTIME_GID)
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

    def _control_ui_allowed_origins(self) -> list[str]:
        settings = self.settings
        candidates: list[str] = []
        configured_url = settings.control_ui_public_url.strip()
        if configured_url:
            candidates.append(configured_url)
        else:
            gateway_host = settings.iron_agent_gateway_host.strip() or "127.0.0.1"
            if gateway_host in {"0.0.0.0", "::", "[::]"}:
                gateway_host = "127.0.0.1"
            gateway_port = settings.iron_agent_gateway_port or 18789
            candidates.extend(
                [
                    f"http://{gateway_host}:{gateway_port}",
                    "http://127.0.0.1:19001",
                    "http://localhost:19001",
                ]
            )
        raw_allowed_origins = settings.control_ui_allowed_origins_json.strip()
        if raw_allowed_origins:
            try:
                parsed = json.loads(raw_allowed_origins)
            except json.JSONDecodeError:
                parsed = []
            if isinstance(parsed, list):
                candidates.extend(str(item).strip() for item in parsed if str(item).strip())

        origins: list[str] = []
        for candidate in candidates:
            parsed = urlparse(candidate)
            if not parsed.scheme or not parsed.netloc:
                continue
            hostname = parsed.hostname or ""
            port = parsed.port
            alt_hostname = ""
            if hostname == "127.0.0.1":
                alt_hostname = "localhost"
            elif hostname == "localhost":
                alt_hostname = "127.0.0.1"

            variants = [parsed.netloc]
            if alt_hostname:
                alt_netloc = f"{alt_hostname}:{port}" if port is not None else alt_hostname
                variants.append(alt_netloc)

            for netloc in variants:
                origin = f"{parsed.scheme}://{netloc}"
                if origin not in origins:
                    origins.append(origin)
        return origins

    def _write_iron_agent_config(
        self,
        *,
        assistant_id: str,
        state_root: Path,
        gateway_token: str,
        capability_profile: str,
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
        allowed_origins = self._control_ui_allowed_origins()
        if allowed_origins:
            control_ui["allowedOrigins"] = allowed_origins
        else:
            control_ui.pop("allowedOrigins", None)
        control_ui["dangerouslyAllowHostHeaderOriginFallback"] = True
        control_ui["dangerouslyDisableDeviceAuth"] = True
        allowed_origins = [
            str(origin).strip()
            for origin in control_ui.get("allowedOrigins", [])
            if str(origin).strip()
        ]
        for origin in self._control_ui_allowed_origins():
            if origin not in allowed_origins:
                allowed_origins.append(origin)
        control_ui["allowedOrigins"] = allowed_origins
        payload.pop("ironagent", None)

        agents = payload.setdefault("agents", {})
        if not isinstance(agents, dict):
            agents = {}
            payload["agents"] = agents
        defaults = agents.setdefault("defaults", {})
        if not isinstance(defaults, dict):
            defaults = {}
            agents["defaults"] = defaults
        defaults["workspace"] = self.settings.iron_agent_workspace_mount_path

        # Each managed assistant container represents a single IronAgent agent.
        # Register it explicitly so agent-scoped session keys like
        # `agent:<assistant_id>:main` resolve inside the gateway.
        agent_id = assistant_id.strip().lower()
        agent_list = agents.get("list")
        if not isinstance(agent_list, list):
            agent_list = []
        normalized_entries: list[dict[str, object]] = []
        for entry in agent_list:
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("id", "")).strip().lower()
            if entry_id == agent_id:
                continue
            entry_copy = dict(entry)
            entry_copy["default"] = False
            normalized_entries.append(entry_copy)
        normalized_entries.insert(
            0,
            {
                "id": agent_id or "main",
                "name": assistant_id.strip() or "main",
                "default": True,
                "workspace": self.settings.iron_agent_workspace_mount_path,
            },
        )
        agents["list"] = normalized_entries

        tools = payload.setdefault("tools", {})
        if not isinstance(tools, dict):
            tools = {}
            payload["tools"] = tools
        normalized_profile = capability_profile.strip() or "onlyread"
        allowed_tools = self._tool_allowlist_for_profile(normalized_profile)
        tools.pop("capability_profile", None)
        tools["allow"] = allowed_tools
        if "exec" in allowed_tools:
            exec_settings = tools.setdefault("exec", {})
            if not isinstance(exec_settings, dict):
                exec_settings = {}
                tools["exec"] = exec_settings
            # IronAgent owns the approval decision for managed assistant containers, so
            # disable IronAgent's built-in host-exec approval prompts here.
            exec_settings["security"] = "full"
            exec_settings["ask"] = "off"
        else:
            tools.pop("exec", None)

        effective_provider_id = self._provider_id_from_config(model_config)
        has_custom_provider_config = self._has_custom_provider_config(model_config)
        if model_config is not None and model_config.auth_mode == "provider_api_key":
            # Fail closed: never serialize a half-formed provider block. A missing
            # provider id or api key would drop authHeader/headers (and the model
            # allowlist) and leave the runtime without working credentials, so
            # surface a visible provision failure instead of a silent 401.
            if not effective_provider_id or not model_config.provider_api_key.strip():
                raise ValueError(
                    "provider_api_key auth mode requires a resolvable provider id "
                    "and a non-empty provider_api_key; refusing to write an "
                    "incomplete ironagent.json"
                )
            if is_placeholder_api_key(model_config.provider_api_key):
                raise ValueError(
                    "provider_api_key is a placeholder ('***'/'******'), not a real "
                    "credential; refusing to write an unusable ironagent.json"
                )
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
            provider_api = self._provider_api_from_config(
                effective_provider_id=effective_provider_id,
                model_config=model_config,
            )
            if provider_api:
                provider_settings["api"] = provider_api
            if model_config is not None and model_config.provider_base_url.strip():
                provider_settings["baseUrl"] = model_config.provider_base_url.strip()
            if model_config is not None and model_config.provider_api_key.strip():
                provider_api_key = model_config.provider_api_key.strip()
                if provider_api == "openai-completions":
                    provider_settings["apiKey"] = RUNTIME_API_KEY_PLACEHOLDER
                    # The managed LLM gateway expects the raw API key in the
                    # Authorization header (no "Bearer " scheme), so keep
                    # authHeader off and pass the key through verbatim.
                    provider_settings["authHeader"] = False
                    provider_settings["headers"] = {"Authorization": provider_api_key}
                else:
                    provider_settings["apiKey"] = provider_api_key
            provider_settings["models"] = self._provider_model_entries_from_config(
                _effective_provider_id=effective_provider_id,
                model_config=model_config,
            )
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

    def _tool_allowlist_for_profile(self, profile_name: str) -> list[str]:
        normalized = profile_name.strip() or "onlyread"
        try:
            profile = load_capability_profile(capability_file(normalized))
        except Exception as exc:
            logger.warning(
                "capability_profile_load_failed",
                profile_name=normalized,
                error=f"{type(exc).__name__}: {exc}",
            )
            return []
        return sorted(profile.allowed_tools | profile.approval_tools)

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

    def _provider_model_entries_from_config(
        self,
        *,
        _effective_provider_id: str,
        model_config: AssistantIronAgentConfig | None,
    ) -> list[dict[str, str]]:
        if model_config is None or not model_config.primary_model.strip():
            return []
        primary_model = self._effective_primary_model(model_config)
        _provider_id, separator, model_id = primary_model.partition("/")
        if separator and model_id.strip():
            model_name = model_id.strip()
        else:
            model_name = primary_model.strip()
        return [{"id": model_name, "name": model_name}]

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

    def _provider_api_from_config(
        self,
        *,
        effective_provider_id: str,
        model_config: AssistantIronAgentConfig | None,
    ) -> str | None:
        if model_config is None or model_config.auth_mode != "provider_api_key":
            return None
        if effective_provider_id == "ollama":
            # Force the native Ollama API so tool calling does not accidentally
            # fall back to an OpenAI-compatible /v1 endpoint.
            return "ollama"
        if (
            effective_provider_id
            or model_config.provider_base_url.strip()
            or model_config.provider_api_key.strip()
        ):
            # The admin-managed Provider API Key flow currently captures
            # OpenAI-compatible providers such as Model Studio. Persist the
            # adapter explicitly so managed assistant runtimes do not fall back
            # to the wrong protocol when the provider-specific plugin defaults
            # are unavailable at runtime.
            return "openai-completions"
        return None

    def _merge_host_auth_profiles(
        self,
        *,
        payload: dict[str, object],
        model_config: AssistantIronAgentConfig | None,
    ) -> None:
        if not self._uses_host_openai_codex_auth(model_config):
            return
        host_config_path = self.settings.host_iron_agent_root / "ironagent.json"
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

    def _sync_host_iron_agent_auth(
        self,
        *,
        state_root: Path,
        model_config: AssistantIronAgentConfig | None,
    ) -> bool:
        if not self._uses_host_openai_codex_auth(model_config):
            return False
        host_agent_root = (
            self.settings.host_iron_agent_root
            / "agents"
            / self.settings.host_iron_agent_agent_id
            / "agent"
        )
        target_agent_root = state_root / "agents" / self.settings.host_iron_agent_agent_id / "agent"
        target_agent_root.mkdir(parents=True, exist_ok=True)
        changed = False
        for filename in ("auth-profiles.json", "models.json"):
            source = host_agent_root / filename
            if source.exists():
                target = target_agent_root / filename
                if filename == "auth-profiles.json":
                    changed = self._copy_if_newer_or_missing(source, target) or changed
                else:
                    changed = self._copy_if_changed(source, target) or changed
        return changed

    def _sync_provider_api_key_auth(
        self,
        *,
        assistant_id: str,
        state_root: Path,
        model_config: AssistantIronAgentConfig | None,
    ) -> bool:
        if model_config is None or model_config.auth_mode != "provider_api_key":
            return False
        provider_id = self._provider_id_from_config(model_config).strip()
        provider_api_key = model_config.provider_api_key.strip()
        if not provider_id or not provider_api_key:
            return False

        agent_id = assistant_id.strip().lower() or "main"
        auth_path = state_root / "agents" / agent_id / "agent" / "auth-profiles.json"
        payload = self._read_config_payload(auth_path)
        payload["version"] = 1
        profiles = payload.setdefault("profiles", {})
        if not isinstance(profiles, dict):
            profiles = {}
            payload["profiles"] = profiles
        profile_id = f"{provider_id}:default"
        profiles[profile_id] = {
            "type": "api_key",
            "provider": provider_id,
            "key": provider_api_key,
        }
        order = payload.setdefault("order", {})
        if not isinstance(order, dict):
            order = {}
            payload["order"] = order
        order[provider_id] = [profile_id]
        last_good = payload.setdefault("lastGood", {})
        if not isinstance(last_good, dict):
            last_good = {}
            payload["lastGood"] = last_good
        last_good[provider_id] = profile_id

        serialized = json.dumps(payload, indent=2, ensure_ascii=True) + "\n"
        if auth_path.exists() and auth_path.read_text(encoding="utf-8") == serialized:
            return False
        auth_path.parent.mkdir(parents=True, exist_ok=True)
        auth_path.write_text(serialized, encoding="utf-8")
        return True

    def _uses_host_openai_codex_auth(
        self,
        model_config: AssistantIronAgentConfig | None,
    ) -> bool:
        if model_config is None:
            return False
        if model_config.auth_mode != "host_iron_agent":
            return False
        primary_model = model_config.primary_model.strip()
        return primary_model.startswith("openai-codex/")

    def _copy_if_changed(self, source: Path, target: Path) -> bool:
        if target.exists() and target.read_bytes() == source.read_bytes():
            return False
        copy2(source, target)
        return True

    def _copy_if_newer_or_missing(self, source: Path, target: Path) -> bool:
        if target.exists():
            if target.read_bytes() == source.read_bytes():
                return False
            if source.stat().st_mtime <= target.stat().st_mtime:
                return False
        copy2(source, target)
        return True

    def _copy_if_missing(self, source: Path, target: Path) -> None:
        if target.exists():
            return
        copy2(source, target)

    def _copy_if_missing_or_placeholder(
        self,
        source: Path,
        target: Path,
        *,
        placeholder: str,
    ) -> None:
        if target.exists() and target.read_text(encoding="utf-8") != placeholder:
            return
        copy2(source, target)

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
        # First check if target matches any managed root itself (forbidden)
        for managed_root in managed_roots:
            resolved_root = managed_root.resolve()
            if resolved_target == resolved_root:
                raise ValueError(f"refusing to remove managed root itself: {resolved_target}")
        # Check if target is under any managed root
        for managed_root in managed_roots:
            resolved_root = managed_root.resolve()
            if resolved_root in resolved_target.parents:
                if not resolved_target.exists():
                    return False  # Path doesn't exist, nothing to remove
                rmtree(resolved_target)
                return True
        # Path is not under any managed root
        if not resolved_target.exists():
            return False  # Path doesn't exist, nothing to remove
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

    def _inspect_container_working_dir(self, container_name: str) -> str | None:
        result = self.command_runner(
            [
                self.settings.docker_binary,
                "container",
                "inspect",
                container_name,
                "--format",
                "{{.Config.WorkingDir}}",
            ]
        )
        if self._should_use_engine_api(result):
            payload = self._engine_inspect_container(container_name)
            if payload is None:
                return None
            config = payload.get("Config")
            if not isinstance(config, dict):
                return None
            raw_working_dir = config.get("WorkingDir")
            if not isinstance(raw_working_dir, str):
                return None
            return raw_working_dir.strip()
        if result.returncode != 0:
            return None
        return result.stdout.strip()

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

    def _inspect_container_gateway_publish_hosts(self, container_name: str) -> list[str] | None:
        result = self.command_runner(
            [
                self.settings.docker_binary,
                "container",
                "inspect",
                container_name,
                "--format",
                "{{json .HostConfig.PortBindings}}",
            ]
        )
        if self._should_use_engine_api(result):
            payload = self._engine_inspect_container(container_name)
            if payload is None:
                return None
            host_config = payload.get("HostConfig")
            if not isinstance(host_config, dict):
                return None
            raw_bindings = host_config.get("PortBindings")
        else:
            if result.returncode != 0:
                return None
            try:
                raw_bindings = json.loads(result.stdout.strip() or "{}")
            except json.JSONDecodeError:
                return None
        return self._parse_gateway_publish_hosts(raw_bindings)

    def _parse_gateway_publish_hosts(self, raw_bindings: object) -> list[str] | None:
        if not isinstance(raw_bindings, dict):
            return None
        bindings = raw_bindings.get(f"{self.settings.iron_agent_gateway_port}/tcp")
        if not isinstance(bindings, list):
            return None
        hosts: list[str] = []
        for item in bindings:
            if not isinstance(item, dict):
                continue
            host = str(item.get("HostIp", "")).strip()
            hosts.append(host)
        return hosts

    def _container_gateway_publish_host_changed(self, container_name: str) -> bool:
        current_hosts = self._inspect_container_gateway_publish_hosts(container_name)
        if current_hosts is None:
            return False
        expected_host = self._gateway_publish_host()
        if expected_host:
            return expected_host not in current_hosts
        return any(host not in {"", "0.0.0.0", "::"} for host in current_hosts)

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
            if not self._container_env_value_matches(current_env, key, expected):
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
        if not self.settings.iron_agent_hooks_enabled:
            for key in (
                "IRON_AGENT_HOOKS_ENABLED",
                "IRON_AGENT_SERVICE_URL",
                "IRON_AGENT_FAIL_CLOSED",
                "IRON_AGENT_HOOK_TIMEOUT_MS",
                "IRON_AGENT_APPROVAL_POLL_MS",
                "IRON_AGENT_APPROVAL_MAX_WAIT_MS",
                "IRON_AGENT_API_TOKEN",
            ):
                if self._container_env_has_any_name(current_env, key):
                    return True
        for key in (
            "IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND",
            "IRON_AGENT_WORKSPACE_FILE_SQLITE_PATH",
        ):
            if self._container_env_has_any_name(current_env, key) and key not in expected_env:
                return True
        current_binds = self._inspect_container_binds(container_name)
        if current_binds is None:
            return False
        expected_binds = {
            f"{state_root}:{self.settings.iron_agent_state_mount_path}",
            f"{workspace_root}:{self.settings.iron_agent_workspace_mount_path}",
            *self._shared_workspace_metadata_binds(),
        }
        if not expected_binds.issubset(set(current_binds)):
            return True
        expected_shared_binds = set(self._shared_workspace_metadata_binds())
        if not expected_shared_binds:
            shared_mount_suffix = (
                f":{self.settings.iron_agent_workspace_file_sqlite_mount_path.rstrip('/')}"
            )
            if any(bind.endswith(shared_mount_suffix) for bind in current_binds):
                return True
        current_working_dir = self._inspect_container_working_dir(container_name)
        if (
            current_working_dir is not None
            and current_working_dir != self.settings.iron_agent_workspace_mount_path
        ):
            return True
        if self._container_gateway_publish_host_changed(container_name):
            return True
        expected_extra_hosts = set(self._extra_hosts())
        if not expected_extra_hosts:
            return False
        current_extra_hosts = self._inspect_container_extra_hosts(container_name)
        if current_extra_hosts is None:
            return False
        return not expected_extra_hosts.issubset(set(current_extra_hosts))

    def _container_env_value_matches(
        self,
        current_env: dict[str, str],
        key: str,
        expected: str,
    ) -> bool:
        return any(current_env.get(name) == expected for name in self._container_env_names(key))

    def _container_env_has_any_name(self, current_env: dict[str, str], key: str) -> bool:
        return any(name in current_env for name in self._container_env_names(key))

    def _container_env_names(self, key: str) -> tuple[str, ...]:
        if key.startswith("IRON_AGENT_"):
            return (key,)
        return (key,)

    def _create_container(
        self,
        *,
        assistant_id: str,
        container_name: str,
        workspace_root: Path,
        state_root: Path,
    ) -> CommandResult:
        env_args: list[str] = []
        for key, value in self._managed_container_env(assistant_id).items():
            env_args.extend(["-e", f"{key}={value}"])
        extra_host_args: list[str] = []
        for entry in self._extra_hosts():
            extra_host_args.extend(["--add-host", entry])
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
                f"{state_root}:{self.settings.iron_agent_state_mount_path}",
                "-v",
                f"{workspace_root}:{self.settings.iron_agent_workspace_mount_path}",
                *[
                    item
                    for bind in self._shared_workspace_metadata_binds()
                    for item in ("-v", bind)
                ],
                "-w",
                self.settings.iron_agent_workspace_mount_path,
                "-p",
                self._gateway_port_binding_spec(),
                self.settings.iron_agent_container_image,
                "node",
                "/app/ironagent.mjs",
                "gateway",
                "--bind",
                self.settings.iron_agent_gateway_bind,
                "--port",
                str(self.settings.iron_agent_gateway_port),
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
        for attempt in range(5):
            result = self.command_runner(
                [
                    self.settings.docker_binary,
                    "port",
                    container_name,
                    f"{self.settings.iron_agent_gateway_port}/tcp",
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

    def _parse_port_output(self, output: str) -> tuple[str, int] | None:
        candidates: list[tuple[str, int]] = []
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
            candidates.append((host, port))
        return self._select_gateway_endpoint(candidates)

    def _select_gateway_endpoint(
        self,
        candidates: list[tuple[str, int]],
    ) -> tuple[str, int] | None:
        preferred_host = self._preferred_gateway_access_host()
        wildcard_match: tuple[str, int] | None = None
        loopback_match: tuple[str, int] | None = None
        fallback: tuple[str, int] | None = None

        for host, port in candidates:
            normalized_host = self._normalize_gateway_host(host)
            if host in WILDCARD_GATEWAY_HOSTS:
                wildcard_match = wildcard_match or (normalized_host, port)
                if preferred_host != "127.0.0.1":
                    return (normalized_host, port)
                continue
            if host in {"127.0.0.1", "localhost"}:
                mapped_host = preferred_host if preferred_host != "127.0.0.1" else "127.0.0.1"
                loopback_match = loopback_match or (mapped_host, port)
                if preferred_host != "127.0.0.1":
                    continue
                if wildcard_match is None:
                    return loopback_match
                continue
            if normalized_host == preferred_host:
                return (normalized_host, port)
            fallback = fallback or (normalized_host, port)

        return wildcard_match or loopback_match or fallback

    def _gateway_port_binding_spec(self) -> str:
        publish_host = self._gateway_publish_host()
        if publish_host:
            return f"{publish_host}::{self.settings.iron_agent_gateway_port}"
        return f"::{self.settings.iron_agent_gateway_port}"

    def _engine_gateway_port_binding(self) -> dict[str, str]:
        binding = {"HostPort": ""}
        publish_host = self._gateway_publish_host()
        if publish_host:
            binding["HostIp"] = publish_host
        return binding

    def _gateway_publish_host(self) -> str:
        publish_host = str(
            getattr(self.settings, "iron_agent_gateway_publish_host", "")
            or os.environ.get("IRON_AGENT_RUNTIME_GATEWAY_PUBLISH_HOST", "")
        ).strip()
        if publish_host:
            return publish_host
        host = self.settings.iron_agent_gateway_host.strip()
        if host in {"", "127.0.0.1", "localhost"}:
            # Publishing wildcard ports is more reliable on Docker Desktop than
            # an explicit loopback bind for dynamically created containers.
            return ""
        return host

    def _preferred_gateway_access_host(self) -> str:
        access_host = str(
            getattr(self.settings, "iron_agent_gateway_access_host", "")
            or os.environ.get("IRON_AGENT_RUNTIME_GATEWAY_ACCESS_HOST", "")
        ).strip()
        if access_host:
            if access_host == "localhost":
                return "127.0.0.1"
            return access_host
        configured = self.settings.iron_agent_gateway_host.strip()
        if configured and configured not in {*WILDCARD_GATEWAY_HOSTS, "127.0.0.1", "localhost"}:
            return configured
        for candidate in (
            self.settings.control_ui_public_url,
            self.settings.security_service_public_url,
        ):
            host = self._hostname_from_url(candidate)
            if not host or host in WILDCARD_GATEWAY_HOSTS:
                continue
            if host == "localhost":
                return "127.0.0.1"
            return host
        if configured in {"127.0.0.1", "localhost"}:
            return "127.0.0.1"

    def _normalize_gateway_host(self, host: str) -> str:
        trimmed = host.strip()
        if trimmed in WILDCARD_GATEWAY_HOSTS:
            return self._preferred_gateway_access_host()
        if trimmed == "localhost":
            return "127.0.0.1"
        return trimmed

    def _error_text(self, result: CommandResult, fallback: str) -> str:
        return result.stderr.strip() or result.stdout.strip() or fallback

    def _wait_for_runtime_probe(
        self,
        *,
        assistant_id: str,
        transient_statuses: set[RuntimeStatusName],
        tolerate_unhealthy: bool = False,
        attempts: int = 12,
        interval_seconds: float = 0.5,
    ) -> RuntimeProbeResult:
        latest_probe = self.probe_runtime(assistant_id=assistant_id)
        for _ in range(attempts):
            if latest_probe.status in transient_statuses:
                time.sleep(interval_seconds)
                latest_probe = self.probe_runtime(assistant_id=assistant_id)
                continue
            if (
                tolerate_unhealthy
                and latest_probe.status == "failed"
                and latest_probe.last_error
                in {
                    "container health is unhealthy",
                    "gateway endpoint not published yet",
                    "gateway healthz is not reachable",
                }
            ):
                time.sleep(interval_seconds)
                latest_probe = self.probe_runtime(assistant_id=assistant_id)
                continue
            return latest_probe
        return latest_probe

    def probe_runtime(self, *, assistant_id: str) -> RuntimeProbeResult:
        container_name, inspected = self._inspect_existing_container_with_health(assistant_id)
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
            if health_state == "unhealthy":
                status = "failed"
                last_error = "container health is unhealthy"
            else:
                status = "starting"
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
            if self._gateway_healthz_reachable(gateway_url):
                status = "running"
                last_error = ""
            elif gateway_url is None and not last_error:
                last_error = "gateway endpoint not published yet"
            elif not last_error:
                last_error = "gateway healthz is not reachable"

        if status == "running":
            auth_error = self._recent_runtime_auth_error(container_name)
            if auth_error:
                last_error = auth_error

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

    def _recent_runtime_auth_error(self, container_name: str) -> str:
        result = self.command_runner(
            [
                self.settings.docker_binary,
                "logs",
                "--tail",
                "120",
                container_name,
            ]
        )
        if self._should_use_engine_api(result):
            result = self._engine_container_logs(container_name, tail=120)
        if result.returncode != 0:
            return ""
        return self._extract_runtime_auth_error(f"{result.stdout}\n{result.stderr}")

    @staticmethod
    def _extract_runtime_auth_error(log_text: str) -> str:
        lower = log_text.lower()
        if any(marker in lower for marker in OPENAI_CODEX_AUTH_FAILURE_MARKERS):
            return OPENAI_CODEX_AUTH_FAILURE_MESSAGE
        return ""

    def _gateway_runtime_status(
        self,
        gateway_url: str | None,
    ) -> tuple[RuntimeStatusName, str]:
        if gateway_url is None:
            return "starting", "gateway endpoint not published yet"
        if self._gateway_healthz_reachable(gateway_url):
            return "running", ""
        return "starting", "gateway healthz is not reachable"

    def _gateway_healthz_reachable(self, gateway_url: str | None) -> bool:
        if not gateway_url:
            return False
        parsed = urlparse(gateway_url)
        if parsed.scheme != "http" or not parsed.hostname:
            return False
        path_prefix = parsed.path.rstrip("/")
        health_paths = (
            f"{path_prefix}/healthz" if path_prefix else "/healthz",
            f"{path_prefix}/api/v1/health" if path_prefix else "/api/v1/health",
            f"{path_prefix}/health" if path_prefix else "/health",
        )
        for path in health_paths:
            connection = HTTPConnection(parsed.hostname, parsed.port or 80, timeout=1.0)
            try:
                connection.request("GET", path)
                response = connection.getresponse()
                response.read()
                if 200 <= response.status < 400:
                    return True
                if response.status not in {404, 405}:
                    return False
            except Exception:
                return False
            finally:
                connection.close()
        return False

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
                "{{.Id}}|{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}",
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
        if not raw:
            return (None, "unknown", None)
        parts = raw.split("|", 2)
        container_id = parts[0].strip() if len(parts) >= 1 else ""
        container_state = parts[1].strip() if len(parts) >= 2 else "unknown"
        health_state = parts[2].strip() if len(parts) >= 3 else ""
        return (
            container_id or None,
            container_state or "unknown",
            health_state or None,
        )
