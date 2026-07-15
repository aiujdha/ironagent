"""Unit tests for workspace file service — upload, download, delete, security."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.approval.service import ApprovalService
from src.assistants.service import AssistantService
from src.runtime.models import AssistantWorkspaceConfig
from src.shared.async_utils import run_sync
from src.shared.config import get_settings
from src.shared.object_storage import WorkspaceObjectStorage
from src.users.service import UserService
from src.workspace_files.models import ExternalPushRequest, WorkspaceFile
from src.workspace_files.service import FileSecurityError, WorkspaceFileService


@pytest.fixture(autouse=True)
def approval_mode_enabled(monkeypatch):
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
def file_svc(tmp_path: Path, workspace_root: Path) -> WorkspaceFileService:
    assistant_svc = AssistantService(store_path=tmp_path / "assistants.json")
    # Seed a test assistant
    assistant_svc.upsert_assistant(
        assistant_id="test-bot",
        name="Test Bot",
        capability_profile="onlyread",
        provision_runtime=False,
    )
    user_svc = UserService(
        store_path=tmp_path / "users.json",
        bindings_path=tmp_path / "bindings.json",
    )
    approval_svc = ApprovalService(store_path=tmp_path / "approvals.json")
    svc = WorkspaceFileService(
        files_store=tmp_path / "files.json",
        events_store=tmp_path / "events.json",
        push_store=tmp_path / "push.json",
        assistant_service=assistant_svc,
        user_service=user_svc,
        approval_service=approval_svc,
    )
    # Monkey-patch workspace dir to use tmp_path
    svc._workspace_dir = lambda aid: workspace_root / aid
    return svc


class FakeObjectStore:
    prefix = "workspace-files"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.upload_calls: list[str] = []
        self.delete_calls: list[str] = []

    def put_bytes(self, *, key: str, content: bytes, content_type: str = ""):
        object_key = f"{self.prefix}/{key.lstrip('/')}"
        self.objects[object_key] = content
        self.upload_calls.append(object_key)

        class Stored:
            backend = "cos"

            def __init__(self, object_key: str) -> None:
                self.key = object_key
                self.url = f"https://example-cos.local/{object_key}"

        return Stored(object_key)

    def get_bytes(self, *, key: str) -> bytes | None:
        return self.objects.get(key)

    def delete(self, *, key: str) -> None:
        self.delete_calls.append(key)
        self.objects.pop(key, None)


@pytest.fixture()
def object_store() -> FakeObjectStore:
    return FakeObjectStore()


@pytest.fixture()
def file_svc_with_object_store(
    tmp_path: Path,
    workspace_root: Path,
    object_store: FakeObjectStore,
) -> WorkspaceFileService:
    assistant_svc = AssistantService(store_path=tmp_path / "assistants.json")
    assistant_svc.upsert_assistant(
        assistant_id="test-bot",
        name="Test Bot",
        capability_profile="onlyread",
        provision_runtime=False,
    )
    user_svc = UserService(
        store_path=tmp_path / "users.json",
        bindings_path=tmp_path / "bindings.json",
    )
    approval_svc = ApprovalService(store_path=tmp_path / "approvals.json")
    svc = WorkspaceFileService(
        files_store=tmp_path / "files.json",
        events_store=tmp_path / "events.json",
        push_store=tmp_path / "push.json",
        assistant_service=assistant_svc,
        user_service=user_svc,
        approval_service=approval_svc,
        object_storage=object_store,
    )
    svc._workspace_dir = lambda aid: workspace_root / aid
    return svc


# ── Security tests ───────────────────────────────────────────────────────────


def test_reject_forbidden_filenames() -> None:
    for name in ["AGENTS.md", "USER.md", ".env"]:
        with pytest.raises(FileSecurityError):
            WorkspaceFileService.validate_filename(name)


def test_reject_forbidden_filenames_case_insensitive() -> None:
    for name in ["agents.md", "user.md", ".ENV", "Agents.MD", "User.Md"]:
        with pytest.raises(FileSecurityError):
            WorkspaceFileService.validate_filename(name)


def test_reject_path_traversal() -> None:
    for name in ["../etc/passwd", "foo/../../bar", "..\\windows\\system32"]:
        with pytest.raises(FileSecurityError):
            WorkspaceFileService.validate_filename(name)


def test_reject_absolute_paths() -> None:
    for name in ["/etc/passwd", "C:\\Windows\\system32"]:
        with pytest.raises(FileSecurityError):
            WorkspaceFileService.validate_filename(name)


def test_accept_valid_filenames() -> None:
    for name in ["report.pdf", "subdir/notes.txt", "my-file_v2.docx"]:
        WorkspaceFileService.validate_filename(name)  # should not raise


# ── Upload tests ─────────────────────────────────────────────────────────────


def test_upload_temp_file(file_svc: WorkspaceFileService) -> None:
    result = file_svc.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="notes.txt",
        content=b"hello world",
        source_type="test",
    )
    assert isinstance(result, WorkspaceFile)
    assert result.filename == "notes.txt"
    assert result.bucket == "temp"
    assert result.size_bytes == 11


def test_upload_persistent_file(file_svc: WorkspaceFileService) -> None:
    result = file_svc.upload_file(
        assistant_id="test-bot",
        bucket="persistent",
        filename="report.pdf",
        content=b"pdf-content",
    )
    assert isinstance(result, WorkspaceFile)
    assert result.bucket == "persistent"


def test_upload_persistent_file_syncs_to_object_storage(
    file_svc_with_object_store: WorkspaceFileService,
    object_store: FakeObjectStore,
) -> None:
    result = file_svc_with_object_store.upload_file(
        assistant_id="test-bot",
        bucket="persistent",
        filename="report.pdf",
        content=b"pdf-content",
    )
    assert isinstance(result, WorkspaceFile)
    assert result.storage_backend == "cos"
    assert result.object_key == "workspace-files/test-bot/persistent/report.pdf"
    assert object_store.objects[result.object_key] == b"pdf-content"


def test_upload_uses_assistant_name_and_id_in_object_storage_key_when_they_differ(
    tmp_path: Path,
    workspace_root: Path,
    object_store: FakeObjectStore,
) -> None:
    assistant_svc = AssistantService(store_path=tmp_path / "assistants.json")
    assistant_svc.upsert_assistant(
        assistant_id="assistant-42",
        name="Finance Analyst",
        capability_profile="onlyread",
        provision_runtime=False,
    )
    svc = WorkspaceFileService(
        files_store=tmp_path / "files.json",
        events_store=tmp_path / "events.json",
        push_store=tmp_path / "push.json",
        assistant_service=assistant_svc,
        user_service=UserService(
            store_path=tmp_path / "users.json",
            bindings_path=tmp_path / "bindings.json",
        ),
        approval_service=ApprovalService(store_path=tmp_path / "approvals.json"),
        object_storage=object_store,
    )
    svc._workspace_dir = lambda aid: workspace_root / aid

    result = svc.upload_file(
        assistant_id="assistant-42",
        bucket="persistent",
        filename="report.pdf",
        content=b"pdf-content",
    )

    assert isinstance(result, WorkspaceFile)
    assert result.object_key == "workspace-files/finance-analyst-assistant-42/persistent/report.pdf"


def test_list_files_does_not_reupload_uploaded_persistent_file(
    file_svc_with_object_store: WorkspaceFileService,
    object_store: FakeObjectStore,
) -> None:
    uploaded = file_svc_with_object_store.upload_file(
        assistant_id="test-bot",
        bucket="persistent",
        filename="report.pdf",
        content=b"pdf-content",
        source_type="webui-chat",
    )

    first_pass = file_svc_with_object_store.list_files("test-bot", bucket="persistent")
    second_pass = file_svc_with_object_store.list_files("test-bot", bucket="persistent")

    assert isinstance(uploaded, WorkspaceFile)
    assert object_store.upload_calls == ["workspace-files/test-bot/persistent/report.pdf"]
    assert first_pass[0].file_role == "input"
    assert first_pass[0].direction == "inbound"
    assert second_pass[0].file_role == "input"
    assert second_pass[0].direction == "inbound"


def test_upload_duplicate_without_overwrite_replaces_existing_file(
    file_svc: WorkspaceFileService,
) -> None:
    original = file_svc.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="dup.txt",
        content=b"first",
    )
    assert isinstance(original, WorkspaceFile)

    updated = file_svc.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="dup.txt",
        content=b"second",
    )

    assert isinstance(updated, WorkspaceFile)
    assert updated.file_id == original.file_id
    assert updated.size_bytes == len(b"second")
    assert file_svc.download_file(original.file_id).content == b"second"

    events = file_svc.list_events(file_id=original.file_id)
    assert any(event.action == "overwrite" for event in events)


def test_upload_file_writes_into_resolved_workspace_slug(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "managed-workspaces"
    monkeypatch.setenv("IRON_AGENT_ASSISTANTS_WORKSPACE_ROOT", str(workspace_root))
    get_settings.cache_clear()
    try:
        assistant_svc = AssistantService(store_path=tmp_path / "assistants.json")
        assistant_svc.upsert_assistant(
            assistant_id="assistant-42",
            name="Finance Analyst",
            capability_profile="onlyread",
            workspace_config=AssistantWorkspaceConfig(workspace_slug="finance-space"),
            provision_runtime=False,
        )
        svc = WorkspaceFileService(
            files_store=tmp_path / "files.json",
            events_store=tmp_path / "events.json",
            push_store=tmp_path / "push.json",
            assistant_service=assistant_svc,
            user_service=UserService(
                store_path=tmp_path / "users.json",
                bindings_path=tmp_path / "bindings.json",
            ),
            approval_service=ApprovalService(store_path=tmp_path / "approvals.json"),
        )

        result = svc.upload_file(
            assistant_id="assistant-42",
            bucket="temp",
            filename="notes.txt",
            content=b"hello",
        )

        assert isinstance(result, WorkspaceFile)
        assert (workspace_root / "finance-space" / "temp" / "notes.txt").read_bytes() == b"hello"
        assert not (workspace_root / "assistant-42" / "temp" / "notes.txt").exists()
    finally:
        get_settings.cache_clear()


def test_overwrite_temp_file_succeeds(file_svc: WorkspaceFileService) -> None:
    file_svc.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="dup.txt",
        content=b"first",
    )
    result = file_svc.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="dup.txt",
        content=b"second-longer",
        overwrite=True,
    )
    assert isinstance(result, WorkspaceFile)
    assert result.size_bytes == len(b"second-longer")


def test_overwrite_persistent_file_succeeds_without_approval(
    file_svc: WorkspaceFileService,
) -> None:
    file_svc.upload_file(
        assistant_id="test-bot",
        bucket="persistent",
        filename="report.pdf",
        content=b"v1",
    )
    result = file_svc.upload_file(
        assistant_id="test-bot",
        bucket="persistent",
        filename="report.pdf",
        content=b"v2",
        overwrite=True,
    )
    assert isinstance(result, WorkspaceFile)
    assert result.size_bytes == len(b"v2")
    assert file_svc.download_file(result.file_id).content == b"v2"


def test_overwrite_persistent_file_succeeds_without_overwrite_flag(
    file_svc: WorkspaceFileService,
) -> None:
    file_svc.upload_file(
        assistant_id="test-bot",
        bucket="persistent",
        filename="report.pdf",
        content=b"v1",
    )
    result = file_svc.upload_file(
        assistant_id="test-bot",
        bucket="persistent",
        filename="report.pdf",
        content=b"v2",
    )
    assert isinstance(result, WorkspaceFile)
    assert result.size_bytes == len(b"v2")
    assert file_svc.download_file(result.file_id).content == b"v2"


def test_persistent_overwrite_executes_without_approval(file_svc: WorkspaceFileService) -> None:
    original = file_svc.upload_file(
        assistant_id="test-bot",
        bucket="persistent",
        filename="report.pdf",
        content=b"v1",
        content_type="application/pdf",
    )
    assert isinstance(original, WorkspaceFile)
    updated = file_svc.upload_file(
        assistant_id="test-bot",
        bucket="persistent",
        filename="report.pdf",
        content=b"v2-new",
        content_type="application/pdf",
        overwrite=True,
        uploader_user_id="emp001",
        source_type="oa",
        source_ref="doc-001",
    )
    assert isinstance(updated, WorkspaceFile)
    assert updated.file_id == original.file_id
    assert updated.size_bytes == len(b"v2-new")
    assert updated.uploader_user_id == "emp001"
    assert updated.source_type == "oa"
    assert updated.source_ref == "doc-001"

    downloaded = file_svc.download_file(original.file_id)
    assert downloaded is not None
    assert downloaded.record.file_id == original.file_id
    assert downloaded.content == b"v2-new"
    assert downloaded.download_source == "workspace"

    pushes = file_svc.list_push_requests(assistant_id="test-bot")
    matched_push = next(p for p in pushes if p.filename == "report.pdf" and p.status == "accepted")
    assert matched_push.status == "accepted"
    assert matched_push.approval_id is None
    assert matched_push.staged_path is None

    events = file_svc.list_events(file_id=original.file_id)
    assert any(event.action == "overwrite" for event in events)


def test_persistent_overwrite_no_longer_creates_approval(
    file_svc: WorkspaceFileService,
) -> None:
    original = file_svc.upload_file(
        assistant_id="test-bot",
        bucket="persistent",
        filename="report.pdf",
        content=b"v1",
    )
    assert isinstance(original, WorkspaceFile)
    updated = file_svc.upload_file(
        assistant_id="test-bot",
        bucket="persistent",
        filename="report.pdf",
        content=b"v2-new",
        overwrite=True,
    )
    assert isinstance(updated, WorkspaceFile)

    downloaded = file_svc.download_file(original.file_id)
    assert downloaded is not None
    assert downloaded.record.file_id == original.file_id
    assert downloaded.content == b"v2-new"
    assert downloaded.download_source == "workspace"

    pushes = file_svc.list_push_requests(assistant_id="test-bot")
    assert not any(push.approval_id for push in pushes if push.filename == "report.pdf")


def test_upload_forbidden_filename_raises(file_svc: WorkspaceFileService) -> None:
    with pytest.raises(FileSecurityError):
        file_svc.upload_file(
            assistant_id="test-bot",
            bucket="temp",
            filename="AGENTS.md",
            content=b"hack",
        )


@pytest.mark.parametrize("bucket", ["skills", "plugins"])
def test_upload_to_capability_buckets_raises(
    file_svc: WorkspaceFileService,
    bucket: str,
) -> None:
    with pytest.raises(FileSecurityError, match="read-only"):
        file_svc.upload_file(
            assistant_id="test-bot",
            bucket=bucket,  # type: ignore[arg-type]
            filename="package.txt",
            content=b"capability overwrite",
        )


def test_upload_nonexistent_assistant_raises(file_svc: WorkspaceFileService) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        file_svc.upload_file(
            assistant_id="nonexistent",
            bucket="temp",
            filename="test.txt",
            content=b"data",
        )


# ── List / Download / Delete ────────────────────────────────────────────────


def test_list_files(file_svc: WorkspaceFileService) -> None:
    file_svc.upload_file(assistant_id="test-bot", bucket="temp", filename="a.txt", content=b"a")
    file_svc.upload_file(
        assistant_id="test-bot", bucket="persistent", filename="b.txt", content=b"b"
    )
    all_files = file_svc.list_files("test-bot")
    assert len(all_files) == 2
    temp_only = file_svc.list_files("test-bot", bucket="temp")
    assert len(temp_only) == 1
    assert temp_only[0].filename == "a.txt"


def test_list_files_deduplicates_active_records_by_workspace_path(
    file_svc: WorkspaceFileService,
) -> None:
    base_time = datetime(2026, 1, 1, tzinfo=UTC)
    older = WorkspaceFile(
        file_id="older-record",
        assistant_id="test-bot",
        bucket="skills",
        filename="web-crawl/manifest.json",
        relative_path="skills/web-crawl/manifest.json",
        source_type="capability_package",
        sync_status="pending",
        created_at=base_time,
        updated_at=base_time,
    )
    newer = older.model_copy(
        update={
            "file_id": "newer-record",
            "sync_status": "synced",
            "updated_at": base_time + timedelta(seconds=1),
        }
    )

    run_sync(file_svc.file_repository.asave_file(older))
    run_sync(file_svc.file_repository.asave_file(newer))

    files = file_svc.list_files("test-bot", bucket="skills")

    assert [item.file_id for item in files] == ["newer-record"]


def test_list_files_imports_legacy_object_storage_records(
    file_svc: WorkspaceFileService,
) -> None:
    imported = WorkspaceFile(
        assistant_id="test-bot",
        bucket="temp",
        filename="legacy.txt",
        relative_path="temp/legacy.txt",
        direction="inbound",
        file_role="input",
        size_bytes=12,
        content_type="text/plain",
        source_type="manual_import",
        source_ref="legacy_object_storage",
        storage_provider="cos",
        storage_key="test-bot/temp/legacy.txt",
        storage_status="synced",
        storage_backend="cos",
        object_key="test-bot/temp/legacy.txt",
        object_url="https://cos.example/test-bot/temp/legacy.txt",
    )

    file_svc.legacy_object_storage_import_enabled = True
    file_svc._discover_legacy_object_storage_records = lambda assistant_id: [imported]

    first_pass = file_svc.list_files("test-bot", bucket="temp")
    second_pass = file_svc.list_files("test-bot", bucket="temp")

    assert len(first_pass) == 1
    assert first_pass[0].filename == "legacy.txt"
    assert first_pass[0].storage_backend == "cos"
    assert len(second_pass) == 1
    assert second_pass[0].filename == "legacy.txt"


def test_isolated_service_does_not_import_legacy_object_storage_records_by_default(
    file_svc: WorkspaceFileService,
) -> None:
    imported = WorkspaceFile(
        assistant_id="test-bot",
        bucket="temp",
        filename="legacy.txt",
        relative_path="temp/legacy.txt",
        direction="inbound",
        file_role="input",
        size_bytes=12,
        content_type="text/plain",
        source_type="manual_import",
        source_ref="legacy_object_storage",
        storage_provider="cos",
        storage_key="test-bot/temp/legacy.txt",
        storage_status="synced",
        storage_backend="cos",
        object_key="test-bot/temp/legacy.txt",
        object_url="https://cos.example/test-bot/temp/legacy.txt",
    )

    file_svc._discover_legacy_object_storage_records = lambda assistant_id: [imported]

    listed = file_svc.list_files("test-bot", bucket="temp")

    assert listed == []


def test_service_without_explicit_stores_uses_configured_workspace_object_storage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IRON_AGENT_WORKSPACE_OBJECT_STORAGE_BACKEND", "cos")
    monkeypatch.setenv("IRON_AGENT_WORKSPACE_OBJECT_STORAGE_PREFIX", "")
    monkeypatch.setenv("IRON_AGENT_COS_SECRET_ID", "demo-id")
    monkeypatch.setenv("IRON_AGENT_COS_SECRET_KEY", "demo-key")
    monkeypatch.setenv("IRON_AGENT_COS_BUCKET", "demo-bucket")
    monkeypatch.setenv("IRON_AGENT_COS_REGION", "ap-shanghai")
    monkeypatch.setenv("IRON_AGENT_ASSISTANTS_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    get_settings.cache_clear()

    assistant_svc = AssistantService(store_path=tmp_path / "assistants.json")
    assistant_svc.upsert_assistant(
        assistant_id="test-bot",
        name="Test Bot",
        capability_profile="onlyread",
        provision_runtime=False,
    )
    user_svc = UserService(
        store_path=tmp_path / "users.json",
        bindings_path=tmp_path / "bindings.json",
    )
    approval_svc = ApprovalService(store_path=tmp_path / "approvals.json")

    svc = WorkspaceFileService(
        assistant_service=assistant_svc,
        user_service=user_svc,
        approval_service=approval_svc,
    )
    try:
        assert isinstance(svc.object_storage, WorkspaceObjectStorage)
        assert svc.object_storage.backend == "cos"
    finally:
        get_settings.cache_clear()


def test_list_files_supports_direction_filter(file_svc: WorkspaceFileService) -> None:
    inbound = file_svc.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="input.txt",
        content=b"inbound",
    )
    assert isinstance(inbound, WorkspaceFile)

    result_dir = file_svc._workspace_dir("test-bot") / "temp"
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "result.txt").write_bytes(b"outbound")
    file_svc.scan_workspace_results("test-bot")

    inbound_only = file_svc.list_files("test-bot", direction="inbound")
    outbound_only = file_svc.list_files("test-bot", direction="outbound")

    assert len(inbound_only) == 1
    assert inbound_only[0].filename == "input.txt"
    assert len(outbound_only) == 1
    assert outbound_only[0].filename == "result.txt"


def test_download_file(file_svc: WorkspaceFileService) -> None:
    uploaded = file_svc.upload_file(
        assistant_id="test-bot", bucket="temp", filename="dl.txt", content=b"download me"
    )
    assert isinstance(uploaded, WorkspaceFile)
    result = file_svc.download_file(uploaded.file_id)
    assert result is not None
    assert result.record.file_id == uploaded.file_id
    assert result.content == b"download me"
    assert result.download_source == "workspace"


def test_delete_temp_file(file_svc: WorkspaceFileService) -> None:
    uploaded = file_svc.upload_file(
        assistant_id="test-bot", bucket="temp", filename="del.txt", content=b"delete me"
    )
    assert isinstance(uploaded, WorkspaceFile)
    result = file_svc.delete_file(uploaded.file_id)
    assert isinstance(result, WorkspaceFile)
    assert result.deleted
    assert file_svc.list_files("test-bot") == []


def test_delete_capability_bucket_file_raises(
    file_svc: WorkspaceFileService,
    workspace_root: Path,
) -> None:
    skill_path = workspace_root / "test-bot" / "skills" / "web-crawl" / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text("crawl skill", encoding="utf-8")
    file_svc.scan_workspace_results(assistant_id="test-bot", bucket="skills")
    record = file_svc.list_files("test-bot", bucket="skills")[0]

    with pytest.raises(FileSecurityError, match="read-only"):
        file_svc.delete_file(record.file_id)

    assert skill_path.exists()
    latest = file_svc.get_file(record.file_id)
    assert latest is not None
    assert latest.deleted is False


def test_delete_assistant_files_deletes_all_buckets_without_approval(
    file_svc: WorkspaceFileService,
) -> None:
    temp_file = file_svc.upload_file(
        assistant_id="test-bot", bucket="temp", filename="scratch.txt", content=b"scratch"
    )
    persistent_file = file_svc.upload_file(
        assistant_id="test-bot",
        bucket="persistent",
        filename="persist.txt",
        content=b"persist",
    )
    assert isinstance(temp_file, WorkspaceFile)
    assert isinstance(persistent_file, WorkspaceFile)

    deleted = file_svc.delete_assistant_files("test-bot", reason="assistant_deleted")

    assert deleted == 2
    assert file_svc.list_files("test-bot") == []
    assert file_svc.download_file(temp_file.file_id) is None
    assert file_svc.download_file(persistent_file.file_id) is None


def test_delete_persistent_requires_approval(file_svc: WorkspaceFileService) -> None:
    uploaded = file_svc.upload_file(
        assistant_id="test-bot", bucket="persistent", filename="keep.pdf", content=b"important"
    )
    assert isinstance(uploaded, WorkspaceFile)
    result = file_svc.delete_file(uploaded.file_id)
    assert isinstance(result, ExternalPushRequest)
    assert result.status == "pending_approval"
    assert result.approval_id is not None
    approval = file_svc.approval_svc.get_request(result.approval_id)
    assert approval is not None
    assert approval.capability_profile == "onlyread"
    # File should still exist (not deleted yet)
    assert len(file_svc.list_files("test-bot")) == 1


def test_delete_persistent_allows_when_approval_mode_disabled(
    file_svc: WorkspaceFileService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IRON_AGENT_APPROVAL_MODE", "disabled")
    get_settings.cache_clear()
    try:
        uploaded = file_svc.upload_file(
            assistant_id="test-bot",
            bucket="persistent",
            filename="no-approval.pdf",
            content=b"important",
        )
        assert isinstance(uploaded, WorkspaceFile)

        result = file_svc.delete_file(uploaded.file_id, user_id="emp001")

        assert isinstance(result, WorkspaceFile)
        assert result.deleted is True
        assert file_svc.list_files("test-bot") == []
        assert file_svc.download_file(uploaded.file_id) is None
        assert file_svc.approval_svc.list_requests() == []
    finally:
        get_settings.cache_clear()


def test_allow_persistent_delete_requires_explicit_apply_step(
    file_svc: WorkspaceFileService,
) -> None:
    uploaded = file_svc.upload_file(
        assistant_id="test-bot",
        bucket="persistent",
        filename="pending-apply.pdf",
        content=b"important",
    )
    assert isinstance(uploaded, WorkspaceFile)
    pending = file_svc.delete_file(uploaded.file_id, user_id="emp001")
    assert isinstance(pending, ExternalPushRequest)
    assert pending.approval_id is not None

    resolved = file_svc.approval_svc.resolve_request(pending.approval_id, "allow")
    assert resolved is not None

    remaining = file_svc.list_files("test-bot")
    assert len(remaining) == 1
    assert remaining[0].file_id == uploaded.file_id


def test_allow_persistent_delete_executes_delete(file_svc: WorkspaceFileService) -> None:
    uploaded = file_svc.upload_file(
        assistant_id="test-bot", bucket="persistent", filename="approved.pdf", content=b"important"
    )
    assert isinstance(uploaded, WorkspaceFile)
    pending = file_svc.delete_file(uploaded.file_id, user_id="emp001")
    assert isinstance(pending, ExternalPushRequest)
    assert pending.approval_id is not None

    resolved = file_svc.approval_svc.resolve_request(pending.approval_id, "allow")
    assert resolved is not None

    deleted = file_svc.apply_approval_resolution(pending.approval_id)
    assert deleted is not None
    assert deleted.deleted
    assert file_svc.list_files("test-bot") == []
    assert file_svc.download_file(uploaded.file_id) is None

    pushes = file_svc.list_push_requests(assistant_id="test-bot")
    matched_push = next(p for p in pushes if p.approval_id == pending.approval_id)
    assert matched_push.status == "accepted"

    events = file_svc.list_events(file_id=uploaded.file_id)
    assert any(
        event.action == "delete" and event.detail.get("approval_id") == pending.approval_id
        for event in events
    )


def test_deny_persistent_delete_keeps_file(file_svc: WorkspaceFileService) -> None:
    uploaded = file_svc.upload_file(
        assistant_id="test-bot", bucket="persistent", filename="denied.pdf", content=b"important"
    )
    assert isinstance(uploaded, WorkspaceFile)
    pending = file_svc.delete_file(uploaded.file_id, user_id="emp001")
    assert isinstance(pending, ExternalPushRequest)
    assert pending.approval_id is not None

    resolved = file_svc.approval_svc.resolve_request(pending.approval_id, "deny")
    assert resolved is not None

    deleted = file_svc.apply_approval_resolution(pending.approval_id)
    assert deleted is None

    remaining = file_svc.list_files("test-bot")
    assert len(remaining) == 1
    assert remaining[0].filename == "denied.pdf"

    pushes = file_svc.list_push_requests(assistant_id="test-bot")
    matched_push = next(p for p in pushes if p.approval_id == pending.approval_id)
    assert matched_push.status == "rejected"


# ── User-based routing ──────────────────────────────────────────────────────


def test_upload_by_user_id(file_svc: WorkspaceFileService) -> None:
    file_svc.user_svc.create_user("emp001")
    file_svc.user_svc.bind_assistant("emp001", "test-bot")
    result = file_svc.upload_file(
        user_id="emp001",
        bucket="temp",
        filename="routed.txt",
        content=b"routed by user",
    )
    assert isinstance(result, WorkspaceFile)
    assert result.assistant_id == "test-bot"


def test_upload_by_user_id_no_binding_raises(file_svc: WorkspaceFileService) -> None:
    file_svc.user_svc.create_user("emp002")
    with pytest.raises(ValueError, match="no assistant bound"):
        file_svc.upload_file(
            user_id="emp002",
            bucket="temp",
            filename="fail.txt",
            content=b"no binding",
        )


# ── Events audit trail ──────────────────────────────────────────────────────


def test_events_recorded_on_upload(file_svc: WorkspaceFileService) -> None:
    file_svc.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="audit.txt",
        content=b"track me",
        source_type="oa",
        uploader_user_id="emp001",
    )
    events = file_svc.list_events(assistant_id="test-bot")
    assert len(events) >= 1
    upload_event = next(event for event in events if event.action == "upload")
    refresh_event = next(event for event in events if event.action == "sync")
    assert upload_event.user_id == "emp001"
    assert refresh_event.user_id == "emp001"
    assert refresh_event.detail["workspace_refresh_requested"] is True
    assert refresh_event.detail["workspace_reload_requested"] is True


def test_push_requests_recorded(file_svc: WorkspaceFileService) -> None:
    file_svc.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="pushed.txt",
        content=b"data",
        source_type="email",
        source_ref="msg-123",
    )
    pushes = file_svc.list_push_requests(assistant_id="test-bot")
    assert len(pushes) >= 1
    assert pushes[0].status == "accepted"
    assert pushes[0].source_type == "email"


def test_workspace_scan_syncs_result_file(
    file_svc: WorkspaceFileService,
    workspace_root: Path,
) -> None:
    result_path = workspace_root / "test-bot" / "persistent" / "reports" / "summary.txt"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("assistant generated result", encoding="utf-8")

    summary = file_svc.scan_workspace_results(assistant_id="test-bot", bucket="persistent")

    assert summary.files_scanned == 1
    assert summary.files_synced == 1
    files = file_svc.list_files("test-bot", bucket="persistent")
    assert len(files) == 1
    assert files[0].filename == "reports/summary.txt"
    assert files[0].file_role == "result"
    assert files[0].sync_status == "synced_to_workspace"


def test_workspace_scan_changed_after_skips_older_result_file(
    file_svc: WorkspaceFileService,
    workspace_root: Path,
) -> None:
    result_path = workspace_root / "test-bot" / "persistent" / "reports" / "old.txt"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("old assistant result", encoding="utf-8")
    old_mtime = datetime.now(tz=UTC) - timedelta(minutes=10)
    os.utime(result_path, (old_mtime.timestamp(), old_mtime.timestamp()))

    summary = file_svc.scan_workspace_results(
        assistant_id="test-bot",
        bucket="persistent",
        changed_after=datetime.now(tz=UTC) - timedelta(minutes=1),
    )

    assert summary.files_scanned == 1
    assert summary.files_synced == 0
    assert file_svc.list_files("test-bot", bucket="persistent") == []


def test_workspace_scan_results_filters_prefix_and_records_task_ref(
    file_svc: WorkspaceFileService,
    workspace_root: Path,
) -> None:
    task_a = workspace_root / "test-bot" / "temp" / "go-gateway-results" / "task-a" / "result.txt"
    task_b = workspace_root / "test-bot" / "temp" / "go-gateway-results" / "task-b" / "result.txt"
    task_a.parent.mkdir(parents=True, exist_ok=True)
    task_b.parent.mkdir(parents=True, exist_ok=True)
    task_a.write_text("task a result", encoding="utf-8")
    task_b.write_text("task b result", encoding="utf-8")

    summary = file_svc.scan_workspace_results(
        assistant_id="test-bot",
        bucket="temp",
        user_id="u-task-a",
        result_path_prefix="go-gateway-results/task-a/",
        result_source_ref="task-a",
    )

    assert summary.files_scanned == 1
    assert summary.files_synced == 1
    files = file_svc.list_files("test-bot", bucket="temp")
    assert len(files) == 1
    assert files[0].filename == "go-gateway-results/task-a/result.txt"
    assert files[0].source_ref == "task-a"
    assert files[0].owner_user_id == "u-task-a"
    assert files[0].direction == "outbound"
    assert files[0].file_role == "result"


def test_workspace_scan_results_records_multiple_files_in_same_task_prefix(
    file_svc: WorkspaceFileService,
    workspace_root: Path,
) -> None:
    result_dir = workspace_root / "test-bot" / "temp" / "go-gateway-results" / "task-a"
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "summary.json").write_text('{"ok": true}', encoding="utf-8")
    (result_dir / "report.txt").write_text("task a report", encoding="utf-8")

    summary = file_svc.scan_workspace_results(
        assistant_id="test-bot",
        bucket="temp",
        user_id="u-task-a",
        result_path_prefix="go-gateway-results/task-a/",
        result_source_ref="task-a",
    )

    assert summary.files_scanned == 2
    assert summary.files_synced == 2
    files = sorted(file_svc.list_files("test-bot", bucket="temp"), key=lambda item: item.filename)
    assert [item.filename for item in files] == [
        "go-gateway-results/task-a/report.txt",
        "go-gateway-results/task-a/summary.json",
    ]
    assert {item.source_ref for item in files} == {"task-a"}
    assert {item.owner_user_id for item in files} == {"u-task-a"}
    assert {item.direction for item in files} == {"outbound"}
    assert {item.file_role for item in files} == {"result"}


def test_workspace_scan_results_normalizes_existing_result_metadata(
    file_svc: WorkspaceFileService,
    workspace_root: Path,
) -> None:
    result_path = (
        workspace_root / "test-bot" / "temp" / "go-gateway-results" / "task-a" / "result.txt"
    )
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("old result", encoding="utf-8")
    first = file_svc.scan_workspace_results(
        assistant_id="test-bot",
        bucket="temp",
    )
    assert first.files_synced == 1
    result_path.write_text("new result", encoding="utf-8")

    summary = file_svc.scan_workspace_results(
        assistant_id="test-bot",
        bucket="temp",
        user_id="new-user",
        result_path_prefix="go-gateway-results/task-a/",
        result_source_ref="task-a",
    )

    assert summary.files_synced == 1
    files = file_svc.list_files("test-bot", bucket="temp")
    assert len(files) == 1
    assert files[0].source_ref == "task-a"
    assert files[0].owner_user_id == "new-user"
    assert files[0].uploader_user_id == "new-user"
    assert files[0].source_type == "assistant_result"
    assert files[0].direction == "outbound"
    assert files[0].file_role == "result"


def test_workspace_scan_results_normalizes_unchanged_task_scoped_metadata(
    file_svc: WorkspaceFileService,
    workspace_root: Path,
) -> None:
    result_path = (
        workspace_root / "test-bot" / "temp" / "go-gateway-results" / "task-a" / "result.txt"
    )
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("same result", encoding="utf-8")
    first = file_svc.scan_workspace_results(
        assistant_id="test-bot",
        bucket="temp",
    )
    assert first.files_synced == 1

    summary = file_svc.scan_workspace_results(
        assistant_id="test-bot",
        bucket="temp",
        user_id="new-user",
        result_path_prefix="go-gateway-results/task-a/",
        result_source_ref="task-a",
    )

    assert summary.files_synced == 1
    files = file_svc.list_files("test-bot", bucket="temp")
    assert len(files) == 1
    assert files[0].source_ref == "task-a"
    assert files[0].owner_user_id == "new-user"
    assert files[0].uploader_user_id == "new-user"
    assert files[0].direction == "outbound"
    assert files[0].file_role == "result"


def test_workspace_scan_results_rejects_existing_task_binding_mismatch(
    file_svc: WorkspaceFileService,
    workspace_root: Path,
) -> None:
    result_path = (
        workspace_root / "test-bot" / "temp" / "go-gateway-results" / "task-a" / "result.txt"
    )
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("old task result", encoding="utf-8")
    first = file_svc.scan_workspace_results(
        assistant_id="test-bot",
        bucket="temp",
        user_id="old-user",
        result_path_prefix="go-gateway-results/task-a/",
        result_source_ref="old-task",
    )
    assert first.files_synced == 1
    result_path.write_text("new task result", encoding="utf-8")

    summary = file_svc.scan_workspace_results(
        assistant_id="test-bot",
        bucket="temp",
        user_id="new-user",
        result_path_prefix="go-gateway-results/task-a/",
        result_source_ref="task-a",
    )

    assert summary.files_synced == 0
    assert summary.errors
    assert "existing result binding mismatch" in summary.errors[0]
    files = file_svc.list_files("test-bot", bucket="temp")
    assert len(files) == 1
    assert files[0].source_ref == "old-task"
    assert files[0].owner_user_id == "old-user"
    assert files[0].uploader_user_id == "old-user"
    assert files[0].size_bytes == len("old task result")


def test_workspace_scan_results_unscoped_scan_does_not_rebind_task_metadata(
    file_svc: WorkspaceFileService,
    workspace_root: Path,
) -> None:
    result_path = (
        workspace_root / "test-bot" / "temp" / "go-gateway-results" / "task-a" / "result.txt"
    )
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("task a result", encoding="utf-8")
    initial = file_svc.scan_workspace_results(assistant_id="test-bot", bucket="temp")
    assert initial.files_synced == 1

    summary = file_svc.scan_workspace_results(
        assistant_id="test-bot",
        bucket="temp",
        user_id="new-user",
        result_source_ref="task-a",
    )

    assert summary.files_synced == 0
    files = file_svc.list_files("test-bot", bucket="temp")
    assert len(files) == 1
    assert files[0].source_ref == "workspace_scan"
    assert not files[0].owner_user_id
    assert not files[0].uploader_user_id


def test_workspace_scan_results_deletes_only_scoped_prefix(
    file_svc: WorkspaceFileService,
    workspace_root: Path,
) -> None:
    task_a = workspace_root / "test-bot" / "temp" / "go-gateway-results" / "task-a" / "result.txt"
    task_b = workspace_root / "test-bot" / "temp" / "go-gateway-results" / "task-b" / "result.txt"
    task_a.parent.mkdir(parents=True, exist_ok=True)
    task_b.parent.mkdir(parents=True, exist_ok=True)
    task_a.write_text("task a result", encoding="utf-8")
    task_b.write_text("task b result", encoding="utf-8")

    initial = file_svc.scan_workspace_results(assistant_id="test-bot", bucket="temp")
    assert initial.files_scanned == 2
    assert initial.files_synced == 2

    task_a.unlink()
    summary = file_svc.scan_workspace_results(
        assistant_id="test-bot",
        bucket="temp",
        result_path_prefix="go-gateway-results/task-a/",
        result_source_ref="task-a",
    )

    assert summary.files_deleted == 1
    filenames = {item.filename for item in file_svc.list_files("test-bot", bucket="temp")}
    assert "go-gateway-results/task-a/result.txt" not in filenames
    assert "go-gateway-results/task-b/result.txt" in filenames


def test_workspace_scan_results_rejects_invalid_prefix(
    file_svc: WorkspaceFileService,
) -> None:
    with pytest.raises(FileSecurityError, match="path traversal"):
        file_svc.scan_workspace_results(
            assistant_id="test-bot",
            bucket="temp",
            result_path_prefix="../go-gateway-results/task-a/",
        )


def test_workspace_scan_results_rejects_non_result_bucket(
    file_svc: WorkspaceFileService,
) -> None:
    with pytest.raises(FileSecurityError, match="result sync bucket"):
        file_svc.scan_workspace_results(
            assistant_id="test-bot",
            bucket="skills",  # type: ignore[arg-type]
            result_path_prefix="go-gateway-results/task-a/",
        )


def test_workspace_scan_results_rejects_bucket_level_prefix(
    file_svc: WorkspaceFileService,
) -> None:
    with pytest.raises(FileSecurityError, match="task directory"):
        file_svc.scan_workspace_results(
            assistant_id="test-bot",
            result_path_prefix="temp",
        )


def test_workspace_scan_results_without_bucket_scans_only_result_buckets(
    file_svc: WorkspaceFileService,
    workspace_root: Path,
) -> None:
    for bucket in ("temp", "persistent", "skills", "plugins"):
        result_file = (
            workspace_root
            / "test-bot"
            / bucket
            / "go-gateway-results"
            / "task-both-buckets"
            / "result.txt"
        )
        result_file.parent.mkdir(parents=True, exist_ok=True)
        result_file.write_text(f"{bucket} result", encoding="utf-8")

    summary = file_svc.scan_workspace_results(
        assistant_id="test-bot",
        result_path_prefix="go-gateway-results/task-both-buckets/",
        result_source_ref="task-both-buckets",
    )

    assert summary.files_scanned == 2
    assert summary.files_synced == 2
    files = file_svc.list_files("test-bot")
    assert {item.bucket for item in files} == {"temp", "persistent"}
    assert {item.source_ref for item in files} == {"task-both-buckets"}


def test_workspace_scan_results_accepts_bucket_prefixed_prefix(
    file_svc: WorkspaceFileService,
    workspace_root: Path,
) -> None:
    temp_file = (
        workspace_root / "test-bot" / "temp" / "go-gateway-results" / "task-prefixed" / "result.txt"
    )
    persistent_file = (
        workspace_root
        / "test-bot"
        / "persistent"
        / "go-gateway-results"
        / "task-prefixed"
        / "result.txt"
    )
    temp_file.parent.mkdir(parents=True, exist_ok=True)
    persistent_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file.write_text("temp result", encoding="utf-8")
    persistent_file.write_text("persistent result", encoding="utf-8")

    summary = file_svc.scan_workspace_results(
        assistant_id="test-bot",
        result_path_prefix="temp/go-gateway-results/task-prefixed/",
        result_source_ref="task-prefixed",
    )

    assert summary.files_scanned == 1
    files = file_svc.list_files("test-bot")
    assert len(files) == 1
    assert files[0].bucket == "temp"
    assert files[0].filename == "go-gateway-results/task-prefixed/result.txt"


def test_workspace_scan_indexes_skill_package_files(
    file_svc: WorkspaceFileService,
    workspace_root: Path,
) -> None:
    skill_path = workspace_root / "test-bot" / "skills" / "web-crawl" / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text("crawl skill", encoding="utf-8")

    summary = file_svc.scan_workspace_results(assistant_id="test-bot", bucket="skills")

    assert summary.files_scanned == 1
    assert summary.files_synced == 1
    files = file_svc.list_files("test-bot", bucket="skills")
    assert len(files) == 1
    assert files[0].filename == "web-crawl/SKILL.md"
    assert files[0].direction == "inbound"
    assert files[0].file_role == "input"
    assert files[0].source_type == "capability_package"
    assert files[0].sync_status == "synced_to_workspace"


def test_workspace_scan_skips_deleted_assistant(
    file_svc: WorkspaceFileService,
    workspace_root: Path,
) -> None:
    file_svc.assistant_svc.upsert_assistant(
        assistant_id="deleted-bot",
        name="Deleted Bot",
        capability_profile="onlyread",
        provision_runtime=False,
    )
    deleted = file_svc.assistant_svc.delete_assistant("deleted-bot")
    assert deleted is not None
    result_dir = workspace_root / "deleted-bot" / "persistent"
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "result.txt").write_text("deleted result", encoding="utf-8")

    summary = file_svc.scan_workspace_results(assistant_id="deleted-bot", bucket="persistent")
    synced = file_svc.sync_workspace_outputs("deleted-bot")

    assert summary.assistants_scanned == 0
    assert summary.files_scanned == 0
    assert list(summary) == []
    assert synced == []
    assert file_svc.list_files("deleted-bot") == []


def test_ensure_workspace_storage_prefixes_creates_persistent_and_temp_markers(
    file_svc_with_object_store: WorkspaceFileService,
    object_store: FakeObjectStore,
) -> None:
    created = file_svc_with_object_store.ensure_workspace_storage_prefixes("test-bot")

    assert created == [
        "workspace-files/test-bot/persistent/.keep",
        "workspace-files/test-bot/temp/.keep",
    ]
    assert object_store.objects["workspace-files/test-bot/persistent/.keep"] == b""
    assert object_store.objects["workspace-files/test-bot/temp/.keep"] == b""


def test_download_returns_none_when_workspace_copy_missing(
    file_svc_with_object_store: WorkspaceFileService,
    workspace_root: Path,
) -> None:
    uploaded = file_svc_with_object_store.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="remote-only.txt",
        content=b"download from object storage",
    )
    assert isinstance(uploaded, WorkspaceFile)

    workspace_copy = workspace_root / "test-bot" / "temp" / "remote-only.txt"
    workspace_copy.unlink()

    downloaded = file_svc_with_object_store.download_file(uploaded.file_id)

    assert downloaded is None


def test_download_does_not_use_record_object_url_when_workspace_copy_missing(
    file_svc_with_object_store: WorkspaceFileService,
    workspace_root: Path,
    object_store: FakeObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploaded = file_svc_with_object_store.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="remote-url.txt",
        content=b"download from object url",
    )
    assert isinstance(uploaded, WorkspaceFile)

    workspace_copy = workspace_root / "test-bot" / "temp" / "remote-url.txt"
    workspace_copy.unlink()
    object_store.objects.pop(uploaded.object_key, None)
    uploaded.object_key = "workspace-files/test-bot/temp/missing-remote-url.txt"
    uploaded.storage_key = uploaded.object_key
    uploaded.object_url = (
        "https://bucket.cos.ap-shanghai.myqcloud.com/"
        "heinu/workspace-files/test-bot/temp/remote-url.txt"
    )
    file_svc_with_object_store.file_records.write_files([uploaded])

    requested_urls: list[str] = []

    def fake_download_cos_url_bytes(url: str) -> bytes | None:
        requested_urls.append(url)
        return b"download from object url"

    monkeypatch.setattr(
        "src.workspace_files.service.download_cos_url_bytes",
        fake_download_cos_url_bytes,
    )

    downloaded = file_svc_with_object_store.download_file(uploaded.file_id)

    assert downloaded is None
    assert requested_urls == []
