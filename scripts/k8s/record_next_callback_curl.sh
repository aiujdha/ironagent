#!/usr/bin/env bash
set -euo pipefail

NS="${NS:-ironagent}"
GATEWAY_DEPLOY="${GATEWAY_DEPLOY:-go-gateway}"
MYSQL_POD="${MYSQL_POD:-mysql-0}"
KUBECTL="${KUBECTL:-kubectl}"
TASK_ID="${TASK_ID:-}"
OUT_DIR="${OUT_DIR:-/tmp/go-gateway-callback-curl}"
WATCH_TIMEOUT_SECONDS="${WATCH_TIMEOUT_SECONDS:-300}"
DB_WAIT_SECONDS="${DB_WAIT_SECONDS:-20}"
EVENT_LIMIT="${EVENT_LIMIT:-10}"
REWRITE_CALLBACK_URL="${REWRITE_CALLBACK_URL:-}"
RUN_RESPONSE_COMPARE="${RUN_RESPONSE_COMPARE:-false}"
CURL_TIMEOUT_SECONDS="${CURL_TIMEOUT_SECONDS:-20}"
WATCH_REGEX="${WATCH_REGEX:-callback事件已入队|开始推送callback事件|callback事件推送成功}"

usage() {
  cat <<'EOF'
Usage:
  record_next_callback_curl.sh [options]

Capture the next go-gateway callback task_id from logs, query the outbox row,
write equivalent curl commands for the real callback HTTP POST, and compare
the real request with the rewritten curl request.

Options:
  --task-id <id>                  Do not watch logs; generate curl for this task_id.
  --namespace <ns>                Kubernetes namespace. Default: ironagent.
  --gateway-deploy <name>         Go Gateway deployment name. Default: go-gateway.
  --mysql-pod <pod>               MySQL pod name. Default: mysql-0.
  --out-dir <dir>                 Output directory. Default: /tmp/go-gateway-callback-curl.
  --timeout <seconds>             Log watch timeout. Default: 300.
  --db-wait <seconds>             Wait for outbox rows after seeing task_id. Default: 20.
  --event-limit <n>               Max callback events to render for the task. Default: 10.
  --rewrite-callback-url <url>    Generate the changed curl request with this URL instead of stored callback_url.
                                  Useful for replaying the same body to local :7090.
  --run-response-compare          Execute both real and changed curl requests, then compare HTTP responses.
                                  This sends POST requests again; use only when duplicate callback is acceptable.
  --curl-timeout <seconds>        Curl max-time for response compare. Default: 20.
  -h, --help                      Show this help.

Examples:
  # Start this first, then submit the next task. It prints curl commands.
  bash scripts/k8s/record_next_callback_curl.sh

  # Generate curl commands for an existing task.
  TASK_ID=gw-xxx bash scripts/k8s/record_next_callback_curl.sh
  bash scripts/k8s/record_next_callback_curl.sh --task-id gw-xxx --namespace ironagent

  # Replay the same body to a local receiver on port 7090.
  bash scripts/k8s/record_next_callback_curl.sh \
    --rewrite-callback-url 'http://127.0.0.1:7090/intelligent-assistant/h5/ironagent/taskcallback'

  # Also send both requests again and compare HTTP responses.
  bash scripts/k8s/record_next_callback_curl.sh \
    --task-id gw-xxx \
    --rewrite-callback-url 'http://127.0.0.1:7090/intelligent-assistant/h5/ironagent/taskcallback' \
    --run-response-compare
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

log() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --task-id)
      TASK_ID="${2:-}"
      shift 2
      ;;
    --namespace|-n)
      NS="${2:-}"
      shift 2
      ;;
    --gateway-deploy)
      GATEWAY_DEPLOY="${2:-}"
      shift 2
      ;;
    --mysql-pod)
      MYSQL_POD="${2:-}"
      shift 2
      ;;
    --out-dir)
      OUT_DIR="${2:-}"
      shift 2
      ;;
    --timeout)
      WATCH_TIMEOUT_SECONDS="${2:-}"
      shift 2
      ;;
    --db-wait)
      DB_WAIT_SECONDS="${2:-}"
      shift 2
      ;;
    --event-limit)
      EVENT_LIMIT="${2:-}"
      shift 2
      ;;
    --rewrite-callback-url)
      REWRITE_CALLBACK_URL="${2:-}"
      shift 2
      ;;
    --run-response-compare)
      RUN_RESPONSE_COMPARE="true"
      shift
      ;;
    --curl-timeout)
      CURL_TIMEOUT_SECONDS="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

command -v "$KUBECTL" >/dev/null 2>&1 || die "kubectl not found; set KUBECTL=/path/to/kubectl"
command -v python3 >/dev/null 2>&1 || die "python3 not found"

mkdir -p "$OUT_DIR"
WATCH_PY="$OUT_DIR/.watch_callback_task_id.py"
RENDER_PY="$OUT_DIR/.render_callback_curl.py"
RESPONSE_COMPARE_PY="$OUT_DIR/.compare_callback_responses.py"

write_python_helpers() {
  cat >"$WATCH_PY" <<'PY'
import re
import sys

event_re = re.compile(sys.argv[1])
patterns = [
    re.compile(r'"task_id"\s*:\s*"([^"]+)"'),
    re.compile(r'\btask_id=([^,\s]+)'),
]

for line in sys.stdin:
    if not event_re.search(line):
        continue
    for pattern in patterns:
        match = pattern.search(line)
        if match:
            print(match.group(1), flush=True)
            raise SystemExit(0)
raise SystemExit(1)
PY

  cat >"$RENDER_PY" <<'PY'
import hashlib
import json
import shlex
import sys

task_id = sys.argv[1]
rewrite_url = sys.argv[2].strip()
real_curl_file = sys.argv[3]
changed_curl_file = sys.argv[4]
compare_file = sys.argv[5]
request_jsonl_file = sys.argv[6]
rows_file = sys.argv[7]

def from_hex(value):
    value = value.strip()
    if not value:
        return ""
    return bytes.fromhex(value).decode("utf-8", errors="replace")

def shell_quote(value):
    return shlex.quote(value)

def callback_files(event):
    files = []
    for item in event.get("files") or []:
        file_id = str(item.get("file_id") or "").strip()
        if not file_id:
            continue
        out = {"fileId": file_id}
        filename = item.get("filename")
        if filename:
            out["fileName"] = filename
        size = item.get("size") or 0
        try:
            size_int = int(size)
        except Exception:
            size_int = 0
        if size_int > 0:
            out["size"] = str(size_int)
        mime_type = item.get("mime_type")
        if mime_type:
            out["mimeType"] = mime_type
        download_url = item.get("download_url")
        if download_url:
            out["download_url"] = download_url
        files.append(out)
    return files

def to_callback_payload(event):
    payload = {
        "taskId": event.get("task_id") or "",
        "status": event.get("status") or "",
        "stage": event.get("stage") or "",
        "replyText": event.get("reply_text") or "",
    }
    files = callback_files(event)
    if files:
        payload["files"] = files
    if event.get("error_code"):
        payload["errorCode"] = event["error_code"]
    if event.get("error_message"):
        payload["errorMessage"] = event["error_message"]
    if event.get("reason"):
        payload["errorReason"] = event["reason"]
    return payload

def curl_command(meta, url, payload):
    return "\n".join([
        "# task_id={task_id} event_id={event_id} event_type={event_type} event_seq={event_seq} outbox_status={status}".format(**meta),
        "curl -sS -i -X POST \\",
        "  {0} \\".format(shell_quote(url)),
        "  -H 'Content-Type: application/json' \\",
        "  -H 'Accept: application/json' \\",
        "  --data-raw {0}".format(shell_quote(payload)),
    ])

def body_hash(payload):
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def callback_file_summary(payload):
    files = payload.get("files") or []
    file_ids = []
    download_urls = []
    for item in files:
        if not isinstance(item, dict):
            continue
        file_id = str(item.get("fileId") or item.get("file_id") or "").strip()
        download_url = str(item.get("download_url") or "").strip()
        if file_id:
            file_ids.append(file_id)
        if download_url:
            download_urls.append(download_url)
    return {
        "files_len": len(files) if isinstance(files, list) else 0,
        "file_ids": file_ids,
        "download_urls": download_urls,
    }

real_commands = []
changed_commands = []
compare_sections = []
request_rows = []
with open(rows_file, "r", encoding="utf-8") as fh:
    for raw in fh:
        raw = raw.rstrip("\n")
        if not raw:
            continue
        parts = raw.split("\t")
        if len(parts) != 6:
            continue
        event_id = from_hex(parts[0])
        event_type = from_hex(parts[1])
        event_seq = parts[2]
        status = from_hex(parts[3])
        callback_url = from_hex(parts[4])
        event_json = from_hex(parts[5])
        event = json.loads(event_json)
        payload = json.dumps(to_callback_payload(event), ensure_ascii=False, separators=(",", ":"))
        changed_url = rewrite_url or callback_url
        meta = {
            "task_id": task_id,
            "event_id": event_id,
            "event_type": event_type,
            "event_seq": event_seq,
            "status": status,
        }
        payload_obj = to_callback_payload(event)
        file_summary = callback_file_summary(payload_obj)
        real_commands.append(curl_command(meta, callback_url, payload))
        changed_commands.append(curl_command(meta, changed_url, payload))
        compare_sections.append("\n".join([
            "event_id={0} event_type={1} event_seq={2} outbox_status={3}".format(event_id, event_type, event_seq, status),
            "validation=not_performed",
            "callback_files_len={0}".format(file_summary["files_len"]),
            "callback_file_ids={0}".format(",".join(file_summary["file_ids"])),
            "callback_download_urls={0}".format(",".join(file_summary["download_urls"])),
            "method.same=yes",
            "method.real=POST",
            "method.changed=POST",
            "url.same={0}".format("yes" if callback_url == changed_url else "no"),
            "url.real={0}".format(callback_url),
            "url.changed={0}".format(changed_url),
            "headers.same=yes",
            "headers.real=Content-Type: application/json; Accept: application/json",
            "headers.changed=Content-Type: application/json; Accept: application/json",
            "body.same=yes",
            "body.sha256.real={0}".format(body_hash(payload)),
            "body.sha256.changed={0}".format(body_hash(payload)),
            "body.real={0}".format(payload),
            "body.changed={0}".format(payload),
        ]))
        request_rows.append({
            "task_id": task_id,
            "event_id": event_id,
            "event_type": event_type,
            "event_seq": event_seq,
            "outbox_status": status,
            "real_url": callback_url,
            "changed_url": changed_url,
            "validation": "not_performed",
            "callback_files_len": file_summary["files_len"],
            "callback_file_ids": file_summary["file_ids"],
            "callback_download_urls": file_summary["download_urls"],
            "payload": payload,
        })

if not real_commands:
    raise SystemExit("no outbox rows were rendered")

real_output = "\n\n".join(real_commands) + "\n"
changed_output = "\n\n".join(changed_commands) + "\n"
compare_output = "\n\n".join(compare_sections) + "\n"

print("========== REAL GATEWAY CALLBACK REQUEST ==========")
print(real_output)
print("========== CHANGED CURL REQUEST ==========")
print(changed_output)
print("========== REQUEST COMPARISON ==========")
print(compare_output)

with open(real_curl_file, "w", encoding="utf-8") as fh:
    fh.write("#!/usr/bin/env bash\nset -euo pipefail\n\n")
    fh.write(real_output)
with open(changed_curl_file, "w", encoding="utf-8") as fh:
    fh.write("#!/usr/bin/env bash\nset -euo pipefail\n\n")
    fh.write(changed_output)
with open(compare_file, "w", encoding="utf-8") as fh:
    fh.write(compare_output)
with open(request_jsonl_file, "w", encoding="utf-8") as fh:
    for row in request_rows:
        fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
PY

  cat >"$RESPONSE_COMPARE_PY" <<'PY'
import hashlib
import json
import os
import subprocess
import sys

request_jsonl_file = sys.argv[1]
out_dir = sys.argv[2]
report_file = sys.argv[3]
curl_timeout = sys.argv[4]

def safe_name(value):
    value = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)
    return value[:120] or "event"

def sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def parse_headers(raw):
    blocks = [block for block in raw.replace("\r\n", "\n").split("\n\n") if block.strip()]
    if not blocks:
        return "", {}
    last = blocks[-1].splitlines()
    status_line = last[0].strip() if last else ""
    headers = {}
    for line in last[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return status_line, headers

def read_file(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except TypeError:
        with open(path, "r") as fh:
            return fh.read()

def write_file(path, value):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(value)

def run_curl(kind, row, event_dir):
    payload_file = os.path.join(event_dir, kind + "-payload.json")
    headers_file = os.path.join(event_dir, kind + "-headers.txt")
    body_file = os.path.join(event_dir, kind + "-body.txt")
    meta_file = os.path.join(event_dir, kind + "-meta.txt")
    stderr_file = os.path.join(event_dir, kind + "-stderr.txt")
    write_file(payload_file, row["payload"])
    url = row["real_url"] if kind == "real" else row["changed_url"]
    cmd = [
        "curl",
        "-sS",
        "--max-time", str(curl_timeout),
        "-D", headers_file,
        "-o", body_file,
        "-w", "http_code=%{http_code}\ntime_total=%{time_total}\nsize_download=%{size_download}\n",
        "-X", "POST",
        url,
        "-H", "Content-Type: application/json",
        "-H", "Accept: application/json",
        "--data-binary", "@" + payload_file,
    ]
    with open(stderr_file, "w", encoding="utf-8") as stderr:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr)
        out, _ = proc.communicate()
    meta = out.decode("utf-8", errors="replace")
    write_file(meta_file, meta)
    body = read_file(body_file) if os.path.exists(body_file) else ""
    header_raw = read_file(headers_file) if os.path.exists(headers_file) else ""
    status_line, headers = parse_headers(header_raw)
    info = {
        "kind": kind,
        "url": url,
        "return_code": proc.returncode,
        "status_line": status_line,
        "content_type": headers.get("content-type", ""),
        "body": body,
        "body_sha256": sha256_text(body),
        "meta": meta.strip(),
        "headers_file": headers_file,
        "body_file": body_file,
        "meta_file": meta_file,
        "stderr_file": stderr_file,
    }
    for line in meta.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            info[key.strip()] = value.strip()
    return info

sections = []
with open(request_jsonl_file, "r", encoding="utf-8") as fh:
    for raw in fh:
        raw = raw.strip()
        if not raw:
            continue
        row = json.loads(raw)
        event_name = safe_name("{0}-{1}-{2}".format(row["event_seq"], row["event_type"], row["event_id"]))
        event_dir = os.path.join(out_dir, event_name)
        os.makedirs(event_dir, exist_ok=True)
        real = run_curl("real", row, event_dir)
        changed = run_curl("changed", row, event_dir)
        sections.append("\n".join([
            "event_id={0} event_type={1} event_seq={2} outbox_status={3}".format(
                row["event_id"], row["event_type"], row["event_seq"], row["outbox_status"]
            ),
            "real.url={0}".format(real["url"]),
            "changed.url={0}".format(changed["url"]),
            "response.return_code.same={0}".format("yes" if real["return_code"] == changed["return_code"] else "no"),
            "response.return_code.real={0}".format(real["return_code"]),
            "response.return_code.changed={0}".format(changed["return_code"]),
            "response.http_code.same={0}".format("yes" if real.get("http_code", "") == changed.get("http_code", "") else "no"),
            "response.http_code.real={0}".format(real.get("http_code", "")),
            "response.http_code.changed={0}".format(changed.get("http_code", "")),
            "response.status_line.real={0}".format(real["status_line"]),
            "response.status_line.changed={0}".format(changed["status_line"]),
            "response.content_type.same={0}".format("yes" if real["content_type"] == changed["content_type"] else "no"),
            "response.content_type.real={0}".format(real["content_type"]),
            "response.content_type.changed={0}".format(changed["content_type"]),
            "response.body.same={0}".format("yes" if real["body"] == changed["body"] else "no"),
            "response.body_sha256.real={0}".format(real["body_sha256"]),
            "response.body_sha256.changed={0}".format(changed["body_sha256"]),
            "response.body.real={0}".format(real["body"].strip()),
            "response.body.changed={0}".format(changed["body"].strip()),
            "response.time_total.real={0}".format(real.get("time_total", "")),
            "response.time_total.changed={0}".format(changed.get("time_total", "")),
            "response.files.real.headers={0}".format(real["headers_file"]),
            "response.files.real.body={0}".format(real["body_file"]),
            "response.files.changed.headers={0}".format(changed["headers_file"]),
            "response.files.changed.body={0}".format(changed["body_file"]),
        ]))

report = "\n\n".join(sections) + "\n"
print("========== RESPONSE COMPARISON ==========")
print(report)
write_file(report_file, report)
PY
}

write_python_helpers

sql_escape() {
  printf "%s" "$1" | sed "s/'/''/g"
}

show_deploy_hint() {
  echo "ERROR: deployment '$GATEWAY_DEPLOY' was not found in namespace '$NS'." >&2
  echo "Check the real namespace/deployment with:" >&2
  echo "  $KUBECTL get ns" >&2
  echo "  $KUBECTL -n '$NS' get deploy" >&2
  echo "Then pass them explicitly, for example:" >&2
  echo "  bash $0 --namespace ironagent --gateway-deploy <real-go-gateway-deploy>" >&2
  echo "If you already know the task id, skip log watching:" >&2
  echo "  bash $0 --namespace ironagent --task-id <gw-task-id>" >&2
  echo "Do not set NS/TASK_ID on separate shell lines unless you export them." >&2
  echo "Use either:" >&2
  echo "  export NS=ironagent; export TASK_ID=<gw-task-id>; bash $0" >&2
  echo "or:" >&2
  echo "  NS=ironagent TASK_ID=<gw-task-id> bash $0" >&2
}

watch_next_task_id() {
  local log_file="$OUT_DIR/go-gateway-watch-$(date '+%Y%m%d-%H%M%S').log"
  log "watching next callback log from deploy/$GATEWAY_DEPLOY in namespace $NS"
  log "raw matched logs will be saved to $log_file"

  if ! "$KUBECTL" -n "$NS" get deploy "$GATEWAY_DEPLOY" >/dev/null 2>&1; then
    show_deploy_hint
    exit 1
  fi

  local task
  if command -v timeout >/dev/null 2>&1; then
    task="$(
      timeout "$WATCH_TIMEOUT_SECONDS" "$KUBECTL" -n "$NS" logs -f "deploy/$GATEWAY_DEPLOY" --since=1s 2>&1 \
        | tee "$log_file" \
        | python3 "$WATCH_PY" "$WATCH_REGEX"
    )" || true
  else
    log "timeout command not found; log watch will not auto-timeout"
    task="$(
      "$KUBECTL" -n "$NS" logs -f "deploy/$GATEWAY_DEPLOY" --since=1s 2>&1 \
        | tee "$log_file" \
        | python3 "$WATCH_PY" "$WATCH_REGEX"
    )" || true
  fi

  [[ -n "$task" ]] || die "did not capture a callback task_id within ${WATCH_TIMEOUT_SECONDS}s"
  printf "%s" "$task"
}

query_outbox_rows() {
  local task_id="$1"
  local escaped
  escaped="$(sql_escape "$task_id")"

  cat <<SQL | "$KUBECTL" -n "$NS" exec -i "$MYSQL_POD" -- sh -lc \
    'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --default-character-set=utf8mb4 -uroot "$MYSQL_DATABASE" --batch --raw --skip-column-names' 2>/dev/null
SELECT
  HEX(COALESCE(event_id, '')),
  HEX(COALESCE(event_type, '')),
  COALESCE(event_seq, 0),
  HEX(COALESCE(status, '')),
  HEX(COALESCE(callback_url, '')),
  HEX(COALESCE(CAST(payload_json AS CHAR CHARACTER SET utf8mb4), ''))
FROM gateway_event_outbox
WHERE task_id = '$escaped'
ORDER BY event_seq ASC, created_at ASC
LIMIT $EVENT_LIMIT;
SQL
}

render_curl_commands() {
  local task_id="$1"
  local rows_file="$2"
  local real_curl_file="$3"
  local changed_curl_file="$4"
  local compare_file="$5"
  local request_jsonl_file="$6"

  python3 "$RENDER_PY" "$task_id" "$REWRITE_CALLBACK_URL" "$real_curl_file" "$changed_curl_file" "$compare_file" "$request_jsonl_file" "$rows_file"
}

run_response_compare() {
  local request_jsonl_file="$1"
  local response_dir="$2"
  local response_compare_file="$3"

  command -v curl >/dev/null 2>&1 || die "curl not found; response compare requires curl"
  mkdir -p "$response_dir"
  python3 "$RESPONSE_COMPARE_PY" "$request_jsonl_file" "$response_dir" "$response_compare_file" "$CURL_TIMEOUT_SECONDS"
}

if [[ -z "$TASK_ID" ]]; then
  TASK_ID="$(watch_next_task_id)"
fi

log "captured task_id=$TASK_ID"

stamp="$(date '+%Y%m%d-%H%M%S')"
rows_file="$OUT_DIR/callback-outbox-${TASK_ID}-${stamp}.tsv"
real_curl_file="$OUT_DIR/callback-curl-real-${TASK_ID}-${stamp}.sh"
changed_curl_file="$OUT_DIR/callback-curl-changed-${TASK_ID}-${stamp}.sh"
compare_file="$OUT_DIR/callback-request-compare-${TASK_ID}-${stamp}.txt"
request_jsonl_file="$OUT_DIR/callback-requests-${TASK_ID}-${stamp}.jsonl"
response_dir="$OUT_DIR/callback-responses-${TASK_ID}-${stamp}"
response_compare_file="$OUT_DIR/callback-response-compare-${TASK_ID}-${stamp}.txt"

rows=""
for _ in $(seq 1 "$DB_WAIT_SECONDS"); do
  rows="$(query_outbox_rows "$TASK_ID" || true)"
  if [[ -n "$rows" ]]; then
    break
  fi
  sleep 1
done

[[ -n "$rows" ]] || die "no gateway_event_outbox rows found for task_id=$TASK_ID after ${DB_WAIT_SECONDS}s"
printf '%s\n' "$rows" >"$rows_file"

render_curl_commands "$TASK_ID" "$rows_file" "$real_curl_file" "$changed_curl_file" "$compare_file" "$request_jsonl_file"
chmod +x "$real_curl_file" "$changed_curl_file"

if [[ "$RUN_RESPONSE_COMPARE" == "true" ]]; then
  log "running response compare; this will send POST requests again"
  run_response_compare "$request_jsonl_file" "$response_dir" "$response_compare_file"
fi

log "outbox rows: $rows_file"
log "request jsonl file: $request_jsonl_file"
log "real gateway curl file: $real_curl_file"
log "changed curl file: $changed_curl_file"
log "request compare file: $compare_file"
if [[ "$RUN_RESPONSE_COMPARE" == "true" ]]; then
  log "response dir: $response_dir"
  log "response compare file: $response_compare_file"
fi
