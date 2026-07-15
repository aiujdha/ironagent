from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


RuntimeStatusName = Literal[
    "pending",
    "starting",
    "running",
    "paused",
    "stopping",
    "stopped",
    "restarting",
    "failed",
]
RuntimeSyncStateName = Literal["current", "pending", "stale", "degraded", "failed", "unknown"]
AssistantAuthMode = Literal["host_iron_agent", "provider_api_key"]
WorkspaceSelectionMode = Literal["default_root", "managed_root"]

RUNTIME_STATUS_NAMES = {
    "pending",
    "starting",
    "running",
    "paused",
    "stopping",
    "stopped",
    "restarting",
    "failed",
}
RUNTIME_SYNC_STATE_NAMES = {"current", "pending", "stale", "degraded", "failed", "unknown"}


def desired_state_for_status(status: RuntimeStatusName) -> RuntimeStatusName:
    if status in {"starting", "restarting"}:
        return "running"
    if status == "stopping":
        return "stopped"
    return status


def sync_state_for_runtime(
    *,
    desired_state: RuntimeStatusName,
    observed_state: RuntimeStatusName,
    last_runtime_error: str = "",
) -> RuntimeSyncStateName:
    if observed_state == "failed":
        return "failed" if desired_state == "failed" else "degraded"
    if last_runtime_error.strip():
        return "degraded"
    if desired_state == observed_state:
        return "current"
    if observed_state in {"pending", "starting", "stopping", "restarting"}:
        return "pending"
    return "stale"


class AssistantIronAgentConfig(BaseModel):
    primary_model: str = "openai-codex/gpt-5.4"
    auth_mode: AssistantAuthMode = "host_iron_agent"
    provider_id: str = ""
    provider_base_url: str = ""
    provider_api_key: str = ""

    @model_validator(mode="before")
    @classmethod
    def infer_auth_mode_for_legacy_records(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        if data.get("auth_mode"):
            return data

        primary_model = str(data.get("primary_model", "")).strip()
        provider_id = str(data.get("provider_id", "")).strip()
        provider_base_url = str(data.get("provider_base_url", "")).strip()
        provider_api_key = str(data.get("provider_api_key", "")).strip()

        if provider_id or provider_base_url or provider_api_key:
            data["auth_mode"] = "provider_api_key"
        elif primary_model and not primary_model.startswith("openai-codex/"):
            data["auth_mode"] = "provider_api_key"
        else:
            data["auth_mode"] = "host_iron_agent"
        return data


class AssistantWorkspaceConfig(BaseModel):
    selection_mode: WorkspaceSelectionMode = "default_root"
    root_id: str = "default"
    workspace_slug: str = ""
    auto_place_outputs: bool = True


class AssistantRuntimeRecord(BaseModel):
    container_name: str | None = None
    container_id: str | None = None
    workspace_root: str | None = None
    workspace_root_id: str | None = None
    workspace_slug: str | None = None
    temp_dir: str = "temp"
    persistent_dir: str = "persistent"
    state_root: str | None = None
    status: RuntimeStatusName = "pending"
    image: str | None = None
    gateway_host: str | None = None
    gateway_port: int | None = None
    gateway_url: str | None = None
    gateway_token: str | None = None
    last_error: str = ""
    desired_state: RuntimeStatusName = "pending"
    observed_state: RuntimeStatusName = "pending"
    sync_state: RuntimeSyncStateName = "unknown"
    last_observed_at: datetime | None = None
    last_reconciled_at: datetime | None = None
    last_runtime_error: str = ""
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_runtime_status(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        normalized: dict[str, Any] = dict(data)
        raw_status = str(normalized.get("status", "")).strip().lower()
        if raw_status in RUNTIME_STATUS_NAMES:
            normalized["status"] = raw_status
            return normalized

        if raw_status == "created":
            normalized["status"] = "starting"
            normalized["desired_state"] = "running"
            normalized["observed_state"] = "starting"
            normalized["sync_state"] = sync_state_for_runtime(
                desired_state="running",
                observed_state="starting",
                last_runtime_error=str(normalized.get("last_runtime_error", "") or ""),
            )
            return normalized

        normalized["status"] = "failed"
        if raw_status and not str(normalized.get("last_error", "")).strip():
            normalized["last_error"] = f"container state is {raw_status}"
        if raw_status and not str(normalized.get("last_runtime_error", "")).strip():
            normalized["last_runtime_error"] = str(normalized.get("last_error", ""))
        normalized["desired_state"] = "failed"
        normalized["observed_state"] = "failed"
        normalized["sync_state"] = "failed"
        return normalized

    @model_validator(mode="before")
    @classmethod
    def derive_runtime_state_fields(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        normalized: dict[str, Any] = dict(data)
        raw_status = str(normalized.get("status", "pending")).strip().lower()
        status = raw_status if raw_status in RUNTIME_STATUS_NAMES else "failed"
        last_error = str(normalized.get("last_error", "") or "").strip()
        last_runtime_error = str(normalized.get("last_runtime_error", "") or "").strip()
        if not last_runtime_error and last_error:
            last_runtime_error = last_error
            normalized["last_runtime_error"] = last_runtime_error
        if last_runtime_error and not last_error:
            normalized["last_error"] = last_runtime_error

        desired_state = str(
            normalized.get("desired_state", desired_state_for_status(status)) or ""
        ).strip().lower()
        observed_state = str(normalized.get("observed_state", status) or "").strip().lower()
        sync_state = str(normalized.get("sync_state", "") or "").strip().lower()

        if desired_state not in RUNTIME_STATUS_NAMES:
            desired_state = desired_state_for_status(status)
        if observed_state not in RUNTIME_STATUS_NAMES:
            observed_state = status
        if sync_state not in RUNTIME_SYNC_STATE_NAMES:
            sync_state = sync_state_for_runtime(
                desired_state=desired_state,
                observed_state=observed_state,
                last_runtime_error=last_runtime_error,
            )

        normalized["desired_state"] = desired_state
        normalized["observed_state"] = observed_state
        normalized["sync_state"] = sync_state
        return normalized
