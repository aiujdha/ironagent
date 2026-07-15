"""API routes for the Assistants Service.

Endpoints
---------
GET    /health                          — liveness probe
GET    /api/v1/assistants               — list all assistants
GET    /api/v1/assistants/{id}          — get single assistant
POST   /api/v1/assistants               — create assistant
PUT    /api/v1/assistants/{id}          — update assistant
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.assistants.models import AssistantRecord
from src.assistants.service import AssistantService
from src.assistants_service.schemas import (
    AssistantListResponse,
    AssistantResponse,
    CreateAssistantRequest,
    HealthResponse,
    RuntimeInfo,
    UpdateAssistantRequest,
)
from src.runtime.models import AssistantIronAgentConfig
from src.shared.auth import require_internal_service_token

router = APIRouter()


def _get_service() -> AssistantService:
    return AssistantService()


SERVICE_DEP = Depends(_get_service)


def _to_response(record: AssistantRecord) -> AssistantResponse:
    return AssistantResponse(
        assistant_id=record.assistant_id,
        name=record.name,
        capability_profile=record.capability_profile,
        notes=record.notes,
        runtime=RuntimeInfo(
            container_name=record.runtime.container_name,
            container_id=record.runtime.container_id,
            workspace_root=record.runtime.workspace_root,
            state_root=record.runtime.state_root,
            status=record.runtime.status,
            image=record.runtime.image,
            gateway_host=record.runtime.gateway_host,
            gateway_port=record.runtime.gateway_port,
            gateway_url=record.runtime.gateway_url,
            last_error=record.runtime.last_error,
            updated_at=record.runtime.updated_at,
        ),
        created_at=record.created_at,
        updated_at=record.updated_at,
        deleted=record.deleted,
        deleted_at=record.deleted_at,
    )


def _active_records(records: list[AssistantRecord]) -> list[AssistantRecord]:
    return [record for record in records if not record.deleted]


# ── Health ───────────────────────────────────────────────────────────────────


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse()


# ── CRUD ─────────────────────────────────────────────────────────────────────


@router.get("/api/v1/assistants", response_model=AssistantListResponse)
async def list_assistants(
    svc: AssistantService = SERVICE_DEP,
) -> AssistantListResponse:
    records, _, _ = await svc.async_runtime_views()
    items = [_to_response(r) for r in _active_records(records)]
    return AssistantListResponse(assistants=items, total=len(items))


@router.get("/api/v1/assistants/{assistant_id}", response_model=AssistantResponse)
async def get_assistant(
    assistant_id: str,
    svc: AssistantService = SERVICE_DEP,
) -> AssistantResponse:
    record = await svc.aget_assistant(assistant_id)
    if record is None or record.deleted:
        raise HTTPException(status_code=404, detail="assistant not found")
    records, _, _ = await svc.async_runtime_views([record])
    return _to_response(records[0])


@router.post(
    "/api/v1/assistants",
    response_model=AssistantResponse,
    status_code=201,
    dependencies=[Depends(require_internal_service_token)],
)
async def create_assistant(
    payload: CreateAssistantRequest,
    svc: AssistantService = SERVICE_DEP,
) -> AssistantResponse:
    existing = await svc.aget_assistant(payload.assistant_id)
    if existing is not None:
        raise HTTPException(status_code=409, detail="assistant already exists")

    model_config: AssistantIronAgentConfig | None = None
    if payload.iron_agent_config is not None:
        model_config = AssistantIronAgentConfig(
            primary_model=payload.iron_agent_config.primary_model,
            auth_mode=payload.iron_agent_config.auth_mode,
            provider_id=payload.iron_agent_config.provider_id,
            provider_base_url=payload.iron_agent_config.provider_base_url,
            provider_api_key=payload.iron_agent_config.provider_api_key,
        )

    record = await svc.aupsert_assistant(
        assistant_id=payload.assistant_id,
        name=payload.name,
        capability_profile=payload.capability_profile,
        notes=payload.notes,
        model_config=model_config,
        provision_runtime=payload.provision_runtime,
    )
    return _to_response(record)


@router.put(
    "/api/v1/assistants/{assistant_id}",
    response_model=AssistantResponse,
    dependencies=[Depends(require_internal_service_token)],
)
async def update_assistant(
    assistant_id: str,
    payload: UpdateAssistantRequest,
    svc: AssistantService = SERVICE_DEP,
) -> AssistantResponse:
    existing = await svc.aget_assistant(assistant_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="assistant not found")

    name = payload.name if payload.name is not None else existing.name
    profile = (
        payload.capability_profile
        if payload.capability_profile is not None
        else existing.capability_profile
    )
    notes = payload.notes if payload.notes is not None else existing.notes

    model_config: AssistantIronAgentConfig | None = None
    if payload.iron_agent_config is not None:
        model_config = AssistantIronAgentConfig(
            primary_model=payload.iron_agent_config.primary_model,
            auth_mode=payload.iron_agent_config.auth_mode,
            provider_id=payload.iron_agent_config.provider_id,
            provider_base_url=payload.iron_agent_config.provider_base_url,
            provider_api_key=payload.iron_agent_config.provider_api_key,
        )

    record = await svc.aupsert_assistant(
        assistant_id=assistant_id,
        name=name,
        capability_profile=profile,
        notes=notes,
        model_config=model_config,
        provision_runtime=payload.provision_runtime,
    )
    return _to_response(record)
