from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from src.security_service.schemas import HOOK_PROTOCOL_VERSION


def utc_now() -> datetime:
    return datetime.now(UTC)


def capability_audit_event(
    *,
    operator_id: str,
    assistant_id: str,
    kind: str,
    capability_id: str,
    slug: str,
    version: str,
    action: str,
    runtime_reload_status: str,
    runtime_reload_error: str = "",
    previous_version: str = "",
    request_id: str | None = None,
) -> dict[str, Any]:
    occurred_at = utc_now()
    runtime_failed = runtime_reload_status == "failed"
    error_code = "RUNTIME_RELOAD_FAILED" if runtime_failed else ""
    result = "runtime_not_effective" if runtime_failed else "success"
    return {
        "envelope": {
            "protocol_version": HOOK_PROTOCOL_VERSION,
            "request_id": request_id or str(uuid4()),
            "source": "admin_web",
            "hook_name": "capability_runtime_reload",
            "occurred_at": occurred_at.isoformat(),
        },
        "operator": {
            "operator_id": operator_id or "admin-web",
        },
        "assistant": {
            "assistant_id": assistant_id,
            "capability_profile": "",
        },
        "capability": {
            "kind": kind,
            "id": capability_id,
            "slug": slug,
            "version": version,
        },
        "tool": {
            "name": action,
            "args": {
                "assistant_id": assistant_id,
                "capability_id": capability_id,
                "capability_slug": slug,
                "version": version,
                "previous_version": previous_version,
        },
            },
        "decision": action,
        "result": {
            "success": not runtime_failed,
            "error": (
                {
                    "code": error_code,
                    "message": runtime_reload_error,
                }
                if runtime_failed
                else None
            ),
            "output_summary": {
                "action": action,
                "result": result,
                "runtime_reload_status": runtime_reload_status,
        },
            },
        "metadata": {
            "operator_id": operator_id or "admin-web",
            "assistant_id": assistant_id,
            "capability_id": capability_id,
            "capability_slug": slug,
            "kind": kind,
            "version": version,
            "previous_version": previous_version,
            "action": action,
            "result": result,
            "error_code": error_code,
            "error_message": runtime_reload_error,
        },
    }
