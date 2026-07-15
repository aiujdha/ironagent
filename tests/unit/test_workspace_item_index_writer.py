from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.workspace_files.item_index_writer import (
    WorkspaceItemIndexRecord,
    record_workspace_item_installed,
    record_workspace_item_reload_result,
    record_workspace_item_removed,
    record_workspace_item_reused,
    record_workspace_item_toggle,
)


class FakeItemIndexStore:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str, str], WorkspaceItemIndexRecord] = {}
        self.calls: list[str] = []

    async def aget_item(
        self,
        *,
        assistant_id: str,
        kind: str,
        slug: str,
        include_deleted: bool = False,
    ) -> WorkspaceItemIndexRecord | None:
        self.calls.append("aget_item")
        record = self.records.get((assistant_id, kind, slug))
        if record is not None and record.deleted and not include_deleted:
            return None
        return record

    async def aupsert_item(self, record: WorkspaceItemIndexRecord) -> WorkspaceItemIndexRecord:
        self.calls.append("aupsert_item")
        self.records[(record.assistant_id, record.kind, record.slug)] = record
        return record

    def list_workspace_items(self, *_args, **_kwargs):
        raise AssertionError("writer must not scan assistant workspace items")

    def list_cos_workspace_summary_records(self, *_args, **_kwargs):
        raise AssertionError("writer must not read COS")

    def scan_workspace_results(self, *_args, **_kwargs):
        raise AssertionError("writer must not scan local workspace")


@pytest.mark.asyncio
async def test_record_workspace_item_installed_upserts_active_record() -> None:
    store = FakeItemIndexStore()
    now = datetime(2026, 5, 20, 8, 0, tzinfo=UTC)

    record = await record_workspace_item_installed(
        store=store,
        assistant_id="assistant-a",
        kind="skills",
        slug="office-docs",
        name="Office Docs",
        version="1.2.0",
        install_path="/workspace/assistant-a/skills/office-docs",
        runtime_reload_status="applied",
        last_synced_at=now,
        now=now,
    )

    assert record.assistant_id == "assistant-a"
    assert record.kind == "skills"
    assert record.slug == "office-docs"
    assert record.name == "Office Docs"
    assert record.version == "1.2.0"
    assert record.enabled is True
    assert record.deleted is False
    assert record.source == "market"
    assert record.sync_status == "synced"
    assert record.runtime_reload_status == "applied"
    assert record.runtime_effective is True
    assert record.last_synced_at == now
    assert store.calls == ["aget_item", "aupsert_item"]


@pytest.mark.asyncio
async def test_record_workspace_item_toggle_updates_enabled_and_reload_status() -> None:
    store = FakeItemIndexStore()
    created_at = datetime(2026, 5, 20, 8, 0, tzinfo=UTC)
    updated_at = created_at + timedelta(minutes=5)
    store.records[("assistant-a", "plugins", "sheet-export")] = WorkspaceItemIndexRecord(
        assistant_id="assistant-a",
        kind="plugins",
        slug="sheet-export",
        name="Sheet Export",
        version="2.0.0",
        enabled=True,
        install_path="/workspace/assistant-a/plugins/sheet-export",
        created_at=created_at,
        updated_at=created_at,
    )

    record = await record_workspace_item_toggle(
        store=store,
        assistant_id="assistant-a",
        kind="plugins",
        slug="sheet-export",
        enabled=False,
        runtime_reload_status="applied",
        now=updated_at,
    )

    assert record.enabled is False
    assert record.deleted is False
    assert record.name == "Sheet Export"
    assert record.version == "2.0.0"
    assert record.install_path == "/workspace/assistant-a/plugins/sheet-export"
    assert record.runtime_reload_status == "applied"
    assert record.runtime_effective is True
    assert record.created_at == created_at
    assert record.updated_at == updated_at


@pytest.mark.asyncio
async def test_record_workspace_item_reused_upserts_target_assistant_active_record() -> None:
    store = FakeItemIndexStore()
    now = datetime(2026, 5, 20, 9, 0, tzinfo=UTC)

    record = await record_workspace_item_reused(
        store=store,
        source_assistant_id="source-assistant",
        target_assistant_id="target-assistant",
        kind="skills",
        slug="risk-tools",
        name="Risk Tools",
        version="3.1.0",
        install_path="/workspace/target-assistant/skills/risk-tools",
        runtime_reload_status="skipped",
        now=now,
    )

    assert record.assistant_id == "target-assistant"
    assert record.kind == "skills"
    assert record.slug == "risk-tools"
    assert record.enabled is True
    assert record.deleted is False
    assert record.source == "reuse"
    assert record.runtime_reload_status == "skipped"
    assert record.runtime_effective is True


@pytest.mark.asyncio
async def test_record_workspace_item_removed_soft_deletes_existing_record() -> None:
    store = FakeItemIndexStore()
    created_at = datetime(2026, 5, 20, 10, 0, tzinfo=UTC)
    removed_at = created_at + timedelta(minutes=3)
    store.records[("assistant-a", "skills", "old-skill")] = WorkspaceItemIndexRecord(
        assistant_id="assistant-a",
        kind="skills",
        slug="old-skill",
        name="Old Skill",
        version="0.9.0",
        enabled=True,
        install_path="/workspace/assistant-a/skills/old-skill",
        created_at=created_at,
        updated_at=created_at,
    )

    record = await record_workspace_item_removed(
        store=store,
        assistant_id="assistant-a",
        kind="skills",
        slug="old-skill",
        runtime_reload_status="applied",
        now=removed_at,
    )

    assert record.deleted is True
    assert record.enabled is False
    assert record.sync_status == "deleted"
    assert record.runtime_reload_status == "applied"
    assert record.runtime_effective is True
    assert record.created_at == created_at
    assert record.updated_at == removed_at
    assert store.records[("assistant-a", "skills", "old-skill")].deleted is True


@pytest.mark.asyncio
async def test_record_workspace_item_reload_failure_keeps_index_and_marks_runtime_failed() -> None:
    store = FakeItemIndexStore()
    created_at = datetime(2026, 5, 20, 11, 0, tzinfo=UTC)
    failed_at = created_at + timedelta(minutes=7)
    store.records[("assistant-a", "plugins", "browser-tool")] = WorkspaceItemIndexRecord(
        assistant_id="assistant-a",
        kind="plugins",
        slug="browser-tool",
        name="Browser Tool",
        version="1.0.0",
        enabled=True,
        install_path="/workspace/assistant-a/plugins/browser-tool",
        created_at=created_at,
        updated_at=created_at,
        deleted=False,
    )

    record = await record_workspace_item_reload_result(
        store=store,
        assistant_id="assistant-a",
        kind="plugins",
        slug="browser-tool",
        runtime_reload_status="failed",
        runtime_reload_error="runtime command failed",
        now=failed_at,
    )

    assert record.deleted is False
    assert record.enabled is True
    assert record.runtime_reload_status == "failed"
    assert record.runtime_effective is False
    assert record.runtime_reload_error == "runtime command failed"
    assert record.updated_at == failed_at
    assert store.calls == ["aget_item", "aupsert_item"]
