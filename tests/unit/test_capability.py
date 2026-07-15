from pathlib import Path

from src.security.capability import load_capability_profile


def test_load_capability_profile() -> None:
    profile = load_capability_profile(Path("config/capabilities/onlyread.yaml"))
    assert profile.level == 1
    assert "read" in profile.allowed_tools


def test_work_no_exec_profile_denies_shell_tools() -> None:
    profile = load_capability_profile(Path("config/capabilities/work_no_exec.yaml"))

    assert profile.level == 2
    assert {"read", "write", "edit", "apply_patch"} <= profile.allowed_tools
    assert {
        "exec",
        "process",
        "code_execution",
        "cron",
        "sessions_send",
        "sessions_spawn",
        "sessions_history",
        "subagents",
    }.isdisjoint(profile.allowed_tools)
    assert profile.approval_tools == set()


def test_work_no_exec_deployment_seed_matches_canonical_profile() -> None:
    canonical = Path("config/capabilities/work_no_exec.yaml").read_text(encoding="utf-8")
    deployment_seed = Path(
        "deployment/kustomize/ironagent/base/files/capabilities/work_no_exec.yaml"
    ).read_text(encoding="utf-8")

    assert deployment_seed == canonical
