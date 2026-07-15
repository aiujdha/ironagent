import asyncio

from src.shared.logging import get_logger

logger = get_logger("approval.notifier")


async def notify_pending(
    tool_name: str,
    approval_id: str | None = None,
    assistant_id: str | None = None,
    reason: str | None = None,
    approval_scope: str | None = None,
    gateway_url: str | None = None,
    session_key: str | None = None,
    expires_at=None,
    admin_backend_url: str | None = None,
) -> None:
    """
    通知 Gateway 有待审批请求需要处理

    此方法为 async，调用方应 await 以确保通知在返回前发送完成。
    即使通知失败，审批请求仍然有效（bridge 可通过轮询获取状态）。

    Args:
        tool_name: 工具名称
        approval_id: 审批 ID（可选，用于异步通知）
        assistant_id: 助手 ID
        reason: 审批原因
        approval_scope: 审批范围，"user" 或 "admin"
        gateway_url: Gateway URL
        session_key: 可选的 session key
        expires_at: 审批过期时间
        admin_backend_url: 管理后台 URL，用于前端展示审批链接
    """
    # 如果提供了完整信息，通过 WebSocket 通知 Gateway
    if approval_id and reason and approval_scope:
        try:
            from src.approval.gateway_client import notify_approval_requested

            # 使用超时保护，避免通知阻塞过久
            await asyncio.wait_for(
                notify_approval_requested(
                    approval_id=approval_id,
                    tool_name=tool_name,
                    assistant_id=assistant_id,
                    reason=reason,
                    approval_scope=approval_scope,
                    gateway_url=gateway_url,
                    session_key=session_key,
                    expires_at=expires_at,
                    admin_backend_url=admin_backend_url,
                ),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.warning("approval_notify_timeout", approval_id=approval_id)
        except Exception as e:
            logger.warning("approval_notify_failed", approval_id=approval_id, error=str(e))
