#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# 在同一 namespace 内部署临时 callback mock receiver，把 go-gateway 默认 callback
# 临时指向该 receiver，然后复用 validate_callback_files_flow_cn.sh 跑真实 live E2E。

NS="${NS:-}"
USER_ID="${USER_ID:-}"
API_KEY="${API_KEY:-}"
OUT_DIR="${OUT_DIR:-}"
GATEWAY_DEPLOY="${GATEWAY_DEPLOY:-deploy/go-gateway}"
GATEWAY_CONFIGMAP="${GATEWAY_CONFIGMAP:-go-gateway-config}"
SINK_NAME="${SINK_NAME:-callback-sink}"
SINK_IMAGE="${SINK_IMAGE:-python:3.12-alpine}"
SINK_ROLLOUT_TIMEOUT="${SINK_ROLLOUT_TIMEOUT:-180s}"
MOCK_EVIDENCE_WAIT_SECONDS="${MOCK_EVIDENCE_WAIT_SECONDS:-30}"
RESTORE_GATEWAY_CONFIG="true"
KEEP_MOCK_UPSTREAM="false"
CONFIRM_LIVE_WRITE="false"
CONFIRM_PATCH_GATEWAY="false"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VALIDATE_SCRIPT="${VALIDATE_SCRIPT:-${SCRIPT_DIR}/validate_callback_files_flow_cn.sh}"
RUN_ID="$(date +%Y%m%d%H%M%S)-$$"
if [[ -z "$OUT_DIR" ]]; then
  OUT_DIR="/tmp/ironagent-callback-mock-e2e-${RUN_ID}"
fi

RUN_LOG=""
ORIGINAL_CONFIG_JSON=""
EVIDENCE_FILE=""
LOGS_PID=""
VALIDATE_ARGS=()

usage() {
  cat <<'EOF'
用法：
  scripts/k8s/run_callback_files_mock_e2e_cn.sh \
    --namespace ironagent-kf \
    --user-id 12025772 \
    --confirm-live-write \
    --confirm-patch-gateway \
    --allow-mysql-root \
    [validate_callback_files_flow_cn.sh 选项]

作用：
  1. 部署临时 callback-sink Deployment/Service。
  2. 备份并 patch go-gateway-config，让默认 callback 指向 callback-sink。
  3. rollout restart go-gateway。
  4. tail callback-sink 日志为 upstream evidence。
  5. 调用 validate_callback_files_flow_cn.sh --live-e2e。
  6. 默认恢复 go-gateway-config 并删除 callback-sink。

必要确认：
  --confirm-live-write        允许提交真实 live 测试任务。
  --confirm-patch-gateway     允许临时 patch go-gateway callback 配置并重启 Gateway。

常用选项：
  --namespace NS              K8s namespace。
  --user-id USER_ID           测试用户。
  --api-key KEY               Gateway API key；也可用环境变量 API_KEY。
  --allow-read-gateway-key    透传给 validate 脚本，允许从 Pod 读取首个 key。
  --allow-public-unauthenticated
                              透传给 validate 脚本，允许无 key 调用公开接口。
  --allow-mysql-root          透传给 validate 脚本，用 root 做 DB 证据查询。
  --require-real-result       透传给 validate 脚本，要求真实 result 文件。
  --message TEXT              透传给 validate 脚本。
  --file PATH                 透传给 validate 脚本。
  --out-dir DIR               证据目录。
  --sink-name NAME            mock receiver 名称，默认 callback-sink。
  --sink-image IMAGE          mock receiver 镜像，默认 python:3.12-alpine。
  --mock-evidence-wait-seconds N
                              validate 等待 receiver 日志匹配秒数，默认 30。
  --no-restore-gateway        结束后不恢复 Gateway callback 配置。
  --keep-mock-upstream        结束后不删除 callback-sink。
  --                          后面的参数原样透传给 validate 脚本。
EOF
}

init_output() {
  mkdir -p "$OUT_DIR"
  chmod 700 "$OUT_DIR"
  RUN_LOG="$OUT_DIR/run.log"
  ORIGINAL_CONFIG_JSON="$OUT_DIR/go-gateway-config.original.json"
  EVIDENCE_FILE="$OUT_DIR/mock-upstream-callback-events.jsonl"
  : >"$RUN_LOG"
  : >"$EVIDENCE_FILE"
  chmod 600 "$RUN_LOG" "$EVIDENCE_FILE"
}

log() {
  local msg="$*"
  if [[ -n "${RUN_LOG:-}" ]]; then
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$msg" | tee -a "$RUN_LOG" >&2
  else
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$msg" >&2
  fi
}

die() {
  log "失败：$*"
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "缺少命令：$1"
}

require_arg() {
  local name="$1"
  local value="${2:-}"
  if [[ -z "$value" || "$value" == --* ]]; then
    echo "参数 $name 缺少值" >&2
    usage >&2
    exit 2
  fi
}

require_nonnegative_int() {
  local name="$1"
  local value="$2"
  if ! [[ "$value" =~ ^[0-9]+$ ]]; then
    die "$name 必须是非负整数，当前值：$value"
  fi
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --namespace|-n)
        require_arg "$1" "${2:-}"
        NS="$2"
        VALIDATE_ARGS+=("--namespace" "$2")
        shift 2
        ;;
      --user-id)
        require_arg "$1" "${2:-}"
        USER_ID="$2"
        VALIDATE_ARGS+=("--user-id" "$2")
        shift 2
        ;;
      --api-key)
        require_arg "$1" "${2:-}"
        API_KEY="$2"
        shift 2
        ;;
      --confirm-live-write)
        CONFIRM_LIVE_WRITE="true"
        VALIDATE_ARGS+=("--confirm-live-write")
        shift
        ;;
      --confirm-patch-gateway)
        CONFIRM_PATCH_GATEWAY="true"
        shift
        ;;
      --allow-mysql-root|--allow-read-gateway-key|--allow-public-unauthenticated|--require-real-result|--run-negative|--skip-negative|--include-global-errors)
        VALIDATE_ARGS+=("$1")
        shift
        ;;
      --base-url|--local-port|--file|--message|--poll-seconds|--poll-interval|--other-user-id)
        require_arg "$1" "${2:-}"
        VALIDATE_ARGS+=("$1" "$2")
        shift 2
        ;;
      --out-dir)
        require_arg "$1" "${2:-}"
        OUT_DIR="$2"
        shift 2
        ;;
      --gateway-deploy)
        require_arg "$1" "${2:-}"
        GATEWAY_DEPLOY="$2"
        shift 2
        ;;
      --gateway-configmap)
        require_arg "$1" "${2:-}"
        GATEWAY_CONFIGMAP="$2"
        shift 2
        ;;
      --sink-name)
        require_arg "$1" "${2:-}"
        SINK_NAME="$2"
        shift 2
        ;;
      --sink-image)
        require_arg "$1" "${2:-}"
        SINK_IMAGE="$2"
        shift 2
        ;;
      --mock-evidence-wait-seconds)
        require_arg "$1" "${2:-}"
        MOCK_EVIDENCE_WAIT_SECONDS="$2"
        shift 2
        ;;
      --no-restore-gateway)
        RESTORE_GATEWAY_CONFIG="false"
        shift
        ;;
      --keep-mock-upstream)
        KEEP_MOCK_UPSTREAM="true"
        shift
        ;;
      --expected-callback-url|--upstream-evidence-file|--upstream-evidence-wait-seconds|--allow-prod-callback|--read-only-inspect|--live-e2e)
        die "$1 由 mock E2E wrapper 管理，请不要手工传入。"
        ;;
      --)
        shift
        VALIDATE_ARGS+=("$@")
        break
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
}

mock_callback_url() {
  printf 'http://%s:8080/callback' "$SINK_NAME"
}

deploy_mock_upstream() {
  log "部署 mock callback receiver：namespace=$NS name=$SINK_NAME image=$SINK_IMAGE"
  SINK_NAME="$SINK_NAME" SINK_IMAGE="$SINK_IMAGE" python3 - <<'PY' | kubectl -n "$NS" apply -f -
import os
import textwrap

name = os.environ["SINK_NAME"]
image = os.environ["SINK_IMAGE"]
server = r'''
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    server_version = "IronAgentCallbackSink/1.0"

    def log_message(self, fmt, *args):
        return

    def _send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def do_GET(self):
        if self.path == "/healthz":
            self._send_json(200, {"ok": True})
            return
        self._send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        body_bytes = self.rfile.read(max(length, 0))
        body_text = body_bytes.decode("utf-8", "replace")
        record = {
            "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "method": self.command,
            "path": self.path,
            "headers": {key: value for key, value in self.headers.items()},
            "body": body_text,
            "body_bytes": len(body_bytes),
        }
        try:
            record["json"] = json.loads(body_text)
        except Exception as exc:
            record["json_error"] = str(exc)
        print(json.dumps(record, ensure_ascii=False, separators=(",", ":")), flush=True)
        self._send_json(200, {"ok": True})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
'''

print(textwrap.dedent(f"""\
apiVersion: v1
kind: ConfigMap
metadata:
  name: {name}
  labels:
    app: {name}
data:
  server.py: |
"""), end="")
for line in server.strip("\n").splitlines():
    print("    " + line)
print(textwrap.dedent(f"""\
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
  labels:
    app: {name}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {name}
  template:
    metadata:
      labels:
        app: {name}
    spec:
      containers:
        - name: {name}
          image: {image}
          imagePullPolicy: IfNotPresent
          command: ["python", "/app/server.py"]
          ports:
            - name: http
              containerPort: 8080
          readinessProbe:
            httpGet:
              path: /healthz
              port: http
            initialDelaySeconds: 2
            periodSeconds: 3
          livenessProbe:
            httpGet:
              path: /healthz
              port: http
            initialDelaySeconds: 5
            periodSeconds: 10
          volumeMounts:
            - name: app
              mountPath: /app/server.py
              subPath: server.py
              readOnly: true
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
          resources:
            requests:
              cpu: 20m
              memory: 64Mi
            limits:
              cpu: 200m
              memory: 128Mi
      volumes:
        - name: app
          configMap:
            name: {name}
---
apiVersion: v1
kind: Service
metadata:
  name: {name}
  labels:
    app: {name}
spec:
  selector:
    app: {name}
  ports:
    - name: http
      port: 8080
      targetPort: http
"""), end="")
PY
  kubectl -n "$NS" rollout status "deploy/${SINK_NAME}" --timeout="$SINK_ROLLOUT_TIMEOUT"
}

snapshot_gateway_config() {
  log "备份 Gateway ConfigMap：$GATEWAY_CONFIGMAP"
  kubectl -n "$NS" get configmap "$GATEWAY_CONFIGMAP" -o json >"$ORIGINAL_CONFIG_JSON"
  chmod 600 "$ORIGINAL_CONFIG_JSON"
}

patch_gateway_config_to_mock() {
  local callback_url patch
  callback_url="$(mock_callback_url)"
  patch="$(NS="$NS" SINK_NAME="$SINK_NAME" python3 - <<'PY'
import json
import os

ns = os.environ["NS"]
name = os.environ["SINK_NAME"]
hosts = [
    name,
    f"{name}:8080",
    f"{name}.{ns}",
    f"{name}.{ns}:8080",
    f"{name}.{ns}.svc",
    f"{name}.{ns}.svc:8080",
    f"{name}.{ns}.svc.cluster.local",
    f"{name}.{ns}.svc.cluster.local:8080",
]
print(json.dumps({
    "data": {
        "GO_GATEWAY_DEFAULT_CALLBACK_URL": f"http://{name}:8080/callback",
        "GO_GATEWAY_CALLBACK_ALLOWED_HOSTS": ",".join(hosts),
        "GO_GATEWAY_CALLBACK_ALLOW_PRIVATE_HOSTS": "true",
        "GO_GATEWAY_CALLBACK_FIRE_AND_FORGET": "true",
    }
}, separators=(",", ":")))
PY
)"
  log "临时 patch Gateway callback 到 mock receiver：$callback_url"
  kubectl -n "$NS" patch configmap "$GATEWAY_CONFIGMAP" --type merge -p "$patch"
  kubectl -n "$NS" rollout restart "$GATEWAY_DEPLOY"
  kubectl -n "$NS" rollout status "$GATEWAY_DEPLOY" --timeout=180s
}

restore_gateway_config() {
  [[ -s "$ORIGINAL_CONFIG_JSON" ]] || return 0
  local patch
  patch="$(python3 - "$ORIGINAL_CONFIG_JSON" <<'PY'
import json
import sys

keys = [
    "GO_GATEWAY_DEFAULT_CALLBACK_URL",
    "GO_GATEWAY_CALLBACK_ALLOWED_HOSTS",
    "GO_GATEWAY_CALLBACK_ALLOW_PRIVATE_HOSTS",
    "GO_GATEWAY_CALLBACK_FIRE_AND_FORGET",
]
with open(sys.argv[1], "r", encoding="utf-8") as fh:
    original = json.load(fh)
data = original.get("data") or {}
print(json.dumps({"data": {key: data[key] if key in data else None for key in keys}}, separators=(",", ":")))
PY
)"
  log "恢复 Gateway callback ConfigMap，并重启 Gateway"
  kubectl -n "$NS" patch configmap "$GATEWAY_CONFIGMAP" --type merge -p "$patch" >/dev/null
  kubectl -n "$NS" rollout restart "$GATEWAY_DEPLOY" >/dev/null
  kubectl -n "$NS" rollout status "$GATEWAY_DEPLOY" --timeout=180s >/dev/null
}

delete_mock_upstream() {
  log "删除 mock callback receiver：$SINK_NAME"
  kubectl -n "$NS" delete "service/${SINK_NAME}" "deployment/${SINK_NAME}" "configmap/${SINK_NAME}" --ignore-not-found >/dev/null
}

start_evidence_capture() {
  log "开始采集 mock receiver 日志：$EVIDENCE_FILE"
  : >"$EVIDENCE_FILE"
  chmod 600 "$EVIDENCE_FILE"
  kubectl -n "$NS" logs -f "deploy/${SINK_NAME}" --since=10m >"$EVIDENCE_FILE" 2>"$OUT_DIR/mock-upstream-logs.stderr" &
  LOGS_PID="$!"
  sleep 1
}

run_validation() {
  local validate_out_dir="$OUT_DIR/validate"
  mkdir -p "$validate_out_dir"
  chmod 700 "$validate_out_dir"
  log "启动 callback files mock E2E 验收；validate_out_dir=$validate_out_dir"
  API_KEY="$API_KEY" NS="$NS" USER_ID="$USER_ID" GATEWAY_DEPLOY="$GATEWAY_DEPLOY" \
    bash "$VALIDATE_SCRIPT" \
      "${VALIDATE_ARGS[@]}" \
      --live-e2e \
      --out-dir "$validate_out_dir" \
      --expected-callback-url "$(mock_callback_url)" \
      --upstream-evidence-file "$EVIDENCE_FILE" \
      --upstream-evidence-wait-seconds "$MOCK_EVIDENCE_WAIT_SECONDS"
}

cleanup() {
  local code=$?
  if [[ -n "$LOGS_PID" ]]; then
    kill "$LOGS_PID" >/dev/null 2>&1 || true
    wait "$LOGS_PID" >/dev/null 2>&1 || true
  fi
  if [[ "$RESTORE_GATEWAY_CONFIG" == "true" ]]; then
    restore_gateway_config || true
  fi
  if [[ "$KEEP_MOCK_UPSTREAM" != "true" ]]; then
    delete_mock_upstream || true
  fi
  log "证据目录：$OUT_DIR"
  exit "$code"
}

main() {
  parse_args "$@"
  init_output
  trap cleanup EXIT

  require_cmd kubectl
  require_cmd python3
  [[ -x "$VALIDATE_SCRIPT" || -f "$VALIDATE_SCRIPT" ]] || die "找不到 validate 脚本：$VALIDATE_SCRIPT"
  [[ -n "$NS" ]] || die "必须传 --namespace"
  [[ -n "$USER_ID" ]] || die "必须传 --user-id"
  [[ "$CONFIRM_LIVE_WRITE" == "true" ]] || die "必须传 --confirm-live-write"
  [[ "$CONFIRM_PATCH_GATEWAY" == "true" ]] || die "必须传 --confirm-patch-gateway"
  require_nonnegative_int "MOCK_EVIDENCE_WAIT_SECONDS" "$MOCK_EVIDENCE_WAIT_SECONDS"

  log "namespace=$NS"
  log "user_id=$USER_ID"
  log "gateway_deploy=$GATEWAY_DEPLOY"
  log "gateway_configmap=$GATEWAY_CONFIGMAP"
  log "mock_callback_url=$(mock_callback_url)"
  log "restore_gateway_config=$RESTORE_GATEWAY_CONFIG"
  log "keep_mock_upstream=$KEEP_MOCK_UPSTREAM"

  deploy_mock_upstream
  snapshot_gateway_config
  patch_gateway_config_to_mock
  start_evidence_capture
  run_validation
}

main "$@"
