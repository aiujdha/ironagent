from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.workspace_files.item_backfill import backfill_workspace_item_index
from src.workspace_files.item_index import WorkspaceItemIndexFilters, WorkspaceItemIndexStore
from src.workspace_files.item_index_writer import WorkspaceItemIndexRecord
from src.workspace_files.models import WorkspaceFile


class FakeWorkspaceFileRepository:
    def __init__(self, files: list[WorkspaceFile]) -> None:
        self.files = files
        self.calls: list[dict[str, object]] = []

    async def alist_files(
        self,
        *,
        assistant_id: str | None = None,
        bucket: str | None = None,
        include_deleted: bool = False,
    ) -> list[WorkspaceFile]:
        self.calls.append(
            {
                "assistant_id": assistant_id,
                "bucket": bucket,
                "include_deleted": include_deleted,
            }
        )
        records = self.files
        if assistant_id is not None:
            records = [record for record in records if record.assistant_id == assistant_id]
        if bucket is not None:
            records = [record for record in records if record.bucket == bucket]
        if not include_deleted:
            records = [record for record in records if not record.deleted]
        return records

    def list_cos_capability_package_records(
        self,
        *_args: object,
        **_kwargs: object,
    ) -> list[object]:
        raise AssertionError("backfill must not scan COS")

    def scan_workspace_results(self, *_args: object, **_kwargs: object) -> list[object]:
        raise AssertionError("backfill must not scan local workspace")


def _item_store(tmp_path: Path) -> WorkspaceItemIndexStore:
    return WorkspaceItemIndexStore(tmp_path / "workspace-items.db")


def _file(
    *,
    assistant_id: str = "assistant-a",
    bucket: str = "skills",
    relative_path: str = "office-docs/SKILL.md",
    file_id: str | None = None,
    deleted: bool = False,
    last_synced_at: datetime | None = None,
) -> WorkspaceFile:
    return WorkspaceFile(
        file_id=file_id or f"{assistant_id}:{bucket}:{relative_path}",
        assistant_id=assistant_id,
        bucket=bucket,  # type: ignore[arg-type]
        filename=relative_path,
        relative_path=relative_path,
        source_type="capability_package",
        sync_status="synced",
        storage_status="synced",
        deleted=deleted,
        last_synced_at=last_synced_at,
    )


@pytest.mark.asyncio
async def test_backfill_creates_items_from_capability_workspace_files(tmp_path: Path) -> None:
    store = _item_store(tmp_path)
    synced_at = datetime(2026, 5, 20, 9, 0, tzinfo=UTC)
    repository = FakeWorkspaceFileRepository(
        [
            _file(relative_path="office-docs/SKILL.md", last_synced_at=synced_at),
            _file(relative_path="office-docs/scripts/convert.py", file_id="file-2"),
            _file(bucket="plugins", relative_path="sheet-export/manifest.json"),
            _file(bucket="persistent", relative_path="report.pdf"),
        ]
    )

    stats = await backfill_workspace_item_index(
        file_repository=repository,
        item_index_store=store,
        now=synced_at,
    )

    assert stats.scanned == 4
    assert stats.created == 2
    assert stats.updated == 0
    assert stats.skipped == 1
    assert stats.errors == 0
    page = store.list_items(WorkspaceItemIndexFilters(assistant_id="assistant-a"))
    assert page.total == 2
    indexed = store.get_item(assistant_id="assistant-a", kind="skills", slug="office-docs")
    assert indexed is not None
    assert indexed.name == "office-docs"
    assert indexed.source == "backfill"
    assert indexed.sync_status == "stored"
    assert indexed.runtime_reload_status == "skipped"
    assert indexed.files_count == 2
    assert indexed.last_synced_at == synced_at


@pytest.mark.asyncio
async def test_backfill_is_repeatable_without_duplicate_items(tmp_path: Path) -> None:
    store = _item_store(tmp_path)
    repository = FakeWorkspaceFileRepository([_file(relative_path="office-docs/SKILL.md")])
    now = datetime(2026, 5, 20, 10, 0, tzinfo=UTC)

    first = await backfill_workspace_item_index(
        file_repository=repository,
        item_index_store=store,
        now=now,
    )
    second = await backfill_workspace_item_index(
        file_repository=repository,
        item_index_store=store,
        now=now,
    )

    assert first.created == 1
    assert second.created == 0
    assert second.updated == 1
    page = store.list_items(WorkspaceItemIndexFilters(assistant_id="assistant-a"))
    assert page.total == 1
    assert [record.slug for record in page.records] == ["office-docs"]


@pytest.mark.asyncio
async def test_backfill_filters_by_assistant_and_kind(tmp_path: Path) -> None:
    store = _item_store(tmp_path)
    repository = FakeWorkspaceFileRepository(
        [
            _file(
                assistant_id="assistant-a",
                bucket="skills",
                relative_path="office-docs/SKILL.md",
            ),
            _file(
                assistant_id="assistant-a",
                bucket="plugins",
                relative_path="sheet-export/main.py",
            ),
            _file(
                assistant_id="assistant-b",
                bucket="plugins",
                relative_path="browser-tool/main.py",
            ),
        ]
    )

    stats = await backfill_workspace_item_index(
        file_repository=repository,
        item_index_store=store,
        assistant_id="assistant-a",
        kind="plugins",
    )

    assert repository.calls == [
        {"assistant_id": "assistant-a", "bucket": "plugins", "include_deleted": True}
    ]
    assert stats.scanned == 1
    assert stats.created == 1
    assert store.get_item(
        assistant_id="assistant-a",
        kind="plugins",
        slug="sheet-export",
    ) is not None
    assert store.list_items(WorkspaceItemIndexFilters(assistant_id="assistant-b")).total == 0


@pytest.mark.asyncio
async def test_backfill_skips_deleted_workspace_files(tmp_path: Path) -> None:
    store = _item_store(tmp_path)
    repository = FakeWorkspaceFileRepository(
        [_file(relative_path="old-skill/SKILL.md", deleted=True)]
    )

    stats = await backfill_workspace_item_index(
        file_repository=repository,
        item_index_store=store,
    )

    assert stats.scanned == 1
    assert stats.created == 0
    assert stats.skipped == 1
    assert store.list_items(WorkspaceItemIndexFilters(assistant_id="assistant-a")).total == 0


@pytest.mark.asyncio
async def test_backfill_skips_invalid_paths(tmp_path: Path) -> None:
    store = _item_store(tmp_path)
    repository = FakeWorkspaceFileRepository(
        [
            _file(relative_path="office-docs"),
            _file(relative_path=".hidden/SKILL.md"),
            _file(bucket="skills", relative_path="skills/"),
        ]
    )

    stats = await backfill_workspace_item_index(
        file_repository=repository,
        item_index_store=store,
    )

    assert stats.scanned == 3
    assert stats.created == 0
    assert stats.skipped == 3
    assert stats.errors == 0
    assert store.list_items(WorkspaceItemIndexFilters(assistant_id="assistant-a")).total == 0


@pytest.mark.asyncio
async def test_backfill_prunes_missing_enabled_items(tmp_path: Path) -> None:
    store = _item_store(tmp_path)
    store.upsert_item(
        WorkspaceItemIndexRecord(
            assistant_id="assistant-a",
            kind="skills",
            slug="old-skill",
            name="Old Skill",
            files_count=2,
            enabled=True,
            deleted=False,
    )
        )
    repository = FakeWorkspaceFileRepository([])

    stats = await backfill_workspace_item_index(
        file_repository=repository,
        item_index_store=store,
        assistant_id="assistant-a",
        kind="skills",
        prune_missing=True,
    )

    assert stats.removed == 1
    deleted = store.get_item(
        assistant_id="assistant-a",
        kind="skills",
        slug="old-skill",
        include_deleted=True,
    )
    assert deleted is not None
    assert deleted.deleted is True
    assert deleted.enabled is False
    assert deleted.sync_status == "deleted_from_workspace"
    assert store.list_items(WorkspaceItemIndexFilters(assistant_id="assistant-a")).total == 0


@pytest.mark.asyncio
async def test_backfill_does_not_prune_disabled_missing_items(tmp_path: Path) -> None:
    store = _item_store(tmp_path)
    store.upsert_item(
        WorkspaceItemIndexRecord(
            assistant_id="assistant-a",
            kind="plugins",
            slug="disabled-plugin",
            name="Disabled Plugin",
            files_count=0,
            enabled=False,
            deleted=False,
    )
        )
    repository = FakeWorkspaceFileRepository([])

    stats = await backfill_workspace_item_index(
        file_repository=repository,
        item_index_store=store,
        assistant_id="assistant-a",
        kind="plugins",
        prune_missing=True,
    )

    assert stats.removed == 0
    kept = store.get_item(
        assistant_id="assistant-a",
        kind="plugins",
        slug="disabled-plugin",
    )
    assert kept is not None
    assert kept.deleted is False
    assert kept.enabled is False
