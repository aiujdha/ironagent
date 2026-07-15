#!/usr/bin/env python3
"""Delete an IronAgent user through the admin API.

This calls DELETE /api/v1/admin/users/{user_id}. The backend removes the user
and that user's assistant bindings, but it does not delete assistant records.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen


DEFAULT_ADMIN_BASE = "http://127.0.0.1:8501"
DEFAULT_TOKEN = "change-me"


@dataclass(frozen=True)
class ApiError(Exception):
    status: int
    body: dict[str, Any] | str


def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def _request(
    *,
    base_url: str,
    method: str,
    path: str,
    token: str,
) -> dict[str, Any]:
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if token:
        headers["X-IronAgent-Internal-Token"] = token
        headers["X-Admin-Token"] = token
        headers["Authorization"] = f"Bearer {token}"

    request = Request(url, headers=headers, method=method)
    try:
        with urlopen(request, timeout=120) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed: dict[str, Any] | str = json.loads(raw) if raw else ""
        except json.JSONDecodeError:
            parsed = raw
        raise ApiError(exc.code, parsed) from exc
    except URLError as exc:
        raise SystemExit(f"admin API unreachable: {exc.reason}") from exc

    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"admin API returned non-json response: {raw}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit(f"admin API returned unexpected response: {parsed!r}")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete an IronAgent user and the user's assistant bindings.",
    )
    parser.add_argument(
        "--admin-base",
        default=_env_first("IRON_AGENT_ADMIN_BASE", "ADMIN_BASE", default=DEFAULT_ADMIN_BASE),
        help=f"Admin API base URL. Default: {DEFAULT_ADMIN_BASE}",
    )
    parser.add_argument(
        "--token",
        default=_env_first(
            "IRON_AGENT_INTERNAL_SERVICE_TOKEN",
            "IRON_AGENT_ADMIN_TOKEN",
            "ADMIN_TOKEN",
            default=DEFAULT_TOKEN,
        ),
        help="Admin/internal token. Defaults to env token or change-me.",
    )
    parser.add_argument(
        "--user-id",
        default=_env_first("USER_ID"),
        required=not bool(_env_first("USER_ID")),
        help="User ID to delete. Can also be set with USER_ID.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        default=_env_first("YES", "CONFIRM_DELETE_USER") in {"1", "true", "yes", "YES"},
        help="Actually perform the delete. Required unless --dry-run is set.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Only print the target request; do not delete.",
    )
    parser.add_argument(
        "--ignore-missing",
        action="store_true",
        default=False,
        help="Return success if the user is already missing.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    user_id = args.user_id.strip()
    path = f"/api/v1/admin/users/{quote(user_id, safe='')}"

    if args.dry_run:
        print(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": True,
                    "method": "DELETE",
                    "admin_base": args.admin_base,
                    "path": path,
                    "user_id": user_id,
                    "note": "backend deletes the user and user-assistant bindings, not assistants",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not args.yes:
        print(
            "Refusing to delete without --yes. Use --dry-run to inspect the target.",
            file=sys.stderr,
        )
        return 2

    try:
        result = _request(
            base_url=args.admin_base,
            method="DELETE",
            path=path,
            token=args.token,
        )
    except ApiError as exc:
        if exc.status == 404 and args.ignore_missing:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "user_id": user_id,
                        "deleted": False,
                        "missing": True,
                        "status": exc.status,
                        "response": exc.body,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        print(
            json.dumps(
                {
                    "ok": False,
                    "user_id": user_id,
                    "status": exc.status,
                    "error": exc.body,
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1

    print(
        json.dumps(
            {
                "ok": True,
                "user_id": user_id,
                "deleted": result.get("status") == "deleted",
                "response": result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
