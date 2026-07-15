#!/usr/bin/env python3
"""Delete an IronAgent assistant through admin API, then hard-delete DB records.

The admin endpoint performs lifecycle cleanup and soft-deletes the assistant.
This script can then remove the assistant row and direct assistant-scoped rows
from MySQL so test environments can be reset cleanly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlparse
from urllib.request import Request, urlopen


DEFAULT_ADMIN_BASE = "http://127.0.0.1:8501"
DEFAULT_TOKEN = "change-me"

DEFAULT_DB_DELETES: tuple[tuple[str, str], ...] = (
    ("user_assistant_bindings", "assistant_id"),
    ("gateway_runtime_heartbeats", "assistant_id"),
    ("gateway_runtimes", "assistant_id"),
    ("assistants", "assistant_id"),
)

PURGE_RELATED_DB_DELETES: tuple[tuple[str, str], ...] = (
    ("workspace_file_events", "assistant_id"),
    ("workspace_files", "assistant_id"),
    ("external_push_requests", "assistant_id"),
    ("approvals", "assistant_id"),
    ("shared_workspace_objects", "source_assistant_id"),
    ("collaboration_claims", "assistant_id"),
    ("collaboration_events", "actor_id"),
    ("collaboration_tasks", "source_assistant_id"),
    ("collaboration_tasks", "target_assistant_id"),
)


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
        with urlopen(request, timeout=180) as response:
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


def _redact_url(raw: str) -> str:
    if not raw:
        return ""
    parsed = urlparse(raw)
    if not parsed.password:
        return raw
    netloc = parsed.netloc.replace(f":{parsed.password}@", ":***@")
    return parsed._replace(netloc=netloc).geturl()


def _parse_mysql_url(raw: str) -> dict[str, Any]:
    parsed = urlparse(raw)
    if not parsed.scheme.startswith("mysql"):
        raise SystemExit(
            "Only MySQL URLs are supported for DB hard delete, for example "
            "mysql+pymysql://user:pass@host:3306/db"
        )
    database = parsed.path.lstrip("/")
    if not database:
        raise SystemExit("DB URL must include a database name")
    query: dict[str, str] = {}
    if parsed.query:
        for item in parsed.query.split("&"):
            if not item:
                continue
            key, _, value = item.partition("=")
            query[unquote(key)] = unquote(value)
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": database,
        "charset": query.get("charset", "utf8mb4"),
        "unix_socket": query.get("unix_socket"),
    }


def _table_exists(cursor: Any, table: str) -> bool:
    cursor.execute("SHOW TABLES LIKE %s", (table,))
    return cursor.fetchone() is not None


def _column_exists(cursor: Any, table: str, column: str) -> bool:
    cursor.execute(f"SHOW COLUMNS FROM `{table}` LIKE %s", (column,))
    return cursor.fetchone() is not None


def _delete_from_table(cursor: Any, table: str, column: str, assistant_id: str) -> int | None:
    if not _table_exists(cursor, table):
        return None
    if not _column_exists(cursor, table, column):
        return None
    cursor.execute(f"DELETE FROM `{table}` WHERE `{column}` = %s", (assistant_id,))
    return int(cursor.rowcount)


def hard_delete_db_records(
    *,
    db_url: str,
    assistant_id: str,
    purge_related: bool,
) -> list[dict[str, Any]]:
    try:
        import pymysql
    except ModuleNotFoundError as exc:
        raise SystemExit("pymysql is required. Install project dependencies first.") from exc

    params = _parse_mysql_url(db_url)
    unix_socket = params.pop("unix_socket", None)
    if unix_socket:
        params["unix_socket"] = unix_socket
        params.pop("host", None)
        params.pop("port", None)

    conn = pymysql.connect(**params, autocommit=False)
    deletes = list(PURGE_RELATED_DB_DELETES if purge_related else ())
    deletes.extend(DEFAULT_DB_DELETES)
    results: list[dict[str, Any]] = []
    try:
        with conn.cursor() as cursor:
            for table, column in deletes:
                deleted = _delete_from_table(cursor, table, column, assistant_id)
                results.append(
                    {
                        "table": table,
                        "column": column,
                        "deleted_rows": deleted,
                        "skipped": deleted is None,
                    }
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Delete an IronAgent assistant through admin API, then hard-delete "
            "the assistant's DB records."
        ),
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
        "--assistant-id",
        default=_env_first("ASSISTANT_ID"),
        required=not bool(_env_first("ASSISTANT_ID")),
        help="Assistant ID to delete. Can also be set with ASSISTANT_ID.",
    )
    parser.add_argument(
        "--db-url",
        default=_env_first(
            "ASSISTANT_DB_URL",
            "IRON_AGENT_DATABASE_URL",
            "GO_GATEWAY_DATABASE_URL",
            "DATABASE_URL",
            "CALLBACK_DB_URL",
        ),
        help=(
            "MySQL DB URL for hard delete. Env fallback order: ASSISTANT_DB_URL, "
            "IRON_AGENT_DATABASE_URL, GO_GATEWAY_DATABASE_URL, DATABASE_URL, CALLBACK_DB_URL."
        ),
    )
    parser.add_argument(
        "--skip-admin-delete",
        action="store_true",
        default=False,
        help="Skip DELETE /api/v1/admin/assistants/{assistant_id} and only delete DB rows.",
    )
    parser.add_argument(
        "--skip-db-hard-delete",
        action="store_true",
        default=False,
        help="Only call the admin delete endpoint; do not hard-delete DB rows.",
    )
    parser.add_argument(
        "--purge-related-db-records",
        action="store_true",
        default=False,
        help=(
            "Also delete assistant-scoped file, approval, and collaboration rows. "
            "Task/callback gateway history is still preserved."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        default=_env_first("YES", "CONFIRM_DELETE_ASSISTANT") in {"1", "true", "yes", "YES"},
        help="Actually perform the delete. Required unless --dry-run is set.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Only print the target operations; do not delete.",
    )
    parser.add_argument(
        "--ignore-missing",
        action="store_true",
        default=False,
        help="Return success if the admin API reports that the assistant is already missing.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    assistant_id = args.assistant_id.strip()
    if not assistant_id:
        print("assistant_id must not be empty", file=sys.stderr)
        return 2

    path = f"/api/v1/admin/assistants/{quote(assistant_id, safe='')}"
    db_hard_delete = not args.skip_db_hard_delete
    admin_delete = not args.skip_admin_delete

    if args.dry_run:
        print(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": True,
                    "assistant_id": assistant_id,
                    "admin_delete": {
                        "enabled": admin_delete,
                        "method": "DELETE",
                        "admin_base": args.admin_base,
                        "path": path,
                    },
                    "db_hard_delete": {
                        "enabled": db_hard_delete,
                        "db_url": _redact_url(args.db_url),
                        "default_tables": [
                            {"table": table, "column": column}
                            for table, column in DEFAULT_DB_DELETES
                        ],
                        "purge_related": args.purge_related_db_records,
                        "purge_related_tables": [
                            {"table": table, "column": column}
                            for table, column in PURGE_RELATED_DB_DELETES
                        ]
                        if args.purge_related_db_records
                        else [],
                    },
                    "note": "no API call or DB write was performed",
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

    if not admin_delete and not db_hard_delete:
        print("Nothing to do: both admin delete and DB hard delete are disabled.", file=sys.stderr)
        return 2

    if db_hard_delete and not args.db_url.strip():
        print(
            "DB hard delete is enabled but no DB URL was provided. Set ASSISTANT_DB_URL "
            "or pass --db-url, or use --skip-db-hard-delete.",
            file=sys.stderr,
        )
        return 2

    admin_result: dict[str, Any] | None = None
    if admin_delete:
        try:
            admin_result = _request(
                base_url=args.admin_base,
                method="DELETE",
                path=path,
                token=args.token,
            )
        except ApiError as exc:
            if not (exc.status == 404 and args.ignore_missing):
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "assistant_id": assistant_id,
                            "stage": "admin_delete",
                            "status": exc.status,
                            "error": exc.body,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    file=sys.stderr,
                )
                return 1
            admin_result = {
                "assistant_id": assistant_id,
                "status": "missing",
                "deleted": False,
                "response": exc.body,
            }

    db_result: list[dict[str, Any]] = []
    if db_hard_delete:
        try:
            db_result = hard_delete_db_records(
                db_url=args.db_url.strip(),
                assistant_id=assistant_id,
                purge_related=args.purge_related_db_records,
            )
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "assistant_id": assistant_id,
                        "stage": "db_hard_delete",
                        "admin_response": admin_result,
                        "db_url": _redact_url(args.db_url),
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
            return 1

    assistant_row = next(
        (
            item
            for item in db_result
            if item["table"] == "assistants" and item["column"] == "assistant_id"
        ),
        None,
    )
    if db_hard_delete and assistant_row:
        if assistant_row.get("skipped"):
            print(
                json.dumps(
                    {
                        "ok": False,
                        "assistant_id": assistant_id,
                        "stage": "db_hard_delete",
                        "admin_response": admin_result,
                        "db_result": db_result,
                        "error": (
                            "assistants.assistant_id was not found in DB; pass the control-plane "
                            "DB URL or use --skip-db-hard-delete"
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
            return 1
        if assistant_row.get("deleted_rows") == 0 and not args.ignore_missing:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "assistant_id": assistant_id,
                        "stage": "db_hard_delete",
                        "admin_response": admin_result,
                        "db_result": db_result,
                        "error": "assistant row was not found in DB; pass --ignore-missing if this is expected",
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
                "assistant_id": assistant_id,
                "admin_deleted": admin_result is not None,
                "admin_response": admin_result,
                "db_hard_deleted": db_hard_delete,
                "db_url": _redact_url(args.db_url) if db_hard_delete else "",
                "db_result": db_result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
