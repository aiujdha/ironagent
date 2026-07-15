from pathlib import Path
import re

_DISPLAY_PREFIX_ALIASES: tuple[tuple[str, str], ...] = (
    ("/home/node/.ironagent/workspace", "SPDBPanda 工作空间"),
    ("~/.ironagent", "SPDBPanda 认证目录"),
)


def _replace_path_segment(value: str, segment: str, replacement: str) -> str:
    pattern = rf"(?:(?<=^)|(?<=[/\\\\])){re.escape(segment)}(?=(?:[/\\\\]|$))"
    return re.sub(pattern, replacement, value)


def format_display_path(value: str | Path) -> str:
    text = str(value)
    for prefix, display in _DISPLAY_PREFIX_ALIASES:
        if text == prefix:
            return display
        if text.startswith(f"{prefix}/") or text.startswith(f"{prefix}\\"):
            return f"{display}{text[len(prefix):]}"

    text = text.replace("ironagent.json", "SPDBPanda 运行配置")
    text = _replace_path_segment(text, ".ironagent", "SPDBPanda")
    text = _replace_path_segment(text, "ironagent-main", "SPDBPanda")
    return text
