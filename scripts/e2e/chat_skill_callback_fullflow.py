#!/usr/bin/env python3
"""Run a /chat full-flow test with file upload, skill install, and callback lookup.

The script uses the current IronAgent public/admin API surfaces:
  - POST /api/v1/market/skills/upload
  - POST /api/v1/market/skills/{skill_id}/approve
  - POST /api/v1/market/skills/{skill_id}/install
  - POST /api/v1/files/upload
  - POST /api/v1/chat
  - GET  /api/v1/tasks/{task_id}?user_id=...
  - GET  /api/v1/files/{file_id}/download?user_id=...

Callback lookup is best-effort unless --require-callback-log is set. It can use:
  - a direct MySQL URL via --callback-db-url
  - the existing K8s helper scripts/k8s/show_callback_packets_cn.sh
  - a local log file grep via --callback-log-file
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlparse
from urllib.request import Request, urlopen


TERMINAL_STATUSES = {"completed", "failed", "cancelled", "rejected", "timeout"}
SUCCESS_STATUSES = {"completed"}
SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[2]


@dataclass
class HttpResult:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str
    method: str

    def json(self) -> dict[str, Any]:
        try:
            data = json.loads(self.body.decode("utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def compact_now() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S")


def json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=json_default) + "\n",
        encoding="utf-8",
    )


def get_field(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in data:
            return data[name]
    inner = data.get("data")
    if isinstance(inner, dict):
        for name in names:
            if name in inner:
                return inner[name]
    return None


def files_from_task(data: dict[str, Any]) -> list[dict[str, Any]]:
    value = get_field(data, "files")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def save_http(prefix: Path, result: HttpResult, *, request_payload: Any | None = None) -> None:
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix(".status").write_text(f"{result.status}\n", encoding="utf-8")
    write_json(prefix.with_suffix(".headers.json"), result.headers)
    prefix.with_suffix(".body").write_bytes(result.body)
    meta: dict[str, Any] = {
        "method": result.method,
        "url": result.url,
        "status": result.status,
    }
    if request_payload is not None:
        meta["request"] = request_payload
    write_json(prefix.with_suffix(".meta.json"), meta)
    payload = result.json()
    if payload:
        write_json(prefix.with_suffix(".pretty.json"), payload)


class Client:
    def __init__(
        self,
        *,
        base_url: str,
        timeout: int,
        headers: dict[str, str] | None = None,
        hmac_secret: str = "",
        hmac_client_id: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = headers or {}
        self.hmac_secret = hmac_secret
        self.hmac_client_id = hmac_client_id

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResult:
        if path_or_url.startswith(("http://", "https://")):
            url = path_or_url
        else:
            url = urljoin(self.base_url + "/", path_or_url.lstrip("/"))
        merged_headers = {"Accept": "application/json"}
        merged_headers.update(self.headers)
        if headers:
            merged_headers.update(headers)
        if self.hmac_secret and merged_headers.get("X-API-Key"):
            client_id = self.hmac_client_id or merged_headers.get("X-Client-ID") or "chat-skill-callback-fullflow"
            timestamp = str(int(time.time()))
            nonce = uuid.uuid4().hex
            body_hash = hashlib.sha256(body or b"").hexdigest()
            parsed_url = urlparse(url)
            request_uri = parsed_url.path or "/"
            if parsed_url.query:
                request_uri += f"?{parsed_url.query}"
            signature_payload = ".".join(
                [
                    client_id,
                    timestamp,
                    nonce,
                    method.upper(),
                    request_uri,
                    body_hash,
                ]
            )
            signature = hmac.new(
                self.hmac_secret.encode("utf-8"),
                signature_payload.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            merged_headers["X-Client-ID"] = client_id
            merged_headers["X-Timestamp"] = timestamp
            merged_headers["X-Nonce"] = nonce
            merged_headers["X-Signature"] = signature
        request = Request(url, data=body, headers=merged_headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return HttpResult(
                    status=response.status,
                    headers={k: v for k, v in response.headers.items()},
                    body=response.read(),
                    url=url,
                    method=method,
                )
        except HTTPError as exc:
            return HttpResult(
                status=exc.code,
                headers={k: v for k, v in exc.headers.items()},
                body=exc.read(),
                url=url,
                method=method,
            )
        except URLError as exc:
            raise RuntimeError(f"request failed: {method} {url}: {exc}") from exc

    def get(self, path_or_url: str) -> HttpResult:
        return self.request("GET", path_or_url)

    def post_json(self, path: str, payload: dict[str, Any]) -> HttpResult:
        return self.request(
            "POST",
            path,
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

    def post_multipart(
        self,
        path: str,
        *,
        fields: dict[str, str],
        files: dict[str, tuple[str, bytes, str]],
    ) -> HttpResult:
        boundary = f"----ironagent-{uuid.uuid4().hex}"
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.append(f"--{boundary}\r\n".encode())
            chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            chunks.append(value.encode("utf-8"))
            chunks.append(b"\r\n")
        for name, (filename, content, content_type) in files.items():
            chunks.append(f"--{boundary}\r\n".encode())
            chunks.append(
                (
                    f'Content-Disposition: form-data; name="{name}"; '
                    f'filename="{filename}"\r\n'
                    f"Content-Type: {content_type}\r\n\r\n"
                ).encode()
            )
            chunks.append(content)
            chunks.append(b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode())
        return self.request(
            "POST",
            path,
            body=b"".join(chunks),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )


def admin_headers(token: str) -> dict[str, str]:
    if not token:
        return {}
    return {
        "X-IronAgent-Internal-Token": token,
        "X-Admin-Token": token,
        "Authorization": f"Bearer {token}",
    }


def gateway_headers(api_key: str) -> dict[str, str]:
    if not api_key:
        return {}
    return {"X-API-Key": api_key}


def create_default_skill_package(
    *,
    out_dir: Path,
    run_id: str,
    slug: str,
    display_name: str,
    version: str,
) -> Path:
    source_dir = out_dir / "skill-source" / slug
    source_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        source_dir / "manifest.json",
        {
            "name": slug,
            "version": version,
            "description": f"Full-flow e2e skill package for run_id={run_id}",
            "author": "ironagent-e2e",
        },
    )
    (source_dir / "SKILL.md").write_text(
        "\n".join(
            [
                f"# {display_name}",
                "",
                "Use this skill when the task prompt mentions this skill slug.",
                "",
                "Workflow:",
                "1. Read the uploaded file referenced by files[].",
                "2. Create a result markdown file under the exact task-specific /workspace/temp/go-gateway-results/<task-id>/ directory named by the Gateway result-file section in the prompt.",
                f"3. Include RUN_ID={run_id} and SKILL_SLUG={slug} in the result file.",
                "4. Reply only with: TASK_RESULT_READY. Do not restate result paths, run ids, or file ids in the reply text; files[] is the authoritative result reference.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (source_dir / "main.py").write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "",
                "",
                "def run() -> str:",
                f"    return 'skill executed: run_id={run_id} slug={slug}'",
                "",
            ]
        ),
        encoding="utf-8",
    )
    package_path = out_dir / f"{slug}-{version}.zip"
    with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in sorted(source_dir.rglob("*")):
            if file_path.is_file():
                archive.write(file_path, arcname=file_path.relative_to(source_dir))
    return package_path


def upload_approve_install_skill(
    *,
    admin_client: Client,
    out_dir: Path,
    assistant_id: str,
    package_path: Path,
    slug: str,
    display_name: str,
    owner: str,
    version: str,
) -> dict[str, Any]:
    content_type = mimetypes.guess_type(package_path.name)[0] or "application/zip"
    upload_result = admin_client.post_multipart(
        "/api/v1/market/skills/upload",
        fields={
            "slug": slug,
            "display_name": display_name,
            "owner": owner,
            "version": version,
            "tags": "e2e,chat,callback",
        },
        files={"file": (package_path.name, package_path.read_bytes(), content_type)},
    )
    save_http(
        out_dir / "01-skill-upload",
        upload_result,
        request_payload={
            "slug": slug,
            "display_name": display_name,
            "owner": owner,
            "version": version,
            "filename": package_path.name,
        },
    )
    upload_payload = upload_result.json()
    skill_id = str(get_field(upload_payload, "id", "skill_id", "skillId") or "")
    if upload_result.status != 200 or not skill_id:
        raise RuntimeError(f"skill upload failed: status={upload_result.status} body={upload_payload}")

    approve_result = admin_client.request("POST", f"/api/v1/market/skills/{quote(skill_id)}/approve")
    save_http(out_dir / "02-skill-approve", approve_result)
    if approve_result.status != 200:
        raise RuntimeError(f"skill approve failed: status={approve_result.status} body={approve_result.json()}")

    install_result = admin_client.post_multipart(
        f"/api/v1/market/skills/{quote(skill_id)}/install",
        fields={"assistant_id": assistant_id},
        files={},
    )
    save_http(
        out_dir / "03-skill-install",
        install_result,
        request_payload={"assistant_id": assistant_id, "skill_id": skill_id},
    )
    install_payload = install_result.json()
    if install_result.status != 200:
        raise RuntimeError(f"skill install failed: status={install_result.status} body={install_payload}")
    return {
        "skill_id": skill_id,
        "upload": upload_payload,
        "approve": approve_result.json(),
        "install": install_payload,
    }


def create_default_input_file(out_dir: Path, *, run_id: str, slug: str) -> Path:
    path = out_dir / f"input-{run_id}.txt"
    path.write_text(
        "\n".join(
            [
                f"RUN_ID={run_id}",
                f"SKILL_SLUG={slug}",
                "This file is uploaded by chat_skill_callback_fullflow.py.",
                "The assistant should read it before creating the result file.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def upload_input_file(
    *,
    gateway_client: Client,
    out_dir: Path,
    user_id: str,
    file_path: Path,
) -> str:
    content_type = mimetypes.guess_type(file_path.name)[0] or "text/plain"
    result = gateway_client.post_multipart(
        "/api/v1/files/upload",
        fields={"user_id": user_id},
        files={"file": (file_path.name, file_path.read_bytes(), content_type)},
    )
    save_http(
        out_dir / "04-file-upload",
        result,
        request_payload={"user_id": user_id, "filename": file_path.name},
    )
    payload = result.json()
    file_id = str(get_field(payload, "file_id", "fileId", "id") or "")
    if result.status != 200 or not file_id:
        raise RuntimeError(f"file upload failed: status={result.status} body={payload}")

    meta = gateway_client.get(f"/api/v1/files/{quote(file_id)}?user_id={quote(user_id)}")
    save_http(out_dir / "04-file-meta", meta)
    download = gateway_client.get(
        f"/api/v1/files/{quote(file_id)}/download?user_id={quote(user_id)}"
    )
    save_http(out_dir / "04-input-download", download)
    return file_id


def build_chat_message(args: argparse.Namespace, *, run_id: str, skill_slug: str, file_id: str) -> str:
    if args.message:
        return args.message
    result_name = f"result-skill-{run_id}.md"
    hold_seconds = int(getattr(args, "runtime_hold_seconds", 0) or 0)
    hold_instruction = ""
    if hold_seconds > 0:
        hold_instruction = (
            f"为了并发采样，开始处理后请先执行一次约 {hold_seconds} 秒的等待"
            f"（例如 `python3 -c \"import time; time.sleep({hold_seconds})\"`），"
            "等待结束后再创建结果文件。"
        )
    return (
        f"请使用已经安装到当前 assistant 的 skill `{skill_slug}` 执行这条任务。"
        f"{hold_instruction}"
        f"先读取 files[] 里的上传文件 file_id={file_id}，然后根据该 skill 的 SKILL.md 或 main.py 完成处理。"
        "请查看本次完整提示末尾 Gateway 追加的 `[Runtime result file handling]` 区块，"
        "逐字复制其中的完整 `/workspace/temp/go-gateway-results/<gw-task-id>/` 目录；"
        f"必须包含最后的 `gw-...` 任务 ID 子目录，并在这个目录中创建结果文件 `{result_name}`，"
        f"内容必须包含 RUN_ID={run_id}、"
        f"SKILL_SLUG={skill_slug}、INPUT_FILE_ID={file_id}。"
        "不要写到 `/workspace/temp/go-gateway-results/` 父目录、其他任务目录、/workspace/skills 或任意非任务专属目录。"
        "最后只回复固定文本 `TASK_RESULT_READY`；不要在 replyText 正文里手写结果路径、run_id 或 file_id，"
        "结果文件准确性只以终态 files[] 和下载校验为准。"
	)


def submit_chat(
    *,
    gateway_client: Client,
    out_dir: Path,
    args: argparse.Namespace,
    run_id: str,
    file_id: str,
    message: str,
) -> str:
    request_id = args.request_id or f"skill-callback-{run_id}"
    conversation_id = args.conversation_id or f"conv-{request_id}"
    payload: dict[str, Any] = {
        "request_id": request_id,
        "user_id": args.user_id,
        "conversation_id": conversation_id,
        "message": message,
        "files": [file_id],
    }
    if args.requires_result_file:
        payload["requires_result_file"] = True
    if args.timeout_seconds > 0:
        payload["timeout_seconds"] = args.timeout_seconds
    if getattr(args, "chat_extended_fields", False):
        if args.client_id:
            payload["client_id"] = args.client_id
        payload["created_by"] = args.user_id
        if args.assistant_id:
            payload["target_assistant_id"] = args.assistant_id
        if args.callback_url:
            payload["callback_url"] = args.callback_url
        payload["metadata"] = {
            "run_id": run_id,
            "skill_slug": args.skill_slug,
            "source": "chat_skill_callback_fullflow.py",
        }

    write_json(out_dir / "05-chat-request.json", payload)
    result = gateway_client.post_json("/api/v1/chat", payload)
    save_http(out_dir / "05-chat", result, request_payload=payload)
    data = result.json()
    task_id = str(get_field(data, "task_id", "taskId", "id") or "")
    if result.status not in {200, 202} or not task_id:
        raise RuntimeError(f"chat submit failed: status={result.status} body={data}")
    return task_id


def poll_task(
    *,
    gateway_client: Client,
    out_dir: Path,
    user_id: str,
    task_id: str,
    poll_seconds: int,
    poll_interval: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + poll_seconds
    polls_file = out_dir / "06-task-polls.jsonl"
    final_payload: dict[str, Any] = {}
    count = 0
    while True:
        count += 1
        result = gateway_client.get(f"/api/v1/tasks/{quote(task_id)}?user_id={quote(user_id)}")
        save_http(out_dir / f"06-task-poll-{count:03d}", result)
        data = result.json()
        status = str(get_field(data, "status") or "")
        stage = str(get_field(data, "stage") or "")
        files = files_from_task(data)
        row = {
            "poll": count,
            "time": now_iso(),
            "http_status": result.status,
            "status": status,
            "stage": stage,
            "files_len": len(files),
            "reply_len": len(str(get_field(data, "replyText", "reply_text") or "")),
            "errorCode": get_field(data, "errorCode", "error_code") or "",
            "errorMessage": get_field(data, "errorMessage", "error_message") or "",
        }
        with polls_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(
            f"[poll] #{count} http={result.status} status={status} stage={stage} files={len(files)}",
            flush=True,
        )
        final_payload = data
        if status in TERMINAL_STATUSES:
            break
        if time.monotonic() >= deadline:
            final_payload["_poll_timeout"] = True
            break
        time.sleep(poll_interval)
    write_json(out_dir / "06-final-task.json", final_payload)
    return final_payload


def download_result_files(
    *,
    gateway_client: Client,
    out_dir: Path,
    user_id: str,
    task_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    downloads: list[dict[str, Any]] = []
    for index, item in enumerate(files_from_task(task_payload), start=1):
        file_id = str(item.get("fileId") or item.get("file_id") or item.get("id") or "")
        download_url = str(item.get("download_url") or item.get("downloadUrl") or "")
        if not download_url and file_id:
            download_url = f"/api/v1/files/{quote(file_id)}/download?user_id={quote(user_id)}"
        if not download_url:
            downloads.append({"index": index, "file_id": file_id, "status": 0, "download_url": ""})
            continue
        result = gateway_client.get(download_url)
        prefix = out_dir / f"07-result-file-{index:02d}"
        save_http(prefix, result)
        prefix.with_suffix(".head.txt").write_text(
            result.body[:4096].decode("utf-8", errors="replace"),
            encoding="utf-8",
        )
        raw_size = item.get("size") or ""
        expected_size: int | None = None
        if raw_size != "":
            try:
                expected_size = int(raw_size)
            except (TypeError, ValueError):
                expected_size = None
        size_matches = True
        if expected_size is not None and expected_size >= 0:
            size_matches = len(result.body) == expected_size
        downloads.append(
            {
                "index": index,
                "file_id": file_id,
                "file_name": item.get("fileName") or item.get("file_name") or "",
                "status": result.status,
                "download_url": download_url,
                "size": raw_size,
                "body_size": len(result.body),
                "body_sha256": hashlib.sha256(result.body).hexdigest(),
                "size_matches": size_matches,
                "mime_type": item.get("mimeType") or item.get("mime_type") or "",
                "head_file": str(prefix.with_suffix(".head.txt")),
            }
        )
    write_json(out_dir / "07-result-downloads.json", downloads)
    return downloads


def parse_mysql_url(db_url: str) -> dict[str, Any]:
    parsed = urlparse(db_url)
    if parsed.scheme.startswith("mysql") is False:
        raise ValueError("callback DB URL must be a mysql URL")
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": (parsed.path or "/").lstrip("/"),
        "charset": "utf8mb4",
        "autocommit": True,
        "connect_timeout": 10,
        "read_timeout": 20,
        "write_timeout": 20,
    }


def mysql_connection_args(
    db_url: str,
    *,
    host_override: str = "",
    port_override: int = 0,
) -> dict[str, Any]:
    args = parse_mysql_url(db_url)
    if host_override:
        args["host"] = host_override
    if port_override > 0:
        args["port"] = port_override
    return args


def table_columns(cursor: Any, table: str) -> set[str]:
    try:
        cursor.execute(f"SHOW COLUMNS FROM {table}")
    except Exception:
        return set()
    return {str(row[0]) for row in cursor.fetchall()}


def row_to_dict(cursor: Any, row: Any) -> dict[str, Any]:
    names = [desc[0] for desc in cursor.description]
    result: dict[str, Any] = {}
    for name, value in zip(names, row):
        if isinstance(value, (bytes, bytearray)):
            result[name] = value.decode("utf-8", errors="replace")
        else:
            result[name] = value
    return result


def query_callback_db(
    *,
    db_url: str,
    task_id: str,
    out_dir: Path,
    host_override: str = "",
    port_override: int = 0,
    attempts: int = 3,
    retry_delay: float = 1.0,
) -> dict[str, Any]:
    try:
        import pymysql  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError("pymysql is not installed; use --callback-mode kubectl or install pymysql") from exc

    last_error: Exception | None = None
    attempts = max(1, attempts)
    for attempt in range(1, attempts + 1):
        connection_args = mysql_connection_args(
            db_url,
            host_override=host_override,
            port_override=port_override,
        )
        connection = None
        try:
            connection = pymysql.connect(**connection_args)
            with connection.cursor() as cursor:
                outbox_cols = table_columns(cursor, "gateway_event_outbox")
                delivery_cols = table_columns(cursor, "gateway_webhook_deliveries")
                outbox_select = [
                    col
                    for col in (
                        "event_id",
                        "task_id",
                        "event_seq",
                        "event_type",
                        "callback_url",
                        "status",
                        "attempts",
                        "last_error",
                        "created_at",
                        "updated_at",
                        "payload_json",
                    )
                    if col in outbox_cols
                ]
                if not outbox_select:
                    events = []
                else:
                    cursor.execute(
                        f"SELECT {', '.join(outbox_select)} FROM gateway_event_outbox "
                        "WHERE task_id=%s ORDER BY event_seq, event_type, event_id",
                        (task_id,),
                    )
                    events = [row_to_dict(cursor, row) for row in cursor.fetchall()]

                delivery_select = [
                    f"d.{col}"
                    for col in (
                        "event_id",
                        "callback_url",
                        "attempt",
                        "status",
                        "error",
                        "delivery_mode",
                        "request_packet",
                        "response_observed",
                        "response_status_code",
                        "response_status",
                        "response_body",
                        "response_body_truncated",
                        "response_packet",
                        "duration_ms",
                        "created_at",
                    )
                    if col in delivery_cols
                ]
                deliveries: list[dict[str, Any]] = []
                if delivery_select and "event_id" in outbox_cols and "event_id" in delivery_cols:
                    cursor.execute(
                        f"SELECT {', '.join(delivery_select)} "
                        "FROM gateway_webhook_deliveries d "
                        "JOIN gateway_event_outbox o ON d.event_id=o.event_id "
                        "WHERE o.task_id=%s ORDER BY d.created_at, d.attempt, d.event_id",
                        (task_id,),
                    )
                    deliveries = [row_to_dict(cursor, row) for row in cursor.fetchall()]
            break
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                raise
            time.sleep(max(0.1, retry_delay))
        finally:
            if connection is not None:
                connection.close()
    else:
        raise RuntimeError(f"callback DB query failed: {last_error}")

    result = {"events": events, "deliveries": deliveries}
    write_json(out_dir / "08-callback-db.json", result)
    return result


def grep_callback_log(*, log_file: Path, task_id: str, out_dir: Path) -> dict[str, Any]:
    if not log_file.exists():
        result = {"status": "missing", "path": str(log_file), "matches": 0}
        write_json(out_dir / "08-callback-log-grep.json", result)
        return result
    matches: list[str] = []
    for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if task_id in line:
            matches.append(line)
    output = out_dir / "08-callback-log-grep.txt"
    output.write_text("\n".join(matches) + ("\n" if matches else ""), encoding="utf-8")
    result = {"status": "ok", "path": str(log_file), "matches": len(matches), "output": str(output)}
    write_json(out_dir / "08-callback-log-grep.json", result)
    return result


def should_run_kubectl_helper(mode: str) -> bool:
    if mode != "auto":
        return mode == "kubectl"
    if os.environ.get("KUBECTL", "").strip():
        return True
    return shutil.which("kubectl") is not None


def run_kubectl_callback_helper(
    *,
    args: argparse.Namespace,
    task_id: str,
    out_dir: Path,
) -> dict[str, Any]:
    helper = Path(args.callback_helper).resolve()
    callback_out = out_dir / "08-callback-k8s"
    callback_out.mkdir(parents=True, exist_ok=True)
    if not helper.exists():
        result = {"status": "missing_helper", "helper": str(helper)}
        write_json(out_dir / "08-callback-k8s.json", result)
        return result
    cmd = [
        "bash",
        str(helper),
        "--task-id",
        task_id,
        "--namespace",
        args.namespace,
        "--out-dir",
        str(callback_out),
        "--since",
        args.callback_since,
    ]
    if args.admin_target:
        cmd.extend(["--admin-target", args.admin_target])
    if args.gateway_deploy:
        cmd.extend(["--gateway-deploy", args.gateway_deploy])
    if args.gateway_secret:
        cmd.extend(["--gateway-secret", args.gateway_secret])

    started = time.monotonic()
    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    (out_dir / "08-callback-k8s.stdout.txt").write_text(proc.stdout, encoding="utf-8")
    (out_dir / "08-callback-k8s.stderr.txt").write_text(proc.stderr, encoding="utf-8")
    result = {
        "status": "ok" if proc.returncode == 0 else "failed",
        "returncode": proc.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "out_dir": str(callback_out),
        "report": str(callback_out / "callback-packets.txt"),
        "logs": str(callback_out / "go-gateway-logs.txt"),
    }
    write_json(out_dir / "08-callback-k8s.json", result)
    return result


def locate_callback(*, args: argparse.Namespace, task_id: str, out_dir: Path) -> dict[str, Any]:
    wait_seconds = float(getattr(args, "callback_wait_seconds", 0) or 0)
    poll_interval = float(getattr(args, "callback_poll_interval", 2) or 2)
    deadline = time.monotonic() + max(0.0, wait_seconds)
    last_evidence: dict[str, Any] | None = None
    while True:
        evidence = locate_callback_once(args=args, task_id=task_id, out_dir=out_dir)
        last_evidence = evidence
        if callback_evidence_found(evidence) or time.monotonic() >= deadline:
            return evidence
        time.sleep(max(0.1, poll_interval))
    return last_evidence or {"mode": args.callback_mode, "items": []}


def locate_callback_once(*, args: argparse.Namespace, task_id: str, out_dir: Path) -> dict[str, Any]:
    evidence: dict[str, Any] = {"mode": args.callback_mode, "items": []}
    if args.callback_mode == "none":
        write_json(out_dir / "08-callback-evidence.json", evidence)
        return evidence

    if args.callback_mode in {"auto", "db"} and args.callback_db_url:
        try:
            db_result = query_callback_db(
                db_url=args.callback_db_url,
                task_id=task_id,
                out_dir=out_dir,
                host_override=getattr(args, "callback_db_host_override", ""),
                port_override=int(getattr(args, "callback_db_port_override", 0) or 0),
                attempts=int(getattr(args, "callback_db_attempts", 3) or 3),
                retry_delay=float(getattr(args, "callback_db_retry_delay", 1.0) or 1.0),
            )
            evidence["items"].append(
                {
                    "source": "db",
                    "status": "ok",
                    "events": len(db_result.get("events", [])),
                    "deliveries": len(db_result.get("deliveries", [])),
                    "path": str(out_dir / "08-callback-db.json"),
                }
            )
        except Exception as exc:
            evidence["items"].append({"source": "db", "status": "failed", "error": str(exc)})

    if args.callback_log_file:
        result = grep_callback_log(
            log_file=Path(args.callback_log_file),
            task_id=task_id,
            out_dir=out_dir,
        )
        result["source"] = "log_file"
        evidence["items"].append(result)

    if args.callback_mode in {"auto", "kubectl"} and should_run_kubectl_helper(args.callback_mode):
        result = run_kubectl_callback_helper(args=args, task_id=task_id, out_dir=out_dir)
        result["source"] = "kubectl"
        evidence["items"].append(result)

    write_json(out_dir / "08-callback-evidence.json", evidence)
    return evidence


def callback_evidence_found(evidence: dict[str, Any]) -> bool:
    for item in evidence.get("items", []):
        if item.get("source") == "db" and item.get("status") == "ok":
            if int(item.get("events") or 0) > 0 or int(item.get("deliveries") or 0) > 0:
                return True
        if item.get("source") == "kubectl" and item.get("status") == "ok":
            report = Path(str(item.get("report") or ""))
            if report.exists() and task_id_in_file(report):
                return True
        if item.get("source") == "log_file" and int(item.get("matches") or 0) > 0:
            return True
    return False


def task_id_in_file(path: Path) -> bool:
    try:
        return bool(path.read_text(encoding="utf-8", errors="replace").strip())
    except Exception:
        return False


def run_health_checks(*, gateway_client: Client, admin_client: Client, out_dir: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for label, client, path in (
        ("gateway-healthz", gateway_client, "/healthz"),
        ("gateway-readyz", gateway_client, "/readyz"),
        ("admin-market-skills", admin_client, "/api/v1/market/skills"),
    ):
        http_result = client.get(path)
        save_http(out_dir / f"00-{label}", http_result)
        result[label] = http_result.status
    return result


def write_report(
    *,
    out_dir: Path,
    summary: dict[str, Any],
    callback_found: bool,
) -> None:
    task = summary["task"]
    lines = [
        "# /chat + skill + callback 全流程测试报告",
        "",
        f"- run_id: `{summary['run_id']}`",
        f"- user_id: `{summary['user_id']}`",
        f"- assistant_id: `{summary['assistant_id']}`",
        f"- gateway: `{summary['gateway_base']}`",
        f"- admin: `{summary['admin_base']}`",
        f"- 输出目录: `{out_dir}`",
        "",
        "## 关键结果",
        "",
        f"- skill_id: `{summary['skill'].get('skill_id', '')}`",
        f"- upload_file_id: `{summary['upload_file_id']}`",
        f"- task_id: `{task['task_id']}`",
        f"- task_status: `{task['status']}`",
        f"- task_stage: `{task['stage']}`",
        f"- task_files_len: `{task['files_len']}`",
        f"- result_download_200: `{summary['result_download_200']}`",
        f"- callback_evidence_found: `{callback_found}`",
        "",
        "## Callback 定位",
        "",
    ]
    for item in summary["callback"].get("items", []):
        source = item.get("source", "")
        status = item.get("status", "")
        if source == "db":
            lines.append(
                f"- db: status=`{status}`, events=`{item.get('events', 0)}`, "
                f"deliveries=`{item.get('deliveries', 0)}`, path=`{item.get('path', '')}`"
            )
        elif source == "kubectl":
            lines.append(
                f"- kubectl: status=`{status}`, report=`{item.get('report', '')}`, "
                f"logs=`{item.get('logs', '')}`"
            )
        elif source == "log_file":
            lines.append(
                f"- log_file: status=`{status}`, matches=`{item.get('matches', 0)}`, "
                f"output=`{item.get('output', '')}`"
            )
        else:
            lines.append(f"- {source or 'unknown'}: `{status}`")
    if not summary["callback"].get("items"):
        lines.append("- 未配置 callback DB、日志文件，且 auto 模式未找到可用 kubectl helper。")
    lines.extend(
        [
            "",
            "## 文件索引",
            "",
            "- `01-skill-upload.*`: skill 上传响应",
            "- `03-skill-install.*`: skill 安装响应",
            "- `04-file-upload.*`: 上传文件响应",
            "- `05-chat.*`: /chat 请求与响应",
            "- `06-task-polls.jsonl`: task 轮询过程",
            "- `06-final-task.json`: 最终 task payload",
            "- `07-result-downloads.json`: 结果文件下载清单",
            "- `08-callback-evidence.json`: callback 定位汇总",
            "",
        ]
    )
    (out_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def add_bool_arg(
    parser: argparse.ArgumentParser,
    name: str,
    *,
    default: bool,
    dest: str | None = None,
) -> None:
    """Python 3.8 compatible replacement for argparse.BooleanOptionalAction."""
    if not name.startswith("--"):
        raise ValueError("boolean option name must start with --")
    target = dest or name[2:].replace("-", "_")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(name, dest=target, action="store_true")
    group.add_argument(f"--no-{name[2:]}", dest=target, action="store_false")
    parser.set_defaults(**{target: default})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Full-flow test: install skill, upload file, submit /chat, locate callback logs.",
    )
    parser.add_argument("--gateway-base", default=env_first("GATEWAY_BASE", "BASE_URL", "GATEWAY_URL", default="http://127.0.0.1:8080"))
    parser.add_argument("--admin-base", default=env_first("ADMIN_BASE", "IRON_AGENT_ADMIN_BASE", default="http://127.0.0.1:8501"))
    parser.add_argument("--admin-token", default=env_first("IRON_AGENT_INTERNAL_SERVICE_TOKEN", "IRON_AGENT_ADMIN_TOKEN", "ADMIN_TOKEN", default="change-me"))
    parser.add_argument("--gateway-api-key", default=env_first("GATEWAY_API_KEY", "API_KEY"))
    parser.add_argument("--gateway-hmac-secret", default=env_first("GATEWAY_HMAC_SECRET", "GO_GATEWAY_HMAC_SECRET", default=""))
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--assistant-id", required=True)
    parser.add_argument("--run-id", default=env_first("RUN_ID", default=""))
    parser.add_argument("--out-dir", default=env_first("OUT_DIR", default=""))
    parser.add_argument("--http-timeout", type=int, default=int(env_first("HTTP_TIMEOUT", default="120")))
    parser.add_argument("--skill-package", default=env_first("SKILL_PACKAGE", default=""))
    parser.add_argument("--skill-slug", default=env_first("SKILL_SLUG", default=""))
    parser.add_argument("--skill-display-name", default=env_first("SKILL_DISPLAY_NAME", default=""))
    parser.add_argument("--skill-owner", default=env_first("SKILL_OWNER", default="e2e"))
    parser.add_argument("--skill-version", default=env_first("SKILL_VERSION", default=""))
    parser.add_argument("--upload-file", default=env_first("UPLOAD_FILE", default=""))
    parser.add_argument("--message", default=env_first("MESSAGE", default=""))
    parser.add_argument("--request-id", default=env_first("REQUEST_ID", default=""))
    parser.add_argument("--conversation-id", default=env_first("CONVERSATION_ID", default=""))
    parser.add_argument("--client-id", default=env_first("CLIENT_ID", default="chat-skill-callback-fullflow"))
    parser.add_argument("--callback-url", default=env_first("CALLBACK_URL", default=""))
    parser.add_argument("--timeout-seconds", type=int, default=int(env_first("TIMEOUT_SECONDS", default="0")))
    parser.add_argument("--runtime-hold-seconds", type=int, default=int(env_first("RUNTIME_HOLD_SECONDS", default="0")))
    parser.add_argument("--poll-seconds", type=int, default=int(env_first("POLL_SECONDS", default="900")))
    parser.add_argument("--poll-interval", type=int, default=int(env_first("POLL_INTERVAL", default="10")))
    add_bool_arg(
        parser,
        "--requires-result-file",
        default=env_first("REQUIRES_RESULT_FILE", default="1") != "0",
    )
    parser.add_argument("--chat-extended-fields", action="store_true", default=env_first("CHAT_EXTENDED_FIELDS", default="") == "1")
    parser.add_argument("--allow-incomplete", action="store_true", default=env_first("ALLOW_INCOMPLETE", default="") == "1")
    parser.add_argument("--require-callback-log", action="store_true", default=env_first("REQUIRE_CALLBACK_LOG", default="") == "1")
    parser.add_argument("--callback-mode", choices=("auto", "kubectl", "db", "none"), default=env_first("CALLBACK_MODE", default="auto"))
    parser.add_argument("--callback-db-url", default=env_first("CALLBACK_DB_URL", "GO_GATEWAY_DATABASE_URL", default=""))
    parser.add_argument("--callback-db-host-override", default=env_first("CALLBACK_DB_HOST_OVERRIDE", "MYSQL_HOST_OVERRIDE", default=""))
    parser.add_argument("--callback-db-port-override", type=int, default=int(env_first("CALLBACK_DB_PORT_OVERRIDE", "MYSQL_PORT_OVERRIDE", default="0")))
    parser.add_argument("--callback-db-attempts", type=int, default=int(env_first("CALLBACK_DB_ATTEMPTS", default="5")))
    parser.add_argument("--callback-db-retry-delay", type=float, default=float(env_first("CALLBACK_DB_RETRY_DELAY", default="1")))
    parser.add_argument("--callback-wait-seconds", type=float, default=float(env_first("CALLBACK_WAIT_SECONDS", default="60")))
    parser.add_argument("--callback-poll-interval", type=float, default=float(env_first("CALLBACK_POLL_INTERVAL", default="2")))
    parser.add_argument("--callback-log-file", default=env_first("CALLBACK_LOG_FILE", default=""))
    parser.add_argument("--callback-helper", default=env_first("CALLBACK_HELPER", default=str(REPO_ROOT / "scripts/k8s/show_callback_packets_cn.sh")))
    parser.add_argument("--namespace", "-n", default=env_first("NS", "NAMESPACE", default="ironagent"))
    parser.add_argument("--admin-target", default=env_first("ADMIN_TARGET", default="deploy/admin"))
    parser.add_argument("--gateway-deploy", default=env_first("GATEWAY_DEPLOY", default="deploy/go-gateway"))
    parser.add_argument("--gateway-secret", default=env_first("GATEWAY_SECRET", default="go-gateway-secret"))
    parser.add_argument("--callback-since", default=env_first("CALLBACK_SINCE", default="120m"))
    return parser.parse_args()


def run(args: argparse.Namespace) -> int:
    started_at = now_iso()
    run_id = args.run_id or compact_now()
    skill_slug = args.skill_slug or f"chat-skill-callback-e2e-{run_id}"
    args.skill_slug = skill_slug
    skill_display_name = args.skill_display_name or f"Chat Skill Callback E2E {run_id}"
    skill_version = args.skill_version or f"0.1.{run_id}"
    out_dir = Path(args.out_dir or f"output/fullflow-chat-skill-callback-{run_id}").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[start] out_dir={out_dir}", flush=True)
    admin_client = Client(
        base_url=args.admin_base,
        timeout=args.http_timeout,
        headers=admin_headers(args.admin_token),
    )
    gateway_client = Client(
        base_url=args.gateway_base,
        timeout=args.http_timeout,
        headers=gateway_headers(args.gateway_api_key),
        hmac_secret=args.gateway_hmac_secret,
        hmac_client_id=args.client_id,
    )

    if args.chat_extended_fields:
        print("[info] chat_extended_fields is set; /chat payload will include internal/legacy optional fields.", flush=True)
    if args.gateway_api_key and args.gateway_hmac_secret:
        print("[info] gateway_hmac_secret is set; gateway requests will be signed.", flush=True)

    health = run_health_checks(gateway_client=gateway_client, admin_client=admin_client, out_dir=out_dir)

    package_path = Path(args.skill_package).resolve() if args.skill_package else create_default_skill_package(
        out_dir=out_dir,
        run_id=run_id,
        slug=skill_slug,
        display_name=skill_display_name,
        version=skill_version,
    )
    if not package_path.exists():
        raise RuntimeError(f"skill package not found: {package_path}")

    skill = upload_approve_install_skill(
        admin_client=admin_client,
        out_dir=out_dir,
        assistant_id=args.assistant_id,
        package_path=package_path,
        slug=skill_slug,
        display_name=skill_display_name,
        owner=args.skill_owner,
        version=skill_version,
    )
    print(f"[skill] id={skill['skill_id']} slug={skill_slug}", flush=True)

    input_path = Path(args.upload_file).resolve() if args.upload_file else create_default_input_file(
        out_dir,
        run_id=run_id,
        slug=skill_slug,
    )
    if not input_path.exists():
        raise RuntimeError(f"upload file not found: {input_path}")
    upload_file_id = upload_input_file(
        gateway_client=gateway_client,
        out_dir=out_dir,
        user_id=args.user_id,
        file_path=input_path,
    )
    print(f"[upload] file_id={upload_file_id}", flush=True)

    message = build_chat_message(args, run_id=run_id, skill_slug=skill_slug, file_id=upload_file_id)
    task_id = submit_chat(
        gateway_client=gateway_client,
        out_dir=out_dir,
        args=args,
        run_id=run_id,
        file_id=upload_file_id,
        message=message,
    )
    print(f"[chat] task_id={task_id}", flush=True)

    final_task = poll_task(
        gateway_client=gateway_client,
        out_dir=out_dir,
        user_id=args.user_id,
        task_id=task_id,
        poll_seconds=args.poll_seconds,
        poll_interval=args.poll_interval,
    )
    downloads = download_result_files(
        gateway_client=gateway_client,
        out_dir=out_dir,
        user_id=args.user_id,
        task_payload=final_task,
    )
    callback = locate_callback(args=args, task_id=task_id, out_dir=out_dir)
    callback_found = callback_evidence_found(callback)

    task_status = str(get_field(final_task, "status") or "")
    task_stage = str(get_field(final_task, "stage") or "")
    task_files = files_from_task(final_task)
    result_download_200 = bool(task_files) and len(downloads) == len(task_files) and all(
        int(item.get("status") or 0) == 200 and item.get("size_matches", True) is not False
        for item in downloads
    )
    summary = {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": now_iso(),
        "gateway_base": args.gateway_base,
        "admin_base": args.admin_base,
        "user_id": args.user_id,
        "assistant_id": args.assistant_id,
        "health": health,
        "skill": skill,
        "skill_slug": skill_slug,
        "skill_package": str(package_path),
        "upload_file": str(input_path),
        "upload_file_id": upload_file_id,
        "task": {
            "task_id": task_id,
            "status": task_status,
            "stage": task_stage,
            "files_len": len(task_files),
            "reply_len": len(str(get_field(final_task, "replyText", "reply_text") or "")),
            "errorCode": get_field(final_task, "errorCode", "error_code") or "",
            "errorMessage": get_field(final_task, "errorMessage", "error_message") or "",
            "poll_timeout": bool(final_task.get("_poll_timeout")),
        },
        "downloads": downloads,
        "result_download_200": result_download_200,
        "callback": callback,
        "callback_evidence_found": callback_found,
    }
    write_json(out_dir / "summary.json", summary)
    (out_dir / "summary.env").write_text(
        "\n".join(
            [
                f"RUN_ID={run_id}",
                f"OUT_DIR={out_dir}",
                f"USER_ID={args.user_id}",
                f"ASSISTANT_ID={args.assistant_id}",
                f"SKILL_SLUG={skill_slug}",
                f"SKILL_ID={skill.get('skill_id', '')}",
                f"UPLOAD_FILE_ID={upload_file_id}",
                f"TASK_ID={task_id}",
                f"TASK_STATUS={task_status}",
                f"TASK_STAGE={task_stage}",
                f"TASK_FILES_LEN={len(task_files)}",
                f"CALLBACK_EVIDENCE_FOUND={str(callback_found).lower()}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    write_report(out_dir=out_dir, summary=summary, callback_found=callback_found)
    print(f"[done] report={out_dir / 'REPORT.md'}", flush=True)

    failures: list[str] = []
    if task_status not in SUCCESS_STATUSES:
        failures.append(f"task status is {task_status or '<empty>'}")
    if args.requires_result_file and not task_files:
        failures.append("task files[] is empty")
    if task_files and not result_download_200:
        failures.append("not all result files downloaded with HTTP 200 and matching size")
    if args.require_callback_log and not callback_found:
        failures.append("callback evidence not found")
    if failures and not args.allow_incomplete:
        for item in failures:
            print(f"[fail] {item}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run(parse_args()))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
