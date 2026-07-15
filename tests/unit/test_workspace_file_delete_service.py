from src.approval.service import ApprovalService
from src.shared.config import get_settings
from src.workspace_files.file_delete_service import FileDeleteService
from src.workspace_files.file_event_repository import FileEventRepository
from src.workspace_files.file_record_repository import FileRecordRepository
from src.workspace_files.models import WorkspaceFile
from src.workspace_files.push_request_repository import PushRequestRepository
from src.workspace_files.workspace_writer import WorkspaceWriter


def test_file_delete_service_allows_persistent_delete_when_approval_mode_disabled(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("IRON_AGENT_APPROVAL_MODE", "disabled")
    get_settings.cache_clear()
    try:
        file_records = FileRecordRepository(tmp_path / "files.json")
        file_events = FileEventRepository(tmp_path / "events.json")
        push_requests = PushRequestRepository(tmp_path / "push.json")
        workspace_root = tmp_path / "workspace"
        workspace_writer = WorkspaceWriter(lambda assistant_id: workspace_root / assistant_id)
        approval_service = ApprovalService(store_path=tmp_path / "approvals.json")
        record = WorkspaceFile(
            assistant_id="assistant-a",
            bucket="persistent",
            filename="keep.pdf",
            relative_path="persistent/keep.pdf",
        )
        file_records.write_files([record])
        workspace_writer.write_file(
            assistant_id=record.assistant_id,
            bucket=record.bucket,
            filename=record.filename,
            content=b"important",
        )
        service = FileDeleteService(
            file_records=file_records,
            file_events=file_events,
            push_requests=push_requests,
            workspace_writer=workspace_writer,
            approval_service=approval_service,
        )

        result = service.delete_file(record.file_id, user_id="emp001")

        assert isinstance(result, WorkspaceFile)
        assert result.deleted is True
        assert approval_service.list_requests() == []
        assert push_requests.load_push_requests() == []
        events = file_events.list_events(file_id=record.file_id)
        assert events[0].detail["reason"] == "approval_mode_disabled"
        assert not (workspace_root / "assistant-a" / "persistent" / "keep.pdf").exists()
    finally:
        get_settings.cache_clear()
