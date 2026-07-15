#!/usr/bin/env bash
set -u

usage() {
  cat <<'EOF'
Usage:
  bash scripts/reproduce_callback_http500.sh --gateway-url <url> [options]

Purpose:
  Reproduce Go Gateway callback HTTP 500 logging by sending callback events to a mock
  callback endpoint that always returns HTTP 500.

Common examples:
  # 1) Start a local mock callback server and submit one task.
  #    The Gateway must already be configured to call this callback URL.
  bash scripts/reproduce_callback_http500.sh \
    --gateway-url http://203.0.113.10:30808 \
    --start-local-callback --callback-host 192.0.2.10 --callback-port 8135 \
    --namespace ironagent --gateway-deploy go-gateway

  # 2) Test environment only: temporarily patch Go Gateway configmap, restart Gateway,
  #    submit one task, collect logs, then restore the original configmap and restart back.
  bash scripts/reproduce_callback_http500.sh \
    --gateway-url http://203.0.113.10:30808 \
    --start-local-callback --callback-host 192.0.2.10 --callback-port 8135 \
    --namespace ironagent --gateway-deploy go-gateway \
    --patch-configmap

  # 3) Use an existing callback URL that already returns 500.
  bash scripts/reproduce_callback_http500.sh \
    --gateway-url http://203.0.113.10:30808 \
    --callback-url http://192.0.2.10:8135/callback \
    --namespace ironagent --gateway-deploy go-gateway

Options:
  --gateway-url URL          Go Gateway public base URL, for example http://host:30808
  --callback-url URL         Callback URL that returns HTTP 500
  --start-local-callback     Start a local mock callback server returning HTTP 500
  --callback-host HOST       Host/IP that Gateway should use to reach this machine
  --callback-port PORT       Local mock callback port, default 8135
  --namespace NS             Kubernetes namespace for log collection / patching
  --gateway-deploy NAME      Go Gateway Deployment name, default go-gateway
  --gateway-container NAME   Optional container name for kubectl logs
  --api-key KEY              Optional X-API-Key for Gateway public API
  --request-id ID            Optional request_id; default repro500-<timestamp>
  --user-id ID               Default repro-user
  --conversation-id ID       Default repro-conversation
  --message TEXT             Default reproduction message
  --wait-seconds N           Wait after chat submit before reading logs, default 20
  --since DURATION           kubectl logs --since value, default 10m
  --patch-configmap          Patch go-gateway-file-config/go-gateway.json temporarily
  --configmap NAME           ConfigMap name, default go-gateway-file-config
  --config-key KEY           ConfigMap key, default go-gateway.json
  --accept-any-response      Compatibility mode: mark any HTTP response delivered
  --no-restore               Do not restore configmap after patching

Notes:
  - /api/v1/chat does not accept callback_url in the request body. Gateway uses
    default_callback_url from config.
  - To log callback_response_status_code=500, Gateway must run with
    callback_fire_and_forget=false.
  - By default this script keeps callback_accept_any_response=false, so HTTP 500
    should retry or dead-letter instead of being marked delivered.
  - Use --accept-any-response only to reproduce the old compatibility behavior.
  - This script does not require jq.
EOF
}

GATEWAY_URL=""
CALLBACK_URL=""
START_LOCAL_CALLBACK=0
CALLBACK_HOST=""
CALLBACK_PORT="8135"
NAMESPACE=""
GATEWAY_DEPLOY="go-gateway"
GATEWAY_CONTAINER=""
API_KEY="${API_KEY:-}"
REQUEST_ID=""
USER_ID="repro-user"
CONVERSATION_ID="repro-conversation"
MESSAGE="reproduce callback http 500"
WAIT_SECONDS="20"
SINCE="10m"
PATCH_CONFIGMAP=0
CONFIGMAP_NAME="go-gateway-file-config"
CONFIG_KEY="go-gateway.json"
RESTORE_ON_EXIT=1
ACCEPT_ANY_RESPONSE=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --gateway-url) GATEWAY_URL="${2:-}"; shift 2 ;;
    --callback-url) CALLBACK_URL="${2:-}"; shift 2 ;;
    --start-local-callback) START_LOCAL_CALLBACK=1; shift ;;
    --callback-host) CALLBACK_HOST="${2:-}"; shift 2 ;;
    --callback-port) CALLBACK_PORT="${2:-}"; shift 2 ;;
    --namespace|-n) NAMESPACE="${2:-}"; shift 2 ;;
    --gateway-deploy) GATEWAY_DEPLOY="${2:-}"; shift 2 ;;
    --gateway-container) GATEWAY_CONTAINER="${2:-}"; shift 2 ;;
    --api-key) API_KEY="${2:-}"; shift 2 ;;
    --request-id) REQUEST_ID="${2:-}"; shift 2 ;;
    --user-id) USER_ID="${2:-}"; shift 2 ;;
    --conversation-id) CONVERSATION_ID="${2:-}"; shift 2 ;;
    --message) MESSAGE="${2:-}"; shift 2 ;;
    --wait-seconds) WAIT_SECONDS="${2:-}"; shift 2 ;;
    --since) SINCE="${2:-}"; shift 2 ;;
    --patch-configmap) PATCH_CONFIGMAP=1; shift ;;
    --configmap) CONFIGMAP_NAME="${2:-}"; shift 2 ;;
    --config-key) CONFIG_KEY="${2:-}"; shift 2 ;;
    --accept-any-response) ACCEPT_ANY_RESPONSE=1; shift ;;
    --no-restore) RESTORE_ON_EXIT=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1"; usage; exit 1 ;;
  esac
done

if [ -z "$GATEWAY_URL" ]; then
  echo "ERROR: --gateway-url is required"
  usage
  exit 1
fi

if [ "$START_LOCAL_CALLBACK" -eq 0 ] && [ -z "$CALLBACK_URL" ]; then
  echo "ERROR: provide --callback-url or --start-local-callback"
  usage
  exit 1
fi

if [ "$PATCH_CONFIGMAP" -eq 1 ] && [ -z "$NAMESPACE" ]; then
  echo "ERROR: --patch-configmap requires --namespace"
  exit 1
fi

if [ "$PATCH_CONFIGMAP" -eq 1 ]; then
  command -v kubectl >/dev/null 2>&1 || { echo "ERROR: kubectl is required for --patch-configmap"; exit 1; }
  command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is required for --patch-configmap"; exit 1; }
fi

command -v curl >/dev/null 2>&1 || { echo "ERROR: curl is required"; exit 1; }

REQUEST_ID="${REQUEST_ID:-repro500-$(date +%Y%m%d%H%M%S)}"
OUT_DIR="./callback-http500-repro-${REQUEST_ID}"
mkdir -p "$OUT_DIR" || exit 1

log() {
  echo "$*" | tee -a "$OUT_DIR/run.log"
}

guess_host_ip() {
  if [ -n "$CALLBACK_HOST" ]; then
    echo "$CALLBACK_HOST"
    return
  fi
  if command -v hostname >/dev/null 2>&1; then
    local ip
    ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
    if [ -n "$ip" ]; then
      echo "$ip"
      return
    fi
  fi
  if command -v ip >/dev/null 2>&1; then
    local ip
    ip="$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ {for (i=1; i<=NF; i++) if ($i=="src") {print $(i+1); exit}}' || true)"
    if [ -n "$ip" ]; then
      echo "$ip"
      return
    fi
  fi
  echo "127.0.0.1"
}

MOCK_PID=""
ORIGINAL_CONFIG="$OUT_DIR/original-${CONFIG_KEY}"
PATCH_FILE="$OUT_DIR/configmap-patch.json"
PATCHED=0

jsonpath_key() {
  printf '%s' "$1" | sed 's/\./\\./g'
}

patch_gateway_configmap() {
  local callback_url="$1"
  local escaped_key
  escaped_key="$(jsonpath_key "$CONFIG_KEY")"

  log "Patching ConfigMap $NAMESPACE/$CONFIGMAP_NAME key=$CONFIG_KEY"
  kubectl get configmap "$CONFIGMAP_NAME" -n "$NAMESPACE" \
    -o "jsonpath={.data.${escaped_key}}" > "$ORIGINAL_CONFIG"

  if [ ! -s "$ORIGINAL_CONFIG" ]; then
    log "ERROR: failed to read configmap key $CONFIG_KEY"
    exit 1
  fi

  python3 - "$ORIGINAL_CONFIG" "$PATCH_FILE" "$callback_url" "$CONFIG_KEY" "$ACCEPT_ANY_RESPONSE" <<'PY'
import json
import sys
from urllib.parse import urlparse

original_path, patch_path, callback_url, config_key, accept_any = sys.argv[1:6]
with open(original_path, encoding="utf-8") as f:
    cfg = json.load(f)

host = urlparse(callback_url).hostname or ""
allowed = cfg.get("callback_allowed_hosts")
if not isinstance(allowed, list):
    allowed = []
if host and host not in allowed:
    allowed.append(host)

cfg["default_callback_url"] = callback_url
cfg["callback_fire_and_forget"] = False
cfg["callback_accept_any_response"] = accept_any == "1"
cfg["callback_required"] = True
cfg["callback_allow_private_hosts"] = True
cfg["callback_allowed_hosts"] = allowed

patch = {"data": {config_key: json.dumps(cfg, ensure_ascii=False, indent=2)}}
with open(patch_path, "w", encoding="utf-8") as f:
    json.dump(patch, f, ensure_ascii=False)
PY

  kubectl patch configmap "$CONFIGMAP_NAME" -n "$NAMESPACE" --type merge -p "$(cat "$PATCH_FILE")"
  kubectl rollout restart "deploy/${GATEWAY_DEPLOY}" -n "$NAMESPACE"
  kubectl rollout status "deploy/${GATEWAY_DEPLOY}" -n "$NAMESPACE" --timeout=180s
  PATCHED=1
}

restore_gateway_configmap() {
  if [ "$PATCHED" -ne 1 ] || [ "$RESTORE_ON_EXIT" -ne 1 ]; then
    return
  fi
  if [ ! -s "$ORIGINAL_CONFIG" ]; then
    return
  fi
  log "Restoring original ConfigMap $NAMESPACE/$CONFIGMAP_NAME key=$CONFIG_KEY"
  python3 - "$ORIGINAL_CONFIG" "$PATCH_FILE.restore" "$CONFIG_KEY" <<'PY'
import json
import sys

original_path, patch_path, config_key = sys.argv[1:4]
with open(original_path, encoding="utf-8") as f:
    original = f.read()
with open(patch_path, "w", encoding="utf-8") as f:
    json.dump({"data": {config_key: original}}, f, ensure_ascii=False)
PY
  kubectl patch configmap "$CONFIGMAP_NAME" -n "$NAMESPACE" --type merge -p "$(cat "$PATCH_FILE.restore")" || true
  kubectl rollout restart "deploy/${GATEWAY_DEPLOY}" -n "$NAMESPACE" || true
  kubectl rollout status "deploy/${GATEWAY_DEPLOY}" -n "$NAMESPACE" --timeout=180s || true
}

cleanup() {
  if [ -n "$MOCK_PID" ]; then
    kill "$MOCK_PID" >/dev/null 2>&1 || true
  fi
  restore_gateway_configmap
}
trap cleanup EXIT

start_local_callback() {
  command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is required for --start-local-callback"; exit 1; }
  local host_ip
  host_ip="$(guess_host_ip)"
  CALLBACK_URL="http://${host_ip}:${CALLBACK_PORT}/callback"
  log "Starting local HTTP 500 callback mock on 0.0.0.0:${CALLBACK_PORT}"
  log "Gateway callback URL should be: $CALLBACK_URL"

  python3 - "$CALLBACK_PORT" "$OUT_DIR/mock-callback.log" <<'PY' &
import json
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

port = int(sys.argv[1])
log_path = sys.argv[2]

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "client": self.client_address[0],
            "path": self.path,
            "headers": {k: v for k, v in self.headers.items()},
            "body": body.decode("utf-8", "replace"),
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        payload = {"code": 500, "msg": "mock callback http 500", "data": ""}
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(500)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        payload = b'{"status":"mock-callback-running"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        return

server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
server.serve_forever()
PY
  MOCK_PID="$!"
  sleep 1
  if ! kill -0 "$MOCK_PID" >/dev/null 2>&1; then
    log "ERROR: mock callback server failed to start"
    exit 1
  fi
  local direct_status
  direct_status="$(curl -sS -o "$OUT_DIR/mock-direct-response.txt" -w "%{http_code}" -X POST "$CALLBACK_URL" -d '{"probe":true}' -H "Content-Type: application/json" || true)"
  log "Direct mock probe HTTP status: $direct_status"
}

submit_chat() {
  local body_file="$OUT_DIR/chat-request.json"
  local response_file="$OUT_DIR/chat-response.json"
  local status_file="$OUT_DIR/chat-status.txt"
  local headers=("-H" "Content-Type: application/json")
  if [ -n "$API_KEY" ]; then
    headers+=("-H" "X-API-Key: $API_KEY")
  fi

  python3 - "$body_file" "$REQUEST_ID" "$USER_ID" "$CONVERSATION_ID" "$MESSAGE" <<'PY'
import json
import sys

path, request_id, user_id, conversation_id, message = sys.argv[1:6]
payload = {
    "request_id": request_id,
    "user_id": user_id,
    "conversation_id": conversation_id,
    "message": message,
}
with open(path, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False)
PY

  local chat_url="${GATEWAY_URL%/}/api/v1/chat"
  log "Submitting chat request to $chat_url"
  local status
  status="$(curl -sS -o "$response_file" -w "%{http_code}" \
    -X POST "$chat_url" "${headers[@]}" --data-binary "@$body_file" || true)"
  printf '%s\n' "$status" > "$status_file"
  log "Chat HTTP status: $status"
  log "Chat response saved: $response_file"

  TASK_ID="$(python3 - "$response_file" <<'PY' 2>/dev/null || true
import json
import sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    print("")
    raise SystemExit
print(data.get("task_id") or data.get("id") or "")
PY
)"
  if [ -n "$TASK_ID" ]; then
    log "Task ID: $TASK_ID"
  else
    log "WARN: task_id not found in response"
  fi
}

collect_gateway_logs() {
  if [ -z "$NAMESPACE" ] || ! command -v kubectl >/dev/null 2>&1; then
    log "Skip kubectl log collection: --namespace missing or kubectl unavailable"
    return
  fi
  log "Waiting ${WAIT_SECONDS}s for callback delivery logs..."
  sleep "$WAIT_SECONDS"

  local log_file="$OUT_DIR/go-gateway.log"
  local grep_file="$OUT_DIR/go-gateway-filtered.log"
  local target="deploy/${GATEWAY_DEPLOY}"
  local args=(-n "$NAMESPACE" "$target" "--since=$SINCE")
  if [ -n "$GATEWAY_CONTAINER" ]; then
    args+=("-c" "$GATEWAY_CONTAINER")
  fi
  kubectl logs "${args[@]}" > "$log_file" 2>"$OUT_DIR/kubectl-logs.err" || {
    log "WARN: kubectl logs failed, see $OUT_DIR/kubectl-logs.err"
    return
  }

  local pattern="callback事件|callback_response_status_code|callback_response_body|${REQUEST_ID}"
  if [ -n "${TASK_ID:-}" ]; then
    pattern="${pattern}|${TASK_ID}"
  fi
  grep -E "$pattern" "$log_file" > "$grep_file" || true

  log "Gateway log saved: $log_file"
  log "Filtered log saved: $grep_file"
  if grep -Eq 'callback_response_status_code"?[:=] ?500|callback returned 500|http_status"?[:=] ?500' "$grep_file"; then
    log "RESULT: reproduced HTTP 500 in Go Gateway callback logs."
  else
    log "RESULT: did not find HTTP 500 in filtered Gateway logs."
    log "Check whether Gateway default_callback_url points to $CALLBACK_URL and callback_fire_and_forget=false."
  fi
}

log "== Callback HTTP 500 reproduction =="
log "output_dir=$OUT_DIR"

if [ "$START_LOCAL_CALLBACK" -eq 1 ]; then
  start_local_callback
fi

log "callback_url=$CALLBACK_URL"

if [ "$PATCH_CONFIGMAP" -eq 1 ]; then
  patch_gateway_configmap "$CALLBACK_URL"
else
  log "Not patching Gateway config. Ensure Go Gateway default_callback_url is already $CALLBACK_URL"
  log "Also ensure callback_fire_and_forget=false, otherwise Gateway will not observe HTTP 500 response."
fi

submit_chat
collect_gateway_logs

log "== Done =="
log "Run log: $OUT_DIR/run.log"
if [ -f "$OUT_DIR/mock-callback.log" ]; then
  log "Mock callback received requests: $OUT_DIR/mock-callback.log"
fi
