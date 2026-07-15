from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.workspace_files.admin_index import (
    WorkspaceFileIndexFilters,
    list_workspace_file_index_page,
    list_workspace_folder_summaries,
)
from src.workspace_files.models import TargetBucket, WorkspaceFile
from src.workspace_files.sqlite_repository import (
    SQLiteWorkspaceFileRepository,
    SQLiteWorkspaceFileStore,
)


def _record(
    file_id: str,
    *,
    assistant_id: str = "assistant-a",
    bucket: TargetBucket = "persistent",
    filename: str | None = None,
    relative_path: str | None = None,
    source_type: str = "user_upload",
    sync_status: str = "synced",
    updated_at: datetime,
    deleted: bool = False,
) -> WorkspaceFile:
    resolved_filename = filename or (relative_path or f"{file_id}.txt").rsplit("/", 1)[-1]
    return WorkspaceFile(
        file_id=file_id,
        assistant_id=assistant_id,
        bucket=bucket,
        filename=resolved_filename,
        relative_path=relative_path or f"{bucket}/{resolved_filename}",
        source_type=source_type,
        sync_status=sync_status,
        storage_status=sync_status,
        created_at=updated_at - timedelta(minutes=5),
        updated_at=updated_at,
        deleted=deleted,
    )


def _sqlite_repository(tmp_path: Path) -> SQLiteWorkspaceFileRepository:
    return SQLiteWorkspaceFileRepository(SQLiteWorkspaceFileStore(tmp_path / "workspace.db"))


@pytest.mark.asyncio
async def test_sqlite_index_page_is_db_only_and_rejects_large_page_size(tmp_path: Path) -> None:
    base = datetime(2026, 5, 20, 8, 0, tzinfo=UTC)
    repository = _sqlite_repository(tmp_path)
    await repository.asave_file(_record("file-1", updated_at=base))
    await repository.asave_file(_record("file-2", updated_at=base + timedelta(minutes=1)))
    await repository.asave_file(_record("file-3", updated_at=base + timedelta(minutes=2)))
    await repository.asave_file(
        _record("deleted-file", updated_at=base + timedelta(minutes=3), deleted=True)
    )

    async def fail_alist_files(**_kwargs):
        raise AssertionError("admin index must not call alist_files for SQLite")

    repository.alist_files = fail_alist_files

    page = await list_workspace_file_index_page(
        repository,
        WorkspaceFileIndexFilters(assistant_id="assistant-a", page=1, page_size=2),
    )

    assert [record.file_id for record in page.records] == ["file-3", "file-2"]
    assert page.total == 3
    assert page.has_next is True
    assert page.last_indexed_at == base + timedelta(minutes=2)
    assert page.sync_status_counts == {"synced": 3}

    with pytest.raises(ValueError, match="page_size must be <= 200"):
        await list_workspace_file_index_page(
            repository,
            WorkspaceFileIndexFilters(page_size=201),
        )


@pytest.mark.asyncio
async def test_sqlite_tree_index_page_lists_current_directory(tmp_path: Path) -> None:
    base = datetime(2026, 5, 20, 8, 30, tzinfo=UTC)
    repository = _sqlite_repository(tmp_path)
    await repository.asave_file(
        _record(
            "jan",
            bucket="persistent",
            relative_path="persistent/reports/jan.txt",
            updated_at=base,
    )
        )
    await repository.asave_file(
        _record(
            "feb",
            bucket="persistent",
            relative_path="persistent/reports/feb.txt",
            updated_at=base + timedelta(minutes=1),
    )
        )
    await repository.asave_file(
        _record(
            "readme",
            bucket="persistent",
            relative_path="persistent/readme.txt",
            updated_at=base + timedelta(minutes=2),
    )
        )
    await repository.asave_file(
        _record(
            "temp-log",
            bucket="temp",
            relative_path="temp/out.log",
            updated_at=base + timedelta(minutes=3),
    )
        )

    root = await list_workspace_file_index_page(
        repository,
        WorkspaceFileIndexFilters(
            assistant_id="assistant-a",
            view="tree",
            page=1,
            page_size=10,
        ),
    )

    root_entries = [
        (record.entry_type, record.relative_path, record.child_count)
        for record in root.records
    ]
    assert root_entries == [
        ("folder", "persistent", 2),
        ("folder", "temp", 1),
    ]
    assert root.total == 2
    assert root.sync_status_counts == {"synced": 4}

    persistent = await list_workspace_file_index_page(
        repository,
        WorkspaceFileIndexFilters(
            assistant_id="assistant-a",
            view="tree",
            parent="persistent",
            page=1,
            page_size=10,
        ),
    )

    assert [
        (record.entry_type, record.relative_path, record.child_count)
        for record in persistent.records
    ] == [
        ("folder", "persistent/reports", 2),
        ("file", "persistent/readme.txt", 0),
    ]
    assert persistent.total == 2

    reports = await list_workspace_file_index_page(
        repository,
        WorkspaceFileIndexFilters(
            assistant_id="assistant-a",
            view="tree",
            parent="persistent/reports",
            page=1,
            page_size=10,
        ),
    )

    assert [record.relative_path for record in reports.records] == [
        "persistent/reports/feb.txt",
        "persistent/reports/jan.txt",
    ]
    assert [record.entry_type for record in reports.records] == ["file", "file"]

    with pytest.raises(ValueError, match="parent must not contain"):
        await list_workspace_file_index_page(
            repository,
            WorkspaceFileIndexFilters(
                assistant_id="assistant-a",
                view="tree",
                parent="../persistent",
            ),
        )


@pytest.mark.asyncio
async def test_sqlite_index_page_filters_visible_assistant_ids(tmp_path: Path) -> None:
    base = datetime(2026, 5, 20, 9, 0, tzinfo=UTC)
    repository = _sqlite_repository(tmp_path)
    await repository.asave_file(_record("visible", assistant_id="assistant-a", updated_at=base))
    await repository.asave_file(
        _record("hidden", assistant_id="assistant-b", updated_at=base + timedelta(minutes=1))
    )

    page = await list_workspace_file_index_page(
        repository,
        WorkspaceFileIndexFilters(assistant_ids={"assistant-a"}),
    )

    assert page.total == 1
    assert [record.file_id for record in page.records] == ["visible"]

    blocked = await list_workspace_file_index_page(
        repository,
        WorkspaceFileIndexFilters(
            assistant_ids={"assistant-a"},
            assistant_id="assistant-b",
        ),
    )
    assert blocked.total == 0
    assert blocked.records == []


@pytest.mark.asyncio
async def test_sqlite_folder_summaries_are_db_only(tmp_path: Path) -> None:
    base = datetime(2026, 5, 20, 9, 30, tzinfo=UTC)
    repository = _sqlite_repository(tmp_path)
    await repository.asave_file(
        _record("a-persistent", assistant_id="assistant-a", bucket="persistent", updated_at=base)
    )
    await repository.asave_file(
        _record(
            "a-temp",
            assistant_id="assistant-a",
            bucket="temp",
            sync_status="failed",
            updated_at=base + timedelta(minutes=1),
    )
        )
    await repository.asave_file(
        _record("hidden", assistant_id="assistant-hidden", updated_at=base + timedelta(minutes=2))
    )

    async def fail_alist_files(**_kwargs):
        raise AssertionError("folder summary must not call alist_files for SQLite")

    repository.alist_files = fail_alist_files

    page = await list_workspace_folder_summaries(
        repository,
        assistant_ids={"assistant-a", "assistant-b"},
    )

    by_id = {item.assistant_id: item for item in page.summaries}
    assert set(by_id) == {"assistant-a", "assistant-b"}
    assert by_id["assistant-a"].files_count == 2
    assert by_id["assistant-a"].root_items_count == 2
    assert by_id["assistant-a"].buckets == ["persistent", "temp"]
    assert by_id["assistant-a"].stale is True
    assert by_id["assistant-a"].sync_status == "unknown"
    assert by_id["assistant-b"].files_count == 0
    assert by_id["assistant-b"].sync_status == "empty"
    assert page.total_files == 2
    assert page.stale_count == 1
    assert page.last_indexed_at == base + timedelta(minutes=1)


@pytest.mark.asyncio
async def test_sqlite_folder_summaries_support_large_assistant_sets(tmp_path: Path) -> None:
    base = datetime(2026, 5, 20, 9, 45, tzinfo=UTC)
    repository = _sqlite_repository(tmp_path)
    await repository.asave_file(
        _record("first", assistant_id="assistant-0001", updated_at=base)
    )
    await repository.asave_file(
        _record(
            "last",
            assistant_id="assistant-1199",
            bucket="skills",
            updated_at=base + timedelta(minutes=1),
    )
        )

    async def fail_alist_files(**_kwargs):
        raise AssertionError("large folder summary must remain DB-only")

    repository.alist_files = fail_alist_files

    assistant_ids = {f"assistant-{index:04d}" for index in range(1200)}
    page = await list_workspace_folder_summaries(repository, assistant_ids=assistant_ids)

    by_id = {item.assistant_id: item for item in page.summaries}
    assert len(page.summaries) == 1200
    assert page.total_files == 2
    assert by_id["assistant-0001"].files_count == 1
    assert by_id["assistant-1199"].buckets == ["skills"]
    assert by_id["assistant-0500"].sync_status == "empty"


@pytest.mark.asyncio
async def test_sqlite_index_page_filters_query_source_type_and_sync_status(
    tmp_path: Path,
) -> None:
    base = datetime(2026, 5, 20, 10, 0, tzinfo=UTC)
    repository = _sqlite_repository(tmp_path)
    await repository.asave_file(
        _record(
            "match",
            filename="risk-report.txt",
            source_type="workspace_scan",
            sync_status="failed",
            updated_at=base,
    )
        )
    await repository.asave_file(
        _record(
            "wrong-source",
            filename="risk-report-copy.txt",
            source_type="user_upload",
            sync_status="failed",
            updated_at=base + timedelta(minutes=1),
    )
        )
    await repository.asave_file(
        _record(
            "wrong-status",
            filename="risk-report-old.txt",
            source_type="workspace_scan",
            sync_status="synced",
            updated_at=base + timedelta(minutes=2),
    )
        )

    page = await list_workspace_file_index_page(
        repository,
        WorkspaceFileIndexFilters(
            query="risk-report",
            source_type="workspace_scan",
            sync_status="failed",
        ),
    )

    assert [record.file_id for record in page.records] == ["match"]
    assert page.sync_status_counts == {"failed": 1}
    assert page.stale_count == 1
    assert page.records[0].stale is True


@pytest.mark.asyncio
async def test_fallback_uses_repository_alist_files_only() -> None:
    base = datetime(2026, 5, 20, 11, 0, tzinfo=UTC)

    class FakeRepository:
        def __init__(self) -> None:
            self.alist_calls: list[tuple[str | None, str | None]] = []
            self.records = [
                _record(
                    "a-visible",
                    assistant_id="assistant-a",
                    bucket="persistent",
                    updated_at=base,
                ),
                _record(
                    "b-visible",
                    assistant_id="assistant-b",
                    bucket="persistent",
                    updated_at=base + timedelta(minutes=1),
                ),
                _record(
                    "hidden-temp",
                    assistant_id="assistant-a",
                    bucket="temp",
                    updated_at=base + timedelta(minutes=2),
                ),
            ]

        async def alist_files(
            self,
            *,
            assistant_id: str | None = None,
            bucket: TargetBucket | None = None,
            include_deleted: bool = False,
            limit: int | None = None,
        ) -> list[WorkspaceFile]:
            self.alist_calls.append((assistant_id, bucket))
            return [
                record
                for record in self.records
                if (assistant_id is None or record.assistant_id == assistant_id)
                and (bucket is None or record.bucket == bucket)
                and (include_deleted or not record.deleted)
            ]

        def list_files(self, *_args, **_kwargs):
            raise AssertionError("fallback must not call service-style list_files")

        def list_cos_workspace_summary_records(self, *_args, **_kwargs):
            raise AssertionError("fallback must not call COS summary listing")

    repository = FakeRepository()

    page = await list_workspace_file_index_page(
        repository,
        WorkspaceFileIndexFilters(
            assistant_ids={"assistant-a", "assistant-b"},
            bucket="persistent",
            page_size=10,
        ),
    )

    assert repository.alist_calls == [
        ("assistant-a", "persistent"),
        ("assistant-b", "persistent"),
    ]
    assert [record.file_id for record in page.records] == ["b-visible", "a-visible"]
    assert page.total == 2
