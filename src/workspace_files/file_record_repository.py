"""Persistence for workspace file records."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.shared.async_utils import run_sync
from src.shared.database import get_session_factory
from src.shared.json_store import read_json_object, write_json_object
from src.workspace_files.database_models import WorkspaceFileRow, workspace_file_active_path_key
from src.workspace_files.models import TargetBucket, WorkspaceFile


class FileRecordRepository:
    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path

    def load_files(self) -> list[WorkspaceFile]:
        payload = read_json_object(self.store_path, None)
        if payload is None:
            return []
        items = payload.get("files", []) if isinstance(payload, dict) else []
        return [WorkspaceFile.model_validate(item) for item in items]

    def write_files(self, files: list[WorkspaceFile]) -> None:
        write_json_object(
            self.store_path,
            {"files": [item.model_dump(mode="json") for item in files]},
        )

    def list_files(
        self,
        assistant_id: str,
        *,
        bucket: TargetBucket | None = None,
        include_deleted: bool = False,
    ) -> list[WorkspaceFile]:
        files = [item for item in self.load_files() if item.assistant_id == assistant_id]
        if bucket is not None:
            files = [item for item in files if item.bucket == bucket]
        if not include_deleted:
            files = [item for item in files if not item.deleted]
        return files

    def get_file(self, file_id: str) -> WorkspaceFile | None:
        return next((item for item in self.load_files() if item.file_id == file_id), None)


class DatabaseFileRecordRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.session_factory = session_factory or get_session_factory()

    def load_files(self) -> list[WorkspaceFile]:
        return run_sync(self.aload_files())

    async def aload_files(self) -> list[WorkspaceFile]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(WorkspaceFileRow).order_by(WorkspaceFileRow.created_at, WorkspaceFileRow.file_id)
            )
            rows = result.scalars().all()
            return [_workspace_file_from_row(row) for row in rows]

    def write_files(self, files: list[WorkspaceFile]) -> None:
        run_sync(self.awrite_files(files))

    async def awrite_files(self, files: list[WorkspaceFile]) -> None:
        async with self.session_factory() as session:
            for item in files:
                row = await session.get(WorkspaceFileRow, item.file_id)
                if row is None:
                    row = WorkspaceFileRow(file_id=item.file_id)
                    session.add(row)
                _apply_workspace_file_to_row(row, item)
            await session.commit()

    def list_files(
        self,
        assistant_id: str,
        *,
        bucket: TargetBucket | None = None,
        include_deleted: bool = False,
    ) -> list[WorkspaceFile]:
        return run_sync(
            self.alist_files(
                assistant_id,
                bucket=bucket,
                include_deleted=include_deleted,
        )
            )

    async def alist_files(
        self,
        assistant_id: str,
        *,
        bucket: TargetBucket | None = None,
        include_deleted: bool = False,
    ) -> list[WorkspaceFile]:
        stmt = select(WorkspaceFileRow).where(WorkspaceFileRow.assistant_id == assistant_id)
        if bucket is not None:
            stmt = stmt.where(WorkspaceFileRow.bucket == bucket)
        if not include_deleted:
            stmt = stmt.where(WorkspaceFileRow.deleted.is_(False))
        stmt = stmt.order_by(WorkspaceFileRow.created_at, WorkspaceFileRow.file_id)
        async with self.session_factory() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [_workspace_file_from_row(row) for row in rows]

    def get_file(self, file_id: str) -> WorkspaceFile | None:
        return run_sync(self.aget_file(file_id))

    async def aget_file(self, file_id: str) -> WorkspaceFile | None:
        trimmed = file_id.strip()
        if not trimmed:
            return None
        async with self.session_factory() as session:
            row = await session.get(WorkspaceFileRow, trimmed)
            return _workspace_file_from_row(row) if row is not None else None


FileRecordRepositoryLike = FileRecordRepository | DatabaseFileRecordRepository


def _workspace_file_from_row(row: WorkspaceFileRow) -> WorkspaceFile:
    return WorkspaceFile.model_validate(
        {
            "file_id": row.file_id,
            "assistant_id": row.assistant_id,
            "bucket": row.bucket,
            "filename": row.filename,
            "relative_path": row.relative_path,
            "file_role": row.file_role,
            "source_file_id": row.source_file_id,
            "owner_user_id": row.owner_user_id,
            "size_bytes": row.size_bytes,
            "content_type": row.content_type,
            "uploader_user_id": row.uploader_user_id,
            "source_type": row.source_type,
            "source_ref": row.source_ref,
            "storage_provider": row.storage_provider,
            "storage_key": row.storage_key,
            "storage_path": row.storage_path,
            "storage_status": row.storage_status,
            "storage_error": row.storage_error,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "deleted": row.deleted,
        }
    )


def _apply_workspace_file_to_row(row: WorkspaceFileRow, item: WorkspaceFile) -> None:
    row.assistant_id = item.assistant_id
    row.bucket = cast(str, item.bucket)
    row.filename = item.filename
    row.relative_path = item.relative_path
    row.file_role = cast(str, item.file_role)
    row.source_file_id = item.source_file_id
    row.owner_user_id = item.owner_user_id
    row.size_bytes = item.size_bytes
    row.content_type = item.content_type
    row.uploader_user_id = item.uploader_user_id
    row.source_type = item.source_type
    row.source_ref = item.source_ref
    row.active_path_key = workspace_file_active_path_key(
        item.assistant_id,
        str(item.bucket),
        item.filename,
        deleted=item.deleted,
    )
    row.storage_provider = item.storage_provider
    row.storage_key = item.storage_key
    row.storage_path = item.storage_path
    row.storage_status = cast(str, item.storage_status)
    row.storage_error = item.storage_error
    row.created_at = item.created_at
    row.updated_at = item.updated_at
    row.deleted = item.deleted
