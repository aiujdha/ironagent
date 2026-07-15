"""Workspace-file object storage integration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from src.shared.object_storage import (
    delete_file_from_object_storage,
    download_bytes_from_object_storage,
    is_object_storage_enabled,
    upload_bytes_to_object_storage,
    upload_file_to_object_storage,
)
from src.workspace_files.models import FileStorageBackend, TargetBucket


class ObjectStorageError(RuntimeError):
    """Raised when object storage sync fails."""


@dataclass(frozen=True, slots=True)
class StoredWorkspaceObject:
    storage_backend: FileStorageBackend = "disk"
    object_key: str = ""
    object_url: str = ""


class WorkspaceObjectStore(Protocol):
    def is_enabled(self) -> bool: ...

    def upload_bytes(
        self,
        *,
        assistant_id: str,
        bucket: TargetBucket,
        filename: str,
        content: bytes,
        content_type: str = "",
    ) -> StoredWorkspaceObject: ...

    def upload_file(
        self,
        *,
        assistant_id: str,
        bucket: TargetBucket,
        filename: str,
        local_path: Path,
        content_type: str = "",
    ) -> StoredWorkspaceObject: ...

    def download_bytes(self, object_key: str) -> bytes: ...

    def delete(self, object_key: str) -> None: ...


class DefaultWorkspaceObjectStore:
    def is_enabled(self) -> bool:
        return is_object_storage_enabled()

    def _build_key(self, assistant_id: str, bucket: TargetBucket, filename: str) -> str:
        trimmed_filename = filename.strip().lstrip("/")
        return f"workspace-files/{assistant_id}/{bucket}/{trimmed_filename}"

    def upload_bytes(
        self,
        *,
        assistant_id: str,
        bucket: TargetBucket,
        filename: str,
        content: bytes,
        content_type: str = "",
    ) -> StoredWorkspaceObject:
        if not self.is_enabled():
            return StoredWorkspaceObject()
        object_key = self._build_key(assistant_id, bucket, filename)
        success, result = upload_bytes_to_object_storage(
            content,
            object_key,
            content_type=content_type,
        )
        if not success:
            raise ObjectStorageError(f"object storage upload failed: {result}")
        return StoredWorkspaceObject(
            storage_backend="cos",
            object_key=object_key,
            object_url=result,
        )

    def upload_file(
        self,
        *,
        assistant_id: str,
        bucket: TargetBucket,
        filename: str,
        local_path: Path,
        content_type: str = "",
    ) -> StoredWorkspaceObject:
        if not self.is_enabled():
            return StoredWorkspaceObject()
        object_key = self._build_key(assistant_id, bucket, filename)
        success, result = upload_file_to_object_storage(
            local_path,
            object_key,
            content_type=content_type,
        )
        if not success:
            raise ObjectStorageError(f"object storage upload failed: {result}")
        return StoredWorkspaceObject(
            storage_backend="cos",
            object_key=object_key,
            object_url=result,
        )

    def download_bytes(self, object_key: str) -> bytes:
        success, content, detail = download_bytes_from_object_storage(object_key)
        if not success or content is None:
            raise ObjectStorageError(
                f"object storage download failed: {detail or 'empty response'}"
            )
        return content

    def delete(self, object_key: str) -> None:
        if not object_key.strip():
            return
        success, detail = delete_file_from_object_storage(object_key)
        if not success:
            raise ObjectStorageError(f"object storage delete failed: {detail}")


def build_workspace_object_store() -> WorkspaceObjectStore:
    return DefaultWorkspaceObjectStore()
