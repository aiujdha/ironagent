#!/usr/bin/env python3
"""Create an IronAgent user and assistant through the admin API."""

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

    def detail(self) -> str:
        if isinstance(self.body, dict):
            return str(self.body.get("detail") or self.body)
        return self.body


def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def _json_request(
    *,
    base_url: str,
    method: str,
    path: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if token:
        headers["X-IronAgent-Internal-Token"] = token
        headers["X-Admin-Token"] = token

    request = Request(url, data=body, headers=headers, method=method)
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


def _create_or_update_user(args: argparse.Namespace) -> dict[str, Any]:
    payload = {
        "user_id": args.user_id,
        "display_name": args.display_name or args.user_id,
        "department": args.department,
        "employee_id": args.employee_id,
        "role": args.role,
    }
    try:
        result = _json_request(
            base_url=args.admin_base,
            method="POST",
            path="/api/v1/admin/users",
            token=args.token,
            payload=payload,
        )
        result["action"] = "created"
        return result
    except ApiError as exc:
        if exc.status != 409:
            raise

    result = _json_request(
        base_url=args.admin_base,
        method="PUT",
        path=f"/api/v1/admin/users/{quote(args.user_id, safe='')}",
        token=args.token,
        payload=payload,
    )
    result["action"] = "updated"
    return result


def _bind_existing_assistant(args: argparse.Namespace) -> dict[str, Any]:
    payload = {
        "assistant_id": args.assistant_id,
        "alias": args.assistant_name or args.assistant_id,
        "source_system": "create-user-and-assistant-script",
    }
    result = _json_request(
        base_url=args.admin_base,
        method="POST",
        path=f"/api/v1/admin/users/{quote(args.user_id, safe='')}/assistants/bind",
        token=args.token,
        payload=payload,
    )
    result["action"] = "bound_existing"
    return result


def _create_assistant(args: argparse.Namespace) -> dict[str, Any]:
    payload = {
        "assistant_id": args.assistant_id,
        "name": args.assistant_name or args.assistant_id,
        "capability_profile": args.capability_profile,
        "user_id": args.user_id,
        "notes": args.notes,
        "model_id": args.model_id,
        "primary_model": args.primary_model,
        "provider_id": args.provider_id,
        "provider_base_url": args.provider_base_url,
        "provider_api_key": args.provider_api_key,
        "workspace_slug": args.workspace_slug,
    }
    try:
        result = _json_request(
            base_url=args.admin_base,
            method="POST",
            path="/api/v1/admin/assistants",
            token=args.token,
            payload=payload,
        )
        result["action"] = "created"
        return result
    except ApiError as exc:
        detail = exc.detail()
        if exc.status == 409 and detail.startswith("assistant id already exists"):
            return _bind_existing_assistant(args)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create or update an IronAgent user, then create and bind an assistant.",
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
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--display-name", default="")
    parser.add_argument("--department", default="")
    parser.add_argument("--employee-id", default="")
    parser.add_argument("--role", default="user", choices=("user", "admin", "developer"))
    parser.add_argument("--assistant-id", required=True)
    parser.add_argument("--assistant-name", "--name", dest="assistant_name", default="")
    parser.add_argument(
        "--capability-profile",
        default=_env_first("IRON_AGENT_ASSISTANT_CAPABILITY_PROFILE", default="chat"),
    )
    parser.add_argument("--notes", default="")
    parser.add_argument(
        "--model-id",
        default=_env_first("IRON_AGENT_ASSISTANT_MODEL_ID", "MODEL_ID"),
        help="Model registry ID. If set, admin resolves this model and ignores direct provider fields.",
    )
    parser.add_argument(
        "--primary-model",
        "--model-name",
        dest="primary_model",
        default=_env_first("IRON_AGENT_ASSISTANT_PRIMARY_MODEL"),
        help="Primary model name, for example minimax-m2.7.",
    )
    parser.add_argument(
        "--provider-id",
        "--model-provider",
        "--provider",
        dest="provider_id",
        default=_env_first("IRON_AGENT_ASSISTANT_PROVIDER_ID"),
        help="Model provider/vendor ID, for example minimax or openai.",
    )
    parser.add_argument(
        "--provider-base-url",
        "--model-base-url",
        "--model-url",
        dest="provider_base_url",
        default=_env_first("IRON_AGENT_ASSISTANT_PROVIDER_BASE_URL"),
        help="Provider API base URL.",
    )
    parser.add_argument(
        "--provider-api-key",
        "--model-api-key",
        "--apikey",
        dest="provider_api_key",
        default=_env_first(
            "IRON_AGENT_ASSISTANT_PROVIDER_API_KEY",
            "DASHSCOPE_API_KEY",
            "OPENAI_API_KEY",
        ),
        help="Provider API key. The value is sent to the admin API but is not printed by this script.",
    )
    parser.add_argument("--workspace-slug", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        user = _create_or_update_user(args)
        assistant = _create_assistant(args)
    except ApiError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
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
                "user": user,
                "assistant": assistant,
            },
            ensure_ascii=False,
            indent=2,
    )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
