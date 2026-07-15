import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import src.admin_web.app as admin_web_app
from src.shared.config import get_settings


def _admin_headers() -> dict[str, str]:
    return {"X-IronAgent-Internal-Token": get_settings().internal_service_token}


def _install_db_index(monkeypatch, captured: dict[str, object]) -> None:
    class StubIndexFilters:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def list_workspace_file_index_page(repository, filters):
        captured["index_repository"] = repository
        captured["index_filters"] = filters
        return {
            "records": [
                {
                    "file_id": "file-1",
                    "assistant_id": filters.assistant_id,
                    "bucket": "persistent",
                    "filename": "report.txt",
                    "relative_path": "persistent/report.txt",
                    "source_type": "workspace_scan",
                    "sync_status": "synced",
                }
            ],
            "pagination": {"page": 1, "page_size": 50, "total": 1, "has_next": False},
            "indexStatus": {"source": "db", "sync_status": "synced", "stale": False},
        }

    monkeypatch.setattr(
        admin_web_app,
        "_workspace_admin_index_repository",
        lambda: "db-index-repository",
    )
    monkeypatch.setitem(
        sys.modules,
        "src.workspace_files.admin_index",
        SimpleNamespace(
            WorkspaceFileIndexFilters=StubIndexFilters,
            list_workspace_file_index_page=list_workspace_file_index_page,
        ),
    )


def _install_forbidden_inline_workspace_work(monkeypatch) -> dict[str, bool]:
    calls = {
        "workspace_sync_service": False,
        "workspace_file_service": False,
    }

    class ForbiddenWorkspaceSyncService:
        def __init__(self, *_args, **_kwargs):
            calls["workspace_sync_service"] = True
            raise AssertionError("job create route must not run workspace sync")

    class ForbiddenWorkspaceFileService:
        def __init__(self, *_args, **_kwargs):
            calls["workspace_file_service"] = True
            raise AssertionError("job create route must not inspect workspace files or COS")

    monkeypatch.setattr(admin_web_app, "WorkspaceSyncService", ForbiddenWorkspaceSyncService)
    monkeypatch.setattr(admin_web_app, "WorkspaceFileService", ForbiddenWorkspaceFileService)
    return calls


def _install_assistant_service(monkeypatch, captured: dict[str, object]) -> None:
    class StubAssistantService:
        async def aget_assistant(self, assistant_id: str):
            captured["assistant_lookup"] = assistant_id
            return SimpleNamespace(assistant_id=assistant_id)

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)


def _install_sync_job_service(monkeypatch, captured: dict[str, object]) -> None:
    class StubWorkspaceSyncJobService:
        def create_or_get_job(self, **kwargs):
            captured["job_kwargs"] = kwargs
            return SimpleNamespace(
                job_id=f"job-{kwargs['mode']}",
                status="pending",
                mode=kwargs["mode"],
                assistant_id=kwargs["assistant_id"],
                scope_kind=kwargs["scope_kind"],
                scope_slug=kwargs["scope_slug"],
                priority=kwargs["priority"],
                idempotency_key=kwargs["idempotency_key"],
                requested_by=kwargs["requested_by"],
                requested_from=kwargs["requested_from"],
            )

    monkeypatch.setattr(
        admin_web_app,
        "_load_workspace_sync_job_service",
        lambda: StubWorkspaceSyncJobService(),
    )


@pytest.mark.parametrize(
    ("mode", "scope_kind", "scope_slug"),
    [
        ("reconcile", "capability", "skills/office-docs"),
        ("cos_import", "bucket", "persistent"),
    ],
)
def test_create_phase3_workspace_sync_job_rejects_unsupported_modes(
    monkeypatch,
    mode: str,
    scope_kind: str,
    scope_slug: str,
) -> None:
    captured: dict[str, object] = {}
    forbidden_calls = _install_forbidden_inline_workspace_work(monkeypatch)
    _install_sync_job_service(monkeypatch, captured)

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.post(
        "/api/v1/admin/workspace-sync-jobs",
        headers=_admin_headers(),
        json={
            "assistant_id": "alpha",
            "scope_kind": scope_kind,
            "scope_slug": scope_slug,
            "mode": mode,
            "priority": "normal",
            "reason": f"{mode} route test",
            "idempotency_key": f"idem-{mode}",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "unsupported mode"
    assert forbidden_calls == {
        "workspace_sync_service": False,
        "workspace_file_service": False,
    }
    assert "job_kwargs" not in captured


def test_create_workspace_sync_job_default_mode_still_index_only(monkeypatch) -> None:
    captured: dict[str, object] = {}
    forbidden_calls = _install_forbidden_inline_workspace_work(monkeypatch)
    _install_assistant_service(monkeypatch, captured)
    _install_db_index(monkeypatch, captured)
    _install_sync_job_service(monkeypatch, captured)

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.post(
        "/api/v1/admin/workspace-sync-jobs",
        headers=_admin_headers(),
        json={
            "assistant_id": "alpha",
            "scope_kind": "assistant",
            "priority": "normal",
        },
    )

    assert response.status_code == 202
    assert forbidden_calls == {
        "workspace_sync_service": False,
        "workspace_file_service": False,
    }
    assert captured["job_kwargs"]["mode"] == "index_only"
    assert captured["job_kwargs"]["scope_kind"] == "assistant"
    assert captured["job_kwargs"]["scope_slug"] == ""
    assert captured["job_kwargs"]["priority"] == 100
    assert response.json()["mode"] == "index_only"


def test_create_workspace_sync_job_still_rejects_legacy_admin_token(monkeypatch) -> None:
    captured: dict[str, bool] = {"job_service_loaded": False}

    def forbidden_job_service():
        captured["job_service_loaded"] = True
        raise AssertionError("legacy admin token must not reach route body")

    monkeypatch.setattr(
        admin_web_app,
        "_load_workspace_sync_job_service",
        forbidden_job_service,
    )

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.post(
        "/api/v1/admin/workspace-sync-jobs",
        headers={"X-Admin-Token": get_settings().internal_service_token},
        json={
            "assistant_id": "alpha",
            "scope_kind": "assistant",
            "mode": "reconcile",
        },
    )

    assert response.status_code == 401
    assert captured["job_service_loaded"] is False
