import asyncio
import json
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

import src.admin_web.app as admin_web_app
from src.admin_web.app import (
    _assistant_fresh_session_key,
    _assistant_main_session_key,
    _build_capability_profiles,
    _build_model_payload,
    _load_assistants_for_bootstrap,
    _load_assistants_for_bootstrap_fast,
    _load_market_for_bootstrap,
    _normalize_capability_profile,
    _resolve_assistant_identity,
    _resolve_capability_profile,
    _resolve_control_ui_allowed_origins,
    _sync_runtime_control_ui_allowed_origins,
)
from src.approval.models import ApprovalRequest
from src.shared.config import get_settings
from src.shared.repositories import JsonAuditRepository


def _admin_headers() -> dict[str, str]:
    return {"X-IronAgent-Internal-Token": get_settings().internal_service_token}


def test_assistant_fresh_session_key_does_not_reuse_main_session() -> None:
    session_key = _assistant_fresh_session_key("apply-test")

    assert session_key.startswith("agent:apply-test:ui-")
    assert session_key != _assistant_main_session_key("apply-test")


def test_resolve_assistant_identity_uses_workspace_slug_when_provided() -> None:
    assistant_id, workspace_slug = _resolve_assistant_identity(
        assistant_id="",
        workspace_slug="finance-workspace",
        name="财务分析助手",
    )

    assert assistant_id == "finance-workspace"
    assert workspace_slug == "finance-workspace"


def test_resolve_assistant_identity_generates_unique_slug_for_non_ascii_name() -> None:
    assistant_id, workspace_slug = _resolve_assistant_identity(
        assistant_id="",
        workspace_slug="",
        name="财务分析助手",
    )

    assert assistant_id.startswith("assistant-")
    assert workspace_slug == assistant_id


def test_resolve_assistant_identity_keeps_explicit_assistant_id() -> None:
    assistant_id, workspace_slug = _resolve_assistant_identity(
        assistant_id="finance-bot",
        workspace_slug="",
        name="财务分析助手",
    )

    assert assistant_id == "finance-bot"
    assert workspace_slug == "finance-bot"


def test_resolve_control_ui_allowed_origins_includes_gateway_and_legacy_ui() -> None:
    settings = SimpleNamespace(
        control_ui_public_url="",
        control_ui_allowed_origins_json="",
        iron_agent_gateway_host="0.0.0.0",
        iron_agent_gateway_port=18789,
    )

    assert _resolve_control_ui_allowed_origins(settings) == [
        "http://127.0.0.1:18789",
        "http://localhost:18789",
        "http://127.0.0.1:19001",
        "http://localhost:19001",
    ]


def test_resolve_control_ui_allowed_origins_includes_public_control_ui_origin() -> None:
    settings = SimpleNamespace(
        control_ui_public_url="http://192.0.2.11:30789/chat?session=main",
        control_ui_allowed_origins_json="",
        iron_agent_gateway_host="0.0.0.0",
        iron_agent_gateway_port=18789,
    )

    assert _resolve_control_ui_allowed_origins(settings) == [
        "http://192.0.2.11:30789",
    ]


def test_sync_runtime_control_ui_allowed_origins_updates_iron_agent_config(tmp_path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    config_path = state_root / "ironagent.json"
    config_path.write_text(
        '{"gateway":{"controlUi":{"allowedOrigins":["http://127.0.0.1:19001"]}}}\n',
        encoding="utf-8",
    )
    record = SimpleNamespace(runtime=SimpleNamespace(state_root=str(state_root)))
    settings = SimpleNamespace(
        control_ui_public_url="",
        iron_agent_gateway_host="127.0.0.1",
        iron_agent_gateway_port=18789,
    )

    changed = _sync_runtime_control_ui_allowed_origins(record, settings)

    assert changed is True
    assert json.loads(config_path.read_text(encoding="utf-8"))["gateway"]["controlUi"][
        "allowedOrigins"
    ] == [
        "http://127.0.0.1:19001",
        "http://127.0.0.1:18789",
        "http://localhost:18789",
        "http://localhost:19001",
    ]


def test_normalize_capability_profile_handles_legacy_values() -> None:
    assert _normalize_capability_profile("general") == "chat"
    assert _normalize_capability_profile("") == "chat"
    assert _normalize_capability_profile("readonly") == "onlyread"
    assert _normalize_capability_profile("write") == "onlywrite"
    assert _normalize_capability_profile("safe_write") == "work_no_exec"
    assert _normalize_capability_profile("no_exec") == "work_no_exec"
    assert _normalize_capability_profile("danger") == "dangerous"


def test_resolve_capability_profile_preserves_custom_profile(monkeypatch, tmp_path) -> None:
    capability_dir = tmp_path / "capabilities"
    capability_dir.mkdir()
    (capability_dir / "demo-profile.yaml").write_text(
        "name: Demo Profile\nallowed_tools:\n  - read\napproval_tools: []\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        admin_web_app,
        "get_settings",
        lambda: SimpleNamespace(config_dir=tmp_path),
    )

    assert _resolve_capability_profile("demo-profile") == "demo-profile"
    with pytest.raises(admin_web_app.HTTPException) as exc:
        _resolve_capability_profile("missing-profile")
    assert exc.value.status_code == 400
    assert exc.value.detail == "unknown capability profile: missing-profile"


def test_build_capability_profiles_falls_back_to_builtin_profiles(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        admin_web_app,
        "get_settings",
        lambda: SimpleNamespace(config_dir=tmp_path),
    )

    profiles = _build_capability_profiles()

    assert [item["id"] for item in profiles] == [
        "chat",
        "onlyread",
        "onlywrite",
        "work_no_exec",
        "dangerous",
    ]
    work_no_exec = next(item for item in profiles if item["id"] == "work_no_exec")
    assert {"read", "write", "edit", "apply_patch"} <= set(work_no_exec["allowed_tools"])
    assert "exec" not in work_no_exec["allowed_tools"]


def test_save_capability_profile_refreshes_matching_assistants(
    monkeypatch,
    tmp_path,
) -> None:
    state_root = tmp_path / "assistants" / "demo-main" / ".ironagent"
    state_root.mkdir(parents=True)
    (state_root / "ironagent.json").write_text(
        json.dumps({"tools": {"allow": ["old-tool"]}}, ensure_ascii=True),
        encoding="utf-8",
    )
    settings = get_settings().model_copy(
        update={
            "config_dir": tmp_path,
            "assistants_runtime_root": tmp_path / "runtimes",
        }
    )
    refreshed: list[dict[str, object]] = []

    class StubAssistantService:
        def list_assistants(self):
            return [
                SimpleNamespace(
                    assistant_id="demo-main",
                    name="Demo Main",
                    capability_profile="demo-profile",
                    notes="demo notes",
                    iron_agent_config=SimpleNamespace(primary_model="model-a"),
                    workspace_config=SimpleNamespace(workspace_slug="demo-main"),
                    runtime=SimpleNamespace(state_root=str(state_root)),
                ),
                SimpleNamespace(
                    assistant_id="other",
                    name="Other",
                    capability_profile="onlyread",
                    notes="",
                    iron_agent_config=None,
                    workspace_config=None,
                    runtime=SimpleNamespace(state_root=""),
                ),
            ]

        def upsert_assistant(self, **kwargs):
            refreshed.append(kwargs)
            return SimpleNamespace(assistant_id=kwargs["assistant_id"])

    monkeypatch.setattr(admin_web_app, "get_settings", lambda: settings)
    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)

    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)
    response = client.post(
        "/api/v1/admin/capabilities",
        headers=_admin_headers(),
        json={
            "id": "demo-profile",
            "name": "Demo Profile",
            "level": 2,
            "allowed_tools": ["read", "write"],
            "approval_tools": ["exec"],
        },
    )

    assert response.status_code == 200
    assert response.json()["refreshed_assistants"] == 1
    assert response.json()["warnings"] == []
    assert [item["assistant_id"] for item in refreshed] == ["demo-main"]
    assert refreshed[0]["capability_profile"] == "demo-profile"
    assert refreshed[0]["provision_runtime"] is False
    profile_path = tmp_path / "capabilities" / "demo-profile.yaml"
    saved = profile_path.read_text(encoding="utf-8")
    assert "allowed_tools:" in saved
    assert "- write" in saved
    assert "- exec" in saved
    runtime_config = json.loads((state_root / "ironagent.json").read_text(encoding="utf-8"))
    assert runtime_config["tools"]["allow"] == ["exec", "read", "write"]


def test_admin_bootstrap_requires_internal_service_auth() -> None:
    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)

    response = client.get("/api/v1/admin/bootstrap/fast")

    assert response.status_code == 401


def test_admin_readyz_reports_workspace_item_index_path(monkeypatch, tmp_path) -> None:
    item_index_path = tmp_path / "persistent" / "workspace-items.sqlite"
    monkeypatch.setenv("IRON_AGENT_WORKSPACE_ITEM_INDEX_SQLITE_PATH", str(item_index_path))
    monkeypatch.setenv("IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND", "sqlite")
    get_settings.cache_clear()
    try:
        app = admin_web_app.create_admin_web_app()
        client = TestClient(app)

        response = client.get("/readyz")

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["checks"]["workspaceItemIndex"]["path"] == str(item_index_path)
        assert payload["checks"]["workspaceItemIndex"]["writable"] is True
        assert payload["checks"]["workspaceFileMetadata"]["backend"] == "sqlite"
        assert item_index_path.parent.exists()
    finally:
        get_settings.cache_clear()


def test_index_does_not_embed_internal_service_token() -> None:
    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "__IRON_AGENT_INTERNAL_SERVICE_TOKEN__" not in response.text
    assert "x-ironagent-internal-token" not in response.text.lower()
    assert "change-me" not in response.text


def test_index_uses_admin_prefixed_login_when_served_by_gateway() -> None:
    index_html = (Path(admin_web_app.__file__).with_name("static") / "index.html").read_text(
        encoding="utf-8"
    )

    assert (
        "const loginPath = window.location.pathname.startsWith('/admin') "
        "? '/admin/login' : '/login';"
    ) in index_html
    assert (
        "window.location.href = `${loginPath}?next=${encodeURIComponent(next || '/')}`;"
        in index_html
    )


def test_loopback_index_sets_direct_admin_session_cookie() -> None:
    app = admin_web_app.create_admin_web_app()
    client = TestClient(
        app,
        base_url="http://127.0.0.1:18501",
        client=("127.0.0.1", 50000),
    )

    response = client.get("/")

    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie", "")
    assert "iron_agent_admin_direct_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "change-me" not in response.text
    assert "change-me" not in set_cookie


def test_docker_loopback_proxy_index_sets_direct_admin_session_cookie() -> None:
    app = admin_web_app.create_admin_web_app()
    client = TestClient(
        app,
        base_url="http://127.0.0.1:18501",
        client=("192.168.65.1", 50000),
    )

    response = client.get("/")

    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie", "")
    assert "iron_agent_admin_direct_session=" in set_cookie
    assert "change-me" not in set_cookie


def test_loopback_direct_admin_session_authenticates_api() -> None:
    app = admin_web_app.create_admin_web_app()

    @app.get("/__test_admin_only")
    async def admin_only(_admin: None = Depends(admin_web_app.require_internal_service_token)):
        return {"ok": True}

    client = TestClient(
        app,
        base_url="http://127.0.0.1:18501",
        client=("127.0.0.1", 50000),
    )

    assert client.get("/__test_admin_only").status_code == 401
    assert client.get("/").status_code == 200
    response = client.get("/__test_admin_only")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_loopback_login_refreshes_session_and_returns_next() -> None:
    app = admin_web_app.create_admin_web_app()
    client = TestClient(
        app,
        base_url="http://127.0.0.1:18501",
        client=("127.0.0.1", 50000),
    )

    response = client.get("/login?next=/audit", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/audit"
    set_cookie = response.headers.get("set-cookie", "")
    assert "iron_agent_admin_direct_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "change-me" not in set_cookie


def test_admin_audit_logs_filter_export_and_cleanup(monkeypatch, tmp_path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    archive_path = tmp_path / "archive.jsonl"
    monkeypatch.setenv("IRON_AGENT_STORAGE_BACKEND", "json")
    monkeypatch.setenv("IRON_AGENT_AUDIT_LOG_PATH", str(audit_path))
    monkeypatch.setenv("IRON_AGENT_AUDIT_ARCHIVE_PATH", str(archive_path))
    monkeypatch.setenv("IRON_AGENT_AUDIT_RETENTION_DAYS", "45")
    monkeypatch.setenv("IRON_AGENT_APPROVAL_STORE_PATH", str(tmp_path / "approvals.json"))
    monkeypatch.setenv("IRON_AGENT_ASSISTANTS_STORE_PATH", str(tmp_path / "assistants.json"))
    get_settings.cache_clear()

    now = datetime.now(UTC)
    repository = JsonAuditRepository(audit_path)
    repository.append_event(
        {
            "envelope": {
                "request_id": "old-req",
                "hook_name": "control_session",
                "occurred_at": (now - timedelta(days=60)).isoformat(),
            },
            "session": {"user_id": "admin-old"},
            "assistant": {"assistant_id": "assistant-old"},
            "operation_type": "control_session.expire",
            "result": {"success": False, "status": "failure", "error_code": "expired"},
        }
    )
    repository.append_event(
        {
            "envelope": {
                "request_id": "current-req",
                "hook_name": "control_session",
                "occurred_at": now.isoformat(),
            },
            "session": {"user_id": "admin-current"},
            "assistant": {"assistant_id": "assistant-current"},
            "operation_type": "control_session.reject",
            "result": {"success": False, "status": "failure", "error_code": "origin_mismatch"},
        }
    )

    try:
        app = admin_web_app.create_admin_web_app()
        client = TestClient(app)

        page = client.get(
            "/api/v1/admin/audit-logs",
            params={
                "start_time": (now - timedelta(minutes=1)).isoformat(),
                "end_time": (now + timedelta(minutes=1)).isoformat(),
                "user_id": "admin-current",
                "assistant_id": "assistant-current",
                "operation_type": "reject",
                "result": "failure",
                "error_code": "origin_mismatch",
            },
            headers=_admin_headers(),
        )
        csv_export = client.get(
            "/api/v1/admin/audit-logs/export",
            params={"format": "csv", "error_code": "origin_mismatch"},
            headers=_admin_headers(),
        )
        json_export = client.get(
            "/api/v1/admin/audit-logs/export",
            params={"format": "json", "error_code": "origin_mismatch"},
            headers=_admin_headers(),
        )
        cleanup = client.post(
            "/api/v1/admin/audit-logs/retention/cleanup",
            headers=_admin_headers(),
        )

        assert page.status_code == 200, page.text
        assert page.json()["auditPagination"]["total"] == 1
        assert page.json()["auditLogs"][0]["request_id"] == "current-req"
        assert page.json()["auditLogs"][0]["error_code"] == "origin_mismatch"
        assert csv_export.status_code == 200, csv_export.text
        assert "current-req" in csv_export.text
        assert "origin_mismatch" in csv_export.text
        assert csv_export.headers["X-IronAgent-Export-Page"] == "1"
        assert csv_export.headers["X-IronAgent-Export-Page-Size"] == "1000"
        assert csv_export.headers["X-IronAgent-Export-Total"] == "1"
        assert csv_export.headers["X-IronAgent-Export-Truncated"] == "false"
        assert json_export.status_code == 200, json_export.text
        assert json_export.json()[0]["request_id"] == "current-req"
        assert cleanup.status_code == 200, cleanup.text
        assert cleanup.json()["deleted"] == 1
        assert cleanup.json()["archived"] == 1
        assert "old-req" in archive_path.read_text(encoding="utf-8")
    finally:
        get_settings.cache_clear()


def test_admin_audit_export_uses_bounded_pagination(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def audit_payload(index: int) -> dict[str, object]:
        return {
            "envelope": {
                "request_id": f"req-{index}",
                "hook_name": "control_session",
                "occurred_at": "2026-05-20T00:00:00+00:00",
            },
            "session": {"user_id": "admin"},
            "assistant": {"assistant_id": "assistant-main"},
            "tool": {"name": "shell"},
            "operation_type": "control_session.allow",
            "decision": "allow",
            "result": {"success": True, "status": "success"},
        }

    class StubAssistantService:
        def list_assistants(self):
            return []

    class StubAuditRepository:
        def list_page(self, *, limit: int, offset: int, filters=None):
            calls.append({"limit": limit, "offset": offset, "filters": filters})
            total = 5
            return SimpleNamespace(
                items=[audit_payload(index) for index in range(offset, min(offset + limit, total))],
                total=total,
            )

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "build_audit_repository", lambda: StubAuditRepository())

    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)

    default_response = client.get(
        "/api/v1/admin/audit-logs/export",
        params={"format": "json"},
        headers=_admin_headers(),
    )
    paged_response = client.get(
        "/api/v1/admin/audit-logs/export",
        params={"format": "json", "page": "2", "page_size": "2"},
        headers=_admin_headers(),
    )

    assert default_response.status_code == 200, default_response.text
    assert calls[0]["limit"] == 1000
    assert calls[0]["offset"] == 0
    assert default_response.headers["X-IronAgent-Export-Total"] == "5"
    assert default_response.headers["X-IronAgent-Export-Truncated"] == "false"

    assert paged_response.status_code == 200, paged_response.text
    assert calls[1]["limit"] == 2
    assert calls[1]["offset"] == 2
    assert paged_response.json()[0]["request_id"] == "req-2"
    assert paged_response.headers["X-IronAgent-Export-Page"] == "2"
    assert paged_response.headers["X-IronAgent-Export-Page-Size"] == "2"
    assert paged_response.headers["X-IronAgent-Export-Total"] == "5"
    assert paged_response.headers["X-IronAgent-Export-Total-Pages"] == "3"
    assert paged_response.headers["X-IronAgent-Export-Has-Next"] == "true"
    assert paged_response.headers["X-IronAgent-Export-Truncated"] == "true"


def test_assistant_status_ui_prefers_sse_and_short_poll_fallback() -> None:
    index_html = (Path(admin_web_app.__file__).with_name("static") / "index.html").read_text(
        encoding="utf-8"
    )

    assert "new EventSource('/api/v1/admin/assistants/status/stream')" in index_html
    assert "ASSISTANT_STATUS_FALLBACK_POLL_MS = 4000" in index_html
    assert "'/api/v1/admin/assistants/status'" in index_html
    assert "'/api/v1/admin/assistants/status?refresh=true'" in index_html
    assert "apiRequest(endpoint)" in index_html
    assert "assistantStatusSync" in index_html
    assert (
        "mergeAssistantStatusPayload(payload, { source: 'fallback', render: false })" in index_html
    )
    assert "document.addEventListener('visibilitychange', handleVisibilityChange)" in index_html
    assert "void refreshAssistantStatus({ silent: true });" in index_html
    assert "void refreshBootstrap({ fast: true, silent: true }).then" not in index_html
    assert "assistant-detail-status-sync" in index_html
    assert "displayStatus" in index_html
    assert "assistantDisplayState" in index_html
    assert "assistantDisplayBadge" in index_html
    assert "void syncWorkspaceFolders({ silent: true });" in index_html
    assert "void syncWorkspaceFiles({ silent: true, sync: true });" not in index_html
    assert "sourceName && sourceName !== itemName" in index_html
    assert "if (!isStartAction && !confirm(" in index_html
    assert "确认重启助理" in index_html
    assert "确认删除助理" in index_html


def test_assistant_status_snapshot_reads_db_state_without_runtime_probe(
    monkeypatch,
) -> None:
    checked_at = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
    assistant = SimpleNamespace(
        assistant_id="demo-main",
        runtime=SimpleNamespace(
            status="running",
            container_name="ironagent-assistant-demo-main",
            gateway_url="http://127.0.0.1:49153",
            updated_at=checked_at,
            last_error="",
        ),
    )
    captured: dict[str, bool] = {}

    class StubAssistantService:
        def list_assistants(self):
            captured["listed"] = True
            return [assistant]

        def sync_runtime_views(self):
            raise AssertionError("status stream must not probe runtime")

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)

    payload = admin_web_app._assistant_status_snapshot_sync()

    assert captured["listed"] is True
    assert payload["fallbackPollMs"] == 4000
    assert payload["runtimeProbeDegraded"] is False
    assert payload["pollingSource"] == "status_snapshot"
    assert payload["partial"] is False
    assert payload["assistants"] == [
        {
            "id": "demo-main",
            "status": "running",
            "container": "ironagent-assistant-demo-main",
            "gateway": "http://127.0.0.1:49153",
            "lastStatusAt": checked_at.isoformat(timespec="seconds"),
            "lastError": "",
            "observedState": "running",
            "desiredState": "running",
            "observedStatus": "running",
            "desiredStatus": "running",
            "syncState": "current",
            "lastObservedAt": checked_at.isoformat(timespec="seconds"),
            "lastReconciledAt": checked_at.isoformat(timespec="seconds"),
            "lastRuntimeError": "",
            "runtimeProbeDegraded": False,
            "pollingSource": "status_snapshot",
            "partial": False,
            "displayStatus": "running",
            "displaySeverity": "success",
            "displayLabel": "运行中",
            "displayReason": "current",
            "displaySource": "status_snapshot",
            "displayPartial": False,
            "displayObservedAt": checked_at.isoformat(timespec="seconds"),
        }
    ]


def test_assistant_status_snapshot_excludes_deleted_assistants(monkeypatch) -> None:
    checked_at = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
    active = SimpleNamespace(
        assistant_id="active-main",
        deleted_at=None,
        runtime=SimpleNamespace(
            status="running",
            container_name="ironagent-assistant-active-main",
            gateway_url="http://127.0.0.1:49153",
            updated_at=checked_at,
            last_error="",
        ),
    )
    deleted = SimpleNamespace(
        assistant_id="deleted-main",
        deleted_at=checked_at,
        runtime=SimpleNamespace(
            status="stopped",
            container_name="ironagent-assistant-deleted-main",
            gateway_url="",
            updated_at=checked_at,
            last_error="assistant soft deleted",
        ),
    )

    class StubAssistantService:
        def list_assistants(self):
            return [active, deleted]

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)

    payload = admin_web_app._assistant_status_snapshot_sync()

    assert [item["id"] for item in payload["assistants"]] == ["active-main"]


def test_assistant_status_snapshot_reports_degraded_when_db_read_fails(monkeypatch) -> None:
    class StubAssistantService:
        def list_assistants(self):
            raise ConnectionResetError("repository reset by peer")

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)

    payload = admin_web_app._assistant_status_snapshot_sync()

    assert payload["runtimeProbeDegraded"] is True
    assert payload["assistants"] == []


def test_assistant_status_endpoint_uses_process_cache(monkeypatch) -> None:
    checked_at = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
    assistant = SimpleNamespace(
        assistant_id="demo-main",
        runtime=SimpleNamespace(
            status="running",
            container_name="ironagent-assistant-demo-main",
            gateway_url="http://127.0.0.1:49153",
            updated_at=checked_at,
            last_error="",
        ),
    )
    calls = {"list": 0}

    class StubAssistantService:
        def list_assistants(self):
            calls["list"] += 1
            return [assistant]

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)

    client = TestClient(admin_web_app.create_admin_web_app())
    first = client.get("/api/v1/admin/assistants/status", headers=_admin_headers())
    second = client.get("/api/v1/admin/assistants/status", headers=_admin_headers())
    refreshed = client.get(
        "/api/v1/admin/assistants/status?refresh=true",
        headers=_admin_headers(),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert refreshed.status_code == 200
    assert calls["list"] == 2
    assert first.json()["cacheStatus"] == "fresh"
    assert second.json()["cacheStatus"] == "fresh"
    assert refreshed.json()["cacheStatus"] == "fresh"


def test_admin_users_endpoint_batches_user_bindings(monkeypatch) -> None:
    calls = {"grouped": 0, "per_user": 0}
    users = [
        SimpleNamespace(user_id="user-a", display_name="User A", department="研发", role="user"),
        SimpleNamespace(user_id="user-b", display_name="User B", department="测试", role="admin"),
    ]
    bindings_by_user = {
        "user-a": [SimpleNamespace(assistant_id="assistant-1")],
        "user-b": [
            SimpleNamespace(assistant_id="assistant-2"),
            SimpleNamespace(assistant_id="assistant-3"),
        ],
    }

    class StubUserService:
        def list_users(self):
            return users

        def list_bindings_grouped_by_user(self):
            calls["grouped"] += 1
            return bindings_by_user

        def list_bindings_for_user(self, user_id):
            calls["per_user"] += 1
            raise AssertionError("admin users endpoint must batch bindings")

    monkeypatch.setattr(admin_web_app, "UserService", StubUserService)

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.get("/api/v1/admin/users", headers=_admin_headers())

    assert response.status_code == 200
    assert calls == {"grouped": 1, "per_user": 0}
    assert response.json()["users"] == [
        {
            "id": "user-a",
            "name": "User A",
            "department": "研发",
            "role": "user",
            "assistants": ["assistant-1"],
        },
        {
            "id": "user-b",
            "name": "User B",
            "department": "测试",
            "role": "admin",
            "assistants": ["assistant-2", "assistant-3"],
        },
    ]
    assert response.json()["userProbeDegraded"] is False


def test_runtime_health_check_reconciles_without_formal_audit(monkeypatch) -> None:
    captured: dict[str, bool] = {}
    assistant = SimpleNamespace(
        assistant_id="demo-main",
        runtime=SimpleNamespace(
            status="restarting",
            updated_at=datetime.now(UTC) - timedelta(seconds=120),
        ),
    )

    class StubAssistantService:
        def list_assistants(self):
            return [assistant]

        def sync_runtime_views(self, assistants):
            captured["synced"] = True
            assert assistants == [assistant]
            return [assistant], {}, 1

    def fail_if_audit_is_built():
        raise AssertionError("runtime health reconciliation must not write formal audit")

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "build_audit_repository", fail_if_audit_is_built)

    result = admin_web_app._run_assistant_runtime_health_check()

    assert captured["synced"] is True
    assert result == {"checked": 1, "updated": 1}


def test_runtime_health_check_skips_fresh_restarting_assistants(monkeypatch) -> None:
    assistant = SimpleNamespace(
        assistant_id="demo-main",
        runtime=SimpleNamespace(
            status="restarting",
            updated_at=datetime.now(UTC),
        ),
    )

    class StubAssistantService:
        def list_assistants(self):
            return [assistant]

        def sync_runtime_views(self, assistants):
            raise AssertionError("fresh restarting assistants should not be probed")

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)

    assert admin_web_app._run_assistant_runtime_health_check() == {
        "checked": 0,
        "updated": 0,
    }


def test_runtime_health_check_skips_stable_assistants(monkeypatch) -> None:
    assistant = SimpleNamespace(
        assistant_id="demo-main",
        runtime=SimpleNamespace(status="running"),
    )

    class StubAssistantService:
        def list_assistants(self):
            return [assistant]

        def sync_runtime_views(self, assistants):
            raise AssertionError("stable assistants should not be probed")

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)

    assert admin_web_app._run_assistant_runtime_health_check() == {
        "checked": 0,
        "updated": 0,
    }


def test_runtime_health_check_skips_deleted_assistants(monkeypatch) -> None:
    assistant = SimpleNamespace(
        assistant_id="demo-main",
        deleted=True,
        runtime=SimpleNamespace(status="restarting"),
    )

    class StubAssistantService:
        def list_assistants(self):
            return [assistant]

        def sync_runtime_views(self, assistants):
            raise AssertionError("deleted assistants should not be probed")

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)

    assert admin_web_app._run_assistant_runtime_health_check() == {
        "checked": 0,
        "updated": 0,
    }


def test_runtime_health_check_skips_deleted_at_assistants(monkeypatch) -> None:
    assistant = SimpleNamespace(
        assistant_id="demo-main",
        deleted_at=datetime.now(UTC),
        runtime=SimpleNamespace(status="restarting"),
    )

    class StubAssistantService:
        def list_assistants(self):
            return [assistant]

        def sync_runtime_views(self, assistants):
            raise AssertionError("soft-deleted assistants should not be probed")

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)

    assert admin_web_app._run_assistant_runtime_health_check() == {
        "checked": 0,
        "updated": 0,
    }


def test_admin_runtime_background_task_uses_dedicated_executor(monkeypatch) -> None:
    thread_names: list[str] = []

    def fake_restart_task(assistant_id: str) -> None:
        assert assistant_id == "demo-main"
        thread_names.append(threading.current_thread().name)

    monkeypatch.setattr(admin_web_app, "_run_restart_assistant_task", fake_restart_task)
    app = admin_web_app.create_admin_web_app()
    try:
        asyncio.run(admin_web_app._restart_assistant_in_background(app, "demo-main"))
    finally:
        admin_web_app._shutdown_admin_runtime_task_executor(app)

    assert thread_names
    assert thread_names[0].startswith("admin-runtime-task")


def test_restart_background_task_uses_sync_bridge_path(monkeypatch) -> None:
    captured: dict[str, str] = {}

    class StubAssistantService:
        async def arestart_assistant(self, assistant_id: str):
            raise AssertionError("background restart must not create a private event loop")

        def restart_assistant(self, assistant_id: str):
            captured["assistant_id"] = assistant_id
            return SimpleNamespace(
                assistant_id=assistant_id,
                runtime=SimpleNamespace(status="running"),
            )

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "_sync_runtime_control_ui_allowed_origins", lambda *_: True)

    admin_web_app._run_restart_assistant_task("demo-main")

    assert captured["assistant_id"] == "demo-main"


def test_runtime_health_check_uses_sync_bridge_path(monkeypatch) -> None:
    captured: dict[str, object] = {}
    assistant = SimpleNamespace(
        assistant_id="demo-main",
        runtime=SimpleNamespace(status="starting"),
    )

    class StubAssistantService:
        async def alist_assistants(self):
            raise AssertionError("runtime health check must not create a private event loop")

        def list_assistants(self):
            return [assistant]

        async def async_runtime_views(self, assistants):
            raise AssertionError("runtime health check must not create a private event loop")

        def sync_runtime_views(self, assistants):
            captured["assistants"] = assistants
            return assistants, {}, 1

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)

    assert admin_web_app._run_assistant_runtime_health_check() == {
        "checked": 1,
        "updated": 1,
    }
    assert captured["assistants"] == [assistant]


def test_manual_assistant_lifecycle_routes_append_formal_audit(monkeypatch) -> None:
    audit_events: list[dict[str, object]] = []

    class StubAssistantService:
        def get_assistant(self, assistant_id: str):
            return SimpleNamespace(
                assistant_id=assistant_id,
                runtime=SimpleNamespace(status="running"),
            )

        def start_assistant(self, assistant_id: str):
            return SimpleNamespace(
                assistant_id=assistant_id,
                runtime=SimpleNamespace(status="running"),
            )

        def stop_assistant(self, assistant_id: str):
            return SimpleNamespace(
                assistant_id=assistant_id,
                runtime=SimpleNamespace(status="stopped"),
            )

        def mark_assistant_restarting(self, assistant_id: str):
            return SimpleNamespace(
                assistant_id=assistant_id,
                runtime=SimpleNamespace(status="restarting"),
            )

        def restart_assistant(self, assistant_id: str):
            return SimpleNamespace(
                assistant_id=assistant_id,
                runtime=SimpleNamespace(status="running"),
            )

        def delete_assistant(self, assistant_id: str):
            return SimpleNamespace(
                assistant_id=assistant_id,
                soft_deleted=True,
                deleted_at=datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC),
                runtime_disabled=True,
                container_removed=True,
                workspace_removed=False,
                state_removed=False,
            )

    class StubAuditRepository:
        def append_event(self, payload: dict[str, object]) -> None:
            audit_events.append(payload)

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "build_audit_repository", lambda: StubAuditRepository())
    monkeypatch.setattr(admin_web_app, "_sync_runtime_control_ui_allowed_origins", lambda *_: False)

    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)
    headers = {**_admin_headers(), "X-Admin-Operator": "operator-1"}

    start_response = client.post(
        "/api/v1/admin/assistants/demo-main/start",
        headers=headers,
    )
    stop_response = client.post(
        "/api/v1/admin/assistants/demo-main/stop",
        headers=headers,
    )
    restart_response = client.post(
        "/api/v1/admin/assistants/demo-main/restart",
        headers=headers,
    )
    delete_response = client.delete(
        "/api/v1/admin/assistants/demo-main",
        headers=headers,
    )

    assert start_response.status_code == 200, start_response.text
    assert stop_response.status_code == 200, stop_response.text
    assert restart_response.status_code == 200, restart_response.text
    assert delete_response.status_code == 200, delete_response.text
    assert [event["operation_type"] for event in audit_events] == [
        "assistant_start",
        "assistant_stop",
        "assistant_restart",
        "assistant_delete",
    ]
    for event in audit_events:
        assert event["envelope"]["hook_name"] == "assistant_lifecycle"
        assert event["operator"]["operator_id"] == "operator-1"
        assert event["session"]["user_id"] == "operator-1"
        assert event["assistant"]["assistant_id"] == "demo-main"
        assert event["result"]["status"] == "success"


def test_index_uses_packaged_static_assets() -> None:
    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "https://cdn.tailwindcss.com" not in response.text
    assert "https://unpkg.com/lucide" not in response.text
    assert 'href="static/assets/admin.css"' in response.text
    assert 'src="static/assets/lucide.min.js"' in response.text

    css_response = client.get("/static/assets/admin.css")
    js_response = client.get("/static/assets/lucide.min.js")

    assert css_response.status_code == 200
    assert js_response.status_code == 200
    assert "bg-apple-blue" in css_response.text
    assert "window.lucide" in js_response.text


def test_index_main_views_are_sibling_sections() -> None:
    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    main_fragment = html[
        html.index("<!-- Scrollable Area -->") : html.index("<!-- MODALS -->")
    ]
    depth = 0
    view_depths: dict[str, int] = {}
    for match in re.finditer(r"</div>|<div\b([^>]*)>", main_fragment):
        if match.group(0) == "</div>":
            depth -= 1
            continue
        attrs = match.group(1) or ""
        view_id = re.search(r'id="(view-[^"]+)"', attrs)
        if view_id:
            view_depths[view_id.group(1)] = depth
        depth += 1

    assert depth == 0
    assert view_depths == {
        "view-dashboard": 2,
        "view-approvals": 2,
        "view-market": 2,
        "view-files": 2,
        "view-models": 2,
        "view-assistants": 2,
        "view-audit": 2,
        "view-users": 2,
        "view-whitelist": 2,
    }


def test_build_model_payload_masks_api_key() -> None:
    class StubModelRegistryService:
        def assistant_ids_for_model(self, item, assistants=None):
            return ["demo-main"]

    payload = _build_model_payload(
        SimpleNamespace(
            model_id="modelstudio-qwen",
            name="qwen",
            provider="modelstudio",
            url="https://example.com/v1",
            api_key="secret-key",
            status="active",
        ),
        StubModelRegistryService(),
        [],
    )

    assert payload["apiKey"] == "******"
    assert payload["apiKeySet"] is True
    assert payload["assistantIds"] == ["demo-main"]


def test_admin_models_requires_token_and_masks_api_key(monkeypatch) -> None:
    model = SimpleNamespace(
        model_id="modelstudio-qwen",
        name="qwen",
        provider="modelstudio",
        url="https://example.com/v1",
        api_key="secret-key",
        status="active",
    )

    class StubModelRegistryService:
        def import_from_assistants(self, assistants):
            return [model]

        def assistant_ids_for_model(self, item, assistants=None):
            return []

    class StubAssistantService:
        def list_assistants(self):
            return []

    monkeypatch.setattr(admin_web_app, "ModelRegistryService", StubModelRegistryService)
    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)

    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)

    assert client.get("/api/v1/admin/models").status_code == 401
    response = client.get(
        "/api/v1/admin/models",
        headers={"X-IronAgent-Internal-Token": "change-me"},
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["apiKey"] == "******"
    assert item["apiKeySet"] is True


def test_admin_model_detail_uses_same_assistant_context_as_list(monkeypatch) -> None:
    captured: dict[str, object] = {}
    assistant = SimpleNamespace(assistant_id="demo-main")
    model = SimpleNamespace(
        model_id="modelstudio-qwen",
        name="qwen",
        provider="modelstudio",
        url="https://example.com/v1",
        api_key="secret-key",
        status="active",
    )

    class StubModelRegistryService:
        def import_from_assistants(self, assistants):
            captured["import_assistants"] = assistants
            return [model]

        def get_model(self, model_id):
            captured["model_id"] = model_id
            return model

        def assistant_ids_for_model(self, item, assistants=None):
            captured["payload_assistants"] = assistants
            return [assistant.assistant_id]

    class StubAssistantService:
        def list_assistants(self):
            return [assistant]

    monkeypatch.setattr(admin_web_app, "ModelRegistryService", StubModelRegistryService)
    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.get(
        "/api/v1/admin/models/modelstudio-qwen",
        headers=_admin_headers(),
    )

    assert response.status_code == 200
    assert captured["model_id"] == "modelstudio-qwen"
    assert captured["import_assistants"] == [assistant]
    assert captured["payload_assistants"] == [assistant]
    payload = response.json()
    assert payload["apiKey"] == "******"
    assert payload["assistantIds"] == ["demo-main"]


def test_admin_model_probe_reports_runtime_auth_failure(monkeypatch) -> None:
    model = SimpleNamespace(
        model_id="host-openai-codex",
        name="openai-codex/gpt-5.4",
        provider="host_iron_agent",
        url="",
        api_key="",
        status="active",
    )
    assistant = SimpleNamespace(assistant_id="demo-main")

    class StubModelRegistryService:
        def probe_model(self, model_id):
            return {
                "model_id": model_id,
                "ok": True,
                "status": "configured",
                "message": "configured",
            }

        def get_model(self, model_id):
            return model

        def assistant_ids_for_model(self, item, assistants=None):
            return [assistant.assistant_id]

    class StubAssistantService:
        def list_assistants(self):
            return [assistant]

        def sync_runtime_views(self, assistants):
            return (
                assistants,
                {
                    "demo-main": SimpleNamespace(
                        last_error="OpenAI Codex OAuth token refresh failed"
                    )
                },
                1,
            )

    monkeypatch.setattr(admin_web_app, "ModelRegistryService", StubModelRegistryService)
    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.post(
        "/api/v1/admin/models/host-openai-codex/probe",
        headers=_admin_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["status"] == "auth_failed"
    assert "OAuth" in payload["message"]
    assert payload["assistant_ids"] == ["demo-main"]


def test_admin_approvals_list_returns_pending_items(monkeypatch) -> None:
    captured: dict[str, object] = {}
    requested_at = datetime(2026, 5, 20, 8, 0, tzinfo=UTC)

    class StubApprovalService:
        def list_requests(self, status=None):
            captured["status"] = status
            return [
                ApprovalRequest(
                    id="approval-1",
                    tool_name="workspace_file_delete",
                    assistant_id="demo-main",
                    capability_profile="dangerous",
                    approval_scope="admin",
                    reason="delete persistent file",
                    target="persistent/report.xlsx",
                    status="pending",
                    requested_at=requested_at,
                    expires_at=requested_at + timedelta(minutes=15),
                )
            ]

    monkeypatch.setattr(admin_web_app, "ApprovalService", StubApprovalService)

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.get(
        "/api/v1/admin/approvals?status=pending",
        headers=_admin_headers(),
    )

    assert response.status_code == 200
    assert captured["status"] == "pending"
    payload = response.json()
    assert payload["approvalProbeDegraded"] is False
    assert payload["approvals"] == [
        {
            "id": "approval-1",
            "kind": "admin",
            "name": "workspace_file_delete",
            "owner": "demo-main",
            "assistantId": "demo-main",
            "assistantProfile": "dangerous",
            "status": "pending",
            "time": "2026-05-20T08:00:00+00:00",
            "expiresAt": "2026-05-20T08:15:00+00:00",
            "reason": "delete persistent file",
            "operatedFile": "persistent/report.xlsx",
            "file": "persistent/report.xlsx",
            "userId": "",
        }
    ]


def test_create_assistant_normalizes_profile_and_preserves_provider_config(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class StubAssistantService:
        def list_assistants(self):
            return []

        def upsert_assistant(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                assistant_id=kwargs["assistant_id"],
                runtime=SimpleNamespace(status="running"),
            )

    class StubModelRegistryService:
        def resolve_model_config(self, model_id: str):
            raise AssertionError(f"unexpected model registry lookup: {model_id}")

    class StubWorkspaceSyncService:
        def __init__(self, assistant_service=None):
            self.assistant_service = assistant_service

        async def async_sync_assistant_workspace(self, assistant_id: str) -> None:
            captured["synced_assistant_id"] = assistant_id

    class StubUserService:
        def get_user(self, user_id: str):
            captured["get_user_id"] = user_id
            return SimpleNamespace(user_id=user_id)

        def bind_assistant(self, user_id: str, assistant_id: str, **kwargs):
            captured["binding"] = {"user_id": user_id, "assistant_id": assistant_id, **kwargs}
            return SimpleNamespace(user_id=user_id, assistant_id=assistant_id)

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(
        admin_web_app,
        "ModelRegistryService",
        StubModelRegistryService,
    )
    monkeypatch.setattr(
        admin_web_app,
        "WorkspaceSyncService",
        StubWorkspaceSyncService,
    )
    monkeypatch.setattr(admin_web_app, "UserService", StubUserService)

    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)
    response = client.post(
        "/api/v1/admin/assistants",
        headers=_admin_headers(),
        json={
            "assistant_id": "demo-created",
            "name": "Demo Created",
            "capability_profile": "general",
            "user_id": "admin-user-001",
            "notes": "runtime smoke",
            "primary_model": "modelstudio/qwen3-coder-next",
            "provider_id": "modelstudio",
            "provider_base_url": "https://coding.dashscope.aliyuncs.com/v1",
            "provider_api_key": "secret-key",
            "workspace_slug": "demo-created",
        },
    )

    assert response.status_code == 200
    assert captured["assistant_id"] == "demo-created"
    assert captured["capability_profile"] == "chat"
    model_config = captured["model_config"]
    assert model_config.primary_model == "modelstudio/qwen3-coder-next"
    assert model_config.auth_mode == "provider_api_key"
    assert model_config.provider_id == "modelstudio"
    assert model_config.provider_base_url == "https://coding.dashscope.aliyuncs.com/v1"
    assert model_config.provider_api_key == "secret-key"
    assert captured["synced_assistant_id"] == "demo-created"
    assert captured["binding"] == {
        "user_id": "admin-user-001",
        "assistant_id": "demo-created",
        "alias": "Demo Created",
        "source_system": "admin-assistant-create",
    }


def test_create_assistant_requires_user_id() -> None:
    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)
    response = client.post(
        "/api/v1/admin/assistants",
        headers=_admin_headers(),
        json={
            "assistant_id": "demo-created",
            "name": "Demo Created",
            "capability_profile": "chat",
            "primary_model": "modelstudio/qwen3-coder-next",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "user_id is required"


def test_create_assistant_rejects_unknown_user_id(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class StubAssistantService:
        def list_assistants(self):
            return []

        def upsert_assistant(self, **kwargs):
            raise AssertionError("assistant should not be created for an unknown user")

    class StubUserService:
        def get_user(self, user_id: str):
            captured["get_user_id"] = user_id
            return None

        def create_user(self, user_id: str, **kwargs):
            raise AssertionError("admin assistant creation must not auto-create users")

        def bind_assistant(self, user_id: str, assistant_id: str, **kwargs):
            raise AssertionError("unknown user should not be bound to an assistant")

    class StubModelRegistryService:
        def resolve_model_config(self, model_id: str):
            raise AssertionError(f"unexpected model registry lookup: {model_id}")

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "UserService", StubUserService)
    monkeypatch.setattr(admin_web_app, "ModelRegistryService", StubModelRegistryService)

    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)
    response = client.post(
        "/api/v1/admin/assistants",
        headers=_admin_headers(),
        json={
            "assistant_id": "demo-created",
            "name": "Demo Created",
            "capability_profile": "chat",
            "primary_model": "modelstudio/qwen3-coder-next",
            "user_id": "missing-user-001",
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "user not found: missing-user-001"
    assert captured["get_user_id"] == "missing-user-001"


def test_create_assistant_binds_upstream_user_id(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class StubAssistantService:
        def list_assistants(self):
            return []

        def upsert_assistant(self, **kwargs):
            return SimpleNamespace(
                assistant_id=kwargs["assistant_id"],
                runtime=SimpleNamespace(status="running"),
            )

    class StubModelRegistryService:
        def resolve_model_config(self, model_id: str):
            raise AssertionError(f"unexpected model registry lookup: {model_id}")

    class StubWorkspaceSyncService:
        def __init__(self, assistant_service=None):
            self.assistant_service = assistant_service

        async def async_sync_assistant_workspace(self, assistant_id: str) -> None:
            captured["synced_assistant_id"] = assistant_id

    class StubUserService:
        def get_user(self, user_id: str):
            captured["get_user_id"] = user_id
            return SimpleNamespace(user_id=user_id)

        def create_user(self, user_id: str, **kwargs):
            raise AssertionError("admin assistant creation must not auto-create users")

        def bind_assistant(self, user_id: str, assistant_id: str, **kwargs):
            captured["binding"] = {"user_id": user_id, "assistant_id": assistant_id, **kwargs}
            return SimpleNamespace(user_id=user_id, assistant_id=assistant_id)

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "ModelRegistryService", StubModelRegistryService)
    monkeypatch.setattr(admin_web_app, "WorkspaceSyncService", StubWorkspaceSyncService)
    monkeypatch.setattr(admin_web_app, "UserService", StubUserService)

    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)
    response = client.post(
        "/api/v1/admin/assistants",
        headers=_admin_headers(),
        json={
            "assistant_id": "demo-created",
            "name": "Demo Created",
            "capability_profile": "chat",
            "primary_model": "modelstudio/qwen3-coder-next",
            "userId": "upstream-user-001",
        },
    )

    assert response.status_code == 200
    assert response.json()["binding"] == {
        "user_id": "upstream-user-001",
        "assistant_id": "demo-created",
    }
    assert captured["get_user_id"] == "upstream-user-001"
    assert captured["binding"] == {
        "user_id": "upstream-user-001",
        "assistant_id": "demo-created",
        "alias": "Demo Created",
        "source_system": "admin-assistant-create",
    }


def test_update_assistant_model_config_from_registry_masks_secret(monkeypatch) -> None:
    captured: dict[str, object] = {}
    existing_config = admin_web_app.AssistantIronAgentConfig(
        primary_model="old-model",
        auth_mode="host_iron_agent",
    )
    existing = SimpleNamespace(
        assistant_id="demo-main",
        name="Demo Main",
        capability_profile="dangerous",
        notes="existing notes",
        iron_agent_config=existing_config,
        workspace_config=SimpleNamespace(workspace_slug="demo-main"),
        runtime=SimpleNamespace(status="running"),
    )
    registry_config = admin_web_app.AssistantIronAgentConfig(
        primary_model="MiniMax-M2.5",
        auth_mode="provider_api_key",
        provider_id="dashscope-coding",
        provider_base_url="https://coding.dashscope.aliyuncs.com/v1",
        provider_api_key="secret-key",
    )

    class StubAssistantService:
        def get_assistant(self, assistant_id: str):
            captured["get_assistant_id"] = assistant_id
            return existing

        def upsert_assistant(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                assistant_id=kwargs["assistant_id"],
                runtime=SimpleNamespace(status="running"),
                iron_agent_config=kwargs["model_config"],
            )

    class StubModelRegistryService:
        def resolve_model_config(self, model_id: str):
            captured["model_id"] = model_id
            return registry_config

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "ModelRegistryService", StubModelRegistryService)

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.put(
        "/api/v1/admin/assistants/demo-main/model-config",
        headers=_admin_headers(),
        json={"model_id": "dashscope-coding-minimax-m2-5"},
    )

    assert response.status_code == 200
    assert captured["get_assistant_id"] == "demo-main"
    assert captured["model_id"] == "dashscope-coding-minimax-m2-5"
    assert captured["assistant_id"] == "demo-main"
    assert captured["name"] == "Demo Main"
    assert captured["capability_profile"] == "dangerous"
    assert captured["notes"] == "existing notes"
    assert captured["workspace_config"] == existing.workspace_config
    assert captured["provision_runtime"] is True
    model_config = captured["model_config"]
    assert model_config.primary_model == "MiniMax-M2.5"
    assert model_config.auth_mode == "provider_api_key"
    assert model_config.provider_id == "dashscope-coding"
    assert model_config.provider_base_url == "https://coding.dashscope.aliyuncs.com/v1"
    assert model_config.provider_api_key == "secret-key"
    payload = response.json()
    assert payload["model_config"] == {
        "primary_model": "MiniMax-M2.5",
        "auth_mode": "provider_api_key",
        "provider_id": "dashscope-coding",
        "provider_base_url": "https://coding.dashscope.aliyuncs.com/v1",
        "provider_api_key_set": True,
    }
    assert "secret-key" not in response.text


def test_create_assistant_rejects_duplicate_name(monkeypatch) -> None:
    existing = SimpleNamespace(
        assistant_id="existing-assistant",
        name="Demo Created",
    )

    class StubAssistantService:
        def list_assistants(self):
            return [existing]

        def upsert_assistant(self, **kwargs):
            raise AssertionError("duplicate assistant should not be created")

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)

    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)
    response = client.post(
        "/api/v1/admin/assistants",
        headers={"X-IronAgent-Internal-Token": "change-me"},
        json={
            "assistant_id": "new-assistant",
            "name": " demo   created ",
            "capability_profile": "chat",
            "user_id": "duplicate-user",
            "primary_model": "modelstudio/qwen3-coder-next",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "assistant name already exists: demo   created"


def test_delete_assistant_reports_soft_delete_and_keeps_workspace(monkeypatch) -> None:
    deleted_at = datetime(2026, 5, 17, 8, 30, tzinfo=UTC)

    class StubAssistantService:
        def get_assistant(self, assistant_id: str):
            return SimpleNamespace(
                assistant_id=assistant_id,
                runtime=SimpleNamespace(status="running"),
            )

        def delete_assistant(self, assistant_id: str):
            return SimpleNamespace(
                assistant_id=assistant_id,
                soft_deleted=True,
                runtime_disabled=True,
                deleted_at=deleted_at,
                container_removed=True,
                workspace_removed=False,
                state_removed=False,
            )

    class StubAuditRepository:
        def append_event(self, payload):
            return None

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "build_audit_repository", lambda: StubAuditRepository())

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.delete("/api/v1/admin/assistants/demo-main", headers=_admin_headers())

    assert response.status_code == 200
    assert response.json() == {
        "assistant_id": "demo-main",
        "status": "deleted",
        "deleted": True,
        "deleted_at": "2026-05-17T08:30:00+00:00",
        "runtime_disabled": True,
        "container_removed": True,
        "workspace_removed": False,
        "state_removed": False,
        "warnings": [],
    }


def test_resolve_approval_appends_audit_event(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class StubApprovalService:
        def resolve_request(self, approval_id: str, resolution: str) -> ApprovalRequest:
            captured["approval_id"] = approval_id
            captured["resolution"] = resolution
            return ApprovalRequest(
                id=approval_id,
                tool_name="workspace_file_overwrite",
                assistant_id="finance-bot",
                capability_profile="chat",
                approval_scope="admin",
                reason="overwrite persistent file",
                target="persistent/report.xlsx",
                status="approved" if resolution == "allow" else "denied",
                resolution="allow" if resolution == "allow" else "deny",
                requested_at=datetime(2026, 4, 20, 6, 0, tzinfo=UTC),
                resolved_at=datetime(2026, 4, 20, 6, 1, tzinfo=UTC),
            )

    class StubWorkspaceFileService:
        def apply_approval_resolution(self, approval_id: str) -> None:
            captured["applied_approval_id"] = approval_id

    class StubAuditRepository:
        def append_event(self, payload: dict[str, object]) -> None:
            captured["audit_payload"] = payload

    monkeypatch.setattr(admin_web_app, "ApprovalService", StubApprovalService)
    monkeypatch.setattr(admin_web_app, "WorkspaceFileService", StubWorkspaceFileService)
    monkeypatch.setattr(
        admin_web_app,
        "build_audit_repository",
        lambda: StubAuditRepository(),
    )

    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)
    response = client.post(
        "/api/v1/admin/approvals/approval-123/resolve",
        headers=_admin_headers(),
        json={"resolution": "allow"},
    )

    assert response.status_code == 200
    assert captured["applied_approval_id"] == "approval-123"
    audit_payload = captured["audit_payload"]
    assert audit_payload["envelope"]["hook_name"] == "approval_resolution"
    assert audit_payload["envelope"]["request_id"] == "approval-123"
    assert audit_payload["assistant"]["assistant_id"] == "finance-bot"
    assert audit_payload["tool"]["name"] == "workspace_file_overwrite"
    assert audit_payload["decision"] == "allow"
    assert audit_payload["result"]["success"] is True


def test_admin_approvals_endpoint_lists_pending_requests(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class StubApprovalService:
        def list_requests(self, *, status: str | None = None):
            captured["status"] = status
            return [
                ApprovalRequest(
                    id="approval-123",
                    tool_name="workspace_file_overwrite",
                    assistant_id="finance-bot",
                    capability_profile="chat",
                    approval_scope="admin",
                    reason="Overwrite persistent file: report.xlsx",
                    target="persistent/report.xlsx",
                    status="pending",
                    requested_at=datetime(2026, 4, 20, 6, 0, tzinfo=UTC),
                    expires_at=datetime(2026, 4, 20, 6, 15, tzinfo=UTC),
                )
            ]

    monkeypatch.setattr(admin_web_app, "ApprovalService", StubApprovalService)

    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)
    response = client.get(
        "/api/v1/admin/approvals?status=pending",
        headers=_admin_headers(),
    )

    assert response.status_code == 200
    assert captured["status"] == "pending"
    payload = response.json()
    assert payload["approvals"] == [
        {
            "id": "approval-123",
            "kind": "admin",
            "name": "workspace_file_overwrite",
            "owner": "finance-bot",
            "assistantId": "finance-bot",
            "assistantProfile": "chat",
            "status": "pending",
            "time": "2026-04-20T06:00:00+00:00",
            "expiresAt": "2026-04-20T06:15:00+00:00",
            "reason": "Overwrite persistent file: report.xlsx",
            "operatedFile": "persistent/report.xlsx",
            "file": "persistent/report.xlsx",
            "userId": "",
        }
    ]
    assert payload["approvalProbeDegraded"] is False


def test_resolve_approval_returns_warning_when_workspace_apply_fails(monkeypatch) -> None:
    class StubApprovalService:
        def resolve_request(self, approval_id: str, resolution: str) -> ApprovalRequest:
            return ApprovalRequest(
                id=approval_id,
                tool_name="workspace_file_delete",
                assistant_id="finance-bot",
                capability_profile="chat",
                approval_scope="admin",
                reason="delete persistent file",
                target="persistent/report.xlsx",
                status="approved" if resolution == "allow" else "denied",
                resolution="allow" if resolution == "allow" else "deny",
                requested_at=datetime(2026, 4, 20, 6, 0, tzinfo=UTC),
                resolved_at=datetime(2026, 4, 20, 6, 1, tzinfo=UTC),
            )

    class StubWorkspaceFileService:
        def apply_approval_resolution(self, approval_id: str) -> None:
            raise RuntimeError("workspace writer unavailable")

    class StubAuditRepository:
        def append_event(self, payload: dict[str, object]) -> None:
            pass

    monkeypatch.setattr(admin_web_app, "ApprovalService", StubApprovalService)
    monkeypatch.setattr(admin_web_app, "WorkspaceFileService", StubWorkspaceFileService)
    monkeypatch.setattr(
        admin_web_app,
        "build_audit_repository",
        lambda: StubAuditRepository(),
    )

    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)
    response = client.post(
        "/api/v1/admin/approvals/approval-123/resolve",
        headers=_admin_headers(),
        json={"resolution": "allow"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == "approval-123"
    assert payload["status"] == "approved"
    assert payload["apply_status"] == "failed"
    assert "workspace writer unavailable" in payload["warnings"][0]


def test_load_assistants_for_bootstrap_falls_back_when_runtime_probe_fails() -> None:
    assistant = SimpleNamespace(assistant_id="demo-bootstrap")

    class FailingAssistantService:
        def sync_runtime_views(self):
            raise ConnectionResetError("containerd socket reset by peer")

        def list_assistants(self):
            return [assistant]

    assistants, degraded = _load_assistants_for_bootstrap(FailingAssistantService())

    assert degraded is True
    assert assistants == [assistant]


def test_load_assistants_for_bootstrap_returns_empty_when_probe_and_fallback_fail() -> None:
    class FailingAssistantService:
        def sync_runtime_views(self):
            raise ConnectionResetError("containerd socket reset by peer")

        def list_assistants(self):
            raise ConnectionError("mysql unavailable")

    assistants, degraded = _load_assistants_for_bootstrap(FailingAssistantService())

    assert degraded is True
    assert assistants == []


def test_load_assistants_for_bootstrap_fast_probes_starting_assistants_only() -> None:
    running = SimpleNamespace(
        assistant_id="running-assistant",
        runtime=SimpleNamespace(status="running", gateway_url="", gateway_token=""),
    )
    running_missing_token = SimpleNamespace(
        assistant_id="running-missing-token",
        runtime=SimpleNamespace(
            status="running",
            gateway_url="http://10.244.144.101:18789",
            gateway_token="",
        ),
    )
    starting = SimpleNamespace(
        assistant_id="starting-assistant",
        runtime=SimpleNamespace(status="starting", gateway_url="", gateway_token=""),
    )
    updated_starting = SimpleNamespace(
        assistant_id="starting-assistant",
        runtime=SimpleNamespace(
            status="running",
            gateway_url="http://10.244.144.102:18789",
            gateway_token="token",
        ),
    )
    failed = SimpleNamespace(
        assistant_id="failed-assistant",
        runtime=SimpleNamespace(status="failed", gateway_url="", gateway_token=""),
    )
    captured: dict[str, list[str]] = {}

    class StubAssistantService:
        def list_assistants(self):
            return [running, running_missing_token, starting, failed]

        def sync_runtime_views(self, assistants):
            captured["assistant_ids"] = [item.assistant_id for item in assistants]
            return [updated_starting], {}, 1

    assistants, degraded = _load_assistants_for_bootstrap_fast(StubAssistantService())

    assert degraded is False
    assert captured["assistant_ids"] == ["starting-assistant"]
    assert assistants == [
        running,
        running_missing_token,
        updated_starting,
        failed,
    ]


def test_load_assistants_for_bootstrap_fast_does_not_probe_transient_assistants() -> None:
    starting = SimpleNamespace(
        assistant_id="starting-assistant",
        runtime=SimpleNamespace(status="starting"),
    )

    class StubAssistantService:
        def list_assistants(self):
            return [starting]

        def sync_runtime_views(self, assistants):
            raise AssertionError("fast bootstrap must not probe runtime")

    assistants, degraded = _load_assistants_for_bootstrap_fast(
        StubAssistantService(),
        probe_transient_runtime=False,
    )

    assert degraded is False
    assert assistants == [starting]


def test_load_assistants_for_bootstrap_fast_returns_empty_when_list_fails() -> None:
    class FailingAssistantService:
        def list_assistants(self):
            raise ConnectionError("mysql unavailable")

    assistants, degraded = _load_assistants_for_bootstrap_fast(FailingAssistantService())

    assert degraded is True
    assert assistants == []


def test_fast_bootstrap_payload_updates_visible_status_and_marks_partial(
    monkeypatch,
) -> None:
    checked_at = datetime(2026, 5, 18, 10, 0, 0, tzinfo=UTC)
    starting = SimpleNamespace(
        assistant_id="starting-assistant",
        name="Starting Assistant",
        capability_profile="chat",
        deleted_at=None,
        runtime=SimpleNamespace(
            status="starting",
            container_name="old-container",
            workspace_root="",
            gateway_url="",
            gateway_token="",
            updated_at=checked_at,
            last_error="gateway endpoint not published yet",
        ),
    )

    class StubAssistantService:
        def list_assistants(self):
            return [starting]

        def sync_runtime_views(self, assistants):
            raise AssertionError("fast bootstrap must not probe runtime")

    class StubApprovalService:
        def list_requests(self):
            return []

    class StubUserService:
        def list_users(self):
            return []

    class StubMarketService:
        def list_skills(self):
            return []

        def list_plugins(self):
            return []

    class StubModelRegistryService:
        def import_from_assistants(self, assistants):
            return []

    class StubWorkspaceFileService:
        pass

    class StubAuditRepository:
        def list_page(self, limit: int = 25, offset: int = 0):
            return SimpleNamespace(items=[], total=0)

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "ApprovalService", StubApprovalService)
    monkeypatch.setattr(admin_web_app, "UserService", StubUserService)
    monkeypatch.setattr(admin_web_app, "MarketService", StubMarketService)
    monkeypatch.setattr(admin_web_app, "ModelRegistryService", StubModelRegistryService)
    monkeypatch.setattr(admin_web_app, "WorkspaceFileService", StubWorkspaceFileService)
    monkeypatch.setattr(admin_web_app, "build_audit_repository", lambda: StubAuditRepository())

    payload = admin_web_app._build_bootstrap_sync(
        probe_runtime=False,
        include_workspace_items=False,
        include_market=False,
        include_user_bindings=False,
        include_workspace_paths=False,
        include_workspace_files=False,
        probe_fast_runtime=False,
        initial_limit=25,
    )

    assert payload["partial"] is True
    assert payload["pollingSource"] == "bootstrap_fast"
    assert payload["runtimeProbeDegraded"] is False
    assert payload["assistants"][0]["status"] == "starting"
    assert payload["assistants"][0]["observedState"] == "starting"
    assert payload["assistants"][0]["desiredState"] == "starting"
    assert payload["assistants"][0]["observedStatus"] == "starting"
    assert payload["assistants"][0]["desiredStatus"] == "starting"
    assert payload["assistants"][0]["syncState"] == "current"
    assert payload["assistants"][0]["runtimeProbeDegraded"] is False
    assert payload["assistants"][0]["pollingSource"] == "bootstrap_fast"
    assert payload["assistants"][0]["partial"] is True
    assert payload["assistants"][0]["displayStatus"] == "starting"
    assert payload["assistants"][0]["displaySeverity"] == "info"
    assert payload["assistants"][0]["displayReason"] == "partial status payload"
    assert payload["assistants"][0]["displaySource"] == "bootstrap_fast"
    assert payload["assistants"][0]["displayPartial"] is True


def test_fast_bootstrap_payload_marks_runtime_fallback_stale(monkeypatch) -> None:
    checked_at = datetime(2026, 5, 18, 10, 0, 0, tzinfo=UTC)
    starting = SimpleNamespace(
        assistant_id="starting-assistant",
        name="Starting Assistant",
        capability_profile="chat",
        deleted_at=None,
        runtime=SimpleNamespace(
            status="starting",
            container_name="old-container",
            workspace_root="",
            gateway_url="",
            gateway_token="",
            updated_at=checked_at,
            last_error="gateway endpoint not published yet",
        ),
    )

    class StubAssistantService:
        def list_assistants(self):
            return [starting]

        def sync_runtime_views(self, assistants):
            raise AssertionError("fast bootstrap must not probe runtime")

    class StubApprovalService:
        def list_requests(self):
            return []

    class StubUserService:
        def list_users(self):
            return []

    class StubMarketService:
        def list_skills(self):
            return []

        def list_plugins(self):
            return []

    class StubModelRegistryService:
        def import_from_assistants(self, assistants):
            return []

    class StubWorkspaceFileService:
        pass

    class StubAuditRepository:
        def list_page(self, limit: int = 25, offset: int = 0):
            return SimpleNamespace(items=[], total=0)

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "ApprovalService", StubApprovalService)
    monkeypatch.setattr(admin_web_app, "UserService", StubUserService)
    monkeypatch.setattr(admin_web_app, "MarketService", StubMarketService)
    monkeypatch.setattr(admin_web_app, "ModelRegistryService", StubModelRegistryService)
    monkeypatch.setattr(admin_web_app, "WorkspaceFileService", StubWorkspaceFileService)
    monkeypatch.setattr(admin_web_app, "build_audit_repository", lambda: StubAuditRepository())

    payload = admin_web_app._build_bootstrap_sync(
        probe_runtime=False,
        include_workspace_items=False,
        include_market=False,
        include_user_bindings=False,
        include_workspace_paths=False,
        include_workspace_files=False,
        probe_fast_runtime=False,
        initial_limit=25,
    )

    assert payload["partial"] is True
    assert payload["runtimeProbeDegraded"] is False
    assert payload["assistants"][0]["status"] == "starting"
    assert payload["assistants"][0]["syncState"] == "current"
    assert payload["assistants"][0]["runtimeProbeDegraded"] is False
    assert payload["assistants"][0]["pollingSource"] == "bootstrap_fast"
    assert payload["assistants"][0]["partial"] is True
    assert payload["assistants"][0]["displayStatus"] == "starting"
    assert payload["assistants"][0]["displaySeverity"] == "info"
    assert payload["assistants"][0]["displayReason"] == "partial status payload"


def test_bootstrap_payload_degrades_when_display_repositories_are_unavailable(
    monkeypatch,
) -> None:
    class FailingAssistantService:
        def list_assistants(self):
            raise ConnectionError("assistant mysql unavailable")

        def sync_runtime_views(self, assistants=None):
            raise ConnectionError("container runtime unavailable")

    class FailingApprovalService:
        def list_requests(self):
            raise ConnectionError("approval mysql unavailable")

    class FailingUserService:
        def list_users(self):
            raise ConnectionError("user mysql unavailable")

    class StubMarketService:
        def list_skills(self, sync_cos: bool = False):
            return []

        def list_plugins(self, sync_cos: bool = False):
            return []

    class StubModelRegistryService:
        def import_from_assistants(self, assistants):
            return []

    class StubWorkspaceFileService:
        pass

    class FailingAuditRepository:
        def list_page(self, limit: int = 25, offset: int = 0):
            raise ConnectionError("audit mysql unavailable")

    monkeypatch.setattr(admin_web_app, "AssistantService", FailingAssistantService)
    monkeypatch.setattr(admin_web_app, "ApprovalService", FailingApprovalService)
    monkeypatch.setattr(admin_web_app, "UserService", FailingUserService)
    monkeypatch.setattr(admin_web_app, "MarketService", StubMarketService)
    monkeypatch.setattr(admin_web_app, "ModelRegistryService", StubModelRegistryService)
    monkeypatch.setattr(admin_web_app, "WorkspaceFileService", StubWorkspaceFileService)
    monkeypatch.setattr(admin_web_app, "build_audit_repository", lambda: FailingAuditRepository())

    payload = admin_web_app._build_bootstrap_sync(
        probe_runtime=False,
        include_workspace_items=False,
        include_market=False,
        include_user_bindings=False,
        include_workspace_paths=False,
        include_workspace_files=False,
        initial_limit=25,
    )

    assert payload["assistants"] == []
    assert payload["approvals"] == []
    assert payload["users"] == []
    assert payload["auditLogs"] == []
    assert payload["runtimeProbeDegraded"] is True
    assert payload["approvalProbeDegraded"] is True
    assert payload["userProbeDegraded"] is True
    assert payload["auditProbeDegraded"] is True


def test_audit_page_returns_empty_payload_when_repository_is_unavailable(
    monkeypatch,
) -> None:
    class FailingApprovalService:
        def expire_pending_requests(self):
            raise ConnectionError("approval mysql unavailable")

    monkeypatch.setattr(admin_web_app, "ApprovalService", FailingApprovalService)

    payload = admin_web_app._build_audit_page_sync(page=1, page_size=25)

    assert payload["auditLogs"] == []
    assert payload["auditPagination"]["total"] == 0
    assert payload["auditPagination"]["page"] == 1


def test_fast_bootstrap_can_skip_transient_runtime_probe() -> None:
    starting = SimpleNamespace(
        assistant_id="starting-assistant",
        runtime=SimpleNamespace(status="starting"),
    )

    class StubAssistantService:
        def list_assistants(self):
            return [starting]

        def sync_runtime_views(self, assistants):
            raise AssertionError("fast route should be able to read DB without runtime probe")

    assistants, runtime_probe_degraded = _load_assistants_for_bootstrap_fast(
        StubAssistantService(),
        probe_transient_runtime=False,
    )

    assert assistants == [starting]
    assert runtime_probe_degraded is False


def test_admin_assistant_page_response_does_not_mint_launch_tickets(monkeypatch) -> None:
    def fail_if_ticket_is_minted(**kwargs):
        raise AssertionError("assistant list rendering must not issue launch tickets")

    monkeypatch.setattr(admin_web_app, "mint_control_ui_launch_ticket", fail_if_ticket_is_minted)

    assistant = SimpleNamespace(
        assistant_id="demo-main",
        name="Demo Main",
        capability_profile="chat",
        deleted_at=None,
        runtime=SimpleNamespace(
            status="running",
            container_name="demo-pod",
            workspace_root="/workspace/demo-main",
            gateway_url="http://10.244.0.10:18789",
            gateway_token="runtime-token",
            updated_at=datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC),
            last_error="",
        ),
    )

    payload = admin_web_app._admin_assistants_page_response(
        assistants=[assistant],
        page=1,
        page_size=50,
        query=None,
        status=None,
        profile=None,
        assistant_service=None,
        include_workspace_items=False,
        runtime_probe_degraded=False,
        polling_source="assistants_page",
        partial=False,
    )

    assert payload["assistants"][0]["assistantUrl"] == ""
    assert payload["assistants"][0]["gatewayLaunchUrl"] == ""
    assert payload["assistants"][0]["gateway"] == "http://10.244.0.10:18789"
    assert payload["assistants"][0]["gatewayToken"] == "runtime-token"


def test_load_market_for_bootstrap_falls_back_when_market_probe_fails() -> None:
    class FailingMarketService:
        def list_skills(self):
            raise ConnectionError("market mysql unavailable")

        def list_plugins(self):
            raise AssertionError("plugins should not be queried after skills failure")

    skills, plugins, degraded = _load_market_for_bootstrap(FailingMarketService())

    assert degraded is True
    assert skills == []
    assert plugins == []


@pytest.mark.asyncio
async def test_workspace_sync_job_worker_runs_pending_job(monkeypatch) -> None:
    calls: list[str] = []

    class StubWorkspaceSyncJobService:
        async def run_next_pending_index_only_job(self):
            calls.append("run-next")
            return SimpleNamespace(
                job_id="job-1",
                assistant_id="alpha",
                status="succeeded",
            )

    async def stop_after_one_iteration(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(
        admin_web_app,
        "_load_workspace_sync_job_service",
        lambda: StubWorkspaceSyncJobService(),
    )
    monkeypatch.setattr(admin_web_app.asyncio, "sleep", stop_after_one_iteration)

    with pytest.raises(asyncio.CancelledError):
        await admin_web_app._workspace_sync_job_worker_loop(SimpleNamespace())

    assert calls == ["run-next"]


def test_toggle_workspace_item_triggers_runtime_reload_and_audit(monkeypatch) -> None:
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
    monkeypatch.setattr(
        admin_web_app,
        "build_audit_repository",
        lambda: StubAuditRepository(),
    )

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.post(
        "/api/v1/admin/assistants/demo-assistant/workspace-items/toggle",
        headers={**_admin_headers(), "X-Admin-Operator": "operator-1"},
        json={"kind": "skills", "slug": "office-docs", "enabled": False},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["runtime_reload_status"] == "failed"
    assert body["runtime_effective"] is False
    assert body["action"] == "capability_disable"
    assert audit_events[0]["operator"]["operator_id"] == "operator-1"
    assert audit_events[0]["assistant"]["assistant_id"] == "demo-assistant"
    assert audit_events[0]["capability"] == {
        "kind": "skills",
        "id": "office-docs",
        "slug": "office-docs",
        "version": "2.0.0",
    }
    assert audit_events[0]["metadata"]["error_code"] == "RUNTIME_RELOAD_FAILED"
    assert audit_events[0]["metadata"]["error_message"] == "runtime command failed"


def test_install_workspace_item_uploads_package_and_returns_runtime_status(monkeypatch) -> None:
    install_calls: list[dict[str, object]] = []
    audit_events: list[dict[str, object]] = []

    class StubAssistantService:
        async def aget_assistant(self, assistant_id: str):
            return SimpleNamespace(assistant_id=assistant_id, deleted=False)

    class StubInstallService:
        def __init__(self, **kwargs):
            install_calls.append({"init": kwargs})

        async def ainstall_uploaded_package(
            self,
            *,
            assistant_id,
            kind,
            filename,
            content,
            slug,
            display_name,
            owner,
            version,
        ):
            install_calls.append(
                {
                    "assistant_id": assistant_id,
                    "kind": kind,
                    "filename": filename,
                    "content": content,
                    "slug": slug,
                    "display_name": display_name,
                    "owner": owner,
                    "version": version,
                }
            )
            return SimpleNamespace(
                assistant_id=assistant_id,
                kind="skills",
                capability_id="market-1",
                slug="office-docs",
                name="Office Docs",
                version="1.2.3",
                install_path="skills/office-docs",
                install_count=1,
                sync_status="synced",
                sync_error="",
                runtime_reload_status="applied",
                runtime_reload_error="",
                action="install",
                previous_version="",
            )

    class StubAuditRepository:
        def append_event(self, payload):
            audit_events.append(payload)

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(
        admin_web_app,
        "AssistantCapabilityInstallService",
        StubInstallService,
    )
    monkeypatch.setattr(
        admin_web_app,
        "_workspace_item_index_store",
        lambda: SimpleNamespace(name="index-store"),
    )
    monkeypatch.setattr(
        admin_web_app,
        "build_audit_repository",
        lambda: StubAuditRepository(),
    )

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.post(
        "/api/v1/admin/assistants/demo-assistant/workspace-items/install",
        headers={**_admin_headers(), "X-Admin-Operator": "operator-1"},
        data={"kind": "skill"},
        files={"file": ("office-docs.zip", b"archive-bytes", "application/zip")},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "skills"
    assert body["type"] == "skill"
    assert body["slug"] == "office-docs"
    assert body["name"] == "Office Docs"
    assert body["runtime_reload_status"] == "applied"
    assert body["runtime_effective"] is True
    assert body["action"] == "capability_install"
    assert install_calls[1] == {
        "assistant_id": "demo-assistant",
        "kind": "skill",
        "filename": "office-docs.zip",
        "content": b"archive-bytes",
        "slug": "",
        "display_name": "",
        "owner": "",
        "version": "",
    }
    assert audit_events[0]["operator"]["operator_id"] == "operator-1"
    assert audit_events[0]["assistant"]["assistant_id"] == "demo-assistant"
    assert audit_events[0]["capability"]["slug"] == "office-docs"
    assert audit_events[0]["decision"] == "capability_install"


def test_reuse_workspace_item_triggers_sync_and_runtime_reload(monkeypatch) -> None:
    sync_calls: list[tuple[str, list[tuple[str, str]]]] = []
    reload_calls: list[tuple[str, str, str]] = []

    class StubAssistantService:
        async def aget_assistant(self, assistant_id: str):
            return SimpleNamespace(assistant_id=assistant_id, deleted=False)

        def reuse_workspace_item(
            self,
            *,
            source_assistant_id,
            target_assistant_id,
            kind,
            slug,
        ):
            return SimpleNamespace(
                target_assistant_id=target_assistant_id,
                kind=kind,
                slug=slug,
            )

        async def areload_workspace_capability(self, *, assistant_id, kind, slug):
            reload_calls.append((assistant_id, kind, slug))
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

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.post(
        "/api/v1/admin/assistants/source-assistant/workspace-items/reuse",
        headers=_admin_headers(),
        json={
            "target_assistant_id": "target-assistant",
            "kind": "plugins",
            "slug": "spreadsheet-exporter",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["slug"] == "spreadsheet-exporter"
    assert body["target_assistant_id"] == "target-assistant"
    assert body["runtime_reload_status"] == "applied"
    assert body["runtime_reload_error"] == ""
    assert sync_calls == [("target-assistant", [("plugins", "spreadsheet-exporter")])]
    assert reload_calls == [("target-assistant", "plugins", "spreadsheet-exporter")]


def test_reuse_workspace_item_returns_runtime_reload_failure(monkeypatch) -> None:
    class StubAssistantService:
        async def aget_assistant(self, assistant_id: str):
            return SimpleNamespace(assistant_id=assistant_id, deleted=False)

        def reuse_workspace_item(
            self,
            *,
            source_assistant_id,
            target_assistant_id,
            kind,
            slug,
        ):
            return SimpleNamespace(
                target_assistant_id=target_assistant_id,
                kind=kind,
                slug=slug,
            )

        async def areload_workspace_capability(self, *, assistant_id, kind, slug):
            return SimpleNamespace(
                assistant_id=assistant_id,
                kind=kind,
                slug=slug,
                status="failed",
                error="runtime command failed",
            )

    class StubWorkspaceSyncService:
        def __init__(self, *, assistant_service):
            self.assistant_service = assistant_service

        async def async_sync_assistant_workspace_scopes(self, assistant_id, scopes):
            return None

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "WorkspaceSyncService", StubWorkspaceSyncService)

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.post(
        "/api/v1/admin/assistants/source-assistant/workspace-items/reuse",
        headers=_admin_headers(),
        json={
            "target_assistant_id": "target-assistant",
            "kind": "skills",
            "slug": "office-docs",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["slug"] == "office-docs"
    assert body["target_assistant_id"] == "target-assistant"
    assert body["runtime_reload_status"] == "failed"
    assert body["runtime_reload_error"] == "runtime command failed"


def test_delete_workspace_item_triggers_runtime_reload_and_audit(monkeypatch) -> None:
    audit_events: list[dict[str, object]] = []

    class StubAssistantService:
        async def aget_assistant(self, assistant_id):
            return SimpleNamespace(assistant_id=assistant_id)

        def list_workspace_items(self, assistant_id):
            return [
                SimpleNamespace(
                    assistant_id=assistant_id,
                    kind="plugins",
                    slug="spreadsheet-exporter",
                    version="1.0.0",
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
            return 3

    class StubAuditRepository:
        def append_event(self, payload):
            audit_events.append(payload)

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "WorkspaceFileService", StubWorkspaceFileService)
    monkeypatch.setattr(
        admin_web_app,
        "build_audit_repository",
        lambda: StubAuditRepository(),
    )

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.delete(
        "/api/v1/admin/assistants/demo-assistant/workspace-items",
        params={"kind": "plugins", "slug": "spreadsheet-exporter"},
        headers=_admin_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["removed"] is True
    assert body["workspaceFilesDeleted"] == 3
    assert body["runtime_reload_status"] == "applied"
    assert body["action"] == "capability_uninstall"
    assert audit_events[0]["decision"] == "capability_uninstall"
    assert audit_events[0]["metadata"]["version"] == "1.0.0"


def test_index_renders_when_runtime_probe_fails(monkeypatch) -> None:
    assistant = SimpleNamespace(
        assistant_id="demo-bootstrap",
        name="Demo Bootstrap",
        capability_profile="dangerous",
        runtime=SimpleNamespace(
            status="unknown",
            container_name="",
            workspace_root="",
            gateway_url="",
            gateway_token="",
        ),
    )

    class StubAssistantService:
        def sync_runtime_views(self):
            raise ConnectionResetError("containerd socket reset by peer")

        def list_assistants(self):
            return [assistant]

        def list_workspace_items(self, assistant_id: str):
            return []

    class StubApprovalService:
        def list_requests(self):
            return []

    class StubUserService:
        def list_users(self):
            return []

    class StubMarketService:
        def list_skills(self):
            return []

        def list_plugins(self):
            return []

    class StubModelRegistryService:
        def import_from_assistants(self, assistants):
            return []

    class StubFileRepository:
        async def alist_recent_files(
            self,
            assistant_id=None,
            bucket=None,
            include_deleted=False,
            limit=None,
        ):
            return []

    class StubWorkspaceFileService:
        def __init__(self):
            self.file_repository = StubFileRepository()

    class StubAuditRepository:
        def list_page(self, limit: int = 25, offset: int = 0):
            return SimpleNamespace(items=[], total=0)

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "ApprovalService", StubApprovalService)
    monkeypatch.setattr(admin_web_app, "UserService", StubUserService)
    monkeypatch.setattr(admin_web_app, "MarketService", StubMarketService)
    monkeypatch.setattr(admin_web_app, "ModelRegistryService", StubModelRegistryService)
    monkeypatch.setattr(admin_web_app, "WorkspaceFileService", StubWorkspaceFileService)
    monkeypatch.setattr(admin_web_app, "build_audit_repository", lambda: StubAuditRepository())
    monkeypatch.setattr(
        admin_web_app,
        "_gateway_launch_url",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("bootstrap should not prebuild launch urls")
        ),
    )

    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)

    response = client.get(
        "/api/v1/admin/bootstrap",
        headers={"X-IronAgent-Internal-Token": "change-me"},
    )

    assert response.status_code == 200
    assert response.json()["assistants"][0]["id"] == "demo-bootstrap"
    assert response.json()["assistants"][0]["assistantUrl"] == ""
    assert response.json()["assistants"][0]["gatewayLaunchUrl"] == ""
    assert response.json()["runtimeProbeDegraded"] is True


def test_admin_assistant_launch_url_reuses_main_session_by_default(monkeypatch) -> None:
    captured: dict[str, object] = {}
    assistant = SimpleNamespace(
        assistant_id="demo-main",
        runtime=SimpleNamespace(
            gateway_url="http://host.docker.internal:18789",
            gateway_token="runtime-token",
        ),
    )

    class StubAssistantService:
        def get_assistant(self, assistant_id: str):
            assert assistant_id == "demo-main"
            return assistant

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)

    def fake_gateway_launch_url(**kwargs):
        captured.update(kwargs)
        return "http://gateway.example/assistants/demo-main/ui/?launchTicket=fresh"

    monkeypatch.setattr(admin_web_app, "_gateway_launch_url", fake_gateway_launch_url)

    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)

    response = client.get(
        "/api/v1/admin/assistants/demo-main/launch-url",
        headers={"X-IronAgent-Internal-Token": "change-me"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "assistantUrl": "http://gateway.example/assistants/demo-main/ui/?launchTicket=fresh",
        "gatewayLaunchUrl": "http://gateway.example/assistants/demo-main/ui/?launchTicket=fresh",
        "fallbackAssistantUrl": "",
    }
    assert captured["fresh_session"] is False
    assert captured["gateway_token"] == "runtime-token"


def test_admin_assistant_launch_url_supports_explicit_fresh_session(monkeypatch) -> None:
    captured: dict[str, object] = {}
    assistant = SimpleNamespace(
        assistant_id="demo-main",
        runtime=SimpleNamespace(
            gateway_url="http://127.0.0.1:18789",
            gateway_token="runtime-token",
        ),
    )

    class StubAssistantService:
        def get_assistant(self, assistant_id: str):
            assert assistant_id == "demo-main"
            return assistant

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)

    def fake_gateway_launch_url(**kwargs):
        captured.update(kwargs)
        return "http://gateway.example/assistants/demo-main/ui/?launchTicket=fresh"

    monkeypatch.setattr(admin_web_app, "_gateway_launch_url", fake_gateway_launch_url)

    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)

    response = client.get(
        "/api/v1/admin/assistants/demo-main/launch-url?fresh=true",
        headers={"X-IronAgent-Internal-Token": "change-me"},
    )

    assert response.status_code == 200
    assert captured["fresh_session"] is True


def test_admin_assistant_launch_url_avoids_admin_origin_control_ui(monkeypatch) -> None:
    monkeypatch.setenv("IRON_AGENT_CONTROL_UI_PUBLIC_URL", "http://127.0.0.1:19001")
    monkeypatch.delenv("IRON_AGENT_GO_GATEWAY_PUBLIC_URL", raising=False)
    monkeypatch.delenv("IRON_AGENT_SECURITY_SERVICE_PUBLIC_URL", raising=False)
    get_settings.cache_clear()

    assistant = SimpleNamespace(
        assistant_id="demo-main",
        runtime=SimpleNamespace(
            gateway_url="http://127.0.0.1:18789",
            gateway_token="runtime-token",
        ),
    )

    class StubAssistantService:
        def get_assistant(self, assistant_id: str):
            assert assistant_id == "demo-main"
            return assistant

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)

    try:
        app = admin_web_app.create_admin_web_app()
        client = TestClient(app, base_url="http://127.0.0.1:19001")

        response = client.get(
            "/api/v1/admin/assistants/demo-main/launch-url",
            headers={"X-IronAgent-Internal-Token": "change-me"},
        )

        assert response.status_code == 200
        launch_url = response.json()["gatewayLaunchUrl"]
        assert launch_url.startswith(
            "http://127.0.0.1:18789/chat?session=agent%3Ademo-main%3Amain#"
        )
        assert "127.0.0.1%3A19001%2Fchat" not in launch_url
        assert "gatewayUrl=ws%3A%2F%2F127.0.0.1%3A18789" in launch_url
        assert "host.docker.internal" not in launch_url
        assert "token=runtime-token" in launch_url
    finally:
        get_settings.cache_clear()


def test_gateway_launch_url_mints_ticket_for_runtime_control_ui_origin(monkeypatch) -> None:
    settings = get_settings().model_copy(
        update={
            "control_ui_public_url": "http://127.0.0.1:19001",
            "security_service_public_url": "http://127.0.0.1:19000",
            "go_gateway_public_url": "",
        },
    )
    captured: dict[str, object] = {}

    def fake_mint_control_ui_launch_ticket(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(ticket="ticket-123")

    monkeypatch.setattr(admin_web_app, "get_settings", lambda: settings)
    monkeypatch.setattr(
        admin_web_app,
        "mint_control_ui_launch_ticket",
        fake_mint_control_ui_launch_ticket,
    )

    launch_url = admin_web_app._gateway_launch_url(
        assistant_id="demo-main",
        gateway_url="http://host.docker.internal:18789",
        gateway_token="runtime-token",
        admin_origin_url="http://127.0.0.1:19001/",
    )

    assert launch_url is not None
    assert launch_url.startswith(
        "http://127.0.0.1:18789/chat?session=agent%3Ademo-main%3Amain#"
    )
    assert "launchTicket=ticket-123" in launch_url
    assert captured["control_ui_url"] == "http://127.0.0.1:18789"
    assert captured["gateway_url"] == "ws://host.docker.internal:18789"


def test_admin_assistants_list_is_paginated_and_filtered(monkeypatch) -> None:
    assistants = [
        SimpleNamespace(
            assistant_id="alpha",
            name="Alpha Bot",
            capability_profile="chat",
            deleted_at=None,
            runtime=SimpleNamespace(
                status="running",
                container_name="alpha-pod",
                workspace_root="/workspace/alpha",
                gateway_url="",
                updated_at="2026-05-20T09:00:00+08:00",
                last_error="",
        ),
            ),
        SimpleNamespace(
            assistant_id="beta",
            name="Beta Bot",
            capability_profile="dangerous",
            deleted_at=None,
            runtime=SimpleNamespace(
                status="stopped",
                container_name="beta-pod",
                workspace_root="/workspace/beta",
                gateway_url="",
                updated_at="2026-05-20T10:00:00+08:00",
                last_error="",
        ),
            ),
    ]

    class StubAssistantService:
        def list_assistants(self):
            return assistants

        def sync_runtime_views(self):
            raise AssertionError("default paginated list should not probe runtime")

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)

    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)
    response = client.get(
        "/api/v1/admin/assistants?page=1&page_size=1&status=running",
        headers=_admin_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["assistants"][0]["id"] == "alpha"
    assert body["pagination"]["page_size"] == 1
    assert body["pagination"]["total"] == 1
    assert body["pagination"]["has_next"] is False


def test_admin_assistants_list_excludes_deleted_records(monkeypatch) -> None:
    deleted_at = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
    assistants = [
        SimpleNamespace(
            assistant_id="active-main",
            name="Active Bot",
            capability_profile="chat",
            deleted_at=None,
            runtime=SimpleNamespace(
                status="running",
                container_name="active-pod",
                workspace_root="/workspace/active",
                gateway_url="",
                updated_at="2026-05-20T09:00:00+08:00",
                last_error="",
        ),
            ),
        SimpleNamespace(
            assistant_id="deleted-main",
            name="Deleted Bot",
            capability_profile="chat",
            deleted_at=deleted_at,
            runtime=SimpleNamespace(
                status="stopped",
                container_name="deleted-pod",
                workspace_root="/workspace/deleted",
                gateway_url="",
                updated_at="2026-05-20T10:00:00+08:00",
                last_error="assistant soft deleted",
        ),
            ),
    ]

    class StubAssistantService:
        def list_assistants(self):
            return assistants

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.get("/api/v1/admin/assistants", headers=_admin_headers())

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["assistants"]] == ["active-main"]
    assert body["pagination"]["total"] == 1


def test_admin_assistant_workspace_summary_sync_true_reads_db_index_only(monkeypatch) -> None:
    captured: dict[str, object] = {"sync_called": False, "file_service_called": False}

    class StubAssistantService:
        async def aget_assistant(self, assistant_id: str):
            captured["loaded_assistant_id"] = assistant_id
            return SimpleNamespace(assistant_id=assistant_id)

    class StubWorkspaceSyncService:
        def __init__(self, assistant_service=None):
            self.assistant_service = assistant_service

        async def async_sync_assistant_workspace(self, assistant_id: str) -> None:
            captured["sync_called"] = True

    class StubWorkspaceFileService:
        def list_cos_workspace_summary_records(self, assistant_id: str):
            captured["file_service_called"] = True
            raise AssertionError("workspace summary should use db index before fallback")

        def list_files(self, assistant_id: str):
            captured["file_service_called"] = True
            raise AssertionError("workspace summary should use db index before fallback")

    async def fake_query_workspace_admin_index(**kwargs):
        captured["index_kwargs"] = kwargs
        return {
            "records": [
                {
                    "file_id": "demo-file",
                    "assistant_id": "demo-main",
                    "bucket": "persistent",
                    "filename": "report.txt",
                    "relative_path": "persistent/report.txt",
                    "source_type": "workspace_scan",
                    "sync_status": "synced",
                }
            ],
            "pagination": {"page": 1, "page_size": 50, "total": 1, "has_next": False},
            "indexStatus": {"source": "db", "sync_status": "synced"},
        }

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "WorkspaceSyncService", StubWorkspaceSyncService)
    monkeypatch.setattr(admin_web_app, "WorkspaceFileService", StubWorkspaceFileService)
    monkeypatch.setattr(
        admin_web_app,
        "_query_workspace_admin_index",
        fake_query_workspace_admin_index,
    )

    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)
    response = client.get(
        "/api/v1/admin/assistants/demo-main/workspace-files/summary?sync=true",
        headers=_admin_headers(),
    )

    assert response.status_code == 200
    assert captured["loaded_assistant_id"] == "demo-main"
    assert captured["sync_called"] is True
    assert captured["file_service_called"] is False
    assert captured["index_kwargs"]["assistant_id"] == "demo-main"
    assert captured["index_kwargs"]["assistant_ids"] == ["demo-main"]
    payload = response.json()
    assert payload["compat"] == {"sync_requested": True, "sync_ignored": False}
    assert payload["workspaceFiles"][0]["relative_path"] == "persistent/report.txt"


def test_admin_workspace_summary_ignores_sync_query(monkeypatch) -> None:
    captured: dict[str, object] = {"synced": [], "file_service_called": False}

    class StubAssistantService:
        def list_assistants(self):
            return [
                SimpleNamespace(assistant_id="alpha"),
                SimpleNamespace(assistant_id="beta"),
            ]

    class StubWorkspaceSyncService:
        def __init__(self, assistant_service=None):
            self.assistant_service = assistant_service

        async def async_sync_assistant_workspace(self, assistant_id: str) -> None:
            captured["synced"].append(assistant_id)

    class StubWorkspaceFileService:
        def list_cos_workspace_summary_records(self, assistant_id: str):
            captured["file_service_called"] = True
            raise AssertionError("workspace summary should use db index before fallback")

    async def fake_query_workspace_admin_index(**kwargs):
        captured["index_kwargs"] = kwargs
        return {
            "records": [
                {
                    "file_id": "alpha-file",
                    "assistant_id": "alpha",
                    "bucket": "temp",
                    "filename": "alpha.txt",
                    "relative_path": "temp/alpha.txt",
                    "source_type": "workspace_scan",
                    "sync_status": "synced",
                },
                {
                    "file_id": "beta-file",
                    "assistant_id": "beta",
                    "bucket": "temp",
                    "filename": "beta.txt",
                    "relative_path": "temp/beta.txt",
                    "source_type": "workspace_scan",
                    "sync_status": "synced",
                },
            ],
            "pagination": {"page": 1, "page_size": 50, "total": 2, "has_next": False},
            "indexStatus": {"source": "db", "sync_status": "synced"},
        }

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "WorkspaceSyncService", StubWorkspaceSyncService)
    monkeypatch.setattr(admin_web_app, "WorkspaceFileService", StubWorkspaceFileService)
    monkeypatch.setattr(
        admin_web_app,
        "_query_workspace_admin_index",
        fake_query_workspace_admin_index,
    )

    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)
    response = client.get(
        "/api/v1/admin/workspace-files/summary?sync=true",
        headers=_admin_headers(),
    )

    assert response.status_code == 200
    assert captured["synced"] == ["alpha", "beta"]
    assert captured["file_service_called"] is False
    assert captured["index_kwargs"]["assistant_ids"] == ["alpha", "beta"]
    payload = response.json()
    assert payload["compat"] == {"sync_requested": True, "sync_ignored": False}
    assert {item["assistant_id"] for item in payload["workspaceFiles"]} == {
        "alpha",
        "beta",
    }


def test_admin_workspace_summary_does_not_sync_by_default(monkeypatch) -> None:
    captured: dict[str, object] = {"synced": False, "file_service_called": False}

    class StubAssistantService:
        def list_assistants(self):
            return [SimpleNamespace(assistant_id="alpha")]

    class StubWorkspaceSyncService:
        def __init__(self, assistant_service=None):
            self.assistant_service = assistant_service

        async def async_sync_assistant_workspace(self, assistant_id: str) -> None:
            captured["synced"] = True

    class StubWorkspaceFileService:
        def list_cos_workspace_summary_records(self, assistant_id: str):
            captured["file_service_called"] = True
            raise AssertionError("workspace summary should use db index before fallback")

    async def fake_query_workspace_admin_index(**kwargs):
        captured["index_kwargs"] = kwargs
        return {
            "records": [
                {
                    "file_id": "alpha-file",
                    "assistant_id": "alpha",
                    "bucket": "temp",
                    "filename": "alpha.txt",
                    "relative_path": "temp/alpha.txt",
                    "source_type": "workspace_scan",
                    "sync_status": "synced",
                }
            ],
            "pagination": {"page": 1, "page_size": 50, "total": 1, "has_next": False},
            "indexStatus": {"source": "db", "sync_status": "synced"},
        }

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "WorkspaceSyncService", StubWorkspaceSyncService)
    monkeypatch.setattr(admin_web_app, "WorkspaceFileService", StubWorkspaceFileService)
    monkeypatch.setattr(
        admin_web_app,
        "_query_workspace_admin_index",
        fake_query_workspace_admin_index,
    )

    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)
    response = client.get(
        "/api/v1/admin/workspace-files/summary",
        headers=_admin_headers(),
    )

    assert response.status_code == 200
    assert captured["synced"] is False
    assert captured["file_service_called"] is False
    assert captured["index_kwargs"]["assistant_ids"] == ["alpha"]
    assert response.json()["workspaceFiles"][0]["relative_path"] == "temp/alpha.txt"


def test_admin_workspace_items_falls_back_to_workspace_file_records() -> None:
    class StubAssistantService:
        def list_workspace_items(self, assistant_id: str):
            return [
                SimpleNamespace(
                    kind="skills",
                    slug="tabular-table-agent",
                    name="",
                    version="",
                    enabled=True,
                    install_path=f"/workspace-root/assistants/{assistant_id}/skills/tabular-table-agent",
                )
            ]

    class StubWorkspaceFileService:
        def list_cos_capability_package_records(
            self,
            assistant_id: str,
            *,
            force_refresh: bool = False,
        ):
            return []

        def list_files(self, assistant_id: str, *, bucket: str, **_kwargs):
            if bucket != "skills":
                return []
            return [
                admin_web_app.WorkspaceFile(
                    assistant_id=assistant_id,
                    bucket="skills",
                    filename="tabular-table-agent/SKILL.md",
                    relative_path="skills/tabular-table-agent/SKILL.md",
                    source_type="capability_package",
                    sync_status="synced",
                    size_bytes=10,
                ),
                admin_web_app.WorkspaceFile(
                    assistant_id=assistant_id,
                    bucket="skills",
                    filename="tabular-table-agent/scripts/skill.py",
                    relative_path="skills/tabular-table-agent/scripts/skill.py",
                    source_type="capability_package",
                    sync_status="synced",
                    size_bytes=20,
                ),
            ]

        def _bucket_dir(self, assistant_id: str, bucket: str):
            return Path(f"/workspace-root/assistants/{assistant_id}/{bucket}")

    items = admin_web_app._installed_capabilities_for_assistant(
        assistant_id="demo-main",
        assistant_service=StubAssistantService(),
        workspace_file_service=StubWorkspaceFileService(),
        catalog_by_key={},
        force_refresh=True,
    )

    assert items == [
        {
            "id": "tabular-table-agent",
            "slug": "tabular-table-agent",
            "type": "skill",
            "name": "tabular-table-agent",
            "version": "",
            "enabled": True,
            "filesCount": 2,
            "install_path": "/workspace/skills/tabular-table-agent",
            "sync_status": "synced",
        }
    ]


def test_admin_workspace_items_uses_workspace_skill_metadata(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace-root"
    skill_dir = workspace_root / "assistants" / "demo-main" / "skills" / "test"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: excel-agent\nversion: 1.0.0\n---\n\n# Excel Agent\n",
        encoding="utf-8",
    )

    class StubAssistantService:
        def list_workspace_items(self, assistant_id: str):
            return []

    class StubWorkspaceFileService:
        def list_cos_capability_package_records(
            self,
            assistant_id: str,
            *,
            force_refresh: bool = False,
        ):
            return []

        def list_files(self, assistant_id: str, *, bucket: str, **_kwargs):
            if bucket != "skills":
                return []
            return [
                admin_web_app.WorkspaceFile(
                    assistant_id=assistant_id,
                    bucket="skills",
                    filename="test/SKILL.md",
                    relative_path="skills/test/SKILL.md",
                    source_type="capability_package",
                    sync_status="synced",
                    size_bytes=10,
                )
            ]

        def _bucket_dir(self, assistant_id: str, bucket: str):
            return workspace_root / "assistants" / assistant_id / bucket

    items = admin_web_app._installed_capabilities_for_assistant(
        assistant_id="demo-main",
        assistant_service=StubAssistantService(),
        workspace_file_service=StubWorkspaceFileService(),
        catalog_by_key={},
        force_refresh=True,
    )

    assert items[0]["slug"] == "test"
    assert items[0]["name"] == "excel-agent"
    assert items[0]["version"] == "1.0.0"


def test_admin_workspace_items_route_requires_internal_token(monkeypatch, tmp_path) -> None:
    class StubAssistantService:
        async def aget_assistant(self, assistant_id: str):
            return SimpleNamespace(assistant_id=assistant_id)

        def list_workspace_items(self, assistant_id: str):
            return []

    class StubWorkspaceFileService:
        def list_cos_capability_package_records(
            self,
            assistant_id: str,
            *,
            force_refresh: bool = False,
        ):
            return []

        def list_files(self, assistant_id: str, *, bucket: str, **_kwargs):
            return []

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "WorkspaceFileService", StubWorkspaceFileService)
    monkeypatch.setenv(
        "IRON_AGENT_WORKSPACE_ITEM_INDEX_SQLITE_PATH",
        str(tmp_path / "workspace-items.sqlite"),
    )
    admin_web_app.get_settings.cache_clear()

    client = TestClient(admin_web_app.create_admin_web_app())

    unauthorized = client.get("/api/v1/admin/assistants/demo-main/workspace-items")
    authorized = client.get(
        "/api/v1/admin/assistants/demo-main/workspace-items",
        headers=_admin_headers(),
    )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    payload = authorized.json()
    assert payload["items"] == []
    assert payload["compat"] == {
        "force_requested": False,
        "force_scan_disabled": False,
        "refresh_required": False,
    }
    assert isinstance(payload["indexStatus"], dict)
    assert set(payload["indexStatus"]) >= {"source", "sync_status"}


def test_index_renders_when_market_probe_fails(monkeypatch) -> None:
    assistant = SimpleNamespace(
        assistant_id="demo-bootstrap",
        name="Demo Bootstrap",
        capability_profile="dangerous",
        runtime=SimpleNamespace(
            status="running",
            container_name="",
            workspace_root="",
            gateway_url="",
            gateway_token="",
        ),
    )

    class StubAssistantService:
        def sync_runtime_views(self):
            return [assistant], 0, 0

        def list_assistants(self):
            return [assistant]

        def list_workspace_items(self, assistant_id: str):
            return []

    class StubApprovalService:
        def list_requests(self):
            return []

    class StubUserService:
        def list_users(self):
            return []

    class FailingMarketService:
        def list_skills(self):
            raise ConnectionError("market mysql unavailable")

        def list_plugins(self):
            raise AssertionError("plugins should not be queried after skills failure")

    class StubModelRegistryService:
        def import_from_assistants(self, assistants):
            return []

    class StubFileRepository:
        async def alist_recent_files(
            self,
            assistant_id=None,
            bucket=None,
            include_deleted=False,
            limit=None,
        ):
            return []

    class StubWorkspaceFileService:
        def __init__(self):
            self.file_repository = StubFileRepository()

    class StubAuditRepository:
        def list_page(self, limit: int = 25, offset: int = 0):
            return SimpleNamespace(items=[], total=0)

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "ApprovalService", StubApprovalService)
    monkeypatch.setattr(admin_web_app, "UserService", StubUserService)
    monkeypatch.setattr(admin_web_app, "MarketService", FailingMarketService)
    monkeypatch.setattr(admin_web_app, "ModelRegistryService", StubModelRegistryService)
    monkeypatch.setattr(admin_web_app, "WorkspaceFileService", StubWorkspaceFileService)
    monkeypatch.setattr(admin_web_app, "build_audit_repository", lambda: StubAuditRepository())

    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)

    response = client.get(
        "/api/v1/admin/bootstrap",
        headers={"X-IronAgent-Internal-Token": "change-me"},
    )

    assert response.status_code == 200
    assert response.json()["assistants"][0]["id"] == "demo-bootstrap"
    assert response.json()["marketProbeDegraded"] is True


def test_bootstrap_keeps_assistants_without_local_workspace_files(monkeypatch) -> None:
    assistants = [
        SimpleNamespace(
            assistant_id="001",
            name="001",
            capability_profile="dangerous",
            runtime=SimpleNamespace(
                status="running",
                container_name="ironagent-assistant-001",
                workspace_root="/workspaces/001",
                gateway_url="http://172.28.135.254:3221",
                gateway_token="ironagent-local-001",
        ),
            ),
        SimpleNamespace(
            assistant_id="demo-main",
            name="Demo Main Assistant",
            capability_profile="onlyread",
            runtime=SimpleNamespace(
                status="failed",
                container_name="ironagent-assistant-demo-main",
                workspace_root="/workspaces/demo-main",
                gateway_url="",
                gateway_token="ironagent-local-demo-main",
        ),
            ),
    ]

    class StubAssistantService:
        def sync_runtime_views(self):
            return assistants, {}, 0

        def list_assistants(self):
            return assistants

        def list_workspace_items(self, assistant_id: str):
            return []

    class StubApprovalService:
        def list_requests(self):
            return []

    class StubUserService:
        def list_users(self):
            return []

    class StubMarketService:
        def list_skills(self):
            return []

        def list_plugins(self):
            return []

    class StubModelRegistryService:
        def import_from_assistants(self, assistants):
            return []

    class StubFileRepository:
        async def alist_recent_files(
            self,
            assistant_id=None,
            bucket=None,
            include_deleted=False,
            limit=None,
        ):
            return [
                SimpleNamespace(
                    file_id="file-1",
                    assistant_id="demo-main",
                    bucket="persistent",
                    filename="doc.txt",
                    origin="manual_import",
                    source_type="manual_import",
                    sync_status="synced",
                    relative_path="persistent/doc.txt",
                )
            ]

    class StubWorkspaceFileService:
        def __init__(self):
            self.file_repository = StubFileRepository()

        def _bucket_dir(self, assistant_id: str, bucket: str):
            return Path(f"/tmp/{assistant_id}/{bucket}")

    class StubAuditRepository:
        def list_page(self, limit: int = 25, offset: int = 0):
            return SimpleNamespace(items=[], total=0)

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "ApprovalService", StubApprovalService)
    monkeypatch.setattr(admin_web_app, "UserService", StubUserService)
    monkeypatch.setattr(admin_web_app, "MarketService", StubMarketService)
    monkeypatch.setattr(admin_web_app, "ModelRegistryService", StubModelRegistryService)
    monkeypatch.setattr(admin_web_app, "WorkspaceFileService", StubWorkspaceFileService)
    monkeypatch.setattr(admin_web_app, "build_audit_repository", lambda: StubAuditRepository())

    app = admin_web_app.create_admin_web_app()
    client = TestClient(app)

    response = client.get(
        "/api/v1/admin/bootstrap",
        headers={"X-IronAgent-Internal-Token": "change-me"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert {item["id"] for item in payload["assistants"]} == {"001", "demo-main"}
    items = {item["id"]: item for item in payload["assistants"]}
    assert items["001"]["gateway"] == "http://172.28.135.254:3221"
    assert items["001"]["gatewayToken"] == "ironagent-local-001"
    assert items["demo-main"]["gatewayToken"] == "ironagent-local-demo-main"
