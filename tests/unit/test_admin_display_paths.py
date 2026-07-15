from pathlib import Path

from src.admin.display_paths import format_display_path


def test_format_display_path_aliases_workspace_root_prefix() -> None:
    assert (
        format_display_path("/home/node/.ironagent/workspace/main/skills")
        == "SPDBPanda 工作空间/main/skills"
    )


def test_format_display_path_aliases_hidden_directory_and_config_name() -> None:
    assert (
        format_display_path(Path("/Users/demo/.ironagent/agents/main/agent/ironagent.json"))
        == "/Users/demo/SPDBPanda/agents/main/agent/SPDBPanda 运行配置"
    )


def test_format_display_path_aliases_windows_style_hidden_directory() -> None:
    assert (
        format_display_path(r"C:\Users\demo\.ironagent\agents\main\agent\ironagent.json")
        == r"C:\Users\demo\SPDBPanda\agents\main\agent\SPDBPanda 运行配置"
    )
