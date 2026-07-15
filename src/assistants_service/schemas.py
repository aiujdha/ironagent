"""Request / response schemas for the Assistants Service API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.assistants.models import CapabilityProfileName
from src.runtime.models import AssistantAuthMode


# ── Request schemas ──────────────────────────────────────────────────────────


class CreateAssistantRequest(BaseModel):
    assistant_id: str
    name: str
    capability_profile: CapabilityProfileName = "onlyread"
    notes: str = ""
    provision_runtime: bool = True
    iron_agent_config: IronAgentConfigPayload | None = None


class UpdateAssistantRequest(BaseModel):
    name: str | None = None
    capability_profile: CapabilityProfileName | None = None
    notes: str | None = None
    provision_runtime: bool = True
    iron_agent_config: IronAgentConfigPayload | None = None


class IronAgentConfigPayload(BaseModel):
    primary_model: str = ""
    auth_mode: AssistantAuthMode = "host_iron_agent"
    provider_id: str = ""
    provider_base_url: str = ""
    provider_api_key: str = ""


# ── Response schemas ─────────────────────────────────────────────────────────


class RuntimeInfo(BaseModel):
    container_name: str | None = None
    container_id: str | None = None
    workspace_root: str | None = None
    state_root: str | None = None
    status: str = "pending"
    image: str | None = None
    gateway_host: str | None = None
    gateway_port: int | None = None
    gateway_url: str | None = None
    last_error: str = ""
    updated_at: datetime | None = None


class AssistantResponse(BaseModel):
    assistant_id: str
    name: str
    capability_profile: CapabilityProfileName
    notes: str = ""
    runtime: RuntimeInfo = Field(default_factory=RuntimeInfo)
    created_at: datetime
    updated_at: datetime
    deleted: bool = False
    deleted_at: datetime | None = None


class AssistantListResponse(BaseModel):
    assistants: list[AssistantResponse]
    total: int


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str = "assistants_service"


# Forward reference update
CreateAssistantRequest.model_rebuild()
UpdateAssistantRequest.model_rebuild()
