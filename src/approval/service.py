from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from src.approval.models import ApprovalRequest
from src.approval.timeout import expire_if_timed_out
from src.shared.async_utils import run_sync
from src.shared.config import get_settings
from src.shared.logging import get_logger
from src.shared.repositories import (
    ApprovalRepository,
    AuditRepository,
    JsonApprovalRepository,
    JsonAuditRepository,
    build_approval_repository,
    build_audit_repository,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


log = get_logger(__name__)


class ApprovalService:
    def __init__(
        self,
        store_path: Path | None = None,
        repository: ApprovalRepository | None = None,
        audit_repository: AuditRepository | None = None,
    ) -> None:
        explicit_store_path = store_path
        self.store_path = explicit_store_path or get_settings().approval_store_path
        if repository is not None:
            self.repository = repository
        elif explicit_store_path is not None:
            self.repository = JsonApprovalRepository(self.store_path)
        else:
            self.repository = build_approval_repository()
        if audit_repository is not None:
            self.audit_repository = audit_repository
        elif store_path is not None:
            self.audit_repository = JsonAuditRepository(self.store_path.with_name("audit.jsonl"))
        else:
            self.audit_repository = build_audit_repository()

    def create_request(
        self,
        tool_name: str,
        *,
        assistant_id: str | None = None,
        capability_profile: str | None = None,
        approval_scope: Literal["user", "admin"] = "admin",
        reason: str | None = None,
        target: str | None = None,
        gateway_url: str | None = None,
    ) -> ApprovalRequest:
        return run_sync(
            self.acreate_request(
                tool_name,
                assistant_id=assistant_id,
                capability_profile=capability_profile,
                approval_scope=approval_scope,
                reason=reason,
                target=target,
                gateway_url=gateway_url,
        )
            )

    async def acreate_request(
        self,
        tool_name: str,
        *,
        assistant_id: str | None = None,
        capability_profile: str | None = None,
        approval_scope: Literal["user", "admin"] = "admin",
        reason: str | None = None,
        target: str | None = None,
        gateway_url: str | None = None,
    ) -> ApprovalRequest:
        # 如果没有提供 gateway_url，尝试根据 assistant_id 查询
        if not gateway_url and assistant_id:
            gateway_url = await self._aget_gateway_url_for_assistant(assistant_id)

        item = ApprovalRequest(
            tool_name=tool_name,
            assistant_id=assistant_id,
            capability_profile=capability_profile,
            approval_scope=approval_scope,
            reason=reason,
            target=target,
            gateway_url=gateway_url,
        )
        saved = await self.repository.acreate_request(item)

        # 只对 admin 级别的审批注册超时 watchdog
        # user 级别由 IronAgent 自身处理（timeoutBehavior 配置）
        if approval_scope == "admin":
            from src.approval.watchdog import register_approval_timeout
            await register_approval_timeout(saved)

        await self._aappend_approval_audit(
            saved,
            decision="pending_approval",
            success=None,
            message="approval request created",
        )
        return saved

    async def _aget_gateway_url_for_assistant(self, assistant_id: str) -> str | None:
        """根据 assistant_id 获取 Gateway URL"""
        try:
            from src.assistants.service import AssistantService
            service = AssistantService()
            assistant = await service.aget_assistant(assistant_id)
            if assistant and assistant.runtime:
                return assistant.runtime.gateway_url
        except Exception:
            pass
        return None

    def list_requests(
        self,
        *,
        approval_scope: Literal["user", "admin"] | None = None,
        status: str | None = None,
        assistant_id: str | None = None,
    ) -> list[ApprovalRequest]:
        return run_sync(
            self.alist_requests(
                assistant_id=assistant_id,
                approval_scope=approval_scope,
                status=status,
        )
            )

    async def alist_requests(
        self,
        *,
        approval_scope: Literal["user", "admin"] | None = None,
        status: str | None = None,
        assistant_id: str | None = None,
    ) -> list[ApprovalRequest]:
        await self.aexpire_pending_requests(
            approval_scope=approval_scope,
            assistant_id=assistant_id,
        )
        return await self.repository.alist_requests(
            assistant_id=assistant_id,
            approval_scope=approval_scope,
            status=status,
        )

    def list_pending(
        self,
        *,
        approval_scope: Literal["user", "admin"] | None = None,
        assistant_id: str | None = None,
    ) -> list[ApprovalRequest]:
        return run_sync(
            self.alist_pending(
                approval_scope=approval_scope,
                assistant_id=assistant_id,
        )
            )

    async def alist_pending(
        self,
        *,
        approval_scope: Literal["user", "admin"] | None = None,
        assistant_id: str | None = None,
    ) -> list[ApprovalRequest]:
        return await self.alist_requests(
            approval_scope=approval_scope,
            status="pending",
            assistant_id=assistant_id,
        )

    def get_request(self, approval_id: str) -> ApprovalRequest | None:
        return run_sync(self.aget_request(approval_id))

    async def aget_request(self, approval_id: str) -> ApprovalRequest | None:
        item = await self.repository.aget_request(approval_id)
        if item is None:
            return None
        return await self._aexpire_request_if_needed(item)

    def resolve_request(
        self,
        approval_id: str,
        resolution: str,
        *,
        is_allow_always: bool = False,
    ) -> ApprovalRequest | None:
        return run_sync(
            self.aresolve_request(approval_id, resolution, is_allow_always=is_allow_always)
        )

    async def aresolve_request(
        self,
        approval_id: str,
        resolution: str,
        *,
        is_allow_always: bool = False,
        skip_gateway_notify: bool = False,
    ) -> ApprovalRequest | None:
        trimmed_id = approval_id.strip()
        trimmed_resolution = resolution.strip().lower()
        if not trimmed_id or trimmed_resolution not in {"allow", "deny"}:
            return None

        resolved = await self.repository.aget_request(trimmed_id)
        if resolved is None:
            return None
        resolved = await self._aexpire_request_if_needed(resolved)
        if resolved.status != "pending":
            return resolved

        # 取消 watchdog 超时任务
        from src.approval.watchdog import cancel_approval_timeout
        await cancel_approval_timeout(trimmed_id)

        resolved.status = "approved" if trimmed_resolution == "allow" else "denied"
        resolved.resolution = "allow" if trimmed_resolution == "allow" else "deny"
        resolved.resolved_at = utc_now()
        saved = await self.repository.asave_request(resolved)
        await self._aappend_approval_audit(
            saved,
            decision=saved.resolution or trimmed_resolution,
            success=True,
            message="approval request resolved",
        )

        # 通知 Gateway 审批已解决
        # 无论 user 还是 admin scope 都需要通知，以便 Gateway:
        # 1. resolve ExecApprovalManager 中的 pending 条目
        # 2. 广播 exec.approval.resolved 事件给 UI
        # 3. 解除 bridge 的轮询等待
        # 当审批由 Gateway 事件触发时（skip_gateway_notify=True），跳过通知以避免冗余
        if not skip_gateway_notify:
            try:
                from src.approval.gateway_client import notify_approval_resolved

                await notify_approval_resolved(
                    approval_id=saved.id,
                    tool_name=saved.tool_name,
                    assistant_id=saved.assistant_id,
                    resolution=saved.resolution or trimmed_resolution,
                    gateway_url=saved.gateway_url,
                    is_allow_always=is_allow_always,
                )
            except Exception as exc:
                log.warning(
                    "gateway_notification_failed",
                    approval_id=saved.id,
                    error=str(exc),
                )

        return saved

    def expire_pending_requests(
        self,
        *,
        approval_scope: Literal["user", "admin"] | None = None,
        assistant_id: str | None = None,
    ) -> list[ApprovalRequest]:
        return run_sync(
            self.aexpire_pending_requests(
                approval_scope=approval_scope,
                assistant_id=assistant_id,
        )
            )

    async def aexpire_pending_requests(
        self,
        *,
        approval_scope: Literal["user", "admin"] | None = None,
        assistant_id: str | None = None,
    ) -> list[ApprovalRequest]:
        pending = await self.repository.alist_requests(
            assistant_id=assistant_id,
            approval_scope=approval_scope,
            status="pending",
        )
        expired: list[ApprovalRequest] = []
        now = utc_now()
        for item in pending:
            if expire_if_timed_out(item, now=now):
                saved = await self.repository.asave_request(item)
                await self._aappend_timeout_audit(saved)
                expired.append(saved)
        return expired

    async def _aexpire_request_if_needed(self, item: ApprovalRequest) -> ApprovalRequest:
        if not expire_if_timed_out(item):
            return item
        saved = await self.repository.asave_request(item)
        await self._aappend_timeout_audit(saved)
        return saved

    async def _aappend_timeout_audit(self, item: ApprovalRequest) -> None:
        await self._aappend_approval_audit(
            item,
            decision="timeout",
            success=False,
            message="approval request timed out",
        )

    async def _aappend_approval_audit(
        self,
        item: ApprovalRequest,
        *,
        decision: str,
        success: bool | None,
        message: str,
    ) -> None:
        occurred_at = (item.resolved_at or utc_now()).isoformat()
        await self.audit_repository.aappend_event(
            {
                "envelope": {
                    "request_id": item.id,
                    "hook_name": "approval",
                    "occurred_at": occurred_at,
                },
                "assistant": {
                    "assistant_id": item.assistant_id or "",
                    "capability_profile": item.capability_profile or "",
                },
                "tool": {
                    "name": item.tool_name,
                    "target": item.target or "",
                },
                "decision": decision,
                "result": {
                    "success": success,
                    "reason": message,
                },
                "approval": {
                    "approval_id": item.id,
                    "scope": item.approval_scope,
                    "status": item.status,
                    "resolution": item.resolution,
                    "requested_at": item.requested_at.isoformat(),
                    "expires_at": item.expires_at.isoformat(),
                    "resolved_at": occurred_at,
                    "reason": item.reason or "",
                },
            }
        )
