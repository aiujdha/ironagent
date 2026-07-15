#!/usr/bin/env python3
"""测试 IronAgent 助手主流程接口耗时，并输出中文日志。

脚本会写出：
  - run.log：中文运行日志
  - events.jsonl：每次请求和检查点的结构化事件
  - summary.csv：接口级耗时汇总表
  - summary.json：本次执行的汇总和产物
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import logging
import mimetypes
import os
from pathlib import Path
import ssl
import sys
import time
from datetime import datetime, timezone
from http.client import HTTPResponse
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
import uuid


TERMINAL_TASK_STATUSES = {"completed", "failed", "rejected", "cancelled"}
SECRET_HEADER_NAMES = {
    "authorization",
    "x-api-key",
    "x-client-id",
    "x-signature",
    "x-ironagent-internal-token",
}
SECRET_KEYS = {
    "api_key",
    "authorization",
    "provider_api_key",
    "internal_token",
    "token",
    "secret",
    "password",
    "signature",
}
STEP_LABELS = {
    "user.create": "创建用户",
    "assistant.create": "创建助手",
    "assistant.start": "启动助手",
    "assistant.get_status": "查询助手运行状态",
    "workspace.upload_file": "上传文件到助手工作空间",
    "workspace.upload_file.approval_allow": "审批通过文件上传",
    "workspace.list_files_after_approval": "审批后查询工作空间文件",
    "workspace.download_file": "从助手工作空间下载文件",
    "skill.upload": "上传技能包",
    "skill.approve": "审批技能",
    "skill.install": "安装 skill 到指定助手",
    "assistant.use_skill.chat_submit": "提交助手使用指定 skill 的任务",
    "assistant.use_skill.task_get": "查询助手使用 skill 的任务",
    "assistant.analyze_file.chat_submit": "提交助手分析指定文件的任务",
    "assistant.analyze_file.task_get": "查询助手分析文件的任务",
}
CHECKPOINT_LABELS = {
    "run_started": "测试开始",
    "run_finished": "测试结束",
    "run_failed": "测试失败",
    "iteration_started": "轮次开始",
    "iteration_finished": "轮次结束",
    "assistant_ready": "助手运行态已就绪",
    "workspace.download_file.completed": "工作空间文件下载完成",
    "skill.install.completed": "skill 安装完成",
    "assistant.use_skill.task_terminal": "助手使用 skill 的任务已结束",
    "assistant.analyze_file.task_terminal": "助手分析文件的任务已结束",
}


def step_label(step: str) -> str:
    return STEP_LABELS.get(step, step)


def checkpoint_label(name: str) -> str:
    return CHECKPOINT_LABELS.get(name, name)


def plane_label(plane: str) -> str:
    labels = {
        "control": "后台控制面",
        "gateway": "Go Gateway",
    }
    return labels.get(plane, plane)


def response_text(event: dict[str, Any], limit: int = 300) -> str:
    response = event.get("response", {})
    if not isinstance(response, dict):
        return ""
    text = str(response.get("text") or "")
    return text[:limit]


def root_cause_hint(event: dict[str, Any]) -> str:
    status_code = int(event.get("status_code") or 0)
    error = str(event.get("error") or "")
    if "Timeout" in error:
        return "请求超时；优先检查目标服务是否卡住、下游依赖是否阻塞、超时时间是否过短"
    if "URLError" in error or "Connection refused" in error:
        return "服务不可达；优先检查地址、端口、Ingress/Service、Pod 是否运行"
    if status_code in {401, 403}:
        return "认证或权限失败；优先检查内部服务 token、Gateway API key/HMAC、能力白名单"
    if status_code == 404:
        return "路由或对象不存在；优先检查接口路径、assistant_id、user_id、file_id、skill_id"
    if status_code in {400, 422}:
        return "请求参数或接口契约不匹配；优先检查请求体字段和后端校验错误"
    if status_code == 409:
        return "资源状态冲突；优先检查对象是否已存在、状态是否允许当前操作"
    if status_code == 429:
        return "触发限流；优先检查并发、频率和网关限流配置"
    if 500 <= status_code <= 599:
        return "服务端或下游依赖异常；用 run_id/request_id 到对应服务日志继续查"
    if status_code == 0:
        return "请求未拿到 HTTP 响应；优先检查网络、DNS、TLS、服务监听状态"
    if error:
        return "请求失败；结合错误摘要和响应摘要继续查"
    return ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def monotonic_ms() -> float:
    return time.perf_counter() * 1000


def sanitize_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(secret in lowered for secret in SECRET_KEYS):
        if value in (None, ""):
            return value
        return "***"
    if isinstance(value, dict):
        return sanitize_mapping(value)
    if isinstance(value, list):
        return [sanitize_value(key, item) for item in value]
    return value


def sanitize_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    return {key: sanitize_value(key, value) for key, value in mapping.items()}


def preview_bytes(raw: bytes, *, limit: int) -> dict[str, Any]:
    text = raw[:limit].decode("utf-8", errors="replace")
    return {
        "size_bytes": len(raw),
        "truncated": len(raw) > limit,
        "text": text,
    }


def join_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


def path_with_query(path: str, query: dict[str, str] | None = None) -> str:
    if not query:
        return path
    encoded = urlencode({key: value for key, value in query.items() if value is not None})
    if not encoded:
        return path
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{encoded}"


class EventLogger:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = output_dir / "events.jsonl"
        self.summary_rows: list[dict[str, Any]] = []
        self.request_events: list[dict[str, Any]] = []
        self.artifacts: dict[str, Any] = {}

    def emit(self, event: dict[str, Any]) -> None:
        payload = {"ts": utc_now(), **event}
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def record_request(self, event: dict[str, Any]) -> None:
        self.request_events.append(event)
        self.emit(
            {
                "event": "http_request",
                "message": "接口请求完成" if event.get("ok") else "接口请求失败",
                "step_label": step_label(str(event.get("step", ""))),
                **event,
            }
        )
        self.summary_rows.append(
            {
                "步骤": step_label(str(event.get("step", ""))),
                "步骤标识": event.get("step", ""),
                "服务": plane_label(str(event.get("plane", ""))),
                "方法": event.get("method", ""),
                "路径": event.get("path", ""),
                "追踪号": event.get("request_id", ""),
                "状态码": event.get("status_code", ""),
                "是否成功": "是" if event.get("ok", False) else "否",
                "耗时毫秒": event.get("duration_ms", ""),
                "响应字节数": event.get("response_bytes", ""),
                "响应摘要": response_text(event),
                "错误摘要": event.get("error", ""),
                "根因线索": root_cause_hint(event),
            }
        )

    def checkpoint(self, name: str, **fields: Any) -> None:
        self.emit(
            {
                "event": "checkpoint",
                "message": checkpoint_label(name),
                "name": name,
                **sanitize_mapping(fields),
            }
        )

    def write_summaries(self, *, run_id: str, started_at: str, finished_at: str, ok: bool) -> None:
        summary_csv = self.output_dir / "summary.csv"
        with summary_csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "步骤",
                    "步骤标识",
                    "服务",
                    "方法",
                    "路径",
                    "追踪号",
                    "状态码",
                    "是否成功",
                    "耗时毫秒",
                    "响应字节数",
                    "响应摘要",
                    "错误摘要",
                    "根因线索",
                ],
            )
            writer.writeheader()
            writer.writerows(self.summary_rows)

        durations = [
            float(row["耗时毫秒"])
            for row in self.summary_rows
            if row.get("耗时毫秒") not in ("", None)
        ]
        summary = {
            "run_id": run_id,
            "message": "测试完成" if ok else "测试失败",
            "started_at": started_at,
            "finished_at": finished_at,
            "ok": ok,
            "request_count": len(self.summary_rows),
            "failed_request_count": sum(
                1 for row in self.summary_rows if row.get("是否成功") != "是"
            ),
            "first_failure": self.first_failure_summary(),
            "duration_ms_total": round(sum(durations), 3),
            "duration_ms_max": round(max(durations), 3) if durations else 0,
            "artifacts": sanitize_mapping(self.artifacts),
            "files": {
                "events_jsonl": str(self.jsonl_path),
                "summary_csv": str(summary_csv),
                "run_log": str(self.output_dir / "run.log"),
            },
        }
        (self.output_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def first_failure_summary(self) -> dict[str, Any]:
        for row in self.summary_rows:
            if row.get("是否成功") == "是":
                continue
            return {
                "步骤": row.get("步骤", ""),
                "步骤标识": row.get("步骤标识", ""),
                "服务": row.get("服务", ""),
                "方法": row.get("方法", ""),
                "路径": row.get("路径", ""),
                "追踪号": row.get("追踪号", ""),
                "状态码": row.get("状态码", ""),
                "错误摘要": row.get("错误摘要", ""),
                "响应摘要": row.get("响应摘要", ""),
                "根因线索": row.get("根因线索", ""),
            }
        return {}


class HTTPResult:
    def __init__(
        self,
        *,
        status_code: int,
        headers: dict[str, str],
        body: bytes,
        duration_ms: float,
        error: str = "",
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self.body = body
        self.duration_ms = duration_ms
        self.error = error

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300 and not self.error

    def json(self) -> Any:
        if not self.body:
            return {}
        return json.loads(self.body.decode("utf-8"))


class APIClient:
    def __init__(
        self,
        *,
        control_base: str,
        gateway_base: str,
        internal_token: str,
        gateway_api_key: str,
        gateway_hmac_secret: str,
        gateway_client_id: str,
        timeout_seconds: float,
        response_preview_bytes: int,
        insecure_tls: bool,
        events: EventLogger,
        run_id: str,
    ) -> None:
        self.control_base = control_base.rstrip("/")
        self.gateway_base = gateway_base.rstrip("/")
        self.internal_token = internal_token.strip()
        self.gateway_api_key = gateway_api_key.strip()
        self.gateway_hmac_secret = gateway_hmac_secret.strip()
        self.gateway_client_id = gateway_client_id.strip() or "ironagent-latency-smoke"
        self.timeout_seconds = timeout_seconds
        self.response_preview_bytes = response_preview_bytes
        self.events = events
        self.run_id = run_id
        self.ssl_context = ssl._create_unverified_context() if insecure_tls else None

    def control_json(
        self,
        step: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        expect: set[int] | None = None,
    ) -> Any:
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8") if payload is not None else b""
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self.internal_token:
            headers["X-IronAgent-Internal-Token"] = self.internal_token
        result = self._request(
            step=step,
            base=self.control_base,
            method=method,
            path=path,
            body=body if payload is not None else None,
            headers=headers,
            plane="control",
            expected_statuses=expect or {200, 201, 202, 204},
        )
        return result.json() if result.body else {}

    def control_form(
        self,
        step: str,
        method: str,
        path: str,
        fields: dict[str, str],
        files: list[tuple[str, str, bytes, str]] | None = None,
        *,
        expect: set[int] | None = None,
    ) -> Any:
        body, content_type = encode_multipart(fields, files or [])
        headers = {"Accept": "application/json", "Content-Type": content_type}
        if self.internal_token:
            headers["X-IronAgent-Internal-Token"] = self.internal_token
        result = self._request(
            step=step,
            base=self.control_base,
            method=method,
            path=path,
            body=body,
            headers=headers,
            plane="control",
            expected_statuses=expect or {200, 201, 202},
        )
        return result.json() if result.body else {}

    def control_download(self, step: str, path: str, *, expect: set[int] | None = None) -> bytes:
        headers = {}
        if self.internal_token:
            headers["X-IronAgent-Internal-Token"] = self.internal_token
        result = self._request(
            step=step,
            base=self.control_base,
            method="GET",
            path=path,
            body=None,
            headers=headers,
            plane="control",
            expected_statuses=expect or {200},
            binary_response=True,
        )
        return result.body

    def gateway_json(
        self,
        step: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        expect: set[int] | None = None,
    ) -> Any:
        body = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if payload is not None else b""
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        headers.update(self._gateway_auth_headers(method, path, body if payload is not None else b""))
        result = self._request(
            step=step,
            base=self.gateway_base,
            method=method,
            path=path,
            body=body if payload is not None else None,
            headers=headers,
            plane="gateway",
            expected_statuses=expect or {200, 201, 202},
        )
        return result.json() if result.body else {}

    def _gateway_auth_headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        if not self.gateway_api_key:
            return {}
        headers = {"X-API-Key": self.gateway_api_key}
        if not self.gateway_hmac_secret:
            return headers
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        body_hash = hashlib.sha256(body).hexdigest()
        payload = ".".join(
            [self.gateway_client_id, timestamp, nonce, method.upper(), path, body_hash]
        )
        signature = hmac.new(
            self.gateway_hmac_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers.update(
            {
                "X-Client-ID": self.gateway_client_id,
                "X-Timestamp": timestamp,
                "X-Nonce": nonce,
                "X-Signature": signature,
            }
        )
        return headers

    def _request(
        self,
        *,
        step: str,
        base: str,
        method: str,
        path: str,
        body: bytes | None,
        headers: dict[str, str],
        plane: str,
        expected_statuses: set[int],
        binary_response: bool = False,
    ) -> HTTPResult:
        url = join_url(base, path)
        request_headers = dict(headers)
        request_id = f"{self.run_id}-{uuid.uuid4().hex[:12]}"
        request_headers.setdefault("X-IronAgent-Test-Run", self.run_id)
        request_headers.setdefault("X-Request-ID", request_id)
        request = Request(url, data=body, method=method.upper(), headers=request_headers)
        started = monotonic_ms()
        status_code = 0
        response_headers: dict[str, str] = {}
        response_body = b""
        error = ""
        try:
            with urlopen(
                request,
                timeout=self.timeout_seconds,
                context=self.ssl_context,
            ) as response:
                assert isinstance(response, HTTPResponse)
                status_code = response.status
                response_headers = dict(response.headers.items())
                response_body = response.read()
        except HTTPError as exc:
            status_code = exc.code
            response_headers = dict(exc.headers.items()) if exc.headers else {}
            response_body = exc.read()
            error = f"HTTPError: {exc.code}"
        except URLError as exc:
            error = f"URLError: {exc.reason}"
        except TimeoutError as exc:
            error = f"TimeoutError: {exc}"
        except OSError as exc:
            error = f"{type(exc).__name__}: {exc}"
        duration_ms = round(monotonic_ms() - started, 3)

        result = HTTPResult(
            status_code=status_code,
            headers=response_headers,
            body=response_body,
            duration_ms=duration_ms,
            error=error,
        )
        ok = status_code in expected_statuses and not error
        event = {
            "step": step,
            "step_label": step_label(step),
            "plane": plane,
            "plane_label": plane_label(plane),
            "request_id": request_id,
            "run_id": self.run_id,
            "method": method.upper(),
            "url_base": redact_url_base(base),
            "path": path,
            "status_code": status_code,
            "ok": ok,
            "duration_ms": duration_ms,
            "request_bytes": len(body or b""),
            "response_bytes": len(response_body),
            "headers": sanitize_headers(request_headers),
            "response": preview_bytes(
                response_body,
                limit=self.response_preview_bytes if not binary_response else 0,
            ),
            "error": error if error else ("" if ok else f"非预期状态码 {status_code}"),
        }
        event["root_cause_hint"] = root_cause_hint(event)
        self.events.record_request(event)
        logging.info(
            "步骤=%s 服务=%s 方法=%s 路径=%s 状态码=%s 耗时=%.1fms 追踪号=%s",
            step_label(step),
            plane_label(plane),
            method.upper(),
            path,
            status_code or "ERR",
            duration_ms,
            request_id,
        )
        if not ok:
            raise RuntimeError(
                f"{step_label(step)}失败：{method.upper()} {path} 状态码={status_code} 错误={event['error']}"
            )
        return result


def redact_url_base(base: str) -> str:
    split = urlsplit(base)
    if "@" not in split.netloc:
        return base.rstrip("/")
    userinfo, _, host = split.netloc.rpartition("@")
    if ":" in userinfo:
        user, _, _password = userinfo.partition(":")
        netloc = f"{user}:***@{host}"
    else:
        netloc = f"***@{host}"
    return split._replace(netloc=netloc).geturl().rstrip("/")


def sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    sanitized = {}
    for key, value in headers.items():
        sanitized[key] = "***" if key.lower() in SECRET_HEADER_NAMES else value
    return sanitized


def encode_multipart(
    fields: dict[str, str],
    files: list[tuple[str, str, bytes, str]],
) -> tuple[bytes, str]:
    boundary = "----ironagent-latency-" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{escape_multipart(key)}"\r\n\r\n'.encode(
                    "utf-8"
                ),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    for field, filename, content, content_type in files:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                (
                    f'Content-Disposition: form-data; name="{escape_multipart(field)}"; '
                    f'filename="{escape_multipart(filename)}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {content_type or 'application/octet-stream'}\r\n\r\n".encode(
                    "utf-8"
                ),
                content,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def escape_multipart(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def read_file_payload(path: Path) -> tuple[str, bytes, str]:
    content = path.read_bytes()
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return path.name, content, content_type


def create_default_test_file(output_dir: Path, run_id: str) -> Path:
    path = output_dir / f"latency-test-{run_id}.txt"
    path.write_text(
        "\n".join(
            [
                "IronAgent API latency smoke test file.",
                f"run_id={run_id}",
                f"created_at={utc_now()}",
                "请分析这份文件中的关键信息，并给出简短结论。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def require_field(payload: Any, field_names: list[str], *, context: str) -> str:
    if isinstance(payload, dict):
        for name in field_names:
            value = payload.get(name)
            if value not in (None, ""):
                return str(value)
    raise RuntimeError(f"{context} 响应缺少必要字段：{'/'.join(field_names)}")


def maybe_approve(
    client: APIClient,
    payload: Any,
    *,
    label: str,
    auto_approve: bool,
) -> None:
    if not isinstance(payload, dict):
        return
    if payload.get("status") != "pending_approval":
        return
    approval_id = str(payload.get("approval_id") or "")
    if not approval_id:
        raise RuntimeError(f"{step_label(label)}需要审批，但响应中没有 approval_id")
    if not auto_approve:
        raise RuntimeError(f"{step_label(label)}需要审批：{approval_id}")
    client.control_json(
        f"{label}.approval_allow",
        "POST",
        f"/api/v1/admin/approvals/{approval_id}/resolve",
        {"resolution": "allow"},
    )


def find_file_id_by_name(
    client: APIClient,
    *,
    assistant_id: str,
    bucket: str,
    filename: str,
) -> str:
    payload = client.control_json(
        "workspace.list_files_after_approval",
        "GET",
        path_with_query(
            "/api/v1/workspace-files",
            {"assistant_id": assistant_id, "bucket": bucket, "sync": "true"},
        ),
        None,
    )
    files = payload.get("files", []) if isinstance(payload, dict) else []
    for item in files:
        if isinstance(item, dict) and item.get("filename") == filename and item.get("file_id"):
            return str(item["file_id"])
    raise RuntimeError(f"工作空间列表中未找到已上传文件：{filename!r}")


def wait_assistant_ready(
    client: APIClient,
    *,
    assistant_id: str,
    timeout_seconds: float,
    interval_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_status = ""
    while time.monotonic() < deadline:
        payload = client.control_json(
            "assistant.get_status",
            "GET",
            f"/api/v1/assistants/{assistant_id}",
            None,
        )
        runtime = payload.get("runtime", {}) if isinstance(payload, dict) else {}
        last_status = str(runtime.get("status") or payload.get("status") or "")
        if last_status in {"running", "healthy", "ready"}:
            client.events.checkpoint("assistant_ready", assistant_id=assistant_id, status=last_status)
            return
        time.sleep(interval_seconds)
    raise RuntimeError(
        f"助手 {assistant_id} 在超时时间内未就绪；最后状态={last_status}"
    )


def wait_task(
    client: APIClient,
    *,
    task_id: str,
    label: str,
    timeout_seconds: float,
    interval_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_payload: dict[str, Any] = {}
    started = monotonic_ms()
    while time.monotonic() < deadline:
        payload = client.gateway_json(
            f"{label}.task_get",
            "GET",
            f"/api/v1/tasks/{task_id}",
            None,
            expect={200},
        )
        if isinstance(payload, dict):
            last_payload = payload
            status = str(payload.get("status") or "").lower()
            if status in TERMINAL_TASK_STATUSES:
                client.events.checkpoint(
                    f"{label}.task_terminal",
                    task_id=task_id,
                    status=status,
                    stage=payload.get("stage", ""),
                    wait_duration_ms=round(monotonic_ms() - started, 3),
                )
                return payload
        time.sleep(interval_seconds)
    raise RuntimeError(
        f"{step_label(label)}任务 {task_id} 在超时时间内未结束；最后响应={last_payload}"
    )


def upload_or_resolve_skill(
    client: APIClient,
    *,
    args: argparse.Namespace,
    run_id: str,
) -> str:
    if args.skill_id:
        return args.skill_id
    if not args.skill_package:
        raise RuntimeError("必须提供 --skill-id 或 --skill-package")
    package_path = Path(args.skill_package).expanduser().resolve()
    filename, content, content_type = read_file_payload(package_path)
    slug = args.skill_slug or f"{package_path.stem}-{run_id}"
    payload = client.control_form(
        "skill.upload",
        "POST",
        "/api/v1/market/skills/upload",
        {
            "slug": slug,
            "display_name": args.skill_display_name or slug,
            "owner": args.skill_owner,
            "version": args.skill_version,
            "tags": args.skill_tags,
        },
        [("file", filename, content, content_type)],
    )
    skill_id = require_field(payload, ["id", "skill_id"], context="上传技能包")
    client.control_json("skill.approve", "POST", f"/api/v1/market/skills/{skill_id}/approve", {})
    return skill_id


def run_once(
    *,
    client: APIClient,
    args: argparse.Namespace,
    run_id: str,
    iteration: int,
    test_file: Path,
) -> None:
    suffix = f"{run_id}-{iteration:02d}"
    user_id = args.user_id or f"latency-user-{suffix}"
    assistant_id = args.assistant_id or f"latency-assistant-{suffix}"
    assistant_name = args.assistant_name or f"Latency Assistant {suffix}"

    client.events.artifacts["user_id"] = user_id
    client.events.artifacts["assistant_id"] = assistant_id
    client.events.artifacts["test_file"] = str(test_file)

    client.control_json(
        "user.create",
        "POST",
        "/api/v1/admin/users",
        {
            "user_id": user_id,
            "display_name": args.user_display_name or user_id,
            "department": args.user_department,
            "employee_id": args.employee_id or user_id,
            "role": args.user_role,
        },
    )

    client.control_json(
        "assistant.create",
        "POST",
        "/api/v1/admin/assistants",
        {
            "assistant_id": assistant_id,
            "name": assistant_name,
            "capability_profile": args.capability_profile,
            "user_id": user_id,
            "notes": "Created by measure_api_latency.py",
            "model_id": args.model_id,
            "primary_model": args.primary_model,
            "provider_id": args.provider_id,
            "provider_base_url": args.provider_base_url,
            "provider_api_key": args.provider_api_key,
            "workspace_slug": args.workspace_slug or assistant_id,
        },
    )

    if args.ensure_start:
        try:
            client.control_json(
                "assistant.start",
                "POST",
                f"/api/v1/admin/assistants/{assistant_id}/start",
                {},
            )
        except RuntimeError:
            logging.exception("启动助手失败；继续轮询助手运行态")
    if args.wait_assistant_ready:
        wait_assistant_ready(
            client,
            assistant_id=assistant_id,
            timeout_seconds=args.assistant_ready_timeout,
            interval_seconds=args.assistant_ready_interval,
        )

    upload_filename, upload_content, upload_content_type = read_file_payload(test_file)
    if args.upload_filename:
        upload_filename = args.upload_filename
    upload_payload = client.control_form(
        "workspace.upload_file",
        "POST",
        "/api/v1/workspace-files/upload",
        {
            "assistant_id": assistant_id,
            "user_id": user_id,
            "target_bucket": args.upload_bucket,
            "overwrite": "true" if args.overwrite else "false",
            "target_path": args.target_path,
            "source_type": "api-latency-smoke",
            "source_ref": run_id,
            "uploader_user_id": user_id,
        },
        [("file", upload_filename, upload_content, upload_content_type)],
    )
    maybe_approve(
        client,
        upload_payload,
        label="workspace.upload_file",
        auto_approve=args.auto_approve,
    )
    file_id = ""
    if isinstance(upload_payload, dict):
        file_id = str(upload_payload.get("file_id") or "")
    if not file_id:
        file_id = find_file_id_by_name(
            client,
            assistant_id=assistant_id,
            bucket=args.upload_bucket,
            filename=upload_filename,
        )
    client.events.artifacts["file_id"] = file_id

    downloaded = client.control_download(
        "workspace.download_file",
        path_with_query(
            f"/api/v1/workspace-files/{file_id}/download",
            {"disposition": "attachment"},
        ),
    )
    client.events.checkpoint(
        "workspace.download_file.completed",
        file_id=file_id,
        downloaded_bytes=len(downloaded),
    )

    skill_id = upload_or_resolve_skill(client, args=args, run_id=suffix)
    client.events.artifacts["skill_id"] = skill_id
    install_payload = client.control_form(
        "skill.install",
        "POST",
        f"/api/v1/market/skills/{skill_id}/install",
        {"assistant_id": assistant_id},
        [],
    )
    client.events.checkpoint("skill.install.completed", response=install_payload)

    skill_prompt = args.skill_prompt.format(
        skill=args.skill_name or args.skill_slug or skill_id,
        file_id=file_id,
        filename=upload_filename,
    )
    skill_task = client.gateway_json(
        "assistant.use_skill.chat_submit",
        "POST",
        "/api/v1/chat",
        {
            "request_id": f"{suffix}-use-skill",
            "user_id": user_id,
            "conversation_id": f"{suffix}-skill",
            "message": skill_prompt,
            "files": [],
        },
    )
    skill_task_id = require_field(
        skill_task,
        ["task_id", "taskId"],
        context="提交助手使用 skill 的任务",
    )
    client.events.artifacts["skill_task_id"] = skill_task_id
    if args.wait_task:
        wait_task(
            client,
            task_id=skill_task_id,
            label="assistant.use_skill",
            timeout_seconds=args.task_timeout,
            interval_seconds=args.task_poll_interval,
        )

    analysis_prompt = args.analysis_prompt.format(file_id=file_id, filename=upload_filename)
    analysis_task = client.gateway_json(
        "assistant.analyze_file.chat_submit",
        "POST",
        "/api/v1/chat",
        {
            "request_id": f"{suffix}-analyze-file",
            "user_id": user_id,
            "conversation_id": f"{suffix}-analysis",
            "message": analysis_prompt,
            "files": [file_id],
        },
    )
    analysis_task_id = require_field(
        analysis_task,
        ["task_id", "taskId"],
        context="提交助手分析文件的任务",
    )
    client.events.artifacts["analysis_task_id"] = analysis_task_id
    if args.wait_task:
        wait_task(
            client,
            task_id=analysis_task_id,
            label="assistant.analyze_file",
            timeout_seconds=args.task_timeout,
            interval_seconds=args.task_poll_interval,
        )


def setup_logging(output_dir: Path) -> None:
    log_path = output_dir / "run.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="执行 IronAgent 用户、助手、文件、skill、对话主流程，并记录接口耗时。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--control-base", required=True, help="后台控制面地址，例如 http://host:30808")
    parser.add_argument("--gateway-base", required=True, help="Go Gateway 地址，例如 http://host:30808")
    parser.add_argument("--internal-token", default=os.getenv("IRON_AGENT_INTERNAL_SERVICE_TOKEN", ""))
    parser.add_argument("--gateway-api-key", default=os.getenv("GO_GATEWAY_API_KEY", ""))
    parser.add_argument("--gateway-hmac-secret", default=os.getenv("GO_GATEWAY_HMAC_SECRET", ""))
    parser.add_argument("--gateway-client-id", default="ironagent-latency-smoke")
    parser.add_argument("--insecure-tls", action="store_true")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--response-preview-bytes", type=int, default=4096)

    parser.add_argument("--user-id", default="")
    parser.add_argument("--user-display-name", default="")
    parser.add_argument("--user-department", default="latency-test")
    parser.add_argument("--employee-id", default="")
    parser.add_argument("--user-role", default="user")
    parser.add_argument("--assistant-id", default="")
    parser.add_argument("--assistant-name", default="")
    parser.add_argument("--capability-profile", default="dangerous")
    parser.add_argument("--workspace-slug", default="")
    parser.add_argument("--model-id", default="")
    parser.add_argument("--primary-model", default="")
    parser.add_argument("--provider-id", default="")
    parser.add_argument("--provider-base-url", default="")
    parser.add_argument("--provider-api-key", default=os.getenv("IRON_AGENT_TEST_PROVIDER_API_KEY", ""))
    parser.add_argument("--ensure-start", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--wait-assistant-ready", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--assistant-ready-timeout", type=float, default=180.0)
    parser.add_argument("--assistant-ready-interval", type=float, default=5.0)

    parser.add_argument("--file-path", default="")
    parser.add_argument("--upload-filename", default="")
    parser.add_argument("--upload-bucket", default="persistent", choices=["temp", "persistent"])
    parser.add_argument("--target-path", default="")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--auto-approve", action=argparse.BooleanOptionalAction, default=True)

    parser.add_argument("--skill-id", default="")
    parser.add_argument("--skill-package", default="")
    parser.add_argument("--skill-slug", default="")
    parser.add_argument("--skill-name", default="")
    parser.add_argument("--skill-display-name", default="")
    parser.add_argument("--skill-owner", default="api-latency-smoke")
    parser.add_argument("--skill-version", default="1.0.0")
    parser.add_argument("--skill-tags", default="latency,smoke")
    parser.add_argument(
        "--skill-prompt",
        default="请使用已安装技能 {skill} 完成一次最小可验证操作，并用一句话说明结果。",
    )
    parser.add_argument(
        "--analysis-prompt",
        default="请分析工作空间文件 {filename}，文件ID为 {file_id}，总结关键内容和可能风险。",
    )
    parser.add_argument("--wait-task", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--task-timeout", type=float, default=300.0)
    parser.add_argument("--task-poll-interval", type=float, default=5.0)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else Path("output/api-latency") / run_id
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(output_dir)
    events = EventLogger(output_dir)
    started_at = utc_now()
    ok = False
    events.checkpoint(
        "run_started",
        run_id=run_id,
        args=sanitize_mapping(vars(args)),
        cwd=str(Path.cwd()),
    )
    logging.info("测试开始：run_id=%s 日志目录=%s", run_id, output_dir)

    try:
        test_file = Path(args.file_path).expanduser().resolve() if args.file_path else create_default_test_file(output_dir, run_id)
        if not test_file.is_file():
            raise RuntimeError(f"测试文件不存在：{test_file}")

        client = APIClient(
            control_base=args.control_base,
            gateway_base=args.gateway_base,
            internal_token=args.internal_token,
            gateway_api_key=args.gateway_api_key,
            gateway_hmac_secret=args.gateway_hmac_secret,
            gateway_client_id=args.gateway_client_id,
            timeout_seconds=args.timeout,
            response_preview_bytes=args.response_preview_bytes,
            insecure_tls=args.insecure_tls,
            events=events,
            run_id=run_id,
        )

        for index in range(1, args.iterations + 1):
            events.checkpoint("iteration_started", iteration=index)
            logging.info("第 %s 轮测试开始", index)
            run_once(client=client, args=args, run_id=run_id, iteration=index, test_file=test_file)
            events.checkpoint("iteration_finished", iteration=index)
            logging.info("第 %s 轮测试完成", index)
        ok = True
        return 0
    except Exception as exc:
        logging.exception("测试失败：%s", exc)
        events.checkpoint("run_failed", error=f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        finished_at = utc_now()
        events.checkpoint("run_finished", run_id=run_id, ok=ok)
        events.write_summaries(
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            ok=ok,
        )
        logging.info("测试汇总文件：%s", output_dir / "summary.json")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
