import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient

import src.admin_web.app as admin_web_app
from src.shared.config import get_settings


def _admin_headers() -> dict[str, str]:
    return {"X-IronAgent-Internal-Token": get_settings().internal_service_token}


def test_admin_workspace_summary_sync_true_uses_db_index_only(monkeypatch) -> None:
    captured: dict[str, object] = {"sync_called": False, "file_service_called": False}
    index_repository = object()

    class StubAssistantService:
        def list_assistants(self):
            return [SimpleNamespace(assistant_id="alpha")]

    class StubWorkspaceSyncService:
        def __init__(self, *args, **kwargs):
            pass

        async def async_sync_assistant_workspace(self, assistant_id: str) -> None:
            captured["sync_called"] = True

    class ForbiddenWorkspaceFileService:
        def list_cos_workspace_summary_records(self, assistant_id: str):
            captured["file_service_called"] = True
            raise AssertionError("summary must not list COS")

        def list_files(self, assistant_id: str):
            captured["file_service_called"] = True
            raise AssertionError("summary must not list files")

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
                    "assistant_id": "alpha",
                    "bucket": "persistent",
                    "filename": "report.txt",
                    "relative_path": "persistent/report.txt",
                    "source_type": "workspace_scan",
                    "sync_status": "synced",
                }
            ],
            "pagination": {"page": 1, "page_size": 50, "total": 1, "has_next": False},
            "indexStatus": {
                "last_indexed_at": "2026-05-20T09:00:00+08:00",
                "sync_status": "synced",
                "sync_error": "",
                "stale": False,
                "source": "db",
            },
        }

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "WorkspaceSyncService", StubWorkspaceSyncService)
    monkeypatch.setattr(admin_web_app, "WorkspaceFileService", ForbiddenWorkspaceFileService)
    monkeypatch.setattr(
        admin_web_app,
        "_workspace_admin_index_repository",
        lambda: index_repository,
    )
    monkeypatch.setitem(
        sys.modules,
        "src.workspace_files.admin_index",
        SimpleNamespace(
            WorkspaceFileIndexFilters=StubIndexFilters,
            list_workspace_file_index_page=list_workspace_file_index_page,
        ),
    )

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.get(
        "/api/v1/admin/workspace-files/summary?sync=true",
        headers=_admin_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert captured["sync_called"] is True
    assert captured["file_service_called"] is False
    assert captured["index_repository"] is index_repository
    assert captured["index_filters"].assistant_ids == ["alpha"]
    assert payload["compat"] == {"sync_requested": True, "sync_ignored": False}
    assert payload["assistantIds"] == ["alpha"]
    assert payload["pagination"]["page_size"] == 50
    assert payload["indexStatus"]["source"] == "db"
    assert payload["workspaceFiles"][0]["relative_path"] == "persistent/report.txt"


def test_admin_workspace_summary_empty_index_does_not_use_legacy_fallback(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {"file_service_called": False, "sync_called": False}

    class StubAssistantService:
        def list_assistants(self):
            return [SimpleNamespace(assistant_id="alpha")]

    class StubWorkspaceSyncService:
        def __init__(self, *args, **kwargs):
            pass

        async def async_sync_assistant_workspace(self, assistant_id: str) -> None:
            captured["sync_called"] = True

    class ForbiddenWorkspaceFileService:
        def list_cos_workspace_summary_records(self, *args, **kwargs):
            captured["file_service_called"] = True
            raise AssertionError("empty DB index must not fallback to COS")

        def list_files(self, *args, **kwargs):
            captured["file_service_called"] = True
            raise AssertionError("empty DB index must not fallback to legacy list")

    class StubIndexFilters:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def list_workspace_file_index_page(repository, filters):
        captured["index_filters"] = filters
        return {
            "records": [],
            "pagination": {"page": 1, "page_size": 50, "total": 0, "has_next": False},
            "indexStatus": {
                "last_indexed_at": None,
                "sync_status": "empty",
                "sync_error": "",
                "stale": False,
                "source": "db",
            },
        }

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "WorkspaceSyncService", StubWorkspaceSyncService)
    monkeypatch.setattr(admin_web_app, "WorkspaceFileService", ForbiddenWorkspaceFileService)
    monkeypatch.setitem(
        sys.modules,
        "src.workspace_files.admin_index",
        SimpleNamespace(
            WorkspaceFileIndexFilters=StubIndexFilters,
            list_workspace_file_index_page=list_workspace_file_index_page,
        ),
    )

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.get(
        "/api/v1/admin/workspace-files/summary?sync=true",
        headers=_admin_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert captured["sync_called"] is True
    assert captured["file_service_called"] is False
    assert captured["index_filters"].assistant_ids == ["alpha"]
    assert payload["workspaceFiles"] == []
    assert payload["pagination"]["total"] == 0
    assert payload["indexStatus"]["source"] == "db"
    assert payload["compat"] == {"sync_requested": True, "sync_ignored": False}


def test_admin_assistant_workspace_summary_empty_index_does_not_use_legacy_fallback(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {"file_service_called": False, "sync_called": False}

    class StubAssistantService:
        async def aget_assistant(self, assistant_id: str):
            return SimpleNamespace(assistant_id=assistant_id)

    class StubWorkspaceSyncService:
        def __init__(self, *args, **kwargs):
            pass

        async def async_sync_assistant_workspace(self, assistant_id: str) -> None:
            captured["sync_called"] = True

    class ForbiddenWorkspaceFileService:
        def list_cos_workspace_summary_records(self, *args, **kwargs):
            captured["file_service_called"] = True
            raise AssertionError("empty assistant DB index must not fallback to COS")

        def list_files(self, *args, **kwargs):
            captured["file_service_called"] = True
            raise AssertionError("empty assistant DB index must not fallback to legacy list")

    class StubIndexFilters:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def list_workspace_file_index_page(repository, filters):
        captured["index_filters"] = filters
        return {
            "records": [],
            "pagination": {"page": 1, "page_size": 50, "total": 0, "has_next": False},
            "indexStatus": {
                "last_indexed_at": None,
                "sync_status": "empty",
                "sync_error": "",
                "stale": False,
                "source": "db",
            },
        }

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "WorkspaceSyncService", StubWorkspaceSyncService)
    monkeypatch.setattr(admin_web_app, "WorkspaceFileService", ForbiddenWorkspaceFileService)
    monkeypatch.setitem(
        sys.modules,
        "src.workspace_files.admin_index",
        SimpleNamespace(
            WorkspaceFileIndexFilters=StubIndexFilters,
            list_workspace_file_index_page=list_workspace_file_index_page,
        ),
    )

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.get(
        "/api/v1/admin/assistants/alpha/workspace-files/summary?sync=true",
        headers=_admin_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert captured["sync_called"] is True
    assert captured["file_service_called"] is False
    assert captured["index_filters"].assistant_id == "alpha"
    assert captured["index_filters"].assistant_ids == ["alpha"]
    assert payload["workspaceFiles"] == []
    assert payload["pagination"]["total"] == 0
    assert payload["assistantSummary"]["assistant_id"] == "alpha"
    assert payload["indexStatus"]["source"] == "db"
    assert payload["compat"] == {"sync_requested": True, "sync_ignored": False}


def test_admin_assistant_workspace_summary_passes_tree_parent(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class StubAssistantService:
        async def aget_assistant(self, assistant_id: str):
            return SimpleNamespace(assistant_id=assistant_id)

    class StubIndexFilters:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def list_workspace_file_index_page(repository, filters):
        captured["index_filters"] = filters
        return {
            "records": [
                {
                    "file_id": "folder:alpha:persistent/reports",
                    "assistant_id": filters.assistant_id,
                    "bucket": "persistent",
                    "filename": "reports",
                    "relative_path": "persistent/reports",
                    "source_type": "cos_directory",
                    "entry_type": "folder",
                    "content_type": "application/x-directory",
                    "child_count": 2,
                    "sync_status": "synced",
                }
            ],
            "pagination": {
                "page": filters.page,
                "page_size": filters.page_size,
                "total": 1,
                "has_next": False,
            },
            "indexStatus": {"source": "db", "sync_status": "synced", "stale": False},
        }

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setitem(
        sys.modules,
        "src.workspace_files.admin_index",
        SimpleNamespace(
            WorkspaceFileIndexFilters=StubIndexFilters,
            list_workspace_file_index_page=list_workspace_file_index_page,
        ),
    )

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.get(
        "/api/v1/admin/assistants/alpha/workspace-files/summary"
        "?view=tree&parent=persistent&page=2&page_size=10",
        headers=_admin_headers(),
    )

    assert response.status_code == 200
    filters = captured["index_filters"]
    assert filters.assistant_id == "alpha"
    assert filters.view == "tree"
    assert filters.parent == "persistent"
    assert filters.page == 2
    assert filters.page_size == 10
    payload = response.json()
    assert payload["workspaceFiles"][0]["entry_type"] == "folder"
    assert payload["workspaceFiles"][0]["content_type"] == "application/x-directory"
    assert payload["workspaceFiles"][0]["child_count"] == 2


def test_admin_workspace_folder_summary_uses_db_index_only(monkeypatch) -> None:
    captured: dict[str, object] = {"sync_called": False, "file_service_called": False}
    index_repository = object()

    class StubAssistantService:
        def list_assistants(self):
            return [
                SimpleNamespace(
                    assistant_id="alpha",
                    name="Alpha",
                    runtime=SimpleNamespace(status="running", container_name="alpha-pod"),
                ),
                SimpleNamespace(
                    assistant_id="beta",
                    name="Beta",
                    runtime=SimpleNamespace(status="stopped", container_name=""),
                ),
            ]

    class StubWorkspaceSyncService:
        def __init__(self, *args, **kwargs):
            pass

        async def async_sync_assistant_workspace(self, assistant_id: str) -> None:
            captured["sync_called"] = True

    class ForbiddenWorkspaceFileService:
        def list_cos_workspace_summary_records(self, assistant_id: str):
            captured["file_service_called"] = True
            raise AssertionError("folder summary must not list COS")

        def list_files(self, assistant_id: str):
            captured["file_service_called"] = True
            raise AssertionError("folder summary must not list files")

    def list_workspace_folder_summaries(repository, assistant_ids):
        captured["index_repository"] = repository
        captured["assistant_ids"] = assistant_ids
        return SimpleNamespace(
            summaries=[
                SimpleNamespace(
                    assistant_id="alpha",
                    buckets=["persistent", "temp"],
                    files_count=12,
                    root_items_count=2,
                    last_indexed_at="2026-05-20T09:00:00+08:00",
                    last_synced_at="2026-05-20T09:00:00+08:00",
                    sync_status="synced",
                    sync_error="",
                    stale=False,
                ),
                SimpleNamespace(
                    assistant_id="beta",
                    buckets=[],
                    files_count=0,
                    root_items_count=0,
                    last_indexed_at=None,
                    last_synced_at=None,
                    sync_status="empty",
                    sync_error="",
                    stale=False,
                ),
            ],
            last_indexed_at="2026-05-20T09:00:00+08:00",
            stale_count=0,
            source="db",
        )

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "WorkspaceSyncService", StubWorkspaceSyncService)
    monkeypatch.setattr(admin_web_app, "WorkspaceFileService", ForbiddenWorkspaceFileService)
    monkeypatch.setattr(
        admin_web_app,
        "_workspace_admin_index_repository",
        lambda: index_repository,
    )
    monkeypatch.setitem(
        sys.modules,
        "src.workspace_files.admin_index",
        SimpleNamespace(list_workspace_folder_summaries=list_workspace_folder_summaries),
    )

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.get(
        "/api/v1/admin/workspace-folders/summary?sync=true",
        headers=_admin_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert captured["sync_called"] is True
    assert captured["file_service_called"] is False
    assert captured["index_repository"] is index_repository
    assert captured["assistant_ids"] == ["alpha", "beta"]
    assert payload["compat"] == {"sync_requested": True, "sync_ignored": False}
    assert "workspaceFiles" not in payload
    assert payload["pagination"]["total"] == 2
    assert payload["indexStatus"]["source"] == "db"
    assert payload["indexStatus"]["filesCount"] == 12
    assert payload["workspaceFolders"][0]["assistantId"] == "alpha"
    assert payload["workspaceFolders"][0]["filesCount"] == 12
    assert payload["workspaceFolders"][0]["rootItemsCount"] == 2


def test_admin_workspace_folder_summary_paginates_folder_rows(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class StubAssistantService:
        def list_assistants(self):
            return [
                SimpleNamespace(
                    assistant_id="alpha",
                    name="Alpha",
                    runtime=SimpleNamespace(status="running", container_name="alpha-pod"),
                ),
                SimpleNamespace(
                    assistant_id="beta",
                    name="Beta",
                    runtime=SimpleNamespace(status="stopped", container_name="beta-pod"),
                ),
            ]

    def list_workspace_folder_summaries(repository, assistant_ids):
        captured["assistant_ids"] = assistant_ids
        summaries_by_id = {
            "alpha": SimpleNamespace(
                assistant_id="alpha",
                buckets=["persistent"],
                files_count=3,
                root_items_count=1,
                last_indexed_at=None,
                last_synced_at=None,
                sync_status="synced",
                sync_error="",
                stale=False,
            ),
            "beta": SimpleNamespace(
                assistant_id="beta",
                buckets=["temp"],
                files_count=2,
                root_items_count=1,
                last_indexed_at=None,
                last_synced_at=None,
                sync_status="synced",
                sync_error="",
                stale=False,
            ),
        }
        return SimpleNamespace(
            summaries=[summaries_by_id[assistant_id] for assistant_id in assistant_ids],
            last_indexed_at=None,
            stale_count=0,
            source="db",
        )

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setitem(
        sys.modules,
        "src.workspace_files.admin_index",
        SimpleNamespace(list_workspace_folder_summaries=list_workspace_folder_summaries),
    )

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.get(
        "/api/v1/admin/workspace-folders/summary?page=2&page_size=1",
        headers=_admin_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert captured["assistant_ids"] == ["beta"]
    assert payload["pagination"]["total"] == 2
    assert payload["pagination"]["page"] == 2
    assert [item["assistantId"] for item in payload["workspaceFolders"]] == ["beta"]
    assert payload["indexStatus"]["filesCount"] == 2


def test_admin_workspace_folder_summary_defaults_to_ten_assistants_per_page(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class StubAssistantService:
        def list_assistants(self):
            return [
                SimpleNamespace(
                    assistant_id=f"assistant-{index:02d}",
                    name=f"Assistant {index:02d}",
                    runtime=None,
                )
                for index in range(12)
            ]

    def list_workspace_folder_summaries(repository, assistant_ids):
        captured["assistant_ids"] = assistant_ids
        return SimpleNamespace(
            summaries=[
                SimpleNamespace(
                    assistant_id=assistant_id,
                    buckets=["persistent"],
                    files_count=1,
                    root_items_count=1,
                    last_indexed_at=None,
                    last_synced_at=None,
                    sync_status="synced",
                    sync_error="",
                    stale=False,
                )
                for assistant_id in assistant_ids
            ],
            last_indexed_at=None,
            stale_count=0,
            source="db",
        )

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setitem(
        sys.modules,
        "src.workspace_files.admin_index",
        SimpleNamespace(list_workspace_folder_summaries=list_workspace_folder_summaries),
    )

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.get(
        "/api/v1/admin/workspace-folders/summary",
        headers=_admin_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert captured["assistant_ids"] == [f"assistant-{index:02d}" for index in range(10)]
    assert payload["pagination"]["page_size"] == 10
    assert payload["pagination"]["total"] == 12
    assert [item["assistantId"] for item in payload["workspaceFolders"]] == [
        f"assistant-{index:02d}" for index in range(10)
    ]


def test_admin_workspace_summary_rejects_page_size_over_200(monkeypatch) -> None:
    class ForbiddenAssistantService:
        def list_assistants(self):
            raise AssertionError("page_size validation should run first")

    monkeypatch.setattr(admin_web_app, "AssistantService", ForbiddenAssistantService)

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.get(
        "/api/v1/admin/workspace-files/summary?page_size=201",
        headers=_admin_headers(),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "page_size must be <= 200"


def test_create_workspace_sync_job_returns_pending_without_sync(monkeypatch) -> None:
    captured: dict[str, object] = {"sync_called": False}

    class StubAssistantService:
        async def aget_assistant(self, assistant_id: str):
            return SimpleNamespace(assistant_id=assistant_id)

    class ForbiddenWorkspaceSyncService:
        async def async_sync_assistant_workspace(self, assistant_id: str) -> None:
            captured["sync_called"] = True
            raise AssertionError("job create must not run sync")

    class StubWorkspaceSyncJobService:
        def create_or_get_job(self, **kwargs):
            captured["job_kwargs"] = kwargs
            return SimpleNamespace(
                job_id="job-1",
                status="pending",
                mode=kwargs["mode"],
                assistant_id=kwargs["assistant_id"],
                scope_kind=kwargs["scope_kind"],
                scope_slug=kwargs["scope_slug"],
                priority=kwargs["priority"],
                idempotency_key=kwargs["idempotency_key"],
            )

    class StubIndexFilters:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def list_workspace_file_index_page(repository, filters):
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

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(admin_web_app, "WorkspaceSyncService", ForbiddenWorkspaceSyncService)
    monkeypatch.setattr(
        admin_web_app,
        "_workspace_admin_index_repository",
        lambda: object(),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.workspace_files.admin_index",
        SimpleNamespace(
            WorkspaceFileIndexFilters=StubIndexFilters,
            list_workspace_file_index_page=list_workspace_file_index_page,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.workspace_files.sync_jobs",
        SimpleNamespace(WorkspaceSyncJobService=StubWorkspaceSyncJobService),
    )

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.post(
        "/api/v1/admin/workspace-sync-jobs",
        headers=_admin_headers(),
        json={
            "assistant_id": "alpha",
            "scope_kind": "assistant",
            "mode": "index_only",
            "priority": "normal",
            "idempotency_key": "idem-1",
        },
    )

    assert response.status_code == 202
    payload = response.json()
    assert captured["sync_called"] is False
    assert captured["job_kwargs"]["assistant_id"] == "alpha"
    assert captured["job_kwargs"]["mode"] == "index_only"
    assert captured["index_filters"].assistant_id == "alpha"
    assert payload["job_id"] == "job-1"
    assert payload["status"] == "pending"
    assert payload["indexStatus"]["source"] == "db"
    assert payload["indexStatus"]["pending_job_id"] == "job-1"
    assert payload["workspaceFiles"][0]["file_id"] == "file-1"
    assert payload["assistantSummary"]["assistant_id"] == "alpha"


def test_create_workspace_sync_job_does_not_run_inline_index_job_when_runner_available(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class StubAssistantService:
        async def aget_assistant(self, assistant_id: str):
            return SimpleNamespace(assistant_id=assistant_id)

    class StubWorkspaceSyncJobService:
        def create_or_get_job(self, **kwargs):
            captured["job_kwargs"] = kwargs
            return SimpleNamespace(
                job_id="job-2",
                status="pending",
                mode=kwargs["mode"],
                assistant_id=kwargs["assistant_id"],
                scope_kind=kwargs["scope_kind"],
                scope_slug=kwargs["scope_slug"],
                priority=kwargs["priority"],
                idempotency_key=kwargs["idempotency_key"],
            )

        async def run_index_only_job(self, job_id: str):
            captured["run_job_id"] = job_id
            return SimpleNamespace(
                job_id=job_id,
                status="succeeded",
                mode="index_only",
                assistant_id="alpha",
                scope_kind="assistant",
                scope_slug="",
                priority=100,
                idempotency_key="idem-2",
                scanned_count=2,
                created_count=1,
                updated_count=1,
            )

    class StubIndexFilters:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def list_workspace_file_index_page(repository, filters):
        captured["index_filters"] = filters
        return {
            "records": [
                {
                    "file_id": "file-2",
                    "assistant_id": filters.assistant_id,
                    "bucket": "persistent",
                    "filename": "after-scan.txt",
                    "relative_path": "persistent/after-scan.txt",
                    "source_type": "workspace_scan",
                    "sync_status": "synced",
                }
            ],
            "pagination": {"page": 1, "page_size": 50, "total": 1, "has_next": False},
            "indexStatus": {"source": "db", "sync_status": "synced", "stale": False},
        }

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setattr(
        admin_web_app,
        "_workspace_admin_index_repository",
        lambda: object(),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.workspace_files.admin_index",
        SimpleNamespace(
            WorkspaceFileIndexFilters=StubIndexFilters,
            list_workspace_file_index_page=list_workspace_file_index_page,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.workspace_files.sync_jobs",
        SimpleNamespace(WorkspaceSyncJobService=StubWorkspaceSyncJobService),
    )

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.post(
        "/api/v1/admin/workspace-sync-jobs",
        headers=_admin_headers(),
        json={
            "assistant_id": "alpha",
            "scope_kind": "assistant",
            "mode": "index_only",
            "priority": "normal",
            "idempotency_key": "idem-2",
        },
    )

    assert response.status_code == 202
    payload = response.json()
    assert "run_job_id" not in captured
    assert captured["index_filters"].assistant_id == "alpha"
    assert payload["job_id"] == "job-2"
    assert payload["status"] == "pending"
    assert "scanned_count" not in payload
    assert payload["workspaceFiles"][0]["file_id"] == "file-2"
    assert payload["indexStatus"]["sync_job_id"] == "job-2"
    assert payload["indexStatus"]["pending_job_id"] == "job-2"


def test_create_workspace_sync_job_rejects_deleted_assistant(monkeypatch) -> None:
    captured: dict[str, object] = {"job_called": False}

    class StubAssistantService:
        async def aget_assistant(self, assistant_id: str):
            return SimpleNamespace(assistant_id=assistant_id, deleted=True)

    class ForbiddenWorkspaceSyncJobService:
        def create_or_get_job(self, **kwargs):
            captured["job_called"] = True
            raise AssertionError("deleted assistant must not enqueue workspace sync")

    monkeypatch.setattr(admin_web_app, "AssistantService", StubAssistantService)
    monkeypatch.setitem(
        sys.modules,
        "src.workspace_files.sync_jobs",
        SimpleNamespace(WorkspaceSyncJobService=ForbiddenWorkspaceSyncJobService),
    )

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.post(
        "/api/v1/admin/workspace-sync-jobs",
        headers=_admin_headers(),
        json={
            "assistant_id": "deleted-alpha",
            "scope_kind": "assistant",
            "mode": "index_only",
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "assistant not found"
    assert captured["job_called"] is False


def test_get_workspace_sync_job_returns_status(monkeypatch) -> None:
    class StubWorkspaceSyncJobService:
        def get_job(self, job_id: str):
            return SimpleNamespace(
                job_id=job_id,
                status="running",
                mode="index_only",
                assistant_id="alpha",
                scope_kind="assistant",
                scope_slug=None,
                priority="normal",
                scanned_count=3,
            )

    monkeypatch.setitem(
        sys.modules,
        "src.workspace_files.sync_jobs",
        SimpleNamespace(WorkspaceSyncJobService=StubWorkspaceSyncJobService),
    )

    client = TestClient(admin_web_app.create_admin_web_app())
    response = client.get(
        "/api/v1/admin/workspace-sync-jobs/job-1",
        headers=_admin_headers(),
    )

    assert response.status_code == 200
    assert response.json()["job_id"] == "job-1"
    assert response.json()["status"] == "running"
    assert response.json()["scanned_count"] == 3
