#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${GATEWAY_URL:-http://127.0.0.1:8080}"
USER_ID="${USER_ID:-}"
ASSISTANT_ID="${ASSISTANT_ID:-}"
MESSAGE="${MESSAGE:-}"
MESSAGE_FILE=""
REQUEST_ID="${REQUEST_ID:-}"
CONVERSATION_ID="${CONVERSATION_ID:-}"
CLIENT_ID="${CLIENT_ID:-curl-chat-test}"
API_KEY="${API_KEY:-}"
HMAC_SECRET="${HMAC_SECRET:-}"
CALLBACK_URL="${CALLBACK_URL:-}"
FILES_CSV="${FILES:-}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-0}"
POLL=0
POLL_INTERVAL=5
POLL_TIMEOUT=180
OUT_DIR="${OUT_DIR:-}"

usage() {
  cat <<'EOF'
Usage:
  scripts/curl_chat_test.sh --url URL --user-id USER --message TEXT [options]

Options:
  --url URL                 Gateway base URL. Default: $GATEWAY_URL or http://127.0.0.1:8080
  --user-id ID              user_id.
  --assistant-id ID         target_assistant_id. Requires --api-key because public chat forbids it.
  --message TEXT            Message/question to submit.
  --message-file PATH       Read message/question from file.
  --request-id ID           request_id. Default: curl-<timestamp>-<random>.
  --conversation-id ID      conversation_id. Default: conv-<request_id>.
  --client-id ID            client_id and HMAC X-Client-ID. Default: curl-chat-test.
  --api-key KEY             X-API-Key. If set, /api/v1/chat is decoded as internal ChatRequest.
  --hmac-secret SECRET      Optional HMAC secret for protected Gateway.
  --callback-url URL        Internal mode only. Overrides Gateway default callback_url.
  --files ID1,ID2           Comma-separated file IDs.
  --timeout-seconds N       Internal mode only. Sets timeout_seconds when N > 0.
  --poll                    Poll /api/v1/tasks/{task_id}?user_id=... after submit.
  --poll-interval SEC       Poll interval. Default: 5.
  --poll-timeout SEC        Poll timeout. Default: 180.
  --out-dir DIR             Save request/response files. Default: temp dir.
  -h, --help                Show help.

Examples:
  # Public mode: target assistant is resolved from user_assistant_bindings.
  scripts/curl_chat_test.sh \
    --url http://gateway.example.com \
    --user-id 12064912 \
    --message '请总结3121212服务号并生成excel' \
    --poll

  # Internal mode: explicitly set target_assistant_id.
  scripts/curl_chat_test.sh \
    --url http://gateway.example.com \
    --api-key public-or-star-scope-key \
    --user-id 12064912 \
    --assistant-id wang \
    --message '请总结3121212服务号并生成excel' \
    --poll
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing command: $1"
}

new_id() {
  python3 - <<'PY'
import secrets, time
print(f"curl-{int(time.time())}-{secrets.token_hex(4)}")
PY
}

urlencode() {
  python3 - "$1" <<'PY'
import sys
from urllib.parse import quote
print(quote(sys.argv[1], safe=""))
PY
}

write_json_body() {
  local path="$1"
  python3 - "$path" <<'PY'
import json
import os
import sys

path = sys.argv[1]
api_key = os.environ.get("API_KEY_VALUE", "").strip()
assistant_id = os.environ.get("ASSISTANT_ID_VALUE", "").strip()
files_csv = os.environ.get("FILES_CSV_VALUE", "").strip()
timeout_seconds = int(os.environ.get("TIMEOUT_SECONDS_VALUE", "0") or "0")

payload = {
    "request_id": os.environ["REQUEST_ID_VALUE"],
    "user_id": os.environ["USER_ID_VALUE"],
    "conversation_id": os.environ["CONVERSATION_ID_VALUE"],
    "message": os.environ["MESSAGE_VALUE"],
}

files = [item.strip() for item in files_csv.split(",") if item.strip()]
if files:
    payload["files"] = files

if api_key:
    payload["client_id"] = os.environ["CLIENT_ID_VALUE"]
    payload["created_by"] = os.environ["USER_ID_VALUE"]
    if assistant_id:
        payload["target_assistant_id"] = assistant_id
    callback_url = os.environ.get("CALLBACK_URL_VALUE", "").strip()
    if callback_url:
        payload["callback_url"] = callback_url
    if timeout_seconds > 0:
        payload["timeout_seconds"] = timeout_seconds

with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
}

append_auth_headers() {
  local method="$1"
  local path="$2"
  local body_file="$3"

  if [[ -z "$API_KEY" ]]; then
    return
  fi

  CURL_HEADERS+=("-H" "X-API-Key: ${API_KEY}")

  if [[ -z "$HMAC_SECRET" ]]; then
    return
  fi

  local header_blob
  header_blob="$(
    python3 - "$method" "$path" "$body_file" "$CLIENT_ID" "$HMAC_SECRET" <<'PY'
import hashlib
import hmac
import secrets
import sys
import time

method, path, body_file, client_id, secret = sys.argv[1:6]
body = b""
if body_file:
    with open(body_file, "rb") as handle:
        body = handle.read()
timestamp = str(int(time.time()))
nonce = secrets.token_hex(16)
body_hash = hashlib.sha256(body).hexdigest()
payload = ".".join([client_id, timestamp, nonce, method.upper(), path, body_hash])
signature = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
print(f"X-Client-ID: {client_id}")
print(f"X-Timestamp: {timestamp}")
print(f"X-Nonce: {nonce}")
print(f"X-Signature: {signature}")
PY
  )"

  while IFS= read -r header; do
    [[ -n "$header" ]] && CURL_HEADERS+=("-H" "$header")
  done <<<"$header_blob"
}

curl_json() {
  local method="$1"
  local path="$2"
  local body_file="$3"
  local response_body="$4"
  local response_headers="$5"
  local url="${BASE_URL%/}${path}"
  CURL_HEADERS=("-H" "Accept: application/json")

  append_auth_headers "$method" "$path" "$body_file"

  if [[ "$method" == "POST" ]]; then
    CURL_HEADERS+=("-H" "Content-Type: application/json")
    curl -sS -D "$response_headers" -o "$response_body" -w "%{http_code}" \
      -X "$method" "${CURL_HEADERS[@]}" --data-binary "@${body_file}" "$url"
  else
    curl -sS -D "$response_headers" -o "$response_body" -w "%{http_code}" \
      -X "$method" "${CURL_HEADERS[@]}" "$url"
  fi
}

print_json_file() {
  local path="$1"
  python3 - "$path" <<'PY'
import json
import sys

path = sys.argv[1]
raw = open(path, "r", encoding="utf-8").read()
try:
    print(json.dumps(json.loads(raw), ensure_ascii=False, indent=2))
except Exception:
    print(raw)
PY
}

extract_task_id() {
  local path="$1"
  python3 - "$path" <<'PY'
import json
import sys

try:
    payload = json.load(open(sys.argv[1], "r", encoding="utf-8"))
except Exception:
    sys.exit(0)

for key in ("taskId", "task_id", "id"):
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        print(value.strip())
        break
PY
}

task_status() {
  local path="$1"
  python3 - "$path" <<'PY'
import json
import sys

try:
    payload = json.load(open(sys.argv[1], "r", encoding="utf-8"))
except Exception:
    sys.exit(0)

print(str(payload.get("status", "")).strip().lower())
PY
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url) BASE_URL="${2:-}"; shift 2 ;;
    --user-id) USER_ID="${2:-}"; shift 2 ;;
    --assistant-id) ASSISTANT_ID="${2:-}"; shift 2 ;;
    --message) MESSAGE="${2:-}"; shift 2 ;;
    --message-file) MESSAGE_FILE="${2:-}"; shift 2 ;;
    --request-id) REQUEST_ID="${2:-}"; shift 2 ;;
    --conversation-id) CONVERSATION_ID="${2:-}"; shift 2 ;;
    --client-id) CLIENT_ID="${2:-}"; shift 2 ;;
    --api-key) API_KEY="${2:-}"; shift 2 ;;
    --hmac-secret) HMAC_SECRET="${2:-}"; shift 2 ;;
    --callback-url) CALLBACK_URL="${2:-}"; shift 2 ;;
    --files) FILES_CSV="${2:-}"; shift 2 ;;
    --timeout-seconds) TIMEOUT_SECONDS="${2:-}"; shift 2 ;;
    --poll) POLL=1; shift ;;
    --poll-interval) POLL_INTERVAL="${2:-}"; shift 2 ;;
    --poll-timeout) POLL_TIMEOUT="${2:-}"; shift 2 ;;
    --out-dir) OUT_DIR="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_cmd curl
require_cmd python3

[[ -n "$BASE_URL" ]] || die "--url is required"
[[ -n "$USER_ID" ]] || die "--user-id is required"

if [[ -n "$MESSAGE_FILE" ]]; then
  [[ -f "$MESSAGE_FILE" ]] || die "message file not found: $MESSAGE_FILE"
  MESSAGE="$(cat "$MESSAGE_FILE")"
fi
[[ -n "$MESSAGE" ]] || die "--message or --message-file is required"

if [[ -n "$ASSISTANT_ID" && -z "$API_KEY" ]]; then
  die "--assistant-id requires --api-key. Public /api/v1/chat does not accept target_assistant_id."
fi

if [[ -z "$REQUEST_ID" ]]; then
  REQUEST_ID="$(new_id)"
fi
if [[ -z "$CONVERSATION_ID" ]]; then
  CONVERSATION_ID="conv-${REQUEST_ID}"
fi
if [[ -z "$OUT_DIR" ]]; then
  OUT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ironagent-curl-chat.XXXXXX")"
fi
mkdir -p "$OUT_DIR"

REQUEST_JSON="${OUT_DIR}/chat-request.json"
RESPONSE_JSON="${OUT_DIR}/chat-response.json"
RESPONSE_HEADERS="${OUT_DIR}/chat-response.headers"

export API_KEY_VALUE="$API_KEY"
export ASSISTANT_ID_VALUE="$ASSISTANT_ID"
export FILES_CSV_VALUE="$FILES_CSV"
export TIMEOUT_SECONDS_VALUE="$TIMEOUT_SECONDS"
export REQUEST_ID_VALUE="$REQUEST_ID"
export USER_ID_VALUE="$USER_ID"
export CONVERSATION_ID_VALUE="$CONVERSATION_ID"
export MESSAGE_VALUE="$MESSAGE"
export CLIENT_ID_VALUE="$CLIENT_ID"
export CALLBACK_URL_VALUE="$CALLBACK_URL"

write_json_body "$REQUEST_JSON"

echo "Request saved: $REQUEST_JSON"
echo "Response dir:  $OUT_DIR"
echo "POST ${BASE_URL%/}/api/v1/chat"
echo "request_id=${REQUEST_ID}"
echo "user_id=${USER_ID}"
if [[ -n "$API_KEY" ]]; then
  echo "mode=internal-json"
  echo "target_assistant_id=${ASSISTANT_ID:-<empty: resolve by user binding or scheduler>}"
else
  echo "mode=public"
  echo "target_assistant_id=<not sent>"
fi

http_code="$(curl_json "POST" "/api/v1/chat" "$REQUEST_JSON" "$RESPONSE_JSON" "$RESPONSE_HEADERS")"
echo "HTTP=${http_code}"
print_json_file "$RESPONSE_JSON"

if [[ "$http_code" -lt 200 || "$http_code" -ge 300 ]]; then
  exit 1
fi

TASK_ID="$(extract_task_id "$RESPONSE_JSON")"
if [[ -n "$TASK_ID" ]]; then
  echo "task_id=${TASK_ID}"
else
  echo "task_id=<not found in response>"
fi

if [[ "$POLL" -eq 0 || -z "$TASK_ID" ]]; then
  exit 0
fi

encoded_user_id="$(urlencode "$USER_ID")"
deadline=$(( $(date +%s) + POLL_TIMEOUT ))
poll_index=0
while true; do
  now="$(date +%s)"
  if [[ "$now" -gt "$deadline" ]]; then
    echo "poll timeout after ${POLL_TIMEOUT}s"
    exit 2
  fi

  poll_index=$((poll_index + 1))
  poll_body="${OUT_DIR}/task-${poll_index}.json"
  poll_headers="${OUT_DIR}/task-${poll_index}.headers"
  poll_path="/api/v1/tasks/${TASK_ID}?user_id=${encoded_user_id}"
  poll_code="$(curl_json "GET" "$poll_path" "" "$poll_body" "$poll_headers")"
  status="$(task_status "$poll_body")"
  echo "poll=${poll_index} HTTP=${poll_code} status=${status:-<unknown>}"
  print_json_file "$poll_body"

  case "$status" in
    completed|failed|cancelled)
      exit 0
      ;;
  esac

  sleep "$POLL_INTERVAL"
done
