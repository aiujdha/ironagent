"""
安全决策引擎

实现统一的安全策略决策点 (Policy Decision Point, PDP)：
1. 读取助手 capability profile
2. 提取统一决策上下文
3. 按优先级评估（P0 → P3）
4. 返回最终决策

优先级顺序：
- P0: 系统级黑名单（永久裁剪工具、Secret 路径）
- P3: Profile 授权白名单（allowed_tools 放行，approval_tools 审批）
"""

from pathlib import Path

from src.approval.notifier import notify_pending
from src.approval.service import ApprovalService
from src.assistants.models import AssistantRecord
from src.assistants.service import AssistantService
from src.security.capability import CapabilityProfile, load_capability_profile
from src.security.config_loader import capability_file
from src.security.pruning import load_pruned_tools
from src.security.request_policy import (
    PolicyOutcome,
    RiskLevel,
    evaluate_request_policy,
)
from src.security.whitelist import tool_is_allowed, tool_requires_approval
from src.security_service.schemas import (
    ApprovalContext,
    PreExecuteHookRequest,
    PreExecuteHookResponse,
)
from src.security_service.session_auto_allow import (
    is_auto_allowed,
    register_approval_conversation,
)
from src.shared.async_utils import run_sync
from src.shared.config import approval_mode_disabled, get_settings
from src.shared.logging import get_logger
from src.workspace.models import WorkspacePlacementResult
from src.workspace.placement import maybe_auto_place_tool_args

log = get_logger(__name__)


class SecurityDecisionEngine:
    def __init__(
        self,
        approval_service: ApprovalService | None = None,
        assistant_service: AssistantService | None = None,
    ) -> None:
        self.approval_service = approval_service or ApprovalService()
        self.assistant_service = assistant_service or AssistantService()

    def _load_profile(self, profile_name: str) -> CapabilityProfile:
        path: Path = capability_file(profile_name)
        return load_capability_profile(path)

    async def _resolve_profile_name(self, request: PreExecuteHookRequest) -> str:
        assistant = await self._load_assistant(request)
        if assistant:
            return assistant.capability_profile
        return request.assistant.capability_profile

    async def _load_assistant(self, request: PreExecuteHookRequest) -> AssistantRecord | None:
        assistant_id = request.assistant.assistant_id
        if not assistant_id:
            return None
        async_loader = getattr(self.assistant_service, "aget_assistant", None)
        if callable(async_loader):
            return await async_loader(assistant_id)
        sync_loader = getattr(self.assistant_service, "get_assistant", None)
        if callable(sync_loader):
            return sync_loader(assistant_id)
        return None

    def _get_risk_level(self, request: PreExecuteHookRequest) -> RiskLevel:
        """获取请求的风险等级，默认为 medium"""
        return (request.risk.level if request.risk else "medium") or "medium"

    def evaluate(self, request: PreExecuteHookRequest) -> PreExecuteHookResponse:
        return run_sync(self.aevaluate(request))

    async def aevaluate(self, request: PreExecuteHookRequest) -> PreExecuteHookResponse:
        # 1. 加载助手和 Profile 信息
        assistant = await self._load_assistant(request)
        profile_name = await self._resolve_profile_name(request)
        profile = self._load_profile(profile_name)
        risk_level = self._get_risk_level(request)

        # 2. 处理工具参数放置
        tool_name = request.tool.name
        placement = maybe_auto_place_tool_args(
            tool_name=tool_name,
            tool_args=request.tool.args,
            assistant=assistant,
        )
        effective_request = request
        if placement is not None:
            effective_request = request.model_copy(
                update={
                    "tool": request.tool.model_copy(update={"args": placement.tool_args}),
                }
            )

        # 3. P0: 检查永久裁剪工具
        pruned_tools = load_pruned_tools()
        if tool_name in pruned_tools:
            return PreExecuteHookResponse(
                request_id=request.envelope.request_id,
                decision="deny",
                reason="该工具已被永久禁用",
                capability_profile=profile.name,
                metadata=_merge_metadata({"policy_source": "pruned_tools"}, placement),
            )

        # 4. P0: 统一策略评估（系统黑名单）
        policy_outcome = evaluate_request_policy(
            effective_request,
            assistant,
            profile_name=profile_name,
            risk_level=risk_level,
        )

        if policy_outcome is not None:
            return await self._handle_policy_outcome_async(
                request=request,
                policy_outcome=policy_outcome,
                tool_name=tool_name,
                profile_name=profile_name,
                assistant=assistant,
                placement=placement,
            )

        # 5. P3: Profile 授权白名单评估
        return await self._evaluate_profile_whitelist_async(
            request=effective_request,
            tool_name=tool_name,
            profile=profile,
            assistant=assistant,
            assistant_id=request.assistant.assistant_id,
            profile_name=profile_name,
            risk_level=risk_level,
            placement=placement,
        )

    async def _handle_policy_outcome_async(
        self,
        request: PreExecuteHookRequest,
        policy_outcome: PolicyOutcome,
        tool_name: str,
        profile_name: str,
        assistant: AssistantRecord | None,
        placement: WorkspacePlacementResult | None,
    ) -> PreExecuteHookResponse:
        """处理 P0 策略评估结果"""
        if policy_outcome.decision == "deny":
            return PreExecuteHookResponse(
                request_id=request.envelope.request_id,
                decision="deny",
                reason=policy_outcome.reason,
                capability_profile=profile_name,
                metadata=_merge_metadata(policy_outcome.metadata, placement),
            )

        if policy_outcome.decision == "pending_approval":
            approval_scope = policy_outcome.approval_scope or "admin"
            assistant_id = assistant.assistant_id if assistant else None
            if approval_mode_disabled():
                return PreExecuteHookResponse(
                    request_id=request.envelope.request_id,
                    decision="allow",
                    reason="approval disabled by configuration",
                    capability_profile=profile_name,
                    metadata=_merge_metadata(
                        {
                            **policy_outcome.metadata,
                            "approval_scope": approval_scope,
                            "approval_mode": "disabled",
                            "approval_bypassed": True,
                            "original_reason": policy_outcome.reason,
                        },
                        placement,
                    ),
                    tool_args=placement.tool_args if placement is not None else None,
                )
            # 使用 run_id（每次 agent run 重新生成）做 auto-allow 判断
            # 这样 "Always Allow" 只在当前一轮对话内生效，用户发新消息后需重新授权
            # 注意：session_id 不是每次 run 变化的，而是 session store 中持久化的 ID
            conversation_id = request.session.conversation_id if request.session else None
            session_id = request.session.session_id if request.session else None
            run_id = request.session.run_id if request.session else None

            # 会话级自动放行：如果当前 run 已被用户标记为 "Always Allow"，
            # 且审批范围为 user，则直接放行，不再弹窗
            auto_allowed = is_auto_allowed(assistant_id, run_id)
            log.info(
                "auto_allow_result",
                auto_allowed=auto_allowed,
                assistant_id=assistant_id,
                run_id=run_id,
                session_id=session_id,
            )
            if approval_scope == "user" and auto_allowed:
                return PreExecuteHookResponse(
                    request_id=request.envelope.request_id,
                    decision="allow",
                    reason="auto-allowed by session always-allow",
                    capability_profile=profile_name,
                    metadata=_merge_metadata({
                        **policy_outcome.metadata,
                        "approval_scope": approval_scope,
                        "auto_allowed": True,
                    }, placement),
                    tool_args=placement.tool_args if placement is not None else None,
                )

            # 创建 IronAgent 数据库审批记录（用于审计追踪和状态管理）
            gateway_url = None
            if assistant and hasattr(assistant, 'runtime') and assistant.runtime:
                gateway_url = getattr(assistant.runtime, 'gateway_url', None)

            approval = await self.approval_service.acreate_request(
                tool_name=tool_name,
                assistant_id=assistant_id or "",
                capability_profile=profile_name,
                approval_scope=approval_scope,
                reason=policy_outcome.reason,
                target=_string_metadata(policy_outcome.metadata.get("normalized_path")),
                gateway_url=gateway_url,
            )

            # 注册审批与 run 的映射，以便 resolve 时能回查 run 标记 auto-allow
            register_approval_conversation(approval.id, assistant_id, run_id)

            # 通知 Gateway：无论 user 还是 admin scope 都通知，
            # 以便 IronAgent UI 显示审批对话框。
            # - user scope：用户在对话框中审批 → UI 回调 IronAgent 更新数据库
            # - admin scope：管理员在 admin 后台审批 → IronAgent 通知 Gateway
            session_key = conversation_id if conversation_id else None
            admin_backend_url = _resolve_admin_backend_url()
            await notify_pending(
                tool_name=tool_name,
                approval_id=approval.id,
                assistant_id=assistant_id,
                reason=policy_outcome.reason,
                approval_scope=approval_scope,
                gateway_url=gateway_url,
                session_key=session_key,
                expires_at=approval.expires_at,
                admin_backend_url=admin_backend_url,
            )
            # 添加 approval_scope 到 metadata（用于 bridge 识别审批类型）
            outcome_metadata = dict(policy_outcome.metadata)
            outcome_metadata["approval_scope"] = approval_scope
            return PreExecuteHookResponse(
                request_id=request.envelope.request_id,
                decision="pending_approval",
                reason=policy_outcome.reason,
                capability_profile=profile_name,
                approval=ApprovalContext(
                    approval_id=approval.id,
                    expires_at=approval.expires_at,
                ),
                metadata=_merge_metadata(outcome_metadata, placement),
                tool_args=placement.tool_args if placement is not None else None,
            )

        # allow - 只有在明确允许时才走这里（目前 P0 不返回 allow）
        return PreExecuteHookResponse(
            request_id=request.envelope.request_id,
            decision="allow",
            reason=policy_outcome.reason,
            capability_profile=profile_name,
            metadata=_merge_metadata(policy_outcome.metadata, placement),
            tool_args=placement.tool_args if placement is not None else None,
        )

    async def _evaluate_profile_whitelist_async(
        self,
        request: PreExecuteHookRequest,
        tool_name: str,
        profile: CapabilityProfile,
        assistant: AssistantRecord | None,
        assistant_id: str | None,
        profile_name: str,
        risk_level: RiskLevel,
        placement: WorkspacePlacementResult | None,
    ) -> PreExecuteHookResponse:
        """
        P3: Profile 授权白名单评估

        - 工具在 allowed_tools 中 → allow
        - 工具在 approval_tools 中 → pending_approval
        - 都不在 → deny
        """
        # 工具直接允许
        if tool_is_allowed(profile, tool_name):
            return PreExecuteHookResponse(
                request_id=request.envelope.request_id,
                decision="allow",
                reason="tool allowed by capability profile",
                capability_profile=profile.name,
                metadata=_merge_metadata({}, placement),
                tool_args=placement.tool_args if placement is not None else None,
            )

        # 工具需要审批
        if tool_requires_approval(profile, tool_name):
            approval_scope = "user"
            if approval_mode_disabled():
                return PreExecuteHookResponse(
                    request_id=request.envelope.request_id,
                    decision="allow",
                    reason="approval disabled by configuration",
                    capability_profile=profile.name,
                    metadata=_merge_metadata(
                        {
                            "policy_source": "profile_approval",
                            "profile_level": profile.level,
                            "approval_scope": approval_scope,
                            "approval_mode": "disabled",
                            "approval_bypassed": True,
                            "original_reason": "tool requires approval",
                        },
                        placement,
                    ),
                    tool_args=placement.tool_args if placement is not None else None,
                )
            # 使用 run_id（每次 agent run 重新生成）做 auto-allow 判断
            conversation_id = request.session.conversation_id if request.session else None
            session_id = request.session.session_id if request.session else None
            run_id = request.session.run_id if request.session else None

            # 会话级自动放行：如果当前 run 已被用户标记为 "Always Allow"，
            # 则直接放行，不再弹窗确认
            auto_allowed = is_auto_allowed(assistant_id, run_id)
            log.info(
                "auto_allow_result_p3",
                auto_allowed=auto_allowed,
                assistant_id=assistant_id,
                run_id=run_id,
                session_id=session_id,
            )
            if auto_allowed:
                return PreExecuteHookResponse(
                    request_id=request.envelope.request_id,
                    decision="allow",
                    reason="auto-allowed by session always-allow",
                    capability_profile=profile.name,
                    metadata=_merge_metadata({
                        "policy_source": "profile_approval",
                        "profile_level": profile.level,
                        "approval_scope": approval_scope,
                        "auto_allowed": True,
                    }, placement),
                    tool_args=placement.tool_args if placement is not None else None,
                )

            # 创建 IronAgent 数据库审批记录并通知 Gateway 弹窗
            gateway_url = None
            if assistant and hasattr(assistant, 'runtime') and assistant.runtime:
                gateway_url = getattr(assistant.runtime, 'gateway_url', None)

            approval = await self.approval_service.acreate_request(
                tool_name=tool_name,
                assistant_id=assistant_id or "",
                capability_profile=profile.name,
                approval_scope=approval_scope,
                reason="该操作需要您的确认",
                gateway_url=gateway_url,
            )

            register_approval_conversation(approval.id, assistant_id, run_id)

            session_key = conversation_id if conversation_id else None
            admin_backend_url = _resolve_admin_backend_url()
            await notify_pending(
                tool_name=tool_name,
                approval_id=approval.id,
                assistant_id=assistant_id,
                reason="该操作需要您的确认",
                approval_scope=approval_scope,
                gateway_url=gateway_url,
                session_key=session_key,
                expires_at=approval.expires_at,
                admin_backend_url=admin_backend_url,
            )
            return PreExecuteHookResponse(
                request_id=request.envelope.request_id,
                decision="pending_approval",
                reason="该操作需要您的确认",
                capability_profile=profile.name,
                approval=ApprovalContext(
                    approval_id=approval.id,
                    expires_at=approval.expires_at,
                ),
                metadata=_merge_metadata({
                    "policy_source": "profile_approval",
                    "profile_level": profile.level,
                    "approval_scope": "user",
                }, placement),
                tool_args=placement.tool_args if placement is not None else None,
            )

        # 工具不在白名单中
        return PreExecuteHookResponse(
            request_id=request.envelope.request_id,
            decision="deny",
            reason=f"当前能力配置为「{profile.name}」，不支持该操作",
            capability_profile=profile.name,
            metadata=_merge_metadata({}, placement),
        )


def _string_metadata(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _resolve_admin_backend_url() -> str | None:
    """解析管理后台 URL，优先使用 control_ui_public_url，其次使用 security_service_public_url"""
    settings = get_settings()
    control_ui_url = settings.control_ui_public_url.strip()
    if control_ui_url:
        return control_ui_url.rstrip("/")
    security_url = settings.security_service_public_url.strip()
    if security_url:
        return security_url.rstrip("/")
    return None


def _merge_metadata(
    base: dict[str, object],
    placement: WorkspacePlacementResult | None,
) -> dict[str, object]:
    if placement is None:
        return dict(base)
    merged = dict(base)
    merged.update(placement.metadata)
    return merged
