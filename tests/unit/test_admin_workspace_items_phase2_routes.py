import sys
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

import src.admin_web.app as admin_web_app
from src.shared.config import get_settings
from src.workspace_files.item_index import WorkspaceItemIndexStore


def _admin_headers() -> dict[str, str]:
    return {"X-IronAgent-Internal-Token": get_settings().internal_service_token}


def _forbid_legacy_workspace_item_scans(monkeypatch) -> dict[str, bool]:
    called = {
        "installed_capabilities": False,
        "assistant_list_workspace_items": False,
        "cos_capability_records": False,
        "list_files": False,
        "sync_service": False,
    }

    def forbidden_installed_capabilities(**_kwargs):
        called["installed_capabilities"] = True
        raise AssertionError("workspace-items GET must not use legacy capability scan")

    class StubAssistantService:
        async def aget_assistant(self, assistant_id: str):
            return SimpleNamespace(assistant_id=assistant_id)

        def list_workspace_items(self, assistant_id: str):
            called["assistant_list_workspace_items"] = True
            raise AssertionError("workspace-items GET must not read assistant workspace items")

    class ForbiddenWorkspaceFileService:
        def list_cos_capability_package_records(self, *_args, **_kwargs):
            called["cos_capability_records"] = True
            raise AssertionError("workspace-items GET must not scan COS capability records")

        def list_files(self, *_args, **_kwargs):
            called["list_files"] = True
            raise AssertionError("workspace-items GET must not list workspace files")

    class ForbiddenWorkspaceSyncService:
        def __init__(self, *_args, **_kwargs):
            called["sync_service"] = True
            raise AssertionError("workspace-items GET must not create sync jobs")

    monkeypatch.setattr(
        admin_web_app,
        "_installed_capabilities_for_assistant",
        forbidden_installed_capabilities,
    )
    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "WorkspaceFileService", ForbiddenWorkspaceFileService)
    monkeypatch.setattr(admin_web_app, "WorkspaceSyncService", ForbiddenWorkspaceSyncService)
    return called


def test_admin_workspace_items_default_get_uses_db_index_only(monkeypatch) -> None:
    legacy_calls = _forbid_legacy_workspace_item_scans(monkeypatch)
    captured: dict[str, object] = {}
    indexed_at = datetime(2026, 5, 20, 10, 30, tzinfo=UTC)

    class StubFilters:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def list_workspace_item_index_page(filters, **kwargs):
        captured["filters"] = filters
        captured["query_kwargs"] = kwargs
        return {
            "items": [
                SimpleNamespace(
                    assistant_id="demo-main",
                    kind="skills",
                    slug="office-docs",
                    name="Office Docs",
                    version="1.2.0",
                    enabled=True,
                    install_path="/workspace/demo-main/skills/office-docs",
                    files_count=3,
                    content_hash="sha256:abc",
                    source="db",
                    sync_status="synced",
                    runtime_reload_status="applied",
                    runtime_reload_error="",
                    runtime_effective=True,
                    last_indexed_at=indexed_at,
                    last_synced_at=indexed_at,
                    last_error="",
                )
            ],
            "indexStatus": {
                "source": "db",
                "sync_status": "synced",
                "stale": False,
                "last_indexed_at": indexed_at,
            },
        }

    monkeypatch.setitem(
        sys.modules,
        "src.workspace_files.item_index",
        SimpleNamespace(
            WorkspaceItemIndexFilters=StubFilters,
            list_workspace_item_index_page=list_workspace_item_index_page,
        ),
    )
    monkeypatch.setattr(admin_web_app, "_workspace_admin_index_repository", lambda: object())

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.get(
        "/api/v1/admin/assistants/demo-main/workspace-items",
        headers=_admin_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert legacy_calls == {
        "installed_capabilities": False,
        "assistant_list_workspace_items": False,
        "cos_capability_records": False,
        "list_files": False,
        "sync_service": False,
    }
    assert captured["filters"].assistant_id == "demo-main"
    assert "workspace_file_repository" in captured["query_kwargs"]
    assert captured["filters"].kind is None
    assert captured["filters"].slug is None
    assert payload["compat"] == {
        "force_requested": False,
        "force_scan_disabled": False,
        "refresh_required": False,
    }
    assert payload["indexStatus"]["source"] == "db"
    assert payload["items"] == [
        {
            "assistant_id": "demo-main",
            "kind": "skills",
            "slug": "office-docs",
            "name": "Office Docs",
            "version": "1.2.0",
            "enabled": True,
            "install_path": "/workspace/demo-main/skills/office-docs",
            "files_count": 3,
            "content_hash": "sha256:abc",
            "source": "db",
            "sync_status": "synced",
            "runtime_reload_status": "applied",
            "runtime_reload_error": "",
            "runtime_effective": True,
            "last_indexed_at": indexed_at.isoformat(timespec="seconds"),
            "last_synced_at": indexed_at.isoformat(timespec="seconds"),
            "last_error": "",
        }
    ]


def test_admin_workspace_items_force_true_does_not_scan_or_create_job(monkeypatch) -> None:
    legacy_calls = _forbid_legacy_workspace_item_scans(monkeypatch)
    captured: dict[str, object] = {}

    class StubFilters:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def list_workspace_item_index_page(filters, **kwargs):
        captured["filters"] = filters
        captured["query_kwargs"] = kwargs
        return {
            "records": [
                {
                    "assistant_id": "demo-main",
                    "kind": "plugins",
                    "slug": "spreadsheet-exporter",
                    "name": "Spreadsheet Exporter",
                    "version": "0.4.0",
                    "enabled": False,
                    "install_path": "/workspace/demo-main/plugins/spreadsheet-exporter",
                    "files_count": 2,
                    "content_hash": "sha256:def",
                    "source": "db",
                    "sync_status": "stale",
                    "runtime_reload_status": "failed",
                    "runtime_reload_error": "reload failed",
                    "runtime_effective": False,
                    "last_indexed_at": "2026-05-20T02:30:00+00:00",
                    "last_synced_at": None,
                    "last_error": "previous sync failed",
                }
            ],
            "index_status": {"source": "db", "sync_status": "stale", "stale": True},
        }

    monkeypatch.setitem(
        sys.modules,
        "src.workspace_files.item_index",
        SimpleNamespace(
            WorkspaceItemIndexFilters=StubFilters,
            list_workspace_item_index_page=list_workspace_item_index_page,
        ),
    )
    monkeypatch.setattr(admin_web_app, "_workspace_admin_index_repository", lambda: object())

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.get(
        "/api/v1/admin/assistants/demo-main/workspace-items",
        params={"kind": "plugins", "slug": "spreadsheet-exporter", "force": "true"},
        headers=_admin_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert legacy_calls == {
        "installed_capabilities": False,
        "assistant_list_workspace_items": False,
        "cos_capability_records": False,
        "list_files": False,
        "sync_service": False,
    }
    assert captured["filters"].assistant_id == "demo-main"
    assert "workspace_file_repository" in captured["query_kwargs"]
    assert captured["filters"].kind == "plugins"
    assert captured["filters"].slug == "spreadsheet-exporter"
    assert payload["compat"] == {
        "force_requested": True,
        "force_scan_disabled": True,
        "refresh_required": True,
    }
    assert payload["indexStatus"]["source"] == "db"
    assert payload["items"][0]["kind"] == "plugins"
    assert payload["items"][0]["slug"] == "spreadsheet-exporter"
    assert payload["items"][0]["sync_status"] == "stale"
    assert payload["items"][0]["runtime_reload_error"] == "reload failed"


def test_admin_workspace_items_returns_404_without_querying_index(monkeypatch) -> None:
    captured = {"index_called": False}

    class MissingAssistantService:
        async def aget_assistant(self, assistant_id: str):
            return None

        def list_workspace_items(self, assistant_id: str):
            raise AssertionError("missing assistant must not read workspace items")

    def forbidden_query(**_kwargs):
        captured["index_called"] = True
        raise AssertionError("missing assistant must not query item index")

    monkeypatch.setattr(admin_web_app, "AssistantService", MissingAssistantService)
    monkeypatch.setitem(
        sys.modules,
        "src.workspace_files.item_index",
        SimpleNamespace(list_workspace_items=forbidden_query),
    )

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.get(
        "/api/v1/admin/assistants/missing/workspace-items",
        headers=_admin_headers(),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "assistant not found"
    assert captured["index_called"] is False


def test_admin_workspace_item_toggle_updates_item_index(monkeypatch, tmp_path) -> None:
    store = WorkspaceItemIndexStore(tmp_path / "workspace-item-index.db")
    audit_events: list[dict[str, object]] = []

    class StubAssistantService:
        async def aget_assistant(self, assistant_id: str):
            return SimpleNamespace(assistant_id=assistant_id, deleted=False)

        def set_workspace_item_enabled(self, *, assistant_id, kind, slug, enabled):
            return SimpleNamespace(
                assistant_id=assistant_id,
                kind=kind,
                slug=slug,
                version="2.0.0",
                enabled=enabled,
            )

        async def areload_workspace_capability(self, *, assistant_id, kind, slug):
            return SimpleNamespace(
                assistant_id=assistant_id,
                kind=kind,
                slug=slug,
                status="failed",
                error="runtime command failed",
            )

    class StubAuditRepository:
        def append_event(self, payload):
            audit_events.append(payload)

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "_workspace_item_index_store", lambda: store)
    monkeypatch.setattr(
        admin_web_app,
        "build_audit_repository",
        lambda: StubAuditRepository(),
    )

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.post(
        "/api/v1/admin/assistants/demo-main/workspace-items/toggle",
        headers=_admin_headers(),
        json={"kind": "skills", "slug": "office-docs", "enabled": False},
    )

    assert response.status_code == 200
    indexed = store.get_item(assistant_id="demo-main", kind="skills", slug="office-docs")
    assert indexed is not None
    assert indexed.enabled is False
    assert indexed.deleted is False
    assert indexed.runtime_reload_status == "failed"
    assert indexed.runtime_effective is False
    assert indexed.runtime_reload_error == "runtime command failed"
    assert audit_events


def test_admin_workspace_item_reuse_updates_target_item_index(monkeypatch, tmp_path) -> None:
    store = WorkspaceItemIndexStore(tmp_path / "workspace-item-index.db")
    sync_calls: list[tuple[str, list[tuple[str, str]]]] = []

    class StubAssistantService:
        async def aget_assistant(self, assistant_id: str):
            return SimpleNamespace(assistant_id=assistant_id, deleted=False)

        def reuse_workspace_item(self, *, source_assistant_id, target_assistant_id, kind, slug):
            return SimpleNamespace(
                source_assistant_id=source_assistant_id,
                target_assistant_id=target_assistant_id,
                kind=kind,
                slug=slug,
                install_path=f"/workspace/{target_assistant_id}/{kind}/{slug}",
            )

        async def areload_workspace_capability(self, *, assistant_id, kind, slug):
            return SimpleNamespace(
                assistant_id=assistant_id,
                kind=kind,
                slug=slug,
                status="applied",
                error="",
            )

    class StubWorkspaceSyncService:
        def __init__(self, *, assistant_service):
            self.assistant_service = assistant_service

        async def async_sync_assistant_workspace_scopes(self, assistant_id, scopes):
            sync_calls.append((assistant_id, list(scopes)))

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "WorkspaceSyncService", StubWorkspaceSyncService)
    monkeypatch.setattr(admin_web_app, "_workspace_item_index_store", lambda: store)

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.post(
        "/api/v1/admin/assistants/source-main/workspace-items/reuse",
        headers=_admin_headers(),
        json={
            "target_assistant_id": "target-main",
            "kind": "plugins",
            "slug": "sheet-export",
        },
    )

    assert response.status_code == 200
    assert sync_calls == [("target-main", [("plugins", "sheet-export")])]
    indexed = store.get_item(assistant_id="target-main", kind="plugins", slug="sheet-export")
    assert indexed is not None
    assert indexed.source == "reuse"
    assert indexed.enabled is True
    assert indexed.install_path == "/workspace/target-main/plugins/sheet-export"
    assert indexed.runtime_reload_status == "applied"
    assert indexed.runtime_effective is True


def test_admin_workspace_item_delete_soft_deletes_item_index(monkeypatch, tmp_path) -> None:
    store = WorkspaceItemIndexStore(tmp_path / "workspace-item-index.db")
    audit_events: list[dict[str, object]] = []

    class StubAssistantService:
        async def aget_assistant(self, assistant_id):
            return SimpleNamespace(assistant_id=assistant_id)

        def list_workspace_items(self, assistant_id):
            return [
                SimpleNamespace(
                    assistant_id=assistant_id,
                    kind="skills",
                    slug="office-docs",
                    version="2.0.0",
                )
            ]

        def remove_workspace_item(self, *, assistant_id, kind, slug):
            return SimpleNamespace(
                assistant_id=assistant_id,
                kind=kind,
                slug=slug,
                removed=True,
            )

        async def areload_workspace_capability(self, *, assistant_id, kind, slug):
            return SimpleNamespace(
                assistant_id=assistant_id,
                kind=kind,
                slug=slug,
                status="applied",
                error="",
            )

    class StubWorkspaceFileService:
        def delete_capability_package(self, *, assistant_id, bucket, slug, user_id):
            return 2

    class StubAuditRepository:
        def append_event(self, payload):
            audit_events.append(payload)

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "WorkspaceFileService", StubWorkspaceFileService)
    monkeypatch.setattr(admin_web_app, "_workspace_item_index_store", lambda: store)
    monkeypatch.setattr(
        admin_web_app,
        "build_audit_repository",
        lambda: StubAuditRepository(),
    )

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.delete(
        "/api/v1/admin/assistants/demo-main/workspace-items",
        params={"kind": "skills", "slug": "office-docs"},
        headers=_admin_headers(),
    )

    assert response.status_code == 200
    indexed = store.get_item(
        assistant_id="demo-main",
        kind="skills",
        slug="office-docs",
        include_deleted=True,
    )
    assert indexed is not None
    assert indexed.deleted is True
    assert indexed.enabled is False
    assert indexed.sync_status == "deleted"
    assert indexed.runtime_reload_status == "applied"
    assert audit_events
