"""Unit tests for the active_path_key normalization.

`active_path_key` backs the `uq_workspace_files_active_path` unique index. Migration
025's deduplication MUST group active rows by this same normalized key — otherwise two
active rows whose raw filenames differ but normalize to the same path (e.g. 'a.txt' vs
'/a.txt', 'dir\\b.txt' vs 'dir/b.txt') compute the same key and `CREATE UNIQUE INDEX`
crashes with ERROR 1062. These tests pin the normalization (the bug's root cause) and
that the migration helper stays byte-for-byte equivalent to the runtime function.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from src.workspace_files.database_models import workspace_file_active_path_key as key


def test_basic_key_is_deterministic_64_hex() -> None:
    k = key("asst", "input", "a.txt")
    assert k is not None
    assert len(k) == 64
    assert k == key("asst", "input", "a.txt")


def test_deleted_or_empty_yields_none() -> None:
    assert key("asst", "input", "a.txt", deleted=True) is None
    assert key("", "input", "a.txt") is None
    assert key("asst", "", "a.txt") is None
    assert key("asst", "input", "") is None
    assert key("asst", "input", "   ") is None  # all whitespace
    assert key("asst", "input", "/") is None  # only leading slash -> empty
    assert key("asst", "input", "\t\n") is None  # only whitespace


def test_normalization_collisions_share_one_key() -> None:
    # Raw filenames differ but normalize identically (\\ -> /, strip all whitespace,
    # strip leading /), so they MUST compute the same key — this is exactly why 025's
    # dedup has to group by the normalized key, not the raw filename.
    base = key("asst", "input", "a.txt")
    assert key("asst", "input", "/a.txt") == base  # leading slash
    assert key("asst", "input", "//a.txt") == base  # multiple leading slashes
    assert key("asst", "input", " a.txt ") == base  # surrounding spaces
    assert key("asst", "input", "\ta.txt\n") == base  # tab / newline
    assert key("asst", "input", " a.txt") == base  # NBSP
    assert key("asst", "input", "　a.txt") == base  # CJK full-width space

    dir_base = key("asst", "input", "dir/b.txt")
    assert key("asst", "input", "dir\\b.txt") == dir_base  # backslash -> slash


def test_distinct_paths_differ() -> None:
    assert key("asst", "input", "a.txt") != key("asst", "input", "b.txt")
    assert key("asst", "input", "a.txt") != key("asst2", "input", "a.txt")
    assert key("asst", "input", "a.txt") != key("asst", "output", "a.txt")
    # only the LEADING slash is stripped; internal separators are significant
    assert key("asst", "input", "x/a.txt") != key("asst", "input", "a.txt")


def test_migration_025_helper_matches_runtime() -> None:
    # Migration 025's _active_path_key must stay byte-for-byte equivalent to the runtime
    # function, or backfilled keys won't match keys the app computes at upsert time.
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "025_workspace_files_active_path_key.py"
    )
    spec = importlib.util.spec_from_file_location("mig025", path)
    assert spec is not None and spec.loader is not None
    mig025 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig025)
    for filename in ["a.txt", "/a.txt", "dir\\b.txt", " x ", "中文.txt", "\ta.txt", ""]:
        assert mig025._active_path_key("asst", "input", filename) == key("asst", "input", filename)
        assert mig025._active_path_key("asst", "input", filename, deleted=True) is None
