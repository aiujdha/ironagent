from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from threading import Lock
from urllib.parse import urlsplit


def _utc_now() -> datetime:
    return datetime.now(UTC)


def normalize_origin(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


@dataclass(frozen=True)
class IssuedControlUiLaunchTicket:
    ticket: str
    expires_at: datetime


@dataclass(frozen=True)
class ResolvedControlUiLaunchTicket:
    assistant_id: str | None
    gateway_url: str
    gateway_token: str
    session_key: str
    expires_at: datetime


@dataclass(frozen=True)
class IssuedControlUiRelaySession:
    token: str
    expires_at: datetime


@dataclass(frozen=True)
class ResolvedControlUiRelaySession:
    assistant_id: str | None
    gateway_url: str
    gateway_token: str
    session_key: str
    expires_at: datetime


@dataclass
class _StoredControlUiLaunchTicket:
    assistant_id: str | None
    gateway_url: str
    gateway_token: str
    session_key: str
    allowed_origin: str | None
    expires_at: datetime


@dataclass
class _StoredControlUiRelaySession:
    assistant_id: str | None
    gateway_url: str
    gateway_token: str
    session_key: str
    expires_at: datetime


class ControlUiLaunchTicketError(RuntimeError):
    pass


class ControlUiLaunchTicketNotFoundError(ControlUiLaunchTicketError):
    pass


class ControlUiLaunchTicketExpiredError(ControlUiLaunchTicketError):
    pass


class ControlUiLaunchTicketOriginMismatchError(ControlUiLaunchTicketError):
    pass


class ControlUiRelaySessionNotFoundError(ControlUiLaunchTicketError):
    pass


class ControlUiRelaySessionExpiredError(ControlUiLaunchTicketError):
    pass


class ControlUiLaunchTicketStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._records: dict[str, _StoredControlUiLaunchTicket] = {}

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def issue(
        self,
        *,
        assistant_id: str | None,
        gateway_url: str,
        gateway_token: str,
        session_key: str,
        control_ui_url: str | None,
        ttl_seconds: int,
    ) -> IssuedControlUiLaunchTicket:
        now = _utc_now()
        expires_at = now + timedelta(seconds=max(ttl_seconds, 0))
        ticket = token_urlsafe(32)
        record = _StoredControlUiLaunchTicket(
            assistant_id=assistant_id.strip() or None if assistant_id else None,
            gateway_url=gateway_url.strip(),
            gateway_token=gateway_token.strip(),
            session_key=session_key.strip() or "main",
            allowed_origin=normalize_origin(control_ui_url),
            expires_at=expires_at,
        )
        with self._lock:
            self._prune_locked(now)
            self._records[ticket] = record
        return IssuedControlUiLaunchTicket(ticket=ticket, expires_at=expires_at)

    def consume(
        self,
        ticket: str,
        *,
        request_origin: str | None,
    ) -> ResolvedControlUiLaunchTicket:
        normalized_ticket = ticket.strip()
        if not normalized_ticket:
            raise ControlUiLaunchTicketNotFoundError("launch ticket not found")

        now = _utc_now()
        with self._lock:
            record = self._records.pop(normalized_ticket, None)
            self._prune_locked(now)

        if record is None:
            raise ControlUiLaunchTicketNotFoundError("launch ticket not found")
        if record.expires_at <= now:
            raise ControlUiLaunchTicketExpiredError("launch ticket expired")
        normalized_origin = normalize_origin(request_origin)
        if (
            record.allowed_origin
            and normalized_origin
            and normalized_origin != record.allowed_origin
        ):
            raise ControlUiLaunchTicketOriginMismatchError("launch ticket origin mismatch")

        return ResolvedControlUiLaunchTicket(
            assistant_id=record.assistant_id,
            gateway_url=record.gateway_url,
            gateway_token=record.gateway_token,
            session_key=record.session_key,
            expires_at=record.expires_at,
        )

    def _prune_locked(self, now: datetime) -> None:
        expired = [ticket for ticket, record in self._records.items() if record.expires_at <= now]
        for ticket in expired:
            self._records.pop(ticket, None)


launch_ticket_store = ControlUiLaunchTicketStore()


class ControlUiRelaySessionStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._records: dict[str, _StoredControlUiRelaySession] = {}

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def issue(
        self,
        *,
        assistant_id: str | None,
        gateway_url: str,
        gateway_token: str,
        session_key: str,
        expires_at: datetime,
    ) -> IssuedControlUiRelaySession:
        now = _utc_now()
        token = token_urlsafe(32)
        record = _StoredControlUiRelaySession(
            assistant_id=assistant_id.strip() or None if assistant_id else None,
            gateway_url=gateway_url.strip(),
            gateway_token=gateway_token.strip(),
            session_key=session_key.strip() or "main",
            expires_at=expires_at,
        )
        with self._lock:
            self._prune_locked(now)
            self._records[token] = record
        return IssuedControlUiRelaySession(token=token, expires_at=expires_at)

    def resolve(self, token: str) -> ResolvedControlUiRelaySession:
        normalized_token = token.strip()
        if not normalized_token:
            raise ControlUiRelaySessionNotFoundError("relay session not found")

        now = _utc_now()
        with self._lock:
            record = self._records.get(normalized_token)
            self._prune_locked(now)

        if record is None:
            raise ControlUiRelaySessionNotFoundError("relay session not found")
        if record.expires_at <= now:
            raise ControlUiRelaySessionExpiredError("relay session expired")

        return ResolvedControlUiRelaySession(
            assistant_id=record.assistant_id,
            gateway_url=record.gateway_url,
            gateway_token=record.gateway_token,
            session_key=record.session_key,
            expires_at=record.expires_at,
        )

    def _prune_locked(self, now: datetime) -> None:
        expired = [token for token, record in self._records.items() if record.expires_at <= now]
        for token in expired:
            self._records.pop(token, None)


control_ui_relay_session_store = ControlUiRelaySessionStore()
