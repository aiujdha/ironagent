from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError

from src.approval.service import ApprovalService
from src.assistants.service import AssistantService
from src.shared.blob_storage import BlobPutResult
from src.users.service import UserService
from src.workspace_files.models import WorkspaceFile
from src.workspace_files.service import WorkspaceFileService
from src.workspace_files.sync_service import WorkspaceSyncService, WorkspaceSyncStats


class StubBlobStorage:
    def __init__(self) -> None:
        self.delete_calls: list[str] = []

    def is_enabled(self) -> bool:
        return True

    def put_bytes(self, *, key: str, content: bytes) -> BlobPutResult:
        return BlobPutResult(backend="cos", key=key, url=f"https://cos.example/{key}")

    def delete(self, key: str) -> None:
        self.delete_calls.append(key)
        return None


def _build_assistant_service(tmp_path: Path) -> AssistantService:
    assistant_svc = AssistantService(store_path=tmp_path / "assistants.json")
    assistant_svc.upsert_assistant(
        assistant_id="test-bot",
        name="Test Bot",
        capability_profile="onlyread",
        provision_runtime=False,
    )
    return assistant_svc


def test_workspace_sync_service_scan_all_skips_deleted_assistants(tmp_path: Path) -> None:
    assistant_svc = AssistantService(store_path=tmp_path / "assistants.json")
    assistant_svc.upsert_assistant(
        assistant_id="active-bot",
        name="Active Bot",
        capability_profile="onlyread",
        provision_runtime=False,
    )
    assistant_svc.upsert_assistant(
        assistant_id="deleted-bot",
        name="Deleted Bot",
        capability_profile="onlyread",
        provision_runtime=False,
    )
    deleted = assistant_svc.delete_assistant("deleted-bot")
    assert deleted is not None

    sync_svc = WorkspaceSyncService(assistant_service=assistant_svc, blob_storage=None)
    calls: list[str] = []

    def sync_assistant_workspace(assistant_id: str) -> WorkspaceSyncStats:
        calls.append(assistant_id)
        return WorkspaceSyncStats(scanned_files=1)

    sync_svc.sync_assistant_workspace = sync_assistant_workspace  # type: ignore[method-assign]

    results = sync_svc.scan_all_assistants()

    assert "active-bot" in calls
    assert "deleted-bot" not in calls
    assert "active-bot" in results
    assert "deleted-bot" not in results


def test_workspace_sync_service_rejects_deleted_assistant_sync(tmp_path: Path) -> None:
    assistant_svc = AssistantService(store_path=tmp_path / "assistants.json")
    assistant_svc.upsert_assistant(
        assistant_id="deleted-bot",
        name="Deleted Bot",
        capability_profile="onlyread",
        provision_runtime=False,
    )
    deleted = assistant_svc.delete_assistant("deleted-bot")
    assert deleted is not None

    sync_svc = WorkspaceSyncService(assistant_service=assistant_svc, blob_storage=None)

    with pytest.raises(ValueError, match="assistant 'deleted-bot' not found"):
        sync_svc.sync_assistant_workspace("deleted-bot")


def test_workspace_sync_service_creates_records_from_workspace(tmp_path: Path) -> None:
    assistant_svc = _build_assistant_service(tmp_path)
    blob = StubBlobStorage()
    file_svc = WorkspaceFileService(
        files_store=tmp_path / "files.json",
        events_store=tmp_path / "events.json",
        push_store=tmp_path / "push.json",
        assistant_service=assistant_svc,
        user_service=UserService(
            store_path=tmp_path / "users.json",
            bindings_path=tmp_path / "bindings.json",
        ),
        approval_service=ApprovalService(store_path=tmp_path / "approvals.json"),
        blob_storage=blob,
    )
    workspace_root = tmp_path / "workspaces" / "test-bot" / "persistent"
    workspace_root.mkdir(parents=True, exist_ok=True)
    (workspace_root / "result.txt").write_text("hello sync", encoding="utf-8")
    file_svc._workspace_dir = lambda aid: tmp_path / "workspaces" / aid

    sync_svc = WorkspaceSyncService(
        assistant_service=assistant_svc,
        repository=file_svc.repository,
        blob_storage=blob,
    )
    assistant = assistant_svc.get_assistant("test-bot")
    assert assistant is not None
    assistant.runtime.workspace_root = str(tmp_path / "workspaces" / "test-bot")
    assistant_svc.repository.upsert_assistant(assistant)

    stats = sync_svc.sync_assistant_workspace("test-bot")

    assert stats.scanned_files == 1
    assert stats.created == 1
    files = file_svc.list_files("test-bot", bucket="persistent")
    assert len(files) == 1
    assert files[0].filename == "result.txt"
    assert files[0].origin == "assistant_output"
    assert files[0].storage_backend == "cos"


def test_workspace_sync_service_reports_metadata_degraded_on_write_error(
    tmp_path: Path,
) -> None:
    assistant_svc = _build_assistant_service(tmp_path)
    workspace_root = tmp_path / "workspaces" / "test-bot" / "persistent"
    workspace_root.mkdir(parents=True, exist_ok=True)
    (workspace_root / "result.txt").write_text("hello sync", encoding="utf-8")
    assistant = assistant_svc.get_assistant("test-bot")
    assert assistant is not None
    assistant.runtime.workspace_root = str(tmp_path / "workspaces" / "test-bot")
    assistant_svc.repository.upsert_assistant(assistant)

    class FailingRepository:
        async def alist_files(self, **_: object) -> list[WorkspaceFile]:
            return []

        async def asave_file(self, record: WorkspaceFile) -> WorkspaceFile:
            del record
            raise SQLAlchemyError("metadata db unavailable")

    sync_svc = WorkspaceSyncService(
        assistant_service=assistant_svc,
        repository=FailingRepository(),  # type: ignore[arg-type]
        blob_storage=None,
    )

    stats = sync_svc.sync_assistant_workspace("test-bot")

    assert stats.scanned_files == 1
    assert stats.metadata_degraded is True


def test_workspace_sync_service_updates_existing_records(tmp_path: Path) -> None:
    assistant_svc = _build_assistant_service(tmp_path)
    blob = StubBlobStorage()
    file_svc = WorkspaceFileService(
        files_store=tmp_path / "files.json",
        events_store=tmp_path / "events.json",
        push_store=tmp_path / "push.json",
        assistant_service=assistant_svc,
        user_service=UserService(
            store_path=tmp_path / "users.json",
            bindings_path=tmp_path / "bindings.json",
        ),
        approval_service=ApprovalService(store_path=tmp_path / "approvals.json"),
        blob_storage=blob,
    )
    file_svc._workspace_dir = lambda aid: tmp_path / "workspaces" / aid
    record = file_svc.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="draft.log",
        content=b"old",
        source_type="assistant_output",
    )
    assert record.filename == "draft.log"

    workspace_file = tmp_path / "workspaces" / "test-bot" / "temp" / "draft.log"
    workspace_file.write_text("new content", encoding="utf-8")

    sync_svc = WorkspaceSyncService(
        assistant_service=assistant_svc,
        repository=file_svc.repository,
        blob_storage=blob,
    )
    assistant = assistant_svc.get_assistant("test-bot")
    assert assistant is not None
    assistant.runtime.workspace_root = str(tmp_path / "workspaces" / "test-bot")
    assistant_svc.repository.upsert_assistant(assistant)

    stats = sync_svc.sync_assistant_workspace("test-bot")

    assert stats.updated == 1
    files = file_svc.list_files("test-bot", bucket="temp")
    assert len(files) == 1
    assert files[0].file_id == record.file_id
    assert files[0].size_bytes == len(b"new content")


def test_workspace_sync_service_marks_missing_assistant_outputs_deleted(tmp_path: Path) -> None:
    assistant_svc = _build_assistant_service(tmp_path)
    blob = StubBlobStorage()
    file_svc = WorkspaceFileService(
        files_store=tmp_path / "files.json",
        events_store=tmp_path / "events.json",
        push_store=tmp_path / "push.json",
        assistant_service=assistant_svc,
        user_service=UserService(
            store_path=tmp_path / "users.json",
            bindings_path=tmp_path / "bindings.json",
        ),
        approval_service=ApprovalService(store_path=tmp_path / "approvals.json"),
        blob_storage=blob,
    )
    file_svc._workspace_dir = lambda aid: tmp_path / "workspaces" / aid
    record = file_svc.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="gone.log",
        content=b"old",
        source_type="assistant_output",
    )
    assert record.filename == "gone.log"

    workspace_root = tmp_path / "workspaces" / "test-bot"
    assistant = assistant_svc.get_assistant("test-bot")
    assert assistant is not None
    assistant.runtime.workspace_root = str(workspace_root)
    assistant_svc.repository.upsert_assistant(assistant)
    (workspace_root / "temp" / "gone.log").unlink()

    sync_svc = WorkspaceSyncService(
        assistant_service=assistant_svc,
        repository=file_svc.repository,
        blob_storage=blob,
    )

    stats = sync_svc.sync_assistant_workspace("test-bot")

    assert stats.removed == 1
    deleted = file_svc.get_file(record.file_id)
    assert deleted is not None
    assert deleted.deleted is True
    assert deleted.sync_status == "deleted_from_workspace"
    assert deleted.storage_status == "deleted_from_workspace"
    assert blob.delete_calls == ["workspace-files/test-bot/temp/gone.log"]


def test_workspace_sync_service_marks_missing_inbound_files_drifted(tmp_path: Path) -> None:
    assistant_svc = _build_assistant_service(tmp_path)
    blob = StubBlobStorage()
    file_svc = WorkspaceFileService(
        files_store=tmp_path / "files.json",
        events_store=tmp_path / "events.json",
        push_store=tmp_path / "push.json",
        assistant_service=assistant_svc,
        user_service=UserService(
            store_path=tmp_path / "users.json",
            bindings_path=tmp_path / "bindings.json",
        ),
        approval_service=ApprovalService(store_path=tmp_path / "approvals.json"),
        blob_storage=blob,
    )
    file_svc._workspace_dir = lambda aid: tmp_path / "workspaces" / aid
    record = file_svc.upload_file(
        assistant_id="test-bot",
        bucket="persistent",
        filename="input.pdf",
        content=b"inbound",
        source_type="web",
    )
    assert record.filename == "input.pdf"

    workspace_root = tmp_path / "workspaces" / "test-bot"
    assistant = assistant_svc.get_assistant("test-bot")
    assert assistant is not None
    assistant.runtime.workspace_root = str(workspace_root)
    assistant_svc.repository.upsert_assistant(assistant)
    (workspace_root / "persistent" / "input.pdf").unlink()

    sync_svc = WorkspaceSyncService(
        assistant_service=assistant_svc,
        repository=file_svc.repository,
        blob_storage=blob,
    )

    stats = sync_svc.sync_assistant_workspace("test-bot")

    assert stats.drifted == 1
    drifted = file_svc.get_file(record.file_id)
    assert drifted is not None
    assert drifted.deleted is False
    assert drifted.sync_status == "missing_from_workspace"


def test_workspace_sync_service_uses_assistant_name_and_id_for_object_storage_key(
    tmp_path: Path,
) -> None:
    assistant_svc = AssistantService(store_path=tmp_path / "assistants.json")
    assistant_svc.upsert_assistant(
        assistant_id="assistant-42",
        name="Finance Analyst",
        capability_profile="onlyread",
        provision_runtime=False,
    )
    blob = StubBlobStorage()
    file_svc = WorkspaceFileService(
        files_store=tmp_path / "files.json",
        events_store=tmp_path / "events.json",
        push_store=tmp_path / "push.json",
        assistant_service=assistant_svc,
        user_service=UserService(
            store_path=tmp_path / "users.json",
            bindings_path=tmp_path / "bindings.json",
        ),
        approval_service=ApprovalService(store_path=tmp_path / "approvals.json"),
        blob_storage=blob,
    )
    workspace_root = tmp_path / "workspaces" / "assistant-42" / "persistent"
    workspace_root.mkdir(parents=True, exist_ok=True)
    (workspace_root / "result.txt").write_text("hello sync", encoding="utf-8")
    file_svc._workspace_dir = lambda aid: tmp_path / "workspaces" / aid

    sync_svc = WorkspaceSyncService(
        assistant_service=assistant_svc,
        repository=file_svc.repository,
        blob_storage=blob,
    )
    assistant = assistant_svc.get_assistant("assistant-42")
    assert assistant is not None
    assistant.runtime.workspace_root = str(tmp_path / "workspaces" / "assistant-42")
    assistant_svc.repository.upsert_assistant(assistant)

    stats = sync_svc.sync_assistant_workspace("assistant-42")

    assert stats.created == 1
    files = file_svc.list_files("assistant-42", bucket="persistent")
    assert len(files) == 1
    assert (
        files[0].storage_key
        == "finance-analyst-assistant-42/persistent/result.txt"
    )


def test_workspace_sync_service_scoped_sync_only_touches_target_directory(tmp_path: Path) -> None:
    assistant_svc = _build_assistant_service(tmp_path)
    blob = StubBlobStorage()
    file_svc = WorkspaceFileService(
        files_store=tmp_path / "files.json",
        events_store=tmp_path / "events.json",
        push_store=tmp_path / "push.json",
        assistant_service=assistant_svc,
        user_service=UserService(
            store_path=tmp_path / "users.json",
            bindings_path=tmp_path / "bindings.json",
        ),
        approval_service=ApprovalService(store_path=tmp_path / "approvals.json"),
        blob_storage=blob,
    )
    file_svc._workspace_dir = lambda aid: tmp_path / "workspaces" / aid
    existing = file_svc.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="existing.log",
        content=b"keep me",
        source_type="assistant_output",
    )

    workspace_root = tmp_path / "workspaces" / "test-bot"
    skill_root = workspace_root / "skills" / "pdf-exporter"
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "SKILL.md").write_text("# PDF Exporter", encoding="utf-8")

    assistant = assistant_svc.get_assistant("test-bot")
    assert assistant is not None
    assistant.runtime.workspace_root = str(workspace_root)
    assistant_svc.repository.upsert_assistant(assistant)

    sync_svc = WorkspaceSyncService(
        assistant_service=assistant_svc,
        repository=file_svc.repository,
        blob_storage=blob,
    )

    stats = sync_svc.sync_assistant_workspace_scopes(
        "test-bot",
        [("skills", "pdf-exporter")],
    )

    assert stats.scanned_files == 1
    assert stats.created == 1
    assert stats.removed == 0
    created = file_svc.list_files("test-bot", bucket="skills")
    assert len(created) == 1
    assert created[0].filename == "pdf-exporter/SKILL.md"
    assert created[0].source_type == "capability_package"
    assert created[0].origin == "capability_package"
    assert created[0].direction == "inbound"
    assert created[0].file_role == "input"
    assert created[0].source_file_id is None
    kept = file_svc.get_file(existing.file_id)
    assert kept is not None
    assert kept.deleted is False
    assert kept.sync_status == "synced"


def test_workspace_sync_service_scoped_mark_missing_only_touches_scope(
    tmp_path: Path,
) -> None:
    assistant_svc = _build_assistant_service(tmp_path)
    blob = StubBlobStorage()
    file_svc = WorkspaceFileService(
        files_store=tmp_path / "files.json",
        events_store=tmp_path / "events.json",
        push_store=tmp_path / "push.json",
        assistant_service=assistant_svc,
        user_service=UserService(
            store_path=tmp_path / "users.json",
            bindings_path=tmp_path / "bindings.json",
        ),
        approval_service=ApprovalService(store_path=tmp_path / "approvals.json"),
        blob_storage=blob,
    )
    file_svc._workspace_dir = lambda aid: tmp_path / "workspaces" / aid
    in_scope = file_svc.repository.save_file(
        WorkspaceFile(
            assistant_id="test-bot",
            bucket="skills",
            filename="pdf-exporter/SKILL.md",
            relative_path="skills/pdf-exporter/SKILL.md",
            direction="inbound",
            file_role="input",
            source_type="capability_package",
            source_ref="workspace_scan",
            storage_key="test-bot/skills/pdf-exporter/SKILL.md",
            sync_status="synced",
    )
        )
    other_skill = file_svc.repository.save_file(
        WorkspaceFile(
            assistant_id="test-bot",
            bucket="skills",
            filename="other-skill/SKILL.md",
            relative_path="skills/other-skill/SKILL.md",
            direction="inbound",
            file_role="input",
            source_type="capability_package",
            source_ref="workspace_scan",
            storage_key="test-bot/skills/other-skill/SKILL.md",
            sync_status="synced",
    )
        )
    other_bucket = file_svc.repository.save_file(
        WorkspaceFile(
            assistant_id="test-bot",
            bucket="temp",
            filename="old.log",
            relative_path="temp/old.log",
            direction="outbound",
            file_role="result",
            source_type="assistant_output",
            source_ref="workspace_scan",
            storage_key="test-bot/temp/old.log",
            sync_status="synced",
    )
        )

    workspace_root = tmp_path / "workspaces" / "test-bot"
    (workspace_root / "skills" / "pdf-exporter").mkdir(parents=True)
    assistant = assistant_svc.get_assistant("test-bot")
    assert assistant is not None
    assistant.runtime.workspace_root = str(workspace_root)
    assistant_svc.repository.upsert_assistant(assistant)

    sync_svc = WorkspaceSyncService(
        assistant_service=assistant_svc,
        repository=file_svc.repository,
        blob_storage=blob,
    )

    stats = sync_svc.sync_assistant_workspace_scopes(
        "test-bot",
        [("skills", "pdf-exporter")],
        mark_missing=True,
    )

    assert stats.removed == 1
    deleted = file_svc.get_file(in_scope.file_id)
    assert deleted is not None
    assert deleted.deleted is True
    assert deleted.sync_status == "deleted_from_workspace"
    kept_skill = file_svc.get_file(other_skill.file_id)
    assert kept_skill is not None
    assert kept_skill.deleted is False
    kept_bucket = file_svc.get_file(other_bucket.file_id)
    assert kept_bucket is not None
    assert kept_bucket.deleted is False


def test_workspace_sync_service_marks_missing_capability_files_deleted(
    tmp_path: Path,
) -> None:
    assistant_svc = _build_assistant_service(tmp_path)
    blob = StubBlobStorage()
    file_svc = WorkspaceFileService(
        files_store=tmp_path / "files.json",
        events_store=tmp_path / "events.json",
        push_store=tmp_path / "push.json",
        assistant_service=assistant_svc,
        user_service=UserService(
            store_path=tmp_path / "users.json",
            bindings_path=tmp_path / "bindings.json",
        ),
        approval_service=ApprovalService(store_path=tmp_path / "approvals.json"),
        blob_storage=blob,
    )
    file_svc._workspace_dir = lambda aid: tmp_path / "workspaces" / aid
    existing = file_svc.repository.save_file(
        WorkspaceFile(
            assistant_id="test-bot",
            bucket="skills",
            filename="pdf-exporter/SKILL.md",
            relative_path="skills/pdf-exporter/SKILL.md",
            direction="inbound",
            file_role="input",
            source_type="capability_package",
            source_ref="workspace_scan",
            storage_key="test-bot/skills/pdf-exporter/SKILL.md",
            sync_status="synced",
    )
        )

    workspace_root = tmp_path / "workspaces" / "test-bot"
    (workspace_root / "skills").mkdir(parents=True)
    assistant = assistant_svc.get_assistant("test-bot")
    assert assistant is not None
    assistant.runtime.workspace_root = str(workspace_root)
    assistant_svc.repository.upsert_assistant(assistant)

    sync_svc = WorkspaceSyncService(
        assistant_service=assistant_svc,
        repository=file_svc.repository,
        blob_storage=blob,
    )

    stats = sync_svc.sync_assistant_workspace("test-bot")

    assert stats.removed == 1
    deleted = file_svc.get_file(existing.file_id)
    assert deleted is not None
    assert deleted.deleted is True
    assert deleted.sync_status == "deleted_from_workspace"
