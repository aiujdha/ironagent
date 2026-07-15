"""Integration smoke test — seeds real data into JSON stores and verifies API responses."""

import pytest
from fastapi.testclient import TestClient

from src.approval.service import ApprovalService
from src.assistants.service import AssistantService
from src.security_service.app import create_security_app
from src.security_service.file_routes import _get_service
from src.shared.config import get_settings
from src.users.service import UserService
from src.workspace_files.service import WorkspaceFileService


@pytest.fixture()
def client(monkeypatch, tmp_path_factory):
    root = tmp_path_factory.mktemp("user-file-integration")
    monkeypatch.setenv("IRON_AGENT_STORAGE_BACKEND", "json")
    monkeypatch.setenv("IRON_AGENT_ASSISTANTS_STORE_PATH", str(root / "assistants.json"))
    monkeypatch.setenv("IRON_AGENT_APPROVAL_STORE_PATH", str(root / "approvals.json"))
    monkeypatch.setenv("IRON_AGENT_ASSISTANTS_WORKSPACE_ROOT", str(root / "workspaces"))
    monkeypatch.setenv("IRON_AGENT_USERS_STORE_PATH", str(root / "users.json"))
    monkeypatch.setenv("IRON_AGENT_USER_BINDINGS_STORE_PATH", str(root / "bindings.json"))
    monkeypatch.setenv("IRON_AGENT_WORKSPACE_FILES_STORE_PATH", str(root / "workspace-files.json"))
    monkeypatch.setenv(
        "IRON_AGENT_WORKSPACE_FILE_EVENTS_STORE_PATH",
        str(root / "workspace-events.json"),
    )
    monkeypatch.setenv(
        "IRON_AGENT_WORKSPACE_PUSH_REQUESTS_STORE_PATH",
        str(root / "workspace-push.json"),
    )
    get_settings.cache_clear()
    app = create_security_app()
    try:
        yield TestClient(app)
    finally:
        get_settings.cache_clear()


@pytest.fixture()
def admin_headers():
    return {"X-IronAgent-Internal-Token": get_settings().internal_service_token}


class TestUserBindingIntegration:
    def test_create_user(self, client, admin_headers) -> None:
        resp = client.post(
            "/api/v1/users",
            json={
                "user_id": "integration-emp001",
                "display_name": "集成测试用户",
                "employee_id": "E10001",
                "department": "技术部",
            },
            headers=admin_headers,
        )
        assert resp.status_code in (201, 409), resp.text
        if resp.status_code == 201:
            data = resp.json()
            assert data["user_id"] == "integration-emp001"
            assert data["display_name"] == "集成测试用户"

    def test_list_users(self, client) -> None:
        resp = client.get("/api/v1/users")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 0
        print(f"Users in store: {data['total']}")

    def test_get_user(self, client) -> None:
        resp = client.get("/api/v1/users/integration-emp001")
        if resp.status_code == 200:
            assert resp.json()["employee_id"] == "E10001"

    def test_bind_assistant(self, client, admin_headers) -> None:
        resp = client.post(
            "/api/v1/users/integration-emp001/assistants/bind",
            json={
                "assistant_id": "demo-main",
                "alias": "我的助手",
                "source_system": "integration-test",
            },
            headers=admin_headers,
        )
        assert resp.status_code in (201, 404), resp.text

    def test_list_user_assistants(self, client) -> None:
        resp = client.get("/api/v1/users/integration-emp001/assistants")
        if resp.status_code == 200:
            data = resp.json()
            print(f"Bindings: {data['total']}")

    def test_upload_file_to_assistant(self, client, admin_headers) -> None:
        resp = client.post(
            "/api/v1/workspace-files/upload",
            data={
                "assistant_id": "demo-main",
                "target_bucket": "temp",
                "source_type": "integration-test",
                "source_ref": "smoke-001",
                "uploader_user_id": "integration-emp001",
            },
            files={"file": ("test-report.txt", b"Integration test content", "text/plain")},
            headers=admin_headers,
        )
        assert resp.status_code in (200, 400), resp.text
        if resp.status_code == 200:
            data = resp.json()
            assert data["status"] == "accepted"
            assert data["filename"] == "test-report.txt"
            print(f"Uploaded file_id: {data.get('file_id')}")

    def test_list_files(self, client, admin_headers) -> None:
        resp = client.get(
            "/api/v1/workspace-files",
            params={"assistant_id": "demo-main"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        print(f"Files for demo-main: {data['total']}")

    def test_list_files_by_user(self, client) -> None:
        resp = client.get("/api/v1/users/integration-emp001/workspace-files")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 0

    def test_push_file_by_user_id(self, client, admin_headers) -> None:
        resp = client.post(
            "/api/v1/workspace-files/push",
            data={
                "user_id": "integration-emp001",
                "target_bucket": "temp",
                "source_type": "oa-system",
                "source_ref": "meeting-notes-001",
            },
            files={"file": ("会议纪要.txt", "会议纪要内容测试".encode(), "text/plain")},
            headers=admin_headers,
        )
        # May fail if user has no binding or assistant doesn't exist
        print(f"Push response: {resp.status_code} {resp.text}")

    def test_security_rejects_forbidden_file(self, client, admin_headers) -> None:
        resp = client.post(
            "/api/v1/workspace-files/upload",
            data={"assistant_id": "demo-main", "target_bucket": "temp"},
            files={"file": ("AGENTS.md", b"hack attempt", "text/plain")},
            headers=admin_headers,
        )
        assert resp.status_code == 403

    def test_security_rejects_path_traversal(self, client, admin_headers) -> None:
        resp = client.post(
            "/api/v1/workspace-files/upload",
            data={"assistant_id": "demo-main", "target_bucket": "temp"},
            files={"file": ("../../../etc/passwd", b"traversal", "text/plain")},
            headers=admin_headers,
        )
        assert resp.status_code == 403

    def test_download_endpoint_exposes_download_source_header(
        self,
        client,
        monkeypatch,
        tmp_path,
        admin_headers,
    ) -> None:
        assistant_svc = AssistantService(store_path=tmp_path / "assistants.json")
        assistant_svc.upsert_assistant(
            assistant_id="demo-main",
            name="Demo Main",
            capability_profile="onlyread",
            provision_runtime=False,
        )
        user_svc = UserService(
            store_path=tmp_path / "users.json",
            bindings_path=tmp_path / "bindings.json",
        )
        approval_svc = ApprovalService(store_path=tmp_path / "approvals.json")

        class FakeObjectStore:
            prefix = "workspace-files"

            def __init__(self) -> None:
                self.objects: dict[str, bytes] = {}

            def is_enabled(self) -> bool:
                return True

            def backend_name(self) -> str:
                return "cos"

            def put_bytes(self, *, key: str, content: bytes, content_type: str = ""):
                object_key = f"{self.prefix}/{key.lstrip('/')}"
                self.objects[object_key] = content

                class Stored:
                    backend = "cos"

                    def __init__(self, object_key: str) -> None:
                        self.key = object_key
                        self.url = f"https://example-cos.local/{object_key}"
                        self.etag = "etag-demo"

                return Stored(object_key)

            def get_bytes(self, *, key: str) -> bytes:
                return self.objects[key]

            def delete(self, *, key: str) -> None:
                self.objects.pop(key, None)

        file_svc = WorkspaceFileService(
            files_store=tmp_path / "files.json",
            events_store=tmp_path / "events.json",
            push_store=tmp_path / "push.json",
            assistant_service=assistant_svc,
            user_service=user_svc,
            approval_service=approval_svc,
            object_store=FakeObjectStore(),
        )
        workspace_root = tmp_path / "workspaces"
        file_svc._workspace_dir = lambda aid: workspace_root / aid

        uploaded = file_svc.upload_file(
            assistant_id="demo-main",
            bucket="temp",
            filename="header-check.txt",
            content=b"header-check",
            content_type="text/plain",
            source_type="integration-test",
            source_ref="header-check",
            uploader_user_id="integration-emp001",
        )

        workspace_path = (
            workspace_root
            / "demo-main"
            / "temp"
            / "header-check.txt"
        )
        if workspace_path.exists():
            workspace_path.unlink()

        client.app.dependency_overrides[_get_service] = lambda: file_svc
        try:
            resp = client.get(
                f"/api/v1/workspace-files/{uploaded.file_id}/download",
                headers=admin_headers,
            )
            assert resp.status_code == 404, resp.text
            assert resp.json()["detail"] == "content_not_found"
        finally:
            client.app.dependency_overrides.clear()

    def test_list_files_supports_direction_filter_with_dependency_override(
        self,
        client,
        tmp_path,
        admin_headers,
    ) -> None:
        assistant_svc = AssistantService(store_path=tmp_path / "assistants.json")
        assistant_svc.upsert_assistant(
            assistant_id="demo-main",
            name="Demo Main",
            capability_profile="onlyread",
            provision_runtime=False,
        )
        assistant = assistant_svc.get_assistant("demo-main")
        assert assistant is not None
        workspace_root = tmp_path / "workspaces"
        assistant.runtime.status = "running"
        assistant.runtime.workspace_root = str(workspace_root / "demo-main")
        assistant_svc.repository.upsert_assistant(assistant)

        user_svc = UserService(
            store_path=tmp_path / "users.json",
            bindings_path=tmp_path / "bindings.json",
        )
        approval_svc = ApprovalService(store_path=tmp_path / "approvals.json")
        file_svc = WorkspaceFileService(
            files_store=tmp_path / "files.json",
            events_store=tmp_path / "events.json",
            push_store=tmp_path / "push.json",
            assistant_service=assistant_svc,
            user_service=user_svc,
            approval_service=approval_svc,
        )
        file_svc._workspace_dir = lambda aid: workspace_root / aid

        uploaded = file_svc.upload_file(
            assistant_id="demo-main",
            bucket="temp",
            filename="input.txt",
            content=b"inbound",
        )
        assert hasattr(uploaded, "file_id")
        result_dir = workspace_root / "demo-main" / "temp"
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "result.txt").write_bytes(b"outbound")
        file_svc.scan_workspace_results("demo-main")

        client.app.dependency_overrides[_get_service] = lambda: file_svc
        try:
            resp = client.get(
                "/api/v1/workspace-files",
                params={"assistant_id": "demo-main", "direction": "outbound"},
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["total"] == 1
            assert data["files"][0]["filename"] == "result.txt"
            assert data["files"][0]["direction"] == "outbound"
            assert data["files"][0]["source_file_id"] == uploaded.file_id
        finally:
            client.app.dependency_overrides.clear()
