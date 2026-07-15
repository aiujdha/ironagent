"""Persistence for external push requests and staged payloads."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.shared.async_utils import run_sync
from src.shared.database import get_session_factory
from src.shared.json_store import read_json_object, write_json_object
from src.workspace_files.database_models import ExternalPushRequestRow
from src.workspace_files.models import ExternalPushRequest


class PushRequestRepository:
    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path

    def load_push_requests(self) -> list[ExternalPushRequest]:
        payload = read_json_object(self.store_path, None)
        if payload is None:
            return []
        items = payload.get("push_requests", []) if isinstance(payload, dict) else []
        return [ExternalPushRequest.model_validate(item) for item in items]

    def write_push_requests(self, push_requests: list[ExternalPushRequest]) -> None:
        write_json_object(
            self.store_path,
            {"push_requests": [item.model_dump(mode="json") for item in push_requests]},
        )

    def append_push_request(self, push_request: ExternalPushRequest) -> None:
        requests = self.load_push_requests()
        requests.append(push_request)
        self.write_push_requests(requests)

    def list_push_requests(
        self,
        *,
        assistant_id: str | None = None,
        limit: int = 100,
    ) -> list[ExternalPushRequest]:
        requests = self.load_push_requests()
        if assistant_id:
            requests = [item for item in requests if item.assistant_id == assistant_id]
        return list(reversed(requests[-limit:]))

    def find_by_approval(
        self,
        push_requests: list[ExternalPushRequest],
        approval_id: str,
    ) -> ExternalPushRequest | None:
        return next(
            (item for item in reversed(push_requests) if item.approval_id == approval_id),
            None,
        )

    def pending_payload_dir(self) -> Path:
        return self.store_path.parent / "pending_payloads"

    def stage_pending_payload(self, *, content: bytes, filename: str) -> str:
        suffix = Path(filename).suffix
        staged_path = self.pending_payload_dir() / f"{uuid4()}{suffix}"
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        staged_path.write_bytes(content)
        return str(staged_path)

    def cleanup_staged_payload(self, staged_path: str | None) -> None:
        if not staged_path:
            return
        path = Path(staged_path)
        if path.exists():
            path.unlink()


class DatabasePushRequestRepository:
    def __init__(
        self,
        store_path: Path,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.store_path = store_path
        self.session_factory = session_factory or get_session_factory()

    def load_push_requests(self) -> list[ExternalPushRequest]:
        return run_sync(self.aload_push_requests())

    async def aload_push_requests(self) -> list[ExternalPushRequest]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ExternalPushRequestRow).order_by(
                    ExternalPushRequestRow.created_at,
                    ExternalPushRequestRow.push_id,
            )
                )
            rows = result.scalars().all()
            return [_push_request_from_row(row) for row in rows]

    def write_push_requests(self, push_requests: list[ExternalPushRequest]) -> None:
        run_sync(self.awrite_push_requests(push_requests))

    async def awrite_push_requests(self, push_requests: list[ExternalPushRequest]) -> None:
        async with self.session_factory() as session:
            for item in push_requests:
                row = await session.get(ExternalPushRequestRow, item.push_id)
                if row is None:
                    row = ExternalPushRequestRow(push_id=item.push_id)
                    session.add(row)
                _apply_push_request_to_row(row, item)
            await session.commit()

    def append_push_request(self, push_request: ExternalPushRequest) -> None:
        run_sync(self.aappend_push_request(push_request))

    async def aappend_push_request(self, push_request: ExternalPushRequest) -> None:
        async with self.session_factory() as session:
            row = ExternalPushRequestRow(push_id=push_request.push_id)
            _apply_push_request_to_row(row, push_request)
            session.add(row)
            await session.commit()

    def list_push_requests(
        self,
        *,
        assistant_id: str | None = None,
        limit: int = 100,
    ) -> list[ExternalPushRequest]:
        return run_sync(self.alist_push_requests(assistant_id=assistant_id, limit=limit))

    async def alist_push_requests(
        self,
        *,
        assistant_id: str | None = None,
        limit: int = 100,
    ) -> list[ExternalPushRequest]:
        stmt = select(ExternalPushRequestRow)
        if assistant_id:
            stmt = stmt.where(ExternalPushRequestRow.assistant_id == assistant_id)
        stmt = stmt.order_by(
            desc(ExternalPushRequestRow.created_at),
            desc(ExternalPushRequestRow.push_id),
        ).limit(limit)
        async with self.session_factory() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [_push_request_from_row(row) for row in rows]

    def find_by_approval(
        self,
        push_requests: list[ExternalPushRequest],
        approval_id: str,
    ) -> ExternalPushRequest | None:
        return next(
            (item for item in reversed(push_requests) if item.approval_id == approval_id),
            None,
        )

    def pending_payload_dir(self) -> Path:
        return self.store_path.parent / "pending_payloads"

    def stage_pending_payload(self, *, content: bytes, filename: str) -> str:
        suffix = Path(filename).suffix
        staged_path = self.pending_payload_dir() / f"{uuid4()}{suffix}"
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        staged_path.write_bytes(content)
        return str(staged_path)

    def cleanup_staged_payload(self, staged_path: str | None) -> None:
        if not staged_path:
            return
        path = Path(staged_path)
        if path.exists():
            path.unlink()


PushRequestRepositoryLike = PushRequestRepository | DatabasePushRequestRepository


def _push_request_from_row(row: ExternalPushRequestRow) -> ExternalPushRequest:
    return ExternalPushRequest.model_validate(
        {
            "push_id": row.push_id,
            "user_id": row.user_id,
            "assistant_id": row.assistant_id,
            "filename": row.filename,
            "bucket": row.bucket,
            "content_type": row.content_type,
            "source_type": row.source_type,
            "source_ref": row.source_ref,
            "status": row.status,
            "approval_id": row.approval_id,
            "staged_path": row.staged_path,
            "error": row.error,
            "created_at": row.created_at,
        }
    )


def _apply_push_request_to_row(row: ExternalPushRequestRow, item: ExternalPushRequest) -> None:
    row.user_id = item.user_id
    row.assistant_id = item.assistant_id
    row.filename = item.filename
    row.bucket = item.bucket
    row.content_type = item.content_type
    row.source_type = item.source_type
    row.source_ref = item.source_ref
    row.status = item.status
    row.approval_id = item.approval_id
    row.staged_path = item.staged_path
    row.error = item.error
    row.created_at = item.created_at
