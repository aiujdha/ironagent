"""
统一安全策略决策点 (Policy Decision Point, PDP)

所有请求必须收集齐所有上下文，经过统一的矩阵评估后，才能得出结论。

优先级（从高到低）：
- P0: 系统级绝对黑名单（直接 Deny，不可绕过）
- P3: Capability Profile 授权白名单（approval_tools 才进入审批）
"""

from __future__ import annotations

import posixpath
import shlex
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Literal

from src.assistants.models import AssistantRecord
from src.security_service.schemas import PreExecuteHookRequest
from src.shared.workspace_roots import matching_workspace_container_root

ApprovalScope = Literal["user", "admin"]
ActionClass = Literal["read", "write", "delete", "execute", "network", "install"]
PathClass = Literal[
    "workspace_root",
    "workspace_temp",
    "workspace_persistent",
    "workspace_other",
    "workspace_sensitive",
    "container_sensitive",
    "container_secret",
    "other",
]
DecisionLiteral = Literal["allow", "deny", "pending_approval"]
RiskLevel = Literal["low", "medium", "high", "critical"]


# ============================================================================
# 常量定义
# ============================================================================

SENSITIVE_WORKSPACE_BASENAMES = {".env", "USER.md", "AGENTS.md"}
CONTAINER_SECRET_PREFIXES = ("/root/.ssh/",)
CONTAINER_SENSITIVE_PREFIXES = ("/etc/", "/usr/local/bin/", "/app/")

# 工具分类
FILE_READ_TOOLS = {"read"}
FILE_WRITE_TOOLS = {"write", "edit", "apply_patch"}
FILE_DELETE_TOOLS = {"delete", "remove", "unlink"}
COMMAND_TOOLS = {"exec", "process", "code_execution"}

# Shell 命令分类
DELETE_COMMANDS = {"rm", "unlink", "del", "remove-item"}
WRITE_COMMANDS = {"cp", "mv", "install", "tee", "dd"}
READ_PATH_COMMANDS = {"ls", "cat", "head", "tail", "find", "du", "stat", "file"}
COMMAND_SEPARATORS = {"&&", "||", ";", "|", "&"}
SHELL_WRAPPER_COMMANDS = {"bash", "sh", "zsh"}
REDIRECT_PATTERNS = {">>", ">", "1>", "2>"}
PATH_VALUE_OPTIONS = {"-o", "--output", "-O"}
REDIRECT_OPERATORS = {
    ">",
    ">>",
    "1>",
    "1>>",
    "2>",
    "2>>",
    "<",
    "0<",
    "<<",
    "<<<",
}


# ============================================================================
# 优先级定义
# ============================================================================

class Priority(IntEnum):
    """策略优先级，数值越小优先级越高"""
    P0_BLACKLIST = 0      # 系统级黑名单
    P3_PROFILE_WHITELIST = 3 # Profile 授权白名单


# ============================================================================
# 数据模型
# ============================================================================

@dataclass(slots=True)
class PathAssessment:
    raw_path: str
    normalized_path: str
    path_class: PathClass


@dataclass(slots=True)
class PolicyOutcome:
    decision: DecisionLiteral
    reason: str
    priority: Priority
    metadata: dict[str, object] = field(default_factory=dict)
    approval_scope: ApprovalScope | None = None


@dataclass(slots=True)
class DecisionContext:
    """
    统一决策上下文，收集四维属性

    - Subject: Profile 类型
    - Action: 动作类型
    - Resource: 目标路径类型
    - Risk: 综合风险等级
    """
    tool_name: str
    profile_name: str
    action: ActionClass | None
    path_assessments: list[PathAssessment]
    risk_level: RiskLevel
    command: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


# ============================================================================
# 核心评估函数
# ============================================================================

def evaluate_request_policy(
    request: PreExecuteHookRequest,
    assistant: AssistantRecord | None = None,
    profile_name: str = "chat",
    risk_level: RiskLevel = "medium",
) -> PolicyOutcome | None:
    """
    统一的策略决策点 (PDP)

    按优先级从高到低评估：
    1. P0: 系统级黑名单（永久裁剪工具、Secret 路径等）
    2. P3: Profile 授权白名单由 decision.py 处理

    所有上下文必须收集完整，才能得出最终结论。
    """
    # 1. 收集统一决策上下文
    context = _build_decision_context(request, assistant, profile_name, risk_level)
    if context is None:
        return None

    # 2. P0: 系统级黑名单检查
    p0_outcome = _evaluate_p0_blacklist(context)
    if p0_outcome is not None:
        return p0_outcome

    # P0 未匹配，返回 None 让 decision.py 按 profile 的
    # allowed_tools / approval_tools 做最终授权。
    return None


def _build_decision_context(
    request: PreExecuteHookRequest,
    assistant: AssistantRecord | None,
    profile_name: str,
    risk_level: RiskLevel,
) -> DecisionContext | None:
    """构建统一的决策上下文"""
    tool_name = request.tool.name
    args = request.tool.args

    # 推断动作类型
    action = _infer_action(tool_name)

    # 如果是命令工具，从 args 中提取命令
    command = None
    if tool_name in COMMAND_TOOLS:
        command = _extract_command(args)

    # 提取并分类路径
    path_assessments = _extract_path_assessments(args, assistant)

    # 如果是命令工具，还需要分析命令中的路径
    if command:
        command_paths = _extract_paths_from_command(command, assistant)
        path_assessments.extend(command_paths)

    return DecisionContext(
        tool_name=tool_name,
        profile_name=profile_name,
        action=action,
        path_assessments=path_assessments,
        risk_level=risk_level,
        command=command,
        metadata={
            "request_tool_args": args,
        },
    )


# ============================================================================
# P0: 系统级绝对黑名单
# ============================================================================

def _evaluate_p0_blacklist(context: DecisionContext) -> PolicyOutcome | None:
    """
    P0: 系统级绝对黑名单

    直接 Deny，不可绕过：
    - 永久裁剪工具（由调用方处理）
    - Secret 路径访问（/root/.ssh/）
    - 敏感系统文件写入/删除（.env, /etc/）
    """
    # 1. Secret 路径访问
    for assessment in context.path_assessments:
        if assessment.path_class == "container_secret":
            return PolicyOutcome(
                decision="deny",
                reason="secret path is not accessible",
                priority=Priority.P0_BLACKLIST,
                metadata=_metadata_for(assessment, context.action or "read"),
            )

    # 2. 敏感系统文件写入/删除
    for assessment in context.path_assessments:
        if assessment.path_class == "workspace_sensitive" and context.action in {"write", "delete"}:
            return PolicyOutcome(
                decision="deny",
                reason="sensitive path is read-only",
                priority=Priority.P0_BLACKLIST,
                metadata=_metadata_for(assessment, context.action or "write"),
            )

        if assessment.path_class == "container_sensitive" and context.action in {"write", "delete"}:
            return PolicyOutcome(
                decision="deny",
                reason="sensitive path is read-only",
                priority=Priority.P0_BLACKLIST,
                metadata=_metadata_for(assessment, context.action or "write"),
            )

    return None


# ============================================================================
# 辅助函数
# ============================================================================

def _metadata_for(assessment: PathAssessment, action: ActionClass | str) -> dict[str, object]:
    return {
        "policy_source": "path_guard",
        "path": assessment.raw_path,
        "normalized_path": assessment.normalized_path,
        "path_class": assessment.path_class,
        "action_class": action,
    }


def _extract_path_assessments(
    args: dict[str, Any],
    assistant: AssistantRecord | None,
) -> list[PathAssessment]:
    paths: list[str] = []
    for key in ("path", "file_path", "filePath", "file", "target_path", "targetPath"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())
    values = args.get("paths")
    if isinstance(values, list):
        for item in values:
            if isinstance(item, str) and item.strip():
                paths.append(item.strip())
    return [_classify_path(path, assistant) for path in paths]


def _extract_paths_from_command(
    command: str,
    assistant: AssistantRecord | None,
) -> list[PathAssessment]:
    """从命令中提取目标路径（用于 persistent 保护）"""
    assessments: list[PathAssessment] = []
    tokens = _tokenize_command(command)

    # 提取删除目标
    for target in _extract_delete_targets(tokens):
        assessments.append(_classify_path(target, assistant))

    # 提取写入目标
    for target in _extract_write_targets(tokens):
        assessments.append(_classify_path(target, assistant))

    # 提取读取/列目录目标
    for target in _extract_read_targets(tokens):
        assessments.append(_classify_path(target, assistant))

    # 提取通过选项传入的目标路径（例如 curl -o /workspace/temp/a.txt）
    for target in _extract_option_path_targets(tokens):
        assessments.append(_classify_path(target, assistant))

    # 提取重定向目标
    for target in _has_redirect_to_persistent(command):
        assessments.append(_classify_path(target, assistant))

    return assessments


def _classify_path(path: str, assistant: AssistantRecord | None) -> PathAssessment:
    normalized = _normalize_path(path, assistant)
    pure = Path(normalized)
    basename = pure.name

    # 工作区逻辑路径（需要优先处理，以正确识别 workspace 内的 persistent/temp 等）
    logical = normalized
    workspace_container_root = matching_workspace_container_root(normalized)
    workspace_is_within = False
    if workspace_container_root is not None:
        logical = normalized.removeprefix(workspace_container_root).lstrip("/")
        workspace_is_within = True
    elif assistant and assistant.runtime.workspace_root:
        workspace_root = str(Path(assistant.runtime.workspace_root).resolve())
        if normalized.startswith(workspace_root):
            logical = normalized.removeprefix(workspace_root).lstrip("/")
            workspace_is_within = True

    # Secret 路径（只检查容器外路径）
    if not workspace_is_within and any(
        normalized == prefix[:-1] or normalized.startswith(prefix)
        for prefix in CONTAINER_SECRET_PREFIXES
    ):
        return PathAssessment(path, normalized, "container_secret")

    # 敏感文件名（工作区内外的 AGENTS.md 等）
    if basename in SENSITIVE_WORKSPACE_BASENAMES:
        return PathAssessment(path, normalized, "workspace_sensitive")

    # 工作区子目录（优先于容器敏感路径检查）
    if workspace_is_within:
        if logical == "temp" or logical.startswith("temp/"):
            return PathAssessment(path, normalized, "workspace_temp")
        if logical == "persistent" or logical.startswith("persistent/"):
            return PathAssessment(path, normalized, "workspace_persistent")
        if not logical or "/" not in logical:
            return PathAssessment(path, normalized, "workspace_root")
        # 工作区内但不是特殊目录
        return PathAssessment(path, normalized, "workspace_other")

    # 容器敏感路径（只检查容器外路径）
    if any(
        normalized == prefix[:-1] or normalized.startswith(prefix)
        for prefix in CONTAINER_SENSITIVE_PREFIXES
    ):
        return PathAssessment(path, normalized, "container_sensitive")

    # 相对路径模式检查（当 assistant 为 None 时，辅助识别工作区相对路径）
    # 例如: "persistent/a.txt", "temp/output.json"
    if not normalized.startswith("/") and not normalized.startswith("."):
        parts = normalized.split("/")
        if len(parts) >= 1:
            first_segment = parts[0]
            if first_segment == "persistent":
                return PathAssessment(path, normalized, "workspace_persistent")
            if first_segment == "temp":
                return PathAssessment(path, normalized, "workspace_temp")
            if first_segment in SENSITIVE_WORKSPACE_BASENAMES:
                return PathAssessment(path, normalized, "workspace_sensitive")

    return PathAssessment(path, normalized, "other")


def _normalize_path(path: str, assistant: AssistantRecord | None) -> str:
    raw = path.strip()
    if not raw:
        return raw
    if matching_workspace_container_root(raw) is not None:
        return _normalize_posix_absolute_path(raw)
    if raw.startswith("/"):
        return _normalize_posix_absolute_path(raw)
    if assistant and assistant.runtime.workspace_root:
        return str((Path(assistant.runtime.workspace_root) / raw).resolve(strict=False))
    return raw


def _normalize_posix_absolute_path(path: str) -> str:
    normalized = posixpath.normpath(path)
    return normalized if normalized.startswith("/") else f"/{normalized}"


def _infer_action(tool_name: str) -> ActionClass | None:
    if tool_name in FILE_READ_TOOLS:
        return "read"
    if tool_name in FILE_WRITE_TOOLS:
        return "write"
    if tool_name in FILE_DELETE_TOOLS:
        return "delete"
    if tool_name in COMMAND_TOOLS:
        return "execute"
    return None


def _extract_command(args: dict[str, Any]) -> str:
    for key in ("command", "cmd", "script"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _tokenize_command(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _extract_command_segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in COMMAND_SEPARATORS:
            if current:
                segments.extend(_unwrap_shell_command(current))
                current = []
            continue
        current.append(token)
    if current:
        segments.extend(_unwrap_shell_command(current))
    return [segment for segment in segments if segment]


def _unwrap_shell_command(tokens: list[str]) -> list[list[str]]:
    if not tokens:
        return []
    executable = tokens[0].lower()
    if executable not in SHELL_WRAPPER_COMMANDS:
        return [tokens]

    for index, token in enumerate(tokens[1:], start=1):
        if token in {"-c", "-lc", "-cl"} and index + 1 < len(tokens):
            nested_tokens = _tokenize_command(tokens[index + 1])
            return _extract_command_segments(nested_tokens)
    return [tokens]


def _extract_delete_targets(tokens: list[str]) -> list[str]:
    if not tokens:
        return []
    targets: list[str] = []
    for segment in _extract_command_segments(tokens):
        executable = segment[0].lower()
        if executable not in DELETE_COMMANDS:
            continue
        for token in _strip_shell_redirections(segment[1:]):
            if token.startswith("-"):
                continue
            targets.append(token)
    return targets


def _extract_write_targets(tokens: list[str]) -> list[str]:
    """提取命令写入的目标路径。"""
    if not tokens:
        return []
    targets: list[str] = []
    for segment in _extract_command_segments(tokens):
        executable = segment[0].lower()
        if executable not in WRITE_COMMANDS:
            continue
        positional = [
            token
            for token in _strip_shell_redirections(segment[1:])
            if not token.startswith("-")
        ]
        if positional:
            targets.append(positional[-1])
    return targets


def _extract_read_targets(tokens: list[str]) -> list[str]:
    if not tokens:
        return []
    targets: list[str] = []
    for segment in _extract_command_segments(tokens):
        executable = segment[0].lower()
        if executable not in READ_PATH_COMMANDS:
            continue
        for token in _strip_shell_redirections(segment[1:]):
            if token.startswith("-"):
                continue
            targets.append(token)
    return targets


def _strip_shell_redirections(tokens: list[str]) -> list[str]:
    cleaned: list[str] = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token in REDIRECT_OPERATORS:
            skip_next = True
            continue
        if _is_shell_redirection_token(token):
            continue
        cleaned.append(token)
    return cleaned


def _is_shell_redirection_token(token: str) -> bool:
    stripped = token.strip()
    if not stripped:
        return False
    if stripped in REDIRECT_OPERATORS:
        return True
    if stripped.startswith(("<<<", "<<", ">>", "1>>", "2>>", "1>", "2>", "0<", "<", ">")):
        return True
    return stripped.startswith(("0>&", "1>&", "2>&", "&>"))


def _extract_option_path_targets(tokens: list[str]) -> list[str]:
    if not tokens:
        return []
    targets: list[str] = []
    for segment in _extract_command_segments(tokens):
        for index, token in enumerate(segment[1:], start=1):
            if token in PATH_VALUE_OPTIONS and index + 1 < len(segment):
                candidate = segment[index + 1]
                if _looks_like_workspace_path(candidate):
                    targets.append(candidate)
                continue
            for option in PATH_VALUE_OPTIONS:
                if token.startswith(f"{option}="):
                    candidate = token.split("=", 1)[1]
                    if _looks_like_workspace_path(candidate):
                        targets.append(candidate)
    return targets


def _looks_like_workspace_path(token: str) -> bool:
    stripped = token.strip()
    if not stripped:
        return False
    return (
        stripped.startswith("/workspace/")
        or stripped == "/workspace"
        or stripped == "temp"
        or stripped.startswith("temp/")
        or stripped == "persistent"
        or stripped.startswith("persistent/")
    )


def _has_redirect_to_persistent(command: str) -> list[str]:
    """
    检查命令是否包含重定向到 persistent 目录的操作
    """
    if not command:
        return []
    targets: list[str] = []
    for pattern in REDIRECT_PATTERNS:
        idx = command.find(pattern)
        while idx != -1:
            rest = command[idx + len(pattern):].lstrip()
            if not rest:
                idx = command.find(pattern, idx + 1)
                continue
            end_chars = {" ", ";", "&", "|", "&&", "||"}
            end_idx = len(rest)
            for i, ch in enumerate(rest):
                if ch in end_chars:
                    end_idx = i
                    break
            target = rest[:end_idx].strip()
            if target:
                targets.append(target)
            idx = command.find(pattern, idx + 1)
    return targets
