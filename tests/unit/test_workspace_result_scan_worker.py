from pathlib import Path

import pytest

from src.approval.service import ApprovalService
from src.assistants.models import AssistantRecord
from src.assistants.service import AssistantService
from src.runtime.models import AssistantRuntimeRecord
from src.users.service import UserService
from src.workspace_files.result_scan_worker import WorkspaceResultScanWorker
from src.workspace_files.scanner import WorkspaceResultScanner
from src.workspace_files.service import WorkspaceFileService


@pytest.fixture()
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspaces"
    root.mkdir()
    return root


@pytest.fixture()
def worker(tmp_path: Path, workspace_root: Path) -> WorkspaceResultScanWorker:
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
    file_svc = WorkspaceFileService(
        files_store=tmp_path / "files.json",
        events_store=tmp_path / "events.json",
        push_store=tmp_path / "push.json",
        assistant_service=assistant_svc,
        user_service=user_svc,
        approval_service=approval_svc,
    )
    file_svc._workspace_dir = lambda aid: workspace_root / aid
    file_svc.workspace_writer.workspace_dir_resolver = lambda aid: workspace_root / aid
    return WorkspaceResultScanWorker(
        assistant_service=assistant_svc,
        file_service=file_svc,
    )


def test_scan_once_ingests_new_result_files(worker: WorkspaceResultScanWorker) -> None:
    result_dir = worker.file_service.workspace_writer.bucket_dir("test-bot", "persistent")
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "worker-result.txt").write_text("worker result", encoding="utf-8")

    result = worker.scan_once()

    assert result.assistants_scanned == 1
    assert result.files_ingested == 1
    files = worker.file_service.list_files("test-bot")
    assert files[0].filename == "worker-result.txt"
    assert files[0].file_role == "result"


def test_scan_once_is_idempotent_for_known_results(worker: WorkspaceResultScanWorker) -> None:
    result_dir = worker.file_service.workspace_writer.bucket_dir("test-bot", "temp")
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "known.txt").write_text("known", encoding="utf-8")

    first = worker.scan_once()
    second = worker.scan_once()

    assert first.files_ingested == 1
    assert second.files_ingested == 0


def test_scan_once_skips_deleted_assistants(worker: WorkspaceResultScanWorker) -> None:
    worker.assistant_service.upsert_assistant(
        assistant_id="deleted-bot",
        name="Deleted Bot",
        capability_profile="onlyread",
        provision_runtime=False,
    )
    deleted = worker.assistant_service.delete_assistant("deleted-bot")
    assert deleted is not None
    result_dir = worker.file_service.workspace_writer.bucket_dir("deleted-bot", "persistent")
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "deleted-result.txt").write_text("deleted result", encoding="utf-8")

    result = worker.scan_once()

    assert result.assistants_scanned == 0
    assert result.files_ingested == 0
    assert worker.file_service.list_files("deleted-bot") == []


def test_workspace_result_scanner_running_filter_skips_deleted_assistants() -> None:
    record = AssistantRecord(
        assistant_id="deleted-bot",
        name="Deleted Bot",
        deleted_at="2026-05-22T00:00:00+00:00",
        runtime=AssistantRuntimeRecord(status="running", workspace_root="/tmp/deleted-bot"),
    )

    assert WorkspaceResultScanner._running_assistants([record]) == []
