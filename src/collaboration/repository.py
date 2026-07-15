from __future__ import annotations

import threading
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from alembic.script import ScriptDirectory
from alembic.script.revision import ResolutionError
from sqlalchemy import JSON, DateTime, Integer, String, Text, create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from src.collaboration.models import (
    CollaborationActorType,
    CollaborationApprovalMode,
    CollaborationClaim,
    CollaborationClaimStatusName,
    CollaborationEvent,
    CollaborationState,
    CollaborationTask,
    CollaborationTaskStatusName,
    SharedObjectAccessMode,
    SharedObjectGranteeType,
    SharedObjectPermission,
    SharedObjectPermissionStatusName,
    SharedObjectPermissionType,
    SharedObjectRetentionPolicy,
    SharedObjectStatusName,
    SharedObjectVisibilityScope,
    SharedWorkspaceObject,
)
from src.shared.config import get_settings

REQUIRED_ALEMBIC_REVISION = "022_collaboration_repository_tables"
PRODUCTION_ENV_NAMES = {"prod", "production"}
RETRYABLE_TASK_FAILURE_CODES = {
    "runtime_lookup_failed",
    "runtime_route_lookup_failed",
    "runtime_gateway_unavailable",
    "runtime_execution_failed",
    "runtime_result_object_failed",
    "gateway_task_stale_progress",
}
REQUIRED_UNIQUE_INDEXES: dict[str, tuple[str, list[str], bool]] = {
    "uq_shared_workspace_objects_idempotency_key": (
        "shared_workspace_objects",
        ["idempotency_key"],
        True,
    ),
    "uq_collaboration_tasks_idempotency_key": (
        "collaboration_tasks",
        ["idempotency_key"],
        True,
    ),
    "uq_collaboration_claims_claim_token": (
        "collaboration_claims",
        ["claim_token"],
        True,
    ),
}


def _alembic_revision_satisfies_required(revisions: Iterable[str]) -> bool:
    revision_ids = {revision.strip() for revision in revisions if revision and revision.strip()}
    if REQUIRED_ALEMBIC_REVISION in revision_ids:
        return True
    if not revision_ids:
        return False

    script_location = Path(__file__).resolve().parents[2] / "migrations"
    try:
        script = ScriptDirectory(str(script_location))
    except Exception:
        return False

    for revision in revision_ids:
        if _revision_descends_from(script, revision, REQUIRED_ALEMBIC_REVISION):
            return True
    return False


def _revision_descends_from(
    script: ScriptDirectory,
    revision: str,
    required_revision: str,
) -> bool:
    pending = [revision]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        if current == required_revision:
            return True
        try:
            script_revision = script.get_revision(current)
        except ResolutionError:
            return False
        if script_revision is None:
            return False
        down_revision = script_revision.down_revision
        if down_revision is None:
            continue
        if isinstance(down_revision, str):
            pending.append(down_revision)
        else:
            pending.extend(str(item) for item in down_revision)
    return False


class Base(DeclarativeBase):
    pass


class SharedWorkspaceObjectRow(Base):
    __tablename__ = "shared_workspace_objects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    storage_region: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    storage_etag: Mapped[str] = mapped_column(String(128), nullable=False)
    storage_version_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_assistant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_workspace_file_id: Mapped[str | None] = mapped_column(String(64))
    parent_object_id: Mapped[str | None] = mapped_column(String(64))
    derived_from_task_id: Mapped[str | None] = mapped_column(String(64))
    visibility_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    access_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    retention_policy: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    meta_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class SharedObjectPermissionRow(Base):
    __tablename__ = "shared_object_permissions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    object_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    grantee_type: Mapped[str] = mapped_column(String(32), nullable=False)
    grantee_id: Mapped[str] = mapped_column(String(64), nullable=False)
    permission_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class CollaborationTaskRow(Base):
    __tablename__ = "collaboration_tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    root_task_id: Mapped[str | None] = mapped_column(String(64), index=True)
    parent_task_id: Mapped[str | None] = mapped_column(String(64))
    depth: Mapped[int] = mapped_column(Integer, nullable=False)
    max_depth: Mapped[int] = mapped_column(Integer, nullable=False)
    source_assistant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_assistant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    input_object_id: Mapped[str] = mapped_column(String(64), nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    approval_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    approval_id: Mapped[str | None] = mapped_column(String(64))
    result_object_id: Mapped[str | None] = mapped_column(String(64))
    result_workspace_file_id: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    budget_limit: Mapped[int | None] = mapped_column(Integer)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_reason_code: Mapped[str | None] = mapped_column(String(64))
    failed_reason_text: Mapped[str | None] = mapped_column(Text)
    meta_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class CollaborationClaimRow(Base):
    __tablename__ = "collaboration_claims"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    assistant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_token: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class CollaborationEventRow(Base):
    __tablename__ = "collaboration_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    detail_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SqlAlchemyCollaborationRepository:
    def __init__(self, url: str) -> None:
        self.url = url
        self._engine: Engine | None = None
        self._session_factory: sessionmaker[Session] | None = None
        self._ready_lock = threading.Lock()

    def _create_engine(self) -> Engine:
        settings = get_settings()
        options: dict[str, Any] = {
            "future": True,
            "pool_pre_ping": True,
        }
        if self.url.startswith("mysql+"):
            options.update(
                {
                    "pool_size": settings.db_pool_size,
                    "max_overflow": settings.db_max_overflow,
                    "pool_timeout": settings.db_pool_timeout_seconds,
                    "pool_recycle": settings.db_pool_recycle_seconds,
                    "connect_args": {
                        "connect_timeout": settings.db_connect_timeout_seconds,
                    },
                }
            )
        elif self.url.startswith("sqlite+"):
            options["connect_args"] = {
                "timeout": settings.db_pool_timeout_seconds,
            }
        try:
            return create_engine(self.url, **options)
        except ModuleNotFoundError as exc:
            if self.url.startswith("mysql+") and exc.name == "pymysql":
                raise RuntimeError(
                    "MySQL driver pymysql is required for collaboration repository"
                ) from exc
            raise

    def _ensure_ready(self) -> None:
        if self._engine is not None:
            return
        with self._ready_lock:
            if self._engine is not None:
                return
            engine = self._create_engine()
            settings = get_settings()
            if (
                settings.collaboration_auto_create_tables
                and settings.env.strip().lower() in PRODUCTION_ENV_NAMES
            ):
                raise RuntimeError(
                    "collaboration_auto_create_tables must be disabled in production"
                )
            if settings.collaboration_auto_create_tables:
                Base.metadata.create_all(engine)
            self._engine = engine
            self._session_factory = sessionmaker(
                bind=engine,
                expire_on_commit=False,
                future=True,
            )

    def check_ready(self) -> dict[str, Any]:
        self._ensure_ready()
        if self._engine is None:
            raise RuntimeError("collaboration repository engine is not initialized")
        settings = get_settings()
        with self._engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            inspector = inspect(connection)
            missing_tables: list[str] = []
            missing_columns: dict[str, list[str]] = {}
            for table_name, table in Base.metadata.tables.items():
                if not inspector.has_table(table_name):
                    missing_tables.append(table_name)
                    continue
                existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
                expected_columns = set(table.columns.keys())
                table_missing = sorted(expected_columns - existing_columns)
                if table_missing:
                    missing_columns[table_name] = table_missing
            if missing_tables or missing_columns:
                raise RuntimeError(
                    "collaboration repository schema is not ready: "
                    f"missing_tables={missing_tables}, missing_columns={missing_columns}"
                )
            missing_indexes, malformed_indexes = self._check_required_indexes(inspector)
            if missing_indexes or malformed_indexes:
                raise RuntimeError(
                    "collaboration repository schema is not ready: "
                    f"missing_indexes={missing_indexes}, malformed_indexes={malformed_indexes}"
                )
            alembic_revision: str | None = None
            if (
                not settings.collaboration_auto_create_tables
                or settings.env.strip().lower() in PRODUCTION_ENV_NAMES
            ):
                alembic_revision = self._check_alembic_revision(connection, inspector)
        return {
            "status": "ok",
            "tables_checked": len(Base.metadata.tables),
            "indexes_checked": len(REQUIRED_UNIQUE_INDEXES),
            "alembic_revision": alembic_revision,
        }

    def _check_required_indexes(self, inspector: Any) -> tuple[list[str], dict[str, str]]:
        missing: list[str] = []
        malformed: dict[str, str] = {}
        for index_name, (table_name, columns, unique) in REQUIRED_UNIQUE_INDEXES.items():
            indexes = list(inspector.get_indexes(table_name))
            constraints = list(inspector.get_unique_constraints(table_name))
            named = next(
                (item for item in [*indexes, *constraints] if item.get("name") == index_name),
                None,
            )
            if named is not None:
                if not self._index_definition_matches(named, columns, unique):
                    malformed[index_name] = (
                        f"expected columns={columns} unique={unique}, got {named}"
                    )
                continue
            matching_definition = any(
                self._index_definition_matches(item, columns, unique)
                for item in [*indexes, *constraints]
            )
            if not matching_definition:
                missing.append(index_name)
        return missing, malformed

    @staticmethod
    def _index_definition_matches(item: dict[str, Any], columns: list[str], unique: bool) -> bool:
        column_names = item.get("column_names") or item.get("columns") or []
        if list(column_names) != columns:
            return False
        if unique:
            return bool(item.get("unique", True))
        return True

    def _check_alembic_revision(self, connection: Any, inspector: Any) -> str:
        if not inspector.has_table("alembic_version"):
            raise RuntimeError(
                "collaboration repository schema is not ready: alembic_version missing"
            )
        revisions = [
            str(row[0])
            for row in connection.execute(text("SELECT version_num FROM alembic_version")).all()
        ]
        if not _alembic_revision_satisfies_required(revisions):
            raise RuntimeError(
                "collaboration repository schema is not ready: "
                f"alembic_revision={revisions}, required={REQUIRED_ALEMBIC_REVISION}"
            )
        return ",".join(revisions)

    def dispose(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
            self._session_factory = None

    def _session(self) -> Session:
        self._ensure_ready()
        if self._session_factory is None:
            raise RuntimeError("collaboration repository session factory is not initialized")
        return self._session_factory()

    def load_state(self) -> CollaborationState:
        with self._session() as session:
            objects = [
                self._object_from_row(row)
                for row in session.scalars(
                    select(SharedWorkspaceObjectRow).order_by(SharedWorkspaceObjectRow.created_at)
                ).all()
            ]
            permissions = [
                self._permission_from_row(row)
                for row in session.scalars(
                    select(SharedObjectPermissionRow).order_by(SharedObjectPermissionRow.created_at)
                ).all()
            ]
            tasks = [
                self._task_from_row(row)
                for row in session.scalars(
                    select(CollaborationTaskRow).order_by(CollaborationTaskRow.created_at)
                ).all()
            ]
            claims = [
                self._claim_from_row(row)
                for row in session.scalars(
                    select(CollaborationClaimRow).order_by(CollaborationClaimRow.claimed_at)
                ).all()
            ]
            events = [
                self._event_from_row(row)
                for row in session.scalars(
                    select(CollaborationEventRow).order_by(CollaborationEventRow.created_at)
                ).all()
            ]
        return CollaborationState(
            objects=objects,
            permissions=permissions,
            tasks=tasks,
            claims=claims,
            events=events,
        )

    def get_task(self, task_id: str) -> CollaborationTask:
        with self._session() as session:
            row = session.get(CollaborationTaskRow, task_id.strip())
            if row is None:
                raise ValueError("collaboration task not found")
            return self._task_from_row(row)

    def create_shared_object_direct(
        self,
        *,
        item: SharedWorkspaceObject,
        source_permission: SharedObjectPermission,
    ) -> SharedWorkspaceObject:
        with self._session() as session:
            with session.begin():
                if item.idempotency_key:
                    existing = session.scalars(
                        select(SharedWorkspaceObjectRow)
                        .where(SharedWorkspaceObjectRow.idempotency_key == item.idempotency_key)
                        .with_for_update()
                    ).first()
                    if existing is not None:
                        return self._object_from_row(existing)

                row = SharedWorkspaceObjectRow()
                self._apply_object(row, item)
                session.add(row)
                self._grant_permission_row_if_missing(session, source_permission)
            return item

    def create_task_direct(
        self,
        *,
        task: CollaborationTask,
        target_permission: SharedObjectPermission | None = None,
    ) -> CollaborationTask:
        with self._session() as session:
            with session.begin():
                item = session.scalars(
                    select(SharedWorkspaceObjectRow)
                    .where(SharedWorkspaceObjectRow.id == task.input_object_id)
                    .with_for_update()
                ).first()
                if item is None:
                    raise ValueError("input shared object not found")
                if item.status != "active":
                    raise ValueError("input shared object is not active")
                if not self._has_active_permission_row(
                    session,
                    object_id=item.id,
                    assistant_id=task.source_assistant_id,
                ):
                    raise ValueError("source assistant is not authorized to share this object")

                if task.idempotency_key:
                    existing = session.scalars(
                        select(CollaborationTaskRow)
                        .where(CollaborationTaskRow.idempotency_key == task.idempotency_key)
                        .with_for_update()
                    ).first()
                    if existing is not None:
                        if (
                            existing.status == "failed"
                            and existing.failed_reason_code in RETRYABLE_TASK_FAILURE_CODES
                        ):
                            self._reset_retryable_task_row(
                                session,
                                existing,
                                reason="retryable_failure",
                            )
                        elif existing.status in {"claimed", "running"}:
                            active_claim = self._active_claim_row_for_task(session, existing.id)
                            if active_claim is None:
                                self._reset_retryable_task_row(
                                    session,
                                    existing,
                                    reason="claim_expired",
                                )
                        return self._task_from_row(existing)

                row = CollaborationTaskRow()
                self._apply_task(row, task)
                session.add(row)
                self._add_event_row(
                    session,
                    CollaborationEvent(
                        task_id=task.id,
                        event_type="created",
                        actor_type="assistant",
                        actor_id=task.source_assistant_id,
                        detail_json={
                            "input_object_id": task.input_object_id,
                            "target_assistant_id": task.target_assistant_id,
                        },
                    ),
                )
                if target_permission is not None:
                    self._grant_permission_row_if_missing(session, target_permission)
            return task

    def claim_task(
        self,
        *,
        task_id: str,
        assistant_id: str,
        claim_lease_seconds: int,
    ) -> CollaborationClaim:
        task_id = task_id.strip()
        assistant_id = assistant_id.strip()
        with self._session() as session:
            with session.begin():
                task = self._task_row_for_update(session, task_id)
                if task is None:
                    raise ValueError("collaboration task not found")
                if task.target_assistant_id != assistant_id:
                    raise ValueError("assistant is not authorized to claim this task")
                now = datetime.now(UTC)
                active_claim = self._active_claim_row_for_task(session, task.id)
                if active_claim is not None:
                    if (
                        task.status in {"claimed", "running"}
                        and active_claim.assistant_id == assistant_id
                    ):
                        active_claim.lease_expires_at = now + timedelta(
                            seconds=claim_lease_seconds
                        )
                        return self._claim_from_row(active_claim)
                    raise ValueError("collaboration task is already claimed")
                if task.status != "pending_claim":
                    if not (
                        task.status == "claimed"
                        and task.started_at is None
                        and task.completed_at is None
                        and task.failed_at is None
                    ):
                        raise ValueError("collaboration task is not claimable")

                claim = CollaborationClaim(
                    task_id=task.id,
                    assistant_id=assistant_id,
                    lease_expires_at=now + timedelta(seconds=claim_lease_seconds),
                )
                task.status = "claimed"
                task.accepted_at = now
                claim_row = CollaborationClaimRow()
                self._apply_claim(claim_row, claim)
                session.add(claim_row)
                self._add_event_row(
                    session,
                    CollaborationEvent(
                        task_id=task.id,
                        event_type="claimed",
                        actor_type="assistant",
                        actor_id=assistant_id,
                        detail_json={"claim_id": claim.id},
                    ),
                )
            return claim

    def renew_claim(
        self,
        *,
        task_id: str,
        assistant_id: str,
        claim_token: str,
        claim_lease_seconds: int,
    ) -> CollaborationClaim:
        with self._session() as session:
            with session.begin():
                task, claim = self._require_active_claim_row(
                    session,
                    task_id=task_id,
                    assistant_id=assistant_id,
                    claim_token=claim_token,
                )
                if task.status not in {"claimed", "running"}:
                    raise ValueError("collaboration task claim cannot be renewed")
                claim.lease_expires_at = datetime.now(UTC) + timedelta(seconds=claim_lease_seconds)
            return self._claim_from_row(claim)

    def start_task(self, *, task_id: str, assistant_id: str, claim_token: str) -> CollaborationTask:
        assistant_id = assistant_id.strip()
        with self._session() as session:
            with session.begin():
                task, _claim = self._require_active_claim_row(
                    session,
                    task_id=task_id,
                    assistant_id=assistant_id,
                    claim_token=claim_token,
                )
                if task.status not in {"claimed", "running"}:
                    raise ValueError("collaboration task cannot be started")
                if task.status != "running":
                    task.status = "running"
                    task.started_at = datetime.now(UTC)
                    self._add_event_row(
                        session,
                        CollaborationEvent(
                            task_id=task.id,
                            event_type="started",
                            actor_type="assistant",
                            actor_id=assistant_id,
                        ),
                    )
            return self._task_from_row(task)

    def complete_task(
        self,
        *,
        task_id: str,
        assistant_id: str,
        claim_token: str,
        result_object_id: str | None = None,
        result_workspace_file_id: str | None = None,
        result_workspace_file_ids: list[str] | None = None,
    ) -> CollaborationTask:
        assistant_id = assistant_id.strip()
        with self._session() as session:
            with session.begin():
                task, claim = self._require_active_claim_row(
                    session,
                    task_id=task_id,
                    assistant_id=assistant_id,
                    claim_token=claim_token,
                )
                if task.status not in {"claimed", "running"}:
                    raise ValueError("collaboration task cannot be completed")
                normalized_file_ids = self._normalize_result_workspace_file_ids(
                    result_workspace_file_id,
                    result_workspace_file_ids,
                )
                if normalized_file_ids and result_workspace_file_id is None:
                    result_workspace_file_id = normalized_file_ids[0]
                if result_object_id is None and result_workspace_file_id is None:
                    raise ValueError("completion requires a result object or workspace file")
                if result_object_id is not None and session.get(
                    SharedWorkspaceObjectRow,
                    result_object_id,
                ) is None:
                    raise ValueError("result shared object not found")
                meta_json = dict(task.meta_json or {})
                if normalized_file_ids:
                    meta_json["runtime_result_workspace_file_ids"] = normalized_file_ids
                    meta_json["runtime_result_file_count"] = len(normalized_file_ids)
                else:
                    meta_json.pop("runtime_result_workspace_file_ids", None)
                    meta_json["runtime_result_file_count"] = 0
                task.status = "completed"
                task.completed_at = datetime.now(UTC)
                task.result_object_id = result_object_id
                task.result_workspace_file_id = result_workspace_file_id
                task.meta_json = meta_json
                claim.status = "released"
                self._add_event_row(
                    session,
                    CollaborationEvent(
                        task_id=task.id,
                        event_type="completed",
                        actor_type="assistant",
                        actor_id=assistant_id,
                        detail_json={
                            "result_object_id": result_object_id,
                            "result_workspace_file_id": result_workspace_file_id,
                            "result_workspace_file_ids": normalized_file_ids,
                        },
                    ),
                )
            return self._task_from_row(task)

    def fail_task(
        self,
        *,
        task_id: str,
        assistant_id: str,
        claim_token: str,
        failed_reason_code: str,
        failed_reason_text: str | None = None,
    ) -> CollaborationTask:
        assistant_id = assistant_id.strip()
        with self._session() as session:
            with session.begin():
                task, claim = self._require_active_claim_row(
                    session,
                    task_id=task_id,
                    assistant_id=assistant_id,
                    claim_token=claim_token,
                )
                if task.status not in {"claimed", "running"}:
                    raise ValueError("collaboration task cannot be failed")
                task.status = "failed"
                task.failed_at = datetime.now(UTC)
                task.failed_reason_code = failed_reason_code.strip()
                task.failed_reason_text = (failed_reason_text or "").strip() or None
                claim.status = "released"
                self._add_event_row(
                    session,
                    CollaborationEvent(
                        task_id=task.id,
                        event_type="failed",
                        actor_type="assistant",
                        actor_id=assistant_id,
                        detail_json={
                            "failed_reason_code": task.failed_reason_code,
                            "failed_reason_text": task.failed_reason_text,
                        },
                    ),
                )
            return self._task_from_row(task)

    def cancel_task(
        self,
        *,
        task_id: str,
        actor_id: str = "system",
        reason: str | None = None,
    ) -> CollaborationTask:
        with self._session() as session:
            with session.begin():
                task = self._task_row_for_update(session, task_id.strip())
                if task is None:
                    raise ValueError("collaboration task not found")
                if task.status in {"completed", "cancelled", "failed", "expired", "rejected"}:
                    raise ValueError("collaboration task can no longer be cancelled")
                task.status = "cancelled"
                task.failed_reason_code = "cancelled"
                task.failed_reason_text = (reason or "").strip() or None
                task.failed_at = datetime.now(UTC)
                active_claim = self._active_claim_row_for_task(session, task.id)
                if active_claim is not None:
                    active_claim.status = "released"
                self._add_event_row(
                    session,
                    CollaborationEvent(
                        task_id=task.id,
                        event_type="cancelled",
                        actor_type="system",
                        actor_id=actor_id,
                        detail_json={"reason": task.failed_reason_text},
                    ),
                )
            return self._task_from_row(task)

    def save_state(self, state: CollaborationState) -> None:
        with self._session() as session:
            self._replace_rows(
                session,
                SharedWorkspaceObjectRow,
                state.objects,
                self._apply_object,
            )
            self._replace_rows(
                session,
                SharedObjectPermissionRow,
                state.permissions,
                self._apply_permission,
            )
            self._replace_rows(
                session,
                CollaborationTaskRow,
                state.tasks,
                self._apply_task,
            )
            self._replace_rows(
                session,
                CollaborationClaimRow,
                state.claims,
                self._apply_claim,
            )
            self._replace_rows(
                session,
                CollaborationEventRow,
                state.events,
                self._apply_event,
            )
            session.commit()

    def _task_row_for_update(
        self,
        session: Session,
        task_id: str,
    ) -> CollaborationTaskRow | None:
        return session.scalars(
            select(CollaborationTaskRow)
            .where(CollaborationTaskRow.id == task_id)
            .with_for_update()
        ).first()

    def _active_claim_row_for_task(
        self,
        session: Session,
        task_id: str,
    ) -> CollaborationClaimRow | None:
        now = datetime.now(UTC)
        rows = session.scalars(
            select(CollaborationClaimRow)
            .where(
                CollaborationClaimRow.task_id == task_id,
                CollaborationClaimRow.status == "active",
            )
            .order_by(CollaborationClaimRow.claimed_at)
            .with_for_update()
        ).all()
        active: CollaborationClaimRow | None = None
        for row in rows:
            lease_expires_at = self._ensure_utc_required(row.lease_expires_at)
            if lease_expires_at <= now:
                row.status = "expired"
                continue
            if active is None:
                active = row
        return active

    def _require_active_claim_row(
        self,
        session: Session,
        *,
        task_id: str,
        assistant_id: str,
        claim_token: str,
    ) -> tuple[CollaborationTaskRow, CollaborationClaimRow]:
        task = self._task_row_for_update(session, task_id.strip())
        if task is None:
            raise ValueError("collaboration task not found")
        if task.status in {"completed", "cancelled", "expired", "failed", "rejected"}:
            raise ValueError("collaboration task can no longer accept execution updates")
        claim = self._active_claim_row_for_task(session, task.id)
        if claim is None:
            raise ValueError("active claim not found")
        if claim.assistant_id != assistant_id.strip():
            raise ValueError("assistant is not authorized to use this claim")
        if claim.claim_token != claim_token.strip():
            raise ValueError("claim token is invalid")
        return task, claim

    def _has_active_permission_row(
        self,
        session: Session,
        *,
        object_id: str,
        assistant_id: str,
    ) -> bool:
        now = datetime.now(UTC)
        row = session.scalars(
            select(SharedObjectPermissionRow)
            .where(
                SharedObjectPermissionRow.object_id == object_id,
                SharedObjectPermissionRow.grantee_type == "assistant",
                SharedObjectPermissionRow.grantee_id == assistant_id.strip(),
                SharedObjectPermissionRow.permission_type == "read",
                SharedObjectPermissionRow.status == "active",
            )
            .with_for_update()
        ).first()
        if row is None:
            return False
        expires_at = self._ensure_utc(row.expires_at)
        return expires_at is None or expires_at > now

    def _grant_permission_row_if_missing(
        self,
        session: Session,
        permission: SharedObjectPermission,
    ) -> SharedObjectPermissionRow:
        now = datetime.now(UTC)
        rows = session.scalars(
            select(SharedObjectPermissionRow)
            .where(
                SharedObjectPermissionRow.object_id == permission.object_id,
                SharedObjectPermissionRow.grantee_type == permission.grantee_type,
                SharedObjectPermissionRow.grantee_id == permission.grantee_id,
                SharedObjectPermissionRow.permission_type == permission.permission_type,
                SharedObjectPermissionRow.status == "active",
            )
            .with_for_update()
        ).all()
        for row in rows:
            expires_at = self._ensure_utc(row.expires_at)
            if expires_at is None or expires_at > now:
                return row
        row = SharedObjectPermissionRow()
        self._apply_permission(row, permission)
        session.add(row)
        return row

    def _reset_retryable_task_row(
        self,
        session: Session,
        task: CollaborationTaskRow,
        *,
        reason: str,
    ) -> None:
        active_claim = self._active_claim_row_for_task(session, task.id)
        if active_claim is not None:
            active_claim.status = "released"
        task.status = "pending_claim"
        task.accepted_at = None
        task.started_at = None
        task.completed_at = None
        task.failed_at = None
        task.failed_reason_code = None
        task.failed_reason_text = None
        task.result_object_id = None
        task.result_workspace_file_id = None
        self._add_event_row(
            session,
            CollaborationEvent(
                task_id=task.id,
                event_type="retry_scheduled",
                actor_type="system",
                actor_id="go-gateway",
                detail_json={"idempotency_key": task.idempotency_key, "reason": reason},
            ),
        )

    def _add_event_row(self, session: Session, event: CollaborationEvent) -> None:
        row = CollaborationEventRow()
        self._apply_event(row, event)
        session.add(row)

    @staticmethod
    def _normalize_result_workspace_file_ids(
        primary: str | None,
        file_ids: list[str] | None,
    ) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in [primary, *(file_ids or [])]:
            normalized = str(value or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            out.append(normalized)
        return out

    def _replace_rows(
        self,
        session: Session,
        row_cls: type[Any],
        items: Iterable[Any],
        apply_fn: Any,
    ) -> None:
        existing_rows: dict[str, Any] = {
            cast(str, row.id): row for row in session.scalars(select(row_cls)).all()
        }
        keep_ids: set[str] = set()
        for item in items:
            keep_ids.add(item.id)
            row = existing_rows.get(item.id)
            if row is None:
                row = row_cls()  # type: ignore[call-arg]
                session.add(row)
            apply_fn(row, item)
        for row_id, row in existing_rows.items():
            if row_id not in keep_ids:
                session.delete(row)

    def _apply_object(self, row: SharedWorkspaceObjectRow, item: SharedWorkspaceObject) -> None:
        row.id = item.id
        row.object_type = item.object_type
        row.filename = item.filename
        row.content_type = item.content_type
        row.size_bytes = item.size_bytes
        row.sha256 = item.sha256
        row.storage_backend = item.storage_backend
        row.storage_bucket = item.storage_bucket
        row.storage_region = item.storage_region
        row.storage_endpoint = item.storage_endpoint
        row.storage_key = item.storage_key
        row.storage_etag = item.storage_etag
        row.storage_version_id = item.storage_version_id
        row.source_assistant_id = item.source_assistant_id
        row.source_workspace_file_id = item.source_workspace_file_id
        row.parent_object_id = item.parent_object_id
        row.derived_from_task_id = item.derived_from_task_id
        row.visibility_scope = item.visibility_scope
        row.access_mode = item.access_mode
        row.retention_policy = item.retention_policy
        row.expires_at = item.expires_at
        row.last_accessed_at = item.last_accessed_at
        row.status = item.status
        row.created_by = item.created_by
        row.created_at = item.created_at
        row.deleted_at = item.deleted_at
        row.idempotency_key = item.idempotency_key
        row.meta_json = item.meta_json

    def _apply_permission(
        self,
        row: SharedObjectPermissionRow,
        item: SharedObjectPermission,
    ) -> None:
        row.id = item.id
        row.object_id = item.object_id
        row.grantee_type = item.grantee_type
        row.grantee_id = item.grantee_id
        row.permission_type = item.permission_type
        row.created_by = item.created_by
        row.created_at = item.created_at
        row.expires_at = item.expires_at
        row.status = item.status

    def _apply_task(self, row: CollaborationTaskRow, item: CollaborationTask) -> None:
        row.id = item.id
        row.root_task_id = item.root_task_id
        row.parent_task_id = item.parent_task_id
        row.depth = item.depth
        row.max_depth = item.max_depth
        row.source_assistant_id = item.source_assistant_id
        row.target_assistant_id = item.target_assistant_id
        row.input_object_id = item.input_object_id
        row.instruction = item.instruction
        row.status = item.status
        row.approval_mode = item.approval_mode
        row.approval_id = item.approval_id
        row.result_object_id = item.result_object_id
        row.result_workspace_file_id = item.result_workspace_file_id
        row.idempotency_key = item.idempotency_key
        row.budget_limit = item.budget_limit
        row.timeout_seconds = item.timeout_seconds
        row.created_by = item.created_by
        row.created_at = item.created_at
        row.accepted_at = item.accepted_at
        row.started_at = item.started_at
        row.completed_at = item.completed_at
        row.failed_at = item.failed_at
        row.failed_reason_code = item.failed_reason_code
        row.failed_reason_text = item.failed_reason_text
        row.meta_json = item.meta_json

    def _apply_claim(self, row: CollaborationClaimRow, item: CollaborationClaim) -> None:
        row.id = item.id
        row.task_id = item.task_id
        row.assistant_id = item.assistant_id
        row.claim_token = item.claim_token
        row.claimed_at = item.claimed_at
        row.lease_expires_at = item.lease_expires_at
        row.status = item.status

    def _apply_event(self, row: CollaborationEventRow, item: CollaborationEvent) -> None:
        row.id = item.id
        row.task_id = item.task_id
        row.event_type = item.event_type
        row.actor_type = item.actor_type
        row.actor_id = item.actor_id
        row.detail_json = item.detail_json
        row.created_at = item.created_at

    def _object_from_row(self, row: SharedWorkspaceObjectRow) -> SharedWorkspaceObject:
        meta_json = row.meta_json or {}
        return SharedWorkspaceObject(
            id=row.id,
            object_type=row.object_type,
            filename=row.filename,
            content_type=row.content_type,
            size_bytes=row.size_bytes,
            sha256=row.sha256,
            storage_backend=row.storage_backend,
            storage_bucket=row.storage_bucket,
            storage_region=row.storage_region,
            storage_endpoint=row.storage_endpoint,
            storage_key=row.storage_key,
            storage_etag=row.storage_etag,
            storage_version_id=row.storage_version_id,
            source_assistant_id=row.source_assistant_id,
            source_workspace_file_id=row.source_workspace_file_id,
            parent_object_id=row.parent_object_id,
            derived_from_task_id=row.derived_from_task_id,
            visibility_scope=cast(SharedObjectVisibilityScope, row.visibility_scope),
            access_mode=cast(SharedObjectAccessMode, row.access_mode),
            retention_policy=cast(SharedObjectRetentionPolicy, row.retention_policy),
            expires_at=self._ensure_utc(row.expires_at),
            last_accessed_at=self._ensure_utc(row.last_accessed_at),
            status=cast(SharedObjectStatusName, row.status),
            created_by=row.created_by,
            created_at=self._ensure_utc_required(row.created_at),
            deleted_at=self._ensure_utc(row.deleted_at),
            idempotency_key=row.idempotency_key,
            meta_json=meta_json,
        )

    def _permission_from_row(self, row: SharedObjectPermissionRow) -> SharedObjectPermission:
        return SharedObjectPermission(
            id=row.id,
            object_id=row.object_id,
            grantee_type=cast(SharedObjectGranteeType, row.grantee_type),
            grantee_id=row.grantee_id,
            permission_type=cast(SharedObjectPermissionType, row.permission_type),
            created_by=row.created_by,
            created_at=self._ensure_utc_required(row.created_at),
            expires_at=self._ensure_utc(row.expires_at),
            status=cast(SharedObjectPermissionStatusName, row.status),
        )

    def _task_from_row(self, row: CollaborationTaskRow) -> CollaborationTask:
        return CollaborationTask(
            id=row.id,
            root_task_id=row.root_task_id,
            parent_task_id=row.parent_task_id,
            depth=row.depth,
            max_depth=row.max_depth,
            source_assistant_id=row.source_assistant_id,
            target_assistant_id=row.target_assistant_id,
            input_object_id=row.input_object_id,
            instruction=row.instruction,
            status=cast(CollaborationTaskStatusName, row.status),
            approval_mode=cast(CollaborationApprovalMode, row.approval_mode),
            approval_id=row.approval_id,
            result_object_id=row.result_object_id,
            result_workspace_file_id=row.result_workspace_file_id,
            idempotency_key=row.idempotency_key,
            budget_limit=row.budget_limit,
            timeout_seconds=row.timeout_seconds,
            created_by=row.created_by,
            created_at=self._ensure_utc_required(row.created_at),
            accepted_at=self._ensure_utc(row.accepted_at),
            started_at=self._ensure_utc(row.started_at),
            completed_at=self._ensure_utc(row.completed_at),
            failed_at=self._ensure_utc(row.failed_at),
            failed_reason_code=row.failed_reason_code,
            failed_reason_text=row.failed_reason_text,
            meta_json=row.meta_json or {},
        )

    def _claim_from_row(self, row: CollaborationClaimRow) -> CollaborationClaim:
        return CollaborationClaim(
            id=row.id,
            task_id=row.task_id,
            assistant_id=row.assistant_id,
            claim_token=row.claim_token,
            claimed_at=self._ensure_utc_required(row.claimed_at),
            lease_expires_at=self._ensure_utc_required(row.lease_expires_at),
            status=cast(CollaborationClaimStatusName, row.status),
        )

    def _event_from_row(self, row: CollaborationEventRow) -> CollaborationEvent:
        return CollaborationEvent(
            id=row.id,
            task_id=row.task_id,
            event_type=row.event_type,
            actor_type=cast(CollaborationActorType, row.actor_type),
            actor_id=row.actor_id,
            detail_json=row.detail_json or {},
            created_at=self._ensure_utc_required(row.created_at),
        )

    def _ensure_utc(self, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is not None:
            return value
        return value.replace(tzinfo=UTC)

    def _ensure_utc_required(self, value: datetime | None) -> datetime:
        if value is None:
            raise RuntimeError("database row is missing a required datetime value")
        return self._ensure_utc(value)  # type: ignore[return-value]
