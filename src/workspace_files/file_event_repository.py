"""Persistence for workspace file events."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.shared.async_utils import run_sync
from src.shared.database import get_session_factory
from src.shared.json_store import read_json_object, write_json_object
from src.workspace_files.database_models import WorkspaceFileEventRow
from src.workspace_files.models import WorkspaceFileEvent


class FileEventRepository:
    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path

    def load_events(self) -> list[WorkspaceFileEvent]:
        payload = read_json_object(self.store_path, None)
        if payload is None:
            return []
        items = payload.get("events", []) if isinstance(payload, dict) else []
        return [WorkspaceFileEvent.model_validate(item) for item in items]

    def append_event(self, event: WorkspaceFileEvent) -> None:
        events = self.load_events()
        events.append(event)
        write_json_object(
            self.store_path,
            {"events": [item.model_dump(mode="json") for item in events]},
        )

    def list_events(
        self,
        *,
        assistant_id: str | None = None,
        file_id: str | None = None,
        limit: int = 100,
    ) -> list[WorkspaceFileEvent]:
        events = self.load_events()
        if assistant_id:
            events = [item for item in events if item.assistant_id == assistant_id]
        if file_id:
            events = [item for item in events if item.file_id == file_id]
        return list(reversed(events[-limit:]))


class DatabaseFileEventRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.session_factory = session_factory or get_session_factory()

    def load_events(self) -> list[WorkspaceFileEvent]:
        return run_sync(self.aload_events())

    async def aload_events(self) -> list[WorkspaceFileEvent]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(WorkspaceFileEventRow).order_by(
                    WorkspaceFileEventRow.created_at,
                    WorkspaceFileEventRow.event_id,
            )
                )
            rows = result.scalars().all()
            return [_workspace_file_event_from_row(row) for row in rows]

    def append_event(self, event: WorkspaceFileEvent) -> None:
        run_sync(self.aappend_event(event))

    async def aappend_event(self, event: WorkspaceFileEvent) -> None:
        async with self.session_factory() as session:
            session.add(_workspace_file_event_row(event))
            await session.commit()

    def list_events(
        self,
        *,
        assistant_id: str | None = None,
        file_id: str | None = None,
        limit: int = 100,
    ) -> list[WorkspaceFileEvent]:
        return run_sync(
            self.alist_events(
                assistant_id=assistant_id,
                file_id=file_id,
                limit=limit,
        )
            )

    async def alist_events(
        self,
        *,
        assistant_id: str | None = None,
        file_id: str | None = None,
        limit: int = 100,
    ) -> list[WorkspaceFileEvent]:
        stmt = select(WorkspaceFileEventRow)
        if assistant_id:
            stmt = stmt.where(WorkspaceFileEventRow.assistant_id == assistant_id)
        if file_id:
            stmt = stmt.where(WorkspaceFileEventRow.file_id == file_id)
        stmt = stmt.order_by(
            desc(WorkspaceFileEventRow.created_at),
            desc(WorkspaceFileEventRow.event_id),
        ).limit(limit)
        async with self.session_factory() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [_workspace_file_event_from_row(row) for row in rows]


FileEventRepositoryLike = FileEventRepository | DatabaseFileEventRepository


def _workspace_file_event_from_row(row: WorkspaceFileEventRow) -> WorkspaceFileEvent:
    return WorkspaceFileEvent.model_validate(
        {
            "event_id": row.event_id,
            "file_id": row.file_id,
            "assistant_id": row.assistant_id,
            "action": row.action,
            "user_id": row.user_id,
            "source_type": row.source_type,
            "source_ref": row.source_ref,
            "detail": row.detail,
            "created_at": row.created_at,
        }
    )


def _workspace_file_event_row(event: WorkspaceFileEvent) -> WorkspaceFileEventRow:
    return WorkspaceFileEventRow(
        event_id=event.event_id,
        file_id=event.file_id,
        assistant_id=event.assistant_id,
        action=cast(str, event.action),
        user_id=event.user_id,
        source_type=event.source_type,
        source_ref=event.source_ref,
        detail=event.detail,
        created_at=event.created_at,
    )
