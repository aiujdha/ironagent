"""Security and path rules for workspace file operations."""

from __future__ import annotations

import posixpath
import re

FORBIDDEN_BASENAMES = {name.upper() for name in ("AGENTS.md", "USER.md", ".env")}
_PATH_TRAVERSAL_RE = re.compile(r"(^|[\\/])\.\.($|[\\/])")


class FileSecurityError(Exception):
    """Raised when a workspace file path violates security rules."""


def validate_filename(filename: str) -> None:
    """Reject forbidden filenames, traversal attempts, and absolute paths."""
    if not filename or not filename.strip():
        raise FileSecurityError("filename must not be empty")
    basename = posixpath.basename(filename.replace("\\", "/"))
    if basename.upper() in FORBIDDEN_BASENAMES:
        raise FileSecurityError(f"writing to {basename!r} is forbidden")
    if _PATH_TRAVERSAL_RE.search(filename):
        raise FileSecurityError("path traversal is not allowed")
    if filename.startswith("/") or (len(filename) >= 2 and filename[1] == ":"):
        raise FileSecurityError("absolute paths are not allowed")