from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.shared.blob_storage import CosBlobStorage, cos_key_from_url
from src.shared.config import get_settings, resolve_workspace_file_metadata_backend
from src.shared.database import Base
from src.workspace_files.database_models import WorkspaceFileRow
from src.workspace_files.models import ExternalPushRequest, WorkspaceFile, WorkspaceFileEvent
from src.workspace_files.repository import (
    DatabaseWorkspaceFileRepository,
    JsonExternalPushRequestRepository,
    JsonWorkspaceFileRepository,
    build_external_push_request_repository,
    build_workspace_file_event_repository,
    build_workspace_file_repository,
)
from src.workspace_files.sqlite_repository import (
    SQLiteWorkspaceFileRepository,
    SQLiteWorkspaceFileStore,
)


def _sample_file() -> WorkspaceFile:
    now = datetime.now(UTC)
    return WorkspaceFile(
        file_id="file-1",
        assistant_id="assistant-1",
        bucket="persistent",
        filename="report.pdf",
        relative_path="persistent/report.pdf",
        size_bytes=12,
        content_type="application/pdf",
        owner_user_id="emp001",
        uploader_user_id="emp001",
        source_type="web",
        source_ref="upload-1",
        storage_backend="cos",
        storage_key="assistant-1/persistent/report.pdf",
        storage_url="https://cos.example/report.pdf",
        sync_status="synced",
        origin="web_upload",
        workspace_path="/workspace/persistent/report.pdf",
        created_at=now,
        updated_at=now,
    )


def _sample_event() -> WorkspaceFileEvent:
    return WorkspaceFileEvent(
        event_id="event-1",
        file_id="file-1",
        assistant_id="assistant-1",
        action="upload",
        user_id="emp001",
        detail={"filename": "report.pdf"},
    )


def _sample_push() -> ExternalPushRequest:
    return ExternalPushRequest(
        push_id="push-1",
        user_id="emp001",
        assistant_id="assistant-1",
        filename="report.pdf",
        bucket="persistent",
        content_type="application/pdf",
        storage_backend="cos",
        storage_key="assistant-1/persistent/report.pdf",
        storage_url="https://cos.example/report.pdf",
        status="accepted",
        approval_id="approval-1",
    )


def test_workspace_file_mysql_schema_uses_text_for_legacy_long_paths() -> None:
    for column_name in (
        "filename",
        "relative_path",
        "source_ref",
        "storage_key",
        "storage_path",
        "object_key",
        "object_url",
    ):
        assert isinstance(WorkspaceFileRow.__table__.c[column_name].type, Text)


def test_json_workspace_file_repository_round_trip(tmp_path: Path) -> None:
    repo = JsonWorkspaceFileRepository(
        files_store=tmp_path / "files.json",
        events_store=tmp_path / "events.json",
        push_store=tmp_path / "push.json",
    )

    repo.save_file(_sample_file())
    repo.append_event(_sample_event())
    repo.save_push_request(_sample_push())

    files = repo.list_files(assistant_id="assistant-1")
    events = repo.list_events(file_id="file-1")
    pushes = repo.list_push_requests(assistant_id="assistant-1")

    assert len(files) == 1
    assert files[0].storage_backend == "cos"
    assert len(events) == 1
    assert events[0].detail["filename"] == "report.pdf"
    assert len(pushes) == 1
    assert pushes[0].content_type == "application/pdf"


@pytest.mark.asyncio
async def test_database_workspace_file_repository_round_trip() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    repo = DatabaseWorkspaceFileRepository(session_factory=session_factory)

    await repo.asave_file(_sample_file().model_copy(update={"scan_fingerprint": "db-fp"}))
    await repo.aappend_event(_sample_event())
    await repo.asave_push_request(_sample_push())

    files = await repo.alist_files(assistant_id="assistant-1")
    events = await repo.alist_events(file_id="file-1")
    pushes = await repo.alist_push_requests(assistant_id="assistant-1")
    push_by_approval = await repo.aget_push_request_by_approval("approval-1")

    assert len(files) == 1
    assert files[0].storage_url == "https://cos.example/report.pdf"
    assert files[0].scan_fingerprint == "db-fp"
    assert len(events) == 1
    assert events[0].action == "upload"
    assert len(pushes) == 1
    assert pushes[0].storage_key == "assistant-1/persistent/report.pdf"
    assert push_by_approval is not None
    assert push_by_approval.push_id == "push-1"

    await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_workspace_file_repository_persists_scan_fingerprint(
    tmp_path: Path,
) -> None:
    store = SQLiteWorkspaceFileStore(tmp_path / "workspace-files.db")
    repo = SQLiteWorkspaceFileRepository(store)
    record = _sample_file().model_copy(update={"scan_fingerprint": "size:12:mtime:1"})

    await repo.asave_file(record)

    loaded = await repo.aget_file(record.file_id)
    assert loaded is not None
    assert loaded.scan_fingerprint == "size:12:mtime:1"
    with store.connect(read_only=True) as conn:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(workspace_files)").fetchall()
        }
        indexes = {
            str(row["name"]): int(row["unique"])
            for row in conn.execute("PRAGMA index_list(workspace_files)").fetchall()
        }
    assert "scan_fingerprint" in columns
    assert indexes["idx_workspace_files_active_path_unique"] == 1


@pytest.mark.asyncio
async def test_sqlite_workspace_file_repository_upserts_active_path(
    tmp_path: Path,
) -> None:
    store = SQLiteWorkspaceFileStore(tmp_path / "workspace-files.db")
    repo = SQLiteWorkspaceFileRepository(store)
    first = _sample_file().model_copy(
        update={
            "file_id": "file-a",
            "size_bytes": 12,
            "scan_fingerprint": "first",
        }
    )
    second = _sample_file().model_copy(
        update={
            "file_id": "file-b",
            "size_bytes": 99,
            "scan_fingerprint": "second",
        }
    )

    saved_first = await repo.asave_file(first)
    saved_second = await repo.asave_file(second)

    files = await repo.alist_files(assistant_id="assistant-1", bucket="persistent")
    assert saved_first.file_id == "file-a"
    assert saved_second.file_id == "file-a"
    assert len(files) == 1
    assert files[0].file_id == "file-a"
    assert files[0].size_bytes == 99
    assert files[0].scan_fingerprint == "second"


def test_sqlite_workspace_file_store_migrates_existing_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "workspace-files.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE workspace_files (
                file_id TEXT PRIMARY KEY,
                assistant_id TEXT NOT NULL,
                bucket TEXT NOT NULL,
                filename TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                direction TEXT NOT NULL DEFAULT 'inbound',
                file_role TEXT NOT NULL DEFAULT 'input',
                source_file_id TEXT,
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
                sync_status TEXT NOT NULL DEFAULT 'pending',
                sync_error TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                workspace_last_modified_at TEXT,
                last_synced_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0
            );

            INSERT INTO workspace_files (
                file_id,
                assistant_id,
                bucket,
                filename,
                relative_path,
                created_at,
                updated_at,
                deleted
            )
            VALUES
                (
                    'file-old',
                    'assistant-1',
                    'persistent',
                    'report.pdf',
                    'persistent/report.pdf',
                    '2026-05-22T00:00:00+00:00',
                    '2026-05-22T00:00:00+00:00',
                    0
                ),
                (
                    'file-new',
                    'assistant-1',
                    'persistent',
                    'report.pdf',
                    'persistent/report.pdf',
                    '2026-05-22T00:00:00+00:00',
                    '2026-05-22T01:00:00+00:00',
                    0
                );

            CREATE TABLE workspace_file_events (
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
            """
        )

    store = SQLiteWorkspaceFileStore(db_path)

    with store.connect(read_only=True) as conn:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(workspace_files)").fetchall()
        }
        active_rows = conn.execute(
            """
            SELECT file_id FROM workspace_files
            WHERE assistant_id = 'assistant-1'
                AND bucket = 'persistent'
                AND filename = 'report.pdf'
                AND deleted = 0
            """
        ).fetchall()
        indexes = {
            str(row["name"]): int(row["unique"])
            for row in conn.execute("PRAGMA index_list(workspace_files)").fetchall()
        }
    assert "scan_fingerprint" in columns
    assert "owner_user_id" in columns
    assert [row["file_id"] for row in active_rows] == ["file-new"]
    assert indexes["idx_workspace_files_active_path_unique"] == 1


def test_cos_key_from_url_strips_configured_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IRON_AGENT_COS_BUCKET", "demo-bucket")
    monkeypatch.setenv("IRON_AGENT_COS_REGION", "ap-shanghai")
    monkeypatch.setenv("IRON_AGENT_COS_PREFIX", "heinu")
    get_settings.cache_clear()

    key = cos_key_from_url("https://demo-bucket.cos.ap-shanghai.myqcloud.com/heinu/a/b.txt")

    assert key == "a/b.txt"
    get_settings.cache_clear()


def test_cos_blob_storage_put_bytes_surfaces_upload_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = CosBlobStorage()
    monkeypatch.setattr(
        "src.shared.blob_storage.upload_file_to_cos",
        lambda *_args: (False, "boom"),
    )

    with pytest.raises(ValueError, match="boom"):
        storage.put_bytes(key="assistant-1/test.txt", content=b"hello")


def test_build_workspace_file_repository_routes_mysql_backend() -> None:
    """战役一 P2:工厂按 backend 路由——mysql → Database repo(惰性 session,不建连)。"""
    assert isinstance(
        build_workspace_file_repository(backend="mysql"),
        DatabaseWorkspaceFileRepository,
    )
    # DatabaseWorkspaceFileRepository 统一实现文件+事件协议,事件工厂同样路由到它
    assert isinstance(
        build_workspace_file_event_repository(backend="mysql"),
        DatabaseWorkspaceFileRepository,
    )
    assert isinstance(
        build_workspace_file_repository(backend="sqlite"),
        SQLiteWorkspaceFileRepository,
    )


def test_workspace_file_metadata_backend_resolves_mysql_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND", raising=False)
    monkeypatch.delenv("IRONAGENT_WORKSPACE_FILE_METADATA_BACKEND", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().workspace_file_metadata_backend == "mysql"
        assert resolve_workspace_file_metadata_backend() == "mysql"
        assert resolve_workspace_file_metadata_backend("") == "mysql"
        assert resolve_workspace_file_metadata_backend("json") == "mysql"
        assert resolve_workspace_file_metadata_backend("sqlite") == "sqlite"
    finally:
        get_settings.cache_clear()


def test_build_workspace_file_repository_default_backend_is_mysql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认 workspace file metadata 后端为 MySQL;SQLite 只作为显式 opt-in。"""
    monkeypatch.delenv("IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND", raising=False)
    monkeypatch.delenv("IRONAGENT_WORKSPACE_FILE_METADATA_BACKEND", raising=False)
    get_settings.cache_clear()
    try:
        assert isinstance(
            build_workspace_file_repository(),
            DatabaseWorkspaceFileRepository,
        )
        assert isinstance(
            build_workspace_file_event_repository(),
            DatabaseWorkspaceFileRepository,
        )
        assert isinstance(
            build_external_push_request_repository(),
            DatabaseWorkspaceFileRepository,
        )
    finally:
        get_settings.cache_clear()


def test_build_workspace_file_repository_sqlite_backend_is_explicit_opt_in() -> None:
    assert isinstance(
        build_workspace_file_repository(backend="sqlite"),
        SQLiteWorkspaceFileRepository,
    )
    assert isinstance(
        build_external_push_request_repository(backend="sqlite"),
        JsonExternalPushRequestRepository,
    )
