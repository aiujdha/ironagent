"""Unit tests for the assistants_service module."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.assistants_service.app import create_assistants_app


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IRON_AGENT_STORAGE_BACKEND", "json")
    monkeypatch.setenv("IRON_AGENT_ASSISTANTS_STORE_PATH", str(tmp_path / "assistants.json"))
    monkeypatch.setenv("IRON_AGENT_INTERNAL_SERVICE_TOKEN", "test-token")
    from src.shared.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_assistants_app(), raise_server_exceptions=False)


ADMIN_HEADERS = {"X-IronAgent-Internal-Token": "test-token"}


class TestHealthEndpoint:
    def test_health(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["service"] == "assistants_service"


class TestListAssistants:
    def test_list_returns_seeded_defaults(self, client: TestClient) -> None:
        resp = client.get("/api/v1/assistants")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 1
        assert isinstance(body["assistants"], list)

    def test_list_after_create(self, client: TestClient) -> None:
        client.post(
            "/api/v1/assistants",
            json={
                "assistant_id": "test-1",
                "name": "Test Assistant",
                "capability_profile": "chat",
                "provision_runtime": False,
            },
            headers=ADMIN_HEADERS,
        )
        resp = client.get("/api/v1/assistants")
        ids = [a["assistant_id"] for a in resp.json()["assistants"]]
        assert "test-1" in ids

    def test_list_excludes_soft_deleted_assistants(self, client: TestClient) -> None:
        from src.assistants.service import AssistantService

        client.post(
            "/api/v1/assistants",
            json={
                "assistant_id": "deleted-list",
                "name": "Deleted List",
                "capability_profile": "chat",
                "provision_runtime": False,
            },
            headers=ADMIN_HEADERS,
        )
        AssistantService().delete_assistant("deleted-list")

        resp = client.get("/api/v1/assistants")

        assert resp.status_code == 200
        ids = [a["assistant_id"] for a in resp.json()["assistants"]]
        assert "deleted-list" not in ids


class TestGetAssistant:
    def test_get_existing(self, client: TestClient) -> None:
        client.post(
            "/api/v1/assistants",
            json={
                "assistant_id": "get-test",
                "name": "Get Test",
                "provision_runtime": False,
            },
            headers=ADMIN_HEADERS,
        )
        resp = client.get("/api/v1/assistants/get-test")
        assert resp.status_code == 200
        assert resp.json()["assistant_id"] == "get-test"

    def test_get_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/v1/assistants/nonexistent-id")
        assert resp.status_code == 404

    def test_get_soft_deleted_returns_not_found(self, client: TestClient) -> None:
        from src.assistants.service import AssistantService

        client.post(
            "/api/v1/assistants",
            json={
                "assistant_id": "deleted-get",
                "name": "Deleted Get",
                "provision_runtime": False,
            },
            headers=ADMIN_HEADERS,
        )
        AssistantService().delete_assistant("deleted-get")

        resp = client.get("/api/v1/assistants/deleted-get")

        assert resp.status_code == 404


class TestCreateAssistant:
    def test_create_success(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/assistants",
            json={
                "assistant_id": "new-1",
                "name": "New Assistant",
                "capability_profile": "onlywrite",
                "notes": "test notes",
                "provision_runtime": False,
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["assistant_id"] == "new-1"
        assert body["name"] == "New Assistant"
        assert body["capability_profile"] == "onlywrite"
        assert body["notes"] == "test notes"

    def test_create_duplicate_returns_409(self, client: TestClient) -> None:
        client.post(
            "/api/v1/assistants",
            json={
                "assistant_id": "dup-1",
                "name": "First",
                "provision_runtime": False,
            },
            headers=ADMIN_HEADERS,
        )
        resp = client.post(
            "/api/v1/assistants",
            json={
                "assistant_id": "dup-1",
                "name": "Second",
                "provision_runtime": False,
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 409

    def test_create_requires_auth(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/assistants",
            json={
                "assistant_id": "no-auth",
                "name": "No Auth",
                "provision_runtime": False,
            },
        )
        assert resp.status_code == 401


class TestUpdateAssistant:
    def test_update_name(self, client: TestClient) -> None:
        client.post(
            "/api/v1/assistants",
            json={
                "assistant_id": "upd-1",
                "name": "Original",
                "provision_runtime": False,
            },
            headers=ADMIN_HEADERS,
        )
        resp = client.put(
            "/api/v1/assistants/upd-1",
            json={"name": "Updated", "provision_runtime": False},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated"

    def test_update_profile(self, client: TestClient) -> None:
        client.post(
            "/api/v1/assistants",
            json={
                "assistant_id": "upd-2",
                "name": "Profile Test",
                "capability_profile": "chat",
                "provision_runtime": False,
            },
            headers=ADMIN_HEADERS,
        )
        resp = client.put(
            "/api/v1/assistants/upd-2",
            json={"capability_profile": "dangerous", "provision_runtime": False},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["capability_profile"] == "dangerous"

    def test_update_not_found(self, client: TestClient) -> None:
        resp = client.put(
            "/api/v1/assistants/nonexistent",
            json={"name": "X", "provision_runtime": False},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 404

    def test_update_requires_auth(self, client: TestClient) -> None:
        resp = client.put(
            "/api/v1/assistants/any-id",
            json={"name": "X", "provision_runtime": False},
        )
        assert resp.status_code == 401
