"""
审批生命周期守护进程 (Approval Watchdog)

功能：
1. 在创建审批时注册超时任务
2. 使用 asyncio 延时任务处理超时
3. 超时时自动 deny 并通知等待的协程
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Awaitable, Callable
from uuid import uuid4

if TYPE_CHECKING:
    from src.approval.models import ApprovalRequest

logger = logging.getLogger(__name__)
DEFAULT_TIMEOUT_SECONDS = 15 * 60


@dataclass
class WatchdogTask:
    """单个超时监控任务"""
    approval_id: str
    expires_at: datetime
    task: asyncio.Task | None = None
    cancelled: bool = False


@dataclass
class TimeoutEvent:
    """超时事件"""
    approval_id: str
    tool_name: str
    assistant_id: str | None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class ApprovalWatchdog:
    """
    审批超时守护进程
    
    使用方法：
    1. 启动守护进程：watchdog.start()
    2. 创建审批时注册：watchdog.register(approval)
    3. 审批被处理时取消：watchdog.cancel(approval_id)
    4. 停止守护进程：watchdog.stop()
    """
    
    def __init__(
        self,
        timeout_seconds: int | None = None,
        on_timeout: Callable[[TimeoutEvent], Awaitable[None]] | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds or DEFAULT_TIMEOUT_SECONDS
        self._on_timeout = on_timeout
        self._tasks: dict[str, WatchdogTask] = {}
        self._lock = asyncio.Lock()
        self._running = False
        self._cleanup_task: asyncio.Task | None = None
    
    def start(self) -> None:
        """启动守护进程"""
        if self._running:
            return
        self._running = True
        logger.info("Approval Watchdog started")
    
    def stop(self) -> None:
        """停止守护进程"""
        self._running = False
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
        # 取消所有待处理的任务
        for task_info in self._tasks.values():
            if task_info.task and not task_info.task.done():
                task_info.task.cancel()
        self._tasks.clear()
        logger.info("Approval Watchdog stopped")
    
    async def register(
        self,
        approval: ApprovalRequest,
        timeout_seconds: int | None = None,
    ) -> None:
        """
        注册一个审批超时监控
        
        Args:
            approval: 审批请求
            timeout_seconds: 自定义超时秒数；留空时按审批 expires_at 计算
        """
        if not self._running:
            self.start()

        timeout = timeout_seconds
        if timeout is None:
            remaining_seconds = int((approval.expires_at - datetime.now(UTC)).total_seconds())
            timeout = max(remaining_seconds, 0)
        if timeout <= 0:
            timeout = self._timeout_seconds
        
        async with self._lock:
            # 如果已存在，先取消旧的
            if approval.id in self._tasks:
                await self.cancel(approval.id)
            
            # 创建超时任务
            task_info = WatchdogTask(
                approval_id=approval.id,
                expires_at=approval.expires_at,
            )
            
            # 创建异步任务
            task = asyncio.create_task(
                self._wait_for_timeout(
                    approval_id=approval.id,
                    tool_name=approval.tool_name,
                    assistant_id=approval.assistant_id,
                    timeout_seconds=timeout,
            )
                )
            task_info.task = task
            self._tasks[approval.id] = task_info
            
            logger.debug(
                f"Registered approval {approval.id} for timeout in {timeout}s"
            )
    
    async def cancel(self, approval_id: str) -> bool:
        """
        取消一个审批的超时监控
        
        Args:
            approval_id: 审批 ID
            
        Returns:
            True 如果取消成功，False 如果不存在
        """
        async with self._lock:
            task_info = self._tasks.pop(approval_id, None)
            if task_info is None:
                return False
            
            task_info.cancelled = True
            if task_info.task and not task_info.task.done():
                task_info.task.cancel()
            
            logger.debug(f"Cancelled timeout for approval {approval_id}")
            return True
    
    async def _wait_for_timeout(
        self,
        approval_id: str,
        tool_name: str,
        assistant_id: str | None,
        timeout_seconds: int,
    ) -> None:
        """等待超时"""
        try:
            await asyncio.sleep(timeout_seconds)
            
            # 检查任务是否被取消
            async with self._lock:
                task_info = self._tasks.get(approval_id)
                if task_info is None or task_info.cancelled:
                    return
            
            # 触发超时回调
            event = TimeoutEvent(
                approval_id=approval_id,
                tool_name=tool_name,
                assistant_id=assistant_id,
            )
            
            if self._on_timeout:
                await self._on_timeout(event)
            else:
                # 默认行为：自动 deny 并发送 abort 信号
                await self._default_timeout_handler(event)
            
            logger.info(
                f"Approval {approval_id} timed out after {timeout_seconds}s"
            )
            
        except asyncio.CancelledError:
            # 任务被取消，正常退出
            pass
        except Exception as e:
            logger.error(f"Error in watchdog timeout handler: {e}")
        finally:
            async with self._lock:
                self._tasks.pop(approval_id, None)
    
    async def _default_timeout_handler(self, event: TimeoutEvent) -> None:
        """默认超时处理：自动 deny 并发送 abort 信号"""
        from src.approval.abort_signal import abort_approval
        
        # 1. 发送 abort 信号，唤醒所有等待此审批的协程
        abort_approval(event.approval_id)
        
        # 2. 自动将 DB 状态更新为 denied
        from src.approval.service import ApprovalService
        service = ApprovalService()
        item = await service.aget_request(event.approval_id)
        if item and item.status == "pending":
            item.status = "denied"
            item.resolution = "deny"
            item.resolved_at = event.occurred_at
            from datetime import UTC
            from src.shared.repositories import build_approval_repository
            repo = build_approval_repository()
            await repo.asave_request(item)
            
            logger.info(
                f"Auto denied approval {event.approval_id}: System Auto Denied - Timeout"
            )


# 全局单例
_watchdog_instance: ApprovalWatchdog | None = None


def get_watchdog() -> ApprovalWatchdog:
    """获取全局 Watchdog 实例"""
    global _watchdog_instance
    if _watchdog_instance is None:
        _watchdog_instance = ApprovalWatchdog()
    return _watchdog_instance


async def register_approval_timeout(approval: ApprovalRequest) -> None:
    """快捷函数：注册审批超时"""
    watchdog = get_watchdog()
    await watchdog.register(approval)


async def cancel_approval_timeout(approval_id: str) -> None:
    """快捷函数：取消审批超时"""
    watchdog = get_watchdog()
    await watchdog.cancel(approval_id)


def start_watchdog() -> None:
    """启动全局 Watchdog"""
    get_watchdog().start()


def stop_watchdog() -> None:
    """停止全局 Watchdog"""
    if _watchdog_instance:
        _watchdog_instance.stop()
