from __future__ import annotations

from fastapi import Header, HTTPException, Request, status

from src.shared.config import Settings, get_settings

INTERNAL_SERVICE_TOKEN_HEADER = "X-IronAgent-Internal-Token"
INTERNAL_SERVICE_TOKEN_HEADERS = (
    INTERNAL_SERVICE_TOKEN_HEADER,
)


def _bearer_token(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw.lower().startswith("bearer "):
        return ""
    return raw[7:].strip()


def _configured_internal_service_token(settings: Settings | None = None) -> str:
    return (settings or get_settings()).internal_service_token.strip()


def internal_service_auth_headers(settings: Settings | None = None) -> dict[str, str]:
    token = _configured_internal_service_token(settings)
    return {header: token for header in INTERNAL_SERVICE_TOKEN_HEADERS}


def is_internal_service_request(request: Request) -> bool:
    expected = _configured_internal_service_token()
    if not expected:
        return False
    header_tokens = [
        request.headers.get(header, "").strip()
        for header in INTERNAL_SERVICE_TOKEN_HEADERS
    ]
    bearer_token = _bearer_token(request.headers.get("Authorization"))
    return bearer_token == expected or any(token == expected for token in header_tokens)


def require_internal_service_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    expected = _configured_internal_service_token()
    header_tokens = [
        request.headers.get(header, "").strip()
        for header in INTERNAL_SERVICE_TOKEN_HEADERS
    ]
    if expected and (_bearer_token(authorization) == expected or expected in header_tokens):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid internal service token",
    )
