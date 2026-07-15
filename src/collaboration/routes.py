import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError

from src.collaboration.schemas import (
    CollaborationClaimResponse,
    CollaborationCleanupResponse,
    CollaborationTaskCancelRequest,
    CollaborationTaskClaimRenewRequest,
    CollaborationTaskClaimRequest,
    CollaborationTaskCompleteRequest,
    CollaborationTaskCreateRequest,
    CollaborationTaskFailRequest,
    CollaborationTaskListResponse,
    CollaborationTaskStartRequest,
    SharedObjectCreateRequest,
    SharedObjectGrantRequest,
    SharedObjectListResponse,
)
from src.collaboration.service import CollaborationService
from src.shared.logging import get_logger

router = APIRouter(prefix="/api/v1/collaboration", tags=["collaboration"])
service = CollaborationService()
log = get_logger(__name__)
T = TypeVar("T")
_SENSITIVE_DETAIL_KEYS = {"claim_token"}


def _raise_http(exc: ValueError) -> None:
    message = str(exc)
    lowered = message.lower()
    if "not found" in lowered:
        raise HTTPException(status_code=404, detail=message) from exc
    if "not authorized" in lowered:
        raise HTTPException(status_code=403, detail=message) from exc
    if any(
        token in lowered
        for token in (
            "claimable",
            "claimed",
            "invalid",
            "cannot",
            "expired",
            "disabled",
            "required",
            "different",
        )
    ):
        raise HTTPException(status_code=409, detail=message) from exc
    raise HTTPException(status_code=400, detail=message) from exc


def _raise_repository_unavailable(exc: Exception, operation: str, **detail: Any) -> None:
    safe_detail = {
        key: ("***" if key in _SENSITIVE_DETAIL_KEYS else value)
        for key, value in detail.items()
        if value not in (None, "")
    }
    log.exception(
        "collaboration_repository_error",
        operation=operation,
        error_type=type(exc).__name__,
        error=str(exc),
        **safe_detail,
    )
    raise HTTPException(
        status_code=503,
        detail="collaboration repository unavailable",
    ) from exc


def _is_repository_runtime_error(exc: RuntimeError) -> bool:
    message = str(exc)
    return (
        "collaboration repository" in message
        or "pymysql is required" in message
        or "session factory is not initialized" in message
    )


async def _run_call(
    operation: str,
    fn: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> T:
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except ValueError as exc:
        _raise_http(exc)
    except SQLAlchemyError as exc:
        _raise_repository_unavailable(exc, operation, **kwargs)
    except RuntimeError as exc:
        if _is_repository_runtime_error(exc):
            _raise_repository_unavailable(exc, operation, **kwargs)
        raise
    raise RuntimeError("unreachable")


@router.post("/objects")
async def create_shared_object(payload: SharedObjectCreateRequest):
    return await _run_call(
        "create_shared_object",
        service.create_shared_object,
        **payload.model_dump(),
    )


@router.get("/objects", response_model=SharedObjectListResponse)
async def list_shared_objects(
    assistant_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> SharedObjectListResponse:
    items = await _run_call(
        "list_shared_objects",
        service.list_shared_objects,
        assistant_id=assistant_id,
        status=status,
    )
    return SharedObjectListResponse(items=items, total=len(items))


@router.get("/readyz")
async def collaboration_readyz() -> dict[str, Any]:
    return await _run_call("collaboration_readyz", service.check_ready)


@router.get("/objects/{object_id}")
async def get_shared_object(
    object_id: str,
    assistant_id: str | None = Query(default=None),
    task_id: str | None = Query(default=None),
):
    return await _run_call(
        "get_shared_object",
        service.get_shared_object,
        object_id,
        assistant_id=assistant_id,
        task_id=task_id,
    )


@router.post("/objects/{object_id}/permissions")
async def grant_shared_object_permission(object_id: str, payload: SharedObjectGrantRequest):
    return await _run_call(
        "grant_shared_object_permission",
        service.grant_shared_object_permission,
        object_id=object_id,
        **payload.model_dump(),
    )


@router.post("/tasks")
async def create_collaboration_task(payload: CollaborationTaskCreateRequest):
    return await _run_call("create_collaboration_task", service.create_task, **payload.model_dump())


@router.get("/tasks", response_model=CollaborationTaskListResponse)
async def list_collaboration_tasks(
    assistant_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    source_assistant_id: str | None = Query(default=None),
) -> CollaborationTaskListResponse:
    items = await _run_call(
        "list_collaboration_tasks",
        service.list_tasks,
        assistant_id=assistant_id,
        status=status,
        source_assistant_id=source_assistant_id,
    )
    return CollaborationTaskListResponse(items=items, total=len(items))


@router.get("/tasks/{task_id}")
async def get_collaboration_task(task_id: str):
    return await _run_call("get_collaboration_task", service.get_task, task_id)


@router.post("/tasks/{task_id}/claim", response_model=CollaborationClaimResponse)
async def claim_collaboration_task(task_id: str, payload: CollaborationTaskClaimRequest):
    claim = await _run_call(
        "claim_collaboration_task",
        service.claim_task,
        task_id=task_id,
        assistant_id=payload.assistant_id,
    )
    task = await _run_call("get_collaboration_task", service.get_task, task_id)
    return CollaborationClaimResponse(task=task, claim=claim)


@router.post("/tasks/{task_id}/claim/renew", response_model=CollaborationClaimResponse)
async def renew_collaboration_task_claim(
    task_id: str,
    payload: CollaborationTaskClaimRenewRequest,
):
    claim = await _run_call(
        "renew_collaboration_task_claim",
        service.renew_claim,
        task_id=task_id,
        assistant_id=payload.assistant_id,
        claim_token=payload.claim_token,
    )
    task = await _run_call("get_collaboration_task", service.get_task, task_id)
    return CollaborationClaimResponse(task=task, claim=claim)


@router.post("/tasks/{task_id}/start")
async def start_collaboration_task(task_id: str, payload: CollaborationTaskStartRequest):
    return await _run_call(
        "start_collaboration_task",
        service.start_task,
        task_id=task_id,
        **payload.model_dump(),
    )


@router.post("/tasks/{task_id}/complete")
async def complete_collaboration_task(task_id: str, payload: CollaborationTaskCompleteRequest):
    return await _run_call(
        "complete_collaboration_task",
        service.complete_task,
        task_id=task_id,
        **payload.model_dump(),
    )


@router.post("/tasks/{task_id}/fail")
async def fail_collaboration_task(task_id: str, payload: CollaborationTaskFailRequest):
    return await _run_call(
        "fail_collaboration_task",
        service.fail_task,
        task_id=task_id,
        **payload.model_dump(),
    )


@router.post("/tasks/{task_id}/cancel")
async def cancel_collaboration_task(task_id: str, payload: CollaborationTaskCancelRequest):
    return await _run_call(
        "cancel_collaboration_task",
        service.cancel_task,
        task_id=task_id,
        actor_id=payload.actor_id,
        reason=payload.reason,
    )


@router.post("/cleanup/run", response_model=CollaborationCleanupResponse)
async def cleanup_collaboration_objects() -> CollaborationCleanupResponse:
    result = await _run_call("cleanup_collaboration_objects", service.cleanup_expired_objects)
    return CollaborationCleanupResponse(
        expired_objects=result.expired_objects,
        expired_tasks=result.expired_tasks,
        expired_permissions=result.expired_permissions,
        expired_claims=result.expired_claims,
    )
