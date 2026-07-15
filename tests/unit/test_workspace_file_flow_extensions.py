from pathlib import Path

import pytest

from src.approval.service import ApprovalService
from src.assistants.service import AssistantService
from src.users.service import UserService
from src.workspace_files.models import WorkspaceFile
from src.workspace_files.service import WorkspaceFileService


@pytest.fixture()
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspaces"
    root.mkdir()
    return root


@pytest.fixture()
def file_svc(tmp_path: Path, workspace_root: Path) -> WorkspaceFileService:
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
    )
    svc._workspace_dir = lambda aid: workspace_root / aid
    svc.workspace_writer.workspace_dir_resolver = lambda aid: workspace_root / aid
    return svc


def test_upload_marks_storage_disabled_when_object_storage_is_not_configured(
    file_svc: WorkspaceFileService,
) -> None:
    result = file_svc.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="notes.txt",
        content=b"hello storage",
    )
    assert isinstance(result, WorkspaceFile)
    assert result.storage_provider == "cos"
    assert result.storage_status == "disabled"
    assert result.storage_error == "object storage is not configured"


def test_scan_workspace_results_ingests_new_files(file_svc: WorkspaceFileService) -> None:
    result_dir = file_svc.workspace_writer.bucket_dir("test-bot", "persistent")
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "generated-report.txt").write_text("report body", encoding="utf-8")

    ingested = file_svc.scan_workspace_results("test-bot")
    assert len(ingested) == 1
    assert ingested[0].filename == "generated-report.txt"
    assert ingested[0].file_role == "result"
    assert ingested[0].relative_path == "persistent/generated-report.txt"
    assert ingested[0].storage_status == "disabled"

    listed = file_svc.list_files("test-bot", bucket="persistent")
    assert len(listed) == 1
    assert listed[0].file_role == "result"

    events = file_svc.list_events(assistant_id="test-bot")
    assert any(event.action == "ingest" for event in events)


def test_download_requires_workspace_file_even_when_object_storage_has_copy(
    file_svc: WorkspaceFileService,
) -> None:
    result = file_svc.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="remote-only.txt",
        content=b"hello remote",
        content_type="text/plain",
    )
    assert isinstance(result, WorkspaceFile)

    result.storage_status = "synced"
    result.storage_provider = "cos"
    result.storage_key = "workspace-files/test-bot/temp/remote-only.txt"
    result.storage_path = "https://example.com/remote-only.txt"
    files = file_svc.file_records.load_files()
    target = next(item for item in files if item.file_id == result.file_id)
    target.storage_status = result.storage_status
    target.storage_provider = result.storage_provider
    target.storage_key = result.storage_key
    target.storage_path = result.storage_path
    file_svc.file_records.write_files(files)

    workspace_path = file_svc.workspace_writer.bucket_dir("test-bot", "temp") / "remote-only.txt"
    workspace_path.unlink()

    file_svc.object_storage_gateway.is_enabled = lambda: True
    def fail_if_used(*, key: str):
        raise AssertionError(f"object storage must not be used for download: {key}")

    file_svc.object_storage_gateway.download_bytes = fail_if_used

    downloaded = file_svc.download_file(result.file_id)
    assert downloaded is None
