from pathlib import Path

from src.security.capability import load_capability_profile
from src.security.whitelist import tool_is_allowed, tool_requires_approval


def test_whitelist_rules() -> None:
    writer = load_capability_profile(Path("config/capabilities/onlywrite.yaml"))
    work_no_exec = load_capability_profile(Path("config/capabilities/work_no_exec.yaml"))
    dangerous = load_capability_profile(Path("config/capabilities/dangerous.yaml"))
    assert tool_is_allowed(writer, "write")
    assert tool_is_allowed(work_no_exec, "write")
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
        assert tool_is_allowed(work_no_exec, tool_name) is False
    assert tool_is_allowed(dangerous, "exec") is False
    assert tool_is_allowed(dangerous, "process") is False
    assert tool_requires_approval(dangerous, "exec") is True
    assert tool_requires_approval(dangerous, "process") is True
    assert work_no_exec.approval_tools == set()
    assert dangerous.approval_tools == {
        "exec",
        "process",
        "cron",
        "sessions_spawn",
        "subagents",
        "code_execution",
    }
