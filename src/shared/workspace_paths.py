from __future__ import annotations

from src.shared.config import get_settings


def runtime_workspace_path(*parts: str) -> str:
    """Return the path as seen from inside an assistant runtime."""
    mount_path = get_settings().iron_agent_workspace_mount_path.strip() or "/workspace"
    normalized_root = mount_path.rstrip("/") or "/"
    normalized_parts: list[str] = []
    for part in parts:
        value = str(part).replace("\\", "/").strip("/")
        if value:
            normalized_parts.append(value)
    if not normalized_parts:
        return normalized_root
    if normalized_root == "/":
        return "/" + "/".join(normalized_parts)
    return "/".join([normalized_root, *normalized_parts])
