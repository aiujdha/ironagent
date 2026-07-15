"""
Run 级别的自动放行（Always Allow）管理

当用户在审批弹窗中选择 "Always Allow" 后，同一 run 中所有后续的
user 级别审批请求将被自动放行，无需再次弹窗确认。

关键设计：
- 以 (assistant_id, run_id) 为粒度跟踪
- run_id 对应 IronAgent 的 runId（每次 agent run 重新生成）
- 因此 "Always Allow" 只在当前一轮对话内生效，用户发新消息后需重新授权
- 注意：不使用 session_id（是 session store 中持久化的 ID，跨 run 不变）
- 纯内存存储，服务重启后自动清空（安全侧）
- 当 run_id 缺失时降级为普通审批流程
"""

from __future__ import annotations

import threading
from typing import Tuple

from src.shared.logging import get_logger

log = get_logger(__name__)

# 内部存储 key 类型 — 使用 conversation_id（对话级稳定标识）
_ConversationKey = Tuple[str, str]  # (assistant_id, conversation_id)

# 全局锁
_lock = threading.Lock()

# 已标记为自动放行的对话集合
_auto_allow_conversations: set[_ConversationKey] = set()

# 审批 ID → 对话 key 的映射（用于 resolve 时回查）
_approval_to_conversation: dict[str, _ConversationKey] = {}


def _make_key(assistant_id: str | None, conversation_id: str | None) -> _ConversationKey | None:
    """构造对话 key，任一参数为空则返回 None"""
    if not assistant_id or not conversation_id:
        log.warning(
            "auto_allow_missing_key_component",
            assistant_id=assistant_id,
            conversation_id=conversation_id,
        )
        return None
    return (assistant_id.strip(), conversation_id.strip())


def mark_auto_allow(assistant_id: str | None, conversation_id: str | None) -> None:
    """标记某个对话为自动放行"""
    key = _make_key(assistant_id, conversation_id)
    if key is None:
        log.debug(
            "auto_allow_skip_missing_key",
            assistant_id=assistant_id,
            conversation_id=conversation_id,
        )
        return
    with _lock:
        _auto_allow_conversations.add(key)
    log.info("auto_allow_marked", assistant_id=key[0], conversation_id=key[1])


def is_auto_allowed(assistant_id: str | None, conversation_id: str | None) -> bool:
    """检查某个对话是否已标记为自动放行"""
    key = _make_key(assistant_id, conversation_id)
    if key is None:
        log.info("is_auto_allowed_skip_missing_key", assistant_id=assistant_id, conversation_id=conversation_id)
        return False
    with _lock:
        result = key in _auto_allow_conversations
    log.info(
        "is_auto_allowed_check",
        assistant_id=key[0],
        conversation_id=key[1],
        result=result,
        all_keys=[list(k) for k in _auto_allow_conversations],
    )
    return result


def register_approval_conversation(
    approval_id: str,
    assistant_id: str | None,
    conversation_id: str | None,
) -> None:
    """
    注册审批与对话的映射关系。

    在创建审批时调用，以便后续 resolve 时能根据 approval_id
    找到对应的对话并标记自动放行。
    """
    key = _make_key(assistant_id, conversation_id)
    if key is None:
        return
    with _lock:
        _approval_to_conversation[approval_id.strip()] = key
    log.debug(
        "approval_conversation_registered",
        approval_id=approval_id,
        assistant_id=key[0],
        conversation_id=key[1],
    )


def handle_allow_always(approval_id: str) -> None:
    """
    处理 allow-always 审批解决。

    根据 approval_id 查找对应的对话并标记为自动放行。
    """
    trimmed_id = approval_id.strip()
    with _lock:
        key = _approval_to_conversation.pop(trimmed_id, None)

    if key is None:
        log.debug("auto_allow_no_conversation_mapping", approval_id=trimmed_id)
        return

    with _lock:
        _auto_allow_conversations.add(key)
    log.info(
        "auto_allow_activated_by_approval",
        approval_id=trimmed_id,
        assistant_id=key[0],
        conversation_id=key[1],
    )


def clear_conversation(assistant_id: str | None, conversation_id: str | None) -> None:
    """清除某个对话的自动放行标记"""
    key = _make_key(assistant_id, conversation_id)
    if key is None:
        return
    with _lock:
        _auto_allow_conversations.discard(key)


def clear_session(assistant_id: str | None, session_id: str | None) -> None:
    """向后兼容：clear_conversation 的别名"""
    clear_conversation(assistant_id, session_id)


def clear_all() -> None:
    """清除所有自动放行标记（用于测试）"""
    with _lock:
        _auto_allow_conversations.clear()
        _approval_to_conversation.clear()


# ============ 向后兼容别名（逐步迁移） ============

def register_approval_session(
    approval_id: str,
    assistant_id: str | None,
    session_id: str | None,
) -> None:
    """向后兼容：register_approval_conversation 的别名"""
    # session_id 在旧调用中可能是 run 级别的，这里仍然存储，
    # 但 auto-allow 的查询已经改为用 conversation_id
    register_approval_conversation(approval_id, assistant_id, session_id)
