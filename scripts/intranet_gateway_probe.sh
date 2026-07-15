#!/usr/bin/env bash
set -u

BASE_URL=""
USER_ID=""
UPLOAD_FILE=""
MESSAGE="内网联调测试，请简短回复 OK"
NAMESPACE="ironagent-kf"
API_KEY=""
TARGET_BUCKET="temp"
SKIP_K8S="false"
POLL_COUNT="6"
POLL_INTERVAL="3"

usage() {
  cat <<'EOF'
Usage:
  scripts/intranet_gateway_probe.sh \
    --base-url http://<gateway-host>:30808 \
    --user-id <user_id> \
    --file /path/to/test-file \
    [--message "测试消息"] \
    [--target-bucket temp|persistent] \
    [--api-key <key>] \
    [--namespace ironagent-kf] \
    [--skip-k8s]

What it checks:
  1. Gateway /api/v1/health
  2. User assistant binding: /api/v1/users/{user_id}/assistants
  3. Public upload: /api/v1/files/upload
  4. File metadata and download through Gateway
  5. Chat without files and chat with uploaded file_id
  6. If kubectl is available: Go Gateway callback ConfigMap and callback logs

Notes:
  - This script is read-only except for creating test tasks/files through public APIs.
  - Without X-API-Key, /api/v1/files/upload only accepts user_id and file,
    and Gateway stores the file in temp.
  - To test persistent upload, use --target-bucket persistent together with
    a Gateway API key that can call the public file upload route.
  - Public /api/v1/chat does not accept callback_url in request body.
    Callback must be configured in Go Gateway via GO_GATEWAY_DEFAULT_CALLBACK_URL
    or go-gateway.json default_callback_url.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --base-url)
      BASE_URL="${2:-}"
      shift 2
      ;;
    --user-id)
      USER_ID="${2:-}"
      shift 2
      ;;
    --file)
      UPLOAD_FILE="${2:-}"
      shift 2
      ;;
    --message)
      MESSAGE="${2:-}"
      shift 2
      ;;
    --api-key)
      API_KEY="${2:-}"
      shift 2
      ;;
    --target-bucket|--bucket)
      TARGET_BUCKET="${2:-}"
      shift 2
      ;;
    --namespace)
      NAMESPACE="${2:-}"
      shift 2
      ;;
    --poll-count)
      POLL_COUNT="${2:-}"
      shift 2
      ;;
    --poll-interval)
      POLL_INTERVAL="${2:-}"
      shift 2
      ;;
    --skip-k8s)
      SKIP_K8S="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [ -z "$BASE_URL" ] || [ -z "$USER_ID" ] || [ -z "$UPLOAD_FILE" ]; then
  usage
  exit 2
fi

if [ ! -f "$UPLOAD_FILE" ]; then
  echo "Upload file not found: $UPLOAD_FILE" >&2
  exit 2
fi

case "$TARGET_BUCKET" in
  temp|persistent)
    ;;
  *)
    echo "Invalid --target-bucket: $TARGET_BUCKET. Expected temp or persistent." >&2
    exit 2
    ;;
esac

if [ "$TARGET_BUCKET" != "temp" ] && [ -z "$API_KEY" ]; then
  echo "Persistent upload requires --api-key; unauthenticated public upload only supports temp." >&2
  exit 2
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required" >&2
  exit 2
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required for JSON parsing" >&2
  exit 2
fi

BASE_URL="${BASE_URL%/}"
TMP_DIR="$(mktemp -d -t ironagent-gateway-probe.XXXXXX)"
SUMMARY_FILE="$TMP_DIR/summary.txt"
TASK_IDS_FILE="$TMP_DIR/task-ids.tsv"
GATEWAY_CALLBACK_URL=""
GATEWAY_CALLBACK_SOURCE=""
GATEWAY_CALLBACK_ALLOWED_HOSTS=""
GATEWAY_CALLBACK_ALLOW_PRIVATE=""
trap 'echo "Artifacts kept at: '"$TMP_DIR"'"' EXIT

log() {
  printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

record() {
  printf '%s\n' "$*" | tee -a "$SUMMARY_FILE"
}

new_id() {
  python3 - <<'PY'
import uuid
print(uuid.uuid4().hex)
PY
}

json_get() {
  python3 - "$1" "$2" <<'PY'
import json
import sys

path = sys.argv[1].split(".")
filename = sys.argv[2]
try:
    with open(filename, "r", encoding="utf-8") as fh:
        value = json.load(fh)
except Exception:
    sys.exit(0)

for key in path:
    if isinstance(value, dict):
        value = value.get(key)
    else:
        value = None
    if value is None:
        sys.exit(0)

if isinstance(value, (dict, list)):
    print(json.dumps(value, ensure_ascii=False))
else:
    print(value)
PY
}

extract_task_id() {
  python3 - "$1" <<'PY'
import json
import re
import sys

raw = ""
try:
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        raw = fh.read()
    data = json.loads(raw)
except Exception:
    match = re.search(r'"(?:task_id|taskId|taskID)"\s*:\s*"([^"]+)"', raw)
    if match:
        print(match.group(1).strip())
    sys.exit(0)

candidates = [
    data.get("taskId") if isinstance(data, dict) else None,
    data.get("taskID") if isinstance(data, dict) else None,
    data.get("task_id") if isinstance(data, dict) else None,
    data.get("id") if isinstance(data, dict) else None,
]
if isinstance(data, dict) and isinstance(data.get("task"), dict):
    candidates.extend([
        data["task"].get("taskId"),
        data["task"].get("taskID"),
        data["task"].get("task_id"),
        data["task"].get("id"),
    ])

for item in candidates:
    if isinstance(item, str) and item.strip():
        print(item.strip())
        break
else:
    match = re.search(r'"(?:task_id|taskId|taskID)"\s*:\s*"([^"]+)"', raw)
    if match:
        print(match.group(1).strip())
PY
}

extract_task_id_from_logs() {
  python3 - "$1" "$2" <<'PY'
import json
import re
import sys

filename, request_id = sys.argv[1:]
try:
    with open(filename, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
except Exception:
    sys.exit(0)

for line in lines:
    if request_id not in line:
        continue
    try:
        data = json.loads(line)
    except Exception:
        data = None
    if isinstance(data, dict):
        for key in ("task_id", "taskId", "taskID"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                print(value.strip())
                sys.exit(0)
    match = re.search(r'"(?:task_id|taskId|taskID)"\s*:\s*"([^"]+)"', line)
    if match:
        print(match.group(1).strip())
        sys.exit(0)
PY
}

lookup_task_id_from_gateway_logs() {
  request_id="$1"
  label="$2"
  if [ "$SKIP_K8S" = "true" ] || ! command -v kubectl >/dev/null 2>&1; then
    return
  fi
  lookup_log="$TMP_DIR/chat-${label}-task-id-lookup.log"
  kubectl -n "$NAMESPACE" logs deploy/go-gateway --since=10m \
    | grep -F "$request_id" > "$lookup_log" 2>/dev/null || true
  extract_task_id_from_logs "$lookup_log" "$request_id"
}

json_file_get() {
  python3 - "$1" "$2" <<'PY'
import json
import sys

filename, field = sys.argv[1:]
try:
    with open(filename, "r", encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    sys.exit(0)

value = data.get(field) if isinstance(data, dict) else None
if value is None:
    sys.exit(0)
if isinstance(value, (dict, list)):
    print(json.dumps(value, ensure_ascii=False))
else:
    print(value)
PY
}

extract_logged_callback_hosts() {
  python3 - "$1" <<'PY'
import re
import sys

text = ""
try:
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        text = fh.read()
except Exception:
    sys.exit(0)

hosts = set()
for pattern in (
    r'callback_host=("[^"]+"|[^,\s]+)',
    r'"callback_host"\s*:\s*"([^"]+)"',
):
    for value in re.findall(pattern, text):
        hosts.add(value.strip().strip('"'))

print(",".join(sorted(item for item in hosts if item)))
PY
}

load_gateway_callback_config() {
  if [ "$SKIP_K8S" = "true" ] || ! command -v kubectl >/dev/null 2>&1; then
    record "Gateway runtime callback URL: <unknown> (--skip-k8s set or kubectl unavailable)"
    return
  fi

  log "0. Gateway runtime callback config"
  runtime_env="$TMP_DIR/k8s-go-gateway-runtime-env.txt"
  runtime_config="$TMP_DIR/k8s-go-gateway-runtime-config.json"

  if kubectl -n "$NAMESPACE" exec deploy/go-gateway -- sh -lc \
    'printenv | grep -E "GO_GATEWAY_DEFAULT_CALLBACK_URL|GO_GATEWAY_CALLBACK_ALLOWED_HOSTS|GO_GATEWAY_CALLBACK_ALLOW_PRIVATE_HOSTS|GO_GATEWAY_CONFIG_FILE" || true' \
    > "$runtime_env" 2>"$TMP_DIR/k8s-go-gateway-runtime-env.err"; then
    record "Gateway runtime env:"
    if [ -s "$runtime_env" ]; then
      cat "$runtime_env" | tee -a "$SUMMARY_FILE"
    else
      record "<empty>"
    fi
  else
    record "K8s: cannot exec deploy/go-gateway to read runtime env."
  fi

  if kubectl -n "$NAMESPACE" exec deploy/go-gateway -- sh -lc \
    '[ -n "${GO_GATEWAY_CONFIG_FILE:-}" ] && [ -f "$GO_GATEWAY_CONFIG_FILE" ] && cat "$GO_GATEWAY_CONFIG_FILE" || true' \
    > "$runtime_config" 2>"$TMP_DIR/k8s-go-gateway-runtime-config.err"; then
    if [ -s "$runtime_config" ]; then
      record "Gateway runtime config file:"
      cat "$runtime_config" | tee -a "$SUMMARY_FILE"
    fi

  GATEWAY_CALLBACK_URL="$(awk -F= '/^GO_GATEWAY_DEFAULT_CALLBACK_URL=/{print substr($0, index($0, "=") + 1); exit}' "$runtime_env" 2>/dev/null || true)"
  GATEWAY_CALLBACK_ALLOWED_HOSTS="$(awk -F= '/^GO_GATEWAY_CALLBACK_ALLOWED_HOSTS=/{print substr($0, index($0, "=") + 1); exit}' "$runtime_env" 2>/dev/null || true)"
  GATEWAY_CALLBACK_ALLOW_PRIVATE="$(awk -F= '/^GO_GATEWAY_CALLBACK_ALLOW_PRIVATE_HOSTS=/{print substr($0, index($0, "=") + 1); exit}' "$runtime_env" 2>/dev/null || true)"
  if [ -n "$GATEWAY_CALLBACK_URL" ]; then
    GATEWAY_CALLBACK_SOURCE="env:GO_GATEWAY_DEFAULT_CALLBACK_URL"
  elif [ -s "$runtime_config" ]; then
    GATEWAY_CALLBACK_URL="$(json_file_get "$runtime_config" default_callback_url)"
    GATEWAY_CALLBACK_ALLOWED_HOSTS="${GATEWAY_CALLBACK_ALLOWED_HOSTS:-$(json_file_get "$runtime_config" callback_allowed_hosts)}"
    GATEWAY_CALLBACK_ALLOW_PRIVATE="${GATEWAY_CALLBACK_ALLOW_PRIVATE:-$(json_file_get "$runtime_config" callback_allow_private_hosts)}"
    if [ -n "$GATEWAY_CALLBACK_URL" ]; then
      GATEWAY_CALLBACK_SOURCE="config:default_callback_url"
    fi

  record "Gateway effective callback URL: ${GATEWAY_CALLBACK_URL:-<empty>}"
  record "Gateway effective callback source: ${GATEWAY_CALLBACK_SOURCE:-<none>}"
  record "Gateway callback allowed hosts: ${GATEWAY_CALLBACK_ALLOWED_HOSTS:-<empty>}"
  record "Gateway callback allow private hosts: ${GATEWAY_CALLBACK_ALLOW_PRIVATE:-<empty>}"
  if [ -z "$GATEWAY_CALLBACK_URL" ]; then
    record "DIAGNOSIS: Gateway default callback URL is empty in the running pod; public /api/v1/chat will not register callback."
  fi
}

summarize_callback_logs() {
  log_file="$1"
  if [ ! -s "$TASK_IDS_FILE" ]; then
    record "Callback summary: no task_id was created by this probe."
    return
  fi
  if [ ! -s "$log_file" ]; then
    record "Callback summary: no go-gateway callback log was captured."
    return
  fi

  log "8. Callback target summary by task"
  while IFS="$(printf '\t')" read -r label request_id task_id; do
    [ -n "$task_id" ] || continue
    task_log="$TMP_DIR/callback-${label}-${task_id}.log"
    grep -F "$task_id" "$log_file" | grep -E 'callback事件|webhook enqueue|dead-letter|callback_url' > "$task_log" || true
    logged_hosts="$(extract_logged_callback_hosts "$task_log")"
    record "callback task label=$label task_id=$task_id request_id=$request_id configured_url=${GATEWAY_CALLBACK_URL:-<unknown>} logged_callback_host=${logged_hosts:-<none>}"
    if [ -s "$task_log" ]; then
      tail -40 "$task_log" | tee -a "$SUMMARY_FILE"
    else
      record "DIAGNOSIS: no callback enqueue/delivery log matched task_id=$task_id. If the task returned 202, check whether the running Gateway has default_callback_url configured and whether this pod handled the request."
    fi
  done < "$TASK_IDS_FILE"
}

curl_common=(-H "Accept: application/json")
if [ -n "$API_KEY" ]; then
  curl_common+=(-H "X-API-Key: $API_KEY")
fi

load_gateway_callback_config

request_json() {
  method="$1"
  path="$2"
  body_file="${3:-}"
  out_body="$4"
  out_headers="$5"
  status_file="$6"

  if [ -n "$body_file" ]; then
    status="$(
      curl -sS -o "$out_body" -D "$out_headers" -w "%{http_code}" \
        -X "$method" "$BASE_URL$path" \
        "${curl_common[@]}" \
        -H "Content-Type: application/json" \
        --data-binary "@$body_file"
    )"
  else
    status="$(
      curl -sS -o "$out_body" -D "$out_headers" -w "%{http_code}" \
        -X "$method" "$BASE_URL$path" \
        "${curl_common[@]}"
    )"
  fi
  printf '%s' "$status" > "$status_file"
}

pretty_print_response() {
  label="$1"
  status_file="$2"
  body_file="$3"
  status="$(cat "$status_file" 2>/dev/null || true)"
  record "$label HTTP $status"
  python3 - "$body_file" <<'PY' || cat "$body_file"
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        data = json.load(fh)
    print(json.dumps(data, ensure_ascii=False, indent=2))
except Exception:
    raise SystemExit(1)
PY
}

log "1. Health check"
request_json GET "/api/v1/health" "" "$TMP_DIR/health.body" "$TMP_DIR/health.headers" "$TMP_DIR/health.status"
pretty_print_response "health" "$TMP_DIR/health.status" "$TMP_DIR/health.body"

log "2. User binding check"
encoded_user="$(python3 - "$USER_ID" <<'PY'
from urllib.parse import quote
import sys
print(quote(sys.argv[1], safe=""))
PY
)"
request_json GET "/api/v1/users/${encoded_user}/assistants" "" "$TMP_DIR/bindings.body" "$TMP_DIR/bindings.headers" "$TMP_DIR/bindings.status"
pretty_print_response "bindings" "$TMP_DIR/bindings.status" "$TMP_DIR/bindings.body"
BINDING_STATUS="$(cat "$TMP_DIR/bindings.status")"
if [ "$BINDING_STATUS" = "404" ]; then
  if grep -q 'user not found' "$TMP_DIR/bindings.body"; then
    record "DIAGNOSIS: user_id=$USER_ID does not exist in southbound users; chat will not create a task or callback."
  else
    record "DIAGNOSIS: binding route returned 404. On a public Go Gateway this route may simply be unavailable; use upload/chat southbound_error details to judge user existence."
  fi
elif [ "$BINDING_STATUS" != "200" ]; then
  record "DIAGNOSIS: binding query failed; fix user/binding before judging callback."
else
  BINDING_TOTAL="$(json_get total "$TMP_DIR/bindings.body")"
  record "Binding total: ${BINDING_TOTAL:-unknown}"
fi

log "3. Upload file through public Gateway"
upload_form=(-F "user_id=$USER_ID" -F "file=@$UPLOAD_FILE")
if [ "$TARGET_BUCKET" != "temp" ]; then
  upload_form+=(-F "target_bucket=$TARGET_BUCKET")
fi
UPLOAD_STATUS="$(
  curl -sS -o "$TMP_DIR/upload.body" -D "$TMP_DIR/upload.headers" -w "%{http_code}" \
    -X POST "$BASE_URL/api/v1/files/upload" \
    "${curl_common[@]}" \
    "${upload_form[@]}"
)"
printf '%s' "$UPLOAD_STATUS" > "$TMP_DIR/upload.status"
pretty_print_response "upload" "$TMP_DIR/upload.status" "$TMP_DIR/upload.body"
FILE_ID="$(json_get file_id "$TMP_DIR/upload.body")"
if [ -z "$FILE_ID" ]; then
  FILE_ID="$(json_get id "$TMP_DIR/upload.body")"
fi

if [ -z "$FILE_ID" ]; then
  record "DIAGNOSIS: upload did not return file_id; stop file workspace verification."
else
  record "Uploaded file_id: $FILE_ID"

  log "4. Read file metadata through Gateway"
  encoded_file="$(python3 - "$FILE_ID" <<'PY'
from urllib.parse import quote
import sys
print(quote(sys.argv[1], safe=""))
PY
)"
  request_json GET "/api/v1/files/${encoded_file}?user_id=${encoded_user}" "" "$TMP_DIR/file-meta.body" "$TMP_DIR/file-meta.headers" "$TMP_DIR/file-meta.status"
  pretty_print_response "file metadata" "$TMP_DIR/file-meta.status" "$TMP_DIR/file-meta.body"

  log "5. Download file back through Gateway"
  DOWNLOAD_STATUS="$(
    curl -sS -o "$TMP_DIR/downloaded-file" -D "$TMP_DIR/download.headers" -w "%{http_code}" \
      "$BASE_URL/api/v1/files/${encoded_file}/download?user_id=${encoded_user}" \
      "${curl_common[@]}"
  )"
  printf '%s' "$DOWNLOAD_STATUS" > "$TMP_DIR/download.status"
  DOWNLOADED_BYTES="$(wc -c < "$TMP_DIR/downloaded-file" 2>/dev/null || echo 0)"
  record "download HTTP $DOWNLOAD_STATUS, bytes=$DOWNLOADED_BYTES"
fi

submit_chat() {
  label="$1"
  include_file="$2"
  request_id="probe-${label}-$(new_id)"
  body="$TMP_DIR/chat-${label}.json"
  if [ "$include_file" = "true" ] && [ -n "${FILE_ID:-}" ]; then
    python3 - "$body" "$request_id" "$USER_ID" "$MESSAGE" "$FILE_ID" <<'PY'
import json
import sys

path, request_id, user_id, message, file_id = sys.argv[1:]
payload = {
    "request_id": request_id,
    "user_id": user_id,
    "conversation_id": "probe-conv-" + request_id,
    "message": message,
    "files": [file_id],
}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=False)
PY
  else
    python3 - "$body" "$request_id" "$USER_ID" "$MESSAGE" <<'PY'
import json
import sys

path, request_id, user_id, message = sys.argv[1:]
payload = {
    "request_id": request_id,
    "user_id": user_id,
    "conversation_id": "probe-conv-" + request_id,
    "message": message,
}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=False)
PY
  fi

  log "6. Submit chat: $label"
  request_json POST "/api/v1/chat" "$body" "$TMP_DIR/chat-${label}.body" "$TMP_DIR/chat-${label}.headers" "$TMP_DIR/chat-${label}.status"
  pretty_print_response "chat $label" "$TMP_DIR/chat-${label}.status" "$TMP_DIR/chat-${label}.body"

  task_id="$(extract_task_id "$TMP_DIR/chat-${label}.body")"
  if [ -z "$task_id" ]; then
    task_id="$(lookup_task_id_from_gateway_logs "$request_id" "$label")"
    if [ -n "$task_id" ]; then
      record "chat $label task_id recovered from Gateway logs by request_id: $task_id"
    fi
  if [ -z "$task_id" ]; then
    record "DIAGNOSIS: chat $label did not return task_id; no callback should be expected."
    return
  fi
  record "chat $label task_id: $task_id"
  record "chat $label expected callback URL: ${GATEWAY_CALLBACK_URL:-<unknown>}"
  printf '%s\t%s\t%s\n' "$label" "$request_id" "$task_id" >> "$TASK_IDS_FILE"

  i=1
  while [ "$i" -le "$POLL_COUNT" ]; do
    sleep "$POLL_INTERVAL"
    request_json GET "/api/v1/tasks/${task_id}" "" "$TMP_DIR/task-${label}-${i}.body" "$TMP_DIR/task-${label}-${i}.headers" "$TMP_DIR/task-${label}-${i}.status"
    status="$(json_get status "$TMP_DIR/task-${label}-${i}.body")"
    stage="$(json_get stage "$TMP_DIR/task-${label}-${i}.body")"
    error_code="$(json_get error_code "$TMP_DIR/task-${label}-${i}.body")"
    error_message="$(json_get error_message "$TMP_DIR/task-${label}-${i}.body")"
    record "task poll $label #$i HTTP $(cat "$TMP_DIR/task-${label}-${i}.status") status=${status:-?} stage=${stage:-?} error_code=${error_code:-} error_message=${error_message:-}"
    case "$status" in
      completed|failed|cancelled|rejected|expired|timeout)
        break
        ;;
    esac
    i=$((i + 1))
  done
}

submit_chat "no-file" "false"
if [ -n "${FILE_ID:-}" ]; then
  submit_chat "with-file" "true"
else
  record "Skip chat with file because upload did not return file_id."
fi

if [ "$SKIP_K8S" != "true" ] && command -v kubectl >/dev/null 2>&1; then
  log "7. Optional Kubernetes callback config and logs"
  if kubectl -n "$NAMESPACE" get cm go-gateway-config >/dev/null 2>&1; then
    kubectl -n "$NAMESPACE" get cm go-gateway-config -o yaml \
      | grep -E 'GO_GATEWAY_DEFAULT_CALLBACK_URL|GO_GATEWAY_CALLBACK_ALLOWED_HOSTS|GO_GATEWAY_CALLBACK_ALLOW_PRIVATE_HOSTS|GO_GATEWAY_CONFIG_FILE' \
      | tee "$TMP_DIR/k8s-go-gateway-config.txt" || true
  else
    record "K8s: cannot read configmap go-gateway-config in namespace $NAMESPACE"
  fi

  if kubectl -n "$NAMESPACE" get cm go-gateway-file-config >/dev/null 2>&1; then
    kubectl -n "$NAMESPACE" get cm go-gateway-file-config -o yaml \
      | grep -E 'default_callback_url|callback_allowed_hosts|callback_allow_private_hosts|callback_client_id|callback_max_attempts' \
      | tee "$TMP_DIR/k8s-go-gateway-file-config.txt" || true
  else
    record "K8s: cannot read configmap go-gateway-file-config in namespace $NAMESPACE"
  fi

  if kubectl -n "$NAMESPACE" logs deploy/go-gateway --since=2h >/dev/null 2>&1; then
    kubectl -n "$NAMESPACE" logs deploy/go-gateway --since=2h \
      | grep -E '提交任务|callback事件|webhook enqueue|dead-letter|callback_url|user not found|southbound_error' \
      | tail -120 \
      | tee "$TMP_DIR/k8s-go-gateway-callback.log" || true
    summarize_callback_logs "$TMP_DIR/k8s-go-gateway-callback.log"
  else
    record "K8s: cannot read deploy/go-gateway logs in namespace $NAMESPACE"
  fi
else
  record "Skip Kubernetes checks. Reason: --skip-k8s set or kubectl unavailable."
fi

log "Diagnosis hints"
cat <<'EOF' | tee -a "$SUMMARY_FILE"
- If binding query is 404 user not found: create/bind the user first. Chat will not callback before task creation succeeds.
- If upload returns file_id but metadata/download fails: file metadata and actual workspace write path are inconsistent; check southbound /api/v1/workspace-files and assistant workspace root.
- If upload metadata/download works but UI workspace is empty: you are likely viewing a different assistant workspace/root, or UI list/index did not sync filesystem state.
- If chat returns 202 task_id but no callback logs: compare "chat expected callback URL" with "Gateway effective callback URL"; if it is empty or wrong, Gateway skips or sends callback to another address.
- If callback logs only show callback_host: use "Gateway effective callback URL" as the full target address and callback_host as the runtime delivery host observed by Gateway.
- If chat fails with callback_url host errors: fix GO_GATEWAY_CALLBACK_ALLOWED_HOSTS and, for intranet IPs, set GO_GATEWAY_CALLBACK_ALLOW_PRIVATE_HOSTS=true during testing.
- If callback is enqueued but fails: check callback receiver reachability from the Gateway Pod and whether receiver returns HTTP 2xx.
EOF

log "Done"
record "Summary file: $SUMMARY_FILE"
