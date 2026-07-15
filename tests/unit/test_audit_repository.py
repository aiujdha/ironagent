from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.shared.database import Base
from src.shared.repositories import (
    AuditLogFilters,
    DatabaseAuditRepository,
    JsonAuditRepository,
)


async def build_session_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ironagent.db'}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def test_json_audit_repository_filters_and_archives_expired_events(tmp_path) -> None:
    repository = JsonAuditRepository(tmp_path / "audit.jsonl")
    now = datetime.now(UTC)
    old_event = {
        "envelope": {
            "request_id": "old-req",
            "hook_name": "control_session",
            "occurred_at": (now - timedelta(days=60)).isoformat(),
        },
        "session": {"user_id": "admin-old"},
        "assistant": {"assistant_id": "assistant-old"},
        "tool": {"name": "control_session.expire"},
        "operation_type": "control_session.expire",
        "result": {"success": False, "status": "failure", "error_code": "expired"},
    }
    current_event = {
        "envelope": {
            "request_id": "current-req",
            "hook_name": "control_session",
            "occurred_at": now.isoformat(),
        },
        "session": {"user_id": "admin-current"},
        "assistant": {"assistant_id": "assistant-current"},
        "tool": {"name": "control_session.consume"},
        "operation_type": "control_session.consume",
        "result": {"success": True, "status": "success"},
    }
    repository.append_event(old_event)
    repository.append_event(current_event)

    filtered = repository.list_page(
        limit=10,
        offset=0,
        filters=AuditLogFilters(
            user_id="admin-current",
            assistant_id="assistant-current",
            operation_type="consume",
            result="success",
        ),
    )
    cleanup = repository.cleanup_expired(
        retention_days=45,
        archive_path=tmp_path / "archive.jsonl",
    )

    assert [row["envelope"]["request_id"] for row in filtered.items] == ["current-req"]
    assert cleanup.deleted == 1
    assert cleanup.archived == 1
    assert cleanup.remaining == 1
    assert "old-req" in (tmp_path / "archive.jsonl").read_text(encoding="utf-8")
    assert repository.count_events() == 1


@pytest.mark.asyncio
async def test_database_audit_repository_filters_in_sql(tmp_path) -> None:
    repository = DatabaseAuditRepository(session_factory=await build_session_factory(tmp_path))
    matching = {
        "envelope": {
            "request_id": "req-match",
            "hook_name": "control_session",
            "occurred_at": datetime(2026, 4, 9, 11, 0, tzinfo=UTC).isoformat(),
        },
        "session": {"user_id": "admin-current"},
        "assistant": {"assistant_id": "assistant-current", "capability_profile": "dangerous"},
        "tool": {"name": "shell"},
        "operation_type": "control_session.reject",
        "result": {
            "success": False,
            "status": "failure",
            "error_code": "origin_mismatch",
        },
        "decision": "deny",
    }
    wrong_error = {
        **matching,
        "envelope": {
            **matching["envelope"],
            "request_id": "req-wrong-error",
            "occurred_at": datetime(2026, 4, 9, 10, 30, tzinfo=UTC).isoformat(),
        },
        "result": {
            "success": False,
            "status": "failure",
            "error_code": "expired",
        },
    }
    wrong_assistant = {
        **matching,
        "envelope": {
            **matching["envelope"],
            "request_id": "req-wrong-assistant",
        },
        "assistant": {"assistant_id": "assistant-other", "capability_profile": "dangerous"},
    }
    old_event = {
        **matching,
        "envelope": {
            **matching["envelope"],
            "request_id": "req-old",
            "occurred_at": datetime(2026, 4, 8, 11, 0, tzinfo=UTC).isoformat(),
        },
    }

    for payload in [matching, wrong_error, wrong_assistant, old_event]:
        await repository.aappend_event(payload)

    page = await repository.alist_page(
        limit=10,
        offset=0,
        filters=AuditLogFilters(
            occurred_from=datetime(2026, 4, 9, 0, 0, tzinfo=UTC),
            occurred_to=datetime(2026, 4, 9, 23, 59, tzinfo=UTC),
            user_id="admin-current",
            assistant_id="assistant-current",
            operation_type="reject",
            result="failure",
            error_code="origin_mismatch",
        ),
    )

    assert page.total == 1
    assert [row["envelope"]["request_id"] for row in page.items] == ["req-match"]
