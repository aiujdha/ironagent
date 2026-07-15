"""Read and write files inside an assistant workspace."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from src.workspace_files.models import TargetBucket, WorkspaceFile

WorkspaceDirResolver = Callable[[str], Path]


class WorkspaceWriter:
    def __init__(self, workspace_dir_resolver: WorkspaceDirResolver) -> None:
        self.workspace_dir_resolver = workspace_dir_resolver

    def workspace_dir(self, assistant_id: str) -> Path:
        return self.workspace_dir_resolver(assistant_id)

    def bucket_dir(self, assistant_id: str, bucket: TargetBucket) -> Path:
        return self.workspace_dir(assistant_id) / bucket

    def write_file(
        self,
        *,
        assistant_id: str,
        bucket: TargetBucket,
        filename: str,
        content: bytes,
    ) -> Path:
        dest_path = self.bucket_dir(assistant_id, bucket) / filename
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(content)
        return dest_path

    def read_file(self, record: WorkspaceFile) -> bytes | None:
        path = self.bucket_dir(record.assistant_id, record.bucket) / record.filename
        if not path.exists():
            return None
        return path.read_bytes()

    def delete_file(self, record: WorkspaceFile) -> None:
        path = self.bucket_dir(record.assistant_id, record.bucket) / record.filename
        if path.exists():
            path.unlink()