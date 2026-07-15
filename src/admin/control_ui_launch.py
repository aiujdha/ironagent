from __future__ import annotations

from dataclasses import dataclass

import httpx

from src.shared.auth import internal_service_auth_headers
from src.shared.config import Settings, get_settings
from src.shared.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class ControlUiLaunchTicket:
    ticket: str
    expires_at: str


def resolve_security_service_internal_url(settings: Settings) -> str:
    for candidate in (
        settings.security_service_internal_url,
        settings.security_service_public_url,
        settings.iron_agent_hook_service_url,
        "http://127.0.0.1:8000",
    ):
        normalized = candidate.strip().rstrip("/")
        if normalized:
            return normalized
    return ""


def mint_control_ui_launch_ticket(
    *,
    assistant_id: str | None,
    gateway_url: str,
    gateway_token: str,
    session_key: str = "main",
    control_ui_url: str | None = None,
) -> ControlUiLaunchTicket | None:
    settings = get_settings()
    service_url = resolve_security_service_internal_url(settings)
    launch_control_ui_url = (control_ui_url or settings.control_ui_public_url).strip()
    if not service_url or not launch_control_ui_url:
        return None

    try:
        with httpx.Client(timeout=3.0) as client:
            response = client.post(
                f"{service_url}/api/v1/control-ui/launch-tickets",
                headers=internal_service_auth_headers(settings),
                json={
                    "assistant_id": assistant_id,
                    "gateway_url": gateway_url,
                    "gateway_token": gateway_token,
                    "session_key": session_key,
                    "control_ui_url": launch_control_ui_url,
                },
            )
    except httpx.HTTPError as exc:
        log.warning(
            "control_ui_launch_ticket_issue_failed",
            assistant_id=assistant_id,
            gateway_url=gateway_url,
            service_url=service_url,
            error=str(exc),
        )
        return None

    if response.status_code != 200:
        log.warning(
            "control_ui_launch_ticket_issue_rejected",
            assistant_id=assistant_id,
            gateway_url=gateway_url,
            service_url=service_url,
            status_code=response.status_code,
            detail=response.text,
        )
        return None

    payload = response.json()
    ticket = str(payload.get("ticket", "")).strip()
    expires_at = str(payload.get("expires_at", "")).strip()
    if not ticket or not expires_at:
        log.warning(
            "control_ui_launch_ticket_issue_invalid_payload",
            assistant_id=assistant_id,
            gateway_url=gateway_url,
            service_url=service_url,
            payload=payload,
        )
        return None
    return ControlUiLaunchTicket(ticket=ticket, expires_at=expires_at)
