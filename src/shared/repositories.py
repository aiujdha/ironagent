from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    case,
    delete,
    desc,
    func,
    literal,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from src.approval.models import ApprovalRequest
from src.assistants.models import AssistantRecord
from src.shared.async_utils import run_sync
from src.shared.config import StorageBackendName, get_settings
from src.shared.database import Base, get_session_factory
from src.shared.json_store import read_json_object, write_json_object
from src.workspace_files.database_models import workspace_file_active_path_key
from src.workspace_files.models import (
    ExternalPushRequest,
    FileDirection,
    WorkspaceFile,
    WorkspaceFileEvent,
)
from src.workspace_files.repository import (
    ExternalPushRequestRow,
    WorkspaceFileEventRow,
    WorkspaceFileRow,
)

_JSON_AUDIT_LOCKS_GUARD = threading.Lock()
_JSON_AUDIT_LOCKS: dict[Path, threading.RLock] = {}


def _json_audit_lock_for_path(path: Path) -> threading.RLock:
    key = path.expanduser().resolve()
    with _JSON_AUDIT_LOCKS_GUARD:
        lock = _JSON_AUDIT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _JSON_AUDIT_LOCKS[key] = lock
        return lock


def utc_now() -> datetime:
    return datetime.now(UTC)


class AssistantRepository(Protocol):
    def is_initialized(self) -> bool: ...

    async def alist_assistants(self) -> list[AssistantRecord]: ...

    def list_assistants(self) -> list[AssistantRecord]: ...

    async def aget_assistant(self, assistant_id: str) -> AssistantRecord | None: ...

    def get_assistant(self, assistant_id: str) -> AssistantRecord | None: ...

    async def aupsert_assistant(self, record: AssistantRecord) -> AssistantRecord: ...

    def upsert_assistant(self, record: AssistantRecord) -> AssistantRecord: ...

    async def adelete_assistant(self, assistant_id: str) -> bool: ...

    def delete_assistant(self, assistant_id: str) -> bool: ...


class ApprovalRepository(Protocol):
    async def alist_requests(
        self,
        *,
        approval_scope: Literal["user", "admin"] | None = None,
        status: str | None = None,
        assistant_id: str | None = None,
    ) -> list[ApprovalRequest]: ...

    def list_requests(
        self,
        *,
        approval_scope: Literal["user", "admin"] | None = None,
        status: str | None = None,
        assistant_id: str | None = None,
    ) -> list[ApprovalRequest]: ...

    async def aget_request(self, approval_id: str) -> ApprovalRequest | None: ...

    def get_request(self, approval_id: str) -> ApprovalRequest | None: ...

    async def acreate_request(self, item: ApprovalRequest) -> ApprovalRequest: ...

    def create_request(self, item: ApprovalRequest) -> ApprovalRequest: ...

    async def asave_request(self, item: ApprovalRequest) -> ApprovalRequest: ...

    def save_request(self, item: ApprovalRequest) -> ApprovalRequest: ...


class AuditRepository(Protocol):
    async def aappend_event(self, payload: dict[str, Any]) -> None: ...

    def append_event(self, payload: dict[str, Any]) -> None: ...

    async def acount_events(self) -> int: ...

    def count_events(self) -> int: ...

    async def alist_recent(self, *, limit: int) -> list[dict[str, Any]]: ...

    def list_recent(self, *, limit: int) -> list[dict[str, Any]]: ...

    async def alist_page(
        self,
        *,
        limit: int,
        offset: int,
        filters: AuditLogFilters | None = None,
    ) -> AuditLogPage: ...

    def list_page(
        self,
        *,
        limit: int,
        offset: int,
        filters: AuditLogFilters | None = None,
    ) -> AuditLogPage: ...

    async def acleanup_expired(
        self,
        *,
        retention_days: int,
        archive_path: Path | None = None,
    ) -> AuditRetentionResult: ...

    def cleanup_expired(
        self,
        *,
        retention_days: int,
        archive_path: Path | None = None,
    ) -> AuditRetentionResult: ...


@dataclass(slots=True)
class AuditLogPage:
    items: list[dict[str, Any]]
    total: int


@dataclass(slots=True)
class AuditLogFilters:
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
    user_id: str | None = None
    assistant_id: str | None = None
    operation_type: str | None = None
    result: str | None = None
    error_code: str | None = None


def _json_payload_text(*keys: str) -> Any:
    expr: Any = AuditEventRow.payload
    for key in keys:
        expr = expr[key]
    return expr.as_string()


def _nullif_empty(expr: Any) -> Any:
    return func.nullif(expr, "")


def _lower_text(expr: Any) -> Any:
    return func.lower(func.coalesce(expr, ""))


def _first_non_empty(*exprs: Any) -> Any:
    return func.coalesce(*[_nullif_empty(expr) for expr in exprs], "")


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _audit_sql_filter_clauses(filters: AuditLogFilters | None) -> list[Any]:
    if filters is None:
        return []

    clauses: list[Any] = []
    if filters.occurred_from is not None:
        clauses.append(AuditEventRow.occurred_at >= _ensure_aware_utc(filters.occurred_from))
    if filters.occurred_to is not None:
        clauses.append(AuditEventRow.occurred_at <= _ensure_aware_utc(filters.occurred_to))
    if assistant_id := (filters.assistant_id or "").strip().lower():
        assistant_expr = _first_non_empty(
            _json_payload_text("assistant", "assistant_id"),
            _json_payload_text("assistant_id"),
            AuditEventRow.assistant_id,
        )
        clauses.append(_lower_text(assistant_expr) == assistant_id)
    if user_id := (filters.user_id or "").strip().lower():
        user_expr = _first_non_empty(
            _json_payload_text("user", "user_id"),
            _json_payload_text("session", "user_id"),
            _json_payload_text("user_id"),
        )
        clauses.append(_lower_text(user_expr) == user_id)
    if operation_type := (filters.operation_type or "").strip().lower():
        operation_expr = _first_non_empty(
            _json_payload_text("operation_type"),
            _json_payload_text("envelope", "hook_name"),
            _json_payload_text("hook_name"),
            _json_payload_text("tool", "name"),
            _json_payload_text("tool_name"),
            AuditEventRow.hook_name,
            AuditEventRow.tool_name,
        )
        clauses.append(
            _lower_text(operation_expr).like(
                _like_pattern(operation_type),
                escape="\\",
        )
            )
    if result := (filters.result or "").strip().lower():
        result_expr = _first_non_empty(
            _json_payload_text("result", "status"),
            _json_payload_text("status"),
            case(
                (AuditEventRow.success.is_(True), literal("success")),
                (AuditEventRow.success.is_(False), literal("failure")),
                else_=None,
            ),
            _json_payload_text("decision"),
        )
        clauses.append(_lower_text(result_expr) == result)
    if error_code := (filters.error_code or "").strip().lower():
        error_expr = _first_non_empty(
            _json_payload_text("result", "error_code"),
            _json_payload_text("error_code"),
            _json_payload_text("error", "code"),
        )
        clauses.append(_lower_text(error_expr) == error_code)
    return clauses


@dataclass(slots=True)
class AuditRetentionResult:
    retention_days: int
    cutoff: datetime
    archived: int
    deleted: int
    remaining: int


class WorkspaceFileRepository(Protocol):
    async def alist_files(
        self,
        *,
        assistant_id: str,
        bucket: Literal["temp", "persistent"] | None = None,
        direction: FileDirection | None = None,
        include_deleted: bool = False,
        limit: int | None = None,
    ) -> list[WorkspaceFile]: ...

    def list_files(
        self,
        *,
        assistant_id: str,
        bucket: Literal["temp", "persistent"] | None = None,
        direction: FileDirection | None = None,
        include_deleted: bool = False,
        limit: int | None = None,
    ) -> list[WorkspaceFile]: ...

    async def aget_file(self, file_id: str) -> WorkspaceFile | None: ...

    def get_file(self, file_id: str) -> WorkspaceFile | None: ...

    async def asave_file(self, record: WorkspaceFile) -> WorkspaceFile: ...

    def save_file(self, record: WorkspaceFile) -> WorkspaceFile: ...

    async def alist_events(
        self,
        *,
        assistant_id: str | None = None,
        file_id: str | None = None,
        limit: int = 100,
    ) -> list[WorkspaceFileEvent]: ...

    def list_events(
        self,
        *,
        assistant_id: str | None = None,
        file_id: str | None = None,
        limit: int = 100,
    ) -> list[WorkspaceFileEvent]: ...

    async def aappend_event(self, event: WorkspaceFileEvent) -> WorkspaceFileEvent: ...

    def append_event(self, event: WorkspaceFileEvent) -> WorkspaceFileEvent: ...

    async def alist_push_requests(
        self,
        *,
        assistant_id: str | None = None,
        limit: int = 100,
    ) -> list[ExternalPushRequest]: ...

    def list_push_requests(
        self,
        *,
        assistant_id: str | None = None,
        limit: int = 100,
    ) -> list[ExternalPushRequest]: ...

    async def aget_push_request_by_approval(
        self,
        approval_id: str,
    ) -> ExternalPushRequest | None: ...

    def get_push_request_by_approval(self, approval_id: str) -> ExternalPushRequest | None: ...

    async def asave_push_request(self, item: ExternalPushRequest) -> ExternalPushRequest: ...

    def save_push_request(self, item: ExternalPushRequest) -> ExternalPushRequest: ...


class AssistantRow(Base):
    __tablename__ = "assistants"

    assistant_id: Mapped[str] = mapped_column(String(191), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    capability_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    iron_agent_config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    workspace_config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    runtime: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApprovalRow(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(191), primary_key=True)
    tool_name: Mapped[str] = mapped_column(String(191), nullable=False)
    assistant_id: Mapped[str | None] = mapped_column(String(191), nullable=True, index=True)
    capability_profile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approval_scope: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    target: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    resolution: Mapped[str | None] = mapped_column(String(32), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditEventRow(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_request_hook", "request_id", "hook_name", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(191), nullable=False, index=True)
    hook_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    assistant_id: Mapped[str | None] = mapped_column(String(191), nullable=True, index=True)
    capability_profile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(191), nullable=True, index=True)
    decision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class JsonAssistantRepository:
    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path

    def is_initialized(self) -> bool:
        return self.store_path.exists()

    def list_assistants(self) -> list[AssistantRecord]:
        return self._load_records()

    async def alist_assistants(self) -> list[AssistantRecord]:
        return self.list_assistants()

    def get_assistant(self, assistant_id: str) -> AssistantRecord | None:
        trimmed = assistant_id.strip()
        if not trimmed:
            return None
        for record in self._load_records():
            if record.assistant_id == trimmed:
                return record
        return None

    async def aget_assistant(self, assistant_id: str) -> AssistantRecord | None:
        return self.get_assistant(assistant_id)

    def upsert_assistant(self, record: AssistantRecord) -> AssistantRecord:
        records = self._load_records()
        replaced = False
        next_records: list[AssistantRecord] = []
        for existing in records:
            if existing.assistant_id == record.assistant_id:
                next_records.append(record)
                replaced = True
            else:
                next_records.append(existing)
        if not replaced:
            next_records.append(record)
        self._write_records(next_records)
        return record

    async def aupsert_assistant(self, record: AssistantRecord) -> AssistantRecord:
        return self.upsert_assistant(record)

    def delete_assistant(self, assistant_id: str) -> bool:
        trimmed = assistant_id.strip()
        if not trimmed:
            return False
        records = self._load_records()
        next_records = [record for record in records if record.assistant_id != trimmed]
        if len(next_records) == len(records):
            return False
        self._write_records(next_records)
        return True

    async def adelete_assistant(self, assistant_id: str) -> bool:
        return self.delete_assistant(assistant_id)

    def _load_records(self) -> list[AssistantRecord]:
        payload = read_json_object(self.store_path, {"assistants": []})
        items = payload.get("assistants", []) if isinstance(payload, dict) else []
        return [AssistantRecord.model_validate(item) for item in items if isinstance(item, dict)]

    def _write_records(self, records: Sequence[AssistantRecord]) -> None:
        write_json_object(
            self.store_path,
            {
                "assistants": [
                    record.model_dump(mode="json")
                    for record in sorted(records, key=lambda item: item.name.lower())
                ]
            },
        )


class JsonApprovalRepository:
    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path

    def list_requests(
        self,
        *,
        approval_scope: Literal["user", "admin"] | None = None,
        status: str | None = None,
        assistant_id: str | None = None,
    ) -> list[ApprovalRequest]:
        requests = self._load_requests()
        if assistant_id is not None:
            requests = [item for item in requests if item.assistant_id == assistant_id]
        if approval_scope is not None:
            requests = [item for item in requests if item.approval_scope == approval_scope]
        if status is not None:
            requests = [item for item in requests if item.status == status]
        return requests

    async def alist_requests(
        self,
        *,
        approval_scope: Literal["user", "admin"] | None = None,
        status: str | None = None,
        assistant_id: str | None = None,
    ) -> list[ApprovalRequest]:
        return self.list_requests(
            approval_scope=approval_scope,
            status=status,
            assistant_id=assistant_id,
        )

    def get_request(self, approval_id: str) -> ApprovalRequest | None:
        trimmed = approval_id.strip()
        if not trimmed:
            return None
        for item in self._load_requests():
            if item.id == trimmed:
                return item
        return None

    async def aget_request(self, approval_id: str) -> ApprovalRequest | None:
        return self.get_request(approval_id)

    def create_request(self, item: ApprovalRequest) -> ApprovalRequest:
        requests = self._load_requests()
        requests.append(item)
        self._write_requests(requests)
        return item

    async def acreate_request(self, item: ApprovalRequest) -> ApprovalRequest:
        return self.create_request(item)

    def save_request(self, item: ApprovalRequest) -> ApprovalRequest:
        requests = self._load_requests()
        replaced = False
        next_requests: list[ApprovalRequest] = []
        for existing in requests:
            if existing.id == item.id:
                next_requests.append(item)
                replaced = True
            else:
                next_requests.append(existing)
        if not replaced:
            next_requests.append(item)
        self._write_requests(next_requests)
        return item

    async def asave_request(self, item: ApprovalRequest) -> ApprovalRequest:
        return self.save_request(item)

    def _load_requests(self) -> list[ApprovalRequest]:
        payload = read_json_object(self.store_path, {"requests": []})
        items = payload.get("requests", []) if isinstance(payload, dict) else []
        return [_approval_from_payload(item) for item in items if isinstance(item, dict)]

    def _write_requests(self, requests: Sequence[ApprovalRequest]) -> None:
        write_json_object(
            self.store_path,
            {
                "requests": [
                    _approval_to_payload(item)
                    for item in sorted(requests, key=lambda request: request.requested_at)
                ]
            },
        )


class JsonAuditRepository:
    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path
        self._lock = _json_audit_lock_for_path(store_path)

    def append_event(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            rows = self._load_events()
            request_id, hook_name = self._event_key(payload)
            replaced = False
            next_rows: list[dict[str, Any]] = []
            for row in rows:
                if self._event_key(row) == (request_id, hook_name):
                    next_rows.append(payload)
                    replaced = True
                else:
                    next_rows.append(row)
            if not replaced:
                next_rows.append(payload)
            with self.store_path.open("w", encoding="utf-8") as handle:
                for row in next_rows:
                    handle.write(_serialize_json(row) + "\n")

    async def aappend_event(self, payload: dict[str, Any]) -> None:
        await asyncio.to_thread(self.append_event, payload)

    def count_events(self) -> int:
        with self._lock:
            if not self.store_path.exists():
                return 0
            lines = self.store_path.read_text(encoding="utf-8").splitlines()
            return sum(1 for line in lines if line.strip())

    async def acount_events(self) -> int:
        return self.count_events()

    def list_recent(self, *, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = sorted(self._load_events(), key=self._event_occurred_at, reverse=True)
            return rows[:limit]

    async def alist_recent(self, *, limit: int) -> list[dict[str, Any]]:
        return self.list_recent(limit=limit)

    def list_page(
        self,
        *,
        limit: int,
        offset: int,
        filters: AuditLogFilters | None = None,
    ) -> AuditLogPage:
        with self._lock:
            normalized_limit = max(1, limit)
            normalized_offset = max(0, offset)
            rows = sorted(
                enumerate(self._load_events()),
                key=lambda item: (self._event_occurred_at(item[1]), item[0]),
                reverse=True,
            )
            payloads = [row for _, row in rows if _audit_event_matches_filters(row, filters)]
            return AuditLogPage(
                items=payloads[normalized_offset : normalized_offset + normalized_limit],
                total=len(payloads),
            )

    async def alist_page(
        self,
        *,
        limit: int,
        offset: int,
        filters: AuditLogFilters | None = None,
    ) -> AuditLogPage:
        return self.list_page(limit=limit, offset=offset, filters=filters)

    def cleanup_expired(
        self,
        *,
        retention_days: int,
        archive_path: Path | None = None,
    ) -> AuditRetentionResult:
        with self._lock:
            cutoff = _audit_retention_cutoff(retention_days)
            rows = self._load_events()
            expired = [row for row in rows if _audit_event_datetime(row) < cutoff]
            kept = [row for row in rows if _audit_event_datetime(row) >= cutoff]
            if expired and archive_path is not None:
                _append_audit_archive(archive_path, expired)
            if expired or self.store_path.exists():
                self.store_path.parent.mkdir(parents=True, exist_ok=True)
                with self.store_path.open("w", encoding="utf-8") as handle:
                    for row in kept:
                        handle.write(_serialize_json(row) + "\n")
            return AuditRetentionResult(
                retention_days=max(0, retention_days),
                cutoff=cutoff,
                archived=len(expired) if archive_path is not None else 0,
                deleted=len(expired),
                remaining=len(kept),
            )

    async def acleanup_expired(
        self,
        *,
        retention_days: int,
        archive_path: Path | None = None,
    ) -> AuditRetentionResult:
        return self.cleanup_expired(retention_days=retention_days, archive_path=archive_path)

    def _load_events(self) -> list[dict[str, Any]]:
        if not self.store_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.store_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = read_json_object_from_text(line)
            if isinstance(payload, dict):
                rows.append(payload)
        return rows

    @staticmethod
    def _event_key(payload: dict[str, Any]) -> tuple[str, str]:
        envelope = _as_dict(payload.get("envelope"))
        return (
            str(envelope.get("request_id", "")).strip(),
            str(envelope.get("hook_name", "")).strip(),
        )

    @staticmethod
    def _event_occurred_at(payload: dict[str, Any]) -> str:
        envelope = _as_dict(payload.get("envelope"))
        return str(envelope.get("occurred_at", "")).strip()


class DatabaseAssistantRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._session_factory = session_factory

    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory or get_session_factory()

    def is_initialized(self) -> bool:
        return True

    def list_assistants(self) -> list[AssistantRecord]:
        return run_sync(self.alist_assistants())

    async def alist_assistants(self) -> list[AssistantRecord]:
        async with self.session_factory()() as session:
            result = await session.execute(select(AssistantRow).order_by(AssistantRow.name.asc()))
            rows = result.scalars().all()
            return [_assistant_from_row(row) for row in rows]

    def get_assistant(self, assistant_id: str) -> AssistantRecord | None:
        return run_sync(self.aget_assistant(assistant_id))

    async def aget_assistant(self, assistant_id: str) -> AssistantRecord | None:
        trimmed = assistant_id.strip()
        if not trimmed:
            return None
        async with self.session_factory()() as session:
            row = await session.get(AssistantRow, trimmed)
            return None if row is None else _assistant_from_row(row)

    def upsert_assistant(self, record: AssistantRecord) -> AssistantRecord:
        return run_sync(self.aupsert_assistant(record))

    async def aupsert_assistant(self, record: AssistantRecord) -> AssistantRecord:
        async with self.session_factory()() as session:
            row = await session.get(AssistantRow, record.assistant_id)
            if row is None:
                row = AssistantRow(assistant_id=record.assistant_id)
                session.add(row)
            _apply_assistant_to_row(row, record)
            await session.commit()
        return record

    def delete_assistant(self, assistant_id: str) -> bool:
        return run_sync(self.adelete_assistant(assistant_id))

    async def adelete_assistant(self, assistant_id: str) -> bool:
        trimmed = assistant_id.strip()
        if not trimmed:
            return False
        async with self.session_factory()() as session:
            row = await session.get(AssistantRow, trimmed)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True


class DatabaseApprovalRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._session_factory = session_factory

    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory or get_session_factory()

    def list_requests(
        self,
        *,
        approval_scope: Literal["user", "admin"] | None = None,
        status: str | None = None,
        assistant_id: str | None = None,
    ) -> list[ApprovalRequest]:
        return run_sync(
            self.alist_requests(
                approval_scope=approval_scope,
                status=status,
                assistant_id=assistant_id,
        )
            )

    async def alist_requests(
        self,
        *,
        approval_scope: Literal["user", "admin"] | None = None,
        status: str | None = None,
        assistant_id: str | None = None,
    ) -> list[ApprovalRequest]:
        async with self.session_factory()() as session:
            query = select(ApprovalRow)
            if assistant_id is not None:
                query = query.where(ApprovalRow.assistant_id == assistant_id)
            if approval_scope is not None:
                query = query.where(ApprovalRow.approval_scope == approval_scope)
            if status is not None:
                query = query.where(ApprovalRow.status == status)
            result = await session.execute(query.order_by(ApprovalRow.requested_at.asc()))
            rows = result.scalars().all()
            return [_approval_from_row(row) for row in rows]

    def get_request(self, approval_id: str) -> ApprovalRequest | None:
        return run_sync(self.aget_request(approval_id))

    async def aget_request(self, approval_id: str) -> ApprovalRequest | None:
        trimmed = approval_id.strip()
        if not trimmed:
            return None
        async with self.session_factory()() as session:
            row = await session.get(ApprovalRow, trimmed)
            return None if row is None else _approval_from_row(row)

    def create_request(self, item: ApprovalRequest) -> ApprovalRequest:
        return run_sync(self.acreate_request(item))

    async def acreate_request(self, item: ApprovalRequest) -> ApprovalRequest:
        async with self.session_factory()() as session:
            row = ApprovalRow(id=item.id)
            session.add(row)
            _apply_approval_to_row(row, item)
            await session.commit()
        return item

    def save_request(self, item: ApprovalRequest) -> ApprovalRequest:
        return run_sync(self.asave_request(item))

    async def asave_request(self, item: ApprovalRequest) -> ApprovalRequest:
        for attempt in range(2):
            try:
                async with self.session_factory()() as session:
                    row = await session.get(ApprovalRow, item.id)
                    if row is None:
                        row = ApprovalRow(id=item.id)
                        session.add(row)
                    _apply_approval_to_row(row, item)
                    await session.commit()
                return item
            except IntegrityError:
                if attempt == 0:
                    continue
                raise


class DatabaseAuditRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._session_factory = session_factory

    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory or get_session_factory()

    def append_event(self, payload: dict[str, Any]) -> None:
        run_sync(self.aappend_event(payload))

    async def aappend_event(self, payload: dict[str, Any]) -> None:
        details = extract_audit_fields(payload)
        for attempt in range(2):
            try:
                async with self.session_factory()() as session:
                    result = await session.execute(
                        select(AuditEventRow).where(
                            AuditEventRow.request_id == details.request_id,
                            AuditEventRow.hook_name == details.hook_name,
                    )
                        )
                    existing = result.scalar_one_or_none()
                    if existing is None:
                        existing = AuditEventRow(
                            request_id=details.request_id,
                            hook_name=details.hook_name,
                            occurred_at=details.occurred_at,
                            payload=payload,
                        )
                        session.add(existing)
                    existing.assistant_id = details.assistant_id
                    existing.capability_profile = details.capability_profile
                    existing.tool_name = details.tool_name
                    existing.decision = details.decision
                    existing.success = details.success
                    existing.occurred_at = details.occurred_at
                    existing.payload = payload
                    existing.created_at = existing.created_at if existing.created_at else utc_now()
                    await session.commit()
                    return
            except IntegrityError:
                if attempt == 0:
                    continue
                raise

    def count_events(self) -> int:
        return run_sync(self.acount_events())

    async def acount_events(self) -> int:
        async with self.session_factory()() as session:
            count = await session.scalar(select(func.count(AuditEventRow.id)))
            return cast(int, count or 0)

    def list_recent(self, *, limit: int) -> list[dict[str, Any]]:
        return run_sync(self.alist_recent(limit=limit))

    async def alist_recent(self, *, limit: int) -> list[dict[str, Any]]:
        async with self.session_factory()() as session:
            result = await session.execute(
                select(AuditEventRow)
                .order_by(desc(AuditEventRow.occurred_at), desc(AuditEventRow.id))
                .limit(limit)
            )
            rows = result.scalars().all()
            return [row.payload for row in rows]

    def list_page(
        self,
        *,
        limit: int,
        offset: int,
        filters: AuditLogFilters | None = None,
    ) -> AuditLogPage:
        return run_sync(self.alist_page(limit=limit, offset=offset, filters=filters))

    async def alist_page(
        self,
        *,
        limit: int,
        offset: int,
        filters: AuditLogFilters | None = None,
    ) -> AuditLogPage:
        normalized_limit = max(1, limit)
        normalized_offset = max(0, offset)
        clauses = _audit_sql_filter_clauses(filters)
        async with self.session_factory()() as session:
            total_query = select(func.count(AuditEventRow.id))
            rows_query = select(AuditEventRow)
            if clauses:
                total_query = total_query.where(*clauses)
                rows_query = rows_query.where(*clauses)
            total = await session.scalar(total_query)
            result = await session.execute(
                rows_query.order_by(desc(AuditEventRow.occurred_at), desc(AuditEventRow.id))
                .offset(normalized_offset)
                .limit(normalized_limit)
            )
            rows = result.scalars().all()
            return AuditLogPage(
                items=[row.payload for row in rows],
                total=cast(int, total or 0),
            )

    def cleanup_expired(
        self,
        *,
        retention_days: int,
        archive_path: Path | None = None,
    ) -> AuditRetentionResult:
        return run_sync(
            self.acleanup_expired(retention_days=retention_days, archive_path=archive_path)
        )

    async def acleanup_expired(
        self,
        *,
        retention_days: int,
        archive_path: Path | None = None,
    ) -> AuditRetentionResult:
        cutoff = _audit_retention_cutoff(retention_days)
        async with self.session_factory()() as session:
            result = await session.execute(
                select(AuditEventRow).where(AuditEventRow.occurred_at < cutoff)
            )
            expired_rows = result.scalars().all()
            expired_payloads = [row.payload for row in expired_rows]
            if expired_payloads and archive_path is not None:
                _append_audit_archive(archive_path, expired_payloads)
            if expired_rows:
                await session.execute(
                    delete(AuditEventRow).where(AuditEventRow.occurred_at < cutoff)
                )
                await session.commit()
            remaining = await session.scalar(select(func.count(AuditEventRow.id)))
        return AuditRetentionResult(
            retention_days=max(0, retention_days),
            cutoff=cutoff,
            archived=len(expired_payloads) if archive_path is not None else 0,
            deleted=len(expired_payloads),
            remaining=cast(int, remaining or 0),
        )


class JsonWorkspaceFileRepository:
    def __init__(self, files_store: Path, events_store: Path, push_store: Path) -> None:
        self.files_store = files_store
        self.events_store = events_store
        self.push_store = push_store

    def list_files(
        self,
        *,
        assistant_id: str,
        bucket: Literal["temp", "persistent"] | None = None,
        direction: FileDirection | None = None,
        include_deleted: bool = False,
        limit: int | None = None,
    ) -> list[WorkspaceFile]:
        files = [item for item in self._load_files() if item.assistant_id == assistant_id]
        if bucket is not None:
            files = [item for item in files if item.bucket == bucket]
        if direction is not None:
            files = [item for item in files if item.direction == direction]
        if not include_deleted:
            files = [item for item in files if not item.deleted]
        if limit is not None:
            files = sorted(
                files,
                key=lambda item: (item.updated_at, item.file_id),
                reverse=True,
            )[:limit]
        return files

    async def alist_files(
        self,
        *,
        assistant_id: str,
        bucket: Literal["temp", "persistent"] | None = None,
        direction: FileDirection | None = None,
        include_deleted: bool = False,
        limit: int | None = None,
    ) -> list[WorkspaceFile]:
        return self.list_files(
            assistant_id=assistant_id,
            bucket=bucket,
            direction=direction,
            include_deleted=include_deleted,
            limit=limit,
        )

    def get_file(self, file_id: str) -> WorkspaceFile | None:
        trimmed = file_id.strip()
        if not trimmed:
            return None
        return next((item for item in self._load_files() if item.file_id == trimmed), None)

    async def aget_file(self, file_id: str) -> WorkspaceFile | None:
        return self.get_file(file_id)

    def save_file(self, record: WorkspaceFile) -> WorkspaceFile:
        files = self._load_files()
        replaced = False
        next_files: list[WorkspaceFile] = []
        for existing in files:
            if existing.file_id == record.file_id:
                next_files.append(record)
                replaced = True
            else:
                next_files.append(existing)
        if not replaced:
            next_files.append(record)
        self._write_files(next_files)
        return record

    async def asave_file(self, record: WorkspaceFile) -> WorkspaceFile:
        return self.save_file(record)

    def list_events(
        self,
        *,
        assistant_id: str | None = None,
        file_id: str | None = None,
        limit: int = 100,
    ) -> list[WorkspaceFileEvent]:
        events = self._load_events()
        if assistant_id:
            events = [item for item in events if item.assistant_id == assistant_id]
        if file_id:
            events = [item for item in events if item.file_id == file_id]
        return list(reversed(events[-limit:]))

    async def alist_events(
        self,
        *,
        assistant_id: str | None = None,
        file_id: str | None = None,
        limit: int = 100,
    ) -> list[WorkspaceFileEvent]:
        return self.list_events(assistant_id=assistant_id, file_id=file_id, limit=limit)

    def append_event(self, event: WorkspaceFileEvent) -> WorkspaceFileEvent:
        events = self._load_events()
        events.append(event)
        self._write_events(events)
        return event

    async def aappend_event(self, event: WorkspaceFileEvent) -> WorkspaceFileEvent:
        return self.append_event(event)

    def list_push_requests(
        self,
        *,
        assistant_id: str | None = None,
        limit: int = 100,
    ) -> list[ExternalPushRequest]:
        requests = self._load_push_requests()
        if assistant_id:
            requests = [item for item in requests if item.assistant_id == assistant_id]
        return list(reversed(requests[-limit:]))

    async def alist_push_requests(
        self,
        *,
        assistant_id: str | None = None,
        limit: int = 100,
    ) -> list[ExternalPushRequest]:
        return self.list_push_requests(assistant_id=assistant_id, limit=limit)

    def get_push_request_by_approval(self, approval_id: str) -> ExternalPushRequest | None:
        trimmed = approval_id.strip()
        if not trimmed:
            return None
        requests = self._load_push_requests()
        return next((item for item in reversed(requests) if item.approval_id == trimmed), None)

    async def aget_push_request_by_approval(self, approval_id: str) -> ExternalPushRequest | None:
        return self.get_push_request_by_approval(approval_id)

    def save_push_request(self, item: ExternalPushRequest) -> ExternalPushRequest:
        requests = self._load_push_requests()
        replaced = False
        next_requests: list[ExternalPushRequest] = []
        for existing in requests:
            if existing.push_id == item.push_id:
                next_requests.append(item)
                replaced = True
            else:
                next_requests.append(existing)
        if not replaced:
            next_requests.append(item)
        self._write_push_requests(next_requests)
        return item

    async def asave_push_request(self, item: ExternalPushRequest) -> ExternalPushRequest:
        return self.save_push_request(item)

    def _load_files(self) -> list[WorkspaceFile]:
        payload = read_json_object(self.files_store, None)
        if payload is None:
            return []
        items = payload.get("files", []) if isinstance(payload, dict) else []
        return [WorkspaceFile.model_validate(item) for item in items if isinstance(item, dict)]

    def _write_files(self, files: Sequence[WorkspaceFile]) -> None:
        write_json_object(
            self.files_store,
            {"files": [item.model_dump(mode="json") for item in files]},
        )

    def _load_events(self) -> list[WorkspaceFileEvent]:
        payload = read_json_object(self.events_store, None)
        if payload is None:
            return []
        items = payload.get("events", []) if isinstance(payload, dict) else []
        return [WorkspaceFileEvent.model_validate(item) for item in items if isinstance(item, dict)]

    def _write_events(self, events: Sequence[WorkspaceFileEvent]) -> None:
        write_json_object(
            self.events_store,
            {"events": [item.model_dump(mode="json") for item in events]},
        )

    def _load_push_requests(self) -> list[ExternalPushRequest]:
        payload = read_json_object(self.push_store, None)
        if payload is None:
            return []
        items = payload.get("push_requests", []) if isinstance(payload, dict) else []
        return [
            ExternalPushRequest.model_validate(item) for item in items if isinstance(item, dict)
        ]

    def _write_push_requests(self, requests: Sequence[ExternalPushRequest]) -> None:
        write_json_object(
            self.push_store,
            {"push_requests": [item.model_dump(mode="json") for item in requests]},
        )


class DatabaseWorkspaceFileRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._session_factory = session_factory
        self._workspace_files_schema_checked = False

    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory or get_session_factory()

    async def _ensure_workspace_file_schema(self, session: AsyncSession) -> None:
        if self._workspace_files_schema_checked:
            return
        connection = await session.connection()
        if connection.dialect.name != "sqlite":
            self._workspace_files_schema_checked = True
            return
        result = await connection.exec_driver_sql("PRAGMA table_info(workspace_files)")
        columns = {str(row[1]) for row in result.fetchall()}
        statements: list[str] = []
        if "checksum_sha256" not in columns:
            statements.append(
                "ALTER TABLE workspace_files "
                "ADD COLUMN checksum_sha256 VARCHAR(64) NOT NULL DEFAULT ''"
            )
        if "workspace_last_modified_at" not in columns:
            statements.append(
                "ALTER TABLE workspace_files ADD COLUMN workspace_last_modified_at DATETIME"
            )
        if "scan_fingerprint" not in columns:
            statements.append(
                "ALTER TABLE workspace_files "
                "ADD COLUMN scan_fingerprint VARCHAR(255) NOT NULL DEFAULT ''"
            )
        if "owner_user_id" not in columns:
            statements.append(
                "ALTER TABLE workspace_files "
                "ADD COLUMN owner_user_id VARCHAR(255) NOT NULL DEFAULT ''"
            )
        if "active_path_key" not in columns:
            statements.append(
                "ALTER TABLE workspace_files ADD COLUMN active_path_key VARCHAR(64)"
            )
        for statement in statements:
            await connection.exec_driver_sql(statement)
        await self._deduplicate_sqlite_active_paths(connection)
        await self._backfill_sqlite_active_path_keys(connection)
        await connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_files_active_path "
            "ON workspace_files(active_path_key)"
        )
        await session.commit()
        self._workspace_files_schema_checked = True

    async def _deduplicate_sqlite_active_paths(self, connection: Any) -> None:
        await connection.exec_driver_sql(
            """
            WITH ranked AS (
                SELECT
                    file_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY assistant_id, bucket, filename
                        ORDER BY updated_at DESC, file_id DESC
                    ) AS row_rank
                FROM workspace_files
                WHERE deleted = 0
            )
            UPDATE workspace_files
            SET
                deleted = 1,
                active_path_key = NULL,
                sync_status = 'deleted_from_workspace',
                storage_status = CASE
                    WHEN COALESCE(storage_status, '') IN ('', 'pending')
                    THEN 'deleted_from_workspace'
                    ELSE storage_status
                END,
                last_error = CASE
                    WHEN COALESCE(last_error, '') = ''
                    THEN 'duplicate active workspace file row superseded during sqlite migration'
                    ELSE last_error
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE file_id IN (SELECT file_id FROM ranked WHERE row_rank > 1)
            """
        )

    async def _backfill_sqlite_active_path_keys(self, connection: Any) -> None:
        result = await connection.exec_driver_sql(
            """
            SELECT file_id, assistant_id, bucket, filename, deleted
            FROM workspace_files
            """
        )
        for row in result.mappings().all():
            active_path_key = workspace_file_active_path_key(
                str(row["assistant_id"]),
                str(row["bucket"]),
                str(row["filename"]),
                deleted=bool(row["deleted"]),
            )
            await connection.exec_driver_sql(
                "UPDATE workspace_files SET active_path_key = ? WHERE file_id = ?",
                (active_path_key, row["file_id"]),
            )

    def list_files(
        self,
        *,
        assistant_id: str,
        bucket: Literal["temp", "persistent"] | None = None,
        direction: FileDirection | None = None,
        include_deleted: bool = False,
        limit: int | None = None,
    ) -> list[WorkspaceFile]:
        return run_sync(
            self.alist_files(
                assistant_id=assistant_id,
                bucket=bucket,
                direction=direction,
                include_deleted=include_deleted,
                limit=limit,
        )
            )

    async def alist_files(
        self,
        *,
        assistant_id: str,
        bucket: Literal["temp", "persistent"] | None = None,
        direction: FileDirection | None = None,
        include_deleted: bool = False,
        limit: int | None = None,
    ) -> list[WorkspaceFile]:
        async with self.session_factory()() as session:
            await self._ensure_workspace_file_schema(session)
            query = select(WorkspaceFileRow).where(WorkspaceFileRow.assistant_id == assistant_id)
            if bucket is not None:
                query = query.where(WorkspaceFileRow.bucket == bucket)
            if direction is not None:
                query = query.where(WorkspaceFileRow.direction == direction)
            if not include_deleted:
                query = query.where(WorkspaceFileRow.deleted.is_(False))
            if limit is None:
                query = query.order_by(
                    WorkspaceFileRow.created_at.asc(),
                    WorkspaceFileRow.file_id.asc(),
                )
            else:
                query = query.order_by(
                    WorkspaceFileRow.updated_at.desc(),
                    WorkspaceFileRow.file_id.desc(),
                ).limit(limit)
            result = await session.execute(query)
            rows = result.scalars().all()
            return [_workspace_file_from_row(row) for row in rows]

    def get_file(self, file_id: str) -> WorkspaceFile | None:
        return run_sync(self.aget_file(file_id))

    async def aget_file(self, file_id: str) -> WorkspaceFile | None:
        trimmed = file_id.strip()
        if not trimmed:
            return None
        async with self.session_factory()() as session:
            await self._ensure_workspace_file_schema(session)
            row = await session.get(WorkspaceFileRow, trimmed)
            return None if row is None else _workspace_file_from_row(row)

    async def aget_active_file_by_path(
        self,
        *,
        assistant_id: str,
        bucket: Literal["temp", "persistent"],
        filename: str,
    ) -> WorkspaceFile | None:
        async with self.session_factory()() as session:
            await self._ensure_workspace_file_schema(session)
            result = await session.execute(
                select(WorkspaceFileRow).where(
                    WorkspaceFileRow.assistant_id == assistant_id,
                    WorkspaceFileRow.bucket == bucket,
                    WorkspaceFileRow.filename == filename,
                    WorkspaceFileRow.deleted.is_(False),
                )
                .order_by(WorkspaceFileRow.updated_at.desc(), WorkspaceFileRow.file_id.desc())
                .limit(1)
            )
            row = result.scalars().first()
            return None if row is None else _workspace_file_from_row(row)

    def save_file(self, record: WorkspaceFile) -> WorkspaceFile:
        return run_sync(self.asave_file(record))

    async def asave_file(self, record: WorkspaceFile) -> WorkspaceFile:
        try:
            return await self._save_file_by_id(record)
        except IntegrityError:
            saved = await self._save_file_by_active_path(record)
            if saved is not None:
                return saved
            return await self._save_file_by_id(record)

    async def _save_file_by_id(self, record: WorkspaceFile) -> WorkspaceFile:
        async with self.session_factory()() as session:
            await self._ensure_workspace_file_schema(session)
            row = await session.get(WorkspaceFileRow, record.file_id)
            if row is None:
                row = WorkspaceFileRow(file_id=record.file_id)
                session.add(row)
            _apply_workspace_file_to_row(row, record)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
            await session.refresh(row)
            return _workspace_file_from_row(row)

    async def _save_file_by_active_path(self, record: WorkspaceFile) -> WorkspaceFile | None:
        active_path_key = workspace_file_active_path_key(
            record.assistant_id,
            str(record.bucket),
            record.filename,
            deleted=record.deleted,
        )
        if active_path_key is None:
            return None
        async with self.session_factory()() as session:
            await self._ensure_workspace_file_schema(session)
            result = await session.execute(
                select(WorkspaceFileRow)
                .where(
                    WorkspaceFileRow.active_path_key == active_path_key,
                    WorkspaceFileRow.deleted.is_(False),
                )
                .limit(1)
            )
            row = result.scalars().first()
            if row is None:
                return None
            _apply_workspace_file_to_row(row, record)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
            await session.refresh(row)
            return _workspace_file_from_row(row)

    def list_events(
        self,
        *,
        assistant_id: str | None = None,
        file_id: str | None = None,
        limit: int = 100,
    ) -> list[WorkspaceFileEvent]:
        return run_sync(self.alist_events(assistant_id=assistant_id, file_id=file_id, limit=limit))

    async def alist_events(
        self,
        *,
        assistant_id: str | None = None,
        file_id: str | None = None,
        limit: int = 100,
    ) -> list[WorkspaceFileEvent]:
        async with self.session_factory()() as session:
            query = select(WorkspaceFileEventRow)
            if assistant_id is not None:
                query = query.where(WorkspaceFileEventRow.assistant_id == assistant_id)
            if file_id is not None:
                query = query.where(WorkspaceFileEventRow.file_id == file_id)
            result = await session.execute(
                query.order_by(
                    desc(WorkspaceFileEventRow.created_at),
                    desc(WorkspaceFileEventRow.event_id),
                ).limit(limit)
            )
            rows = result.scalars().all()
            return [_workspace_file_event_from_row(row) for row in rows]

    def append_event(self, event: WorkspaceFileEvent) -> WorkspaceFileEvent:
        return run_sync(self.aappend_event(event))

    async def aappend_event(self, event: WorkspaceFileEvent) -> WorkspaceFileEvent:
        for attempt in range(2):
            try:
                async with self.session_factory()() as session:
                    row = await session.get(WorkspaceFileEventRow, event.event_id)
                    if row is None:
                        row = WorkspaceFileEventRow(event_id=event.event_id)
                        session.add(row)
                    _apply_workspace_file_event_to_row(row, event)
                    await session.commit()
                return event
            except IntegrityError:
                if attempt == 0:
                    continue
                raise

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
        async with self.session_factory()() as session:
            query = select(ExternalPushRequestRow)
            if assistant_id is not None:
                query = query.where(ExternalPushRequestRow.assistant_id == assistant_id)
            result = await session.execute(
                query.order_by(
                    desc(ExternalPushRequestRow.created_at),
                    desc(ExternalPushRequestRow.push_id),
                ).limit(limit)
            )
            rows = result.scalars().all()
            return [_external_push_request_from_row(row) for row in rows]

    def get_push_request_by_approval(self, approval_id: str) -> ExternalPushRequest | None:
        return run_sync(self.aget_push_request_by_approval(approval_id))

    async def aget_push_request_by_approval(self, approval_id: str) -> ExternalPushRequest | None:
        trimmed = approval_id.strip()
        if not trimmed:
            return None
        async with self.session_factory()() as session:
            result = await session.execute(
                select(ExternalPushRequestRow)
                .where(ExternalPushRequestRow.approval_id == trimmed)
                .order_by(
                    desc(ExternalPushRequestRow.created_at),
                    desc(ExternalPushRequestRow.push_id),
                )
                .limit(1)
            )
            row = result.scalar_one_or_none()
            return None if row is None else _external_push_request_from_row(row)

    def save_push_request(self, item: ExternalPushRequest) -> ExternalPushRequest:
        return run_sync(self.asave_push_request(item))

    async def asave_push_request(self, item: ExternalPushRequest) -> ExternalPushRequest:
        for attempt in range(2):
            try:
                async with self.session_factory()() as session:
                    row = await session.get(ExternalPushRequestRow, item.push_id)
                    if row is None:
                        row = ExternalPushRequestRow(push_id=item.push_id)
                        session.add(row)
                    _apply_external_push_request_to_row(row, item)
                    await session.commit()
                return item
            except IntegrityError:
                if attempt == 0:
                    continue
                raise


@dataclass(slots=True)
class AuditEventFields:
    request_id: str
    hook_name: str
    assistant_id: str | None
    capability_profile: str | None
    tool_name: str | None
    decision: str | None
    success: bool | None
    occurred_at: datetime


def extract_audit_fields(payload: dict[str, Any]) -> AuditEventFields:
    envelope = _as_dict(payload.get("envelope"))
    assistant = _as_dict(payload.get("assistant"))
    tool = _as_dict(payload.get("tool"))
    result = _as_dict(payload.get("result"))
    occurred_at_raw = envelope.get("occurred_at")
    occurred_at = utc_now()
    if isinstance(occurred_at_raw, str):
        try:
            occurred_at = datetime.fromisoformat(occurred_at_raw)
        except ValueError:
            occurred_at = utc_now()
    return AuditEventFields(
        request_id=str(envelope.get("request_id", "")).strip(),
        hook_name=str(envelope.get("hook_name", "")).strip(),
        assistant_id=_coerce_optional_string(assistant.get("assistant_id")),
        capability_profile=_coerce_optional_string(assistant.get("capability_profile")),
        tool_name=_coerce_optional_string(tool.get("name")),
        decision=_coerce_optional_string(payload.get("decision")),
        success=result.get("success") if isinstance(result.get("success"), bool) else None,
        occurred_at=occurred_at,
    )


def audit_payload_user_id(payload: dict[str, Any]) -> str:
    user = _as_dict(payload.get("user"))
    session = _as_dict(payload.get("session"))
    return str(
        user.get("user_id") or session.get("user_id") or payload.get("user_id") or ""
    ).strip()


def audit_payload_assistant_id(payload: dict[str, Any]) -> str:
    assistant = _as_dict(payload.get("assistant"))
    return str(assistant.get("assistant_id") or payload.get("assistant_id") or "").strip()


def audit_payload_operation_type(payload: dict[str, Any]) -> str:
    envelope = _as_dict(payload.get("envelope"))
    tool = _as_dict(payload.get("tool"))
    return str(
        payload.get("operation_type")
        or envelope.get("hook_name")
        or payload.get("hook_name")
        or tool.get("name")
        or payload.get("tool_name")
        or ""
    ).strip()


def audit_payload_result(payload: dict[str, Any]) -> str:
    result = _as_dict(payload.get("result"))
    status = str(result.get("status") or payload.get("status") or "").strip()
    if status:
        return status
    success = result.get("success") if isinstance(result.get("success"), bool) else None
    if success is True:
        return "success"
    if success is False:
        return "failure"
    decision = str(payload.get("decision") or "").strip()
    return decision


def audit_payload_error_code(payload: dict[str, Any]) -> str:
    result = _as_dict(payload.get("result"))
    error = _as_dict(payload.get("error"))
    return str(
        result.get("error_code") or payload.get("error_code") or error.get("code") or ""
    ).strip()


def build_assistant_repository(
    *,
    backend: StorageBackendName | None = None,
) -> AssistantRepository:
    settings = get_settings()
    effective_backend = backend or settings.storage_backend
    if effective_backend == "mysql":
        return DatabaseAssistantRepository()
    return JsonAssistantRepository(settings.assistants_store_path)


def build_approval_repository(
    *,
    backend: StorageBackendName | None = None,
) -> ApprovalRepository:
    settings = get_settings()
    effective_backend = backend or settings.storage_backend
    if effective_backend == "mysql":
        return DatabaseApprovalRepository()
    return JsonApprovalRepository(settings.approval_store_path)


def build_audit_repository(
    *,
    backend: StorageBackendName | None = None,
) -> AuditRepository:
    settings = get_settings()
    effective_backend = backend or settings.storage_backend
    if effective_backend == "mysql":
        return DatabaseAuditRepository()
    return JsonAuditRepository(settings.audit_log_path)


def build_workspace_file_repository(
    *,
    backend: StorageBackendName | None = None,
) -> WorkspaceFileRepository:
    settings = get_settings()
    effective_backend = backend or settings.storage_backend
    if effective_backend == "mysql":
        return DatabaseWorkspaceFileRepository()
    return JsonWorkspaceFileRepository(
        settings.workspace_files_store_path,
        settings.workspace_file_events_store_path,
        settings.workspace_push_requests_store_path,
    )


def _assistant_from_row(row: AssistantRow) -> AssistantRecord:
    return AssistantRecord.model_validate(
        {
            "assistant_id": row.assistant_id,
            "name": row.name,
            "capability_profile": row.capability_profile,
            "notes": row.notes,
            "iron_agent_config": row.iron_agent_config,
            "workspace_config": row.workspace_config,
            "runtime": row.runtime,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "deleted_at": row.deleted_at,
        }
    )


def _apply_assistant_to_row(row: AssistantRow, record: AssistantRecord) -> None:
    row.name = record.name
    row.capability_profile = record.capability_profile
    row.notes = record.notes
    row.iron_agent_config = record.iron_agent_config.model_dump(mode="json")
    row.workspace_config = record.workspace_config.model_dump(mode="json")
    row.runtime = record.runtime.model_dump(mode="json")
    row.created_at = record.created_at
    row.updated_at = record.updated_at
    row.deleted_at = record.deleted_at


def _approval_to_payload(item: ApprovalRequest) -> dict[str, str | None]:
    return {
        "id": item.id,
        "tool_name": item.tool_name,
        "assistant_id": item.assistant_id,
        "capability_profile": item.capability_profile,
        "approval_scope": item.approval_scope,
        "reason": item.reason,
        "target": item.target,
        "status": item.status,
        "resolution": item.resolution,
        "requested_at": item.requested_at.isoformat(),
        "expires_at": item.expires_at.isoformat(),
        "resolved_at": item.resolved_at.isoformat() if item.resolved_at else None,
        "gateway_url": item.gateway_url,
    }


def _approval_from_payload(payload: dict[str, Any]) -> ApprovalRequest:
    requested_at = payload.get("requested_at")
    expires_at = payload.get("expires_at")
    resolved_at = payload.get("resolved_at")
    resolution = payload.get("resolution")
    safe_resolution: Literal["allow", "deny"] | None
    if resolution == "allow":
        safe_resolution = "allow"
    elif resolution == "deny":
        safe_resolution = "deny"
    else:
        safe_resolution = None
    approval_scope = payload.get("approval_scope")
    safe_approval_scope: Literal["user", "admin"]
    if approval_scope == "user":
        safe_approval_scope = "user"
    else:
        safe_approval_scope = "admin"
    return ApprovalRequest(
        id=str(payload.get("id", "")).strip(),
        tool_name=str(payload.get("tool_name", "")).strip(),
        assistant_id=_coerce_optional_string(payload.get("assistant_id")),
        capability_profile=_coerce_optional_string(payload.get("capability_profile")),
        approval_scope=safe_approval_scope,
        reason=_coerce_optional_string(payload.get("reason")),
        target=_coerce_optional_string(payload.get("target")),
        status=_coerce_optional_string(payload.get("status")) or "pending",
        resolution=safe_resolution,
        requested_at=_parse_datetime(requested_at),
        expires_at=_parse_datetime(expires_at),
        resolved_at=_parse_datetime(resolved_at) if resolved_at else None,
        gateway_url=_coerce_optional_string(payload.get("gateway_url")),
    )


def _approval_from_row(row: ApprovalRow) -> ApprovalRequest:
    safe_resolution: Literal["allow", "deny"] | None
    if row.resolution == "allow":
        safe_resolution = "allow"
    elif row.resolution == "deny":
        safe_resolution = "deny"
    else:
        safe_resolution = None
    return ApprovalRequest(
        id=row.id,
        tool_name=row.tool_name,
        assistant_id=row.assistant_id,
        capability_profile=row.capability_profile,
        approval_scope="user" if row.approval_scope == "user" else "admin",
        reason=row.reason,
        target=row.target,
        status=row.status,
        resolution=safe_resolution,
        requested_at=row.requested_at,
        expires_at=row.expires_at,
        resolved_at=row.resolved_at,
    )


def _apply_approval_to_row(row: ApprovalRow, item: ApprovalRequest) -> None:
    row.tool_name = item.tool_name
    row.assistant_id = item.assistant_id
    row.capability_profile = item.capability_profile
    row.approval_scope = item.approval_scope
    row.reason = item.reason
    row.target = item.target
    row.status = item.status
    row.resolution = item.resolution
    row.requested_at = item.requested_at
    row.expires_at = item.expires_at
    row.resolved_at = item.resolved_at


def _workspace_file_from_row(row: WorkspaceFileRow) -> WorkspaceFile:
    storage_backend = row.storage_backend
    if storage_backend == "disk":
        storage_backend = "local"
    object_key = row.object_key or row.storage_key
    object_url = row.object_url or row.storage_path
    last_error = row.last_error or row.sync_error or row.storage_error
    direction = row.direction
    if not direction:
        direction = "outbound" if row.file_role == "output" else "inbound"
    sync_status = _normalize_sync_status(row.sync_status, row.storage_status)
    return WorkspaceFile.model_validate(
        {
            "file_id": row.file_id,
            "assistant_id": row.assistant_id,
            "bucket": row.bucket,
            "filename": row.filename,
            "relative_path": row.relative_path,
            "direction": direction,
            "file_role": row.file_role,
            "size_bytes": row.size_bytes,
            "content_type": row.content_type,
            "owner_user_id": row.owner_user_id,
            "uploader_user_id": row.uploader_user_id,
            "source_type": row.source_type,
            "source_ref": row.source_ref,
            "source_file_id": row.source_file_id,
            "storage_backend": storage_backend,
            "storage_provider": row.storage_provider
            or _storage_provider_for_backend(storage_backend),
            "storage_key": row.storage_key or object_key,
            "storage_path": row.storage_path,
            "storage_status": row.storage_status,
            "storage_error": row.storage_error,
            "object_key": object_key,
            "object_url": object_url,
            "checksum": row.checksum_sha256 or row.checksum,
            "scan_fingerprint": getattr(row, "scan_fingerprint", ""),
            "sync_status": sync_status,
            "last_error": last_error,
            "workspace_last_modified_at": row.workspace_last_modified_at,
            "last_synced_at": row.last_synced_at,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "deleted": row.deleted,
        }
    )


def _apply_workspace_file_to_row(row: WorkspaceFileRow, record: WorkspaceFile) -> None:
    row.assistant_id = record.assistant_id
    row.bucket = record.bucket
    row.filename = record.filename
    row.relative_path = record.relative_path
    row.direction = record.direction
    row.size_bytes = record.size_bytes
    row.content_type = record.content_type
    row.owner_user_id = record.owner_user_id
    row.uploader_user_id = record.uploader_user_id
    row.source_type = record.source_type
    row.source_ref = record.source_ref
    row.active_path_key = workspace_file_active_path_key(
        record.assistant_id,
        str(record.bucket),
        record.filename,
        deleted=record.deleted,
    )
    row.storage_provider = record.storage_provider or _legacy_storage_provider(record)
    row.storage_key = record.storage_key or record.object_key
    row.storage_path = record.storage_path or record.relative_path
    row.file_role = record.file_role or _legacy_file_role(record)
    row.source_file_id = record.source_file_id
    row.storage_status = _legacy_storage_status(record)
    row.storage_error = record.storage_error or record.last_error
    row.storage_backend = record.storage_backend
    row.object_key = record.object_key or record.storage_key
    row.object_url = record.object_url or record.storage_path
    row.checksum_sha256 = record.checksum
    row.scan_fingerprint = record.scan_fingerprint
    row.sync_status = record.sync_status
    row.last_error = record.last_error or record.storage_error
    row.sync_error = record.last_error or record.storage_error
    row.workspace_last_modified_at = record.workspace_last_modified_at
    row.last_synced_at = record.last_synced_at
    row.created_at = record.created_at
    row.updated_at = record.updated_at
    row.deleted = record.deleted


def _legacy_storage_provider(record: WorkspaceFile) -> str:
    if record.storage_backend in {"cos", "obs"}:
        return record.storage_backend
    return "disk"


def _storage_provider_for_backend(storage_backend: str) -> str:
    if storage_backend in {"cos", "obs"}:
        return storage_backend
    return "local"


def _legacy_file_role(record: WorkspaceFile) -> str:
    if record.file_role:
        return record.file_role
    return "output" if record.direction == "outbound" else "input"


def _legacy_storage_status(record: WorkspaceFile) -> str:
    if record.storage_status:
        return record.storage_status
    if record.sync_status == "failed":
        return "failed"
    if record.sync_status == "uploaded":
        return "pending"
    if record.sync_status == "disabled":
        return "disabled"
    if record.sync_status == "pending":
        return "pending"
    return "synced"


def _sync_status_from_legacy(storage_status: str) -> str:
    if storage_status == "failed":
        return "failed"
    if storage_status == "pending":
        return "uploaded"
    return "synced_to_workspace"


def _normalize_sync_status(sync_status: str, legacy_storage_status: str) -> str:
    if sync_status in {
        "uploaded",
        "synced_to_workspace",
        "failed",
        "missing_from_workspace",
        "deleted_from_workspace",
    }:
        return sync_status
    if sync_status:
        return _sync_status_from_legacy(sync_status)
    return _sync_status_from_legacy(legacy_storage_status)


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


def _apply_workspace_file_event_to_row(
    row: WorkspaceFileEventRow,
    event: WorkspaceFileEvent,
) -> None:
    row.file_id = event.file_id
    row.assistant_id = event.assistant_id
    row.action = event.action
    row.user_id = event.user_id
    row.source_type = event.source_type
    row.source_ref = event.source_ref
    row.detail = event.detail
    row.created_at = event.created_at


def _external_push_request_from_row(row: ExternalPushRequestRow) -> ExternalPushRequest:
    return ExternalPushRequest.model_validate(
        {
            "push_id": row.push_id,
            "user_id": row.user_id,
            "assistant_id": row.assistant_id,
            "filename": row.filename,
            "bucket": row.bucket,
            "content_type": row.content_type,
            "storage_backend": row.storage_backend,
            "storage_key": row.storage_key,
            "storage_url": row.storage_url,
            "source_type": row.source_type,
            "source_ref": row.source_ref,
            "status": row.status,
            "approval_id": row.approval_id,
            "staged_path": row.staged_path,
            "error": row.error,
            "created_at": row.created_at,
        }
    )


def _apply_external_push_request_to_row(
    row: ExternalPushRequestRow,
    item: ExternalPushRequest,
) -> None:
    row.user_id = item.user_id
    row.assistant_id = item.assistant_id
    row.filename = item.filename
    row.bucket = item.bucket
    row.content_type = item.content_type
    row.storage_backend = item.storage_backend
    row.storage_key = item.storage_key
    row.storage_url = item.storage_url
    row.source_type = item.source_type
    row.source_ref = item.source_ref
    row.status = item.status
    row.approval_id = item.approval_id
    row.staged_path = item.staged_path
    row.error = item.error
    row.created_at = item.created_at


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _ensure_aware_utc(value)
    if isinstance(value, str) and value:
        try:
            return _ensure_aware_utc(datetime.fromisoformat(value))
        except ValueError:
            return utc_now()


def _ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _audit_event_datetime(payload: dict[str, Any]) -> datetime:
    envelope = _as_dict(payload.get("envelope"))
    return _parse_datetime(envelope.get("occurred_at") or payload.get("occurred_at"))


def _audit_retention_cutoff(retention_days: int) -> datetime:
    return utc_now() - timedelta(days=max(0, retention_days))


def _matches_text(candidate: str, expected: str | None, *, contains: bool = False) -> bool:
    normalized_expected = (expected or "").strip().lower()
    if not normalized_expected:
        return True
    normalized_candidate = candidate.strip().lower()
    if contains:
        return normalized_expected in normalized_candidate
    return normalized_candidate == normalized_expected


def _audit_event_matches_filters(
    payload: dict[str, Any],
    filters: AuditLogFilters | None,
) -> bool:
    if filters is None:
        return True
    occurred_at = _audit_event_datetime(payload)
    if filters.occurred_from is not None and occurred_at < _ensure_aware_utc(filters.occurred_from):
        return False
    if filters.occurred_to is not None and occurred_at > _ensure_aware_utc(filters.occurred_to):
        return False
    if not _matches_text(audit_payload_user_id(payload), filters.user_id):
        return False
    if not _matches_text(audit_payload_assistant_id(payload), filters.assistant_id):
        return False
    if not _matches_text(
        audit_payload_operation_type(payload),
        filters.operation_type,
        contains=True,
    ):
        return False
    if not _matches_text(audit_payload_result(payload), filters.result):
        return False
    if not _matches_text(audit_payload_error_code(payload), filters.error_code):
        return False
    return True


def _append_audit_archive(archive_path: Path, rows: Sequence[dict[str, Any]]) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with archive_path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(_serialize_json(row) + "\n")


def _coerce_optional_string(value: Any) -> str | None:
    if isinstance(value, str):
        trimmed = value.strip()
        return trimmed or None
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _serialize_json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=True)


def read_json_object_from_text(text: str) -> Any:
    import json

    return json.loads(text)
