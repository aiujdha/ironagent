from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import src.workspace_files.item_index as item_index_module

from src.workspace_files.item_index import (
    WorkspaceItemIndexFilters,
    WorkspaceItemIndexStore,
    list_workspace_item_index_page,
)
from src.workspace_files.item_index_writer import (
    WorkspaceItemIndexRecord,
    record_workspace_item_installed,
    record_workspace_item_removed,
)
from src.workspace_files.models import WorkspaceFile
from src.workspace_files.sqlite_repository import (
    SQLiteWorkspaceFileRepository,
    SQLiteWorkspaceFileStore,
    copy_files_to_sqlite,
)


def _store(tmp_path: Path) -> WorkspaceItemIndexStore:
    return WorkspaceItemIndexStore(tmp_path / "workspace-item-index.db")


def _workspace_file_repository(tmp_path: Path) -> SQLiteWorkspaceFileRepository:
    return SQLiteWorkspaceFileRepository(
        SQLiteWorkspaceFileStore(tmp_path / "workspace-files.db")
    )


def test_workspace_item_index_store_uses_configured_path(monkeypatch, tmp_path: Path) -> None:
    configured_path = tmp_path / "configured" / "workspace-items.db"
    monkeypatch.setattr(
        item_index_module,
        "get_settings",
        lambda: SimpleNamespace(workspace_item_index_sqlite_path=configured_path),
    )

    store = WorkspaceItemIndexStore()

    assert store.db_path == configured_path
    assert configured_path.exists()


def test_workspace_item_index_upserts_and_gets_active_item(tmp_path: Path) -> None:
    store = _store(tmp_path)
    indexed_at = datetime(2026, 5, 20, 9, 0, tzinfo=UTC)

    saved = store.upsert_item(
        WorkspaceItemIndexRecord(
            assistant_id="assistant-a",
            kind="skills",
            slug="office-docs",
            name="Office Docs",
            version="1.2.0",
            enabled=True,
            install_path="/workspace/assistant-a/skills/office-docs",
            files_count=3,
            content_hash="sha256:abc",
            source="market",
            sync_status="synced",
            runtime_reload_status="applied",
            runtime_effective=True,
            last_indexed_at=indexed_at,
            last_synced_at=indexed_at,
            created_at=indexed_at,
            updated_at=indexed_at,
    )
        )

    assert saved.slug == "office-docs"
    assert saved.last_indexed_at == indexed_at
    fetched = store.get_item(assistant_id="assistant-a", kind="skills", slug="office-docs")
    assert fetched == saved


def test_workspace_item_index_lists_with_filters_and_pagination(tmp_path: Path) -> None:
    store = _store(tmp_path)
    base = datetime(2026, 5, 20, 9, 0, tzinfo=UTC)
    records = [
        WorkspaceItemIndexRecord(
            assistant_id="assistant-a",
            kind="skills",
            slug="office-docs",
            name="Office Docs",
            sync_status="synced",
            updated_at=base,
            created_at=base,
        ),
        WorkspaceItemIndexRecord(
            assistant_id="assistant-a",
            kind="plugins",
            slug="sheet-export",
            name="Sheet Export",
            sync_status="failed",
            updated_at=base + timedelta(minutes=1),
            created_at=base,
        ),
        WorkspaceItemIndexRecord(
            assistant_id="assistant-b",
            kind="skills",
            slug="risk-tools",
            name="Risk Tools",
            sync_status="synced",
            updated_at=base + timedelta(minutes=2),
            created_at=base,
        ),
    ]
    for record in records:
        store.upsert_item(record)

    page = store.list_items(
        WorkspaceItemIndexFilters(
            assistant_id="assistant-a",
            query="export",
            page=1,
            page_size=1,
    )
        )

    assert page.total == 1
    assert page.has_next is False
    assert [record.slug for record in page.records] == ["sheet-export"]
    assert page.sync_status_counts == {"failed": 1}
    assert page.stale_count == 1
    assert page.index_status.sync_status == "stale"


def test_workspace_item_index_excludes_deleted_by_default(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 5, 20, 10, 0, tzinfo=UTC)
    store.upsert_item(
        WorkspaceItemIndexRecord(
            assistant_id="assistant-a",
            kind="skills",
            slug="old-skill",
            name="Old Skill",
            created_at=now,
            updated_at=now,
            deleted=True,
    )
        )

    assert store.get_item(assistant_id="assistant-a", kind="skills", slug="old-skill") is None
    assert (
        store.get_item(
            assistant_id="assistant-a",
            kind="skills",
            slug="old-skill",
            include_deleted=True,
        )
        is not None
    )
    assert store.list_items(WorkspaceItemIndexFilters(assistant_id="assistant-a")).records == []
    include_deleted = store.list_items(
        WorkspaceItemIndexFilters(assistant_id="assistant-a", include_deleted=True)
    )
    assert [record.slug for record in include_deleted.records] == ["old-skill"]


@pytest.mark.asyncio
async def test_workspace_item_index_store_supports_writer_helpers(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 5, 20, 11, 0, tzinfo=UTC)

    installed = await record_workspace_item_installed(
        store=store,
        assistant_id="assistant-a",
        kind="plugins",
        slug="browser-tool",
        name="Browser Tool",
        runtime_reload_status="applied",
        now=now,
    )
    removed = await record_workspace_item_removed(
        store=store,
        assistant_id="assistant-a",
        kind="plugins",
        slug="browser-tool",
        runtime_reload_status="failed",
        runtime_reload_error="reload failed",
        now=now + timedelta(minutes=3),
    )

    assert installed.deleted is False
    assert removed.deleted is True
    assert removed.runtime_effective is False
    assert store.list_items(WorkspaceItemIndexFilters(assistant_id="assistant-a")).records == []
    deleted = store.get_item(
        assistant_id="assistant-a",
        kind="plugins",
        slug="browser-tool",
        include_deleted=True,
    )
    assert deleted is not None
    assert deleted.runtime_reload_error == "reload failed"


def test_list_workspace_item_index_page_honors_visible_assistant_filter(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    store.upsert_item(
        WorkspaceItemIndexRecord(
            assistant_id="assistant-a",
            kind="skills",
            slug="office-docs",
            created_at=now,
            updated_at=now,
    )
        )

    page = list_workspace_item_index_page(
        WorkspaceItemIndexFilters(
            assistant_ids=["assistant-b"],
            assistant_id="assistant-a",
        ),
        store=store,
    )

    assert page.total == 0
    assert page.records == []


def test_list_workspace_item_index_page_enriches_from_workspace_files_sqlite(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    workspace_file_repository = _workspace_file_repository(tmp_path)
    base = datetime(2026, 5, 20, 13, 0, tzinfo=UTC)
    store.upsert_item(
        WorkspaceItemIndexRecord(
            assistant_id="assistant-a",
            kind="skills",
            slug="office-docs",
            files_count=0,
            last_indexed_at=None,
            last_synced_at=None,
            created_at=base,
            updated_at=base,
    )
        )
    copy_files_to_sqlite(
        [
            WorkspaceFile(
                assistant_id="assistant-a",
                bucket="skills",
                filename="office-docs/SKILL.md",
                relative_path="skills/office-docs/SKILL.md",
                sync_status="synced",
                last_synced_at=base + timedelta(minutes=2),
                updated_at=base + timedelta(minutes=1),
                created_at=base,
            ),
            WorkspaceFile(
                assistant_id="assistant-a",
                bucket="skills",
                filename="office-docs/scripts/run.py",
                relative_path="skills/office-docs/scripts/run.py",
                sync_status="synced",
                last_synced_at=base + timedelta(minutes=4),
                updated_at=base + timedelta(minutes=3),
                created_at=base,
            ),
            WorkspaceFile(
                assistant_id="assistant-a",
                bucket="skills",
                filename="other-skill/SKILL.md",
                relative_path="skills/other-skill/SKILL.md",
                sync_status="synced",
                last_synced_at=base + timedelta(minutes=6),
                updated_at=base + timedelta(minutes=5),
                created_at=base,
            ),
            WorkspaceFile(
                assistant_id="assistant-b",
                bucket="skills",
                filename="office-docs/SKILL.md",
                relative_path="skills/office-docs/SKILL.md",
                sync_status="synced",
                last_synced_at=base + timedelta(minutes=8),
                updated_at=base + timedelta(minutes=7),
                created_at=base,
            ),
        ],
        store=workspace_file_repository.store,
    )

    page = list_workspace_item_index_page(
        WorkspaceItemIndexFilters(assistant_id="assistant-a"),
        store=store,
        workspace_file_repository=workspace_file_repository,
    )

    record = page.records[0]
    assert record.files_count == 2
    assert record.last_indexed_at == base + timedelta(minutes=3)
    assert record.last_synced_at == base + timedelta(minutes=4)


def test_list_workspace_item_index_page_without_workspace_file_repository_keeps_index_record(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    indexed_at = datetime(2026, 5, 20, 14, 0, tzinfo=UTC)
    store.upsert_item(
        WorkspaceItemIndexRecord(
            assistant_id="assistant-a",
            kind="plugins",
            slug="sheet-export",
            files_count=7,
            last_indexed_at=indexed_at,
            last_synced_at=indexed_at,
            created_at=indexed_at,
            updated_at=indexed_at,
    )
        )

    page = list_workspace_item_index_page(
        WorkspaceItemIndexFilters(assistant_id="assistant-a"),
        store=store,
    )

    assert page.records[0].files_count == 7
    assert page.records[0].last_indexed_at == indexed_at
    assert page.records[0].last_synced_at == indexed_at


def test_list_workspace_item_index_page_workspace_file_aggregate_ignores_deleted(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    workspace_file_repository = _workspace_file_repository(tmp_path)
    base = datetime(2026, 5, 20, 15, 0, tzinfo=UTC)
    store.upsert_item(
        WorkspaceItemIndexRecord(
            assistant_id="assistant-a",
            kind="plugins",
            slug="browser-tool",
            created_at=base,
            updated_at=base,
    )
        )
    copy_files_to_sqlite(
        [
            WorkspaceFile(
                assistant_id="assistant-a",
                bucket="plugins",
                filename="browser-tool/manifest.json",
                relative_path="plugins/browser-tool/manifest.json",
                sync_status="synced",
                last_synced_at=base + timedelta(minutes=2),
                updated_at=base + timedelta(minutes=1),
                created_at=base,
            ),
            WorkspaceFile(
                assistant_id="assistant-a",
                bucket="plugins",
                filename="browser-tool/deleted.py",
                relative_path="plugins/browser-tool/deleted.py",
                sync_status="synced",
                last_synced_at=base + timedelta(minutes=4),
                updated_at=base + timedelta(minutes=3),
                created_at=base,
                deleted=True,
            ),
        ],
        store=workspace_file_repository.store,
    )

    page = list_workspace_item_index_page(
        WorkspaceItemIndexFilters(assistant_id="assistant-a"),
        store=store,
        workspace_file_repository=workspace_file_repository,
    )

    record = page.records[0]
    assert record.files_count == 1
    assert record.last_indexed_at == base + timedelta(minutes=1)
    assert record.last_synced_at == base + timedelta(minutes=2)
