from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Any

from src.assistants.models import AssistantRecord
from src.shared.workspace_roots import (
    matching_workspace_container_root,
)
from src.workspace.models import PlacementTarget, WorkspacePlacementResult

SENSITIVE_WORKSPACE_BASENAMES = {".env", "USER.md", "AGENTS.md"}
PERSISTENT_EXTENSIONS = {
    ".csv",
    ".doc",
    ".docx",
    ".html",
    ".json",
    ".md",
    ".ods",
    ".pdf",
    ".ppt",
    ".pptx",
    ".rtf",
    ".txt",
    ".xls",
    ".xlsx",
    ".yaml",
    ".yml",
}
TEMP_EXTENSIONS = {
    ".bak",
    ".cache",
    ".diff",
    ".log",
    ".part",
    ".patch",
    ".temp",
    ".tmp",
    ".trace",
}
PERSISTENT_HINT_KEYWORDS = {
    "deliverable",
    "export",
    "final",
    "invoice",
    "report",
    "result",
    "summary",
}
TEMP_HINT_KEYWORDS = {
    "cache",
    "debug",
    "draft",
    "intermediate",
    "log",
    "scratch",
    "smoke",
    "staging",
    "temp",
    "tmp",
    "trace",
}
WINDOWS_ABSOLUTE_PATH = re.compile(r"^[a-zA-Z]:[/\\]")
APPLY_PATCH_ADD_FILE_MARKER = "*** Add File: "
APPLY_PATCH_MOVE_TO_MARKER = "*** Move to: "


def maybe_auto_place_tool_args(
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    assistant: AssistantRecord | None,
) -> WorkspacePlacementResult | None:
    if assistant is None or not assistant.workspace_config.auto_place_outputs:
        return None
    if not assistant.runtime.workspace_root:
        return None

    normalized_tool_name = tool_name.strip().lower()
    if normalized_tool_name in {"write", "edit"}:
        return _maybe_rewrite_write_like_path(tool_args, assistant)
    if normalized_tool_name == "apply_patch":
        return _maybe_rewrite_apply_patch(tool_args, assistant)
    return None


def _maybe_rewrite_write_like_path(
    tool_args: dict[str, Any],
    assistant: AssistantRecord,
) -> WorkspacePlacementResult | None:
    path_value = _extract_path_value(tool_args)
    if path_value is None:
        return None

    candidate = _candidate_workspace_relative_path(path_value, assistant)
    if candidate is None:
        return None

    placement_target, placement_reason = _infer_placement_target(candidate)
    effective_path = _placed_path(candidate, assistant, placement_target)
    if effective_path == candidate:
        return None

    rewritten = dict(tool_args)
    for key in ("path", "file_path", "filePath", "file", "target_path", "targetPath"):
        if isinstance(rewritten.get(key), str) and rewritten.get(key) == path_value:
            rewritten[key] = effective_path

    return WorkspacePlacementResult(
        tool_args=rewritten,
        metadata={
            "workspace_placement": {
                "placement_target": placement_target,
                "placement_reason": placement_reason,
                "original_path": path_value,
                "effective_path": effective_path,
            }
        },
    )


def _maybe_rewrite_apply_patch(
    tool_args: dict[str, Any],
    assistant: AssistantRecord,
) -> WorkspacePlacementResult | None:
    raw_input = tool_args.get("input")
    if not isinstance(raw_input, str) or not raw_input.strip():
        return None

    lines = raw_input.splitlines(keepends=True)
    rewritten_lines: list[str] = []
    rewrites: list[dict[str, object]] = []
    changed = False

    for line in lines:
        rewritten_line = line
        for marker in (APPLY_PATCH_ADD_FILE_MARKER, APPLY_PATCH_MOVE_TO_MARKER):
            if not line.startswith(marker):
                continue
            suffix = line[len(marker) :]
            path_text = suffix.rstrip("\r\n")
            candidate = _candidate_workspace_relative_path(path_text, assistant)
            if candidate is None:
                break

            placement_target, placement_reason = _infer_placement_target(candidate)
            effective_path = _placed_path(candidate, assistant, placement_target)
            if effective_path == candidate:
                break

            line_ending = suffix[len(path_text) :]
            rewritten_line = f"{marker}{effective_path}{line_ending}"
            rewrites.append(
                {
                    "placement_target": placement_target,
                    "placement_reason": placement_reason,
                    "original_path": path_text,
                    "effective_path": effective_path,
                }
            )
            changed = True
            break
        rewritten_lines.append(rewritten_line)

    if not changed:
        return None

    rewritten = dict(tool_args)
    rewritten["input"] = "".join(rewritten_lines)
    metadata: dict[str, object] = {"workspace_placement": {"rewrites": rewrites}}
    if len(rewrites) == 1:
        metadata["workspace_placement"] = rewrites[0]

    return WorkspacePlacementResult(
        tool_args=rewritten,
        metadata=metadata,
    )


def _extract_path_value(tool_args: dict[str, Any]) -> str | None:
    for key in ("path", "file_path", "filePath", "file", "target_path", "targetPath"):
        value = tool_args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _candidate_workspace_relative_path(
    raw_path: str,
    assistant: AssistantRecord,
) -> str | None:
    trimmed = raw_path.strip()
    if not trimmed or WINDOWS_ABSOLUTE_PATH.match(trimmed):
        return None

    workspace_root = Path(assistant.runtime.workspace_root or "").resolve(strict=False)
    temp_dir = assistant.runtime.temp_dir or "temp"
    persistent_dir = assistant.runtime.persistent_dir or "persistent"

    workspace_container_root = matching_workspace_container_root(trimmed)
    if workspace_container_root is not None:
        relative = trimmed.removeprefix(workspace_container_root).lstrip("/")
    elif trimmed.startswith("/"):
        resolved = Path(trimmed).resolve(strict=False)
        try:
            relative = resolved.relative_to(workspace_root).as_posix()
        except ValueError:
            return None
    else:
        relative = trimmed.replace("\\", "/")

    normalized = posixpath.normpath(relative)
    if normalized in {"", "."} or normalized == ".." or normalized.startswith("../"):
        return None

    basename = Path(normalized).name
    if basename in SENSITIVE_WORKSPACE_BASENAMES:
        return None
    if normalized == "skills" or normalized.startswith("skills/"):
        return None
    if normalized == temp_dir or normalized.startswith(f"{temp_dir}/"):
        return None
    if normalized == persistent_dir or normalized.startswith(f"{persistent_dir}/"):
        return None

    if (workspace_root / Path(normalized)).exists():
        return None

    return normalized
def _infer_placement_target(relative_path: str) -> tuple[PlacementTarget, str]:
    basename = Path(relative_path).name
    lower_name = basename.lower()
    suffix = Path(relative_path).suffix.lower()

    for keyword in sorted(TEMP_HINT_KEYWORDS):
        if keyword in lower_name:
            return "temp", f"filename matched temporary keyword '{keyword}'"
    if suffix in TEMP_EXTENSIONS:
        return "temp", f"extension '{suffix}' defaults to temporary storage"

    for keyword in sorted(PERSISTENT_HINT_KEYWORDS):
        if keyword in lower_name:
            return "persistent", f"filename matched persistent keyword '{keyword}'"
    if suffix in PERSISTENT_EXTENSIONS:
        return "persistent", f"extension '{suffix}' defaults to persistent storage"

    return "temp", "ambiguous output defaults to temp storage"


def _placed_path(
    relative_path: str,
    assistant: AssistantRecord,
    placement_target: PlacementTarget,
) -> str:
    temp_dir = assistant.runtime.temp_dir or "temp"
    persistent_dir = assistant.runtime.persistent_dir or "persistent"
    base_dir = temp_dir if placement_target == "temp" else persistent_dir
    return posixpath.join(base_dir, relative_path)
