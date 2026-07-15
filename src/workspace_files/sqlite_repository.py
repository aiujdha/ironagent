from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from src.shared.config import get_settings
from src.workspace_files.models import TargetBucket, WorkspaceFile, WorkspaceFileEvent


class SQLiteWorkspaceFileStore:
    """SQLite + WAL store shared by workspace file repositories.

    The database intentionally keeps the same logical fields as the current
    MySQL-backed workspace_files and workspace_file_events records. Multiple
    assistant runtimes can share this file and isolate rows by assistant_id.
    """

    _schema_lock = threading.Lock()
    _initialized_paths: set[Path] = set()

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        busy_timeout_ms: int | None = None,
        cache_size_mb: int | None = None,
        wal_autocheckpoint: int | None = None,
        ensure_schema: bool = True,
    ) -> None:
        settings = get_settings()
        self.db_path = db_path or settings.workspace_file_sqlite_path
        self.busy_timeout_ms = busy_timeout_ms or settings.workspace_file_sqlite_busy_timeout_ms
        self.cache_size_mb = cache_size_mb or settings.workspace_file_sqlite_cache_size_mb
        self.wal_autocheckpoint = (
            wal_autocheckpoint or settings.workspace_file_sqlite_wal_autocheckpoint
        )
        if ensure_schema:
            self._ensure_schema()

    def connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            uri = f"file:{self.db_path}?mode=ro"
            conn = sqlite3.connect(
                uri,
                uri=True,
                timeout=self.busy_timeout_ms / 1000,
                isolation_level=None,
            )
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                self.db_path,
                timeout=self.busy_timeout_ms / 1000,
                isolation_level=None,
            )
        conn.row_factory = sqlite3.Row
        self._configure_connection(conn, read_only=read_only)
        return conn

    def _configure_connection(self, conn: sqlite3.Connection, *, read_only: bool) -> None:
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        if not read_only:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(f"PRAGMA wal_autocheckpoint={self.wal_autocheckpoint}")
        cache_kb = max(self.cache_size_mb, 1) * 1024
        conn.execute(f"PRAGMA cache_size=-{cache_kb}")
        conn.execute("PRAGMA foreign_keys=ON")

    def _ensure_schema(self) -> None:
        resolved = self.db_path.resolve()
        with self._schema_lock:
            if resolved in self._initialized_paths:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as conn:
                conn.executescript(_SCHEMA_SQL)
                _migrate_workspace_files_schema(conn)
            self._initialized_paths.add(resolved)


class SQLiteWorkspaceFileRepository:
    def __init__(self, store: SQLiteWorkspaceFileStore | None = None) -> None:
        self.store = store or SQLiteWorkspaceFileStore()

    async def alist_files(
        self,
        *,
        assistant_id: str | None = None,
        bucket: TargetBucket | None = None,
        include_deleted: bool = False,
        limit: int | None = None,
    ) -> list[WorkspaceFile]:
        return await asyncio.to_thread(
            self._list_files,
            assistant_id=assistant_id,
            bucket=bucket,
            include_deleted=include_deleted,
            limit=limit,
        )

    async def alist_recent_files(
        self,
        *,
        assistant_id: str | None = None,
        bucket: TargetBucket | None = None,
        include_deleted: bool = False,
        limit: int = 100,
    ) -> list[WorkspaceFile]:
        return await self.alist_files(
            assistant_id=assistant_id,
            bucket=bucket,
            include_deleted=include_deleted,
            limit=limit,
        )

    async def aget_file(self, file_id: str) -> WorkspaceFile | None:
        return await asyncio.to_thread(self._get_file, file_id)

    async def aget_active_file_by_path(
        self,
        *,
        assistant_id: str,
        bucket: TargetBucket,
        filename: str,
    ) -> WorkspaceFile | None:
        return await asyncio.to_thread(
            self._get_active_file_by_path,
            assistant_id=assistant_id,
            bucket=bucket,
            filename=filename,
        )

    async def asave_file(self, record: WorkspaceFile) -> WorkspaceFile:
        return await asyncio.to_thread(self._save_file, record)

    async def aappend_event(self, event: WorkspaceFileEvent) -> WorkspaceFileEvent:
        return await SQLiteWorkspaceFileEventRepository(self.store).aappend_event(event)

    async def alist_events(
        self,
        *,
        assistant_id: str | None = None,
        file_id: str | None = None,
        limit: int = 100,
    ) -> list[WorkspaceFileEvent]:
        return await SQLiteWorkspaceFileEventRepository(self.store).alist_events(
            assistant_id=assistant_id,
            file_id=file_id,
            limit=limit,
        )

    def _list_files(
        self,
        *,
        assistant_id: str | None,
        bucket: TargetBucket | None,
        include_deleted: bool,
        limit: int | None,
    ) -> list[WorkspaceFile]:
        clauses: list[str] = []
        params: list[Any] = []
        if assistant_id is not None:
            clauses.append("assistant_id = ?")
            params.append(assistant_id)
        if bucket is not None:
            clauses.append("bucket = ?")
            params.append(bucket)
        if not include_deleted:
            clauses.append("deleted = 0")
        sql = "SELECT * FROM workspace_files"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if limit is None:
            sql += " ORDER BY created_at ASC, file_id ASC"
        else:
            sql += " ORDER BY updated_at DESC, file_id DESC LIMIT ?"
            params.append(limit)
        with self.store.connect(read_only=True) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_workspace_file_from_row(row) for row in rows]

    def _get_file(self, file_id: str) -> WorkspaceFile | None:
        trimmed = file_id.strip()
        if not trimmed:
            return None
        with self.store.connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT * FROM workspace_files WHERE file_id = ?",
                (trimmed,),
            ).fetchone()
        return None if row is None else _workspace_file_from_row(row)

    def _get_active_file_by_path(
        self,
        *,
        assistant_id: str,
        bucket: TargetBucket,
        filename: str,
    ) -> WorkspaceFile | None:
        with self.store.connect(read_only=True) as conn:
            row = conn.execute(
                """
                SELECT * FROM workspace_files
                WHERE assistant_id = ? AND bucket = ? AND filename = ? AND deleted = 0
                ORDER BY updated_at DESC, file_id DESC
                LIMIT 1
                """,
                (assistant_id, bucket, filename),
            ).fetchone()
        return None if row is None else _workspace_file_from_row(row)

    def _save_file(self, record: WorkspaceFile) -> WorkspaceFile:
        payload = _workspace_file_to_row(record)
        columns = list(payload)
        placeholders = ", ".join("?" for _ in columns)
        update_columns = [column for column in columns if column != "file_id"]
        updates = ", ".join(f"{column} = excluded.{column}" for column in update_columns)
        values = [payload[column] for column in columns]
        sql = f"""
            INSERT INTO workspace_files ({", ".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT DO UPDATE SET {updates}
        """
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(sql, values)
                saved_row = _fetch_saved_workspace_file(conn, payload)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return _workspace_file_from_row(saved_row) if saved_row is not None else record


class SQLiteWorkspaceFileEventRepository:
    def __init__(self, store: SQLiteWorkspaceFileStore | None = None) -> None:
        self.store = store or SQLiteWorkspaceFileStore()

    async def aappend_event(self, event: WorkspaceFileEvent) -> WorkspaceFileEvent:
        return await asyncio.to_thread(self._append_event, event)

    async def alist_events(
        self,
        *,
        assistant_id: str | None = None,
        file_id: str | None = None,
        limit: int = 100,
    ) -> list[WorkspaceFileEvent]:
        return await asyncio.to_thread(
            self._list_events,
            assistant_id=assistant_id,
            file_id=file_id,
            limit=limit,
        )

    def _append_event(self, event: WorkspaceFileEvent) -> WorkspaceFileEvent:
        payload = _workspace_file_event_to_row(event)
        columns = list(payload)
        placeholders = ", ".join("?" for _ in columns)
        update_columns = [column for column in columns if column != "event_id"]
        updates = ", ".join(f"{column} = excluded.{column}" for column in update_columns)
        values = [payload[column] for column in columns]
        sql = f"""
            INSERT INTO workspace_file_events ({", ".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(event_id) DO UPDATE SET {updates}
        """
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(sql, values)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return event

    def _list_events(
        self,
        *,
        assistant_id: str | None,
        file_id: str | None,
        limit: int,
    ) -> list[WorkspaceFileEvent]:
        clauses: list[str] = []
        params: list[Any] = []
        if assistant_id is not None:
            clauses.append("assistant_id = ?")
            params.append(assistant_id)
        if file_id is not None:
            clauses.append("file_id = ?")
            params.append(file_id)
        sql = "SELECT * FROM workspace_file_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, event_id DESC LIMIT ?"
        params.append(limit)
        with self.store.connect(read_only=True) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_workspace_file_event_from_row(row) for row in rows]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _bool_to_int(value: bool) -> int:
    return 1 if value else 0


def _parse_json_object(value: str | bytes | None) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}


def _row_value(row: sqlite3.Row, name: str, default: Any = "") -> Any:
    return row[name] if name in row.keys() else default


def _migrate_workspace_files_schema(conn: sqlite3.Connection) -> None:
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(workspace_files)").fetchall()
    }
    if "scan_fingerprint" not in columns:
        conn.execute(
            "ALTER TABLE workspace_files "
            "ADD COLUMN scan_fingerprint TEXT NOT NULL DEFAULT ''"
        )
    if "owner_user_id" not in columns:
        conn.execute(
            "ALTER TABLE workspace_files "
            "ADD COLUMN owner_user_id TEXT NOT NULL DEFAULT ''"
        )
    _deduplicate_active_workspace_file_rows(conn)
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_files_active_path_unique
            ON workspace_files(assistant_id, bucket, filename)
            WHERE deleted = 0
        """
    )


def _deduplicate_active_workspace_file_rows(conn: sqlite3.Connection) -> None:
    conn.execute(
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
            updated_at = strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
        WHERE file_id IN (
            SELECT file_id FROM ranked WHERE row_rank > 1
        )
        """
    )


def _fetch_saved_workspace_file(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
) -> sqlite3.Row | None:
    if not payload["deleted"]:
        row = conn.execute(
            """
            SELECT * FROM workspace_files
            WHERE assistant_id = ? AND bucket = ? AND filename = ? AND deleted = 0
            ORDER BY updated_at DESC, file_id DESC
            LIMIT 1
            """,
            (payload["assistant_id"], payload["bucket"], payload["filename"]),
        ).fetchone()
        if row is not None:
            return row
    return conn.execute(
        "SELECT * FROM workspace_files WHERE file_id = ?",
        (payload["file_id"],),
    ).fetchone()


def _workspace_file_to_row(record: WorkspaceFile) -> dict[str, Any]:
    storage_key = record.storage_key or record.object_key
    storage_path = record.storage_path or record.relative_path
    object_key = record.object_key or storage_key
    object_url = record.object_url or storage_path
    storage_status = record.storage_status or record.sync_status
    sync_status = record.sync_status or storage_status
    last_error = record.last_error or record.storage_error
    return {
        "file_id": record.file_id,
        "assistant_id": record.assistant_id,
        "bucket": record.bucket,
        "filename": record.filename,
        "relative_path": record.relative_path,
        "direction": record.direction,
        "file_role": record.file_role,
        "source_file_id": record.source_file_id,
        "owner_user_id": record.owner_user_id,
        "size_bytes": record.size_bytes,
        "content_type": record.content_type,
        "uploader_user_id": record.uploader_user_id,
        "source_type": record.source_type,
        "source_ref": record.source_ref,
        "storage_backend": record.storage_backend,
        "storage_key": storage_key,
        "storage_path": storage_path,
        "storage_status": storage_status,
        "storage_error": record.storage_error or last_error,
        "object_key": object_key,
        "object_url": object_url,
        "checksum_sha256": record.checksum,
        "scan_fingerprint": record.scan_fingerprint,
        "sync_status": sync_status,
        "sync_error": last_error,
        "last_error": last_error,
        "workspace_last_modified_at": _iso(record.workspace_last_modified_at),
        "last_synced_at": _iso(record.last_synced_at),
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
        "deleted": _bool_to_int(record.deleted),
    }


def _workspace_file_from_row(row: sqlite3.Row) -> WorkspaceFile:
    storage_backend = row["storage_backend"]
    if storage_backend == "disk":
        storage_backend = "local"
    object_key = row["object_key"] or row["storage_key"]
    storage_path = row["storage_path"] or row["object_url"]
    object_url = row["object_url"] or storage_path
    checksum = row["checksum_sha256"]
    last_error = row["last_error"] or row["sync_error"] or row["storage_error"]
    storage_status = row["storage_status"] or row["sync_status"]
    sync_status = row["sync_status"] or storage_status
    return WorkspaceFile.model_validate(
        {
            "file_id": row["file_id"],
            "assistant_id": row["assistant_id"],
            "bucket": row["bucket"],
            "filename": row["filename"],
            "relative_path": row["relative_path"],
            "direction": row["direction"],
            "file_role": row["file_role"],
            "source_file_id": row["source_file_id"],
            "owner_user_id": _row_value(row, "owner_user_id", ""),
            "size_bytes": row["size_bytes"],
            "content_type": row["content_type"],
            "uploader_user_id": row["uploader_user_id"],
            "source_type": row["source_type"],
            "source_ref": row["source_ref"],
            "storage_provider": _storage_provider_for_backend(storage_backend),
            "storage_key": row["storage_key"] or object_key,
            "storage_path": storage_path,
            "storage_status": storage_status,
            "storage_error": row["storage_error"] or last_error,
            "storage_backend": storage_backend,
            "object_key": object_key,
            "object_url": object_url,
            "checksum": checksum,
            "scan_fingerprint": _row_value(row, "scan_fingerprint", ""),
            "sync_status": sync_status,
            "last_error": last_error,
            "workspace_last_modified_at": row["workspace_last_modified_at"],
            "last_synced_at": row["last_synced_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "deleted": bool(row["deleted"]),
        }
    )


def _workspace_file_event_to_row(event: WorkspaceFileEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "file_id": event.file_id,
        "assistant_id": event.assistant_id,
        "action": event.action,
        "user_id": event.user_id,
        "source_type": event.source_type,
        "source_ref": event.source_ref,
        "detail": json.dumps(event.detail, ensure_ascii=False, separators=(",", ":")),
        "created_at": _iso(event.created_at),
    }


def _workspace_file_event_from_row(row: sqlite3.Row) -> WorkspaceFileEvent:
    return WorkspaceFileEvent.model_validate(
        {
            "event_id": row["event_id"],
            "file_id": row["file_id"],
            "assistant_id": row["assistant_id"],
            "action": row["action"],
            "user_id": row["user_id"],
            "source_type": row["source_type"],
            "source_ref": row["source_ref"],
            "detail": _parse_json_object(row["detail"]),
            "created_at": row["created_at"],
        }
    )


def _storage_provider_for_backend(storage_backend: str) -> str:
    if storage_backend in {"cos", "obs"}:
        return storage_backend
    return "local"


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workspace_files (
    file_id TEXT PRIMARY KEY,
    assistant_id TEXT NOT NULL,
    bucket TEXT NOT NULL,
    filename TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT 'inbound',
    file_role TEXT NOT NULL DEFAULT 'input',
    source_file_id TEXT,
    owner_user_id TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    content_type TEXT NOT NULL DEFAULT '',
    uploader_user_id TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
    storage_backend TEXT NOT NULL DEFAULT '',
    storage_key TEXT NOT NULL DEFAULT '',
    storage_path TEXT NOT NULL DEFAULT '',
    storage_status TEXT NOT NULL DEFAULT 'pending',
    storage_error TEXT NOT NULL DEFAULT '',
    object_key TEXT NOT NULL DEFAULT '',
    object_url TEXT NOT NULL DEFAULT '',
    checksum_sha256 TEXT NOT NULL DEFAULT '',
    scan_fingerprint TEXT NOT NULL DEFAULT '',
    sync_status TEXT NOT NULL DEFAULT 'pending',
    sync_error TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    workspace_last_modified_at TEXT,
    last_synced_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_workspace_files_assistant_id
    ON workspace_files(assistant_id);
CREATE INDEX IF NOT EXISTS idx_workspace_files_assistant_bucket
    ON workspace_files(assistant_id, bucket);
CREATE INDEX IF NOT EXISTS idx_workspace_files_assistant_deleted_updated
    ON workspace_files(assistant_id, deleted, updated_at);
CREATE INDEX IF NOT EXISTS idx_workspace_files_source_file_id
    ON workspace_files(source_file_id);
CREATE INDEX IF NOT EXISTS idx_workspace_files_storage_status
    ON workspace_files(storage_status);
CREATE INDEX IF NOT EXISTS idx_workspace_files_relative_path
    ON workspace_files(relative_path);

CREATE TABLE IF NOT EXISTS workspace_file_events (
    event_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    assistant_id TEXT NOT NULL,
    action TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workspace_file_events_assistant_created
    ON workspace_file_events(assistant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_workspace_file_events_file_id
    ON workspace_file_events(file_id);
CREATE INDEX IF NOT EXISTS idx_workspace_file_events_action
    ON workspace_file_events(action);
"""


def copy_files_to_sqlite(
    records: Iterable[WorkspaceFile],
    *,
    store: SQLiteWorkspaceFileStore | None = None,
) -> int:
    resolved_store = store or SQLiteWorkspaceFileStore()
    rows = [_workspace_file_to_row(record) for record in records]
    if not rows:
        return 0
    columns = list(rows[0])
    placeholders = ", ".join("?" for _ in columns)
    update_columns = [column for column in columns if column != "file_id"]
    updates = ", ".join(f"{column} = excluded.{column}" for column in update_columns)
    sql = f"""
        INSERT INTO workspace_files ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT DO UPDATE SET {updates}
    """
    values = [[row[column] for column in columns] for row in rows]
    with resolved_store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(sql, values)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return len(rows)


def copy_files_to_sqlite_slow(
    records: Iterable[WorkspaceFile],
    *,
    store: SQLiteWorkspaceFileStore | None = None,
) -> int:
    repo = SQLiteWorkspaceFileRepository(store)
    count = 0
    for record in records:
        repo._save_file(record)
        count += 1
    return count


def copy_events_to_sqlite(
    events: Iterable[WorkspaceFileEvent],
    *,
    store: SQLiteWorkspaceFileStore | None = None,
) -> int:
    resolved_store = store or SQLiteWorkspaceFileStore()
    rows = [_workspace_file_event_to_row(event) for event in events]
    if not rows:
        return 0
    columns = list(rows[0])
    placeholders = ", ".join("?" for _ in columns)
    update_columns = [column for column in columns if column != "event_id"]
    updates = ", ".join(f"{column} = excluded.{column}" for column in update_columns)
    sql = f"""
        INSERT INTO workspace_file_events ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT(event_id) DO UPDATE SET {updates}
    """
    values = [[row[column] for column in columns] for row in rows]
    with resolved_store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(sql, values)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return len(rows)


def copy_events_to_sqlite_slow(
    events: Iterable[WorkspaceFileEvent],
    *,
    store: SQLiteWorkspaceFileStore | None = None,
) -> int:
    repo = SQLiteWorkspaceFileEventRepository(store)
    count = 0
    for event in events:
        repo._append_event(event)
        count += 1
    return count
