import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from src.assistants.models import AssistantRecord
from src.assistants.service import AssistantService
from src.main import app
from src.runtime.models import AssistantRuntimeRecord
from src.security_service import routes
from src.shared.config import get_settings
from src.workspace_files.service import WorkspaceFileService


@pytest.fixture(autouse=True)
def approval_mode_enabled(monkeypatch):
    monkeypatch.setenv("IRON_AGENT_APPROVAL_MODE", "enabled")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def build_pre_execute_payload(
    *,
    request_id: str,
    tool_name: str,
    capability_profile: str = "onlyread",
    risk_level: str = "medium",
    tool_args: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "envelope": {
            "request_id": request_id,
            "hook_name": "pre_execute",
        },
        "assistant": {
            "assistant_id": "assistant-1",
            "capability_profile": capability_profile,
        },
        "session": {
            "session_id": "session-1",
            "run_id": "run-1",
            "channel": "cli",
        },
        "tool": {
            "name": tool_name,
            "args": tool_args or {"path": "README.md"},
            "source": "core",
        },
        "risk": {
            "level": risk_level,
            "reason": "integration test",
            "tags": ["test"],
        },
        "metadata": {
            "origin": "pytest",
        },
    }


def assert_dangerous_requires_approval_response(body: dict[str, object]) -> None:
    assert body["decision"] == "pending_approval"
    assert body["reason"] == "tool requires approval"
    assert body["capability_profile"] == "dangerous"
    assert body["approval"] is not None
    assert body["metadata"]["policy_source"] == "profile_approval"


def read_audit_lines_eventually(path) -> list[str]:
    deadline = time.perf_counter() + 1.0
    while time.perf_counter() < deadline:
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()
            if lines:
                return lines
        time.sleep(0.01)
    return path.read_text(encoding="utf-8").splitlines()


def test_pre_execute_canonical_endpoint_allows_permitted_tool() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/v1/hooks/pre-execute",
        json=build_pre_execute_payload(
            request_id="req-allow",
            tool_name="read",
        ),
    )

    assert response.status_code == 200
    assert response.json() == {
        "protocol_version": "2026-04-03",
        "request_id": "req-allow",
        "decision": "allow",
        "reason": "tool allowed by capability profile",
        "capability_profile": "onlyread",
        "fail_closed": True,
        "approval": None,
        "metadata": {},
        "tool_args": None,
    }


def test_pre_execute_denies_tool_outside_profile() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/v1/hooks/pre-execute",
        json=build_pre_execute_payload(
            request_id="req-deny",
            tool_name="write",
            capability_profile="onlyread",
        ),
    )

    assert response.status_code == 200
    assert response.json() == {
        "protocol_version": "2026-04-03",
        "request_id": "req-deny",
        "decision": "deny",
        "reason": "当前能力配置为「onlyread」，不支持该操作",
        "capability_profile": "onlyread",
        "fail_closed": True,
        "approval": None,
        "metadata": {},
        "tool_args": None,
    }


def test_legacy_security_check_endpoint_requires_dangerous_profile_approval() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/v1/security/check",
        json=build_pre_execute_payload(
            request_id="req-approval",
            tool_name="exec",
            capability_profile="dangerous",
        ),
    )

    body = response.json()
    assert response.status_code == 200
    assert body["protocol_version"] == "2026-04-03"
    assert body["request_id"] == "req-approval"
    assert_dangerous_requires_approval_response(body)
    assert body["tool_args"] is None


def test_approval_status_endpoint_returns_resolved_decision() -> None:
    client = TestClient(app)
    from src.approval.service import ApprovalService

    approval = ApprovalService().create_request(
        "exec",
        assistant_id="assistant-1",
        capability_profile="dangerous",
        approval_scope="user",
    )
    resolved = ApprovalService().resolve_request(approval.id, "allow")
    assert resolved is not None

    status_response = client.get(f"/api/v1/approvals/{approval.id}")

    assert status_response.status_code == 200
    assert status_response.json()["approval_id"] == approval.id
    assert status_response.json()["status"] == "approved"
    assert status_response.json()["resolution"] == "allow"


def test_pre_execute_persistent_delete_requires_approval() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/v1/hooks/pre-execute",
        json=build_pre_execute_payload(
            request_id="req-persistent-delete",
            tool_name="exec",
            capability_profile="dangerous",
            tool_args={"command": "rm persistent/report.txt"},
        ),
    )

    body = response.json()
    assert response.status_code == 200
    assert_dangerous_requires_approval_response(body)


def test_pre_execute_chained_persistent_delete_requires_approval() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/v1/hooks/pre-execute",
        json=build_pre_execute_payload(
            request_id="req-persistent-delete-chained",
            tool_name="exec",
            capability_profile="dangerous",
            tool_args={
                "command": (
                    "cd /workspace && "
                    "rm -f persistent/report.txt && "
                    "test ! -e persistent/report.txt"
                )
            },
        ),
    )

    body = response.json()
    assert response.status_code == 200
    assert_dangerous_requires_approval_response(body)


def test_pre_execute_temp_workspace_exec_requires_approval() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/v1/hooks/pre-execute",
        json=build_pre_execute_payload(
            request_id="req-temp-exec",
            tool_name="exec",
            capability_profile="dangerous",
            tool_args={"command": "ls temp"},
        ),
    )

    body = response.json()
    assert response.status_code == 200
    assert_dangerous_requires_approval_response(body)


def test_pre_execute_temp_workspace_delete_requires_approval() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/v1/hooks/pre-execute",
        json=build_pre_execute_payload(
            request_id="req-temp-delete",
            tool_name="exec",
            capability_profile="dangerous",
            tool_args={"command": "rm -f /workspace/temp/demo.txt"},
        ),
    )

    body = response.json()
    assert response.status_code == 200
    assert_dangerous_requires_approval_response(body)


def test_pre_execute_temp_workspace_delete_without_force_requires_approval() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/v1/hooks/pre-execute",
        json=build_pre_execute_payload(
            request_id="req-temp-delete-without-force",
            tool_name="exec",
            capability_profile="dangerous",
            tool_args={"command": "rm /workspace/temp/hello_world.txt"},
        ),
    )

    body = response.json()
    assert response.status_code == 200
    assert_dangerous_requires_approval_response(body)


def test_pre_execute_high_risk_temp_workspace_delete_requires_approval() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/v1/hooks/pre-execute",
        json=build_pre_execute_payload(
            request_id="req-temp-delete-high-risk",
            tool_name="exec",
            capability_profile="dangerous",
            risk_level="high",
            tool_args={"command": "rm -f /workspace/temp/demo.txt"},
        ),
    )

    body = response.json()
    assert response.status_code == 200
    assert_dangerous_requires_approval_response(body)


def test_pre_execute_high_risk_network_command_targeting_temp_requires_approval(
) -> None:
    client = TestClient(app)

    response = client.post(
        "/api/v1/hooks/pre-execute",
        json=build_pre_execute_payload(
            request_id="req-temp-network-high-risk",
            tool_name="exec",
            capability_profile="dangerous",
            risk_level="high",
            tool_args={"command": "curl -o /workspace/temp/demo.txt https://example.com/demo.txt"},
        ),
    )

    body = response.json()
    assert response.status_code == 200
    assert_dangerous_requires_approval_response(body)


def test_pre_execute_denies_sensitive_workspace_write() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/v1/hooks/pre-execute",
        json=build_pre_execute_payload(
            request_id="req-sensitive-write",
            tool_name="write",
            capability_profile="dangerous",
            tool_args={"path": "AGENTS.md"},
        ),
    )

    body = response.json()
    assert response.status_code == 200
    assert body["decision"] == "deny"
    assert body["reason"] == "sensitive path is read-only"
    assert body["metadata"]["path_class"] == "workspace_sensitive"


def test_pre_execute_returns_rewritten_tool_args_for_auto_placed_outputs(
    tmp_path,
    monkeypatch,
) -> None:
    assistants_store_path = tmp_path / "assistants" / "assistants.json"
    workspace_root = tmp_path / "workspaces" / "assistant-1"
    monkeypatch.setenv("IRON_AGENT_ASSISTANTS_STORE_PATH", str(assistants_store_path))
    get_settings.cache_clear()

    assistants_store_path.parent.mkdir(parents=True, exist_ok=True)
    workspace_root.mkdir(parents=True, exist_ok=True)
    assistants_store_path.write_text(
        json.dumps(
            {
                "assistants": [
                    {
                        "assistant_id": "assistant-1",
                        "name": "Placement Demo",
                        "capability_profile": "dangerous",
                        "workspace_config": {
                            "selection_mode": "default_root",
                            "root_id": "default",
                            "workspace_slug": "assistant-1",
                            "auto_place_outputs": True,
                        },
                        "runtime": {
                            "workspace_root": str(workspace_root),
                            "workspace_root_id": "default",
                            "workspace_slug": "assistant-1",
                            "status": "running",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    original_assistant_service = routes.engine.assistant_service
    routes.engine.assistant_service = AssistantService(store_path=assistants_store_path)

    client = TestClient(app)
    try:
        response = client.post(
            "/api/v1/hooks/pre-execute",
            json=build_pre_execute_payload(
                request_id="req-autoplace",
                tool_name="write",
                capability_profile="dangerous",
                tool_args={"path": "report.docx", "content": "demo"},
            ),
        )

        body = response.json()
        assert response.status_code == 200
        assert body["decision"] == "allow"
        assert body["approval"] is None
        assert body["tool_args"] == {
            "path": "persistent/report.docx",
            "content": "demo",
        }
        assert body["metadata"]["workspace_placement"]["effective_path"] == "persistent/report.docx"
    finally:
        routes.engine.assistant_service = original_assistant_service
        get_settings.cache_clear()


def test_post_execute_endpoint_writes_audit_log(tmp_path, monkeypatch) -> None:
    audit_log_path = tmp_path / "audit" / "audit.jsonl"
    monkeypatch.setenv("IRON_AGENT_AUDIT_LOG_PATH", str(audit_log_path))
    monkeypatch.setenv("IRON_AGENT_STORAGE_BACKEND", "json")
    get_settings.cache_clear()

    client = TestClient(app)
    response = client.post(
        "/api/v1/hooks/post-execute",
        json={
            "envelope": {
                "request_id": "req-audit",
                "hook_name": "post_execute",
            },
            "assistant": {
                "assistant_id": "assistant-1",
                "capability_profile": "onlywrite",
            },
            "session": {
                "session_id": "session-1",
                "run_id": "run-1",
                "channel": "cli",
            },
            "tool": {
                "name": "read",
                "args": {"path": "README.md"},
                "source": "core",
            },
            "result": {
                "success": True,
                "duration_ms": 42,
                "output_summary": {"lines": 1},
            },
            "decision": "allow",
            "metadata": {
                "origin": "pytest",
        },
            },
    )

    assert response.status_code == 200
    assert response.json() == {
        "protocol_version": "2026-04-03",
        "request_id": "req-audit",
        "status": "ok",
    }

    lines = audit_log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["envelope"]["request_id"] == "req-audit"
    assert record["tool"]["name"] == "read"
    assert record["result"]["success"] is True

    get_settings.cache_clear()


def test_pre_execute_approval_bypass_writes_audit_log(tmp_path, monkeypatch) -> None:
    audit_log_path = tmp_path / "audit" / "audit.jsonl"
    monkeypatch.setenv("IRON_AGENT_AUDIT_LOG_PATH", str(audit_log_path))
    monkeypatch.setenv("IRON_AGENT_STORAGE_BACKEND", "json")
    monkeypatch.setenv("IRON_AGENT_APPROVAL_MODE", "disabled")
    get_settings.cache_clear()

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/hooks/pre-execute",
            json=build_pre_execute_payload(
                request_id="req-bypass-audit",
                tool_name="exec",
                capability_profile="dangerous",
                risk_level="high",
                tool_args={"command": "echo ok"},
            ),
        )

        body = response.json()
        assert response.status_code == 200
        assert body["decision"] == "allow"
        assert body["metadata"]["approval_bypassed"] is True

        lines = read_audit_lines_eventually(audit_log_path)
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["envelope"]["request_id"] == "req-bypass-audit"
    assert record["envelope"]["hook_name"] == "pre_execute"
    assert record["operation_type"] == "approval.bypassed"
    assert record["decision"] == "allow"
    assert record["result"]["success"] is True
    assert record["result"]["status"] == "approval_bypassed"
    assert record["metadata"]["approval_mode"] == "disabled"
    assert record["metadata"]["approval_bypassed"] is True
    assert record["metadata"]["original_reason"] == "tool requires approval"

    get_settings.cache_clear()


def test_pre_execute_approval_bypass_audit_does_not_block_decision(
    tmp_path,
    monkeypatch,
) -> None:
    class SlowAuditRepository:
        async def aappend_event(self, payload):
            await asyncio.sleep(0.3)

    monkeypatch.setenv("IRON_AGENT_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("IRON_AGENT_STORAGE_BACKEND", "json")
    monkeypatch.setenv("IRON_AGENT_APPROVAL_MODE", "disabled")
    monkeypatch.setattr(routes, "get_audit_repository", lambda: SlowAuditRepository())
    get_settings.cache_clear()

    client = TestClient(app)
    started_at = time.perf_counter()
    response = client.post(
        "/api/v1/hooks/pre-execute",
        json=build_pre_execute_payload(
            request_id="req-bypass-audit-nonblocking",
            tool_name="exec",
            capability_profile="dangerous",
            risk_level="high",
            tool_args={"command": "echo ok"},
        ),
    )
    elapsed = time.perf_counter() - started_at

    assert response.status_code == 200
    assert response.json()["decision"] == "allow"
    assert elapsed < 0.2

    get_settings.cache_clear()


def test_post_execute_endpoint_syncs_workspace_outputs_without_listing(
    tmp_path,
    monkeypatch,
) -> None:
    audit_log_path = tmp_path / "audit" / "audit.jsonl"
    workspace_root = tmp_path / "workspaces"
    files_store = tmp_path / "workspace-files" / "files.json"
    events_store = tmp_path / "workspace-files" / "events.json"
    push_store = tmp_path / "workspace-files" / "push.json"
    output_path = workspace_root / "assistant-1" / "persistent" / "reports" / "summary.txt"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("hook output", encoding="utf-8")

    monkeypatch.setenv("IRON_AGENT_STORAGE_BACKEND", "json")
    monkeypatch.setenv("IRON_AGENT_AUDIT_LOG_PATH", str(audit_log_path))
    monkeypatch.setenv("IRON_AGENT_ASSISTANTS_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("IRON_AGENT_WORKSPACE_FILES_STORE_PATH", str(files_store))
    monkeypatch.setenv("IRON_AGENT_WORKSPACE_FILE_EVENTS_STORE_PATH", str(events_store))
    monkeypatch.setenv("IRON_AGENT_WORKSPACE_PUSH_REQUESTS_STORE_PATH", str(push_store))
    monkeypatch.setenv("IRON_AGENT_WORKSPACE_OBJECT_STORAGE_BACKEND", "local")
    monkeypatch.delenv("IRON_AGENT_COS_BUCKET", raising=False)
    monkeypatch.delenv("IRON_AGENT_COS_REGION", raising=False)
    monkeypatch.delenv("IRON_AGENT_COS_ENDPOINT", raising=False)
    monkeypatch.delenv("IRON_AGENT_COS_SECRET_ID", raising=False)
    monkeypatch.delenv("IRON_AGENT_COS_SECRET_KEY", raising=False)
    get_settings.cache_clear()

    client = TestClient(app)
    response = client.post(
        "/api/v1/hooks/post-execute",
        json={
            "envelope": {
                "request_id": "req-sync",
                "hook_name": "post_execute",
            },
            "assistant": {
                "assistant_id": "assistant-1",
                "capability_profile": "dangerous",
            },
            "session": {
                "session_id": "session-1",
                "run_id": "run-1",
                "channel": "cli",
            },
            "tool": {
                "name": "exec",
                "args": {"command": "python export.py"},
                "source": "core",
            },
            "result": {
                "success": True,
                "duration_ms": 84,
                "output_summary": {"files_written": 1},
            },
            "decision": "allow",
            "metadata": {
                "origin": "pytest",
        },
            },
    )

    assert response.status_code == 200

    service = WorkspaceFileService()
    files = service.repository.list_files(assistant_id="assistant-1", bucket="persistent")
    assert len(files) == 1
    assert files[0].filename == "reports/summary.txt"
    # PR-A: scan should not rewrite assistant_result back to workspace-scan.
    assert files[0].source_type == "assistant_result"

    get_settings.cache_clear()


def test_assistant_endpoint_returns_latest_profile(tmp_path, monkeypatch) -> None:
    assistants_store_path = tmp_path / "assistants" / "assistants.json"
    monkeypatch.setenv("IRON_AGENT_STORAGE_BACKEND", "json")
    monkeypatch.setenv("IRON_AGENT_ASSISTANTS_STORE_PATH", str(assistants_store_path))
    monkeypatch.setenv("IRON_AGENT_ASSISTANTS_RUNTIME_ROOT", str(tmp_path / "runtimes"))
    get_settings.cache_clear()

    assistants_store_path.parent.mkdir(parents=True, exist_ok=True)
    assistants_store_path.write_text(
        json.dumps(
            {
                "assistants": [
                    {
                        "assistant_id": "demo-main",
                        "name": "Demo Main Assistant",
                        "capability_profile": "onlywrite",
                        "notes": "Latest demo profile",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    client = TestClient(app)
    response = client.get("/api/v1/assistants/demo-main")

    assert response.status_code == 200
    body = response.json()
    assert body["assistant_id"] == "demo-main"
    assert body["capability_profile"] == "onlywrite"

    get_settings.cache_clear()


def test_assistant_endpoint_syncs_runtime_view(monkeypatch) -> None:
    stale = AssistantRecord(
        assistant_id="demo-main",
        name="Demo Main Assistant",
        runtime=AssistantRuntimeRecord(status="starting"),
    )
    synced = stale.model_copy(
        update={
            "runtime": AssistantRuntimeRecord(
                status="running",
                gateway_url="http://10.244.144.101:18789",
            )
        }
    )

    class FakeAssistantService:
        async def aget_assistant(self, assistant_id: str) -> AssistantRecord | None:
            assert assistant_id == "demo-main"
            return stale

        async def async_runtime_views(
            self,
            assistants: list[AssistantRecord] | None = None,
        ) -> tuple[list[AssistantRecord], dict[str, object], int]:
            assert assistants == [stale]
            return [synced], {}, 1

    monkeypatch.setattr(routes, "AssistantService", FakeAssistantService)

    client = TestClient(app)
    response = client.get("/api/v1/assistants/demo-main")

    assert response.status_code == 200
    body = response.json()
    assert body["assistant_id"] == "demo-main"
    assert body["runtime"]["status"] == "running"
    assert body["runtime"]["gateway_url"] == "http://10.244.144.101:18789"
