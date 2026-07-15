from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.approval.service import ApprovalService
from src.assistants.service import AssistantService
from src.security_service.app import create_security_app
from src.security_service.file_routes import _get_service
from src.shared.blob_storage import BlobPutResult
from src.shared.config import get_settings
from src.users.service import UserService
from src.workspace_files.service import WorkspaceFileService


class StubBlobStorage:
    def __init__(self) -> None:
        self.payloads: dict[str, bytes] = {}

    def is_enabled(self) -> bool:
        return True

    def put_bytes(self, *, key: str, content: bytes) -> BlobPutResult:
        self.payloads[key] = content
        return BlobPutResult(backend="cos", key=key, url=f"https://cos.example/{key}")

    def get_bytes(self, key: str) -> bytes:
        return self.payloads[key]

    def delete(self, key: str) -> None:
        self.payloads.pop(key, None)


@pytest.fixture()
def contract_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("IRON_AGENT_WORKSPACE_SCAN_ENABLED", "false")
    monkeypatch.setenv("IRON_AGENT_WORKSPACE_OBJECT_STORAGE_PREFIX", "workspace-files")
    get_settings.cache_clear()

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
    approval_svc = ApprovalService(store_path=tmp_path / "approvals.json")
    blob = StubBlobStorage()
    file_svc = WorkspaceFileService(
        files_store=tmp_path / "files.json",
        events_store=tmp_path / "events.json",
        push_store=tmp_path / "push.json",
        assistant_service=assistant_svc,
        user_service=user_svc,
        approval_service=approval_svc,
        blob_storage=blob,
    )
    file_svc._workspace_dir = lambda aid: tmp_path / "workspaces" / aid

    app = create_security_app()
    app.dependency_overrides[_get_service] = lambda: file_svc
    client = TestClient(app)
    try:
        yield client, file_svc, blob
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_upload_response_exposes_sync_metadata(contract_client) -> None:
    client, _, _ = contract_client

    response = client.post(
        "/api/v1/workspace-files/upload",
        data={
            "assistant_id": "test-bot",
            "target_bucket": "temp",
            "source_type": "web",
            "source_ref": "contract-upload",
            "uploader_user_id": "emp001",
        },
        files={"file": ("contract.txt", b"hello contract", "text/plain")},
        headers={"X-IronAgent-Internal-Token": get_settings().internal_service_token},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "accepted"
    assert body["sync_status"] == "synced"
    assert body["storage_backend"] == "cos"
    assert body["storage_key"] == "workspace-files/test-bot/temp/contract.txt"
    assert body["storage_url"] == "https://cos.example/workspace-files/test-bot/temp/contract.txt"


def test_list_response_includes_sync_fields(contract_client) -> None:
    client, file_svc, _ = contract_client
    record = file_svc.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="list-me.txt",
        content=b"listed",
        content_type="text/plain",
        source_type="assistant_output",
    )

    response = client.get(
        "/api/v1/workspace-files",
        params={"assistant_id": "test-bot"},
        headers={"X-IronAgent-Internal-Token": get_settings().internal_service_token},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    item = body["files"][0]
    assert item["file_id"] == record.file_id
    assert item["origin"] == "assistant_output"
    assert item["sync_status"] == "synced"
    assert item["storage_backend"] == "cos"
    assert item["storage_key"] == "workspace-files/test-bot/temp/list-me.txt"
    assert item["workspace_path"] == "/workspace/temp/list-me.txt"


def test_list_response_includes_scanned_skill_files(contract_client) -> None:
    client, file_svc, _ = contract_client
    skill_file = file_svc._workspace_dir("test-bot") / "skills" / "web-crawl" / "SKILL.md"
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    skill_file.write_text("crawl skill", encoding="utf-8")

    sync_response = client.post(
        "/api/v1/workspace-files/sync-results",
        params={"assistant_id": "test-bot", "bucket": "skills"},
        headers={"X-IronAgent-Internal-Token": get_settings().internal_service_token},
    )
    assert sync_response.status_code == 200, sync_response.text

    response = client.get(
        "/api/v1/workspace-files",
        params={"assistant_id": "test-bot", "bucket": "skills"},
        headers={"X-IronAgent-Internal-Token": get_settings().internal_service_token},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    item = body["files"][0]
    assert item["bucket"] == "skills"
    assert item["filename"] == "web-crawl/SKILL.md"
    assert item["origin"] == "capability_package"
    assert item["workspace_path"] == "/workspace/skills/web-crawl/SKILL.md"


def test_download_uses_workspace_copy(contract_client) -> None:
    client, file_svc, blob = contract_client
    record = file_svc.upload_file(
        assistant_id="test-bot",
        bucket="temp",
        filename="download-me.txt",
        content=b"workspace-copy",
        content_type="text/plain",
    )
    blob.payloads[record.storage_key] = b"blob-copy"

    response = client.get(
        f"/api/v1/workspace-files/{record.file_id}/download",
        headers={"X-IronAgent-Internal-Token": get_settings().internal_service_token},
    )

    assert response.status_code == 200, response.text
    assert response.content == b"workspace-copy"
