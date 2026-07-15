"""Apply approval outcomes for workspace file operations."""

from __future__ import annotations

from pathlib import Path

from src.approval.service import ApprovalService
from src.shared.logging import get_logger
from src.workspace_files.file_delete_service import FileDeleteService
from src.workspace_files.file_event_repository import FileEventRepository
from src.workspace_files.file_record_repository import FileRecordRepository
from src.workspace_files.models import WorkspaceFile, WorkspaceFileEvent, utc_now
from src.workspace_files.object_storage_sync import ObjectStorageSyncService
from src.workspace_files.push_request_repository import PushRequestRepository
from src.workspace_files.workspace_writer import WorkspaceWriter

log = get_logger(__name__)


class FileApprovalService:
    def __init__(
        self,
        *,
        file_records: FileRecordRepository,
        file_events: FileEventRepository,
        push_requests: PushRequestRepository,
        workspace_writer: WorkspaceWriter,
        approval_service: ApprovalService,
        file_delete_service: FileDeleteService,
        storage_sync: ObjectStorageSyncService,
    ) -> None:
        self.file_records = file_records
        self.file_events = file_events
        self.push_requests = push_requests
        self.workspace_writer = workspace_writer
        self.approval_service = approval_service
        self.file_delete_service = file_delete_service
        self.storage_sync = storage_sync

    def apply_approval_resolution(self, approval_id: str) -> WorkspaceFile | None:
        approval = self.approval_service.get_request(approval_id)
        if approval is None:
            return None
        if approval.tool_name == "workspace_file_delete":
            return self._apply_delete_approval_resolution(approval_id)
        if approval.tool_name == "workspace_file_overwrite":
            return self._apply_overwrite_approval_resolution(approval_id)
        return None

    def _reject_push_request(
        self,
        *,
        push_requests: list,
        push_request,
        error: str = "",
    ) -> None:
        push_request.status = "rejected"
        if error:
            push_request.error = error
        self.push_requests.cleanup_staged_payload(push_request.staged_path)
        push_request.staged_path = None
        self.push_requests.write_push_requests(push_requests)

    def _apply_delete_approval_resolution(self, approval_id: str) -> WorkspaceFile | None:
        push_requests = self.push_requests.load_push_requests()
        push_request = self.push_requests.find_by_approval(push_requests, approval_id)
        if push_request is None:
            return None

        approval = self.approval_service.get_request(approval_id)
        if approval is None:
            return None

        if approval.resolution == "deny":
            if push_request.status == "pending_approval":
                self._reject_push_request(push_requests=push_requests, push_request=push_request)
            log.info(
                "file_delete_approval_rejected",
                approval_id=approval_id,
                assistant_id=push_request.assistant_id,
                filename=push_request.filename,
            )
            return None

        if approval.resolution != "allow":
            return None

        if push_request.status != "pending_approval":
            files = self.file_records.load_files()
            return next(
                (
                    item
                    for item in files
                    if item.assistant_id == push_request.assistant_id
                    and item.filename == push_request.filename
                    and item.bucket == push_request.bucket
                    and item.deleted
                ),
                None,
            )

        files = self.file_records.load_files()
        record = next(
            (
                item
                for item in files
                if item.assistant_id == push_request.assistant_id
                and item.filename == push_request.filename
                and item.bucket == push_request.bucket
                and not item.deleted
            ),
            None,
        )
        if record is None:
            push_request.status = "accepted"
            self.push_requests.write_push_requests(push_requests)
            return None

        deleted_record = self.file_delete_service.finalize_delete(
            files,
            record,
            user_id=push_request.user_id,
            approval_id=approval_id,
        )
        push_request.status = "accepted"
        self.push_requests.write_push_requests(push_requests)
        return deleted_record

    def _apply_overwrite_approval_resolution(self, approval_id: str) -> WorkspaceFile | None:
        push_requests = self.push_requests.load_push_requests()
        push_request = self.push_requests.find_by_approval(push_requests, approval_id)
        if push_request is None:
            return None

        approval = self.approval_service.get_request(approval_id)
        if approval is None:
            return None

        if approval.resolution == "deny":
            if push_request.status == "pending_approval":
                self._reject_push_request(push_requests=push_requests, push_request=push_request)
            log.info(
                "file_overwrite_approval_rejected",
                approval_id=approval_id,
                assistant_id=push_request.assistant_id,
                filename=push_request.filename,
            )
            return None

        if approval.resolution != "allow":
            return None

        if push_request.status != "pending_approval":
            files = self.file_records.load_files()
            return next(
                (
                    item
                    for item in files
                    if item.assistant_id == push_request.assistant_id
                    and item.filename == push_request.filename
                    and item.bucket == push_request.bucket
                    and not item.deleted
                ),
                None,
            )

        staged_path = Path(push_request.staged_path) if push_request.staged_path else None
        if staged_path is None or not staged_path.exists():
            self._reject_push_request(
                push_requests=push_requests,
                push_request=push_request,
                error="staged overwrite payload is missing",
            )
            return None

        files = self.file_records.load_files()
        record = next(
            (
                item
                for item in files
                if item.assistant_id == push_request.assistant_id
                and item.filename == push_request.filename
                and item.bucket == push_request.bucket
                and not item.deleted
            ),
            None,
        )
        if record is None:
            self._reject_push_request(
                push_requests=push_requests,
                push_request=push_request,
                error="target file is missing at approval time",
            )
            return None

        content = staged_path.read_bytes()
        self.workspace_writer.write_file(
            assistant_id=record.assistant_id,
            bucket=record.bucket,
            filename=record.filename,
            content=content,
        )
        record.size_bytes = len(content)
        record.content_type = push_request.content_type
        record.owner_user_id = record.owner_user_id or push_request.user_id
        record.uploader_user_id = push_request.user_id
        record.source_type = push_request.source_type
        record.source_ref = push_request.source_ref
        record.file_role = "input"
        record.updated_at = utc_now()
        self.file_records.write_files(files)
        record = self.storage_sync.sync_record(record, files=files)

        self.file_events.append_event(
            WorkspaceFileEvent(
                file_id=record.file_id,
                assistant_id=record.assistant_id,
                action="overwrite",
                user_id=push_request.user_id,
                source_type=push_request.source_type,
                source_ref=push_request.source_ref,
                detail={
                    "filename": record.filename,
                    "bucket": record.bucket,
                    "size": len(content),
                    "approval_id": approval_id,
                    "storage_status": record.storage_status,
                },
        )
            )
        self.push_requests.cleanup_staged_payload(push_request.staged_path)
        push_request.staged_path = None
        push_request.status = "accepted"
        push_request.error = ""
        self.push_requests.write_push_requests(push_requests)
        log.info(
            "file_overwrite_approved",
            approval_id=approval_id,
            assistant_id=record.assistant_id,
            filename=record.filename,
            size=len(content),
            storage_status=record.storage_status,
        )
        return record
