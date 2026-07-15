# shellcheck shell=bash

PF_PID=""
CALLBACK_PID=""
CALLBACK_CLUSTER_CREATED=""
CURRENT_STEP="init"
POD_NAME=""
POD_SELECTOR=""
TASK_ID=""
FILE_ID=""
ASSISTANT_URL=""

safe_file_name() {
  local raw="$1"
  printf '%s' "$raw" | tr -c 'A-Za-z0-9_.-' '_' | sed 's/^_*//;s/_*$//'
}

init_iron_agent_smoke() {
  local run_name="$1"
  local ts safe_id

  : "${NS:=ironagent}"
  : "${LOCAL_GATEWAY_PORT:=18080}"
  : "${GATEWAY_URL:=http://127.0.0.1:${LOCAL_GATEWAY_PORT}}"
  : "${WAIT_SECONDS:=180}"
  : "${TASK_POLL_COUNT:=45}"
  : "${TASK_POLL_INTERVAL_SECONDS:=2}"
  : "${TASK_POLL_MIN_QUERIES:=1}"
  : "${WEBHOOK_WAIT_SECONDS:=60}"
  : "${CALLBACK_HOST:=}"
  : "${CALLBACK_PORT:=18081}"
  : "${CALLBACK_URL:=}"
  : "${CALLBACK_MODE:=cluster}"
  : "${CALLBACK_RECEIVER_NAME:=callback-receiver}"
  : "${CALLBACK_RECEIVER_IMAGE:=}"
  : "${CALLBACK_RECEIVER_CLEANUP:=true}"
  : "${SKIP_CALLBACK_POD_PROBE:=false}"
  : "${AUTO_START:=false}"
  : "${TEST_USER_ID:=local-smoke-user}"
  : "${TEST_CONVERSATION_ID:=local-smoke-conversation}"
  : "${UPLOAD_BUCKET:=temp}"
  : "${UPLOAD_FILENAME:=smoke-upload-$(date +%Y%m%d%H%M%S).txt}"
  : "${UPLOAD_CONTENT:=IronAgent workspace upload smoke $(date '+%F %T')}"
  : "${EXPECT_UPLOAD_APPROVAL:=false}"
  : "${CHAT_MESSAGE:=Please confirm the attachment if present and reply in Chinese with HEARTBEAT_OK.}"

  ts="${SMOKE_TIMESTAMP:-$(date +%Y%m%d%H%M%S)}"
  safe_id="$(safe_file_name "${ASSISTANT_ID:-assistant}")"
  safe_id="${safe_id:-assistant}"

  : "${LOG_FILE:=/tmp/ironagent-${run_name}-${safe_id}-${ts}.log}"
  : "${CALLBACK_EVENT_DIR:=/tmp/ironagent-callback-events-${safe_id}-${ts}}"
  : "${CALLBACK_LOG_FILE:=/tmp/ironagent-callback-${safe_id}-${ts}.log}"

  mkdir -p "$(dirname "$LOG_FILE")"
  touch "$LOG_FILE"
  chmod 600 "$LOG_FILE"

  exec > >(tee -a "$LOG_FILE") 2>&1

  trap 'on_error $LINENO' ERR
  trap cleanup EXIT
}

log() {
  printf '\n[%s] [INFO] %s\n' "$(date '+%F %T')" "$*"
}

warn() {
  printf '\n[%s] [WARN] %s\n' "$(date '+%F %T')" "$*" >&2
}

die() {
  printf '\n[%s] [ERROR] %s\n' "$(date '+%F %T')" "$*" >&2
  exit 1
}

set_step() {
  CURRENT_STEP="$1"
  log "STEP: ${CURRENT_STEP}"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing command: $1"
}

cleanup() {
  if [ -n "${PF_PID:-}" ]; then
    kill "$PF_PID" >/dev/null 2>&1 || true
  fi
  if [ -n "${CALLBACK_PID:-}" ]; then
    kill "$CALLBACK_PID" >/dev/null 2>&1 || true
  fi
  if [ "${CALLBACK_MODE:-local}" = "cluster" ] && [ "${CALLBACK_CLUSTER_CREATED:-}" = "true" ] && [ "${CALLBACK_RECEIVER_CLEANUP:-true}" = "true" ]; then
    kubectl -n "$NS" delete svc "$CALLBACK_RECEIVER_NAME" --ignore-not-found >/dev/null 2>&1 || true
    kubectl -n "$NS" delete pod "$CALLBACK_RECEIVER_NAME" --ignore-not-found >/dev/null 2>&1 || true
    kubectl -n "$NS" delete cm "${CALLBACK_RECEIVER_NAME}-script" --ignore-not-found >/dev/null 2>&1 || true
  fi
}

on_error() {
  local exit_code=$?
  local line_no=$1
  warn "Script failed: exit_code=${exit_code}, line=${line_no}, step=${CURRENT_STEP}"
  print_diagnostics
  warn "Full log saved at: ${LOG_FILE}"
  cleanup
  exit "$exit_code"
}

print_diagnostics() {
  warn "Collecting diagnostics. Log file: ${LOG_FILE}"

  {
    echo
    echo "===== kubectl context ====="
    kubectl config current-context 2>/dev/null || true

    echo
    echo "===== namespace ====="
    kubectl get namespace "$NS" -o wide 2>/dev/null || true

    echo
    echo "===== pods ====="
    kubectl -n "$NS" get pods -o wide 2>/dev/null || true

    echo
    echo "===== services ====="
    kubectl -n "$NS" get svc -o wide 2>/dev/null || true

    echo
    echo "===== endpoints ====="
    kubectl -n "$NS" get endpoints 2>/dev/null || true

    echo
    echo "===== recent events ====="
    kubectl -n "$NS" get events --sort-by=.lastTimestamp 2>/dev/null | tail -80 || true

    echo
    echo "===== go-gateway logs ====="
    kubectl -n "$NS" logs deploy/go-gateway --tail=160 2>/dev/null || true

    echo
    echo "===== admin logs ====="
    kubectl -n "$NS" logs deploy/admin --tail=160 2>/dev/null || true

    echo
    echo "===== security-service logs ====="
    kubectl -n "$NS" logs deploy/security-service --tail=160 2>/dev/null || true

    if [ -n "${POD_NAME:-}" ]; then
      echo
      echo "===== assistant pod describe: ${POD_NAME} ====="
      kubectl -n "$NS" describe pod "$POD_NAME" 2>/dev/null || true

      echo
      echo "===== assistant pod logs: ${POD_NAME} ====="
      kubectl -n "$NS" logs "$POD_NAME" --tail=160 2>/dev/null || true
    fi

    if [ -n "${POD_SELECTOR:-}" ]; then
      echo
      echo "===== assistant pods by selector: ${POD_SELECTOR} ====="
      kubectl -n "$NS" get pods -l "$POD_SELECTOR" -o wide 2>/dev/null || true
    fi

    echo
    echo "===== local port-forward log ====="
    cat /tmp/ironagent-go-gateway-port-forward.log 2>/dev/null || true

    echo
    echo "===== callback receiver log ====="
    cat "${CALLBACK_LOG_FILE:-}" 2>/dev/null || true

    echo
    echo "===== callback events ====="
    ls -la "${CALLBACK_EVENT_DIR:-}" 2>/dev/null || true
    if [ -d "${CALLBACK_EVENT_DIR:-}" ]; then
      for event_file in "$CALLBACK_EVENT_DIR"/*.json; do
        [ -e "$event_file" ] || continue
        echo "--- ${event_file} ---"
        cat "$event_file" 2>/dev/null || true
        echo
      done
    fi

    if [ -n "${CALLBACK_URL:-}" ]; then
      print_callback_runbook
    fi
  } >&2
}

check_basic_dependencies() {
  set_step "check command dependencies"
  need_cmd kubectl
  need_cmd curl
  need_cmd python3
}

check_namespace() {
  set_step "check Kubernetes namespace"
  kubectl get namespace "$NS" >/dev/null
}

secret_get() {
  local secret_name="$1"
  local key="$2"

  kubectl -n "$NS" get secret "$secret_name" \
    -o "jsonpath={.data.${key}}" 2>/dev/null | base64 -d
}

load_required_secrets() {
  set_step "read required secrets"
  IRON_AGENT_INTERNAL_SERVICE_TOKEN="$(secret_get control-plane-shared-secret IRON_AGENT_INTERNAL_SERVICE_TOKEN || true)"

  [ -n "$IRON_AGENT_INTERNAL_SERVICE_TOKEN" ] || die "Cannot read control-plane-shared-secret.IRON_AGENT_INTERNAL_SERVICE_TOKEN"
}

validate_http_code() {
  local code="$1"
  [[ "$code" =~ ^[0-9][0-9][0-9]$ ]]
}

gateway_json_request() {
  local method="$1"
  local path="$2"
  local body_file="$3"
  local out_file="$4"

  local http_code curl_status

  log "HTTP ${method} ${path}"
  curl_status=0
  http_code="$(
    curl -sS -o "$out_file" -w "%{http_code}" \
      -X "$method" "${GATEWAY_URL}${path}" \
      -H "Content-Type: application/json" \
      --data-binary "@${body_file}"
  )" || curl_status=$?

  if [ "$curl_status" -ne 0 ]; then
    warn "curl failed for ${method} ${path}: exit_code=${curl_status}"
    cat "$out_file" >&2 2>/dev/null || true
    return "$curl_status"
  fi

  validate_http_code "$http_code" || {
    warn "Invalid HTTP code for ${method} ${path}: ${http_code:-<empty>}"
    cat "$out_file" >&2 2>/dev/null || true
    return 1
  }

  log "HTTP ${method} ${path} -> ${http_code}"
  if [ "$http_code" -lt 200 ] || [ "$http_code" -ge 300 ]; then
    warn "Response body:"
    cat "$out_file" >&2 || true
    return 1
  fi
}

admin_request() {
  local method="$1"
  local path="$2"
  local out_file="$3"
  local body_file="${4:-}"
  local http_code curl_status

  log "ADMIN ${method} ${path}"
  curl_status=0
  if [ -n "$body_file" ]; then
    http_code="$(
      curl -sS -o "$out_file" -w "%{http_code}" \
        -X "$method" "${GATEWAY_URL}${path}" \
        -H "Content-Type: application/json" \
        -H "X-IronAgent-Internal-Token: ${IRON_AGENT_INTERNAL_SERVICE_TOKEN}" \
        --data-binary "@${body_file}"
    )" || curl_status=$?
  else
    http_code="$(
      curl -sS -o "$out_file" -w "%{http_code}" \
        -X "$method" "${GATEWAY_URL}${path}" \
        -H "X-IronAgent-Internal-Token: ${IRON_AGENT_INTERNAL_SERVICE_TOKEN}"
    )" || curl_status=$?
  fi

  if [ "$curl_status" -ne 0 ]; then
    warn "curl failed for admin ${method} ${path}: exit_code=${curl_status}"
    cat "$out_file" >&2 2>/dev/null || true
    return "$curl_status"
  fi

  validate_http_code "$http_code" || {
    warn "Invalid HTTP code for admin ${method} ${path}: ${http_code:-<empty>}"
    cat "$out_file" >&2 2>/dev/null || true
    return 1
  }

  log "ADMIN ${method} ${path} -> ${http_code}"
  if [ "$http_code" -lt 200 ] || [ "$http_code" -ge 300 ]; then
    warn "Response body:"
    cat "$out_file" >&2 || true
    return 1
  fi
}

print_json_file() {
  local file="$1"
  python3 - "$file" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        print(f.read(), end="")
else:
    print(json.dumps(data, ensure_ascii=False, indent=2))
PY
}

json_get() {
  local file="$1"
  local expr="$2"
  python3 - "$file" "$expr" <<'PY'
import json
import sys

path = sys.argv[1]
expr = sys.argv[2]

try:
    with open(path, "r", encoding="utf-8") as f:
        cur = json.load(f)
except Exception:
    sys.exit(0)

for part in expr.split("."):
    if not part:
        continue
    if isinstance(cur, dict):
        cur = cur.get(part)
    else:
        cur = None
    if cur is None:
        sys.exit(0)

if isinstance(cur, (dict, list)):
    print(json.dumps(cur, ensure_ascii=False))
else:
    print(str(cur))
PY
}

url_path_and_query() {
  local raw_url="$1"
  python3 - "$raw_url" <<'PY'
from urllib.parse import urlparse
import sys

parsed = urlparse(sys.argv[1])
path = parsed.path or "/"
if parsed.query:
    path += "?" + parsed.query
print(path)
PY
}

gateway_local_url() {
  local raw_url="$1"
  local path
  path="$(url_path_and_query "$raw_url")"
  printf '%s%s\n' "$GATEWAY_URL" "$path"
}

callback_health_url() {
  local raw_url="$1"
  python3 - "$raw_url" <<'PY'
from urllib.parse import urlparse, urlunparse
import sys

parsed = urlparse(sys.argv[1])
print(urlunparse((parsed.scheme, parsed.netloc, "/healthz", "", "", "")))
PY
}

detect_callback_host() {
  if [ -n "$CALLBACK_HOST" ]; then
    printf '%s\n' "$CALLBACK_HOST"
    return 0
  fi

  python3 - <<'PY'
import socket
import sys

for host in ("10.96.0.1", "8.8.8.8", "1.1.1.1"):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, 80))
        ip = sock.getsockname()[0]
        if ip and not ip.startswith("127."):
            print(ip)
            sys.exit(0)
    except OSError:
        pass
    finally:
        sock.close()

print("127.0.0.1")
PY
}

start_callback_receiver() {
  if [ "$CALLBACK_MODE" = "cluster" ]; then
    start_cluster_callback_receiver
    return 0
  fi

  mkdir -p "$CALLBACK_EVENT_DIR"
  if [ -z "$CALLBACK_URL" ]; then
    CALLBACK_HOST="$(detect_callback_host)"
    CALLBACK_URL="http://${CALLBACK_HOST}:${CALLBACK_PORT}/callback"
  fi

  if printf '%s' "$CALLBACK_URL" | grep -Eq '://(127\.|localhost)'; then
    warn "callback_url uses localhost/127.*; the Go Gateway Pod usually cannot reach it. Set CALLBACK_HOST to a Pod-reachable host IP."
  fi

  log "Starting webhook receiver: ${CALLBACK_URL}"
  python3 -u - "$CALLBACK_PORT" "$CALLBACK_EVENT_DIR" <<'PY' >"$CALLBACK_LOG_FILE" 2>&1 &
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
import time

port = int(sys.argv[1])
event_dir = sys.argv[2]
os.makedirs(event_dir, exist_ok=True)

class Handler(BaseHTTPRequestHandler):
    server_version = "IronAgentSmokeWebhook/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), fmt % args))

    def _write_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self._write_json(200, {"ok": True})
            return
        self._write_json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length)
        try:
            event = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            self._write_json(400, {"error": "invalid json", "detail": str(exc)})
            return

        event_id = str(event.get("event_id") or "no-event-id")
        task_id = str(event.get("task_id") or "")
        event_type = str(event.get("event_type") or "unknown")
        safe_event_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in event_id)
        filename = "%s-%s-%s.json" % (int(time.time() * 1000), event_type.replace("/", "_"), safe_event_id)
        path = os.path.join(event_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "headers": {k: v for k, v in self.headers.items()},
                    "event": event,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

        self._write_json(200, {
            "received": True,
            "event_id": event_id,
            "task_id": task_id,
            "handled": True,
        })

ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
PY
  CALLBACK_PID="$!"
  sleep 1
  if ! kill -0 "$CALLBACK_PID" >/dev/null 2>&1; then
    warn "webhook receiver process exited. Log:"
    cat "$CALLBACK_LOG_FILE" >&2 2>/dev/null || true
    die "Local webhook receiver failed to start. CALLBACK_PORT=${CALLBACK_PORT} may be busy."
  fi
  curl -fsS "http://127.0.0.1:${CALLBACK_PORT}/healthz" >/dev/null || die "Local webhook receiver health check failed: ${CALLBACK_LOG_FILE}"
  log "webhook receiver ready. events_dir=${CALLBACK_EVENT_DIR}, log=${CALLBACK_LOG_FILE}"
}

start_cluster_callback_receiver() {
  if [ -z "$CALLBACK_URL" ]; then
    CALLBACK_URL="http://${CALLBACK_RECEIVER_NAME}.${NS}.svc.cluster.local:8080/callback"
  fi
  if [ -z "$CALLBACK_RECEIVER_IMAGE" ]; then
    CALLBACK_RECEIVER_IMAGE="$(kubectl -n "$NS" get deploy/admin -o jsonpath='{.spec.template.spec.containers[0].image}')"
  fi
  [ -n "$CALLBACK_RECEIVER_IMAGE" ] || die "Cannot resolve CALLBACK_RECEIVER_IMAGE."

  log "Starting in-cluster webhook receiver: name=${CALLBACK_RECEIVER_NAME}, url=${CALLBACK_URL}, image=${CALLBACK_RECEIVER_IMAGE}"
  kubectl -n "$NS" delete svc "$CALLBACK_RECEIVER_NAME" --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n "$NS" delete pod "$CALLBACK_RECEIVER_NAME" --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n "$NS" delete cm "${CALLBACK_RECEIVER_NAME}-script" --ignore-not-found >/dev/null 2>&1 || true

  cat <<'PY' | kubectl -n "$NS" create cm "${CALLBACK_RECEIVER_NAME}-script" --from-file=server.py=/dev/stdin >/dev/null
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time

EVENT_DIR = "/events"
os.makedirs(EVENT_DIR, exist_ok=True)

class Handler(BaseHTTPRequestHandler):
    server_version = "IronAgentSmokeWebhook/1.0"

    def log_message(self, fmt, *args):
        print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), fmt % args), flush=True)

    def _write_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self._write_json(200, {"ok": True})
            return
        self._write_json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length)
        try:
            event = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            self._write_json(400, {"error": "invalid json", "detail": str(exc)})
            return
        event_id = str(event.get("event_id") or "no-event-id")
        event_type = str(event.get("event_type") or "unknown")
        safe_event_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in event_id)
        filename = "%s-%s-%s.json" % (int(time.time() * 1000), event_type.replace("/", "_"), safe_event_id)
        with open(os.path.join(EVENT_DIR, filename), "w", encoding="utf-8") as f:
            json.dump({"headers": {k: v for k, v in self.headers.items()}, "event": event}, f, ensure_ascii=False, indent=2)
        self._write_json(200, {"received": True, "event_id": event_id, "task_id": event.get("task_id"), "handled": True})

ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
PY

  cat <<EOF | kubectl -n "$NS" apply -f - >/dev/null
apiVersion: v1
kind: Service
metadata:
  name: ${CALLBACK_RECEIVER_NAME}
  labels:
    app: ${CALLBACK_RECEIVER_NAME}
spec:
  selector:
    app: ${CALLBACK_RECEIVER_NAME}
  ports:
    - name: http
      port: 8080
      targetPort: 8080
---
apiVersion: v1
kind: Pod
metadata:
  name: ${CALLBACK_RECEIVER_NAME}
  labels:
    app: ${CALLBACK_RECEIVER_NAME}
spec:
  restartPolicy: Never
  containers:
    - name: receiver
      image: ${CALLBACK_RECEIVER_IMAGE}
      imagePullPolicy: IfNotPresent
      command: ["python3", "/opt/callback/server.py"]
      ports:
        - containerPort: 8080
      volumeMounts:
        - name: script
          mountPath: /opt/callback
        - name: events
          mountPath: /events
  volumes:
    - name: script
      configMap:
        name: ${CALLBACK_RECEIVER_NAME}-script
    - name: events
      emptyDir: {}
EOF

  CALLBACK_CLUSTER_CREATED="true"
  kubectl -n "$NS" wait --for=condition=Ready "pod/${CALLBACK_RECEIVER_NAME}" --timeout=90s >/dev/null
  kubectl -n "$NS" exec "$CALLBACK_RECEIVER_NAME" -- python3 - <<'PY' >/dev/null
import urllib.request
urllib.request.urlopen("http://127.0.0.1:8080/healthz", timeout=5).read()
PY
  log "in-cluster webhook receiver ready."
}

probe_callback_from_gateway_pod() {
  if [ "$SKIP_CALLBACK_POD_PROBE" = "true" ]; then
    warn "SKIP_CALLBACK_POD_PROBE=true; skipping callback probe from Go Gateway Pod."
    return 0
  fi

  local health_url probe_log quoted_url
  health_url="$(callback_health_url "$CALLBACK_URL")"
  probe_log="$(mktemp)"
  quoted_url="$(printf '%q' "$health_url")"

  log "Probing callback health from Go Gateway Pod: ${health_url}"
  if kubectl -n "$NS" exec deploy/go-gateway -- sh -lc "
URL=${quoted_url}
if command -v wget >/dev/null 2>&1; then
  wget -qO- -T 5 \"\$URL\"
elif command -v curl >/dev/null 2>&1; then
  curl -fsS --max-time 5 \"\$URL\"
elif command -v python3 >/dev/null 2>&1; then
  python3 -c '
import sys
import urllib.request
url = sys.argv[1]
with urllib.request.urlopen(url, timeout=5) as response:
    sys.stdout.write(response.read().decode('utf-8', errors='replace'))
' \"\$URL\"
else
  echo 'no curl/wget/python3 in go-gateway container'
  exit 127
fi
" >"$probe_log" 2>&1; then
    log "Go Gateway Pod can reach callback health."
    cat "$probe_log" || true
    return 0
  fi

  if grep -q "no curl/wget/python3" "$probe_log" 2>/dev/null; then
    warn "Go Gateway container lacks wget/curl/python3; cannot pre-probe callback. Continuing with real webhook test."
    cat "$probe_log" >&2 || true
    return 0
  fi

  warn "Go Gateway Pod cannot reach callback health. Probe output:"
  cat "$probe_log" >&2 || true
  warn "Check CALLBACK_HOST=${CALLBACK_HOST:-<auto>} and TCP CALLBACK_PORT=${CALLBACK_PORT} reachability from the cluster."
  return 1
}

print_callback_runbook() {
  local health_url
  health_url="$(callback_health_url "$CALLBACK_URL")"
  cat >&2 <<EOF

Webhook troubleshooting commands:

1. Check listener on the script host:
   ss -ltnp | grep ':${CALLBACK_PORT}' || netstat -ltnp | grep ':${CALLBACK_PORT}'

2. Self-test receiver on the script host:
   curl -v http://127.0.0.1:${CALLBACK_PORT}/healthz
   curl -v ${health_url}

3. Probe from the Go Gateway Pod:
   kubectl -n ${NS} exec deploy/go-gateway -- sh -lc 'wget -S -O- -T 5 ${health_url} || curl -v --max-time 5 ${health_url}'

4. Check Go Gateway callback config:
   kubectl -n ${NS} get cm go-gateway-config -o yaml | grep -E 'GO_GATEWAY_CALLBACK_ALLOWED_HOSTS|GO_GATEWAY_CALLBACK_ALLOW_PRIVATE_HOSTS'

Current callback_url:
   ${CALLBACK_URL}

EOF
}

wait_for_webhook_events() {
  if [ "$CALLBACK_MODE" = "cluster" ]; then
    wait_for_cluster_webhook_events "$@"
    return $?
  fi

  local task_id="$1"
  local min_count="${2:-1}"
  local deadline=$((SECONDS + WEBHOOK_WAIT_SECONDS))
  local event_summary

  while [ "$SECONDS" -lt "$deadline" ]; do
    event_summary="$(
      python3 - "$CALLBACK_EVENT_DIR" "$task_id" "$min_count" <<'PY'
import glob
import json
import os
import sys

event_dir, task_id, min_count = sys.argv[1], sys.argv[2], int(sys.argv[3])
events = []
for path in sorted(glob.glob(os.path.join(event_dir, "*.json"))):
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        continue
    event = payload.get("event", {})
    if event.get("task_id") == task_id:
        events.append(event)

print(",".join(event.get("event_type", "unknown") for event in events))
sys.exit(0 if len(events) >= min_count else 1)
PY
    )" && {
      log "Received webhook events: ${event_summary}"
      return 0
    }
    sleep 2
  done

  warn "webhook receiver log:"
  cat "$CALLBACK_LOG_FILE" >&2 2>/dev/null || true
  return 1
}

wait_for_cluster_webhook_events() {
  local task_id="$1"
  local min_count="${2:-1}"
  local deadline=$((SECONDS + WEBHOOK_WAIT_SECONDS))
  local event_summary

  while [ "$SECONDS" -lt "$deadline" ]; do
    event_summary="$(
      kubectl -n "$NS" exec "$CALLBACK_RECEIVER_NAME" -- python3 -c '
import glob
import json
import os
import sys

task_id, min_count = sys.argv[1], int(sys.argv[2])
events = []
for path in sorted(glob.glob("/events/*.json")):
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        continue
    event = payload.get("event", {})
    if event.get("task_id") == task_id:
        events.append(event)

print(",".join(event.get("event_type", "unknown") for event in events))
sys.exit(0 if len(events) >= min_count else 1)
' "$task_id" "$min_count" 2>/dev/null
    )" && {
      log "Received webhook events: ${event_summary}"
      return 0
    }
    sleep 2
  done

  warn "cluster webhook receiver log:"
  kubectl -n "$NS" logs "$CALLBACK_RECEIVER_NAME" --tail=120 >&2 2>/dev/null || true
  warn "cluster webhook receiver event files:"
  kubectl -n "$NS" exec "$CALLBACK_RECEIVER_NAME" -- sh -lc 'ls -la /events && for f in /events/*.json; do [ -e "$f" ] || continue; echo "--- $f ---"; cat "$f"; echo; done' >&2 2>/dev/null || true
  return 1
}

ensure_gateway_access() {
  set_step "check or establish Go Gateway local access"
  if ! curl -fsS "${GATEWAY_URL}/healthz" >/dev/null 2>&1; then
    log "${GATEWAY_URL}/healthz is not reachable. Starting port-forward: svc/go-gateway ${LOCAL_GATEWAY_PORT}:8080"
    kubectl -n "$NS" port-forward svc/go-gateway "${LOCAL_GATEWAY_PORT}:8080" \
      >/tmp/ironagent-go-gateway-port-forward.log 2>&1 &
    PF_PID="$!"
    sleep 3
  fi

  curl -fsS "${GATEWAY_URL}/healthz" >/dev/null
  log "Go Gateway healthz ok: ${GATEWAY_URL}/healthz"
}

print_readyz() {
  local out_file
  out_file="$(mktemp)"
  log "Go Gateway readyz current status:"
  curl -sS "${GATEWAY_URL}/readyz" > "$out_file" || true
  print_json_file "$out_file"
}

require_assistant_id() {
  [ -n "${ASSISTANT_ID:-}" ] || die "ASSISTANT_ID is required for this route."
}

assistant_label_value() {
  local value="$1"
  python3 - "$value" <<'PY'
import sys

value = sys.argv[1].strip()
safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in value)
safe = "-".join(part for part in safe.split("-") if part)
print((safe[:63].strip("-")) or "assistant")
PY
}

set_assistant_pod_selector() {
  local label_assistant_id
  label_assistant_id="$(assistant_label_value "$ASSISTANT_ID")"
  POD_SELECTOR="app=ironagent-assistant-runtime,ironagent.ironagent/assistant-id=${label_assistant_id}"
}

repair_and_start_assistant() {
  require_assistant_id

  set_step "repair assistant runtime"
  local repair_response
  repair_response="$(mktemp)"
  admin_request POST "/api/v1/admin/assistants/${ASSISTANT_ID}/repair" "$repair_response"
  print_json_file "$repair_response"

  set_step "start assistant runtime"
  local start_response
  start_response="$(mktemp)"
  admin_request POST "/api/v1/admin/assistants/${ASSISTANT_ID}/start" "$start_response"
  print_json_file "$start_response"
}

maybe_auto_start_assistant() {
  if [ "$AUTO_START" = "true" ]; then
    repair_and_start_assistant
  else
    log "AUTO_START is not true; will not repair/start the existing assistant."
  fi
}

wait_for_assistant_pod_ready() {
  require_assistant_id
  set_assistant_pod_selector

  set_step "wait for assistant Pod Ready"
  local deadline phase ready
  deadline=$((SECONDS + WAIT_SECONDS))
  while [ "$SECONDS" -lt "$deadline" ]; do
    POD_NAME="$(kubectl -n "$NS" get pod -l "$POD_SELECTOR" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
    if [ -n "$POD_NAME" ]; then
      phase="$(kubectl -n "$NS" get pod "$POD_NAME" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
      ready="$(kubectl -n "$NS" get pod "$POD_NAME" -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null || true)"
      log "Assistant Pod status: pod=${POD_NAME}, phase=${phase}, ready=${ready}"
      if [ "$phase" = "Running" ] && [ "$ready" = "true" ]; then
        return 0
      fi
    else
      log "Assistant Pod not found yet. selector=${POD_SELECTOR}"
    fi
    sleep 5
  done

  [ -n "$POD_NAME" ] || die "Assistant Pod not found."
  phase="$(kubectl -n "$NS" get pod "$POD_NAME" -o jsonpath='{.status.phase}')"
  ready="$(kubectl -n "$NS" get pod "$POD_NAME" -o jsonpath='{.status.containerStatuses[0].ready}')"
  [ "$phase" = "Running" ] && [ "$ready" = "true" ] || die "Assistant Pod is not Ready."
}

get_assistant_launch_url() {
  require_assistant_id
  set_step "get assistant UI launch URL"
  local launch_response
  launch_response="$(mktemp)"
  admin_request GET "/api/v1/admin/assistants/${ASSISTANT_ID}/launch-url" "$launch_response"
  print_json_file "$launch_response"
  ASSISTANT_URL="$(json_get "$launch_response" assistantUrl)"
  if [ -z "$ASSISTANT_URL" ]; then
    ASSISTANT_URL="$(json_get "$launch_response" gatewayLaunchUrl)"
  fi
  if [ -n "$ASSISTANT_URL" ]; then
    log "Assistant UI URL: ${ASSISTANT_URL}"
  else
    warn "No assistant UI URL returned."
  fi
}

validate_upload_inputs() {
  [ "$UPLOAD_BUCKET" = "temp" ] || die "IronAgent public upload API does not accept a bucket field; smoke verification expects temp. Got: ${UPLOAD_BUCKET}"
  [ -n "$UPLOAD_FILENAME" ] || die "UPLOAD_FILENAME must not be empty."
  case "$UPLOAD_FILENAME" in
    */*) die "UPLOAD_FILENAME must be a plain filename, not a path: ${UPLOAD_FILENAME}" ;;
  esac
}

verify_files_route_for_assistant() {
  require_assistant_id
  [ -n "${POD_NAME:-}" ] || die "POD_NAME is empty; call wait_for_assistant_pod_ready first."
  validate_upload_inputs

  set_step "upload to assistant workspace through file API"
  local upload_source_file upload_response upload_http_code upload_curl_status
  upload_source_file="$(mktemp)"
  printf '%s\n' "$UPLOAD_CONTENT" > "$upload_source_file"

  upload_response="$(mktemp)"
  upload_curl_status=0
  upload_http_code="$(
    curl -sS -o "$upload_response" -w "%{http_code}" \
      -X POST "${GATEWAY_URL}/api/v1/files/upload" \
      -F "user_id=${TEST_USER_ID}" \
      -F "file=@${upload_source_file};filename=${UPLOAD_FILENAME};type=text/plain"
  )" || upload_curl_status=$?
  if [ "$upload_curl_status" -ne 0 ]; then
    warn "direct upload curl failed: exit_code=${upload_curl_status}"
    cat "$upload_response" >&2 2>/dev/null || true
    return "$upload_curl_status"
  fi
  validate_http_code "$upload_http_code" || die "Invalid HTTP code from presigned upload: ${upload_http_code:-<empty>}"
  log "POST /api/v1/files/upload -> ${upload_http_code}"
  if [ "$upload_http_code" -lt 200 ] || [ "$upload_http_code" -ge 300 ]; then
    warn "Upload response body:"
    cat "$upload_response" >&2 || true
    die "File API upload failed."
  fi
  print_json_file "$upload_response"

  FILE_ID="$(json_get "$upload_response" file_id)"
  [ -n "$FILE_ID" ] || die "POST /api/v1/files/upload response did not include file_id."

  set_step "verify uploaded file in assistant container workspace"
  kubectl -n "$NS" exec "$POD_NAME" -- sh -lc '
    set -e
    bucket="$1"
    filename="$2"
    path="/workspace/${bucket}/${filename}"
    test -f "$path"
    echo "Uploaded file content in container:"
    cat "$path"
  ' sh "$UPLOAD_BUCKET" "$UPLOAD_FILENAME"

  set_step "download through file API and verify content"
  local downloaded_file download_http_code download_curl_status
  downloaded_file="$(mktemp)"
  download_curl_status=0
  download_http_code="$(
    curl -sS -o "$downloaded_file" -w "%{http_code}" \
      "${GATEWAY_URL}/api/v1/files/${FILE_ID}/download?user_id=${TEST_USER_ID}"
  )" || download_curl_status=$?
  if [ "$download_curl_status" -ne 0 ]; then
    warn "direct download curl failed: exit_code=${download_curl_status}"
    return "$download_curl_status"
  fi
  validate_http_code "$download_http_code" || die "Invalid HTTP code from direct download: ${download_http_code:-<empty>}"
  log "GET /api/v1/files/${FILE_ID}/download -> ${download_http_code}"
  if [ "$download_http_code" -lt 200 ] || [ "$download_http_code" -ge 300 ]; then
    warn "Download response body:"
    cat "$downloaded_file" >&2 || true
    die "File API download failed."
  fi

  python3 - "$upload_source_file" "$downloaded_file" <<'PY'
import sys

with open(sys.argv[1], "rb") as f:
    expected = f.read()
with open(sys.argv[2], "rb") as f:
    actual = f.read()
if expected != actual:
    raise SystemExit("downloaded content does not match uploaded content")
PY
  log "File download verification passed: file_id=${FILE_ID}, filename=${UPLOAD_FILENAME}"
}

write_chat_body() {
  local out_file="$1"
  local file_id="${2:-}"
  python3 - "$out_file" "$TEST_USER_ID" "$TEST_CONVERSATION_ID" "$CHAT_MESSAGE" "$file_id" <<'PY'
import json
import sys
import time

out_file, user_id, conversation_id, message, file_id = sys.argv[1:6]
request_id = "local-smoke-%s-%d" % (user_id, int(time.time()))
payload = {
    "request_id": request_id,
    "user_id": user_id,
    "conversation_id": conversation_id,
    "files": [file_id] if file_id else [],
    "message": message,
}
with open(out_file, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
}

poll_chat_until_completed() {
  local task_id="$1"
  local empty_body final_status last_status last_stage last_task_response final_task_response
  local task_response task_path status stage reply

  set_step "poll chat task result until completed"
  empty_body="$(mktemp)"
  printf '' > "$empty_body"
  final_status=""
  last_status=""
  last_stage=""
  last_task_response=""
  final_task_response=""

  for i in $(seq 1 "$TASK_POLL_COUNT"); do
    task_response="$(mktemp)"
    last_task_response="$task_response"
    task_path="/api/v1/tasks/${task_id}"
    gateway_json_request GET "$task_path" "$empty_body" "$task_response"

    status="$(json_get "$task_response" status)"
    stage="$(json_get "$task_response" stage)"
    reply="$(json_get "$task_response" reply_text)"
    last_status="$status"
    last_stage="$stage"

    log "Task query ${i}/${TASK_POLL_COUNT}: status=${status}, stage=${stage}"
    if [ -n "$reply" ]; then
      log "reply_text=${reply}"
    fi

    case "$status" in
      completed)
        final_status="$status"
        final_task_response="$task_response"
        if [ "$i" -lt "$TASK_POLL_MIN_QUERIES" ]; then
          log "Task reached completed but continuing until TASK_POLL_MIN_QUERIES=${TASK_POLL_MIN_QUERIES} for observation."
          sleep "$TASK_POLL_INTERVAL_SECONDS"
          continue
        fi
        print_json_file "$final_task_response"
        log "Chat task completed: task_id=${task_id}"
        return 0
        ;;
      failed|rejected|cancelled|timeout)
        print_json_file "$task_response"
        die "Chat task entered non-completed terminal status: ${status}"
        ;;
    esac

    sleep "$TASK_POLL_INTERVAL_SECONDS"
  done

  if [ "$final_status" = "completed" ] && [ -n "$final_task_response" ]; then
    print_json_file "$final_task_response"
    log "Chat task completed after reaching TASK_POLL_COUNT before TASK_POLL_MIN_QUERIES: task_id=${task_id}"
    return 0
  fi

  if [ -n "$last_task_response" ]; then
    print_json_file "$last_task_response"
  fi
  die "Chat task did not complete: final_status=${final_status:-${last_status:-unknown}}, stage=${last_stage:-unknown}"
}

run_chat_route_for_assistant() {
  local file_id="${1:-}"
  require_assistant_id

  set_step "start upstream webhook receiver"
  start_callback_receiver
  probe_callback_from_gateway_pod || {
    print_callback_runbook
    die "Go Gateway Pod cannot reach the script webhook receiver."
  }

  set_step "submit chat task"
  local chat_body chat_response
  chat_body="$(mktemp)"
  chat_response="$(mktemp)"
  write_chat_body "$chat_body" "$file_id"

  gateway_json_request POST /api/v1/chat "$chat_body" "$chat_response"
  print_json_file "$chat_response"
  TASK_ID="$(json_get "$chat_response" task_id)"
  [ -n "$TASK_ID" ] || die "chat response did not include task_id."

  set_step "wait for webhook status event"
  wait_for_webhook_events "$TASK_ID" 1 || die "No upstream webhook event received for task_id=${TASK_ID}."

  poll_chat_until_completed "$TASK_ID"

  set_step "wait for terminal webhook status event"
  wait_for_webhook_events "$TASK_ID" 2 || die "No terminal upstream webhook event received for task_id=${TASK_ID}."
}

print_route_summary() {
  cat <<EOF

Result summary:
  assistant_id: ${ASSISTANT_ID:-}
  user_id:      ${ASSISTANT_USER_ID:-${TEST_USER_ID:-}}
  pod:          ${POD_NAME:-}
  gateway:      ${GATEWAY_URL:-}
  file_id:      ${FILE_ID:-}
  task_id:      ${TASK_ID:-}
  callback_url: ${CALLBACK_URL:-}
  events_dir:   ${CALLBACK_EVENT_DIR:-}
  ui_url:       ${ASSISTANT_URL:-not returned}
  log_file:     ${LOG_FILE:-}

Common follow-up commands:
  kubectl -n ${NS} get pod -l "${POD_SELECTOR:-<selector-not-set>}" -o wide
  curl -sS ${GATEWAY_URL}/readyz

EOF
}
