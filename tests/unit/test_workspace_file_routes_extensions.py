from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.approval.service import ApprovalService
from src.assistants.service import AssistantService
from src.security_service import file_routes
from src.security_service.app import create_security_app
from src.security_service.file_routes import _get_service
from src.shared.config import get_settings
from src.users.service import UserService
from src.workspace_files.service import WorkspaceFileService


@pytest.fixture()
def workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "workspaces"
    root.mkdir()
    monkeypatch.setenv("IRON_AGENT_ASSISTANTS_WORKSPACE_ROOT", str(root))
    get_settings.cache_clear()
    return root


@pytest.fixture()
def file_svc(tmp_path: Path, workspace_root: Path) -> WorkspaceFileService:
    assistant_svc = AssistantService(store_path=tmp_path / "assistants.json")
    assistant_svc.upsert_assistant(
        assistant_id="test-bot",
        name="Test Bot",
        capability_profile="onlyread",
        provision_runtime=False,
    )
    user_svc = UserService(
        store_path=tmp_path / "users.json",
        bindings_path=tmp_path / "bindings.json",
    )
    user_svc.create_user("u-1", display_name="User One")
    user_svc.bind_assistant("u-1", "test-bot", alias="bot")
    approval_svc = ApprovalService(store_path=tmp_path / "approvals.json")
    svc = WorkspaceFileService(
        files_store=tmp_path / "files.json",
        events_store=tmp_path / "events.json",
        push_store=tmp_path / "push.json",
        assistant_service=assistant_svc,
        user_service=user_svc,
        approval_service=approval_svc,
    )
    svc._workspace_dir = lambda aid: workspace_root / aid
    svc.workspace_writer.workspace_dir_resolver = lambda aid: workspace_root / aid
    return svc


@pytest.fixture()
def client(file_svc: WorkspaceFileService) -> TestClient:
    app = create_security_app()
    app.dependency_overrides[_get_service] = lambda: file_svc
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    return {"X-IronAgent-Internal-Token": get_settings().internal_service_token}


def test_sync_file_route_returns_storage_fields(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    upload = client.post(
        "/api/v1/workspace-files/upload",
        data={"assistant_id": "test-bot", "target_bucket": "temp"},
        files={"file": ("sync-me.txt", b"hello", "text/plain")},
        headers=admin_headers,
    )
    assert upload.status_code == 200, upload.text
    file_id = upload.json()["file_id"]

    sync = client.post(f"/api/v1/workspace-files/{file_id}/sync", headers=admin_headers)
    assert sync.status_code == 200, sync.text
    payload = sync.json()
    assert payload["status"] == "synced"
    assert payload["file"]["file_id"] == file_id
    assert payload["file"]["storage_status"] in {"disabled", "synced", "failed"}


def test_scan_workspace_results_route_ingests_new_files(
    client: TestClient,
    file_svc: WorkspaceFileService,
    admin_headers: dict[str, str],
) -> None:
    result_dir = file_svc.workspace_writer.bucket_dir("test-bot", "persistent")
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "scan-result.txt").write_text("scan result body", encoding="utf-8")

    response = client.post(
        "/api/v1/workspace-files/scan",
        data={"assistant_id": "test-bot"},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["assistant_id"] == "test-bot"
    assert payload["scanned"] == 1
    assert payload["files"][0]["filename"] == "scan-result.txt"
    assert payload["files"][0]["file_role"] == "result"


def test_list_files_supports_user_and_role_filters(
    client: TestClient,
    file_svc: WorkspaceFileService,
    admin_headers: dict[str, str],
) -> None:
    input_record = file_svc.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="input.txt",
        content=b"input",
        content_type="text/plain",
        user_id="u-1",
    )
    assert hasattr(input_record, "file_id")

    result_dir = file_svc.workspace_writer.bucket_dir("test-bot", "persistent")
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "result.txt").write_text("result body", encoding="utf-8")
    file_svc.scan_workspace_results("test-bot")

    response = client.get(
        "/api/v1/workspace-files",
        params={"user_id": "u-1", "file_role": "result"},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 1
    assert payload["files"][0]["filename"] == "result.txt"
    assert payload["files"][0]["status"] in {"ready", "workspace_only", "storage_failed", "pending"}
    assert payload["files"][0]["download_url"].endswith("/download")


def test_sync_results_route_scopes_to_result_path_prefix(
    client: TestClient,
    file_svc: WorkspaceFileService,
    workspace_root: Path,
    admin_headers: dict[str, str],
) -> None:
    task_a = workspace_root / "test-bot" / "temp" / "go-gateway-results" / "task-a" / "result.txt"
    task_b = workspace_root / "test-bot" / "temp" / "go-gateway-results" / "task-b" / "result.txt"
    task_a.parent.mkdir(parents=True, exist_ok=True)
    task_b.parent.mkdir(parents=True, exist_ok=True)
    task_a.write_text("task a result", encoding="utf-8")
    task_b.write_text("task b result", encoding="utf-8")

    response = client.post(
        "/api/v1/workspace-files/sync-results",
        params={
            "assistant_id": "test-bot",
            "bucket": "temp",
            "user_id": "u-1",
            "task_id": "task-a",
            "result_path_prefix": "go-gateway-results/task-a/",
        },
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["files_scanned"] == 1
    assert payload["files_synced"] == 1
    files = file_svc.list_files("test-bot", bucket="temp")
    assert len(files) == 1
    assert files[0].filename == "go-gateway-results/task-a/result.txt"
    assert files[0].source_ref == "task-a"
    assert files[0].owner_user_id == "u-1"


def test_sync_results_route_records_multiple_files_in_same_task_prefix(
    client: TestClient,
    file_svc: WorkspaceFileService,
    workspace_root: Path,
    admin_headers: dict[str, str],
) -> None:
    result_dir = workspace_root / "test-bot" / "temp" / "go-gateway-results" / "task-a"
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "report.txt").write_text("task a report", encoding="utf-8")
    (result_dir / "summary.json").write_text('{"ok": true}', encoding="utf-8")

    response = client.post(
        "/api/v1/workspace-files/sync-results",
        params={
            "assistant_id": "test-bot",
            "bucket": "temp",
            "user_id": "u-1",
            "task_id": "task-a",
            "result_path_prefix": "go-gateway-results/task-a/",
        },
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["files_scanned"] == 2
    assert payload["files_synced"] == 2
    files = sorted(file_svc.list_files("test-bot", bucket="temp"), key=lambda item: item.filename)
    assert [item.filename for item in files] == [
        "go-gateway-results/task-a/report.txt",
        "go-gateway-results/task-a/summary.json",
    ]
    assert {item.source_ref for item in files} == {"task-a"}
    assert {item.owner_user_id for item in files} == {"u-1"}
    assert {item.direction for item in files} == {"outbound"}
    assert {item.file_role for item in files} == {"result"}


def test_sync_results_route_accepts_task_id_alias(
    client: TestClient,
    file_svc: WorkspaceFileService,
    workspace_root: Path,
    admin_headers: dict[str, str],
) -> None:
    result_file = (
        workspace_root / "test-bot" / "temp" / "go-gateway-results" / "task-c" / "result.txt"
    )
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.write_text("task c result", encoding="utf-8")

    response = client.post(
        "/api/v1/workspace-files/sync-results",
        params={
            "assistant_id": "test-bot",
            "bucket": "temp",
            "user_id": "u-1",
            "taskId": "task-c",
            "result_path_prefix": "go-gateway-results/task-c/",
        },
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    files = file_svc.list_files("test-bot", bucket="temp")
    assert len(files) == 1
    assert files[0].filename == "go-gateway-results/task-c/result.txt"
    assert files[0].source_ref == "task-c"


def test_sync_results_route_rejects_task_scope_without_result_prefix(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    response = client.post(
        "/api/v1/workspace-files/sync-results",
        params={
            "assistant_id": "test-bot",
            "bucket": "temp",
            "user_id": "u-1",
            "task_id": "task-a",
            "source_ref": "task-a",
        },
        headers=admin_headers,
    )

    assert response.status_code == 400
    assert "result_path_prefix" in response.json()["detail"]

    user_scoped_response = client.post(
        "/api/v1/workspace-files/sync-results",
        params={
            "assistant_id": "test-bot",
            "bucket": "temp",
            "user_id": "u-1",
        },
        headers=admin_headers,
    )

    assert user_scoped_response.status_code == 400
    assert "result_path_prefix" in user_scoped_response.json()["detail"]


def test_sync_results_route_rejects_invalid_result_prefix(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    response = client.post(
        "/api/v1/workspace-files/sync-results",
        params={
            "assistant_id": "test-bot",
            "bucket": "temp",
            "result_path_prefix": "../go-gateway-results/task-a/",
        },
        headers=admin_headers,
    )

    assert response.status_code == 400
    assert "path traversal" in response.json()["detail"]


def test_sync_results_route_rejects_non_result_bucket(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    response = client.post(
        "/api/v1/workspace-files/sync-results",
        params={
            "assistant_id": "test-bot",
            "bucket": "skills",
            "result_path_prefix": "go-gateway-results/task-a/",
        },
        headers=admin_headers,
    )

    assert response.status_code == 400
    assert "result sync bucket" in response.json()["detail"]


def test_sync_results_route_rejects_bucket_level_prefix(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    response = client.post(
        "/api/v1/workspace-files/sync-results",
        params={
            "assistant_id": "test-bot",
            "bucket": "temp",
            "result_path_prefix": "temp",
        },
        headers=admin_headers,
    )

    assert response.status_code == 400
    assert "task directory" in response.json()["detail"]


def test_sync_results_route_source_ref_overrides_task_id(
    client: TestClient,
    file_svc: WorkspaceFileService,
    workspace_root: Path,
    admin_headers: dict[str, str],
) -> None:
    result_file = (
        workspace_root / "test-bot" / "temp" / "go-gateway-results" / "task-d" / "result.txt"
    )
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.write_text("task d result", encoding="utf-8")

    response = client.post(
        "/api/v1/workspace-files/sync-results",
        params={
            "assistant_id": "test-bot",
            "bucket": "temp",
            "task_id": "task-d",
            "source_ref": "runtime-task-d",
            "result_path_prefix": "go-gateway-results/task-d/",
        },
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    files = file_svc.list_files("test-bot", bucket="temp")
    assert len(files) == 1
    assert files[0].source_ref == "runtime-task-d"


def test_list_files_syncs_assistant_workspace_when_requested(
    client: TestClient,
    file_svc: WorkspaceFileService,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_svc.upload_file(
        assistant_id="test-bot",
        bucket="persistent",
        filename="listed.txt",
        content=b"listed",
        content_type="text/plain",
    )
    synced: list[str] = []

    class StubWorkspaceSyncService:
        def sync_assistant_workspace(self, assistant_id: str) -> None:
            synced.append(assistant_id)

    monkeypatch.setattr(file_routes, "WorkspaceSyncService", StubWorkspaceSyncService)

    response = client.get(
        "/api/v1/workspace-files",
        params={"assistant_id": "test-bot", "sync": "true"},
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    assert synced == ["test-bot"]
    assert response.json()["total"] == 1


def test_list_files_sync_failure_does_not_block_listing(
    client: TestClient,
    file_svc: WorkspaceFileService,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_svc.upload_file(
        assistant_id="test-bot",
        bucket="persistent",
        filename="still-listed.txt",
        content=b"listed",
        content_type="text/plain",
    )

    class FailingWorkspaceSyncService:
        def sync_assistant_workspace(self, assistant_id: str) -> None:
            raise RuntimeError(f"sync failed for {assistant_id}")

    monkeypatch.setattr(file_routes, "WorkspaceSyncService", FailingWorkspaceSyncService)

    response = client.get(
        "/api/v1/workspace-files",
        params={"assistant_id": "test-bot", "sync": "true"},
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1


def test_list_files_requires_internal_service_auth(client: TestClient) -> None:
    response = client.get(
        "/api/v1/workspace-files",
        params={"assistant_id": "test-bot"},
    )

    assert response.status_code == 401


def test_list_files_reuses_workspace_root_for_large_responses(
    client: TestClient,
    file_svc: WorkspaceFileService,
    workspace_root: Path,
    admin_headers: dict[str, str],
) -> None:
    for index in range(5):
        record = file_svc.upload_file(
            assistant_id="test-bot",
            bucket="persistent",
            filename=f"bulk-{index}.txt",
            content=f"bulk {index}".encode(),
            content_type="text/plain",
        )
        assert hasattr(record, "file_id")

    calls = 0

    def resolve_workspace(assistant_id: str) -> Path:
        nonlocal calls
        calls += 1
        return workspace_root / assistant_id

    file_svc._workspace_dir = resolve_workspace

    response = client.get(
        "/api/v1/workspace-files",
        params={"assistant_id": "test-bot"},
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 5
    assert calls == 1


def test_download_info_route_returns_frontend_fields(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    upload = client.post(
        "/api/v1/workspace-files/upload",
        data={"assistant_id": "test-bot", "target_bucket": "temp"},
        files={"file": ("front.txt", b"hello", "text/plain")},
        headers=admin_headers,
    )
    assert upload.status_code == 200, upload.text
    file_id = upload.json()["file_id"]

    response = client.get(f"/api/v1/workspace-files/{file_id}/download-info", headers=admin_headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["file_id"] == file_id
    assert payload["filename"] == "front.txt"
    assert payload["workspace_relative_path"] == "temp/front.txt"
    assert payload["download_url"].endswith(f"/{file_id}/download")


def test_download_route_respects_inline_disposition(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    upload = client.post(
        "/api/v1/workspace-files/upload",
        data={"assistant_id": "test-bot", "target_bucket": "temp"},
        files={"file": ("preview.txt", b"preview body", "text/plain")},
        headers=admin_headers,
    )
    assert upload.status_code == 200, upload.text
    file_id = upload.json()["file_id"]

    response = client.get(
        f"/api/v1/workspace-files/{file_id}/download",
        params={"disposition": "inline"},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"].startswith('inline; filename="preview.txt"')
    assert response.text == "preview body"


def test_download_route_requires_internal_service_auth(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    upload = client.post(
        "/api/v1/workspace-files/upload",
        data={"assistant_id": "test-bot", "target_bucket": "temp"},
        files={"file": ("private.txt", b"private body", "text/plain")},
        headers=admin_headers,
    )
    assert upload.status_code == 200, upload.text
    file_id = upload.json()["file_id"]

    response = client.get(f"/api/v1/workspace-files/{file_id}/download")

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("bucket", "relative_name", "content"),
    [
        ("skills", "web-crawl/SKILL.md", "crawl skill"),
        ("plugins", "audit-plugin/manifest.json", '{"name":"audit"}'),
    ],
)
def test_admin_can_download_readonly_capability_files(
    client: TestClient,
    file_svc: WorkspaceFileService,
    workspace_root: Path,
    admin_headers: dict[str, str],
    bucket: str,
    relative_name: str,
    content: str,
) -> None:
    path = workspace_root / "test-bot" / bucket / relative_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    file_svc.scan_workspace_results(assistant_id="test-bot", bucket=bucket)  # type: ignore[arg-type]
    record = file_svc.list_files("test-bot", bucket=bucket)[0]  # type: ignore[arg-type]

    response = client.get(
        f"/api/v1/workspace-files/{record.file_id}/download",
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    assert response.text == content


def test_download_route_encodes_non_ascii_filename_in_content_disposition(
    client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    upload = client.post(
        "/api/v1/workspace-files/upload",
        data={"assistant_id": "test-bot", "target_bucket": "temp"},
        files={"file": ("初稿1.txt", "中文预览内容".encode(), "text/plain")},
        headers=admin_headers,
    )
    assert upload.status_code == 200, upload.text
    file_id = upload.json()["file_id"]

    response = client.get(
        f"/api/v1/workspace-files/{file_id}/download",
        params={"disposition": "inline"},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    header = response.headers["content-disposition"]
    assert header.startswith('inline; filename="1.txt";')
    assert "filename*=UTF-8''%E5%88%9D%E7%A8%BF1.txt" in header
    assert response.text == "中文预览内容"


def test_user_download_checks_owner_and_workspace_presence(
    client: TestClient,
    file_svc: WorkspaceFileService,
    workspace_root: Path,
) -> None:
    uploaded = file_svc.upload_file(
        user_id="u-1",
        bucket="temp",
        filename="owned.txt",
        content=b"owned body",
        content_type="text/plain",
    )
    assert hasattr(uploaded, "file_id")

    response = client.get(f"/api/v1/users/u-1/workspace-files/{uploaded.file_id}/download")
    assert response.status_code == 200, response.text
    assert response.content == b"owned body"
    assert response.headers["x-ironagent-download-source"] == "workspace"

    forbidden = client.get(f"/api/v1/users/u-2/workspace-files/{uploaded.file_id}/download")
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"] == "owner mismatch"

    (workspace_root / "test-bot" / "temp" / "owned.txt").unlink()
    missing = client.get(f"/api/v1/users/u-1/workspace-files/{uploaded.file_id}/download")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "content_not_found"


@pytest.mark.parametrize("bucket", ["skills", "plugins"])
def test_upload_route_rejects_readonly_buckets(
    client: TestClient,
    admin_headers: dict[str, str],
    bucket: str,
) -> None:
    response = client.post(
        "/api/v1/workspace-files/upload",
        data={"assistant_id": "test-bot", "target_bucket": bucket},
        files={"file": ("package.txt", b"package", "text/plain")},
        headers=admin_headers,
    )

    assert response.status_code == 403, response.text
    assert "read-only" in response.json()["detail"]


def test_delete_route_rejects_readonly_capability_files(
    client: TestClient,
    file_svc: WorkspaceFileService,
    workspace_root: Path,
    admin_headers: dict[str, str],
) -> None:
    skill_path = workspace_root / "test-bot" / "skills" / "web-crawl" / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text("crawl skill", encoding="utf-8")
    file_svc.scan_workspace_results(assistant_id="test-bot", bucket="skills")
    record = file_svc.list_files("test-bot", bucket="skills")[0]

    response = client.post(
        f"/api/v1/workspace-files/{record.file_id}/delete",
        headers=admin_headers,
    )

    assert response.status_code == 403, response.text
    assert "read-only" in response.json()["detail"]
    assert skill_path.exists()


@pytest.mark.parametrize("bucket", ["temp", "persistent"])
def test_upload_route_accepts_duplicate_name_overwrite_without_overwrite_flag(
    client: TestClient,
    file_svc: WorkspaceFileService,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    bucket: str,
) -> None:
    monkeypatch.setattr(file_routes, "_get_approval_service", lambda: file_svc.approval_svc)
    filename = f"{bucket}-report.txt"

    first_upload = client.post(
        "/api/v1/workspace-files/upload",
        data={"assistant_id": "test-bot", "target_bucket": bucket},
        files={"file": (filename, b"v1", "text/plain")},
        headers=admin_headers,
    )
    assert first_upload.status_code == 200, first_upload.text
    first_payload = first_upload.json()

    overwrite_attempt = client.post(
        "/api/v1/workspace-files/upload",
        data={"assistant_id": "test-bot", "target_bucket": bucket},
        files={"file": (filename, b"v2", "text/plain")},
        headers=admin_headers,
    )

    assert overwrite_attempt.status_code == 200, overwrite_attempt.text
    payload = overwrite_attempt.json()
    assert payload["status"] == "accepted"
    assert payload["file_id"] == first_payload["file_id"]
    assert payload["approval_id"] is None
    assert payload["approval_scope"] is None
    assert payload["filename"] == filename
    assert payload["bucket"] == bucket

    downloaded = file_svc.download_file(payload["file_id"])
    assert downloaded is not None
    assert downloaded.content == b"v2"


def test_overview_route_counts_files_and_results(
    client: TestClient,
    file_svc: WorkspaceFileService,
    admin_headers: dict[str, str],
) -> None:
    file_svc.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="overview-input.txt",
        content=b"input",
        content_type="text/plain",
        user_id="u-1",
    )
    result_dir = file_svc.workspace_writer.bucket_dir("test-bot", "persistent")
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "overview-result.txt").write_text("result", encoding="utf-8")
    file_svc.scan_workspace_results("test-bot")

    response = client.get(
        "/api/v1/workspace-files/overview",
        params={"user_id": "u-1"},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["assistant_id"] == "test-bot"
    assert payload["total"] == 2
    assert payload["input_count"] == 1
    assert payload["result_count"] == 1
    assert payload["latest_results"][0]["filename"] == "overview-result.txt"
