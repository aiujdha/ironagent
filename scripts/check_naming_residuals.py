#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}
TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".css",
    ".env",
    ".example",
    ".go",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".lock",
    ".md",
    ".py",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}


def _join(*parts: str) -> str:
    return "".join(parts)


LEGACY_WORD = _join("c", "law")
LEGACY_DEPLOYMENT = _join("open", "clow")
LEGACY_HYPHEN_NAME = _join("iron", "-", "agent")
LEGACY_SPACE_NAME = _join("iron", " ", "agent")
RESIDUAL_RE = re.compile(
    rf"{LEGACY_WORD}|{LEGACY_DEPLOYMENT}|{LEGACY_HYPHEN_NAME}|{LEGACY_SPACE_NAME}",
    re.IGNORECASE,
)
RESIDUAL_BYTES = tuple(
    token.encode("utf-8")
    for token in (
        LEGACY_WORD,
        LEGACY_DEPLOYMENT,
        LEGACY_HYPHEN_NAME,
        LEGACY_SPACE_NAME,
    )
)


def _has_residual_bytes(raw: bytes) -> bool:
    lowered = raw.lower()
    return any(token in lowered for token in RESIDUAL_BYTES)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check unclassified IronAgent naming residuals."
    )
    parser.add_argument(
        "--report-allowed",
        action="store_true",
        help="Accepted for older automation; no compatibility allowlist is used.",
    )
    return parser.parse_args()


def main() -> int:
    _parse_args()
    residuals: list[str] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT).as_posix()
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if RESIDUAL_RE.search(relative):
            residuals.append(f"{relative}:0:<path>")
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if not _has_residual_bytes(raw):
            continue
        if path.suffix not in TEXT_SUFFIXES:
            residuals.append(f"{relative}:0:<binary>")
            continue
        try:
            lines = raw.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            residuals.append(f"{relative}:0:<binary>")
            continue
        matched_line = False
        for lineno, line in enumerate(lines, start=1):
            if not RESIDUAL_RE.search(line):
                continue
            matched_line = True
            residuals.append(f"{relative}:{lineno}:{line.strip()}")
        if not matched_line:
            residuals.append(f"{relative}:0:<binary>")

    if residuals:
        print("Unclassified naming residuals:")
        print("\n".join(residuals))
        return 1
    print("No unclassified naming residuals.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
