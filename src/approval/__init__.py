"""Approval workflow modules."""

from src.approval.abort_signal import (
    AbortSignal,
    abort_approval,
    get_abort_registry,
    is_approval_aborted,
    wait_for_approval_abort,
)
from src.approval.watchdog import (
    ApprovalWatchdog,
    TimeoutEvent,
    cancel_approval_timeout,
    get_watchdog,
    register_approval_timeout,
    start_watchdog,
    stop_watchdog,
)

__all__ = [
    # Abort signal
    "AbortSignal",
    "abort_approval",
    "get_abort_registry",
    "is_approval_aborted",
    "wait_for_approval_abort",
    # Watchdog
    "ApprovalWatchdog",
    "TimeoutEvent",
    "cancel_approval_timeout",
    "get_watchdog",
    "register_approval_timeout",
    "start_watchdog",
    "stop_watchdog",
]
