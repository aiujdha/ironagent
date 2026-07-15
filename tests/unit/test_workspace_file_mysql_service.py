"""Unit tests for WorkspaceFileService with the MySQL-style repository path."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.approval.service import ApprovalService
from src.assistants.service import AssistantService
from src.shared.config import get_settings
from src.shared.database import Base
from src.shared.repositories import DatabaseWorkspaceFileRepository
from src.users.service import UserService
from src.workspace_files.models import ExternalPushRequest, WorkspaceFile
from src.workspace_files.scanner import WorkspaceResultScanner
from src.workspace_files.service import WorkspaceFileService


class FakeObjectStore:
    prefix = "workspace-files"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def is_enabled(self) -> bool:
        return True

    def backend_name(self) -> str:
        return "cos"

    def put_bytes(self, *, key: str, content: bytes, content_type: str = ""):
        object_key = f"{self.prefix}/{key.lstrip('/')}"
        self.objects[object_key] = content

        class Stored:
            backend = "cos"

            def __init__(self, object_key: str) -> None:
                self.key = object_key
                self.url = f"https://example-cos.local/{object_key}"
                self.etag = "etag-demo"

        return Stored(object_key)

    def delete(self, *, key: str) -> None:
        self.objects.pop(key, None)

    def get_bytes(self, *, key: str) -> bytes:
        return self.objects[key]


@pytest.fixture()
def approval_mode_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IRON_AGENT_APPROVAL_MODE", "enabled")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspaces"
    root.mkdir()
    return root


@pytest.fixture()
def db_file_svc(tmp_path: Path, workspace_root: Path) -> WorkspaceFileService:
    db_path = tmp_path / "workspace-files.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    async def init_db() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(init_db())

    assistant_svc = AssistantService(store_path=tmp_path / "assistants.json")
    assistant_svc.upsert_assistant(
        assistant_id="test-bot",
        name="Test Bot",
        capability_profile="onlyread",
        provision_runtime=False,
    )
    existing = assistant_svc.get_assistant("test-bot")
    assert existing is not None
    existing.runtime.status = "running"
    existing.runtime.workspace_root = str(workspace_root / "test-bot")
    assistant_svc.repository.upsert_assistant(existing)
    user_svc = UserService(
        store_path=tmp_path / "users.json",
        bindings_path=tmp_path / "bindings.json",
    )
    approval_svc = ApprovalService(store_path=tmp_path / "approvals.json")
    repository = DatabaseWorkspaceFileRepository(session_factory=session_factory)
    svc = WorkspaceFileService(
        push_store=tmp_path / "push.json",
        assistant_service=assistant_svc,
        user_service=user_svc,
        approval_service=approval_svc,
        repository=repository,
    )
    svc._workspace_dir = lambda aid: workspace_root / aid

    try:
        yield svc
    finally:
        asyncio.run(engine.dispose())


@pytest.fixture()
def db_file_svc_with_object_store(
    tmp_path: Path,
    workspace_root: Path,
) -> WorkspaceFileService:
    db_path = tmp_path / "workspace-files-cos.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    async def init_db() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(init_db())

    assistant_svc = AssistantService(store_path=tmp_path / "assistants.json")
    assistant_svc.upsert_assistant(
        assistant_id="test-bot",
        name="Test Bot",
        capability_profile="onlyread",
        provision_runtime=False,
    )
    existing = assistant_svc.get_assistant("test-bot")
    assert existing is not None
    existing.runtime.status = "running"
    existing.runtime.workspace_root = str(workspace_root / "test-bot")
    assistant_svc.repository.upsert_assistant(existing)
    user_svc = UserService(
        store_path=tmp_path / "users.json",
        bindings_path=tmp_path / "bindings.json",
    )
    approval_svc = ApprovalService(store_path=tmp_path / "approvals.json")
    repository = DatabaseWorkspaceFileRepository(session_factory=session_factory)
    svc = WorkspaceFileService(
        push_store=tmp_path / "push.json",
        assistant_service=assistant_svc,
        user_service=user_svc,
        approval_service=approval_svc,
        repository=repository,
        object_store=FakeObjectStore(),
    )
    svc._workspace_dir = lambda aid: workspace_root / aid

    try:
        yield svc
    finally:
        asyncio.run(engine.dispose())


def test_mysql_repository_upload_and_list(db_file_svc: WorkspaceFileService) -> None:
    uploaded = db_file_svc.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="notes.txt",
        content=b"hello mysql",
        content_type="text/plain",
        uploader_user_id="emp001",
        source_type="portal",
        source_ref="upload-001",
    )

    assert isinstance(uploaded, WorkspaceFile)
    files = db_file_svc.list_files("test-bot", bucket="temp")
    assert len(files) == 1
    assert files[0].file_id == uploaded.file_id
    assert files[0].uploader_user_id == "emp001"

    events = db_file_svc.list_events(file_id=uploaded.file_id)
    assert [event.action for event in events].count("upload") == 1

    pushes = db_file_svc.list_push_requests(assistant_id="test-bot")
    assert len(pushes) == 1
    assert pushes[0].status == "accepted"


def test_mysql_repository_list_files_limit_returns_latest(
    db_file_svc: WorkspaceFileService,
) -> None:
    first = db_file_svc.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="first.txt",
        content=b"first",
    )
    second = db_file_svc.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="second.txt",
        content=b"second",
    )
    third = db_file_svc.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="third.txt",
        content=b"third",
    )
    assert isinstance(first, WorkspaceFile)
    assert isinstance(second, WorkspaceFile)
    assert isinstance(third, WorkspaceFile)

    base = datetime(2026, 4, 21, tzinfo=UTC)
    for offset, record in enumerate((first, second, third)):
        updated = record.model_copy(update={"updated_at": base + timedelta(minutes=offset)})
        asyncio.run(db_file_svc.file_repository.asave_file(updated))

    latest = asyncio.run(
        db_file_svc.file_repository.alist_files(assistant_id="test-bot", limit=2)
    )

    assert [item.filename for item in latest] == ["third.txt", "second.txt"]


def test_mysql_repository_persistent_overwrite_roundtrip(
    db_file_svc: WorkspaceFileService,
) -> None:
    original = db_file_svc.upload_file(
        assistant_id="test-bot",
        bucket="persistent",
        filename="report.pdf",
        content=b"v1",
        content_type="application/pdf",
    )
    assert isinstance(original, WorkspaceFile)

    updated = db_file_svc.upload_file(
        assistant_id="test-bot",
        bucket="persistent",
        filename="report.pdf",
        content=b"v2",
        content_type="application/pdf",
        overwrite=True,
        uploader_user_id="emp001",
        source_type="oa",
        source_ref="oa-001",
    )
    assert isinstance(updated, WorkspaceFile)
    assert updated.file_id == original.file_id
    assert updated.size_bytes == 2
    assert updated.source_ref == "oa-001"

    downloaded = db_file_svc.download_file(original.file_id)
    assert downloaded is not None
    assert downloaded.record.file_id == original.file_id
    assert downloaded.content == b"v2"
    assert downloaded.download_source == "workspace"

    matched_push = next(
        item
        for item in db_file_svc.list_push_requests(assistant_id="test-bot")
        if item.filename == "report.pdf" and item.status == "accepted"
    )
    assert matched_push.status == "accepted"
    assert matched_push.approval_id is None
    assert matched_push.staged_path is None


def test_mysql_repository_persistent_delete_roundtrip(
    db_file_svc: WorkspaceFileService,
    approval_mode_enabled: None,
) -> None:
    uploaded = db_file_svc.upload_file(
        assistant_id="test-bot",
        bucket="persistent",
        filename="delete-me.txt",
        content=b"delete me",
    )
    assert isinstance(uploaded, WorkspaceFile)

    pending = db_file_svc.delete_file(uploaded.file_id, user_id="emp001")
    assert isinstance(pending, ExternalPushRequest)
    assert pending.approval_id is not None

    resolved = db_file_svc.approval_svc.resolve_request(pending.approval_id, "allow")
    assert resolved is not None

    deleted = db_file_svc.apply_approval_resolution(pending.approval_id)
    assert deleted is not None
    assert deleted.deleted is True
    assert db_file_svc.download_file(uploaded.file_id) is None

    history = db_file_svc.list_files("test-bot", bucket="persistent", include_deleted=True)
    assert len(history) == 1
    assert history[0].deleted is True


def test_mysql_repository_upload_stores_object_metadata(
    db_file_svc_with_object_store: WorkspaceFileService,
) -> None:
    uploaded = db_file_svc_with_object_store.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="proposal.docx",
        content=b"proposal-content",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        uploader_user_id="emp009",
    )

    assert isinstance(uploaded, WorkspaceFile)
    assert uploaded.storage_backend == "cos"
    assert uploaded.object_key
    assert uploaded.object_url.startswith("https://example-cos.local/")
    assert uploaded.checksum
    assert uploaded.sync_status == "synced_to_workspace"

    disk_path = (
        db_file_svc_with_object_store._workspace_dir("test-bot")
        / "temp"
        / "proposal.docx"
    )
    assert disk_path.exists()
    assert disk_path.read_bytes() == b"proposal-content"


def test_download_returns_none_when_workspace_file_is_missing(
    db_file_svc_with_object_store: WorkspaceFileService,
) -> None:
    uploaded = db_file_svc_with_object_store.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="cos-only.txt",
        content=b"from-cos",
        content_type="text/plain",
    )

    assert isinstance(uploaded, WorkspaceFile)
    disk_path = db_file_svc_with_object_store._workspace_dir("test-bot") / "temp" / "cos-only.txt"
    disk_path.unlink()

    downloaded = db_file_svc_with_object_store.download_file(uploaded.file_id)
    assert downloaded is None

    events = db_file_svc_with_object_store.list_events(file_id=uploaded.file_id)
    assert all(event.action != "download" for event in events)


def test_scan_workspace_results_creates_outbound_file_record(
    db_file_svc_with_object_store: WorkspaceFileService,
) -> None:
    inbound = db_file_svc_with_object_store.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="source-input.txt",
        content=b"source input",
        content_type="text/plain",
    )
    assert isinstance(inbound, WorkspaceFile)

    output_dir = db_file_svc_with_object_store._workspace_dir("test-bot") / "temp"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "assistant-result.txt"
    output_path.write_bytes(b"assistant result")

    records = db_file_svc_with_object_store.scan_workspace_results("test-bot")
    assert len(records) == 1

    record = records[0]
    assert record.direction == "outbound"
    assert record.filename == "assistant-result.txt"
    assert record.source_file_id == inbound.file_id
    assert record.storage_backend == "cos"
    assert record.object_key
    assert record.object_url.startswith("https://example-cos.local/")

    all_files = db_file_svc_with_object_store.list_files("test-bot", bucket="temp")
    assert len(all_files) == 2
    assert {item.file_id for item in all_files} == {inbound.file_id, record.file_id}

    events = db_file_svc_with_object_store.list_events(file_id=record.file_id)
    assert len(events) == 1
    assert events[0].action == "scan_detected"
    assert events[0].detail["kind"] == "created"
    assert events[0].detail["source_file_id"] == inbound.file_id


def test_scan_workspace_results_prefers_same_bucket_inbound_source(
    db_file_svc_with_object_store: WorkspaceFileService,
) -> None:
    temp_inbound = db_file_svc_with_object_store.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="temp-input.txt",
        content=b"temp input",
        content_type="text/plain",
    )
    persistent_inbound = db_file_svc_with_object_store.upload_file(
        assistant_id="test-bot",
        bucket="persistent",
        filename="persistent-input.txt",
        content=b"persistent input",
        content_type="text/plain",
    )

    assert isinstance(temp_inbound, WorkspaceFile)
    assert isinstance(persistent_inbound, WorkspaceFile)

    output_dir = db_file_svc_with_object_store._workspace_dir("test-bot") / "persistent"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "persistent-result.txt").write_bytes(b"persistent result")

    records = db_file_svc_with_object_store.scan_workspace_results("test-bot")
    assert len(records) == 1
    assert records[0].source_file_id == persistent_inbound.file_id


def test_mysql_repository_list_files_supports_direction_filter(
    db_file_svc_with_object_store: WorkspaceFileService,
) -> None:
    inbound = db_file_svc_with_object_store.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="input.txt",
        content=b"inbound",
        content_type="text/plain",
    )
    assert isinstance(inbound, WorkspaceFile)

    output_dir = db_file_svc_with_object_store._workspace_dir("test-bot") / "persistent"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "result.txt").write_bytes(b"outbound")
    db_file_svc_with_object_store.scan_workspace_results("test-bot")

    inbound_only = db_file_svc_with_object_store.list_files("test-bot", direction="inbound")
    outbound_only = db_file_svc_with_object_store.list_files("test-bot", direction="outbound")

    assert len(inbound_only) == 1
    assert inbound_only[0].filename == "input.txt"
    assert len(outbound_only) == 1
    assert outbound_only[0].filename == "result.txt"


def test_workspace_result_scanner_scans_running_assistants(
    db_file_svc_with_object_store: WorkspaceFileService,
) -> None:
    output_dir = db_file_svc_with_object_store._workspace_dir("test-bot") / "persistent"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.pdf").write_bytes(b"summary-pdf")

    scanner = WorkspaceResultScanner(
        assistant_service=db_file_svc_with_object_store.assistant_svc,
        file_service=db_file_svc_with_object_store,
        scan_interval_ms=5000,
    )
    summary = asyncio.run(scanner.scan_once())
    assert summary.scanned_assistants == 1
    assert summary.synced_files == 1

    files = db_file_svc_with_object_store.list_files("test-bot", bucket="persistent")
    assert len(files) == 1
    assert files[0].filename == "summary.pdf"
    assert files[0].direction == "outbound"
