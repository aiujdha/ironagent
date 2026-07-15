import asyncio
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, status
from fastapi.concurrency import run_in_threadpool

from src.assistants.models import AssistantRecord
from src.assistants.service import AssistantService
from src.security_service.control_ui_relay import (
    build_control_ui_relay_url,
    handle_control_ui_gateway_relay,
)
from src.security_service.decision import SecurityDecisionEngine
from src.security_service.launch_tickets import (
    ControlUiLaunchTicketExpiredError,
    ControlUiLaunchTicketNotFoundError,
    ControlUiLaunchTicketOriginMismatchError,
    control_ui_relay_session_store,
    launch_ticket_store,
)
from src.security_service.schemas import (
    ApprovalResolveRequest,
    ApprovalStatusResponse,
    ControlUiLaunchTicketCreateRequest,
    ControlUiLaunchTicketCreateResponse,
    ControlUiLaunchTicketResolveRequest,
    ControlUiLaunchTicketResolveResponse,
    PostExecuteHookRequest,
    PostExecuteHookResponse,
    PreExecuteHookRequest,
    PreExecuteHookResponse,
)
from src.security_service.session_auto_allow import handle_allow_always
from src.shared.auth import is_internal_service_request, require_internal_service_token
from src.shared.config import get_settings
from src.shared.logging import get_logger
from src.shared.repositories import AuditRepository, build_audit_repository
from src.workspace_files.service import WorkspaceFileService

router = APIRouter()
engine = SecurityDecisionEngine()
log = get_logger(__name__)
_approval_bypass_audit_tasks: set[asyncio.Task[None]] = set()


def get_audit_repository() -> AuditRepository:
    return build_audit_repository()


def get_workspace_file_service() -> WorkspaceFileService:
    return WorkspaceFileService()


workspace_file_service_dependency = Depends(get_workspace_file_service)


async def _aappend_control_session_audit(
    *,
    action: Literal["create", "consume", "expire", "reject"],
    assistant_id: str | None,
    session_key: str | None,
    success: bool,
    error_code: str = "",
    error_message: str = "",
) -> None:
    now = datetime.now(UTC)
    payload = {
        "envelope": {
            "request_id": f"control-session-{uuid4()}",
            "source": "security_service",
            "hook_name": "control_session",
            "occurred_at": now.isoformat(),
        },
        "assistant": {
            "assistant_id": assistant_id or "",
        },
        "tool": {
            "name": f"control_session.{action}",
        },
        "operation_type": f"control_session.{action}",
        "decision": "success" if success else "failure",
        "result": {
            "success": success,
            "status": "success" if success else "failure",
            "error_code": error_code,
            "error_message": error_message,
        },
        "session": {
            "session_key": session_key or "",
        },
    }
    try:
        await get_audit_repository().aappend_event(payload)
    except Exception as exc:
        log.warning(
            "control_session_audit_append_failed",
            action=action,
            assistant_id=assistant_id,
            error=f"{type(exc).__name__}: {exc}",
        )


async def _aappend_approval_bypass_audit(
    payload: PreExecuteHookRequest,
    response: PreExecuteHookResponse,
) -> None:
    metadata = dict(response.metadata or {})
    if metadata.get("approval_bypassed") is not True:
        return

    now = datetime.now(UTC)
    audit_payload = {
        "envelope": {
            "request_id": payload.envelope.request_id,
            "source": payload.envelope.source,
            "hook_name": payload.envelope.hook_name,
            "occurred_at": now.isoformat(),
        },
        "assistant": payload.assistant.model_dump(mode="json"),
        "session": payload.session.model_dump(mode="json"),
        "tool": payload.tool.model_dump(mode="json"),
        "operation_type": "approval.bypassed",
        "decision": response.decision,
        "result": {
            "success": response.decision == "allow",
            "status": "approval_bypassed",
            "reason": response.reason,
        },
        "risk": payload.risk.model_dump(mode="json"),
        "metadata": metadata,
    }
    try:
        await get_audit_repository().aappend_event(audit_payload)
    except Exception as exc:
        log.warning(
            "approval_bypass_audit_append_failed",
            request_id=payload.envelope.request_id,
            assistant_id=payload.assistant.assistant_id,
            tool_name=payload.tool.name,
            error=f"{type(exc).__name__}: {exc}",
        )


def _schedule_approval_bypass_audit(
    payload: PreExecuteHookRequest,
    response: PreExecuteHookResponse,
) -> None:
    if (response.metadata or {}).get("approval_bypassed") is not True:
        return

    task = asyncio.create_task(_aappend_approval_bypass_audit(payload, response))
    _approval_bypass_audit_tasks.add(task)

    def _discard(completed_task: asyncio.Task[None]) -> None:
        _approval_bypass_audit_tasks.discard(completed_task)
        if completed_task.cancelled():
            return
        try:
            task_error = completed_task.exception()
        except Exception as exc:
            log.warning(
                "approval_bypass_audit_task_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        if task_error is not None:
            log.warning(
                "approval_bypass_audit_task_failed",
                error=f"{type(task_error).__name__}: {task_error}",
            )

    task.add_done_callback(_discard)


async def drain_approval_bypass_audit_tasks(timeout_seconds: float = 2.0) -> None:
    tasks = [task for task in _approval_bypass_audit_tasks if not task.done()]
    if not tasks:
        return

    done, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
    if done:
        log.info("approval_bypass_audit_tasks_drained", count=len(done))
    if not pending:
        return

    log.warning(
        "approval_bypass_audit_tasks_drain_timeout",
        pending_count=len(pending),
        timeout_seconds=timeout_seconds,
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


def _pre_execute_log_fields(payload: PreExecuteHookRequest) -> dict[str, object]:
    return {
        "request_id": payload.envelope.request_id,
        "assistant_id": payload.assistant.assistant_id,
        "assistant_name": payload.assistant.name,
        "user_id": payload.session.user_id,
        "session_id": payload.session.session_id,
        "run_id": payload.session.run_id,
        "conversation_id": payload.session.conversation_id,
        "tool_name": payload.tool.name,
        "risk_level": payload.risk.level,
        "risk_reason": payload.risk.reason,
    }


def _exec_command_for_log(payload: PreExecuteHookRequest) -> str | None:
    command = payload.tool.args.get("command")
    if command is None:
        return None
    text = str(command)
    if len(text) <= 500:
        return text
    return f"{text[:500]}...(truncated)"


def _post_execute_log_fields(payload: PostExecuteHookRequest) -> dict[str, object]:
    return {
        "request_id": payload.envelope.request_id,
        "assistant_id": payload.assistant.assistant_id,
        "assistant_name": payload.assistant.name,
        "user_id": payload.session.user_id,
        "session_id": payload.session.session_id,
        "run_id": payload.session.run_id,
        "conversation_id": payload.session.conversation_id,
        "tool_name": payload.tool.name,
        "decision": payload.decision,
        "success": payload.result.success,
        "duration_ms": payload.result.duration_ms,
        "error": payload.result.error,
    }


async def _resolve_live_control_ui_gateway_credentials(
    payload: ControlUiLaunchTicketCreateRequest,
) -> tuple[str, str]:
    gateway_url = payload.gateway_url.strip()
    gateway_token = payload.gateway_token.strip()
    assistant_id = (payload.assistant_id or "").strip()
    if not assistant_id:
        return gateway_url, gateway_token

    assistant_service = AssistantService()
    assistant = await assistant_service.aget_assistant(assistant_id)
    if assistant is None or assistant.deleted:
        log.warning(
            "control_ui_launch_ticket_assistant_unavailable",
            assistant_id=assistant_id,
            requested_gateway_url=gateway_url,
            deleted=bool(assistant.deleted) if assistant is not None else False,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="assistant not found",
        )

    probes = await assistant_service.aprobe_runtime_states([assistant])
    probe = probes.get(assistant_id)
    resolved_gateway_url = (
        (probe.gateway_url if probe and probe.gateway_url else None)
        or assistant.runtime.gateway_url
        or gateway_url
    ).strip()
    resolved_gateway_token = (gateway_token or assistant.runtime.gateway_token).strip()

    if resolved_gateway_url != gateway_url:
        log.info(
            "control_ui_launch_ticket_gateway_refreshed",
            assistant_id=assistant_id,
            requested_gateway_url=gateway_url,
            resolved_gateway_url=resolved_gateway_url,
        )

    return resolved_gateway_url, resolved_gateway_token


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/api/v1/hooks/pre-execute", response_model=PreExecuteHookResponse)
@router.post("/api/v1/security/check", response_model=PreExecuteHookResponse)
async def security_check(payload: PreExecuteHookRequest) -> PreExecuteHookResponse:
    fields = _pre_execute_log_fields(payload)
    log.info("安全预执行检查开始", **fields)
    try:
        response = await engine.aevaluate(payload)
    except Exception as exc:
        log.warning("安全预执行检查异常", **fields, error=f"{type(exc).__name__}: {exc}")
        raise
    external_response = _external_hook_response(response)
    if payload.tool.name == "exec":
        log.info(
            "exec 工具决策结果",
            **fields,
            decision=external_response.decision,
            reason=external_response.reason,
            approval_id=(
                external_response.approval.approval_id
                if external_response.approval is not None
                else None
            ),
            capability_profile=external_response.capability_profile,
            command=_exec_command_for_log(payload),
        )
    log.info(
        "安全预执行检查完成",
        **fields,
        decision=external_response.decision,
        reason=external_response.reason,
        approval_id=(
            external_response.approval.approval_id
            if external_response.approval is not None
            else None
        ),
        capability_profile=external_response.capability_profile,
    )
    _schedule_approval_bypass_audit(payload, external_response)
    return external_response


def _external_hook_response(response: PreExecuteHookResponse) -> PreExecuteHookResponse:
    reason = {
        "该操作需要您的确认": "tool requires approval",
    }.get(response.reason, response.reason)
    if reason == response.reason:
        return response
    return response.model_copy(update={"reason": reason})


@router.post("/api/v1/hooks/post-execute", response_model=PostExecuteHookResponse)
@router.post("/api/v1/audit/log", response_model=PostExecuteHookResponse)
async def audit_log(
    payload: PostExecuteHookRequest,
    workspace_file_service: WorkspaceFileService = workspace_file_service_dependency,
) -> PostExecuteHookResponse:
    fields = _post_execute_log_fields(payload)
    log.info("安全后执行审计开始", **fields)
    await get_audit_repository().aappend_event(payload.model_dump(mode="json"))
    assistant_id = (payload.assistant.assistant_id or "").strip()
    if payload.result.success and assistant_id:
        try:
            await run_in_threadpool(workspace_file_service.sync_workspace_outputs, assistant_id)
            log.info(
                "安全后执行工作空间同步完成",
                request_id=payload.envelope.request_id,
                assistant_id=assistant_id,
                tool_name=payload.tool.name,
            )
        except Exception as exc:
            log.warning(
                "安全后执行工作空间同步失败",
                request_id=payload.envelope.request_id,
                assistant_id=assistant_id,
                tool_name=payload.tool.name,
                error=str(exc),
            )
    log.info("安全后执行审计完成", **fields)
    return PostExecuteHookResponse(request_id=payload.envelope.request_id)


@router.get("/api/v1/assistants/{assistant_id}", response_model=AssistantRecord)
async def get_assistant(assistant_id: str) -> AssistantRecord:
    service = AssistantService()
    assistant = await service.aget_assistant(assistant_id)
    if assistant is None or assistant.deleted:
        raise HTTPException(status_code=404, detail="assistant not found")
    try:
        synced_assistants, _, _ = await service.async_runtime_views([assistant])
        if synced_assistants:
            assistant = synced_assistants[0]
    except Exception as exc:
        log.warning(
            "assistant_runtime_probe_failed_for_security_view",
            assistant_id=assistant_id,
            error=str(exc),
        )
    return assistant


@router.get("/api/v1/approvals", response_model=list[ApprovalStatusResponse])
async def list_approvals(
    status: str | None = None,
    assistant_id: str | None = None,
    approval_scope: str | None = None,
) -> list[ApprovalStatusResponse]:
    """
    列出审批请求（支持 IronAgent 轮询）

    IronAgent 的 ironagent-bridge 插件会轮询此端点来检查待审批的请求。
    """
    normalized_scope: Literal["user", "admin"] | None = None
    if approval_scope == "user":
        normalized_scope = "user"
    elif approval_scope == "admin":
        normalized_scope = "admin"
    log.info(
        "审批列表查询开始",
        status=status,
        assistant_id=assistant_id,
        approval_scope=normalized_scope,
    )
    requests = await engine.approval_service.alist_requests(
        status=status,
        assistant_id=assistant_id,
        approval_scope=normalized_scope,
    )
    response = [
        ApprovalStatusResponse(
            approval_id=req.id,
            tool_name=req.tool_name,
            assistant_id=req.assistant_id,
            capability_profile=req.capability_profile,
            approval_scope=req.approval_scope,
            reason=req.reason,
            target=req.target,
            status=req.status,
            resolution=req.resolution,
            requested_at=req.requested_at,
            expires_at=req.expires_at,
            resolved_at=req.resolved_at,
        )
        for req in requests
    ]
    log.info(
        "审批列表查询完成",
        status=status,
        assistant_id=assistant_id,
        approval_scope=normalized_scope,
        count=len(response),
    )
    return response


@router.get("/api/v1/approvals/{approval_id}", response_model=ApprovalStatusResponse)
async def approval_status(approval_id: str) -> ApprovalStatusResponse:
    log.info("审批详情查询开始", approval_id=approval_id)
    approval = await engine.approval_service.aget_request(approval_id)
    if approval is None:
        log.warning("审批详情查询失败", approval_id=approval_id, reason="approval_not_found")
        raise HTTPException(status_code=404, detail="approval not found")

    response = ApprovalStatusResponse(
        approval_id=approval.id,
        tool_name=approval.tool_name,
        assistant_id=approval.assistant_id,
        capability_profile=approval.capability_profile,
        approval_scope=approval.approval_scope,
        reason=approval.reason,
        target=approval.target,
        status=approval.status,
        resolution=approval.resolution,
        requested_at=approval.requested_at,
        expires_at=approval.expires_at,
        resolved_at=approval.resolved_at,
    )
    log.info(
        "审批详情查询完成",
        approval_id=approval.id,
        assistant_id=approval.assistant_id,
        approval_scope=approval.approval_scope,
        status=approval.status,
        resolution=approval.resolution,
    )
    return response


@router.post("/api/v1/approvals/{approval_id}/resolve", response_model=ApprovalStatusResponse)
async def resolve_approval(
    approval_id: str,
    payload: ApprovalResolveRequest,
) -> ApprovalStatusResponse:
    """
    解决审批请求（由 IronAgent Control-UI 在用户点击 Allow/Deny 后调用）

    将 ironagent 格式的决策映射到 ironagent-admin 的 resolution 格式：
      - allow-once → allow
      - allow-always → allow（同时标记会话为自动放行）
      - deny → deny
    """
    is_allow_always = payload.resolution == "allow-always"
    log.info(
        "审批决策开始",
        approval_id=approval_id,
        requested_resolution=payload.resolution,
        is_allow_always=is_allow_always,
    )

    # 如果是 allow-always，标记该会话为自动放行
    if is_allow_always:
        handle_allow_always(approval_id)

    resolution_map = {
        "allow-once": "allow",
        "allow-always": "allow",
        "deny": "deny",
    }
    mapped_resolution = resolution_map.get(payload.resolution, payload.resolution)
    resolved = await engine.approval_service.aresolve_request(
        approval_id,
        mapped_resolution,
        is_allow_always=is_allow_always,
    )
    if resolved is None:
        log.warning(
            "审批决策失败",
            approval_id=approval_id,
            requested_resolution=payload.resolution,
            mapped_resolution=mapped_resolution,
            reason="approval_not_found_or_already_resolved",
        )
        raise HTTPException(status_code=404, detail="approval not found or already resolved")

    response = ApprovalStatusResponse(
        approval_id=resolved.id,
        tool_name=resolved.tool_name,
        assistant_id=resolved.assistant_id,
        capability_profile=resolved.capability_profile,
        approval_scope=resolved.approval_scope,
        reason=resolved.reason,
        target=resolved.target,
        status=resolved.status,
        resolution=resolved.resolution,
        requested_at=resolved.requested_at,
        expires_at=resolved.expires_at,
        resolved_at=resolved.resolved_at,
    )
    log.info(
        "审批决策完成",
        approval_id=resolved.id,
        assistant_id=resolved.assistant_id,
        approval_scope=resolved.approval_scope,
        requested_resolution=payload.resolution,
        mapped_resolution=mapped_resolution,
        status=resolved.status,
        resolution=resolved.resolution,
    )
    return response


@router.get("/api/v1/debug/auto-allow")
async def debug_auto_allow():
    """诊断端点：查看当前 auto-allow 状态"""
    from src.security_service import session_auto_allow

    conv_set = session_auto_allow._auto_allow_conversations
    approval_map = session_auto_allow._approval_to_conversation
    return {
        "auto_allow_conversations": [list(k) for k in conv_set],
        "pending_approval_conversations": {k: list(v) for k, v in approval_map.items()},
        "auto_allow_count": len(conv_set),
        "pending_approval_count": len(approval_map),
    }


@router.delete("/api/v1/auto-allow")
async def revoke_auto_allow(
    request: Request,
    assistant_id: str,
    conversation_id: str,
):
    """撤销指定对话的 Always Allow 状态，使其每次都需要重新授权"""
    from src.security_service.session_auto_allow import clear_conversation

    clear_conversation(assistant_id, conversation_id)
    return {
        "status": "ok",
        "revoked": True,
        "assistant_id": assistant_id,
        "conversation_id": conversation_id,
    }


@router.post(
    "/api/v1/control-ui/launch-tickets",
    response_model=ControlUiLaunchTicketCreateResponse,
    dependencies=[Depends(require_internal_service_token)],
)
async def create_control_ui_launch_ticket(
    payload: ControlUiLaunchTicketCreateRequest,
) -> ControlUiLaunchTicketCreateResponse:
    settings = get_settings()
    (
        resolved_gateway_url,
        resolved_gateway_token,
    ) = await _resolve_live_control_ui_gateway_credentials(payload)
    issued = launch_ticket_store.issue(
        assistant_id=payload.assistant_id,
        gateway_url=resolved_gateway_url,
        gateway_token=resolved_gateway_token,
        session_key=payload.session_key,
        control_ui_url=payload.control_ui_url,
        ttl_seconds=settings.control_ui_launch_ticket_ttl_seconds,
    )
    await _aappend_control_session_audit(
        action="create",
        assistant_id=payload.assistant_id,
        session_key=payload.session_key,
        success=True,
    )
    return ControlUiLaunchTicketCreateResponse(
        ticket=issued.ticket,
        expires_at=issued.expires_at,
    )


@router.post(
    "/api/v1/control-ui/launch-tickets/resolve",
    response_model=ControlUiLaunchTicketResolveResponse,
)
async def resolve_control_ui_launch_ticket(
    payload: ControlUiLaunchTicketResolveRequest,
    request: Request,
) -> ControlUiLaunchTicketResolveResponse:
    try:
        resolved = launch_ticket_store.consume(
            payload.ticket,
            request_origin=request.headers.get("origin"),
        )
    except ControlUiLaunchTicketOriginMismatchError as exc:
        await _aappend_control_session_audit(
            action="reject",
            assistant_id=None,
            session_key=None,
            success=False,
            error_code="origin_mismatch",
            error_message=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except ControlUiLaunchTicketExpiredError as exc:
        await _aappend_control_session_audit(
            action="expire",
            assistant_id=None,
            session_key=None,
            success=False,
            error_code="expired",
            error_message=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=str(exc),
        ) from exc
    except ControlUiLaunchTicketNotFoundError as exc:
        await _aappend_control_session_audit(
            action="reject",
            assistant_id=None,
            session_key=None,
            success=False,
            error_code="not_found",
            error_message=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    relay_session = control_ui_relay_session_store.issue(
        assistant_id=resolved.assistant_id,
        gateway_url=resolved.gateway_url,
        gateway_token=resolved.gateway_token,
        session_key=resolved.session_key,
        expires_at=resolved.expires_at,
    )
    await _aappend_control_session_audit(
        action="consume",
        assistant_id=resolved.assistant_id,
        session_key=resolved.session_key,
        success=True,
    )
    trusted_gateway = is_internal_service_request(request)

    return ControlUiLaunchTicketResolveResponse(
        assistant_id=resolved.assistant_id,
        gateway_url=(
            resolved.gateway_url
            if trusted_gateway
            else build_control_ui_relay_url(
                scheme=request.url.scheme,
                netloc=request.url.netloc,
            )
        ),
        gateway_token=resolved.gateway_token if trusted_gateway else relay_session.token,
        admin_token=resolved.gateway_token,
        session_key=resolved.session_key,
        expires_at=resolved.expires_at,
    )


@router.websocket("/api/v1/control-ui/gateway-relay/ws")
async def control_ui_gateway_relay(websocket: WebSocket) -> None:
    await handle_control_ui_gateway_relay(websocket)


@router.get("/api/v1/control-ui/assistant-context")
async def get_control_ui_assistant_context(
    workspace_file_service: WorkspaceFileService = workspace_file_service_dependency,
) -> dict[str, list[dict[str, str]]]:
    assistant_service = AssistantService()
    assistants = await run_in_threadpool(assistant_service.list_assistants)
    assistant_entries = [
        {
            "id": item.assistant_id,
            "status": item.runtime.status,
        }
        for item in assistants
        if item.assistant_id and not item.deleted
    ]
    if not assistant_entries:
        files = await workspace_file_service.file_repository.alist_files(
            assistant_id=None,
            bucket=None,
            include_deleted=False,
        )
        inferred_ids = sorted(
            {item.assistant_id.strip() for item in files if item.assistant_id.strip()}
        )
        inferred_assistants = await run_in_threadpool(assistant_service.list_assistants)
        active_inferred_ids = {
            item.assistant_id
            for item in inferred_assistants
            if item.assistant_id in inferred_ids and not item.deleted
        }
        assistant_entries = [
            {
                "id": assistant_id,
                "status": "running" if len(active_inferred_ids) == 1 else "",
            }
            for assistant_id in inferred_ids
            if assistant_id in active_inferred_ids
        ]
    return {"assistants": assistant_entries}
