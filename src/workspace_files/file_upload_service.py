"""Upload workflow for workspace files."""

from __future__ import annotations

from src.approval.service import ApprovalService
from src.shared.logging import get_logger
from src.workspace_files.assistant_resolver import AssistantResolver
from src.workspace_files.file_event_repository import FileEventRepository
from src.workspace_files.file_record_repository import FileRecordRepository
from src.workspace_files.models import (
    ExternalPushRequest,
    TargetBucket,
    WorkspaceFile,
    WorkspaceFileEvent,
    utc_now,
)
from src.workspace_files.object_storage_sync import ObjectStorageSyncService
from src.workspace_files.path_policy import validate_filename
from src.workspace_files.push_request_repository import PushRequestRepository
from src.workspace_files.workspace_writer import WorkspaceWriter

log = get_logger(__name__)


class FileUploadService:
    def __init__(
        self,
        *,
        file_records: FileRecordRepository,
        file_events: FileEventRepository,
        push_requests: PushRequestRepository,
        assistant_resolver: AssistantResolver,
        workspace_writer: WorkspaceWriter,
        approval_service: ApprovalService,
        storage_sync: ObjectStorageSyncService,
    ) -> None:
        self.file_records = file_records
        self.file_events = file_events
        self.push_requests = push_requests
        self.assistant_resolver = assistant_resolver
        self.workspace_writer = workspace_writer
        self.approval_service = approval_service
        self.storage_sync = storage_sync

    def upload_file(
        self,
        *,
        assistant_id: str | None = None,
        user_id: str | None = None,
        bucket: TargetBucket = "temp",
        filename: str,
        content: bytes,
        content_type: str = "",
        overwrite: bool = False,
        source_type: str = "",
        source_ref: str = "",
        owner_user_id: str = "",
        uploader_user_id: str = "",
    ) -> WorkspaceFile | ExternalPushRequest:
        resolved_id = self.assistant_resolver.resolve_assistant_id(
            assistant_id=assistant_id,
            user_id=user_id,
        )
        validate_filename(filename)
        self.assistant_resolver.require_assistant(resolved_id)

        files = self.file_records.load_files()
        existing = next(
            (
                item
                for item in files
                if item.assistant_id == resolved_id
                and item.filename == filename
                and item.bucket == bucket
                and not item.deleted
            ),
            None,
        )

        effective_user_id = uploader_user_id or (user_id or "")
        effective_owner_user_id = owner_user_id or (user_id or "")

        self.workspace_writer.write_file(
            assistant_id=resolved_id,
            bucket=bucket,
            filename=filename,
            content=content,
        )

        now = utc_now()
        if existing is not None:
            existing.size_bytes = len(content)
            existing.content_type = content_type
            existing.owner_user_id = effective_owner_user_id or existing.owner_user_id
            existing.uploader_user_id = effective_user_id
            existing.source_type = source_type
            existing.source_ref = source_ref
            existing.file_role = "input"
            existing.updated_at = now
            self.file_records.write_files(files)
            file_record = existing
        else:
            file_record = WorkspaceFile(
                assistant_id=resolved_id,
                bucket=bucket,
                filename=filename,
                relative_path=f"{bucket}/{filename}",
                file_role="input",
                size_bytes=len(content),
                content_type=content_type,
                owner_user_id=effective_owner_user_id,
                uploader_user_id=effective_user_id,
                source_type=source_type,
                source_ref=source_ref,
                created_at=now,
                updated_at=now,
            )
            files.append(file_record)
            self.file_records.write_files(files)

        file_record = self.storage_sync.sync_record(file_record, files=files)

        action = "overwrite" if existing is not None else "upload"
        self.file_events.append_event(
            WorkspaceFileEvent(
                file_id=file_record.file_id,
                assistant_id=resolved_id,
                action=action,
                user_id=effective_user_id,
                source_type=source_type,
                source_ref=source_ref,
                detail={
                    "filename": filename,
                    "bucket": bucket,
                    "size": len(content),
                    "storage_status": file_record.storage_status,
                },
        )
            )

        self.push_requests.append_push_request(
            ExternalPushRequest(
                user_id=effective_user_id,
                assistant_id=resolved_id,
                filename=filename,
                bucket=bucket,
                content_type=content_type,
                source_type=source_type,
                source_ref=source_ref,
                status="accepted",
        )
            )

        log.info(
            "file_uploaded",
            assistant_id=resolved_id,
            filename=filename,
            bucket=bucket,
            size=len(content),
            storage_status=file_record.storage_status,
        )
        return file_record
