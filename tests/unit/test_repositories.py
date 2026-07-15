import asyncio
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.approval.models import ApprovalRequest
from src.assistants.models import AssistantRecord
from src.shared.database import Base
from src.shared.repositories import (
    DatabaseApprovalRepository,
    DatabaseAssistantRepository,
    DatabaseAuditRepository,
    JsonAuditRepository,
)
from src.users.models import User, UserAssistantBinding
from src.users.repository import DatabaseUserBindingRepository, DatabaseUserRepository
from src.workspace_files.models import ExternalPushRequest, WorkspaceFile, WorkspaceFileEvent
from src.workspace_files.repository import (
    DatabaseExternalPushRequestRepository,
    DatabaseWorkspaceFileEventRepository,
    DatabaseWorkspaceFileRepository,
)


async def build_session_factory(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'ironagent.db'}",
        future=True,
        poolclass=NullPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.mark.asyncio
async def test_database_assistant_repository_round_trips_records(tmp_path) -> None:
    repository = DatabaseAssistantRepository(session_factory=await build_session_factory(tmp_path))
    record = AssistantRecord(
        assistant_id="demo-main",
        name="Demo Main Assistant",
        capability_profile="onlyread",
        notes="seeded",
    )

    await repository.aupsert_assistant(record)

    loaded = await repository.alist_assistants()

    assert len(loaded) == 1
    assert loaded[0].assistant_id == "demo-main"
    assert loaded[0].iron_agent_config.primary_model == record.iron_agent_config.primary_model


@pytest.mark.asyncio
async def test_database_assistant_repository_supports_get_upsert_and_delete(tmp_path) -> None:
    repository = DatabaseAssistantRepository(session_factory=await build_session_factory(tmp_path))
    record = AssistantRecord(
        assistant_id="assistant-1",
        name="Writer",
        capability_profile="onlywrite",
        notes="first",
    )

    await repository.aupsert_assistant(record)
    fetched = await repository.aget_assistant("assistant-1")
    assert fetched is not None
    assert fetched.name == "Writer"

    updated = record.model_copy(update={"name": "Writer 2", "notes": "updated"})
    await repository.aupsert_assistant(updated)
    reloaded = await repository.aget_assistant("assistant-1")
    assert reloaded is not None
    assert reloaded.name == "Writer 2"
    assert await repository.adelete_assistant("assistant-1") is True
    assert await repository.aget_assistant("assistant-1") is None
    assert await repository.adelete_assistant("assistant-1") is False


@pytest.mark.asyncio
async def test_database_approval_repository_round_trips_requests(tmp_path) -> None:
    repository = DatabaseApprovalRepository(session_factory=await build_session_factory(tmp_path))
    request = ApprovalRequest(
        id="approval-1",
        tool_name="exec",
        assistant_id="demo-main",
        capability_profile="dangerous",
    )

    await repository.acreate_request(request)

    loaded = await repository.alist_requests()

    assert len(loaded) == 1
    assert loaded[0].id == "approval-1"
    assert loaded[0].status == "pending"


@pytest.mark.asyncio
async def test_database_approval_repository_supports_filters_and_save(tmp_path) -> None:
    repository = DatabaseApprovalRepository(session_factory=await build_session_factory(tmp_path))
    user_request = ApprovalRequest(
        id="approval-1",
        tool_name="exec",
        assistant_id="assistant-a",
        approval_scope="user",
    )
    admin_request = ApprovalRequest(
        id="approval-2",
        tool_name="curl",
        assistant_id="assistant-b",
        approval_scope="admin",
    )

    await repository.acreate_request(user_request)
    await repository.acreate_request(admin_request)

    loaded_user = await repository.alist_requests(approval_scope="user")
    loaded_assistant_b = await repository.alist_requests(
        assistant_id="assistant-b",
        status="pending",
    )
    assert [item.id for item in loaded_user] == ["approval-1"]
    assert [item.id for item in loaded_assistant_b] == ["approval-2"]

    user_request.status = "approved"
    user_request.resolution = "allow"
    await repository.asave_request(user_request)
    saved = await repository.aget_request("approval-1")
    assert saved is not None
    assert saved.status == "approved"
    assert saved.resolution == "allow"


@pytest.mark.asyncio
async def test_database_audit_repository_counts_and_orders_latest_events(tmp_path) -> None:
    repository = DatabaseAuditRepository(session_factory=await build_session_factory(tmp_path))
    older = {
        "envelope": {
            "request_id": "req-1",
            "hook_name": "post_execute",
            "occurred_at": datetime(2026, 4, 9, 10, 0, tzinfo=UTC).isoformat(),
        },
        "assistant": {"assistant_id": "demo-main", "capability_profile": "onlyread"},
        "tool": {"name": "exec"},
        "result": {"success": True},
        "decision": "allow",
    }
    newer = {
        "envelope": {
            "request_id": "req-2",
            "hook_name": "post_execute",
            "occurred_at": datetime(2026, 4, 9, 11, 0, tzinfo=UTC).isoformat(),
        },
        "assistant": {"assistant_id": "demo-main", "capability_profile": "onlyread"},
        "tool": {"name": "curl"},
        "result": {"success": False},
        "decision": "deny",
    }

    await repository.aappend_event(older)
    await repository.aappend_event(newer)

    rows = await repository.alist_recent(limit=10)
    second_page = await repository.alist_page(limit=1, offset=1)

    assert await repository.acount_events() == 2
    assert [row["envelope"]["request_id"] for row in rows] == ["req-2", "req-1"]
    assert second_page.total == 2
    assert [row["envelope"]["request_id"] for row in second_page.items] == ["req-1"]


def test_json_audit_repository_pages_ordered_events(tmp_path) -> None:
    repository = JsonAuditRepository(tmp_path / "audit.jsonl")
    for index in range(3):
        repository.append_event(
            {
                "envelope": {
                    "request_id": f"req-{index}",
                    "hook_name": "post_execute",
                    "occurred_at": datetime(2026, 4, 9, 10 + index, 0, tzinfo=UTC).isoformat(),
                },
                "tool": {"name": "exec"},
                "result": {"success": True},
                "decision": "allow",
            }
        )

    page = repository.list_page(limit=2, offset=1)

    assert page.total == 3
    assert [row["envelope"]["request_id"] for row in page.items] == ["req-1", "req-0"]


@pytest.mark.asyncio
async def test_json_audit_repository_async_append_does_not_block_event_loop(
    tmp_path,
    monkeypatch,
) -> None:
    repository = JsonAuditRepository(tmp_path / "audit.jsonl")

    def slow_append_event(payload):
        time.sleep(0.2)

    monkeypatch.setattr(repository, "append_event", slow_append_event)

    started_at = time.perf_counter()
    append_task = asyncio.create_task(
        repository.aappend_event(
            {
                "envelope": {
                    "request_id": "req-slow-json-audit",
                    "hook_name": "post_execute",
                    "occurred_at": datetime(2026, 4, 9, 10, 0, tzinfo=UTC).isoformat(),
                },
                "tool": {"name": "exec"},
                "result": {"success": True},
                "decision": "allow",
            }
        )
    )

    await asyncio.sleep(0.01)
    elapsed = time.perf_counter() - started_at
    await append_task

    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_json_audit_repository_async_append_serializes_concurrent_writes(
    tmp_path,
) -> None:
    repository = JsonAuditRepository(tmp_path / "audit.jsonl")

    async def append_event(index: int) -> None:
        await repository.aappend_event(
            {
                "envelope": {
                    "request_id": f"req-concurrent-{index}",
                    "hook_name": "post_execute",
                    "occurred_at": datetime(2026, 4, 9, 10, 0, tzinfo=UTC).isoformat(),
                },
                "tool": {"name": "exec"},
                "result": {"success": True},
                "decision": "allow",
            }
        )

    results = await asyncio.gather(
        *(append_event(index) for index in range(50)),
        return_exceptions=True,
    )

    assert results == [None] * 50
    assert repository.count_events() == 50
    assert {
        row["envelope"]["request_id"] for row in repository.list_recent(limit=50)
    } == {f"req-concurrent-{index}" for index in range(50)}


@pytest.mark.asyncio
async def test_database_user_repositories_round_trip_users_and_bindings(tmp_path) -> None:
    session_factory = await build_session_factory(tmp_path)
    user_repository = DatabaseUserRepository(session_factory=session_factory)
    binding_repository = DatabaseUserBindingRepository(session_factory=session_factory)

    user = User(
        user_id="emp001",
        display_name="Alice",
        employee_id="E001",
        department="Security",
        role="admin",
    )
    binding = UserAssistantBinding(
        user_id="emp001",
        assistant_id="assistant-1",
        alias="Primary Bot",
        source_system="spdbpanda",
    )

    await user_repository.aupsert_user(user)
    await binding_repository.aupsert_binding(binding)

    loaded_users = await user_repository.alist_users()
    loaded_bindings = await binding_repository.alist_bindings()

    assert [item.user_id for item in loaded_users] == ["emp001"]
    assert loaded_users[0].role == "admin"
    assert [(item.user_id, item.assistant_id) for item in loaded_bindings] == [
        ("emp001", "assistant-1")
    ]


@pytest.mark.asyncio
async def test_database_workspace_file_repositories_round_trip_records(tmp_path) -> None:
    session_factory = await build_session_factory(tmp_path)
    file_repository = DatabaseWorkspaceFileRepository(session_factory=session_factory)
    event_repository = DatabaseWorkspaceFileEventRepository(session_factory=session_factory)
    push_repository = DatabaseExternalPushRequestRepository(session_factory=session_factory)

    file_record = WorkspaceFile(
        file_id="file-1",
        assistant_id="assistant-1",
        bucket="persistent",
        filename="result/report.txt",
        relative_path="persistent/result/report.txt",
        size_bytes=42,
        content_type="text/plain",
        source_type="assistant_result",
        source_ref="workspace_scan",
        storage_backend="local",
        object_key="workspace-files/assistant-1/persistent/result/report.txt",
        object_url="/tmp/workspace-files/assistant-1/persistent/result/report.txt",
        checksum_sha256="abc123",
        sync_status="synced",
        file_role="result",
    )
    event_record = WorkspaceFileEvent(
        event_id="event-1",
        file_id="file-1",
        assistant_id="assistant-1",
        action="sync",
        detail={"filename": "result/report.txt"},
    )
    push_record = ExternalPushRequest(
        push_id="push-1",
        user_id="emp001",
        assistant_id="assistant-1",
        filename="input/source.docx",
        bucket="persistent",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        source_type="spdbpanda",
        source_ref="upload-1",
        status="accepted",
    )

    await file_repository.asave_file(file_record)
    await event_repository.aappend_event(event_record)
    await push_repository.asave_push_request(push_record)

    loaded_file = await file_repository.aget_file("file-1")
    loaded_events = await event_repository.alist_events(file_id="file-1")
    loaded_push = await push_repository.alist_push_requests(assistant_id="assistant-1")

    assert loaded_file is not None
    assert loaded_file.object_key.endswith("result/report.txt")
    assert loaded_file.file_role == "result"
    assert [item.event_id for item in loaded_events] == ["event-1"]
    assert [item.push_id for item in loaded_push] == ["push-1"]


@pytest.mark.asyncio
async def test_database_workspace_file_repository_upserts_active_path(tmp_path) -> None:
    file_repository = DatabaseWorkspaceFileRepository(
        session_factory=await build_session_factory(tmp_path)
    )
    base_time = datetime(2026, 6, 5, tzinfo=UTC)
    first = WorkspaceFile(
        file_id="file-a",
        assistant_id="assistant-1",
        bucket="persistent",
        filename="go-gateway-results/task-1/report.txt",
        relative_path="persistent/go-gateway-results/task-1/report.txt",
        direction="outbound",
        file_role="result",
        source_type="assistant_result",
        source_ref="task-1",
        size_bytes=12,
        created_at=base_time,
        updated_at=base_time,
    )
    second = first.model_copy(
        update={
            "file_id": "file-b",
            "size_bytes": 99,
            "updated_at": datetime(2026, 6, 5, 0, 1, tzinfo=UTC),
        }
    )

    saved_first = await file_repository.asave_file(first)
    saved_second = await file_repository.asave_file(second)
    files = await file_repository.alist_files(assistant_id="assistant-1", bucket="persistent")

    assert saved_first.file_id == "file-a"
    assert saved_second.file_id == "file-a"
    assert len(files) == 1
    assert files[0].file_id == "file-a"
    assert files[0].size_bytes == 99
    assert files[0].source_ref == "task-1"


@pytest.mark.asyncio
async def test_database_workspace_file_repository_populates_extended_storage_columns(
    tmp_path,
) -> None:
    pytest.importorskip("aiosqlite")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'compat.db'}", future=True)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            """
            CREATE TABLE workspace_files (
                file_id VARCHAR(36) PRIMARY KEY,
                assistant_id VARCHAR(255) NOT NULL,
                bucket VARCHAR(20) NOT NULL,
                filename VARCHAR(1024) NOT NULL,
                relative_path VARCHAR(2048) NOT NULL,
                size_bytes BIGINT NOT NULL,
                content_type VARCHAR(255) NOT NULL,
                uploader_user_id VARCHAR(255) NOT NULL,
                source_type VARCHAR(255) NOT NULL,
                source_ref VARCHAR(1024) NOT NULL,
                storage_provider VARCHAR(255) NOT NULL,
                storage_key VARCHAR(2048) NOT NULL,
                storage_path VARCHAR(2048) NOT NULL,
                storage_backend VARCHAR(20) NOT NULL,
                object_key VARCHAR(2048) NOT NULL,
                object_url VARCHAR(4096) NOT NULL,
                file_role VARCHAR(20) NOT NULL,
                source_file_id VARCHAR(36),
                storage_status VARCHAR(32) NOT NULL,
                storage_error TEXT NOT NULL,
                direction VARCHAR(20) NOT NULL,
                checksum VARCHAR(128) NOT NULL,
                sync_status VARCHAR(64) NOT NULL,
                last_error TEXT NOT NULL,
                sync_error TEXT NOT NULL,
                last_synced_at DATETIME,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                deleted BOOLEAN NOT NULL
            )
            """
        )
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                Base.metadata.tables["workspace_file_events"],
                Base.metadata.tables["external_push_requests"],
            ],
        )

    repository = DatabaseWorkspaceFileRepository(
        session_factory=async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    )
    file = WorkspaceFile(
        file_id="file-compat",
        assistant_id="demo-main",
        bucket="persistent",
        filename="reports/summary.xlsx",
        relative_path="persistent/reports/summary.xlsx",
        size_bytes=10,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        source_type="user_upload",
        storage_backend="cos",
        object_key="workspace-files/demo-main/persistent/reports/summary.xlsx",
        object_url="https://example.invalid/workspace-files/demo-main/persistent/reports/summary.xlsx",
    )

    await repository.asave_file(file)

    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT storage_provider, storage_key, storage_path, file_role, "
                    "storage_status, direction, sync_status "
                    "FROM workspace_files WHERE file_id = :file_id"
                ),
                {"file_id": "file-compat"},
            )
        ).one()

    assert row.storage_provider == "cos"
    assert row.storage_key == "workspace-files/demo-main/persistent/reports/summary.xlsx"
    assert row.storage_path == "persistent/reports/summary.xlsx"
    assert row.file_role == "input"
    assert row.storage_status == "pending"
    assert row.direction == "inbound"
    assert row.sync_status == "pending"
