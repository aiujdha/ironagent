"""Helpers for assistant-scoped workspace object-storage keys."""

from __future__ import annotations

import re

_UNSAFE_SEGMENT_RE = re.compile(r"[\\/:*?\"<>|]+")
_WHITESPACE_RE = re.compile(r"\s+")
_DASH_RE = re.compile(r"-{2,}")


def _normalize_storage_segment(value: str, *, fallback: str) -> str:
    normalized = _UNSAFE_SEGMENT_RE.sub("-", value.strip())
    normalized = _WHITESPACE_RE.sub("-", normalized)
    normalized = _DASH_RE.sub("-", normalized).strip(" .-_").casefold()
    if normalized:
        return normalized

    fallback_normalized = re.sub(r"[^0-9a-zA-Z_-]+", "-", fallback.strip())
    fallback_normalized = _DASH_RE.sub("-", fallback_normalized).strip(" .-_").casefold()
    return fallback_normalized or "assistant"


def build_assistant_storage_scope(*, assistant_id: str, assistant_name: str = "") -> str:
    id_segment = _normalize_storage_segment(assistant_id, fallback="assistant")
    name_segment = _normalize_storage_segment(assistant_name, fallback=id_segment)
    if name_segment == id_segment:
        return id_segment
    return f"{name_segment}-{id_segment}"


def build_workspace_object_key(
    *,
    assistant_id: str,
    assistant_name: str = "",
    bucket: str,
    relative_name: str,
) -> str:
    normalized_name = relative_name.replace("\\", "/").lstrip("/")
    scope = build_assistant_storage_scope(
        assistant_id=assistant_id,
        assistant_name=assistant_name,
    )
    return f"{scope}/{bucket}/{normalized_name}"
