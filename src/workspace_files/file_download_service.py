"""Download workflow for workspace files."""

from __future__ import annotations

from src.workspace_files.file_event_repository import FileEventRepository
from src.workspace_files.file_record_repository import FileRecordRepository
from src.workspace_files.models import WorkspaceFile, WorkspaceFileEvent
from src.workspace_files.object_storage_gateway import ObjectStorageGateway
from src.workspace_files.workspace_writer import WorkspaceWriter


class FileDownloadService:
    def __init__(
        self,
        *,
        file_records: FileRecordRepository,
        file_events: FileEventRepository,
        workspace_writer: WorkspaceWriter,
        storage_gateway: ObjectStorageGateway,
    ) -> None:
        self.file_records = file_records
        self.file_events = file_events
        self.workspace_writer = workspace_writer
        self.storage_gateway = storage_gateway

    def download_file(self, file_id: str) -> tuple[WorkspaceFile, bytes] | None:
        record = self.file_records.get_file(file_id)
        if record is None or record.deleted:
            return None

        content = self.workspace_writer.read_file(record)
        if content is None:
            return None
        self.file_events.append_event(
            WorkspaceFileEvent(
                file_id=record.file_id,
                assistant_id=record.assistant_id,
                action="download",
                detail={
                    "filename": record.filename,
                    "bucket": record.bucket,
                    "source": "workspace",
                },
        )
            )
        return record, content
