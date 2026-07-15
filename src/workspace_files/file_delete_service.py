"""Delete workflow for workspace files."""

from __future__ import annotations

from src.approval.service import ApprovalService
from src.shared.config import approval_mode_disabled
from src.shared.logging import get_logger
from src.workspace_files.file_event_repository import FileEventRepository
from src.workspace_files.file_record_repository import FileRecordRepository
from src.workspace_files.models import (
    ExternalPushRequest,
    WorkspaceFile,
    WorkspaceFileEvent,
    utc_now,
)
from src.workspace_files.push_request_repository import PushRequestRepository
from src.workspace_files.workspace_writer import WorkspaceWriter

log = get_logger(__name__)


class FileDeleteService:
    def __init__(
        self,
        *,
        file_records: FileRecordRepository,
        file_events: FileEventRepository,
        push_requests: PushRequestRepository,
        workspace_writer: WorkspaceWriter,
        approval_service: ApprovalService,
    ) -> None:
        self.file_records = file_records
        self.file_events = file_events
        self.push_requests = push_requests
        self.workspace_writer = workspace_writer
        self.approval_service = approval_service

    def delete_file(
        self,
        file_id: str,
        *,
        user_id: str = "",
    ) -> WorkspaceFile | ExternalPushRequest | None:
        files = self.file_records.load_files()
        record = next(
            (item for item in files if item.file_id == file_id and not item.deleted),
            None,
        )
        if record is None:
            return None
        if record.bucket == "persistent":
            if approval_mode_disabled():
                return self.finalize_delete(
                    files,
                    record,
                    user_id=user_id,
                    reason="approval_mode_disabled",
                )
            approval = self.approval_service.create_request(
                "workspace_file_delete",
                assistant_id=record.assistant_id,
                approval_scope="user",
                reason=f"Delete persistent file: {record.filename}",
                target=record.filename,
            )
            push_request = ExternalPushRequest(
                user_id=user_id,
                assistant_id=record.assistant_id,
                filename=record.filename,
                bucket=record.bucket,
                status="pending_approval",
                approval_id=approval.id,
            )
            self.push_requests.append_push_request(push_request)
            log.info(
                "file_delete_pending_approval",
                file_id=file_id,
                approval_id=approval.id,
            )
            return push_request
        return self.finalize_delete(files, record, user_id=user_id)

    def finalize_delete(
        self,
        files: list[WorkspaceFile],
        record: WorkspaceFile,
        *,
        user_id: str = "",
        approval_id: str | None = None,
        reason: str = "",
    ) -> WorkspaceFile:
        record.deleted = True
        record.updated_at = utc_now()
        self.file_records.write_files(files)
        self.workspace_writer.delete_file(record)

        detail = {"filename": record.filename, "bucket": record.bucket}
        if approval_id:
            detail["approval_id"] = approval_id
        if reason:
            detail["reason"] = reason

        self.file_events.append_event(
            WorkspaceFileEvent(
                file_id=record.file_id,
                assistant_id=record.assistant_id,
                action="delete",
                user_id=user_id,
                detail=detail,
        )
            )
        log.info(
            "file_deleted",
            file_id=record.file_id,
            filename=record.filename,
            approval_id=approval_id,
        )
        return record
