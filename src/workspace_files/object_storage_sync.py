"""Business-level sync from workspace files to object storage."""

from __future__ import annotations

from src.workspace_files.file_record_repository import FileRecordRepository
from src.workspace_files.models import WorkspaceFile, utc_now
from src.workspace_files.object_storage_gateway import ObjectStorageGateway
from src.workspace_files.workspace_writer import WorkspaceWriter


class ObjectStorageSyncService:
    def __init__(
        self,
        *,
        gateway: ObjectStorageGateway,
        file_records: FileRecordRepository,
        workspace_writer: WorkspaceWriter,
    ) -> None:
        self.gateway = gateway
        self.file_records = file_records
        self.workspace_writer = workspace_writer

    def sync_record(self, record: WorkspaceFile, *, files: list[WorkspaceFile] | None = None) -> WorkspaceFile:
        effective_files = files or self.file_records.load_files()
        target = next((item for item in effective_files if item.file_id == record.file_id), record)

        if not self.gateway.is_enabled():
            target.storage_provider = self.gateway.provider
            target.storage_status = "disabled"
            target.storage_error = "object storage is not configured"
            target.updated_at = utc_now()
            self.file_records.write_files(effective_files)
            return target

        path = self.workspace_writer.bucket_dir(target.assistant_id, target.bucket) / target.filename
        if not path.exists():
            target.storage_provider = self.gateway.provider
            target.storage_status = "failed"
            target.storage_error = "workspace file is missing"
            target.updated_at = utc_now()
            self.file_records.write_files(effective_files)
            return target

        key = self.gateway.build_key(
            assistant_id=target.assistant_id,
            relative_path=target.relative_path,
        )
        ok, detail = self.gateway.upload_file(local_path=path, key=key)
        target.storage_provider = self.gateway.provider
        target.storage_key = key
        target.storage_path = detail if ok else ""
        target.storage_status = "synced" if ok else "failed"
        target.storage_error = "" if ok else detail
        target.updated_at = utc_now()
        self.file_records.write_files(effective_files)
        return target
