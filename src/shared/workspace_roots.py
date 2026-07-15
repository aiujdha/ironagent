from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from src.shared.config import Settings, get_settings

WORKSPACE_CONTAINER_ROOT = "/workspace"
LEGACY_WORKSPACE_CONTAINER_ROOT = "/home/node/.ironagent/workspace"


class WorkspaceRootEntry(BaseModel):
    id: str
    label: str
    host_path: Path


def list_workspace_roots(settings: Settings | None = None) -> list[WorkspaceRootEntry]:
    effective_settings = settings or get_settings()
    default_root_id = effective_settings.default_workspace_root_id.strip() or "default"
    entries: dict[str, WorkspaceRootEntry] = {
        default_root_id: WorkspaceRootEntry(
            id=default_root_id,
            label="默认工作空间",
            host_path=effective_settings.assistants_workspace_root,
        )
    }

    raw = effective_settings.allowed_workspace_roots_json.strip()
    if not raw:
        return list(entries.values())

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("IRON_AGENT_ALLOWED_WORKSPACE_ROOTS_JSON 不是合法 JSON") from exc

    if not isinstance(payload, list):
        raise ValueError("IRON_AGENT_ALLOWED_WORKSPACE_ROOTS_JSON 必须是数组")

    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("工作空间根目录配置项必须是对象")
        root_id = str(item.get("id", "")).strip()
        label = str(item.get("label", "")).strip()
        host_path = str(item.get("host_path", "")).strip()
        if not root_id:
            raise ValueError("工作空间根目录配置缺少 id")
        if not host_path:
            raise ValueError(f"工作空间根目录配置 {root_id} 缺少 host_path")
        entries[root_id] = WorkspaceRootEntry(
            id=root_id,
            label=label or root_id,
            host_path=Path(host_path),
        )

    return list(entries.values())


def get_workspace_root(root_id: str, settings: Settings | None = None) -> WorkspaceRootEntry:
    trimmed = root_id.strip()
    if not trimmed:
        raise ValueError("workspace root id 不能为空")
    for entry in list_workspace_roots(settings):
        if entry.id == trimmed:
            return entry
    raise ValueError(f"未找到工作空间根目录配置：{trimmed}")


def normalize_workspace_slug(raw: str, *, fallback: str) -> str:
    candidate = raw.strip() or fallback.strip()
    if not candidate:
        raise ValueError("workspace_slug 不能为空")
    if candidate in {".", ".."}:
        raise ValueError("workspace_slug 不能是 . 或 ..")
    if "/" in candidate or "\\" in candidate:
        raise ValueError("workspace_slug 不能包含路径分隔符")
    return candidate


def matching_workspace_container_root(path: str) -> str | None:
    for candidate in (WORKSPACE_CONTAINER_ROOT, LEGACY_WORKSPACE_CONTAINER_ROOT):
        if path == candidate or path.startswith(f"{candidate}/"):
            return candidate
    return None
