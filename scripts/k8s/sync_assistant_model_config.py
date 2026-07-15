#!/usr/bin/env python3
"""Sync existing assistants to a model config through the admin API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen


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
    payload: dict[str, Any] | None = None,
    timeout: float,
) -> tuple[int, str]:
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
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except URLError as exc:
        return 0, str(exc.reason)


def _assistant_ids_from_cases(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("cases", payload if isinstance(payload, list) else [])
    ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        assistant_id = str(row.get("assistant_id") or row.get("assistantId") or "").strip()
        if assistant_id:
            ids.append(assistant_id)
    return ids


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync existing assistant runtime model config, then optionally restart.",
    )
    parser.add_argument(
        "--admin-base",
        default=_env_first("IRON_AGENT_ADMIN_BASE", "ADMIN_BASE", default="http://127.0.0.1:8501"),
    )
    parser.add_argument(
        "--token",
        default=_env_first(
            "IRON_AGENT_INTERNAL_SERVICE_TOKEN",
            "IRON_AGENT_ADMIN_TOKEN",
            "ADMIN_TOKEN",
            default="change-me",
        ),
    )
    parser.add_argument("--assistant-id", action="append", default=[])
    parser.add_argument("--cases-file", type=Path)
    parser.add_argument("--model-id", default="")
    parser.add_argument("--primary-model", default="")
    parser.add_argument("--provider-id", default="")
    parser.add_argument("--provider-base-url", default="")
    parser.add_argument(
        "--provider-api-key",
        default=_env_first("IRON_AGENT_ASSISTANT_PROVIDER_API_KEY", "DASHSCOPE_API_KEY"),
    )
    parser.add_argument("--no-provision-runtime", action="store_true")
    parser.add_argument("--no-restart", action="store_true")
    parser.add_argument("--timeout", type=float, default=90.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    assistant_ids = [item.strip() for item in args.assistant_id if item.strip()]
    if args.cases_file:
        assistant_ids.extend(_assistant_ids_from_cases(args.cases_file))
    assistant_ids = list(dict.fromkeys(assistant_ids))
    if not assistant_ids:
        print("No assistants supplied. Use --assistant-id or --cases-file.", file=sys.stderr)
        return 2
    if not args.model_id.strip() and not args.primary_model.strip():
        print("Set --model-id or --primary-model.", file=sys.stderr)
        return 2

    update_body = {
        "model_id": args.model_id.strip(),
        "primary_model": args.primary_model.strip(),
        "provider_id": args.provider_id.strip(),
        "provider_base_url": args.provider_base_url.strip(),
        "provider_api_key": args.provider_api_key.strip(),
        "provision_runtime": not args.no_provision_runtime,
    }
    failures = 0
    print(f"admin_base={args.admin_base}")
    print(f"assistant_count={len(assistant_ids)}")
    print(f"model_id={update_body['model_id'] or '<direct-provider>'}")
    print(f"provider_api_key_present={'yes' if update_body['provider_api_key'] else 'no'}")

    for assistant_id in assistant_ids:
        safe_id = quote(assistant_id, safe="")
        status, body = _request(
            base_url=args.admin_base,
            method="PUT",
            path=f"/api/v1/admin/assistants/{safe_id}/model-config",
            token=args.token,
            payload=update_body,
            timeout=args.timeout,
        )
        ok = 200 <= status < 300
        print(f"{assistant_id}\tmodel-config\t{status}\t{body[:240]}")
        if not ok:
            failures += 1
            continue
        if args.no_restart:
            continue
        status, body = _request(
            base_url=args.admin_base,
            method="POST",
            path=f"/api/v1/admin/assistants/{safe_id}/restart",
            token=args.token,
            timeout=args.timeout,
        )
        ok = 200 <= status < 300
        print(f"{assistant_id}\trestart\t{status}\t{body[:240]}")
        if not ok:
            failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
