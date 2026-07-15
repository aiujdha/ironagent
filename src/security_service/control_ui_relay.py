from __future__ import annotations

import asyncio
import json
from secrets import token_urlsafe
from time import time
from urllib.parse import SplitResult, urlsplit, urlunsplit

from fastapi import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from src.security_service.launch_tickets import (
    ControlUiRelaySessionExpiredError,
    ControlUiRelaySessionNotFoundError,
    ResolvedControlUiRelaySession,
    control_ui_relay_session_store,
)
from src.shared.logging import get_logger

log = get_logger(__name__)


def _normalize_upstream_scheme(scheme: str) -> str:
    normalized = scheme.strip().lower()
    if normalized == "http":
        return "ws"
    if normalized == "https":
        return "wss"
    return normalized


def _normalize_origin_scheme_for_gateway(scheme: str) -> str:
    normalized = scheme.strip().lower()
    if normalized == "ws":
        return "http"
    if normalized == "wss":
        return "https"
    return normalized


def _is_private_bridge_host(hostname: str) -> bool:
    if not hostname:
        return False
    normalized = hostname.strip().lower()
    if normalized in {"0.0.0.0", "host.docker.internal"}:
        return True
    if normalized.startswith("10."):
        return True
    if normalized.startswith("192.168."):
        return True
    if normalized.startswith("172."):
        parts = normalized.split(".")
        if len(parts) == 4:
            try:
                second_octet = int(parts[1])
            except ValueError:
                return False
            return 16 <= second_octet <= 31
    return False


def _rebuild_url(parts: SplitResult, *, scheme: str, netloc: str) -> str:
    return urlunsplit(
        SplitResult(
            scheme=scheme,
            netloc=netloc,
            path=parts.path,
            query=parts.query,
            fragment=parts.fragment,
    )
        )


def build_control_ui_relay_upstream_candidates(gateway_url: str) -> list[str]:
    parts = urlsplit(gateway_url.strip())
    scheme = _normalize_upstream_scheme(parts.scheme)
    hostname = parts.hostname or ""
    candidates: list[str] = []
    primary_netloc = parts.netloc
    if primary_netloc:
        candidates.append(_rebuild_url(parts, scheme=scheme, netloc=primary_netloc))

    if hostname in {"127.0.0.1", "localhost", "::1"}:
        loopback_netloc = "127.0.0.1"
        if parts.port:
            loopback_netloc = f"{loopback_netloc}:{parts.port}"
        if loopback_netloc != primary_netloc:
            candidates.append(_rebuild_url(parts, scheme=scheme, netloc=loopback_netloc))
        return candidates

    if _is_private_bridge_host(hostname):
        host_gateway_netloc = "host.docker.internal"
        if parts.port:
            host_gateway_netloc = f"{host_gateway_netloc}:{parts.port}"
        loopback_netloc = "127.0.0.1"
        if parts.port:
            loopback_netloc = f"{loopback_netloc}:{parts.port}"
        for netloc in (host_gateway_netloc, loopback_netloc):
            rebuilt = _rebuild_url(parts, scheme=scheme, netloc=netloc)
            if netloc != primary_netloc and rebuilt not in candidates:
                candidates.append(rebuilt)

    return candidates


def build_control_ui_relay_upstream_origin(gateway_url: str) -> str | None:
    parts = urlsplit(gateway_url.strip())
    if not parts.scheme or not parts.netloc:
        return None
    origin_scheme = _normalize_origin_scheme_for_gateway(parts.scheme)
    if not origin_scheme:
        return None
    return urlunsplit(
        SplitResult(
            scheme=origin_scheme,
            netloc=parts.netloc,
            path="",
            query="",
            fragment="",
        )
    ).rstrip("/")


def build_control_ui_relay_url(*, scheme: str, netloc: str) -> str:
    relay_scheme = "wss" if scheme == "https" else "ws"
    return f"{relay_scheme}://{netloc}/api/v1/control-ui/gateway-relay/ws"

def _build_upstream_client(browser_params: dict[str, object]) -> dict[str, str]:
    client = browser_params.get("client")
    if not isinstance(client, dict):
        return {
            "id": "ironagent-control-ui",
            "version": "ironagent-control-ui-relay",
            "platform": "browser",
            "mode": "ui",
        }

    upstream_client = {
        "id": str(client.get("id", "")).strip() or "ironagent-control-ui",
        "version": str(client.get("version", "")).strip() or "ironagent-control-ui-relay",
        "platform": str(client.get("platform", "")).strip() or "browser",
        "mode": str(client.get("mode", "")).strip() or "ui",
    }
    display_name = str(client.get("displayName", "")).strip()
    device_family = str(client.get("deviceFamily", "")).strip()
    if display_name:
        upstream_client["displayName"] = display_name
    if device_family:
        upstream_client["deviceFamily"] = device_family
    return upstream_client


def _build_upstream_connect_params(
    session: ResolvedControlUiRelaySession,
    browser_params: dict[str, object],
) -> dict[str, object]:
    role = browser_params.get("role")
    normalized_role = role if role in {"operator", "node"} else "operator"
    raw_scopes = browser_params.get("scopes")
    scopes = (
        [item for item in raw_scopes if isinstance(item, str) and item.strip()]
        if isinstance(raw_scopes, list)
        else []
    )
    caps = (
        [item for item in browser_params.get("caps", []) if isinstance(item, str) and item.strip()]
        if isinstance(browser_params.get("caps"), list)
        else ["tool-events"]
    )
    user_agent = browser_params.get("userAgent")
    locale = browser_params.get("locale")
    return {
        "minProtocol": 3,
        "maxProtocol": 3,
        "client": _build_upstream_client(browser_params),
        "role": normalized_role,
        "scopes": scopes,
        "caps": caps,
        "auth": {"token": session.gateway_token},
        "userAgent": (
            user_agent
            if isinstance(user_agent, str) and user_agent.strip()
            else "ironagent-control-ui-relay"
        ),
        "locale": locale if isinstance(locale, str) and locale.strip() else "en-US",
    }


async def _receive_json(websocket: WebSocket) -> dict[str, object]:
    payload = await websocket.receive_text()
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("invalid frame")
    return parsed


def _extract_connect_token(frame: dict[str, object]) -> str:
    params = frame.get("params")
    if not isinstance(params, dict):
        return ""
    auth = params.get("auth")
    if not isinstance(auth, dict):
        return ""
    token = auth.get("token")
    return token.strip() if isinstance(token, str) else ""


def _extract_connect_params(frame: dict[str, object]) -> dict[str, object]:
    params = frame.get("params")
    return params if isinstance(params, dict) else {}


async def _resolve_relay_session_from_browser_frame(
    frame: dict[str, object],
) -> ResolvedControlUiRelaySession:
    token = _extract_connect_token(frame)
    try:
        return control_ui_relay_session_store.resolve(token)
    except ControlUiRelaySessionExpiredError as exc:
        raise ValueError(str(exc)) from exc
    except ControlUiRelaySessionNotFoundError as exc:
        raise ValueError(str(exc)) from exc


async def handle_control_ui_gateway_relay(websocket: WebSocket) -> None:
    await websocket.accept()
    browser_connect_nonce = token_urlsafe(16)
    await websocket.send_json(
        {
            "type": "event",
            "event": "connect.challenge",
            "payload": {"nonce": browser_connect_nonce, "ts": int(time() * 1000)},
        }
    )

    upstream = None
    try:
        first_frame = await _receive_json(websocket)
        if first_frame.get("type") != "req" or first_frame.get("method") != "connect":
            await websocket.send_json(
                {
                    "type": "res",
                    "id": first_frame.get("id") if isinstance(first_frame.get("id"), str) else "",
                    "ok": False,
                    "error": {
                        "code": "INVALID_REQUEST",
                        "message": "first request must be connect",
                    },
                }
            )
            await websocket.close(code=1008, reason="invalid handshake")
            return

        relay_session = await _resolve_relay_session_from_browser_frame(first_frame)
        upstream_errors: list[str] = []
        for upstream_url in build_control_ui_relay_upstream_candidates(relay_session.gateway_url):
            try:
                upstream = await ws_connect(
                    upstream_url,
                    origin=build_control_ui_relay_upstream_origin(relay_session.gateway_url),
                    open_timeout=5,
                    close_timeout=5,
                    max_size=None,
                    proxy=None,
                )
                break
            except Exception as exc:
                upstream_errors.append(f"{upstream_url}: {exc}")
        if upstream is None:
            raise RuntimeError(
                "all upstream gateway relay targets failed: " + "; ".join(upstream_errors)
            )

        upstream_challenge = json.loads(await asyncio.wait_for(upstream.recv(), timeout=5))
        upstream_nonce = (
            upstream_challenge.get("payload", {}).get("nonce")
            if isinstance(upstream_challenge, dict)
            else None
        )
        if not isinstance(upstream_nonce, str) or not upstream_nonce.strip():
            raise RuntimeError("upstream gateway did not provide connect.challenge nonce")

        browser_connect_params = _extract_connect_params(first_frame)
        upstream_connect_id = f"relay-connect-{token_urlsafe(8)}"
        await upstream.send(
            json.dumps(
                {
                    "type": "req",
                    "id": upstream_connect_id,
                    "method": "connect",
                    "params": _build_upstream_connect_params(
                        relay_session,
                        browser_connect_params,
                    ),
                }
        )
            )

        while True:
            upstream_frame = json.loads(await asyncio.wait_for(upstream.recv(), timeout=10))
            if (
                isinstance(upstream_frame, dict)
                and upstream_frame.get("type") == "res"
                and upstream_frame.get("id") == upstream_connect_id
            ):
                break

        await websocket.send_json(
            {
                "type": "res",
                "id": first_frame.get("id") if isinstance(first_frame.get("id"), str) else "",
                "ok": bool(upstream_frame.get("ok")),
                "payload": upstream_frame.get("payload"),
                "error": upstream_frame.get("error"),
            }
        )
        if not upstream_frame.get("ok"):
            await websocket.close(code=1008, reason="upstream connect failed")
            return

        async def browser_to_upstream() -> None:
            while True:
                message = await websocket.receive_text()
                await upstream.send(message)

        async def upstream_to_browser() -> None:
            while True:
                message = await upstream.recv()
                if isinstance(message, bytes):
                    await websocket.send_bytes(message)
                else:
                    await websocket.send_text(message)

        forward_tasks = [
            asyncio.create_task(browser_to_upstream()),
            asyncio.create_task(upstream_to_browser()),
        ]
        done, pending = await asyncio.wait(forward_tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc
    except (ConnectionClosed, WebSocketDisconnect):
        pass
    except Exception as exc:
        log.warning("control_ui_gateway_relay_failed", error=str(exc))
        try:
            await websocket.close(code=1011, reason="gateway relay failed")
        except Exception:
            pass
    finally:
        if upstream is not None:
            try:
                await upstream.close()
            except Exception:
                pass
