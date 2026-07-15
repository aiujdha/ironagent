from datetime import UTC, datetime

from src.approval.models import ApprovalRequest


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def has_timed_out(item: ApprovalRequest) -> bool:
    return datetime.now(UTC) >= _ensure_utc(item.expires_at)


def expire_if_timed_out(
    item: ApprovalRequest,
    *,
    now: datetime | None = None,
) -> bool:
    if item.status != "pending":
        return False
    current_time = _ensure_utc(now or datetime.now(UTC))
    expires_at = _ensure_utc(item.expires_at)
    if current_time < expires_at:
        return False
    item.status = "expired"
    item.resolution = "deny"
    item.resolved_at = current_time
    return True
