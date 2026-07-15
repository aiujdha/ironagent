import pytest

from src.approval.service import ApprovalService
from src.assistants.models import AssistantRecord
from src.assistants.service import AssistantService
from src.runtime.models import AssistantRuntimeRecord, AssistantWorkspaceConfig
from src.security_service.decision import SecurityDecisionEngine
from src.security_service.schemas import (
    AssistantContext,
    HookEnvelope,
    PreExecuteHookRequest,
    RiskContext,
    ToolContext,
)
from src.shared.config import get_settings


@pytest.fixture(autouse=True)
def approval_mode_enabled(monkeypatch):
    monkeypatch.setenv("IRON_AGENT_APPROVAL_MODE", "enabled")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def build_pre_execute_request(
    *,
    capability_profile: str,
    tool_name: str,
    risk_level: str,
    tool_args: dict[str, object] | None = None,
) -> PreExecuteHookRequest:
    return PreExecuteHookRequest(
        envelope=HookEnvelope(hook_name="pre_execute"),
        assistant=AssistantContext(capability_profile=capability_profile),
        tool=ToolContext(name=tool_name, args=tool_args or {}),
        risk=RiskContext(level=risk_level),
    )


def assert_dangerous_profile_requires_approval(result) -> None:
    assert result.decision == "pending_approval"
    assert result.reason in {"tool requires approval", "该操作需要您的确认"}
    assert result.capability_profile == "dangerous"
    assert result.approval is not None
    assert result.metadata["policy_source"] == "profile_approval"


def assert_work_no_exec_profile_deny(result) -> None:
    assert result.decision == "deny"
    assert result.reason == "当前能力配置为「work_no_exec」，不支持该操作"
    assert result.capability_profile == "work_no_exec"
    assert result.approval is None


def test_pruned_tool_is_denied() -> None:
    engine = SecurityDecisionEngine()
    result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="dangerous",
            tool_name="system_reset",
            risk_level="critical",
    )
        )
    assert result.decision == "deny"
    assert result.reason == "该工具已被永久禁用"


def test_p0_hard_deny_precedes_dangerous_allowlist() -> None:
    engine = SecurityDecisionEngine()
    result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="dangerous",
            tool_name="exec",
            risk_level="low",
            tool_args={"command": "cat /root/.ssh/id_rsa"},
    )
        )

    assert result.decision == "deny"
    assert result.reason == "secret path is not accessible"
    assert result.metadata["policy_source"] == "path_guard"


def test_work_no_exec_denies_high_risk_tools() -> None:
    engine = SecurityDecisionEngine()

    for tool_name in (
        "exec",
        "process",
        "code_execution",
        "cron",
        "sessions_send",
        "sessions_spawn",
        "sessions_history",
        "subagents",
    ):
        result = engine.evaluate(
            build_pre_execute_request(
                capability_profile="work_no_exec",
                tool_name=tool_name,
                risk_level="low",
                tool_args={"command": "cat /workspace/temp/input.txt"},
            )
        )

        assert_work_no_exec_profile_deny(result)


def test_work_no_exec_allows_read_and_write_tools() -> None:
    engine = SecurityDecisionEngine()
    read_result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="work_no_exec",
            tool_name="read",
            risk_level="low",
            tool_args={"path": "/workspace/temp/input.txt"},
        )
    )
    write_result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="work_no_exec",
            tool_name="write",
            risk_level="low",
            tool_args={"path": "/workspace/temp/output.txt", "content": "ok"},
        )
    )

    assert read_result.decision == "allow"
    assert read_result.capability_profile == "work_no_exec"
    assert write_result.decision == "allow"
    assert write_result.capability_profile == "work_no_exec"


def test_approval_disabled_allows_profile_approval_tools_without_creating_request(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("IRON_AGENT_APPROVAL_MODE", "disabled")
    get_settings.cache_clear()
    try:
        approval_service = ApprovalService(store_path=tmp_path / "approvals.json")
        engine = SecurityDecisionEngine(approval_service=approval_service)

        result = engine.evaluate(
            build_pre_execute_request(
                capability_profile="dangerous",
                tool_name="exec",
                risk_level="high",
                tool_args={"command": "echo ok"},
            )
        )

        assert result.decision == "allow"
        assert result.reason == "approval disabled by configuration"
        assert result.approval is None
        assert result.metadata["policy_source"] == "profile_approval"
        assert result.metadata["approval_mode"] == "disabled"
        assert approval_service.list_requests() == []
    finally:
        get_settings.cache_clear()


def test_assistant_store_profile_overrides_request_profile(tmp_path) -> None:
    assistant_service = AssistantService(store_path=tmp_path / "assistants.json")
    assistant_service.upsert_assistant(
        assistant_id="assistant-dangerous",
        name="Dangerous Demo",
        capability_profile="dangerous",
    )
    approval_service = ApprovalService(store_path=tmp_path / "approvals.json")
    engine = SecurityDecisionEngine(
        approval_service=approval_service,
        assistant_service=assistant_service,
    )

    result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="chat",
            tool_name="exec",
            risk_level="low",
        ).model_copy(
            update={
                "assistant": AssistantContext(
                    assistant_id="assistant-dangerous",
                    capability_profile="chat",
                )
            }
    )
        )

    assert_dangerous_profile_requires_approval(result)


def test_sensitive_workspace_file_write_is_denied() -> None:
    engine = SecurityDecisionEngine()

    result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="dangerous",
            tool_name="write",
            risk_level="low",
            tool_args={"path": "AGENTS.md"},
    )
        )

    assert result.decision == "deny"
    assert result.reason == "sensitive path is read-only"
    assert result.metadata["path_class"] == "workspace_sensitive"


def test_container_sensitive_path_write_is_denied() -> None:
    engine = SecurityDecisionEngine()

    result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="dangerous",
            tool_name="write",
            risk_level="low",
            tool_args={"path": "/etc/hosts"},
    )
        )

    assert result.decision == "deny"
    assert result.reason == "sensitive path is read-only"
    assert result.metadata["path_class"] == "container_sensitive"
    assert result.metadata["normalized_path"] == "/etc/hosts"


def test_container_sensitive_path_write_is_denied_after_lexical_normalization() -> None:
    engine = SecurityDecisionEngine()

    result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="dangerous",
            tool_name="write",
            risk_level="low",
            tool_args={"path": "/etc/../etc/hosts"},
    )
        )

    assert result.decision == "deny"
    assert result.reason == "sensitive path is read-only"
    assert result.metadata["path_class"] == "container_sensitive"
    assert result.metadata["normalized_path"] == "/etc/hosts"


def test_persistent_delete_command_requires_approval() -> None:
    """dangerous 中 exec 默认需要审批，因此删除命令会触发 profile 审批。"""
    assistant = AssistantRecord(
        assistant_id="test-assistant",
        name="Test Assistant",
        capability_profile="dangerous",
        runtime=AssistantRuntimeRecord(
            workspace_root="/app/ironagent/workspaces/test-assistant",
            workspace_root_id="default",
            workspace_slug="test-assistant",
            status="running",
        ),
    )
    engine = SecurityDecisionEngine(
        assistant_service=_StaticAssistantService(assistant),
    )

    result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="dangerous",
            tool_name="exec",
            risk_level="low",
            tool_args={"command": "rm persistent/demo.txt"},
        ).model_copy(
            update={
                "assistant": AssistantContext(
                    assistant_id="test-assistant",
                    capability_profile="dangerous",
                )
            }
    )
        )

    assert_dangerous_profile_requires_approval(result)


def test_chained_persistent_delete_command_requires_approval() -> None:
    """dangerous 中 exec 默认需要审批，因此链式删除命令会触发 profile 审批。"""
    assistant = AssistantRecord(
        assistant_id="test-assistant",
        name="Test Assistant",
        capability_profile="dangerous",
        runtime=AssistantRuntimeRecord(
            workspace_root="/app/ironagent/workspaces/test-assistant",
            workspace_root_id="default",
            workspace_slug="test-assistant",
            status="running",
        ),
    )
    engine = SecurityDecisionEngine(
        assistant_service=_StaticAssistantService(assistant),
    )

    result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="dangerous",
            tool_name="exec",
            risk_level="low",
            tool_args={
                "command": (
                    "cd /workspace && rm -f persistent/demo.txt && test ! -e persistent/demo.txt"
                )
            },
        ).model_copy(
            update={
                "assistant": AssistantContext(
                    assistant_id="test-assistant",
                    capability_profile="dangerous",
                )
            }
    )
        )

    assert_dangerous_profile_requires_approval(result)


def test_persistent_delete_inside_app_workspace_requires_approval() -> None:
    """dangerous 中 exec 默认需要审批，因此 app workspace 删除命令会触发 profile 审批。"""
    assistant = AssistantRecord(
        assistant_id="demo-approval",
        name="Demo Approval",
        capability_profile="dangerous",
        runtime=AssistantRuntimeRecord(
            workspace_root="/app/ironagent/workspaces/demo-approval",
            workspace_root_id="default",
            workspace_slug="demo-approval",
            status="running",
        ),
    )
    engine = SecurityDecisionEngine(
        assistant_service=_StaticAssistantService(assistant),
    )

    result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="dangerous",
            tool_name="exec",
            risk_level="low",
            tool_args={"command": "rm -f persistent/demo.txt"},
        ).model_copy(
            update={
                "assistant": AssistantContext(
                    assistant_id="demo-approval",
                    capability_profile="dangerous",
                )
            }
    )
        )

    assert_dangerous_profile_requires_approval(result)


def test_third_party_command_requires_approval_under_dangerous_profile() -> None:
    engine = SecurityDecisionEngine()

    result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="dangerous",
            tool_name="exec",
            risk_level="low",
            tool_args={"command": "curl https://example.com"},
    )
        )

    assert_dangerous_profile_requires_approval(result)


def test_chained_plugin_install_requires_approval_under_dangerous_profile() -> None:
    engine = SecurityDecisionEngine()

    result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="dangerous",
            tool_name="exec",
            risk_level="low",
            tool_args={"command": ("cd /workspace && ironagent plugins add demo/plugin")},
    )
        )

    assert_dangerous_profile_requires_approval(result)


def test_temp_workspace_command_requires_approval_under_dangerous_profile() -> None:
    """
    按照统一 PDP 架构，命令工具也需要检查 Profile 授权。

    temp 目录不再特殊绕过；exec 是否执行只由 dangerous approval_tools 审批决定。
    """
    engine = SecurityDecisionEngine()

    result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="dangerous",
            tool_name="exec",
            risk_level="low",
            tool_args={"command": "ls temp"},
    )
        )

    assert_dangerous_profile_requires_approval(result)


def test_temp_workspace_write_command_requires_approval_under_dangerous_profile() -> None:
    engine = SecurityDecisionEngine()

    result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="dangerous",
            tool_name="exec",
            risk_level="low",
            tool_args={"command": "bash -lc 'cp /tmp/demo.txt temp/demo.txt'"},
    )
        )

    assert_dangerous_profile_requires_approval(result)


def test_temp_workspace_delete_command_requires_approval_under_dangerous_profile() -> None:
    engine = SecurityDecisionEngine()

    result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="dangerous",
            tool_name="exec",
            risk_level="low",
            tool_args={"command": "rm -f temp/demo.txt"},
    )
        )

    assert_dangerous_profile_requires_approval(result)


def test_absolute_temp_delete_command_requires_approval_under_dangerous_profile() -> None:
    engine = SecurityDecisionEngine()

    result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="dangerous",
            tool_name="exec",
            risk_level="low",
            tool_args={"command": "rm -f /workspace/temp/demo.txt"},
    )
        )

    assert_dangerous_profile_requires_approval(result)


def test_high_risk_temp_workspace_delete_command_requires_approval_under_dangerous_profile(
) -> None:
    engine = SecurityDecisionEngine()

    result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="dangerous",
            tool_name="exec",
            risk_level="high",
            tool_args={"command": "rm -f /workspace/temp/demo.txt"},
    )
        )

    assert_dangerous_profile_requires_approval(result)


def test_high_risk_network_command_targeting_temp_requires_approval_under_dangerous_profile(
) -> None:
    engine = SecurityDecisionEngine()

    result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="dangerous",
            tool_name="exec",
            risk_level="high",
            tool_args={"command": "curl -o /workspace/temp/demo.txt https://example.com/demo.txt"},
    )
        )

    assert_dangerous_profile_requires_approval(result)


def test_temp_workspace_read_command_with_stderr_redirect_requires_approval_under_dangerous_profile(
) -> None:
    engine = SecurityDecisionEngine()

    result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="dangerous",
            tool_name="exec",
            risk_level="high",
            tool_args={"command": "ls /workspace/temp/text.txt 2>&1"},
    )
        )

    assert_dangerous_profile_requires_approval(result)


def test_temp_workspace_delete_redirect_fallback_requires_approval_for_dangerous_profile() -> None:
    engine = SecurityDecisionEngine()

    result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="dangerous",
            tool_name="exec",
            risk_level="high",
            tool_args={
                "command": 'rm /workspace/temp/text.txt 2>&1 && echo "deleted" || echo "not found"'
            },
    )
        )

    assert_dangerous_profile_requires_approval(result)


def test_workspace_root_command_requires_approval_under_dangerous_profile() -> None:
    engine = SecurityDecisionEngine()

    result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="dangerous",
            tool_name="exec",
            risk_level="high",
            tool_args={"command": "ls /workspace/notes.txt 2>&1"},
    )
        )

    assert_dangerous_profile_requires_approval(result)


def test_workspace_other_directory_command_requires_approval_under_dangerous_profile() -> None:
    engine = SecurityDecisionEngine()

    result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="dangerous",
            tool_name="exec",
            risk_level="high",
            tool_args={"command": "rm -f /workspace/artifacts/demo.txt"},
    )
        )

    assert_dangerous_profile_requires_approval(result)


def test_write_auto_places_persistent_document_in_workspace(tmp_path) -> None:
    """
    测试自动放置仍生效，审批只由 approval_tools 决定。

    write 在 dangerous.allowed_tools 中，因此即使自动放到 persistent 也放行。
    """
    assistant = AssistantRecord(
        assistant_id="assistant-persistent",
        name="Persistent Demo",
        capability_profile="dangerous",
        workspace_config=AssistantWorkspaceConfig(auto_place_outputs=True),
        runtime=AssistantRuntimeRecord(
            workspace_root=str(tmp_path / "assistant-persistent"),
            workspace_root_id="default",
            workspace_slug="assistant-persistent",
            status="running",
        ),
    )
    engine = SecurityDecisionEngine(
        assistant_service=_StaticAssistantService(assistant),
    )

    result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="dangerous",
            tool_name="write",
            risk_level="low",
            tool_args={"path": "report.docx", "content": "demo"},
        ).model_copy(
            update={
                "assistant": AssistantContext(
                    assistant_id="assistant-persistent",
                    capability_profile="dangerous",
                )
            }
    )
        )

    assert result.decision == "allow"
    assert result.tool_args == {"path": "persistent/report.docx", "content": "demo"}
    assert result.metadata["workspace_placement"] == {
        "placement_target": "persistent",
        "placement_reason": "filename matched persistent keyword 'report'",
        "original_path": "report.docx",
        "effective_path": "persistent/report.docx",
    }


def test_write_auto_places_temp_smoke_output(tmp_path) -> None:
    assistant = AssistantRecord(
        assistant_id="assistant-temp",
        name="Temp Demo",
        capability_profile="dangerous",
        workspace_config=AssistantWorkspaceConfig(auto_place_outputs=True),
        runtime=AssistantRuntimeRecord(
            workspace_root=str(tmp_path / "assistant-temp"),
            workspace_root_id="default",
            workspace_slug="assistant-temp",
            status="running",
        ),
    )
    engine = SecurityDecisionEngine(
        assistant_service=_StaticAssistantService(assistant),
    )

    result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="dangerous",
            tool_name="write",
            risk_level="low",
            tool_args={"path": "smoke-output.json", "content": "{}"},
        ).model_copy(
            update={
                "assistant": AssistantContext(
                    assistant_id="assistant-temp",
                    capability_profile="dangerous",
                )
            }
    )
        )

    assert result.decision == "allow"
    assert result.tool_args == {"path": "temp/smoke-output.json", "content": "{}"}
    assert result.metadata["workspace_placement"] == {
        "placement_target": "temp",
        "placement_reason": "filename matched temporary keyword 'smoke'",
        "original_path": "smoke-output.json",
        "effective_path": "temp/smoke-output.json",
    }


def test_sensitive_workspace_file_is_not_rewritten_before_denial(tmp_path) -> None:
    assistant = AssistantRecord(
        assistant_id="assistant-sensitive",
        name="Sensitive Demo",
        capability_profile="dangerous",
        workspace_config=AssistantWorkspaceConfig(auto_place_outputs=True),
        runtime=AssistantRuntimeRecord(
            workspace_root=str(tmp_path / "assistant-sensitive"),
            workspace_root_id="default",
            workspace_slug="assistant-sensitive",
            status="running",
        ),
    )
    engine = SecurityDecisionEngine(
        assistant_service=_StaticAssistantService(assistant),
    )

    result = engine.evaluate(
        build_pre_execute_request(
            capability_profile="dangerous",
            tool_name="write",
            risk_level="low",
            tool_args={"path": "AGENTS.md", "content": "blocked"},
        ).model_copy(
            update={
                "assistant": AssistantContext(
                    assistant_id="assistant-sensitive",
                    capability_profile="dangerous",
                )
            }
    )
        )

    assert result.decision == "deny"
    assert result.reason == "sensitive path is read-only"
    assert result.tool_args is None
    assert "workspace_placement" not in result.metadata


class _StaticAssistantService:
    def __init__(self, assistant: AssistantRecord) -> None:
        self.assistant = assistant

    def get_assistant(self, assistant_id: str) -> AssistantRecord | None:
        if assistant_id == self.assistant.assistant_id:
            return self.assistant
        return None
