"""Scan assistant workspaces for result files not yet recorded."""

from __future__ import annotations

from pathlib import Path

from src.workspace_files.file_record_repository import FileRecordRepository
from src.workspace_files.models import TargetBucket, WorkspaceFile
from src.workspace_files.workspace_writer import WorkspaceWriter


class WorkspaceResultScanner:
    def __init__(
        self,
        *,
        file_records: FileRecordRepository,
        workspace_writer: WorkspaceWriter,
    ) -> None:
        self.file_records = file_records
        self.workspace_writer = workspace_writer

    def discover_result_files(self, assistant_id: str) -> list[tuple[TargetBucket, Path, str]]:
        known = {
            item.relative_path
            for item in self.file_records.list_files(assistant_id, include_deleted=True)
        }
        discovered: list[tuple[TargetBucket, Path, str]] = []
        for bucket in ("temp", "persistent"):
            bucket_dir = self.workspace_writer.bucket_dir(assistant_id, bucket)
            if not bucket_dir.exists():
                continue
            for path in sorted(candidate for candidate in bucket_dir.rglob("*") if candidate.is_file()):
                relative = f"{bucket}/{path.relative_to(bucket_dir).as_posix()}"
                if relative in known:
                    continue
                discovered.append((bucket, path, relative))
        return discovered
