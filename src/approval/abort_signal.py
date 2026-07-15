"""
审批超时 Abort 信号管理

用于通知正在等待审批的协程超时中止
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Awaitable, Callable
from uuid import uuid4

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class AbortSignal(Exception):
    """中止信号异常"""
    
    def __init__(self, approval_id: str, reason: str = "timeout") -> None:
        self.approval_id = approval_id
        self.reason = reason
        super().__init__(f"Approval {approval_id} aborted: {reason}")


class ApprovalAbortRegistry:
    """
    审批中止信号注册表
    
    管理正在等待审批的协程的中止信号
    """
    
    def __init__(self) -> None:
        self._aborted: set[str] = set()
        self._events: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()
    
    def abort(self, approval_id: str) -> None:
        """
        触发中止信号
        
        Args:
            approval_id: 审批 ID
        """
        if approval_id in self._aborted:
            return
        
        self._aborted.add(approval_id)
        
        # 触发所有等待此审批的事件
        event = self._events.get(approval_id)
        if event:
            event.set()
        
        logger.info(f"Abort signal triggered for approval {approval_id}")
    
    def is_aborted(self, approval_id: str) -> bool:
        """检查是否已中止"""
        return approval_id in self._aborted
    
    def wait_for_abort(
        self,
        approval_id: str,
        timeout: float | None = None,
    ) -> Awaitable[bool]:
        """
        等待中止信号
        
        Args:
            approval_id: 审批 ID
            timeout: 超时时间（秒）
            
        Returns:
            True 如果被中止，False 如果超时
        """
        return _WaitForAbort(self, approval_id, timeout)


class _WaitForAbort:
    """等待中止信号的协程"""
    
    def __init__(
        self,
        registry: ApprovalAbortRegistry,
        approval_id: str,
        timeout: float | None,
    ) -> None:
        self._registry = registry
        self._approval_id = approval_id
        self._timeout = timeout
    
    async def __await__(self) -> bool:
        # 如果已经中止，立即返回
        if self._registry.is_aborted(self._approval_id):
            return True
        
        # 创建事件
        event = asyncio.Event()
        self._registry._events[self._approval_id] = event
        
        try:
            if self._timeout:
                try:
                    await asyncio.wait_for(event.wait(), timeout=self._timeout)
                    return True
                except asyncio.TimeoutError:
                    return False
            else:
                await event.wait()
                return True
        finally:
            # 清理事件
            self._registry._events.pop(self._approval_id, None)


# 全局单例
_abort_registry: ApprovalAbortRegistry | None = None


def get_abort_registry() -> ApprovalAbortRegistry:
    """获取全局 Abort 注册表"""
    global _abort_registry
    if _abort_registry is None:
        _abort_registry = ApprovalAbortRegistry()
    return _abort_registry


def abort_approval(approval_id: str) -> None:
    """快捷函数：触发审批中止"""
    get_abort_registry().abort(approval_id)


def is_approval_aborted(approval_id: str) -> bool:
    """快捷函数：检查审批是否已中止"""
    return get_abort_registry().is_aborted(approval_id)


async def wait_for_approval_abort(
    approval_id: str,
    timeout: float | None = None,
) -> bool:
    """快捷函数：等待审批中止信号"""
    return await get_abort_registry().wait_for_abort(approval_id, timeout)
