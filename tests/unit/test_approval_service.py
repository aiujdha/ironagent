import asyncio
from datetime import UTC, datetime, timedelta

from src.approval.models import ApprovalRequest, DEFAULT_APPROVAL_TIMEOUT_MINUTES
from src.approval.service import ApprovalService
from src.approval.watchdog import ApprovalWatchdog
from src.shared.repositories import JsonAuditRepository


def test_approval_service_creates_pending_request(tmp_path) -> None:
    audit_repository = JsonAuditRepository(tmp_path / "audit.jsonl")
    service = ApprovalService(
        store_path=tmp_path / "approvals.json",
        audit_repository=audit_repository,
    )
    item = service.create_request(
        "exec",
        assistant_id="demo-main",
        capability_profile="dangerous",
    )
    assert item.tool_name == "exec"
    assert item.status == "pending"
    assert item.assistant_id == "demo-main"
    assert item.capability_profile == "dangerous"
    assert len(service.list_pending()) == 1
    audit_event = audit_repository.list_recent(limit=1)[0]
    assert audit_event["envelope"]["hook_name"] == "approval"
    assert audit_event["decision"] == "pending_approval"
    assert audit_event["result"]["success"] is None


def test_approval_service_default_timeout_is_15_minutes(tmp_path) -> None:
    service = ApprovalService(store_path=tmp_path / "approvals.json")

    item = service.create_request("exec")

    timeout_seconds = (item.expires_at - item.requested_at).total_seconds()
    assert DEFAULT_APPROVAL_TIMEOUT_MINUTES * 60 <= timeout_seconds <= (
        DEFAULT_APPROVAL_TIMEOUT_MINUTES * 60 + 1
    )


def test_approval_service_resolves_request(tmp_path) -> None:
    audit_repository = JsonAuditRepository(tmp_path / "audit.jsonl")
    service = ApprovalService(
        store_path=tmp_path / "approvals.json",
        audit_repository=audit_repository,
    )
    item = service.create_request("exec")

    resolved = service.resolve_request(item.id, "allow")

    assert resolved is not None
    assert resolved.status == "approved"
    assert resolved.resolution == "allow"
    assert service.list_pending() == []
    audit_events = audit_repository.list_recent(limit=10)
    assert len(audit_events) == 1
    assert audit_events[0]["envelope"]["hook_name"] == "approval"
    assert audit_events[0]["decision"] == "allow"
    assert audit_events[0]["result"]["success"] is True


def test_approval_service_records_deny_resolution_as_successful_operation(tmp_path) -> None:
    audit_repository = JsonAuditRepository(tmp_path / "audit.jsonl")
    service = ApprovalService(
        store_path=tmp_path / "approvals.json",
        audit_repository=audit_repository,
    )
    item = service.create_request("workspace_file_delete")

    resolved = service.resolve_request(item.id, "deny")

    assert resolved is not None
    assert resolved.status == "denied"
    assert resolved.resolution == "deny"
    audit_events = audit_repository.list_recent(limit=10)
    assert len(audit_events) == 1
    assert audit_events[0]["decision"] == "deny"
    assert audit_events[0]["result"]["success"] is True


def test_approval_service_get_request_returns_existing_item(tmp_path) -> None:
    service = ApprovalService(store_path=tmp_path / "approvals.json")
    item = service.create_request("exec")

    fetched = service.get_request(item.id)

    assert fetched is not None
    assert fetched.id == item.id
    assert fetched.tool_name == "exec"


def test_approval_service_filters_requests_by_scope_and_status(tmp_path) -> None:
    service = ApprovalService(store_path=tmp_path / "approvals.json")
    user_item = service.create_request("exec", approval_scope="user")
    service.create_request("curl", approval_scope="admin")
    service.resolve_request(user_item.id, "allow")

    user_requests = service.list_requests(approval_scope="user")
    admin_pending = service.list_pending(approval_scope="admin")
    approved = service.list_requests(status="approved")

    assert len(user_requests) == 1
    assert user_requests[0].approval_scope == "user"
    assert len(admin_pending) == 1
    assert admin_pending[0].approval_scope == "admin"
    assert len(approved) == 1
    assert approved[0].id == user_item.id


def test_approval_service_filters_requests_by_assistant_id(tmp_path) -> None:
    service = ApprovalService(store_path=tmp_path / "approvals.json")
    service.create_request("exec", assistant_id="assistant-a")
    service.create_request("curl", assistant_id="assistant-b")

    assistant_a_requests = service.list_requests(assistant_id="assistant-a")
    assistant_b_pending = service.list_pending(assistant_id="assistant-b")

    assert len(assistant_a_requests) == 1
    assert assistant_a_requests[0].assistant_id == "assistant-a"
    assert len(assistant_b_pending) == 1
    assert assistant_b_pending[0].assistant_id == "assistant-b"


def test_approval_service_expires_timed_out_pending_requests(tmp_path) -> None:
    service = ApprovalService(
        store_path=tmp_path / "approvals.json",
        audit_repository=JsonAuditRepository(tmp_path / "audit.jsonl"),
    )
    item = service.create_request("exec")
    item.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    service.repository.save_request(item)

    pending = service.list_pending()
    expired = service.list_requests(status="expired")

    assert pending == []
    assert len(expired) == 1
    assert expired[0].id == item.id
    assert expired[0].status == "expired"
    assert expired[0].resolution == "deny"
    assert expired[0].resolved_at is not None


def test_approval_service_writes_audit_event_when_request_expires(tmp_path) -> None:
    audit_repository = JsonAuditRepository(tmp_path / "audit.jsonl")
    service = ApprovalService(
        store_path=tmp_path / "approvals.json",
        audit_repository=audit_repository,
    )
    item = service.create_request(
        "workspace_file_delete",
        assistant_id="demo-main",
        capability_profile="chat",
        approval_scope="user",
        reason="delete temp file",
        target="data/workspaces/demo-main/temp/a.txt",
    )
    item.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    service.repository.save_request(item)

    service.list_pending()

    audit_events = audit_repository.list_recent(limit=10)
    assert len(audit_events) == 1
    event = audit_events[0]
    assert event["envelope"]["request_id"] == item.id
    assert event["envelope"]["hook_name"] == "approval"
    assert event["tool"]["name"] == "workspace_file_delete"
    assert event["decision"] == "timeout"
    assert event["result"]["success"] is False
    assert event["approval"]["status"] == "expired"


def test_approval_service_get_request_expires_stale_pending_request(tmp_path) -> None:
    service = ApprovalService(
        store_path=tmp_path / "approvals.json",
        audit_repository=JsonAuditRepository(tmp_path / "audit.jsonl"),
    )
    item = service.create_request("exec")
    item.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    service.repository.save_request(item)

    fetched = service.get_request(item.id)

    assert fetched is not None
    assert fetched.status == "expired"
    assert fetched.resolution == "deny"


def test_approval_service_does_not_allow_expired_request(tmp_path) -> None:
    service = ApprovalService(
        store_path=tmp_path / "approvals.json",
        audit_repository=JsonAuditRepository(tmp_path / "audit.jsonl"),
    )
    item = service.create_request("exec")
    item.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    service.repository.save_request(item)

    resolved = service.resolve_request(item.id, "allow")

    assert resolved is not None
    assert resolved.status == "expired"
    assert resolved.resolution == "deny"


def test_approval_watchdog_uses_approval_expiry_window_by_default() -> None:
    recorded: dict[str, int] = {}
    watchdog = ApprovalWatchdog()

    async def fake_wait_for_timeout(
        *,
        approval_id: str,
        tool_name: str,
        assistant_id: str | None,
        timeout_seconds: int,
    ) -> None:
        recorded["timeout_seconds"] = timeout_seconds

    watchdog._wait_for_timeout = fake_wait_for_timeout  # type: ignore[method-assign]

    async def run() -> None:
        approval = ApprovalRequest("exec")
        await watchdog.register(approval)
        await asyncio.sleep(0)
        watchdog.stop()

    asyncio.run(run())

    assert recorded["timeout_seconds"] >= DEFAULT_APPROVAL_TIMEOUT_MINUTES * 60 - 1
