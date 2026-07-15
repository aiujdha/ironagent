"""Ingest result files discovered in assistant workspaces."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from src.workspace_files.file_event_repository import FileEventRepository
from src.workspace_files.file_record_repository import FileRecordRepository
from src.workspace_files.models import TargetBucket, WorkspaceFile, WorkspaceFileEvent, utc_now
from src.workspace_files.object_storage_sync import ObjectStorageSyncService


class ResultIngestService:
    def __init__(
        self,
        *,
        file_records: FileRecordRepository,
        file_events: FileEventRepository,
        storage_sync: ObjectStorageSyncService,
    ) -> None:
        self.file_records = file_records
        self.file_events = file_events
        self.storage_sync = storage_sync

    def ingest_result_file(
        self,
        *,
        assistant_id: str,
        bucket: TargetBucket,
        path: Path,
        relative_path: str,
        source_file_id: str | None = None,
    ) -> WorkspaceFile:
        files = self.file_records.load_files()
        now = utc_now()
        record = WorkspaceFile(
            assistant_id=assistant_id,
            bucket=bucket,
            filename=path.name,
            relative_path=relative_path,
            file_role="result",
            source_file_id=source_file_id,
            size_bytes=path.stat().st_size,
            content_type=mimetypes.guess_type(path.name)[0] or "",
            source_type="workspace_scan",
            source_ref=relative_path,
            created_at=now,
            updated_at=now,
        )
        files.append(record)
        self.file_records.write_files(files)
        synced = self.storage_sync.sync_record(record, files=files)
        self.file_events.append_event(
            WorkspaceFileEvent(
                file_id=synced.file_id,
                assistant_id=synced.assistant_id,
                action="ingest",
                source_type="workspace_scan",
                source_ref=relative_path,
                detail={
                    "bucket": bucket,
                    "relative_path": relative_path,
                    "storage_status": synced.storage_status,
                },
        )
            )
        return synced
