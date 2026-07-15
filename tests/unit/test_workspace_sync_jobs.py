from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from src.workspace_files.sync_jobs import (
    WorkspaceSyncJobService,
    WorkspaceSyncJobStore,
    create_workspace_sync_job,
    run_index_only_job,
    run_next_pending_index_only_job,
)
from src.workspace_files.sync_service import WorkspaceSyncService


def _store(tmp_path: Path) -> WorkspaceSyncJobStore:
    return WorkspaceSyncJobStore(tmp_path / "workspace-sync-jobs.db")


def test_create_workspace_sync_job_returns_pending_job(tmp_path: Path) -> None:
    store = _store(tmp_path)

    job = create_workspace_sync_job("assistant-1", store=store)

    assert job.job_id
    assert job.status == "pending"
    assert job.assistant_id == "assistant-1"
    assert store.get_job(job.job_id) == job


def test_create_or_get_job_reuses_pending_and_running_job(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.create_or_get_job("assistant-1")

    pending_duplicate = store.create_or_get_job("assistant-1")
    assert pending_duplicate.job_id == first.job_id

    running = store.mark_running(first.job_id)
    assert running is not None
    assert running.status == "running"

    running_duplicate = store.create_or_get_job("assistant-1")
    assert running_duplicate.job_id == first.job_id


@pytest.mark.parametrize("mode", ["reconcile", "cos_import"])
def test_create_or_get_job_persists_and_reuses_non_index_only_modes(
    tmp_path: Path,
    mode: str,
) -> None:
    store = _store(tmp_path)

    first = store.create_or_get_job(
        "assistant-1",
        scope_kind="assistant",
        scope_slug="",
        mode=mode,
    )
    duplicate = store.create_or_get_job(
        "assistant-1",
        scope_kind="assistant",
        scope_slug="",
        mode=mode,
    )

    assert duplicate.job_id == first.job_id
    assert first.status == "pending"
    assert first.mode == mode
    assert first.scope_kind == "assistant"
    assert first.scope_slug == ""
    assert first.orphan_count == 0
    assert store.get_job(first.job_id) == first


@pytest.mark.parametrize(
    ("mode", "scope_kind", "scope_slug"),
    [
        ("reconcile", "capability", "skills/office-docs"),
        ("cos_import", "bucket", "persistent"),
        ("reconcile", "all_visible", ""),
    ],
)
def test_create_or_get_job_persists_explicit_maintenance_scopes(
    tmp_path: Path,
    mode: str,
    scope_kind: str,
    scope_slug: str,
) -> None:
    store = _store(tmp_path)

    first = store.create_or_get_job(
        "assistant-1",
        scope_kind=scope_kind,
        scope_slug=scope_slug,
        mode=mode,
    )
    duplicate = store.create_or_get_job(
        "assistant-1",
        scope_kind=scope_kind,
        scope_slug=scope_slug,
        mode=mode,
    )

    assert duplicate.job_id == first.job_id
    assert first.mode == mode
    assert first.scope_kind == scope_kind
    assert first.scope_slug == scope_slug
    assert first.status == "pending"


def test_index_only_rejects_non_assistant_scope(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="index_only"):
        store.create_or_get_job(
            "assistant-1",
            scope_kind="bucket",
            scope_slug="persistent",
            mode="index_only",
        )


def test_all_visible_scope_rejects_slug(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="all_visible"):
        store.create_or_get_job(
            "assistant-1",
            scope_kind="all_visible",
            scope_slug="unexpected",
            mode="reconcile",
        )


def test_get_missing_workspace_sync_job_returns_none(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.get_job("missing-job") is None


@pytest.mark.asyncio
async def test_run_index_only_job_uses_scoped_sync_with_missing_reconcile(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    job = store.create_or_get_job("assistant-1")

    class StubSyncService:
        def __init__(self) -> None:
            self.scope_calls: list[tuple[str, list[tuple[str, str]], bool]] = []
            self.full_calls: list[str] = []

        async def async_sync_assistant_workspace_scopes(
            self,
            assistant_id: str,
            scopes: list[tuple[str, str]],
            *,
            mark_missing: bool = False,
        ) -> SimpleNamespace:
            self.scope_calls.append((assistant_id, scopes, mark_missing))
            return SimpleNamespace(
                scanned_files=3,
                created=1,
                updated=2,
                drifted=0,
                removed=0,
            )

        async def async_sync_assistant_workspace(self, assistant_id: str) -> None:
            self.full_calls.append(assistant_id)

    stub = StubSyncService()

    finished = await run_index_only_job(
        job.job_id,
        store=store,
        sync_service_factory=cast(Callable[[], WorkspaceSyncService], lambda: stub),
    )

    assert finished is not None
    assert finished.status == "succeeded"
    assert finished.scanned_count == 3
    assert stub.full_calls == []
    assert stub.scope_calls == [
        (
            "assistant-1",
            [
                ("temp", ""),
                ("persistent", ""),
                ("skills", ""),
                ("plugins", ""),
            ],
            True,
        )
    ]


@pytest.mark.asyncio
async def test_run_index_only_job_fails_when_metadata_degraded(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    job = store.create_or_get_job("assistant-1")

    class StubSyncService:
        async def async_sync_assistant_workspace_scopes(
            self,
            assistant_id: str,
            scopes: list[tuple[str, str]],
            *,
            mark_missing: bool = False,
        ) -> SimpleNamespace:
            del assistant_id, scopes, mark_missing
            return SimpleNamespace(
                scanned_files=1,
                created=1,
                updated=0,
                drifted=0,
                removed=0,
                metadata_degraded=True,
            )

    finished = await run_index_only_job(
        job.job_id,
        store=store,
        sync_service_factory=cast(Callable[[], WorkspaceSyncService], StubSyncService),
    )

    assert finished is not None
    assert finished.status == "failed"
    assert finished.error_code == "workspace_sync_metadata_degraded"


@pytest.mark.asyncio
async def test_run_next_pending_index_only_job_claims_pending_by_priority(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    slow = store.create_or_get_job("assistant-slow", priority=200)
    fast = store.create_or_get_job("assistant-fast", priority=50)

    class StubSyncService:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def async_sync_assistant_workspace_scopes(
            self,
            assistant_id: str,
            scopes: list[tuple[str, str]],
            *,
            mark_missing: bool = False,
        ) -> SimpleNamespace:
            del scopes, mark_missing
            self.calls.append(assistant_id)
            return SimpleNamespace(
                scanned_files=1,
                created=1,
                updated=0,
                drifted=0,
                removed=0,
            )

    stub = StubSyncService()

    finished = await run_next_pending_index_only_job(
        store=store,
        sync_service_factory=cast(Callable[[], WorkspaceSyncService], lambda: stub),
    )

    assert finished is not None
    assert finished.job_id == fast.job_id
    assert finished.status == "succeeded"
    assert stub.calls == ["assistant-fast"]
    assert store.get_job(slow.job_id).status == "pending"
    assert store.get_job(fast.job_id) == finished


@pytest.mark.asyncio
async def test_run_next_pending_index_only_job_returns_none_when_empty(
    tmp_path: Path,
) -> None:
    service = WorkspaceSyncJobService(store=_store(tmp_path))

    assert await service.run_next_pending_index_only_job() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["reconcile", "cos_import"])
async def test_run_index_only_job_fails_non_index_only_without_sync_call(
    tmp_path: Path,
    mode: str,
) -> None:
    store = _store(tmp_path)
    job = store.create_or_get_job("assistant-1", mode=mode)
    factory_called = False

    def forbidden_sync_service_factory() -> WorkspaceSyncService:
        nonlocal factory_called
        factory_called = True
        raise AssertionError("non-index-only jobs must not enter index_only sync")

    finished = await run_index_only_job(
        job.job_id,
        store=store,
        sync_service_factory=forbidden_sync_service_factory,
    )

    assert factory_called is False
    assert finished is not None
    assert finished.status == "failed"
    assert finished.error_code == "unsupported_runner"
    assert finished.mode == mode
    assert finished.scope_kind == "assistant"
    assert finished.scope_slug == ""
    assert finished.orphan_count == 0
    assert finished.scanned_count == 0
    assert store.get_job(job.job_id) == finished
