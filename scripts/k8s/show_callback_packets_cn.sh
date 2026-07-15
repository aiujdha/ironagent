#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

NS="${NS:-ironagent}"
ADMIN_TARGET="${ADMIN_TARGET:-deploy/admin}"
GATEWAY_DEPLOY="${GATEWAY_DEPLOY:-deploy/go-gateway}"
GATEWAY_SECRET="${GATEWAY_SECRET:-go-gateway-secret}"
DB_URL_KEY="${DB_URL_KEY:-GO_GATEWAY_DATABASE_URL}"
PYTHON_BIN="${PYTHON_BIN:-/app/.venv/bin/python}"
KUBECTL="${KUBECTL:-kubectl}"
TASK_ID="${TASK_ID:-}"
REQUEST_ID="${REQUEST_ID:-}"
OUT_DIR="${OUT_DIR:-/tmp/go-gateway-callback-packets-$(date +%Y%m%d%H%M%S)}"
SINCE="${SINCE:-60m}"
SHOW_LOGS="${SHOW_LOGS:-true}"
MAX_CHARS="${MAX_CHARS:-12000}"

usage() {
  cat <<'EOF'
用法：
  scripts/k8s/show_callback_packets_cn.sh --task-id gw-xxx [选项]
  scripts/k8s/show_callback_packets_cn.sh --request-id req-xxx [选项]

用途：
  只读查看某个任务的 callback 实际发送包和上游响应包，用来定位 files[] 是否仍然
  带 /root/sh、hotfix-*、test.sh 这类写死/热修路径。

常用选项：
  --task-id ID             按 task_id 查询。
  --request-id ID          按 request_id 查询最近一条任务。
  --namespace NS           K8s namespace，默认 ironagent。
  --admin-target TARGET    执行 DB 查询的容器目标，默认 deploy/admin。
  --gateway-deploy TARGET  Go Gateway 目标，默认 deploy/go-gateway。
  --gateway-secret NAME    存放 GO_GATEWAY_DATABASE_URL 的 secret，默认 go-gateway-secret。
  --python-bin PATH        admin 容器内 Python，默认 /app/.venv/bin/python。
  --out-dir DIR            输出目录，默认 /tmp/go-gateway-callback-packets-*。
  --since DURATION         附带抓取 go-gateway 日志的时间窗口，默认 60m。
  --no-logs                不抓取 go-gateway 日志。
  --max-chars N            request/response 包最大打印字符数，默认 12000。
  -h, --help               显示帮助。

服务器常用示例：
  KUBECTL='sudo KUBECONFIG=/etc/kubernetes/admin.conf kubectl' \
  bash scripts/k8s/show_callback_packets_cn.sh \
    --namespace ironagent \
    --task-id gw-7c298902385469bddfc2cf5cb8189c41

输出文件：
  callback-packets.txt     DB 中的任务、outbox、实际 callback 请求包、响应包、判定结论。
  go-gateway-logs.txt      按 task_id 过滤后的 go-gateway 日志，除非传 --no-logs。
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

log() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*" >&2
}

k() {
  # KUBECTL intentionally supports values with flags, for example:
  # KUBECTL='sudo KUBECONFIG=/etc/kubernetes/admin.conf kubectl'
  ${KUBECTL} "$@"
}

base64_decode() {
  if base64 --help 2>&1 | grep -q -- '-d'; then
    base64 -d
  else
    base64 -D
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --task-id)
      TASK_ID="${2:-}"
      shift 2
      ;;
    --request-id)
      REQUEST_ID="${2:-}"
      shift 2
      ;;
    --namespace|-n)
      NS="${2:-}"
      shift 2
      ;;
    --admin-target)
      ADMIN_TARGET="${2:-}"
      shift 2
      ;;
    --gateway-deploy)
      GATEWAY_DEPLOY="${2:-}"
      shift 2
      ;;
    --gateway-secret)
      GATEWAY_SECRET="${2:-}"
      shift 2
      ;;
    --python-bin)
      PYTHON_BIN="${2:-}"
      shift 2
      ;;
    --out-dir)
      OUT_DIR="${2:-}"
      shift 2
      ;;
    --since)
      SINCE="${2:-}"
      shift 2
      ;;
    --no-logs)
      SHOW_LOGS="false"
      shift
      ;;
    --max-chars)
      MAX_CHARS="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "未知参数：$1"
      ;;
  esac
done

[[ -n "$TASK_ID" || -n "$REQUEST_ID" ]] || die "必须传 --task-id 或 --request-id"
mkdir -p "$OUT_DIR"
chmod 700 "$OUT_DIR"
REPORT_FILE="$OUT_DIR/callback-packets.txt"
LOG_FILE="$OUT_DIR/go-gateway-logs.txt"

log "输出目录：$OUT_DIR"
log "读取数据库连接：secret/$GATEWAY_SECRET key=$DB_URL_KEY"
DB_URL="$(k -n "$NS" get secret "$GATEWAY_SECRET" -o "jsonpath={.data.${DB_URL_KEY}}" | base64_decode)"
[[ -n "$DB_URL" ]] || die "没有从 secret/$GATEWAY_SECRET 读到 $DB_URL_KEY"

{
  echo "========== K8S CONTEXT =========="
  echo "namespace=$NS"
  echo "admin_target=$ADMIN_TARGET"
  echo "gateway_deploy=$GATEWAY_DEPLOY"
  echo "gateway_image=$(k -n "$NS" get "$GATEWAY_DEPLOY" -o jsonpath='{.spec.template.spec.containers[*].image}' 2>/dev/null || true)"
  echo
  echo "========== GATEWAY CALLBACK ENV =========="
  k -n "$NS" exec "$GATEWAY_DEPLOY" -- sh -c \
    'env | grep -E "^(GO_GATEWAY_CALLBACK_FIRE_AND_FORGET|GO_GATEWAY_CALLBACK_ACCEPT_ANY_RESPONSE|GO_GATEWAY_CALLBACK_ALLOWED_HOSTS|GO_GATEWAY_CALLBACK_ALLOW_PRIVATE_HOSTS|GO_GATEWAY_DEFAULT_CALLBACK_URL)=" | sort' \
    2>/dev/null || true
  echo
  echo "========== DB INSPECTION =========="
} | tee "$REPORT_FILE" >/dev/null

k -n "$NS" exec -i "$ADMIN_TARGET" -- \
  env DB_URL="$DB_URL" TASK_ID="$TASK_ID" REQUEST_ID="$REQUEST_ID" MAX_CHARS="$MAX_CHARS" "$PYTHON_BIN" - <<'PY' | tee -a "$REPORT_FILE"
import json
import os
import re
import sys
from datetime import datetime, date

from sqlalchemy import create_engine, text


db_url = os.environ["DB_URL"]
task_id_arg = os.environ.get("TASK_ID", "").strip()
request_id_arg = os.environ.get("REQUEST_ID", "").strip()
max_chars = int(os.environ.get("MAX_CHARS", "12000") or "12000")


def jload(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    value = str(value)
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return value


def jdump(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def scalar(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ")
    return "" if value is None else str(value)


def truncate(value):
    value = "" if value is None else str(value)
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + f"\n... <truncated {len(value) - max_chars} chars>"


def http_body(packet):
    packet = "" if packet is None else str(packet)
    for sep in ("\r\n\r\n", "\n\n"):
        if sep in packet:
            return packet.split(sep, 1)[1]
    return ""


def maybe_pretty_json(value):
    value = "" if value is None else str(value).strip()
    if not value:
        return ""
    try:
        return jdump(json.loads(value))
    except Exception:
        return value


def table_columns(conn, table):
    rows = conn.execute(
        text(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = :table
            """
        ),
        {"table": table},
    )
    return {row[0] for row in rows}


def select_expr(cols, names, qualifier=""):
    parts = []
    for name in names:
        if name in cols:
            if qualifier:
                parts.append(f"{qualifier}.{name} AS {name}")
            else:
                parts.append(name)
        else:
            parts.append(f"NULL AS {name}")
    return ", ".join(parts)


def deep_get(data, *keys):
    cur = data
    for key in keys:
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(key)
    return "" if cur is None else cur


def file_items_from(value):
    data = jload(value)
    if isinstance(data, dict):
        files = data.get("files")
    else:
        files = data
    return files if isinstance(files, list) else []


def file_summary(files):
    ids = []
    names = []
    urls = []
    suspicious = []
    for item in files:
        if not isinstance(item, dict):
            suspicious.append(f"non_object={item!r}")
            continue
        file_id = str(item.get("file_id") or item.get("fileId") or "").strip()
        filename = str(item.get("filename") or item.get("fileName") or "").strip()
        url = str(item.get("download_url") or "").strip()
        mime = str(item.get("mime_type") or item.get("mimeType") or "").strip()
        size = str(item.get("size") or "").strip()
        if file_id:
            ids.append(file_id)
        if filename:
            names.append(filename)
        if url:
            urls.append(url)
        if file_id.startswith("hotfix-"):
            suspicious.append(f"file_id={file_id}")
        if url == "/root/sh":
            suspicious.append("download_url=/root/sh")
        if filename == "test.sh":
            suspicious.append("filename=test.sh")
        if mime == ".sh":
            suspicious.append("mime_type=.sh")
        if filename == "test.sh" and size == "4096":
            suspicious.append("test.sh+4096")
    return {
        "len": len(files),
        "file_ids": ids,
        "filenames": names,
        "download_urls": urls,
        "suspicious": sorted(set(suspicious)),
    }


def print_section(title):
    print()
    print(f"========== {title} ==========")


def print_kv(key, value):
    print(f"{key}={scalar(value)}")


engine = create_engine(db_url, pool_pre_ping=True)
with engine.connect() as conn:
    task_cols = table_columns(conn, "gateway_tasks")
    outbox_cols = table_columns(conn, "gateway_event_outbox")
    delivery_cols = table_columns(conn, "gateway_webhook_deliveries")

    task_names = [
        "task_id",
        "client_id",
        "request_id",
        "conversation_id",
        "user_id",
        "employee_no",
        "target_assistant_id",
        "southbound_task_id",
        "status",
        "stage",
        "callback_url",
        "failed_reason_code",
        "failed_reason_text",
        "metadata_json",
        "task_json",
        "created_at",
        "updated_at",
        "completed_at",
    ]
    if task_id_arg:
        task_sql = f"""
            SELECT {select_expr(task_cols, task_names)}
            FROM gateway_tasks
            WHERE task_id = :task_id
            LIMIT 1
        """
        task_row = conn.execute(text(task_sql), {"task_id": task_id_arg}).mappings().first()
    else:
        task_sql = f"""
            SELECT {select_expr(task_cols, task_names)}
            FROM gateway_tasks
            WHERE request_id = :request_id
            ORDER BY updated_at DESC
            LIMIT 1
        """
        task_row = conn.execute(text(task_sql), {"request_id": request_id_arg}).mappings().first()

    if task_row is None:
        print("resolved_task_id=")
        print("ERROR: gateway_tasks 中没有找到目标任务")
        sys.exit(2)

    task = dict(task_row)
    task_json = jload(task.get("task_json")) or {}
    metadata_json = jload(task.get("metadata_json")) or {}
    resolved_task_id = task.get("task_id") or ""
    result_workspace_file_id = deep_get(task_json, "result_workspace_file_id")
    result_workspace_file_ids = []
    if isinstance(metadata_json, dict):
        value = metadata_json.get("runtime_result_workspace_file_ids")
        if isinstance(value, list):
            result_workspace_file_ids = [str(item).strip() for item in value if str(item).strip()]
    if not result_workspace_file_ids:
        for path in (
            ("meta_json", "runtime_result_workspace_file_ids"),
            ("metadata", "runtime_result_workspace_file_ids"),
        ):
            value = deep_get(task_json, *path)
            if isinstance(value, list):
                result_workspace_file_ids = [str(item).strip() for item in value if str(item).strip()]
                break
    result_object_id = deep_get(task_json, "result_object_id")
    runtime_reply_text = deep_get(task_json, "metadata", "runtime_reply_text")
    if not runtime_reply_text and isinstance(metadata_json, dict):
        runtime_reply_text = metadata_json.get("runtime_reply_text", "")

    print(f"resolved_task_id={resolved_task_id}")

    print_section("TASK ROW")
    for key in (
        "task_id",
        "request_id",
        "conversation_id",
        "user_id",
        "target_assistant_id",
        "southbound_task_id",
        "status",
        "stage",
        "callback_url",
        "failed_reason_code",
        "failed_reason_text",
        "created_at",
        "updated_at",
        "completed_at",
    ):
        print_kv(key, task.get(key))
    print_kv("result_object_id", result_object_id)
    print_kv("result_workspace_file_id", result_workspace_file_id)
    print_kv("runtime_result_workspace_file_ids", jdump(result_workspace_file_ids))
    print_kv("runtime_reply_text_prefix", str(runtime_reply_text)[:500])

    outbox_names = [
        "event_id",
        "task_id",
        "event_seq",
        "event_type",
        "status",
        "attempts",
        "last_error",
        "callback_url",
        "payload_json",
        "created_at",
        "updated_at",
        "delivered_at",
        "failed_at",
        "next_attempt_at",
    ]
    outbox_sql = f"""
        SELECT {select_expr(outbox_cols, outbox_names)}
        FROM gateway_event_outbox
        WHERE task_id = :task_id
        ORDER BY event_seq
    """
    events = [dict(row) for row in conn.execute(text(outbox_sql), {"task_id": resolved_task_id}).mappings()]

    print_section("OUTBOX EVENTS")
    if not events:
        print("没有 gateway_event_outbox 记录。")
    for event in events:
        payload = jload(event.get("payload_json")) or {}
        files = file_items_from(payload)
        summary = file_summary(files)
        print()
        print(f"-- event_seq={event.get('event_seq')} event_type={event.get('event_type')} event_id={event.get('event_id')}")
        for key in ("status", "attempts", "last_error", "callback_url", "created_at", "updated_at", "delivered_at", "failed_at", "next_attempt_at"):
            print_kv(key, event.get(key))
        print("payload_status=" + str(payload.get("status", "") if isinstance(payload, dict) else ""))
        print("payload_stage=" + str(payload.get("stage", "") if isinstance(payload, dict) else ""))
        print("payload_files_summary=" + jdump(summary))
        print("payload_files=" + jdump(files))

    delivery_names = [
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
        "response_content_type",
        "response_content_length",
        "response_body",
        "response_body_bytes",
        "response_body_read_error",
        "response_body_truncated",
        "response_packet",
        "duration_ms",
        "created_at",
    ]
    delivery_sql = f"""
        SELECT
          o.event_seq,
          o.event_type,
          {select_expr(delivery_cols, delivery_names, "d")}
        FROM gateway_event_outbox o
        LEFT JOIN gateway_webhook_deliveries d ON d.event_id = o.event_id
        WHERE o.task_id = :task_id
        ORDER BY o.event_seq, d.attempt
    """
    deliveries = [dict(row) for row in conn.execute(text(delivery_sql), {"task_id": resolved_task_id}).mappings()]

    print_section("DELIVERY REQUEST/RESPONSE PACKETS")
    if "request_packet" not in delivery_cols:
        print("gateway_webhook_deliveries 缺少 request_packet 列：当前库没有落 callback 实际请求包。")
    if "response_packet" not in delivery_cols:
        print("gateway_webhook_deliveries 缺少 response_packet 列：当前库没有落上游响应包。")
    if not deliveries:
        print("没有 gateway_webhook_deliveries 记录。")

    any_request_root_sh = False
    any_payload_root_sh = False
    completed_seen = False
    completed_files_seen = False
    delivery_request_files_seen = False

    for event in events:
        payload = jload(event.get("payload_json")) or {}
        if event.get("event_type") == "task.completed":
            completed_seen = True
            if file_items_from(payload):
                completed_files_seen = True
        for item in file_items_from(payload):
            if isinstance(item, dict) and str(item.get("download_url") or "").strip() == "/root/sh":
                any_payload_root_sh = True

    for delivery in deliveries:
        print()
        print(f"-- event_seq={delivery.get('event_seq')} event_type={delivery.get('event_type')} attempt={delivery.get('attempt')}")
        for key in (
            "status",
            "error",
            "delivery_mode",
            "callback_url",
            "response_observed",
            "response_status_code",
            "response_status",
            "response_content_type",
            "response_content_length",
            "response_body_bytes",
            "response_body_read_error",
            "response_body_truncated",
            "duration_ms",
            "created_at",
        ):
            print_kv(key, delivery.get(key))

        request_packet = delivery.get("request_packet")
        response_packet = delivery.get("response_packet")
        request_body = http_body(request_packet)
        request_body_json = jload(request_body)
        request_files = file_items_from(request_body_json)
        request_summary = file_summary(request_files)
        if request_files:
            delivery_request_files_seen = True
        if "/root/sh" in str(request_body):
            any_request_root_sh = True

        print("request_body_files_summary=" + jdump(request_summary))
        print()
        print("[callback_request_packet]")
        print(truncate(request_packet))
        print()
        print("[callback_request_body_pretty]")
        print(truncate(maybe_pretty_json(request_body)))
        print()
        print("[callback_response_body_pretty]")
        print(truncate(maybe_pretty_json(delivery.get("response_body"))))
        print()
        print("[callback_response_packet]")
        print(truncate(response_packet))

    print_section("DIAGNOSIS")
    print_kv("task_status", task.get("status"))
    print_kv("task_stage", task.get("stage"))
    print_kv("result_workspace_file_id", result_workspace_file_id)
    print_kv("runtime_result_workspace_file_ids", jdump(result_workspace_file_ids))
    print_kv("completed_event_seen", str(completed_seen).lower())
    print_kv("completed_event_files_seen", str(completed_files_seen).lower())
    print_kv("delivery_request_files_seen", str(delivery_request_files_seen).lower())
    print_kv("payload_contains_root_sh", str(any_payload_root_sh).lower())
    print_kv("request_contains_root_sh", str(any_request_root_sh).lower())

    if any_request_root_sh:
        print("结论=已确认实际发出的 callback HTTP 请求体里仍包含 /root/sh。优先检查内网运行镜像是否旧版本、是否仍有旧 hotfix 代码，或当前 task 是旧镜像生成的历史 outbox。")
    elif any_payload_root_sh:
        print("结论=outbox payload_json 里已有 /root/sh，但实际请求包没有或未记录。优先检查 delivery 记录是否为旧 schema/旧事件，或请求包是否缺失。")
    elif completed_seen and not completed_files_seen:
        print("结论=已有 task.completed，但 completed payload 没有 files[]。这通常表示没有真实 result_workspace_file_id，或文件元数据/用户归属校验未通过。")
    elif not completed_seen:
        print("结论=还没有 task.completed 事件。当前只能看到 accepted/progress 等中间态，不会返回最终 files[].download_url。")
    elif completed_files_seen and not delivery_request_files_seen:
        print("结论=completed outbox 有 files[]，但 delivery 请求包没有 files[] 或未记录。优先检查 request_packet 列是否存在、是否查看了正确的 delivery attempt。")
    else:
        print("结论=DB 侧未看到 /root/sh；如果上游仍显示写死路径，优先排查是否看错 task/request_id、上游缓存/旧记录，或内网有另一套 go-gateway 实例。")
PY

RESOLVED_TASK_ID="$(grep -m1 '^resolved_task_id=' "$REPORT_FILE" | cut -d= -f2- || true)"
if [[ "$SHOW_LOGS" == "true" && -n "$RESOLVED_TASK_ID" ]]; then
  log "抓取 go-gateway 日志：task_id=$RESOLVED_TASK_ID since=$SINCE"
  {
    echo "========== GO GATEWAY LOGS =========="
    k -n "$NS" logs "$GATEWAY_DEPLOY" --since="$SINCE" --all-containers 2>/dev/null \
      | grep -E "$RESOLVED_TASK_ID|callback事件|download_url|/root/sh|hotfix-|test\\.sh|结果文件元信息|result_workspace_file_id" \
      || true
  } | tee "$LOG_FILE" >/dev/null
fi

log "完成。主报告：$REPORT_FILE"
if [[ -f "$LOG_FILE" ]]; then
  log "日志报告：$LOG_FILE"
fi
