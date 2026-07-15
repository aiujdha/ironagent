#!/usr/bin/env python3
"""Prove Gateway upload/chat/callback flow with concurrent assistants.

The harness prepares each case first, then submits /chat for all cases in one
burst. It writes raw request/response artifacts plus an aggregate report so the
same command can be rerun in an intranet environment.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[2]
FULLFLOW_PATH = SCRIPT_PATH.with_name("chat_skill_callback_fullflow.py")


def load_fullflow_module() -> Any:
    spec = importlib.util.spec_from_file_location("chat_skill_callback_fullflow", FULLFLOW_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load fullflow helper: {FULLFLOW_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ff = load_fullflow_module()


@dataclass
class Case:
    case_id: str
    user_id: str
    assistant_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PreparedCase:
    case: Case
    out_dir: Path
    skill_slug: str
    upload_file_id: str
    message: str
    args: SimpleNamespace


@dataclass
class CompletedCase:
    case: Case
    out_dir: Path
    task_id: str = ""
    status: str = ""
    stage: str = ""
    files_len: int = 0
    reply_len: int = 0
    result_download_200: bool = False
    callback_found: bool = False
    callback_accurate: bool = False
    callback_event_count: int = 0
    callback_delivery_count: int = 0
    callback_progress_count: int = 0
    callback_terminal_type: str = ""
    callback_terminal_status: str = ""
    callback_terminal_stage: str = ""
    callback_error_code: str = ""
    chat_started_at: float = 0.0
    chat_finished_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""


class RuntimeSampler:
    def __init__(
        self,
        *,
        mysql_url: str,
        mysql_host_override: str = "",
        mysql_port_override: int = 0,
        assistant_ids: list[str],
        out_dir: Path,
        interval_seconds: float,
    ) -> None:
        self.mysql_url = mysql_url
        self.mysql_host_override = mysql_host_override
        self.mysql_port_override = mysql_port_override
        self.assistant_ids = sorted({item for item in assistant_ids if item})
        self.out_dir = out_dir
        self.interval_seconds = interval_seconds
        self.task_ids: list[str] = []
        self._task_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.summary: dict[str, Any] = {
            "enabled": bool(mysql_url),
            "sample_count": 0,
            "max_in_flight_total": 0,
            "max_distinct_assistants_with_inflight": 0,
            "max_active_runtime_instances": 0,
            "max_queue_processing_count": 0,
            "max_distinct_queue_workers": 0,
            "max_task_attempts": 0,
            "tasks_with_retry_count": 0,
            "tasks_with_retry": [],
            "final_task_queue_rows": [],
            "heartbeat_null_in_flight_seen": False,
            "errors": [],
        }

    def start(self) -> None:
        if not self.mysql_url:
            return
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="runtime-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(5.0, self.interval_seconds * 3))
        self.out_dir.mkdir(parents=True, exist_ok=True)
        ff.write_json(self.out_dir / "runtime-summary.json", self.summary)

    def capture_final_task_queue(self) -> None:
        if not self.mysql_url:
            return
        try:
            import pymysql  # type: ignore
        except ModuleNotFoundError:
            self.summary["errors"].append("pymysql is not installed")
            return
        try:
            conn = pymysql.connect(
                **ff.mysql_connection_args(
                    self.mysql_url,
                    host_override=self.mysql_host_override,
                    port_override=self.mysql_port_override,
                )
            )
        except Exception as exc:
            self.summary["errors"].append(f"connect mysql failed: {exc}")
            return
        try:
            sample = self._sample_task_queue(conn)
            if not sample:
                return
            self._update_task_queue_summary(sample)
            self.summary["final_task_queue_rows"] = sample.get("tasks", [])
            final_path = self.out_dir / "task-queue-final.json"
            self.out_dir.mkdir(parents=True, exist_ok=True)
            ff.write_json(final_path, sample)
        except Exception as exc:
            self.summary["errors"].append(f"capture final task queue failed: {exc}")
        finally:
            conn.close()

    def set_task_ids(self, task_ids: list[str]) -> None:
        with self._task_lock:
            self.task_ids = sorted({item for item in task_ids if item})

    def _run(self) -> None:
        try:
            import pymysql  # type: ignore
        except ModuleNotFoundError:
            self.summary["errors"].append("pymysql is not installed")
            return

        samples_path = self.out_dir / "runtime-samples.ndjson"
        queue_path = self.out_dir / "task-queue-samples.ndjson"
        try:
            conn = pymysql.connect(
                **ff.mysql_connection_args(
                    self.mysql_url,
                    host_override=self.mysql_host_override,
                    port_override=self.mysql_port_override,
                )
            )
        except Exception as exc:
            self.summary["errors"].append(f"connect mysql failed: {exc}")
            return

        try:
            while not self._stop.is_set():
                try:
                    sample = self._sample_runtime(conn)
                    with samples_path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(sample, ensure_ascii=False, default=ff.json_default) + "\n")
                    self._update_summary(sample)
                    task_sample = self._sample_task_queue(conn)
                    if task_sample:
                        self._update_task_queue_summary(task_sample)
                        with queue_path.open("a", encoding="utf-8") as fh:
                            fh.write(json.dumps(task_sample, ensure_ascii=False, default=ff.json_default) + "\n")
                except Exception as exc:
                    self.summary["errors"].append(str(exc))
                    try:
                        conn.ping(reconnect=True)
                    except Exception:
                        pass
                self._stop.wait(self.interval_seconds)
        finally:
            conn.close()

    def _sample_runtime(self, conn: Any) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(self.assistant_ids))
            cur.execute(
                "SELECT assistant_id, runtime_id, pod_name, status, capacity, in_flight, "
                "drain_mode, last_heartbeat_at, updated_at "
                "FROM gateway_runtime_instances "
                f"WHERE assistant_id IN ({placeholders}) "
                "ORDER BY assistant_id, runtime_id",
                tuple(self.assistant_ids),
            )
            rows = [ff.row_to_dict(cur, row) for row in cur.fetchall()]
        return {
            "sampled_at": now_iso(),
            "rows": rows,
            "in_flight_total": sum(int(row.get("in_flight") or 0) for row in rows),
            "active_runtime_instances": sum(1 for row in rows if int(row.get("in_flight") or 0) > 0),
            "distinct_assistants_with_inflight": len(
                {
                    str(row.get("assistant_id") or "")
                    for row in rows
                    if int(row.get("in_flight") or 0) > 0
                }
            ),
            "heartbeat_null_in_flight": [
                {
                    "assistant_id": row.get("assistant_id"),
                    "runtime_id": row.get("runtime_id"),
                    "in_flight": row.get("in_flight"),
                }
                for row in rows
                if int(row.get("in_flight") or 0) > 0 and row.get("last_heartbeat_at") is None
            ],
        }

    def _sample_task_queue(self, conn: Any) -> dict[str, Any]:
        with self._task_lock:
            task_ids = list(self.task_ids)
        if not task_ids:
            return {}
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(task_ids))
            cur.execute(
                "SELECT status, COUNT(*) FROM gateway_task_queue "
                f"WHERE task_id IN ({placeholders}) GROUP BY status",
                tuple(task_ids),
            )
            rows = [{"status": str(row[0]), "count": int(row[1])} for row in cur.fetchall()]
            cur.execute(
                "SELECT task_id, status, locked_by, attempt_token, attempts, "
                "last_progress_status, last_progress_stage "
                "FROM gateway_task_queue "
                f"WHERE task_id IN ({placeholders}) "
                "ORDER BY task_id",
                tuple(task_ids),
            )
            tasks = [ff.row_to_dict(cur, row) for row in cur.fetchall()]
        processing = [row for row in tasks if str(row.get("status") or "") == "processing"]
        return {
            "sampled_at": now_iso(),
            "rows": rows,
            "tasks": tasks,
            "processing_count": len(processing),
            "distinct_queue_workers": len(
                {str(row.get("locked_by") or "") for row in processing if str(row.get("locked_by") or "")}
            ),
        }

    def _update_summary(self, sample: dict[str, Any]) -> None:
        self.summary["sample_count"] += 1
        self.summary["max_in_flight_total"] = max(
            int(self.summary["max_in_flight_total"]),
            int(sample.get("in_flight_total") or 0),
        )
        self.summary["max_distinct_assistants_with_inflight"] = max(
            int(self.summary["max_distinct_assistants_with_inflight"]),
            int(sample.get("distinct_assistants_with_inflight") or 0),
        )
        self.summary["max_active_runtime_instances"] = max(
            int(self.summary["max_active_runtime_instances"]),
            int(sample.get("active_runtime_instances") or 0),
        )
        if sample.get("heartbeat_null_in_flight"):
            self.summary["heartbeat_null_in_flight_seen"] = True

    def _update_task_queue_summary(self, sample: dict[str, Any]) -> None:
        self.summary["max_queue_processing_count"] = max(
            int(self.summary.get("max_queue_processing_count") or 0),
            int(sample.get("processing_count") or 0),
        )
        self.summary["max_distinct_queue_workers"] = max(
            int(self.summary.get("max_distinct_queue_workers") or 0),
            int(sample.get("distinct_queue_workers") or 0),
        )
        tasks = [row for row in sample.get("tasks", []) if isinstance(row, dict)]
        max_attempts = max([int(row.get("attempts") or 0) for row in tasks] or [0])
        self.summary["max_task_attempts"] = max(
            int(self.summary.get("max_task_attempts") or 0),
            max_attempts,
        )
        retry_tasks = [
            {
                "task_id": str(row.get("task_id") or ""),
                "status": str(row.get("status") or ""),
                "attempts": int(row.get("attempts") or 0),
                "last_progress_status": str(row.get("last_progress_status") or ""),
                "last_progress_stage": str(row.get("last_progress_stage") or ""),
            }
            for row in tasks
            if int(row.get("attempts") or 0) > 1
        ]
        if retry_tasks:
            self.summary["tasks_with_retry"] = retry_tasks
            self.summary["tasks_with_retry_count"] = len(retry_tasks)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def compact_now() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S")


def redact(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return value[:4] + "***" + value[-4:]


def event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload_json")
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str) and payload.strip():
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    return {}


def normalized_file_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        file_id = str(item.get("fileId") or item.get("file_id") or item.get("id") or "")
        normalized.append(
            {
                "file_id": file_id,
                "file_name": str(item.get("fileName") or item.get("file_name") or ""),
                "size": int(item.get("size") or 0),
                "mime_type": str(item.get("mimeType") or item.get("mime_type") or ""),
                "download_url": str(item.get("download_url") or item.get("downloadUrl") or ""),
            }
        )
    return sorted(normalized, key=lambda item: (item["file_id"], item["file_name"]))


def count_by(values: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = value or ""
        counts[key] = counts.get(key, 0) + 1
    return counts


def callback_db_result(out_dir: Path) -> dict[str, Any]:
    path = out_dir / "08-callback-db.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def analyze_callback_accuracy(*, task_id: str, final_task: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    db_result = callback_db_result(out_dir)
    events = [item for item in db_result.get("events", []) if isinstance(item, dict)]
    deliveries = [item for item in db_result.get("deliveries", []) if isinstance(item, dict)]
    final_status = str(ff.get_field(final_task, "status") or "")
    final_stage = str(ff.get_field(final_task, "stage") or "")
    final_files_len = len(ff.files_from_task(final_task))
    payloads = [event_payload(event) for event in events]
    event_types = [str(event.get("event_type") or "") for event in events]
    terminal_events = [
        event
        for event in events
        if str(event.get("event_type") or "") in {"task.completed", "task.failed", "task.cancelled", "task.rejected"}
    ]
    terminal = terminal_events[-1] if terminal_events else {}
    terminal_payload = event_payload(terminal)
    terminal_files = terminal_payload.get("files")
    terminal_files_len = len(terminal_files) if isinstance(terminal_files, list) else 0
    final_files = ff.files_from_task(final_task)
    final_files_normalized = normalized_file_items(final_files)
    terminal_files_normalized = normalized_file_items(terminal_files)
    terminal_status = str(terminal_payload.get("status") or "")
    terminal_stage = str(terminal_payload.get("stage") or "")
    terminal_error_code = str(terminal_payload.get("errorCode") or terminal_payload.get("error_code") or "")
    payload_task_id_matches = all(
        str(payload.get("taskId") or payload.get("task_id") or "") == task_id
        for payload in payloads
        if payload
    )
    delivery_statuses = [str(item.get("status") or "") for item in deliveries]
    delivery_codes = [int(item.get("response_status_code") or 0) for item in deliveries]
    outbox_statuses = [str(event.get("status") or "") for event in events]
    all_outbox_delivered = bool(events) and all(status in {"", "delivered"} for status in outbox_statuses)
    all_deliveries_ok = (
        (bool(deliveries) and all(status == "delivered" for status in delivery_statuses))
        or (not deliveries and all_outbox_delivered)
    )
    all_responses_ok = (
        (bool(deliveries) and all(code == 0 or 200 <= code < 300 for code in delivery_codes))
        or (not deliveries and all_outbox_delivered)
    )
    terminal_matches_task = bool(terminal_payload) and terminal_status == final_status and terminal_stage == final_stage
    terminal_files_match = terminal_files_normalized == final_files_normalized
    accurate = (
        bool(events)
        and bool(terminal_payload)
        and payload_task_id_matches
        and terminal_matches_task
        and terminal_files_match
        and all_deliveries_ok
        and all_responses_ok
    )
    result = {
        "accurate": accurate,
        "event_count": len(events),
        "delivery_count": len(deliveries),
        "event_type_counts": count_by(event_types),
        "outbox_status_counts": count_by(outbox_statuses),
        "progress_count": sum(1 for item in event_types if item == "task.progress"),
        "accepted_present": "task.accepted" in event_types,
        "terminal_present": bool(terminal_payload),
        "terminal_event_type": str(terminal.get("event_type") or ""),
        "terminal_status": terminal_status,
        "terminal_stage": terminal_stage,
        "terminal_files_len": terminal_files_len,
        "terminal_error_code": terminal_error_code,
        "final_status": final_status,
        "final_stage": final_stage,
        "final_files_len": final_files_len,
        "terminal_files": terminal_files_normalized,
        "final_files": final_files_normalized,
        "payload_task_id_matches": payload_task_id_matches,
        "terminal_matches_task": terminal_matches_task,
        "terminal_files_match": terminal_files_match,
        "all_outbox_delivered": all_outbox_delivered,
        "all_deliveries_ok": all_deliveries_ok,
        "all_responses_ok": all_responses_ok,
    }
    ff.write_json(out_dir / "08-callback-accuracy.json", result)
    return result


def wait_for_callback_accuracy(*, args: Any, task_id: str, final_task: dict[str, Any], out_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    wait_seconds = float(getattr(args, "callback_wait_seconds", 0) or 0)
    poll_interval = float(getattr(args, "callback_poll_interval", 2) or 2)
    deadline = time.monotonic() + max(0.0, wait_seconds)
    callback: dict[str, Any] = {"mode": getattr(args, "callback_mode", ""), "items": []}
    accuracy: dict[str, Any] = {}
    while True:
        callback = ff.locate_callback(args=args, task_id=task_id, out_dir=out_dir)
        accuracy = analyze_callback_accuracy(
            task_id=task_id,
            final_task=final_task,
            out_dir=out_dir,
        )
        if bool(accuracy.get("accurate")) or time.monotonic() >= deadline:
            return callback, accuracy
        time.sleep(max(0.1, poll_interval))


def read_cases(args: argparse.Namespace) -> list[Case]:
    cases: list[Case] = []
    for raw in args.case or []:
        if raw.lstrip().startswith("{"):
            data = json.loads(raw)
            cases.append(
                Case(
                    case_id=str(data.get("case_id") or data.get("id") or f"case-{len(cases)+1:02d}"),
                    user_id=str(data["user_id"]),
                    assistant_id=str(data["assistant_id"]),
                    metadata={k: v for k, v in data.items() if k not in {"case_id", "id", "user_id", "assistant_id"}},
                )
            )
        else:
            user_id, assistant_id = raw.split(":", 1)
            cases.append(
                Case(
                    case_id=f"case-{len(cases)+1:02d}",
                    user_id=user_id.strip(),
                    assistant_id=assistant_id.strip(),
                )
            )
    if args.cases_file:
        path = Path(args.cases_file).resolve()
        if path.suffix.lower() == ".csv":
            with path.open(newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    cases.append(
                        Case(
                            case_id=str(row.get("case_id") or row.get("id") or f"case-{len(cases)+1:02d}"),
                            user_id=str(row["user_id"]),
                            assistant_id=str(row["assistant_id"]),
                            metadata={k: v for k, v in row.items() if k not in {"case_id", "id", "user_id", "assistant_id"}},
                        )
                    )
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
            rows = data.get("cases", data) if isinstance(data, dict) else data
            if not isinstance(rows, list):
                raise RuntimeError("cases JSON must be a list or an object with cases[]")
            for row in rows:
                if not isinstance(row, dict):
                    raise RuntimeError("each case must be an object")
                cases.append(
                    Case(
                        case_id=str(row.get("case_id") or row.get("id") or f"case-{len(cases)+1:02d}"),
                        user_id=str(row["user_id"]),
                        assistant_id=str(row["assistant_id"]),
                        metadata={k: v for k, v in row.items() if k not in {"case_id", "id", "user_id", "assistant_id"}},
                    )
                )
    normalized: list[Case] = []
    seen: set[str] = set()
    for idx, case in enumerate(cases, start=1):
        case.case_id = case.case_id.strip() or f"case-{idx:02d}"
        case.user_id = case.user_id.strip()
        case.assistant_id = case.assistant_id.strip()
        if not case.user_id or not case.assistant_id:
            raise RuntimeError(f"case {case.case_id} missing user_id or assistant_id")
        if case.case_id in seen:
            case.case_id = f"{case.case_id}-{idx:02d}"
        seen.add(case.case_id)
        normalized.append(case)
    if args.limit > 0:
        normalized = normalized[: args.limit]
    if not normalized:
        raise RuntimeError("no cases provided; use --cases-file or --case user_id:assistant_id")
    return normalized


def collect_assistant_preflight(args: argparse.Namespace, cases: list[Case], out_dir: Path, label: str) -> dict[str, Any]:
    admin_client = ff.Client(
        base_url=args.admin_base,
        timeout=args.http_timeout,
        headers=ff.admin_headers(args.admin_token),
    )
    gateway_client = ff.Client(
        base_url=args.gateway_base,
        timeout=args.http_timeout,
        headers=ff.gateway_headers(args.gateway_api_key),
        hmac_secret=args.gateway_hmac_secret,
        hmac_client_id=args.client_id,
    )
    result: dict[str, Any] = {
        "label": label,
        "checked_at": now_iso(),
        "expected_count": args.expected_count,
        "case_count": len(cases),
        "ready_count": 0,
        "missing": [],
        "not_ready": [],
        "items": [],
    }
    try:
        readyz = gateway_client.get("/readyz")
        ff.save_http(out_dir / f"{label}-readyz", readyz)
        result["readyz_status"] = readyz.status
        result["readyz"] = readyz.json()
    except Exception as exc:
        result["readyz_error"] = str(exc)
    try:
        assistants_http = admin_client.get("/api/v1/admin/assistants")
        ff.save_http(out_dir / f"{label}-assistants", assistants_http)
        payload = assistants_http.json()
        assistants = {
            str(item.get("id") or item.get("assistant_id") or ""): item
            for item in payload.get("assistants", [])
            if isinstance(item, dict)
        }
        for case in cases:
            item = assistants.get(case.assistant_id)
            if item is None:
                result["missing"].append(case.assistant_id)
                continue
            status = str(item.get("status") or "")
            observed = str(item.get("observedStatus") or item.get("observed_status") or "")
            ready = status == "running" and observed in {"", "running"}
            if ready:
                result["ready_count"] += 1
            else:
                result["not_ready"].append(
                    {
                        "assistant_id": case.assistant_id,
                        "status": status,
                        "observedStatus": observed,
                        "displayLabel": item.get("displayLabel") or "",
                        "displayReason": item.get("displayReason") or "",
                        "lastRuntimeError": item.get("lastRuntimeError") or item.get("lastError") or "",
                    }
                )
            result["items"].append(
                {
                    "case_id": case.case_id,
                    "user_id": case.user_id,
                    "assistant_id": case.assistant_id,
                    "status": status,
                    "observedStatus": observed,
                    "ready": ready,
                }
            )
        result["passed"] = (
            result["ready_count"] >= args.expected_count
            and not result["missing"]
            and not result["not_ready"]
        )
    except Exception as exc:
        result["assistants_error"] = str(exc)
        result["passed"] = False
    ff.write_json(out_dir / f"{label}-assistant-preflight.json", result)
    return result


def common_args(args: argparse.Namespace, run_id: str, out_dir: Path, case: Case) -> SimpleNamespace:
    return SimpleNamespace(
        gateway_base=args.gateway_base,
        admin_base=args.admin_base,
        admin_token=args.admin_token,
        gateway_api_key=args.gateway_api_key,
        gateway_hmac_secret=args.gateway_hmac_secret,
        user_id=case.user_id,
        assistant_id=case.assistant_id,
        run_id=f"{run_id}-{case.case_id}",
        out_dir=str(out_dir),
        http_timeout=args.http_timeout,
        skill_package=args.skill_package,
        skill_slug=args.skill_slug_template.format(run_id=run_id, case_id=case.case_id),
        skill_display_name=f"Gateway 20 Assistant Proof {run_id} {case.case_id}",
        skill_owner=args.skill_owner,
        skill_version="0.1.0",
        upload_file=args.upload_file,
        message=args.message,
        request_id=f"{args.request_prefix}-{run_id}-{case.case_id}",
        conversation_id=f"{args.conversation_prefix}-{run_id}-{case.case_id}",
        client_id=args.client_id,
        callback_url=args.callback_url,
        timeout_seconds=args.timeout_seconds,
        runtime_hold_seconds=args.runtime_hold_seconds,
        poll_seconds=args.poll_seconds,
        poll_interval=args.poll_interval,
        requires_result_file=args.requires_result_file,
        chat_extended_fields=args.chat_extended_fields,
        allow_incomplete=True,
        require_callback_log=args.require_callback_log,
        callback_mode=args.callback_mode,
        callback_db_url=args.callback_db_url or args.mysql_url,
        callback_db_host_override=args.callback_db_host_override or args.mysql_host_override,
        callback_db_port_override=args.callback_db_port_override or args.mysql_port_override,
        callback_db_attempts=args.callback_db_attempts,
        callback_db_retry_delay=args.callback_db_retry_delay,
        callback_wait_seconds=args.callback_wait_seconds,
        callback_poll_interval=args.callback_poll_interval,
        callback_log_file=args.callback_log_file,
        callback_helper=args.callback_helper,
        namespace=args.namespace,
        admin_target=args.admin_target,
        gateway_deploy=args.gateway_deploy,
        gateway_secret=args.gateway_secret,
        callback_since=args.callback_since,
    )


def prepare_case(args: argparse.Namespace, run_id: str, root_out: Path, case: Case) -> PreparedCase:
    out_dir = root_out / "cases" / case.case_id
    out_dir.mkdir(parents=True, exist_ok=True)
    case_args = common_args(args, run_id, out_dir, case)
    admin_client = ff.Client(
        base_url=case_args.admin_base,
        timeout=case_args.http_timeout,
        headers=ff.admin_headers(case_args.admin_token),
    )
    gateway_client = ff.Client(
        base_url=case_args.gateway_base,
        timeout=case_args.http_timeout,
        headers=ff.gateway_headers(case_args.gateway_api_key),
        hmac_secret=case_args.gateway_hmac_secret,
        hmac_client_id=case_args.client_id,
    )

    if args.setup_mode == "fullflow":
        package_path = (
            Path(case_args.skill_package).resolve()
            if case_args.skill_package
            else ff.create_default_skill_package(
                out_dir=out_dir,
                run_id=case_args.run_id,
                slug=case_args.skill_slug,
                display_name=case_args.skill_display_name,
                version=case_args.skill_version,
            )
        )
        skill = ff.upload_approve_install_skill(
            admin_client=admin_client,
            out_dir=out_dir,
            assistant_id=case.assistant_id,
            package_path=package_path,
            slug=case_args.skill_slug,
            display_name=case_args.skill_display_name,
            owner=case_args.skill_owner,
            version=case_args.skill_version,
        )
        ff.write_json(out_dir / "prep-skill.json", skill)

    input_path = (
        Path(case_args.upload_file).resolve()
        if case_args.upload_file
        else ff.create_default_input_file(out_dir, run_id=case_args.run_id, slug=case_args.skill_slug)
    )
    upload_file_id = ff.upload_input_file(
        gateway_client=gateway_client,
        out_dir=out_dir,
        user_id=case.user_id,
        file_path=input_path,
    )
    if args.setup_mode == "fullflow":
        message = ff.build_chat_message(
            case_args,
            run_id=case_args.run_id,
            skill_slug=case_args.skill_slug,
            file_id=upload_file_id,
        )
    else:
        message = args.message or (
            f"请处理上传文件 file_id={upload_file_id}。请回复 RUN_ID={case_args.run_id}、"
            f"USER_ID={case.user_id}、ASSISTANT_ID={case.assistant_id}，并给出简短处理结果。"
        )
    ff.write_json(
        out_dir / "prep-summary.json",
        {
            "case_id": case.case_id,
            "user_id": case.user_id,
            "assistant_id": case.assistant_id,
            "skill_slug": case_args.skill_slug,
            "upload_file_id": upload_file_id,
            "setup_mode": args.setup_mode,
        },
    )
    return PreparedCase(case, out_dir, case_args.skill_slug, upload_file_id, message, case_args)


def execute_case(prepared: PreparedCase, on_task_id: Any | None = None) -> CompletedCase:
    result = CompletedCase(case=prepared.case, out_dir=prepared.out_dir)
    gateway_client = ff.Client(
        base_url=prepared.args.gateway_base,
        timeout=prepared.args.http_timeout,
        headers=ff.gateway_headers(prepared.args.gateway_api_key),
        hmac_secret=prepared.args.gateway_hmac_secret,
        hmac_client_id=prepared.args.client_id,
    )
    try:
        result.chat_started_at = time.time()
        task_id = ff.submit_chat(
            gateway_client=gateway_client,
            out_dir=prepared.out_dir,
            args=prepared.args,
            run_id=prepared.args.run_id,
            file_id=prepared.upload_file_id,
            message=prepared.message,
        )
        result.chat_finished_at = time.time()
        result.task_id = task_id
        if on_task_id is not None:
            on_task_id(task_id)
        final_task = ff.poll_task(
            gateway_client=gateway_client,
            out_dir=prepared.out_dir,
            user_id=prepared.case.user_id,
            task_id=task_id,
            poll_seconds=prepared.args.poll_seconds,
            poll_interval=prepared.args.poll_interval,
        )
        downloads = ff.download_result_files(
            gateway_client=gateway_client,
            out_dir=prepared.out_dir,
            user_id=prepared.case.user_id,
            task_payload=final_task,
        )
        callback, callback_accuracy = wait_for_callback_accuracy(
            args=prepared.args,
            task_id=task_id,
            final_task=final_task,
            out_dir=prepared.out_dir,
        )
        task_files = ff.files_from_task(final_task)
        result.status = str(ff.get_field(final_task, "status") or "")
        result.stage = str(ff.get_field(final_task, "stage") or "")
        result.files_len = len(task_files)
        result.reply_len = len(str(ff.get_field(final_task, "replyText", "reply_text") or ""))
        result.result_download_200 = bool(task_files) and len(downloads) == len(task_files) and all(
            int(item.get("status") or 0) == 200 and item.get("size_matches", True) is not False
            for item in downloads
        )
        result.callback_found = ff.callback_evidence_found(callback)
        result.callback_accurate = bool(callback_accuracy.get("accurate"))
        result.callback_event_count = int(callback_accuracy.get("event_count") or 0)
        result.callback_delivery_count = int(callback_accuracy.get("delivery_count") or 0)
        result.callback_progress_count = int(callback_accuracy.get("progress_count") or 0)
        result.callback_terminal_type = str(callback_accuracy.get("terminal_event_type") or "")
        result.callback_terminal_status = str(callback_accuracy.get("terminal_status") or "")
        result.callback_terminal_stage = str(callback_accuracy.get("terminal_stage") or "")
        result.callback_error_code = str(callback_accuracy.get("terminal_error_code") or "")
        ff.write_json(
            prepared.out_dir / "case-summary.json",
            {
                "case_id": prepared.case.case_id,
                "user_id": prepared.case.user_id,
                "assistant_id": prepared.case.assistant_id,
                "task_id": result.task_id,
                "status": result.status,
                "stage": result.stage,
                "files_len": result.files_len,
                "reply_len": result.reply_len,
                "result_download_200": result.result_download_200,
                "callback_found": result.callback_found,
                "callback_accurate": result.callback_accurate,
                "callback_event_count": result.callback_event_count,
                "callback_delivery_count": result.callback_delivery_count,
                "callback_progress_count": result.callback_progress_count,
                "callback_terminal_type": result.callback_terminal_type,
                "callback_terminal_status": result.callback_terminal_status,
                "callback_terminal_stage": result.callback_terminal_stage,
                "callback_error_code": result.callback_error_code,
            },
        )
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        (prepared.out_dir / "case-error.txt").write_text(result.error + "\n", encoding="utf-8")
    finally:
        result.finished_at = time.time()
    return result


def run_with_pool(
    *,
    label: str,
    max_workers: int,
    items: list[Any],
    func: Any,
) -> list[Any]:
    results: list[Any] = []
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(func, item): item for item in items}
        for future in as_completed(future_map):
            item = future_map[future]
            try:
                results.append(future.result())
            except Exception as exc:
                failures.append(f"{item}: {type(exc).__name__}: {exc}")
    if failures:
        raise RuntimeError(f"{label} failures: " + "; ".join(failures[:5]))
    return results


def write_cases_template(path: Path, count: int) -> None:
    rows = [
        {
            "case_id": f"a{i:02d}",
            "user_id": f"gw20-user-{i:02d}",
            "assistant_id": f"assistant-{i:02d}",
        }
        for i in range(1, count + 1)
    ]
    ff.write_json(path, {"cases": rows})


def build_summary(
    *,
    args: argparse.Namespace,
    run_id: str,
    out_dir: Path,
    cases: list[Case],
    completed: list[CompletedCase],
    runtime_summary: dict[str, Any],
) -> dict[str, Any]:
    completed_sorted = sorted(completed, key=lambda item: item.case.case_id)
    chat_finish_times = [item.chat_finished_at for item in completed_sorted if item.chat_finished_at > 0]
    submit_span = 0.0
    if chat_finish_times:
        submit_span = max(chat_finish_times) - min(chat_finish_times)
    accepted_count = sum(1 for item in completed_sorted if item.task_id)
    completed_count = sum(1 for item in completed_sorted if item.status == "completed")
    callback_count = sum(1 for item in completed_sorted if item.callback_found)
    callback_accurate_count = sum(1 for item in completed_sorted if item.callback_accurate)
    callback_progress_task_count = sum(1 for item in completed_sorted if item.callback_progress_count > 0)
    files_count = sum(1 for item in completed_sorted if item.files_len > 0)
    download_count = sum(1 for item in completed_sorted if item.result_download_200)
    errors = [item for item in completed_sorted if item.error]
    gates: dict[str, dict[str, Any]] = {}

    def gate(name: str, passed: bool, **extra: Any) -> None:
        gates[name] = {"passed": passed, **extra}

    gate("case_count", len(cases) >= args.expected_count, actual=len(cases), expected=args.expected_count)
    gate("chat_accepted", accepted_count >= args.expected_count, actual=accepted_count, expected=args.expected_count)
    gate("chat_submit_span", submit_span <= args.max_submit_span_seconds, actual_seconds=round(submit_span, 3), threshold_seconds=args.max_submit_span_seconds)
    gate("tasks_completed", completed_count >= args.expected_count, actual=completed_count, expected=args.expected_count)
    if args.requires_result_file or args.require_result_files:
        gate("result_files_present", files_count >= args.expected_count, actual=files_count, expected=args.expected_count)
        gate("result_files_download_200", download_count >= args.expected_count, actual=download_count, expected=args.expected_count)
    if args.require_callback_log:
        gate("callback_evidence", callback_count >= args.expected_count, actual=callback_count, expected=args.expected_count)
        gate(
            "callback_accuracy",
            callback_accurate_count >= args.expected_count,
            actual=callback_accurate_count,
            expected=args.expected_count,
        )
        gate(
            "progress_callback_per_task",
            callback_progress_task_count >= args.expected_count,
            actual=callback_progress_task_count,
            expected=args.expected_count,
        )
    if args.require_runtime_concurrency:
        max_runtime = int(runtime_summary.get("max_distinct_assistants_with_inflight") or 0)
        gate(
            "runtime_concurrency",
            max_runtime >= args.expected_count and not bool(runtime_summary.get("heartbeat_null_in_flight_seen")),
            actual=max_runtime,
            expected=args.expected_count,
            heartbeat_null_in_flight_seen=runtime_summary.get("heartbeat_null_in_flight_seen"),
            sample_count=runtime_summary.get("sample_count", 0),
        )
        max_workers = int(runtime_summary.get("max_distinct_queue_workers") or 0)
        gate(
            "queue_worker_concurrency",
            max_workers >= args.expected_count,
            actual=max_workers,
            expected=args.expected_count,
            max_queue_processing_count=runtime_summary.get("max_queue_processing_count", 0),
        )
    if args.require_no_task_retry:
        max_attempts = int(runtime_summary.get("max_task_attempts") or 0)
        retry_count = int(runtime_summary.get("tasks_with_retry_count") or 0)
        gate(
            "no_task_retry",
            bool(args.mysql_url) and max_attempts <= 1 and retry_count == 0,
            actual_max_attempts=max_attempts,
            retry_task_count=retry_count,
            retry_tasks=runtime_summary.get("tasks_with_retry", []),
            evidence_available=bool(args.mysql_url),
        )
    if errors:
        gate("case_errors", False, errors=[{"case_id": item.case.case_id, "error": item.error} for item in errors])

    if all(item["passed"] for item in gates.values()):
        verdict = "PASS"
    elif accepted_count >= args.expected_count and completed_count >= args.expected_count:
        verdict = "PARTIAL"
    elif not args.mysql_url and args.require_runtime_concurrency:
        verdict = "INCONCLUSIVE"
    else:
        verdict = "FAIL"

    return {
        "schema": "gateway_20_assistant_proof.v1",
        "run_id": run_id,
        "started_at": args._started_at,
        "finished_at": now_iso(),
        "verdict": verdict,
        "out_dir": str(out_dir),
        "gateway_base": args.gateway_base,
        "admin_base": args.admin_base,
        "setup_mode": args.setup_mode,
        "expected_count": args.expected_count,
        "case_count": len(cases),
        "chat_accepted_count": accepted_count,
        "completed_count": completed_count,
        "files_present_count": files_count,
        "result_download_200_count": download_count,
        "callback_evidence_count": callback_count,
        "callback_accurate_count": callback_accurate_count,
        "callback_progress_task_count": callback_progress_task_count,
        "chat_submit_span_seconds": round(submit_span, 3),
        "runtime": runtime_summary,
        "gates": gates,
        "cases": [
            {
                "case_id": item.case.case_id,
                "user_id": item.case.user_id,
                "assistant_id": item.case.assistant_id,
                "task_id": item.task_id,
                "status": item.status,
                "stage": item.stage,
                "files_len": item.files_len,
                "reply_len": item.reply_len,
                "result_download_200": item.result_download_200,
                "callback_found": item.callback_found,
                "callback_accurate": item.callback_accurate,
                "callback_event_count": item.callback_event_count,
                "callback_delivery_count": item.callback_delivery_count,
                "callback_progress_count": item.callback_progress_count,
                "callback_terminal_type": item.callback_terminal_type,
                "callback_terminal_status": item.callback_terminal_status,
                "callback_terminal_stage": item.callback_terminal_stage,
                "callback_error_code": item.callback_error_code,
                "error": item.error,
                "out_dir": str(item.out_dir),
            }
            for item in completed_sorted
        ],
    }


def write_report(out_dir: Path, summary: dict[str, Any]) -> None:
    lines = [
        f"# Gateway {summary['expected_count']} 助手并发证明报告",
        "",
        f"- run_id: `{summary['run_id']}`",
        f"- verdict: `{summary['verdict']}`",
        f"- gateway: `{summary['gateway_base']}`",
        f"- admin: `{summary['admin_base']}`",
        f"- setup_mode: `{summary['setup_mode']}`",
        f"- expected_count: `{summary['expected_count']}`",
        f"- case_count: `{summary['case_count']}`",
        f"- chat_accepted_count: `{summary['chat_accepted_count']}`",
        f"- completed_count: `{summary['completed_count']}`",
        f"- files_present_count: `{summary['files_present_count']}`",
        f"- result_download_200_count: `{summary['result_download_200_count']}`",
        f"- callback_evidence_count: `{summary['callback_evidence_count']}`",
        f"- callback_accurate_count: `{summary['callback_accurate_count']}`",
        f"- callback_progress_task_count: `{summary['callback_progress_task_count']}`",
        f"- chat_submit_span_seconds: `{summary['chat_submit_span_seconds']}`",
        "",
        "## Runtime 并发证据",
        "",
        f"- sample_count: `{summary['runtime'].get('sample_count', 0)}`",
        f"- max_in_flight_total: `{summary['runtime'].get('max_in_flight_total', 0)}`",
        f"- max_distinct_assistants_with_inflight: `{summary['runtime'].get('max_distinct_assistants_with_inflight', 0)}`",
        f"- max_queue_processing_count: `{summary['runtime'].get('max_queue_processing_count', 0)}`",
        f"- max_distinct_queue_workers: `{summary['runtime'].get('max_distinct_queue_workers', 0)}`",
        f"- max_task_attempts: `{summary['runtime'].get('max_task_attempts', 0)}`",
        f"- tasks_with_retry_count: `{summary['runtime'].get('tasks_with_retry_count', 0)}`",
        f"- heartbeat_null_in_flight_seen: `{summary['runtime'].get('heartbeat_null_in_flight_seen', False)}`",
        "",
        "## Gates",
        "",
    ]
    for name, gate in summary["gates"].items():
        lines.append(f"- {name}: `{'PASS' if gate.get('passed') else 'FAIL'}` {json.dumps(gate, ensure_ascii=False)}")
    lines.extend(["", "## Cases", ""])
    for item in summary["cases"]:
        lines.append(
            "- "
            f"{item['case_id']}: user=`{item['user_id']}`, assistant=`{item['assistant_id']}`, "
            f"task=`{item['task_id']}`, status=`{item['status']}`, files=`{item['files_len']}`, "
            f"download200=`{item['result_download_200']}`, callback=`{item['callback_found']}`, "
            f"callback_accurate=`{item['callback_accurate']}`, progress_callbacks=`{item['callback_progress_count']}`"
        )
        if item.get("callback_error_code"):
            lines.append(f"  callback_error_code: `{item['callback_error_code']}`")
        if item.get("error"):
            lines.append(f"  error: `{item['error']}`")
    lines.extend(
        [
            "",
            "## 证据文件",
            "",
            "- `summary.json`: 机器可读总结果。",
            "- `runtime/runtime-samples.ndjson`: runtime capacity/in_flight 采样。",
            "- `runtime/task-queue-samples.ndjson`: 目标任务队列状态采样。",
            "- `runtime/task-queue-final.json`: 目标任务队列最终状态和 attempts。",
            "- `cases/<case_id>/`: 每个助手的 upload、chat、poll、download、callback 原始证据。",
            "- `cases/<case_id>/08-callback-accuracy.json`: callback payload 与任务终态的一致性校验。",
            "",
        ]
    )
    (out_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prove Gateway upload/chat/callback with 20 concurrent assistants.")
    parser.add_argument("--cases-file", default="")
    parser.add_argument("--case", action="append", help="Repeatable user_id:assistant_id or JSON object.")
    parser.add_argument("--write-template", default="", help="Write a cases JSON template and exit.")
    parser.add_argument("--template-count", type=int, default=20)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--expected-count", type=int, default=20)
    parser.add_argument("--gateway-base", default=ff.env_first("GATEWAY_BASE", "BASE_URL", "GATEWAY_URL", default="http://127.0.0.1:8080"))
    parser.add_argument("--admin-base", default=ff.env_first("ADMIN_BASE", "IRON_AGENT_ADMIN_BASE", default="http://127.0.0.1:8501"))
    parser.add_argument("--admin-token", default=ff.env_first("IRON_AGENT_INTERNAL_SERVICE_TOKEN", "IRON_AGENT_ADMIN_TOKEN", "ADMIN_TOKEN", default=""))
    parser.add_argument("--gateway-api-key", default=ff.env_first("GATEWAY_API_KEY", "API_KEY"))
    parser.add_argument("--gateway-hmac-secret", default=ff.env_first("GATEWAY_HMAC_SECRET", "GO_GATEWAY_HMAC_SECRET", default=""))
    parser.add_argument("--client-id", default=ff.env_first("CLIENT_ID", default="gateway-20-assistant-proof"))
    parser.add_argument("--mysql-url", default=ff.env_first("PROOF_MYSQL_URL", "GO_GATEWAY_DATABASE_URL", default=""))
    parser.add_argument("--mysql-host-override", default=ff.env_first("PROOF_MYSQL_HOST_OVERRIDE", "MYSQL_HOST_OVERRIDE", default=""))
    parser.add_argument("--mysql-port-override", type=int, default=int(ff.env_first("PROOF_MYSQL_PORT_OVERRIDE", "MYSQL_PORT_OVERRIDE", default="0")))
    parser.add_argument("--callback-db-url", default=ff.env_first("CALLBACK_DB_URL", default=""))
    parser.add_argument("--callback-db-host-override", default=ff.env_first("CALLBACK_DB_HOST_OVERRIDE", "MYSQL_HOST_OVERRIDE", default=""))
    parser.add_argument("--callback-db-port-override", type=int, default=int(ff.env_first("CALLBACK_DB_PORT_OVERRIDE", "MYSQL_PORT_OVERRIDE", default="0")))
    parser.add_argument("--callback-db-attempts", type=int, default=int(ff.env_first("CALLBACK_DB_ATTEMPTS", default="5")))
    parser.add_argument("--callback-db-retry-delay", type=float, default=float(ff.env_first("CALLBACK_DB_RETRY_DELAY", default="1")))
    parser.add_argument("--callback-wait-seconds", type=float, default=float(ff.env_first("CALLBACK_WAIT_SECONDS", default="60")))
    parser.add_argument("--callback-poll-interval", type=float, default=float(ff.env_first("CALLBACK_POLL_INTERVAL", default="2")))
    parser.add_argument("--callback-mode", choices=("auto", "kubectl", "db", "none"), default=ff.env_first("CALLBACK_MODE", default="auto"))
    parser.add_argument("--callback-log-file", default=ff.env_first("CALLBACK_LOG_FILE", default=""))
    parser.add_argument("--callback-helper", default=ff.env_first("CALLBACK_HELPER", default=str(REPO_ROOT / "scripts/k8s/show_callback_packets_cn.sh")))
    parser.add_argument("--callback-since", default=ff.env_first("CALLBACK_SINCE", default="180m"))
    parser.add_argument("--namespace", "-n", default=ff.env_first("NS", "NAMESPACE", default="ironagent"))
    parser.add_argument("--admin-target", default=ff.env_first("ADMIN_TARGET", default="deploy/admin"))
    parser.add_argument("--gateway-deploy", default=ff.env_first("GATEWAY_DEPLOY", default="deploy/go-gateway"))
    parser.add_argument("--gateway-secret", default=ff.env_first("GATEWAY_SECRET", default="go-gateway-secret"))
    parser.add_argument("--out-dir", default=ff.env_first("OUT_DIR", default=""))
    parser.add_argument("--run-id", default=ff.env_first("RUN_ID", default=""))
    parser.add_argument("--setup-mode", choices=("fullflow", "upload-only"), default=ff.env_first("SETUP_MODE", default="fullflow"))
    parser.add_argument("--setup-concurrency", type=int, default=int(ff.env_first("SETUP_CONCURRENCY", default="4")))
    parser.add_argument("--concurrency", type=int, default=int(ff.env_first("CONCURRENCY", default="20")))
    parser.add_argument("--http-timeout", type=int, default=int(ff.env_first("HTTP_TIMEOUT", default="120")))
    parser.add_argument("--poll-seconds", type=int, default=int(ff.env_first("POLL_SECONDS", default="1200")))
    parser.add_argument("--poll-interval", type=int, default=int(ff.env_first("POLL_INTERVAL", default="10")))
    parser.add_argument("--timeout-seconds", type=int, default=int(ff.env_first("TIMEOUT_SECONDS", default="0")))
    parser.add_argument("--runtime-hold-seconds", type=int, default=int(ff.env_first("RUNTIME_HOLD_SECONDS", default="0")))
    parser.add_argument("--runtime-sample-interval", type=float, default=float(ff.env_first("RUNTIME_SAMPLE_INTERVAL", default="1.0")))
    parser.add_argument("--max-submit-span-seconds", type=float, default=float(ff.env_first("MAX_SUBMIT_SPAN_SECONDS", default="30")))
    parser.add_argument("--skill-package", default=ff.env_first("SKILL_PACKAGE", default=""))
    parser.add_argument("--skill-slug-template", default=ff.env_first("SKILL_SLUG_TEMPLATE", default="gw20-proof-{run_id}-{case_id}"))
    parser.add_argument("--skill-owner", default=ff.env_first("SKILL_OWNER", default="e2e"))
    parser.add_argument("--upload-file", default=ff.env_first("UPLOAD_FILE", default=""))
    parser.add_argument("--message", default=ff.env_first("MESSAGE", default=""))
    parser.add_argument("--request-prefix", default=ff.env_first("REQUEST_PREFIX", default="gw20-proof"))
    parser.add_argument("--conversation-prefix", default=ff.env_first("CONVERSATION_PREFIX", default="gw20-conv"))
    parser.add_argument("--callback-url", default=ff.env_first("CALLBACK_URL", default=""))
    ff.add_bool_arg(
        parser,
        "--requires-result-file",
        default=ff.env_first("REQUIRES_RESULT_FILE", default="1") != "0",
    )
    parser.add_argument("--chat-extended-fields", action="store_true", default=ff.env_first("CHAT_EXTENDED_FIELDS", default="") == "1")
    parser.add_argument("--require-result-files", action="store_true", default=ff.env_first("REQUIRE_RESULT_FILES", default="") == "1")
    parser.add_argument("--require-callback-log", action="store_true", default=ff.env_first("REQUIRE_CALLBACK_LOG", default="") == "1")
    parser.add_argument("--require-runtime-concurrency", action="store_true", default=ff.env_first("REQUIRE_RUNTIME_CONCURRENCY", default="") == "1")
    parser.add_argument("--require-no-task-retry", action="store_true", default=ff.env_first("REQUIRE_NO_TASK_RETRY", default="") == "1")
    ff.add_bool_arg(
        parser,
        "--require-assistants-ready",
        default=ff.env_first("REQUIRE_ASSISTANTS_READY", default="1") != "0",
    )
    parser.add_argument("--allow-partial", action="store_true", default=ff.env_first("ALLOW_PARTIAL", default="") == "1")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.callback_db_url and args.mysql_url:
        args.callback_db_url = args.mysql_url
    if not args.callback_db_host_override and args.mysql_host_override:
        args.callback_db_host_override = args.mysql_host_override
    if not args.callback_db_port_override and args.mysql_port_override:
        args.callback_db_port_override = args.mysql_port_override
    if args.write_template:
        write_cases_template(Path(args.write_template).resolve(), args.template_count)
        print(f"template written: {args.write_template}")
        return 0
    args._started_at = now_iso()
    run_id = args.run_id or f"gw20-{compact_now()}"
    out_dir = Path(args.out_dir or f"output/e2e-gateway-20-assistants-{run_id}").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    cases = read_cases(args)
    ff.write_json(
        out_dir / "manifest.json",
        {
            "run_id": run_id,
            "started_at": args._started_at,
            "gateway_base": args.gateway_base,
            "admin_base": args.admin_base,
            "admin_token": redact(args.admin_token),
            "gateway_api_key": redact(args.gateway_api_key),
            "mysql_url_present": bool(args.mysql_url),
            "callback_mode": args.callback_mode,
            "setup_mode": args.setup_mode,
            "expected_count": args.expected_count,
            "case_count": len(cases),
            "cases": [case.__dict__ for case in cases],
        },
    )
    print(f"[start] run_id={run_id} out_dir={out_dir} cases={len(cases)}", flush=True)

    preflight = collect_assistant_preflight(args, cases, out_dir, "00-preflight")
    print(
        "[preflight] "
        f"ready={preflight.get('ready_count', 0)}/{args.expected_count} "
        f"missing={len(preflight.get('missing', []))} "
        f"not_ready={len(preflight.get('not_ready', []))}",
        flush=True,
    )
    if args.require_assistants_ready and not preflight.get("passed"):
        summary = {
            "schema": "gateway_20_assistant_proof.v1",
            "run_id": run_id,
            "started_at": args._started_at,
            "finished_at": now_iso(),
            "verdict": "INCONCLUSIVE",
            "reason": "assistant readiness preflight failed",
            "out_dir": str(out_dir),
            "preflight": preflight,
        }
        ff.write_json(out_dir / "summary.json", summary)
        (out_dir / "REPORT.md").write_text(
            "\n".join(
                [
                    "# Gateway 20 助手并发证明报告",
                    "",
                    f"- run_id: `{run_id}`",
                    "- verdict: `INCONCLUSIVE`",
                    "- reason: `assistant readiness preflight failed`",
                    f"- ready_count: `{preflight.get('ready_count', 0)}`",
                    f"- expected_count: `{args.expected_count}`",
                    f"- missing_count: `{len(preflight.get('missing', []))}`",
                    f"- not_ready_count: `{len(preflight.get('not_ready', []))}`",
                    "",
                    "请先让 cases 中的目标助手全部进入 running，再重新运行本脚本。",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        print(f"[done] verdict=INCONCLUSIVE report={out_dir / 'REPORT.md'}", flush=True)
        return 1

    sampler = RuntimeSampler(
        mysql_url=args.mysql_url,
        mysql_host_override=args.mysql_host_override,
        mysql_port_override=args.mysql_port_override,
        assistant_ids=[case.assistant_id for case in cases],
        out_dir=out_dir / "runtime",
        interval_seconds=args.runtime_sample_interval,
    )
    sampler.start()

    try:
        print(f"[prepare] setup_mode={args.setup_mode} setup_concurrency={args.setup_concurrency}", flush=True)
        prepared = run_with_pool(
            label="prepare",
            max_workers=max(1, args.setup_concurrency),
            items=cases,
            func=lambda case: prepare_case(args, run_id, out_dir, case),
        )
        prepared = sorted(prepared, key=lambda item: item.case.case_id)
        print(f"[submit] concurrency={args.concurrency}", flush=True)
        results: list[CompletedCase] = []
        result_queue: queue.Queue[CompletedCase] = queue.Queue()
        submitted_task_ids: set[str] = set()
        submitted_task_ids_lock = threading.Lock()

        def register_task_id(task_id: str) -> None:
            task_id = str(task_id or "").strip()
            if not task_id:
                return
            with submitted_task_ids_lock:
                submitted_task_ids.add(task_id)
                sampler.set_task_ids(list(submitted_task_ids))

        def execute_and_publish(item: PreparedCase) -> CompletedCase:
            completed = execute_case(item, on_task_id=register_task_id)
            result_queue.put(completed)
            return completed

        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as executor:
            futures = [executor.submit(execute_and_publish, item) for item in prepared]
            pending = set(futures)
            while pending:
                while True:
                    try:
                        partial = result_queue.get_nowait()
                    except queue.Empty:
                        break
                    if partial.task_id:
                        register_task_id(partial.task_id)
                done = {future for future in pending if future.done()}
                for future in done:
                    result = future.result()
                    results.append(result)
                    if result.task_id:
                        register_task_id(result.task_id)
                    print(
                        "[case] "
                        f"{result.case.case_id} task={result.task_id} status={result.status} "
                        f"files={result.files_len} callback={result.callback_found} "
                        f"callback_accurate={result.callback_accurate} error={result.error}",
                        flush=True,
                    )
                pending -= done
                if pending:
                    time.sleep(0.5)
        sampler.capture_final_task_queue()
        sampler.stop()
        summary = build_summary(
            args=args,
            run_id=run_id,
            out_dir=out_dir,
            cases=cases,
            completed=results,
            runtime_summary=sampler.summary,
        )
        ff.write_json(out_dir / "summary.json", summary)
        write_report(out_dir, summary)
        print(f"[done] verdict={summary['verdict']} report={out_dir / 'REPORT.md'}", flush=True)
        if summary["verdict"] == "PASS" or args.allow_partial:
            return 0
        return 1
    finally:
        sampler.stop()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
