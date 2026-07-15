#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# 中文全链路验收脚本（加固版）：
# - 默认只读，不上传文件、不提交任务、不触发 callback。
# - live 写入必须显式开启，并显式确认。
# - 负向用例默认关闭。
# - Gateway 侧通过、上游侧通过、真实结果文件通过分开判定。

NS="${NS:-}"
MYSQL_POD="${MYSQL_POD:-mysql-0}"
GATEWAY_DEPLOY="${GATEWAY_DEPLOY:-deploy/go-gateway}"
USER_ID="${USER_ID:-}"
OTHER_USER_ID="${OTHER_USER_ID:-}"
BASE_URL="${BASE_URL:-}"
LOCAL_PORT="${LOCAL_PORT:-18080}"
UPLOAD_FILE="${UPLOAD_FILE:-}"
MESSAGE="${MESSAGE:-内网 callback 文件链路验收：请简短回复，并在可能时生成结果文件。}"
POLL_SECONDS="${POLL_SECONDS:-180}"
POLL_INTERVAL="${POLL_INTERVAL:-5}"
CONNECT_TIMEOUT="${CONNECT_TIMEOUT:-5}"
MAX_TIME="${MAX_TIME:-30}"
OUT_DIR="${OUT_DIR:-}"
API_KEY="${API_KEY:-}"
EXPECTED_CALLBACK_URL="${EXPECTED_CALLBACK_URL:-}"
UPSTREAM_EVIDENCE_FILE="${UPSTREAM_EVIDENCE_FILE:-}"
UPSTREAM_EVIDENCE_WAIT_SECONDS="${UPSTREAM_EVIDENCE_WAIT_SECONDS:-0}"
CALLBACK_DELIVERY_MODE="${CALLBACK_DELIVERY_MODE:-http-response}"

READ_ONLY_INSPECT="true"
LIVE_E2E="false"
CONFIRM_LIVE_WRITE="false"
RUN_NEGATIVE="false"
REQUIRE_REAL_RESULT="false"
ALLOW_PROD_CALLBACK="false"
ALLOW_READ_GATEWAY_KEY="false"
ALLOW_PUBLIC_UNAUTHENTICATED="false"
ALLOW_MYSQL_ROOT="false"
INCLUDE_GLOBAL_ERRORS="false"

RUN_ID="$(date +%Y%m%d%H%M%S)-$$"
if [[ -z "$OUT_DIR" ]]; then
  OUT_DIR="/tmp/ironagent-callback-files-${RUN_ID}"
fi

RUN_LOG=""
SUMMARY_FILE=""
MYSQL_STDERR=""
KUBECTL_STDERR=""

init_output_files() {
  mkdir -p "$OUT_DIR"
  chmod 700 "$OUT_DIR"
  RUN_LOG="$OUT_DIR/run.log"
  SUMMARY_FILE="$OUT_DIR/summary.txt"
  MYSQL_STDERR="$OUT_DIR/mysql.stderr"
  KUBECTL_STDERR="$OUT_DIR/kubectl.stderr"
  : >"$RUN_LOG"
  : >"$SUMMARY_FILE"
  : >"$MYSQL_STDERR"
  : >"$KUBECTL_STDERR"
  chmod 600 "$RUN_LOG" "$SUMMARY_FILE" "$MYSQL_STDERR" "$KUBECTL_STDERR"
}

PORT_FORWARD_PID=""
FAIL_COUNT=0
WARN_COUNT=0
SKIP_COUNT=0
TASK_ID=""
UPLOADED_FILE_ID=""
GATEWAY_RESULT="NOT_RUN"
UPSTREAM_RESULT="NOT_VERIFIED"
FILE_SOURCE_RESULT="NOT_VERIFIED"
CALLBACK_URL=""
CALLBACK_FIRE_AND_FORGET=""
PROGRESS_CALLBACK_ENABLED=""
PROGRESS_CALLBACK_BATCH_SIZE=""
PROGRESS_CALLBACK_INTERVAL=""

usage() {
  cat <<'EOF'
用法：
  scripts/k8s/validate_callback_files_flow_cn.sh --namespace ironagent-kf [模式] [选项]

默认模式：
  --read-only-inspect         只读预检，默认开启；不上传、不提交任务、不触发 callback。

live 写入模式：
  --live-e2e                  执行真实上传文件 + 提交任务 + 等待 callback。
  --confirm-live-write        live 写入二次确认；没有这个参数会阻断。

常用选项：
  --namespace NS              必填。K8s namespace，例如 ironagent-kf。
  --user-id USER_ID           live 写入必填。测试用户，例如 12025772。
  --base-url URL              Gateway 访问地址；不传时 live 模式会 port-forward 到本机。
  --local-port PORT           port-forward 本地端口，默认 18080。
  --file PATH                 上传测试文件；不传时 live 模式自动生成 test.sh。
  --poll-seconds N            等待 callback 最长秒数，默认 180。
  --poll-interval N           轮询间隔秒数，默认 5。
  --out-dir DIR               证据目录，默认 /tmp/ironagent-callback-files-*。
  --expected-callback-url URL 期望的 Gateway callback URL；不一致会阻断 live。
  --callback-delivery-mode MODE
                              callback 投递验收模式：默认 http-response。
                              http-response 要求等待上游 2xx；transport-only 要求 fire-and-forget；
                              any 只检查 URL 安全边界。
  --upstream-evidence-file F  上游原始 body/日志证据文件；用于标记上游侧通过。
  --upstream-evidence-wait-seconds N
                              等待上游证据文件出现匹配 body 的秒数，默认 0。

风险开关：
  --run-negative              显式开启失败注入；默认关闭。
  --require-real-result       要求真实 result_workspace_file_id；热修注入会失败。
  --allow-prod-callback       允许把 callback 推到真实上游地址；默认阻断。
  --allow-read-gateway-key    允许从 go-gateway Pod 读取 GO_GATEWAY_API_KEYS；默认不读。
  --allow-public-unauthenticated
                              允许不带 X-API-Key 调公开接口；默认要求 API_KEY。
  --allow-mysql-root          允许使用 mysql-0 中 MYSQL_ROOT_PASSWORD 做 SELECT；默认不查 DB。
  --include-global-errors     日志额外抓 WARN/ERROR；默认只按 task_id/request_id 抓。

环境变量：
  API_KEY                     推荐用短期测试 key。脚本不会打印明文。
  NS / USER_ID / BASE_URL / OUT_DIR 等同名变量可作为默认值。

结果分级：
  INPUT_FILE_OK               上传和输入附件链路通过。
  DEPRECATED_HOTFIX_INJECTED  callback files[] 来自已废弃热修注入；该结果会失败。
  REAL_RESULT_FILE_OK         result_workspace_file_id 非空，callback 文件来自真实结果。
  GATEWAY_CALLBACK_OK         Gateway outbox delivered，且 last_error 为空。
  UPSTREAM_CONFIRMED          上游证据中存在同一 callback body，且 taskId 和完整 files[] 与 outbox 一致。
EOF
}

log() {
  local msg="$*"
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$msg" | tee -a "$RUN_LOG" >&2
}

section() {
  log ""
  log "===== $* ====="
}

pass_case() {
  log "通过：$*"
}

warn_case() {
  WARN_COUNT=$((WARN_COUNT + 1))
  log "警告：$*"
}

fail_case() {
  FAIL_COUNT=$((FAIL_COUNT + 1))
  log "失败：$*"
}

skip_case() {
  SKIP_COUNT=$((SKIP_COUNT + 1))
  log "跳过：$*"
}

finalize() {
  {
    echo "run_id=$RUN_ID"
    echo "mode=$([[ "$LIVE_E2E" == "true" ]] && echo live-e2e || echo read-only-inspect)"
    echo "namespace=${NS:-}"
    echo "base_url=${BASE_URL:-}"
    echo "user_id=${USER_ID:-}"
    echo "uploaded_file_id=${UPLOADED_FILE_ID:-}"
    echo "task_id=${TASK_ID:-}"
    echo "callback_delivery_mode=$CALLBACK_DELIVERY_MODE"
    echo "gateway_result=$GATEWAY_RESULT"
    echo "upstream_result=$UPSTREAM_RESULT"
    echo "file_source_result=$FILE_SOURCE_RESULT"
    echo "fail_count=$FAIL_COUNT"
    echo "warn_count=$WARN_COUNT"
    echo "skip_count=$SKIP_COUNT"
    echo "artifacts=$OUT_DIR"
  } >"$SUMMARY_FILE"
  chmod 600 "$SUMMARY_FILE"
  log "汇总文件：$SUMMARY_FILE"
  log "证据目录：$OUT_DIR"
}

cleanup() {
  if [[ -n "$PORT_FORWARD_PID" ]]; then
    kill "$PORT_FORWARD_PID" >/dev/null 2>&1 || true
    wait "$PORT_FORWARD_PID" >/dev/null 2>&1 || true
  fi
}

on_err() {
  local line="$1"
  local cmd="$2"
  local code="$3"
  fail_case "脚本异常退出：line=$line exit=$code cmd=$cmd"
  finalize
  cleanup
  exit "$code"
}

die() {
  fail_case "$*"
  finalize
  exit 1
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    die "缺少命令：$1"
  fi
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

require_positive_int() {
  local name="$1"
  local value="$2"
  if ! [[ "$value" =~ ^[0-9]+$ ]] || [[ "$value" -le 0 ]]; then
    die "$name 必须是正整数，当前值：$value"
  fi
}

require_nonnegative_int() {
  local name="$1"
  local value="$2"
  if ! [[ "$value" =~ ^[0-9]+$ ]]; then
    die "$name 必须是非负整数，当前值：$value"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --read-only-inspect)
      READ_ONLY_INSPECT="true"
      LIVE_E2E="false"
      shift
      ;;
    --live-e2e)
      LIVE_E2E="true"
      READ_ONLY_INSPECT="false"
      shift
      ;;
    --confirm-live-write)
      CONFIRM_LIVE_WRITE="true"
      shift
      ;;
    --namespace|-n)
      require_arg "$1" "${2:-}"
      NS="$2"
      shift 2
      ;;
    --user-id)
      require_arg "$1" "${2:-}"
      USER_ID="$2"
      shift 2
      ;;
    --other-user-id)
      require_arg "$1" "${2:-}"
      OTHER_USER_ID="$2"
      shift 2
      ;;
    --base-url)
      require_arg "$1" "${2:-}"
      BASE_URL="$2"
      shift 2
      ;;
    --local-port)
      require_arg "$1" "${2:-}"
      LOCAL_PORT="$2"
      shift 2
      ;;
    --file)
      require_arg "$1" "${2:-}"
      UPLOAD_FILE="$2"
      shift 2
      ;;
    --message)
      require_arg "$1" "${2:-}"
      MESSAGE="$2"
      shift 2
      ;;
    --poll-seconds)
      require_arg "$1" "${2:-}"
      POLL_SECONDS="$2"
      shift 2
      ;;
    --poll-interval)
      require_arg "$1" "${2:-}"
      POLL_INTERVAL="$2"
      shift 2
      ;;
    --out-dir)
      require_arg "$1" "${2:-}"
      OUT_DIR="$2"
      shift 2
      ;;
    --expected-callback-url)
      require_arg "$1" "${2:-}"
      EXPECTED_CALLBACK_URL="$2"
      shift 2
      ;;
    --callback-delivery-mode)
      require_arg "$1" "${2:-}"
      CALLBACK_DELIVERY_MODE="$2"
      shift 2
      ;;
    --upstream-evidence-file)
      require_arg "$1" "${2:-}"
      UPSTREAM_EVIDENCE_FILE="$2"
      shift 2
      ;;
    --upstream-evidence-wait-seconds)
      require_arg "$1" "${2:-}"
      UPSTREAM_EVIDENCE_WAIT_SECONDS="$2"
      shift 2
      ;;
    --run-negative)
      RUN_NEGATIVE="true"
      shift
      ;;
    --skip-negative)
      RUN_NEGATIVE="false"
      shift
      ;;
    --require-real-result)
      REQUIRE_REAL_RESULT="true"
      shift
      ;;
    --allow-prod-callback)
      ALLOW_PROD_CALLBACK="true"
      shift
      ;;
    --allow-read-gateway-key)
      ALLOW_READ_GATEWAY_KEY="true"
      shift
      ;;
    --allow-public-unauthenticated)
      ALLOW_PUBLIC_UNAUTHENTICATED="true"
      shift
      ;;
    --allow-mysql-root)
      ALLOW_MYSQL_ROOT="true"
      shift
      ;;
    --include-global-errors)
      INCLUDE_GLOBAL_ERRORS="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "未知参数：$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$OTHER_USER_ID" && -n "$USER_ID" ]]; then
  OTHER_USER_ID="${USER_ID}-other-${RUN_ID}"
fi

init_output_files
trap cleanup EXIT
trap 'on_err "$LINENO" "$BASH_COMMAND" "$?"' ERR

require_positive_int "LOCAL_PORT" "$LOCAL_PORT"
require_positive_int "POLL_SECONDS" "$POLL_SECONDS"
require_positive_int "POLL_INTERVAL" "$POLL_INTERVAL"
require_positive_int "CONNECT_TIMEOUT" "$CONNECT_TIMEOUT"
require_positive_int "MAX_TIME" "$MAX_TIME"
require_nonnegative_int "UPSTREAM_EVIDENCE_WAIT_SECONDS" "$UPSTREAM_EVIDENCE_WAIT_SECONDS"
case "$CALLBACK_DELIVERY_MODE" in
  http-response|transport-only|any)
    ;;
  fire-and-forget)
    CALLBACK_DELIVERY_MODE="transport-only"
    ;;
  *)
    die "CALLBACK_DELIVERY_MODE 只能是 http-response、transport-only、fire-and-forget 或 any，当前值：$CALLBACK_DELIVERY_MODE"
    ;;
esac

sql_escape() {
  python3 - "$1" <<'PY'
import sys
print(sys.argv[1].replace("\\", "\\\\").replace("'", "''"))
PY
}

json_get_file() {
  local file="$1"
  shift
  python3 - "$file" "$@" <<'PY'
import json
import sys

filename = sys.argv[1]
paths = sys.argv[2:]
try:
    with open(filename, "r", encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    sys.exit(0)

def lookup(obj, path):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            cur = None
        if cur is None:
            return None
    return cur

for path in paths:
    value = lookup(data, path)
    if value not in (None, ""):
        if isinstance(value, (dict, list)):
            print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        else:
            print(value)
        break
PY
}

extract_task_id() {
  json_get_file "$1" taskId task_id task.id task.taskId task.task_id id
}

gateway_exec() {
  kubectl -n "$NS" exec "$GATEWAY_DEPLOY" -- sh -lc "$1" 2>>"$KUBECTL_STDERR"
}

mysql_raw() {
  local sql="$1"
  if [[ "$ALLOW_MYSQL_ROOT" != "true" ]]; then
    log "MySQL 查询被安全门阻断：未传 --allow-mysql-root。"
    return 78
  fi
  kubectl -n "$NS" exec -i "$MYSQL_POD" -- sh -lc \
    'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot "$MYSQL_DATABASE" --batch --raw --skip-column-names' \
    <<<"$sql" 2>>"$MYSQL_STDERR"
}

mysql_table() {
  local sql="$1"
  if [[ "$ALLOW_MYSQL_ROOT" != "true" ]]; then
    log "MySQL 表格查询被安全门阻断：未传 --allow-mysql-root。"
    return 78
  fi
  kubectl -n "$NS" exec -i "$MYSQL_POD" -- sh -lc \
    'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot "$MYSQL_DATABASE" --table' \
    <<<"$sql" 2>>"$MYSQL_STDERR"
}

curl_run() {
  local output="$1"
  local stderr_file="$2"
  shift 2
  : >"$output"
  : >"$stderr_file"
  local http_code curl_exit
  set +e
  http_code="$(curl -sS --connect-timeout "$CONNECT_TIMEOUT" --max-time "$MAX_TIME" -o "$output" -w "%{http_code}" "$@" 2>"$stderr_file")"
  curl_exit=$?
  set -e
  printf '%s\t%s\n' "$curl_exit" "$http_code"
}

curl_auth_headers=()
build_curl_auth_headers() {
  curl_auth_headers=()
  if [[ -n "$API_KEY" ]]; then
    curl_auth_headers=(-H "X-API-Key: ${API_KEY}")
  fi
}

start_port_forward_if_needed() {
  if [[ -n "$BASE_URL" ]]; then
    BASE_URL="${BASE_URL%/}"
    log "使用外部传入 Gateway 地址：$BASE_URL"
    return
  fi

  BASE_URL="http://127.0.0.1:${LOCAL_PORT}"
  log "未传 --base-url，启动 kubectl port-forward：$GATEWAY_DEPLOY ${LOCAL_PORT}:8080"
  kubectl -n "$NS" port-forward "$GATEWAY_DEPLOY" "${LOCAL_PORT}:8080" >"$OUT_DIR/port-forward.log" 2>&1 &
  PORT_FORWARD_PID="$!"

  for _ in $(seq 1 30); do
    if curl -sS --connect-timeout 1 --max-time 2 "${BASE_URL}/api/v1/health" >/dev/null 2>&1; then
      pass_case "port-forward 已可访问：$BASE_URL"
      return
    fi
    sleep 1
  done

  sed 's/^/  /' "$OUT_DIR/port-forward.log" | tee -a "$RUN_LOG" >&2 || true
  die "port-forward 未能访问 Gateway"
}

load_gateway_env() {
  section "Gateway callback 运行时配置"
  local env_file="$OUT_DIR/go-gateway-callback-env.txt"
  gateway_exec 'env | grep -E "GO_GATEWAY_DEFAULT_CALLBACK_URL|GO_GATEWAY_CALLBACK_ALLOWED_HOSTS|GO_GATEWAY_CALLBACK_ALLOW_PRIVATE_HOSTS|GO_GATEWAY_CALLBACK_FIRE_AND_FORGET|GO_GATEWAY_CALLBACK_ACCEPT_ANY_RESPONSE|GO_GATEWAY_CALLBACK_REQUIRED|GO_GATEWAY_CALLBACK_WORKER_INTERVAL|GO_GATEWAY_CALLBACK_PENDING_LIMIT|GO_GATEWAY_CALLBACK_LEASE_DURATION|GO_GATEWAY_CALLBACK_MAX_ATTEMPTS|GO_GATEWAY_TASK_PROGRESS_CALLBACK_ENABLED|GO_GATEWAY_TASK_PROGRESS_CALLBACK_BATCH_SIZE|GO_GATEWAY_TASK_PROGRESS_CALLBACK_INTERVAL|GO_GATEWAY_TASK_PROGRESS_CALLBACK_STEP_MAX_CHARS|GO_GATEWAY_TASK_PROGRESS_CALLBACK_STATE_TTL|GO_GATEWAY_ALLOW_PUBLIC_UNAUTHENTICATED" | sort' \
    >"$env_file" || die "读取 go-gateway callback 环境变量失败"
  chmod 600 "$env_file"
  sed 's/^/  /' "$env_file" | tee -a "$RUN_LOG" >&2
  CALLBACK_URL="$(grep '^GO_GATEWAY_DEFAULT_CALLBACK_URL=' "$env_file" | cut -d= -f2- || true)"
  CALLBACK_FIRE_AND_FORGET="$(grep '^GO_GATEWAY_CALLBACK_FIRE_AND_FORGET=' "$env_file" | cut -d= -f2- || true)"
  PROGRESS_CALLBACK_ENABLED="$(grep '^GO_GATEWAY_TASK_PROGRESS_CALLBACK_ENABLED=' "$env_file" | cut -d= -f2- || true)"
  PROGRESS_CALLBACK_BATCH_SIZE="$(grep '^GO_GATEWAY_TASK_PROGRESS_CALLBACK_BATCH_SIZE=' "$env_file" | cut -d= -f2- || true)"
  PROGRESS_CALLBACK_INTERVAL="$(grep '^GO_GATEWAY_TASK_PROGRESS_CALLBACK_INTERVAL=' "$env_file" | cut -d= -f2- || true)"
  if [[ -z "$CALLBACK_URL" ]]; then
    warn_case "未读取到 GO_GATEWAY_DEFAULT_CALLBACK_URL；live-e2e 会阻断。"
  fi
  if [[ -z "$CALLBACK_FIRE_AND_FORGET" ]]; then
    warn_case "未读取到 GO_GATEWAY_CALLBACK_FIRE_AND_FORGET；live-e2e 将按 callback-delivery-mode=$CALLBACK_DELIVERY_MODE 判定。"
  fi
  if [[ "$PROGRESS_CALLBACK_ENABLED" != "true" ]]; then
    warn_case "GO_GATEWAY_TASK_PROGRESS_CALLBACK_ENABLED=${PROGRESS_CALLBACK_ENABLED:-<空>}；不会按执行步骤批量回调 progress。"
  fi
  if [[ "$PROGRESS_CALLBACK_BATCH_SIZE" != "5" ]]; then
    warn_case "GO_GATEWAY_TASK_PROGRESS_CALLBACK_BATCH_SIZE=${PROGRESS_CALLBACK_BATCH_SIZE:-<空>}；当前不是 5 条执行过程一批。"
  fi
  if [[ "$PROGRESS_CALLBACK_INTERVAL" != "90s" ]]; then
    warn_case "GO_GATEWAY_TASK_PROGRESS_CALLBACK_INTERVAL=${PROGRESS_CALLBACK_INTERVAL:-<空>}；若 interval 过短，可能在不足 5 条时提前 flush。"
  fi
}

load_api_key_if_allowed() {
  if [[ -n "$API_KEY" ]]; then
    log "使用外部传入 API_KEY；日志中不会打印密钥明文。"
    return
  fi
  if [[ "$ALLOW_READ_GATEWAY_KEY" == "true" ]]; then
    API_KEY="$(gateway_exec 'printf "%s" "$GO_GATEWAY_API_KEYS" | cut -d, -f1' | tr -d '\r\n' || true)"
    if [[ -n "$API_KEY" ]]; then
      log "已按 --allow-read-gateway-key 从 go-gateway Pod 读取首个 GO_GATEWAY_API_KEYS；日志中不会打印密钥明文。"
      return
    fi
  fi
  if [[ "$ALLOW_PUBLIC_UNAUTHENTICATED" == "true" ]]; then
    warn_case "未使用 API_KEY，将按 --allow-public-unauthenticated 进行公开接口调用。"
    return
  fi
  die "live-e2e 需要 API_KEY；或显式传 --allow-read-gateway-key / --allow-public-unauthenticated。"
}

guard_live_mode() {
  if [[ "$LIVE_E2E" != "true" ]]; then
    return
  fi
  [[ "$CONFIRM_LIVE_WRITE" == "true" ]] || die "live-e2e 会写线上数据，必须显式传 --confirm-live-write。"
  [[ -n "$NS" ]] || die "live-e2e 必须显式传 --namespace。"
  [[ -n "$USER_ID" ]] || die "live-e2e 必须显式传 --user-id。"
  [[ "$ALLOW_MYSQL_ROOT" == "true" ]] || die "live-e2e 当前需要 DB 证据；若没有只读账号支持，必须显式传 --allow-mysql-root。"
  if [[ "$RUN_NEGATIVE" == "true" && "$ALLOW_PROD_CALLBACK" == "true" ]]; then
    die "禁止同时对真实上游执行失败注入；请先去掉 --run-negative。"
  fi
}

guard_callback_mode() {
  if [[ "$LIVE_E2E" != "true" ]]; then
    return
  fi
  [[ -n "$CALLBACK_URL" ]] || die "GO_GATEWAY_DEFAULT_CALLBACK_URL 为空，不能执行 live-e2e。"
  if [[ -n "$EXPECTED_CALLBACK_URL" && "$CALLBACK_URL" != "$EXPECTED_CALLBACK_URL" ]]; then
    die "callback URL 与 --expected-callback-url 不一致：actual=$CALLBACK_URL expected=$EXPECTED_CALLBACK_URL"
  fi
  if [[ "$ALLOW_PROD_CALLBACK" != "true" ]]; then
    case "$CALLBACK_URL" in
      *callback-receiver*|*callback-sink*|*sink*|http://127.0.0.1:*|http://localhost:*)
        pass_case "callback URL 看起来是测试 sink：$CALLBACK_URL"
        ;;
      *)
        die "callback URL 看起来是真实上游：$CALLBACK_URL；必须传 --allow-prod-callback 或改为测试 sink。"
        ;;
    esac
  else
    warn_case "已显式允许真实上游 callback：$CALLBACK_URL"
  fi
  case "$CALLBACK_DELIVERY_MODE" in
    http-response)
      if [[ "$CALLBACK_FIRE_AND_FORGET" != "false" ]]; then
        die "GO_GATEWAY_CALLBACK_FIRE_AND_FORGET=$CALLBACK_FIRE_AND_FORGET；http-response 模式要求等待上游 2xx，必须设置为 false。"
      fi
      pass_case "callback 投递模式满足当前验收：http-response，Gateway 等待上游 2xx 后进入 delivered"
      ;;
    transport-only)
      if [[ "$CALLBACK_FIRE_AND_FORGET" != "true" ]]; then
        die "GO_GATEWAY_CALLBACK_FIRE_AND_FORGET=$CALLBACK_FIRE_AND_FORGET；transport-only 模式只验证写出请求，必须设置为 true。"
      fi
      pass_case "callback 投递模式满足当前验收：transport-only，只验证 Gateway 写出请求并进入 delivered"
      ;;
    any)
      warn_case "已跳过 GO_GATEWAY_CALLBACK_FIRE_AND_FORGET 语义检查：callback-delivery-mode=any"
      ;;
  esac
}

inspect_context() {
  section "K8s 上下文"
  local ctx server
  ctx="$(kubectl config current-context 2>>"$KUBECTL_STDERR" || true)"
  server="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' 2>>"$KUBECTL_STDERR" || true)"
  log "current_context=${ctx:-<空>}"
  log "cluster_server=${server:-<空>}"
  [[ -n "$ctx" ]] || die "当前没有可用 K8s context。"
  [[ -n "$NS" ]] || die "必须显式传 --namespace。"
  kubectl -n "$NS" get "$GATEWAY_DEPLOY" >"$OUT_DIR/go-gateway-deploy.txt" 2>>"$KUBECTL_STDERR" \
    || die "找不到 $NS/$GATEWAY_DEPLOY"
  pass_case "已确认 Gateway deployment 存在：$NS/$GATEWAY_DEPLOY"
}

inspect_triggers() {
  section "只读检查：gateway_event_outbox trigger"
  if [[ "$ALLOW_MYSQL_ROOT" != "true" ]]; then
    skip_case "未传 --allow-mysql-root，跳过 trigger/DB 检查。"
    return
  fi
  mysql_table "
SELECT TRIGGER_NAME, EVENT_MANIPULATION, ACTION_TIMING
FROM information_schema.TRIGGERS
WHERE TRIGGER_SCHEMA = DATABASE()
  AND EVENT_OBJECT_TABLE = 'gateway_event_outbox'
ORDER BY TRIGGER_NAME;" >"$OUT_DIR/outbox-triggers.txt" \
    || die "读取 outbox trigger 列表失败"
  sed 's/^/  /' "$OUT_DIR/outbox-triggers.txt" | tee -a "$RUN_LOG" >&2
  if [[ ! -s "$OUT_DIR/outbox-triggers.txt" ]]; then
    warn_case "gateway_event_outbox 当前没有 trigger；如果依赖 trigger 热修注入 files[]，该热修未生效。"
  fi
  mysql_raw "
SELECT JSON_OBJECT(
  'trigger_name', TRIGGER_NAME,
  'event', EVENT_MANIPULATION,
  'timing', ACTION_TIMING,
  'statement', ACTION_STATEMENT
)
FROM information_schema.TRIGGERS
WHERE TRIGGER_SCHEMA = DATABASE()
  AND EVENT_OBJECT_TABLE = 'gateway_event_outbox'
ORDER BY TRIGGER_NAME;" >"$OUT_DIR/outbox-trigger-definitions.jsonl" \
    || die "读取 outbox trigger 定义失败"
  chmod 600 "$OUT_DIR/outbox-trigger-definitions.jsonl"
  log "trigger 定义已保存：$OUT_DIR/outbox-trigger-definitions.jsonl"
  if grep -Eiq 'payload_json|(^|[^[:alnum:]_])files([^[:alnum:]_]|$)|hotfix-|/root/sh' "$OUT_DIR/outbox-trigger-definitions.jsonl"; then
    fail_case "检测到 gateway_event_outbox trigger 触碰 payload_json/files；请移除后再验收真实 callback 文件链路"
  fi
}

prepare_upload_file() {
  if [[ -n "$UPLOAD_FILE" ]]; then
    [[ -f "$UPLOAD_FILE" ]] || die "指定上传文件不存在：$UPLOAD_FILE"
    log "使用传入上传文件：$UPLOAD_FILE"
    return
  fi
  UPLOAD_FILE="$OUT_DIR/test.sh"
  cat >"$UPLOAD_FILE" <<'EOF'
#!/usr/bin/env sh
echo "ironagent callback files validation"
EOF
  chmod 600 "$UPLOAD_FILE"
  log "已生成测试上传文件：$UPLOAD_FILE"
}

upload_file() {
  local response="$OUT_DIR/upload-response.json"
  local stderr_file="$OUT_DIR/upload.stderr"
  local result curl_exit http_code
  result="$(curl_run "$response" "$stderr_file" "${curl_auth_headers[@]}" -F "user_id=${USER_ID}" -F "file=@${UPLOAD_FILE};type=text/plain" "${BASE_URL}/api/v1/files/upload")"
  curl_exit="$(printf '%s' "$result" | cut -f1)"
  http_code="$(printf '%s' "$result" | cut -f2)"
  log "上传文件 HTTP=$http_code curl_exit=$curl_exit 响应：$(tr '\n' ' ' <"$response")"
  [[ "$curl_exit" == "0" ]] || die "上传文件 curl 失败，stderr=$(tr '\n' ' ' <"$stderr_file")"
  UPLOADED_FILE_ID="$(json_get_file "$response" file_id fileId id)"
  if [[ "$http_code" =~ ^2 && -n "$UPLOADED_FILE_ID" ]]; then
    pass_case "上传成功，file_id=$UPLOADED_FILE_ID"
  else
    die "上传失败或响应缺少 file_id"
  fi
}

workspace_file_json() {
  local file_id="$1"
  local escaped
  escaped="$(sql_escape "$file_id")"
  mysql_raw "
SELECT JSON_OBJECT(
  'count', COUNT(*),
  'file_id', COALESCE(MAX(file_id), ''),
  'assistant_id', COALESCE(MAX(assistant_id), ''),
  'bucket', COALESCE(MAX(bucket), ''),
  'filename', COALESCE(MAX(filename), ''),
  'uploader_user_id', COALESCE(MAX(uploader_user_id), ''),
  'deleted', COALESCE(MAX(CAST(deleted AS UNSIGNED)), 0)
)
FROM workspace_files
WHERE file_id = '${escaped}';"
}

callback_file_ids_sql_list() {
  local outbox_file="$1"
  python3 - "$outbox_file" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)

ids = []
for item in data.get("files") or []:
    if not isinstance(item, dict):
        continue
    file_id = str(item.get("file_id") or item.get("fileId") or "").strip()
    if file_id and file_id not in ids:
        ids.append(file_id)

def quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"

print(",".join(quote(item) for item in ids))
PY
}

result_workspace_files_json() {
  local outbox_file="$1"
  local sql_ids
  sql_ids="$(callback_file_ids_sql_list "$outbox_file")"
  if [[ -z "$sql_ids" ]]; then
    printf '[]\n'
    return 0
  fi
  mysql_raw "
SELECT COALESCE(JSON_ARRAYAGG(JSON_OBJECT(
  'file_id', COALESCE(file_id, ''),
  'assistant_id', COALESCE(assistant_id, ''),
  'bucket', COALESCE(bucket, ''),
  'filename', COALESCE(filename, ''),
  'relative_path', COALESCE(relative_path, ''),
  'owner_user_id', COALESCE(owner_user_id, ''),
  'uploader_user_id', COALESCE(uploader_user_id, ''),
  'source_type', COALESCE(source_type, ''),
  'source_ref', COALESCE(source_ref, ''),
  'direction', COALESCE(direction, ''),
  'file_role', COALESCE(file_role, ''),
  'size_bytes', COALESCE(size_bytes, 0),
  'content_type', COALESCE(content_type, ''),
  'checksum_sha256', COALESCE(checksum_sha256, checksum, ''),
  'deleted', COALESCE(CAST(deleted AS UNSIGNED), 0)
)), JSON_ARRAY())
FROM workspace_files
WHERE file_id IN (${sql_ids});"
}

assert_workspace_file() {
  local file="$OUT_DIR/workspace-file.json"
  workspace_file_json "$UPLOADED_FILE_ID" >"$file" || die "查询 workspace_files 失败"
  chmod 600 "$file"
  log "workspace_files 证据：$(cat "$file")"
  local count owner deleted
  count="$(json_get_file "$file" count)"
  owner="$(json_get_file "$file" uploader_user_id)"
  deleted="$(json_get_file "$file" deleted)"
  [[ "$count" == "1" ]] || fail_case "workspace_files 中 file_id=$UPLOADED_FILE_ID 记录数不是 1：$count"
  [[ "$owner" == "$USER_ID" ]] || fail_case "workspace_files.uploader_user_id 不匹配：actual=${owner:-<空>} expected=$USER_ID"
  [[ "$deleted" == "0" ]] || fail_case "workspace_files.deleted 不是 0：$deleted"
  if [[ "$count" == "1" && "$owner" == "$USER_ID" && "$deleted" == "0" ]]; then
    pass_case "输入文件落库和归属校验通过"
  fi
}

write_chat_body() {
  local request_id="$1"
  local conversation_id="$2"
  local user_id="$3"
  local message="$4"
  local file_id="$5"
  local output="$6"
  REQUEST_ID="$request_id" \
  CONVERSATION_ID="$conversation_id" \
  USER_ID_VALUE="$user_id" \
  MESSAGE_VALUE="$message" \
  FILE_ID_VALUE="$file_id" \
  python3 - "$output" <<'PY'
import json
import os
import sys

payload = {
    "request_id": os.environ["REQUEST_ID"],
    "conversation_id": os.environ["CONVERSATION_ID"],
    "user_id": os.environ["USER_ID_VALUE"],
    "message": os.environ["MESSAGE_VALUE"],
}
file_id = os.environ.get("FILE_ID_VALUE", "").strip()
if file_id:
    payload["files"] = [file_id]
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
PY
}

submit_chat() {
  local label="$1"
  local user_id="$2"
  local file_id="$3"
  local body="$OUT_DIR/chat-${label}.json"
  local response="$OUT_DIR/chat-${label}-response.json"
  local stderr_file="$OUT_DIR/chat-${label}.stderr"
  local request_id="req-${label}-${RUN_ID}"
  local conversation_id="conv-${label}-${RUN_ID}"
  write_chat_body "$request_id" "$conversation_id" "$user_id" "$MESSAGE" "$file_id" "$body"
  chmod 600 "$body"
  local result curl_exit http_code
  result="$(curl_run "$response" "$stderr_file" "${curl_auth_headers[@]}" -H "Content-Type: application/json" --data-binary "@$body" "${BASE_URL}/api/v1/chat")"
  curl_exit="$(printf '%s' "$result" | cut -f1)"
  http_code="$(printf '%s' "$result" | cut -f2)"
  log "提交任务[$label] HTTP=$http_code curl_exit=$curl_exit request_id=$request_id user_id=$user_id file_id=${file_id:-<无>}"
  log "提交任务[$label] 响应：$(tr '\n' ' ' <"$response")"
  printf '%s\t%s\t%s\t%s\n' "$curl_exit" "$http_code" "$response" "$request_id"
}

request_created_task_count() {
  local request_id="$1"
  local escaped
  escaped="$(sql_escape "$request_id")"
  mysql_raw "SELECT COUNT(*) FROM gateway_tasks WHERE request_id = '${escaped}';"
}

run_negative_tests() {
  section "失败注入（显式开启）：缺少 file 表单字段"
  local response="$OUT_DIR/upload-missing-file-response.json"
  local stderr_file="$OUT_DIR/upload-missing-file.stderr"
  local result curl_exit http_code
  result="$(curl_run "$response" "$stderr_file" "${curl_auth_headers[@]}" -F "user_id=${USER_ID}" "${BASE_URL}/api/v1/files/upload")"
  curl_exit="$(printf '%s' "$result" | cut -f1)"
  http_code="$(printf '%s' "$result" | cut -f2)"
  log "缺少 file HTTP=$http_code curl_exit=$curl_exit 响应：$(tr '\n' ' ' <"$response")"
  if [[ "$curl_exit" == "0" && "$http_code" =~ ^4 ]]; then
    pass_case "缺少 file 字段被明确 4xx 拒绝"
  else
    fail_case "缺少 file 字段未得到明确 4xx：HTTP=$http_code curl_exit=$curl_exit"
  fi

  section "失败注入（显式开启）：非法 file_id"
  local invalid_file_id="missing-file-${RUN_ID}"
  local submit_result code curl_code resp request_id created_count
  submit_result="$(submit_chat "invalid-file" "$USER_ID" "$invalid_file_id")"
  curl_code="$(printf '%s' "$submit_result" | cut -f1)"
  code="$(printf '%s' "$submit_result" | cut -f2)"
  resp="$(printf '%s' "$submit_result" | cut -f3)"
  request_id="$(printf '%s' "$submit_result" | cut -f4)"
  created_count="$(request_created_task_count "$request_id" | tr -d '\r' || true)"
  if [[ "$curl_code" == "0" && "$code" =~ ^4 && "$(extract_task_id "$resp")" == "" && "${created_count:-0}" == "0" ]]; then
    pass_case "非法 file_id 被明确 4xx 拒绝，且未创建任务"
  else
    fail_case "非法 file_id 校验不符合预期：HTTP=$code curl_exit=$curl_code created_count=${created_count:-<空>}"
  fi

  section "失败注入（显式开启）：跨 user 使用已上传 file_id"
  submit_result="$(submit_chat "cross-user-file" "$OTHER_USER_ID" "$UPLOADED_FILE_ID")"
  curl_code="$(printf '%s' "$submit_result" | cut -f1)"
  code="$(printf '%s' "$submit_result" | cut -f2)"
  resp="$(printf '%s' "$submit_result" | cut -f3)"
  request_id="$(printf '%s' "$submit_result" | cut -f4)"
  created_count="$(request_created_task_count "$request_id" | tr -d '\r' || true)"
  if [[ "$curl_code" == "0" && "$code" =~ ^4 && "$(extract_task_id "$resp")" == "" && "${created_count:-0}" == "0" ]]; then
    pass_case "跨 user 文件引用被明确 4xx 拒绝，且未创建任务"
  else
    fail_case "跨 user 文件引用校验不符合预期：HTTP=$code curl_exit=$curl_code created_count=${created_count:-<空>}"
  fi
}

task_detail_json() {
  local task_id="$1"
  local escaped
  escaped="$(sql_escape "$task_id")"
  mysql_raw "
SELECT JSON_OBJECT(
  'task_id', task_id,
  'status', status,
  'stage', COALESCE(stage, ''),
  'user_id', COALESCE(user_id, ''),
  'target_assistant_id', COALESCE(target_assistant_id, ''),
  'callback_url', COALESCE(callback_url, ''),
  'input_workspace_file_id', COALESCE(JSON_UNQUOTE(JSON_EXTRACT(task_json, '$.workspace_file.file_id')), ''),
  'input_object_id', COALESCE(JSON_UNQUOTE(JSON_EXTRACT(task_json, '$.input_object_id')), ''),
  'result_object_id', COALESCE(JSON_UNQUOTE(JSON_EXTRACT(task_json, '$.result_object_id')), ''),
  'result_workspace_file_id', COALESCE(JSON_UNQUOTE(JSON_EXTRACT(task_json, '$.result_workspace_file_id')), ''),
  'result_workspace_file_ids', COALESCE(
    JSON_EXTRACT(metadata_json, '$.runtime_result_workspace_file_ids'),
    JSON_EXTRACT(task_json, '$.meta_json.runtime_result_workspace_file_ids'),
    JSON_EXTRACT(task_json, '$.metadata.runtime_result_workspace_file_ids'),
    JSON_ARRAY()
  ),
  'runtime_result_file_count', COALESCE(
    JSON_EXTRACT(metadata_json, '$.runtime_result_file_count'),
    JSON_EXTRACT(task_json, '$.meta_json.runtime_result_file_count'),
    JSON_EXTRACT(task_json, '$.metadata.runtime_result_file_count'),
    0
  ),
  'runtime_reply_text', COALESCE(
    JSON_UNQUOTE(JSON_EXTRACT(metadata_json, '$.runtime_reply_text')),
    JSON_UNQUOTE(JSON_EXTRACT(task_json, '$.meta_json.runtime_reply_text')),
    JSON_UNQUOTE(JSON_EXTRACT(task_json, '$.metadata.runtime_reply_text')),
    ''
  )
)
FROM gateway_tasks
WHERE task_id = '${escaped}'
LIMIT 1;"
}

completed_outbox_json() {
  local task_id="$1"
  local escaped
  escaped="$(sql_escape "$task_id")"
  mysql_raw "
SELECT JSON_OBJECT(
  'event_id', event_id,
  'task_id', task_id,
  'event_type', event_type,
  'status', status,
  'attempts', attempts,
  'last_error', COALESCE(last_error, ''),
  'next_attempt_at', COALESCE(CAST(next_attempt_at AS CHAR), ''),
  'delivered_at', COALESCE(CAST(delivered_at AS CHAR), ''),
  'acked_at', COALESCE(CAST(acked_at AS CHAR), ''),
  'payload', payload_json,
  'payload_status', COALESCE(JSON_UNQUOTE(JSON_EXTRACT(payload_json, '$.status')), ''),
  'payload_stage', COALESCE(JSON_UNQUOTE(JSON_EXTRACT(payload_json, '$.stage')), ''),
  'reply_text', COALESCE(
    JSON_UNQUOTE(JSON_EXTRACT(payload_json, '$.replyText')),
    JSON_UNQUOTE(JSON_EXTRACT(payload_json, '$.reply_text')),
    ''
  ),
  'files_len', COALESCE(JSON_LENGTH(JSON_EXTRACT(payload_json, '$.files')), 0),
  'files', JSON_EXTRACT(payload_json, '$.files')
)
FROM gateway_event_outbox
WHERE task_id = '${escaped}'
  AND event_type = 'task.completed'
ORDER BY event_seq DESC
LIMIT 1;"
}

fetch_public_task_payload() {
  local task_id="$1"
  local output="$2"
  local stderr_file="$3"
  local result curl_exit http_code
  result="$(curl_run "$output" "$stderr_file" "${curl_auth_headers[@]}" "${BASE_URL}/api/v1/tasks/${task_id}")"
  curl_exit="$(printf '%s' "$result" | cut -f1)"
  http_code="$(printf '%s' "$result" | cut -f2)"
  log "查询 fallback task HTTP=$http_code curl_exit=$curl_exit 响应：$(tr '\n' ' ' <"$output")"
  [[ "$curl_exit" == "0" && "$http_code" =~ ^2 ]]
}

validate_files_payload() {
  local outbox_file="$1"
  python3 - "$outbox_file" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)
files = data.get("files") or []
errors = []
if not isinstance(files, list) or not files:
    errors.append("files[] 为空或不是数组")
else:
    for idx, item in enumerate(files):
        if not isinstance(item, dict):
            errors.append(f"files[{idx}] 不是对象")
            continue
        aliases = {
            "file_id/fileId": ("file_id", "fileId"),
            "filename/fileName": ("filename", "fileName"),
            "mime_type/mimeType": ("mime_type", "mimeType"),
            "download_url": ("download_url",),
        }
        for label, keys in aliases.items():
            if not any(str(item.get(key) or "").strip() for key in keys):
                errors.append(f"files[{idx}].{label} 为空")
        size = item.get("size")
        if isinstance(size, int):
            pass
        elif isinstance(size, str) and size.isdigit():
            pass
        else:
            errors.append(f"files[{idx}].size 不是整数或整数字符串：{size!r}")
if errors:
    print("; ".join(errors))
    sys.exit(1)
print("OK")
PY
}

compare_task_query_with_callback() {
  local task_query_file="$1"
  local outbox_file="$2"
  python3 - "$task_query_file" "$outbox_file" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    task = json.load(fh)
with open(sys.argv[2], "r", encoding="utf-8") as fh:
    outbox = json.load(fh)
callback = outbox.get("payload") or {}
errors = []

def get_value(value, *keys):
    if not isinstance(value, dict):
        return ""
    for key in keys:
        candidate = value.get(key)
        if candidate is not None and str(candidate).strip():
            return candidate
    return ""

def normalize_files(value):
    files = value.get("files") or []
    if not isinstance(files, list):
        errors.append("files 不是数组")
        return []
    normalized = []
    for item in files:
        if not isinstance(item, dict):
            errors.append("files 内存在非对象元素")
            continue
        normalized.append({
            "fileId": str(item.get("fileId") or item.get("file_id") or "").strip(),
            "fileName": str(item.get("fileName") or item.get("filename") or "").strip(),
            "size": str(item.get("size") or "").strip(),
            "mimeType": str(item.get("mimeType") or item.get("mime_type") or "").strip(),
            "download_url": str(item.get("download_url") or "").strip(),
        })
    return normalized

field_aliases = {
    "taskId": ("taskId", "task_id"),
    "status": ("status",),
    "stage": ("stage",),
    "replyText": ("replyText", "reply_text"),
    "errorCode": ("errorCode", "error_code"),
    "errorMessage": ("errorMessage", "error_message"),
    "errorReason": ("errorReason", "reason", "error_reason"),
}
for public_key, aliases in field_aliases.items():
    task_value = get_value(task, public_key)
    callback_value = get_value(callback, *aliases)
    if str(task_value or "") != str(callback_value or ""):
        errors.append(f"{public_key} 不一致：task={task_value!r} callback={callback_value!r}")
if normalize_files(task) != normalize_files(callback):
    errors.append(f"files[] 不一致：task={normalize_files(task)!r} callback={normalize_files(callback)!r}")
for forbidden in ("task_id", "reply_text", "error_code", "error_message", "error_reason", "created_at", "updated_at", "approval", "claim_token"):
    if forbidden in task:
        errors.append(f"fallback task 泄漏内部字段：{forbidden}")
if errors:
    print("; ".join(errors))
    sys.exit(1)
print("OK")
PY
}

compute_file_source() {
  local task_file="$1"
  local outbox_file="$2"
  local result_records_file="$3"
  python3 - "$task_file" "$outbox_file" "$result_records_file" <<'PY'
import json
import sys
from urllib.parse import parse_qs, urlparse

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    task = json.load(fh)
with open(sys.argv[2], "r", encoding="utf-8") as fh:
    outbox = json.load(fh)
try:
    with open(sys.argv[3], "r", encoding="utf-8") as fh:
        result_records = json.load(fh)
except Exception:
    result_records = None
raw_files = outbox.get("files") or []
files = raw_files if isinstance(raw_files, list) else []

def append_unique(target, value):
    value = str(value or "").strip()
    if value and value not in target:
        target.append(value)

def list_values(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        return [stripped]
    if value is None:
        return []
    return [str(value).strip()] if str(value).strip() else []

expected_file_ids = []
append_unique(expected_file_ids, task.get("result_workspace_file_id"))
for file_id in list_values(task.get("result_workspace_file_ids")):
    append_unique(expected_file_ids, file_id)

def value(item, *keys):
    for key in keys:
        candidate = str(item.get(key) or "").strip()
        if candidate:
            return candidate
    return ""

file_ids = []
callback_files_by_id = {}
has_any_file_marker = False
has_deprecated_hotfix = False
for item in files:
    if not isinstance(item, dict):
        has_any_file_marker = True
        continue
    file_id = value(item, "file_id", "fileId")
    filename = value(item, "filename", "fileName")
    mime_type = value(item, "mime_type", "mimeType")
    download_url = value(item, "download_url")
    size = str(item.get("size") or "").strip()
    if file_id:
        file_ids.append(file_id)
        callback_files_by_id[file_id] = item
    if file_id or filename or mime_type or download_url:
        has_any_file_marker = True
    if (
        file_id.startswith("hotfix-")
        or download_url == "/root/sh"
        or mime_type == ".sh"
        or (filename == "test.sh" and size == "4096")
    ):
        has_deprecated_hotfix = True

def parse_int(value):
    try:
        return int(value)
    except Exception:
        return None

def deleted(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes"}

def validate_db_records():
    if not isinstance(result_records, list):
        return ["workspace_files 结果记录不是数组或无法读取"]
    expected_user_id = str(task.get("user_id") or "").strip()
    expected_task_id = str(task.get("task_id") or task.get("taskId") or "").strip()
    records_by_id = {}
    errors = []
    for record in result_records:
        if not isinstance(record, dict):
            errors.append(f"workspace_files 内存在非对象记录：{record!r}")
            continue
        record_file_id = value(record, "file_id", "fileId")
        if not record_file_id:
            errors.append(f"workspace_files 记录缺少 file_id：{record!r}")
            continue
        if record_file_id in records_by_id:
            errors.append(f"workspace_files 存在重复 file_id：{record_file_id}")
        records_by_id[record_file_id] = record

    if set(records_by_id) != set(expected_file_ids):
        errors.append(
            "workspace_files file_id 集合不一致："
            f"expected={expected_file_ids!r} actual={sorted(records_by_id)!r}"
        )

    for file_id in expected_file_ids:
        callback_file = callback_files_by_id.get(file_id)
        record = records_by_id.get(file_id)
        if not isinstance(callback_file, dict) or not isinstance(record, dict):
            continue
        filename = value(callback_file, "filename", "fileName")
        record_filename = value(record, "filename")
        if filename and record_filename and filename != record_filename:
            errors.append(f"{file_id} filename 不一致：callback={filename!r} db={record_filename!r}")
        callback_size = parse_int(callback_file.get("size"))
        record_size = parse_int(record.get("size_bytes"))
        if callback_size is None:
            errors.append(f"{file_id} callback size 不是整数：{callback_file.get('size')!r}")
        elif record_size is not None and callback_size != record_size:
            errors.append(f"{file_id} size 不一致：callback={callback_size} db={record_size}")
        mime_type = value(callback_file, "mime_type", "mimeType")
        content_type = value(record, "content_type")
        if mime_type and content_type and mime_type != content_type:
            errors.append(f"{file_id} mimeType 不一致：callback={mime_type!r} db={content_type!r}")
        if value(record, "direction") != "outbound":
            errors.append(f"{file_id} direction 不是 outbound：{value(record, 'direction')!r}")
        if value(record, "file_role") != "result":
            errors.append(f"{file_id} file_role 不是 result：{value(record, 'file_role')!r}")
        source_type = value(record, "source_type")
        if source_type and source_type not in {"assistant_result", "assistant_output"}:
            errors.append(f"{file_id} source_type 不是结果来源：{source_type!r}")
        if expected_task_id and value(record, "source_ref") != expected_task_id:
            errors.append(
                f"{file_id} source_ref 不匹配：actual={value(record, 'source_ref')!r} expected={expected_task_id!r}"
            )
        if expected_user_id and value(record, "owner_user_id") != expected_user_id:
            errors.append(
                f"{file_id} owner_user_id 不匹配：actual={value(record, 'owner_user_id')!r} expected={expected_user_id!r}"
            )
        if deleted(record.get("deleted")):
            errors.append(f"{file_id} workspace_files.deleted 不是 0")
        checksum = value(record, "checksum_sha256", "checksum")
        if not checksum:
            errors.append(f"{file_id} workspace_files.checksum_sha256 为空，不能强验证下载内容")
        download_url = value(callback_file, "download_url")
        if file_id not in download_url:
            errors.append(f"{file_id} download_url 未包含 file_id：{download_url!r}")
        if expected_user_id:
            query_user_ids = parse_qs(urlparse(download_url).query).get("user_id") or []
            if expected_user_id not in query_user_ids:
                errors.append(f"{file_id} download_url 未携带正确 user_id：{download_url!r}")
    return errors

if has_deprecated_hotfix:
    print("DEPRECATED_HOTFIX_INJECTED")
elif expected_file_ids:
    if not files:
        print("REAL_RESULT_MISSING")
    elif (
        len(files) == len(expected_file_ids)
        and len(file_ids) == len(expected_file_ids)
        and set(file_ids) == set(expected_file_ids)
    ):
        metadata_errors = validate_db_records()
        if metadata_errors:
            for error in metadata_errors:
                print(error, file=sys.stderr)
            print("REAL_RESULT_METADATA_MISMATCH")
        else:
            print("REAL_RESULT")
    else:
        print("REAL_RESULT_MISMATCH")
elif has_any_file_marker:
    print("UNKNOWN_INJECTED")
else:
    print("NO_OUTPUT_FILE")
PY
}

verify_callback_downloads() {
  local outbox_file="$1"
  local result_records_file="$2"
  local list_file="$OUT_DIR/callback-downloads.tsv"
  python3 - "$outbox_file" "$result_records_file" >"$list_file" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)
try:
    with open(sys.argv[2], "r", encoding="utf-8") as fh:
        records = json.load(fh)
except Exception:
    records = []
checksums = {}
if isinstance(records, list):
    for record in records:
        if not isinstance(record, dict):
            continue
        file_id = str(record.get("file_id") or record.get("fileId") or "").strip()
        checksum = str(record.get("checksum_sha256") or record.get("checksum") or "").strip()
        if file_id:
            checksums[file_id] = checksum
for item in data.get("files") or []:
    if not isinstance(item, dict):
        continue
    file_id = str(item.get("file_id") or item.get("fileId") or "").strip()
    size = str(item.get("size") or "").strip()
    url = str(item.get("download_url") or "").strip()
    if file_id or url:
        print("\t".join([file_id, size, url, checksums.get(file_id, "")]))
PY
  chmod 600 "$list_file"
  local failed=0
  local idx=0
  local file_id size url checksum full_url output stderr_file result curl_exit http_code actual_size actual_sha256
  while IFS=$'\t' read -r file_id size url checksum; do
    [[ -n "${file_id:-}${url:-}" ]] || continue
    idx=$((idx + 1))
    if [[ -z "$url" ]]; then
      fail_case "callback 下载地址为空：file_id=${file_id:-<空>}"
      failed=$((failed + 1))
      continue
    fi
    full_url="$url"
    if [[ "$full_url" == /* ]]; then
      full_url="${BASE_URL}${full_url}"
    fi
    output="$OUT_DIR/callback-download-${idx}-${file_id:-unknown}.bin"
    stderr_file="$OUT_DIR/callback-download-${idx}-${file_id:-unknown}.stderr"
    result="$(curl_run "$output" "$stderr_file" "${curl_auth_headers[@]}" "$full_url")"
    curl_exit="$(printf '%s' "$result" | cut -f1)"
    http_code="$(printf '%s' "$result" | cut -f2)"
    chmod 600 "$output" "$stderr_file"
    if [[ "$curl_exit" != "0" || ! "$http_code" =~ ^2 ]]; then
      fail_case "callback 下载失败：file_id=${file_id:-<空>} HTTP=$http_code curl_exit=$curl_exit stderr=$(tr '\n' ' ' <"$stderr_file")"
      failed=$((failed + 1))
      continue
    fi
    actual_size="$(wc -c <"$output" | tr -d ' ')"
    if [[ "$size" =~ ^[0-9]+$ && "$actual_size" != "$size" ]]; then
      fail_case "callback 下载大小不一致：file_id=${file_id:-<空>} expected=$size actual=$actual_size"
      failed=$((failed + 1))
      continue
    fi
    if [[ -z "${checksum:-}" ]]; then
      fail_case "callback 下载缺少 DB checksum，不能强验证内容：file_id=${file_id:-<空>}"
      failed=$((failed + 1))
      continue
    fi
    actual_sha256="$(python3 - "$output" <<'PY'
import hashlib
import sys

with open(sys.argv[1], "rb") as fh:
    print(hashlib.sha256(fh.read()).hexdigest())
PY
)"
    if [[ "$actual_sha256" != "$checksum" ]]; then
      fail_case "callback 下载 checksum 不一致：file_id=${file_id:-<空>} expected=$checksum actual=$actual_sha256"
      failed=$((failed + 1))
      continue
    fi
    pass_case "callback 下载可用：file_id=${file_id:-<空>} size=$actual_size sha256=$actual_sha256"
  done <"$list_file"
  [[ "$idx" -gt 0 ]] || fail_case "callback files[] 中没有可下载项"
  [[ "$failed" -eq 0 && "$idx" -gt 0 ]]
}

poll_completed_outbox() {
  local task_id="$1"
  local outbox_file="$OUT_DIR/completed-outbox.json"
  local task_file="$OUT_DIR/gateway-task-final.json"
  local deadline
  deadline=$(( $(date +%s) + POLL_SECONDS ))
  while true; do
    task_detail_json "$task_id" >"$task_file" || die "查询 gateway_tasks 失败"
    chmod 600 "$task_file"
    if completed_outbox_json "$task_id" >"$outbox_file"; then
      chmod 600 "$outbox_file"
      if [[ -s "$outbox_file" ]]; then
        local status files_len delivered_at last_error
        status="$(json_get_file "$outbox_file" status)"
        files_len="$(json_get_file "$outbox_file" files_len)"
        delivered_at="$(json_get_file "$outbox_file" delivered_at)"
        last_error="$(json_get_file "$outbox_file" last_error)"
        log "outbox状态 status=${status:-<空>} files_len=${files_len:-0} delivered_at=${delivered_at:-<空>} last_error=${last_error:-<空>}"
        if [[ "$status" == "delivered" || "$status" == "dead" ]]; then
          return 0
        fi
      else
        local task_status task_stage
        task_status="$(json_get_file "$task_file" status)"
        task_stage="$(json_get_file "$task_file" stage)"
        log "尚未看到 task.completed outbox；任务状态 status=${task_status:-<空>} stage=${task_stage:-<空>}"
      fi
    else
      die "查询 completed outbox 失败，不能伪装成未生成。"
    fi
    if (( $(date +%s) >= deadline )); then
      return 1
    fi
    sleep "$POLL_INTERVAL"
  done
}

print_recent_logs() {
  local task_id="$1"
  local raw="$OUT_DIR/go-gateway-logs-raw-${task_id}.log"
  local filtered="$OUT_DIR/go-gateway-callback-${task_id}.log"
  section "采集 go-gateway 日志"
  if kubectl -n "$NS" logs "$GATEWAY_DEPLOY" --since=20m --all-containers >"$raw" 2>>"$KUBECTL_STDERR"; then
    chmod 600 "$raw"
    if [[ "$INCLUDE_GLOBAL_ERRORS" == "true" ]]; then
      grep -E "${task_id}|提交任务|文件上传|callback事件|dead-letter|结果文件元信息补齐失败|unmarshal|WARN|ERROR" "$raw" >"$filtered" || true
    else
      grep -F "$task_id" "$raw" >"$filtered" || true
    fi
    chmod 600 "$filtered"
    if [[ -s "$filtered" ]]; then
      sed 's/^/  /' "$filtered" | tail -n 80 | tee -a "$RUN_LOG" >&2
    else
      warn_case "未采集到匹配 task_id=$task_id 的 go-gateway 日志。"
    fi
  else
    warn_case "kubectl logs 执行失败，详见 $KUBECTL_STDERR"
  fi
}

confirm_upstream_evidence() {
  local task_id="$1"
  local outbox_file="$2"
  if [[ ! -s "$outbox_file" ]]; then
    UPSTREAM_RESULT="UPSTREAM_NOT_CONFIRMED"
    fail_case "缺少 callback outbox 证据，不能判定上游已收到 callback。"
    return
  fi
  if [[ -z "$UPSTREAM_EVIDENCE_FILE" ]]; then
    UPSTREAM_RESULT="NOT_VERIFIED"
    warn_case "未提供 --upstream-evidence-file；只能判 Gateway 侧通过，不能判上游侧通过。"
    return
  fi
  [[ -f "$UPSTREAM_EVIDENCE_FILE" ]] || die "上游证据文件不存在：$UPSTREAM_EVIDENCE_FILE"
  local evidence_result deadline
  deadline=$(( $(date +%s) + UPSTREAM_EVIDENCE_WAIT_SECONDS ))
  while true; do
    if evidence_result="$(python3 - "$task_id" "$outbox_file" "$UPSTREAM_EVIDENCE_FILE" <<'PY'
import json
import sys

task_id = sys.argv[1]
with open(sys.argv[2], "r", encoding="utf-8") as fh:
    outbox = json.load(fh)
with open(sys.argv[3], "r", encoding="utf-8", errors="replace") as fh:
    evidence_text = fh.read()

expected_urls = []
expected_file_ids = []
for item in outbox.get("files") or []:
    if not isinstance(item, dict):
        continue
    url = str(item.get("download_url") or "").strip()
    file_id = str(item.get("file_id") or item.get("fileId") or "").strip()
    if url and url not in expected_urls:
        expected_urls.append(url)
    if file_id and file_id not in expected_file_ids:
        expected_file_ids.append(file_id)

if not expected_urls:
    print("callback files[] 中没有 download_url")
    sys.exit(2)

def parse_json_string(value):
    if not isinstance(value, str):
        return []
    stripped = value.strip()
    if not stripped or stripped[0] not in "{[":
        return []
    try:
        return [json.loads(stripped)]
    except Exception:
        return []

def candidate_payloads(obj):
    if isinstance(obj, dict):
        if any(key in obj for key in ("taskId", "task_id", "files")):
            yield obj
        for key in ("body", "request_body", "payload", "request", "message"):
            value = obj.get(key)
            if isinstance(value, (dict, list)):
                yield from candidate_payloads(value)
            for parsed in parse_json_string(value):
                yield from candidate_payloads(parsed)
        for value in obj.values():
            if isinstance(value, list):
                yield from candidate_payloads(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from candidate_payloads(item)

def item_value(item, *keys):
    if not isinstance(item, dict):
        return ""
    for key in keys:
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""

def payload_matches(candidate):
    if not isinstance(candidate, dict):
        return False
    candidate_task_id = item_value(candidate, "taskId", "task_id")
    if candidate_task_id != task_id:
        return False
    files = candidate.get("files")
    if not isinstance(files, list) or len(files) != len(expected_urls):
        return False
    urls = []
    file_ids = []
    for item in files:
        if not isinstance(item, dict):
            return False
        url = item_value(item, "download_url")
        file_id = item_value(item, "fileId", "file_id")
        if not url or not file_id:
            return False
        urls.append(url)
        file_ids.append(file_id)
    return set(urls) == set(expected_urls) and set(file_ids) == set(expected_file_ids)

def scanned_json_candidates(text):
    decoder = json.JSONDecoder()
    index = 0
    seen = set()
    while index < len(text):
        next_positions = [pos for pos in (text.find("{", index), text.find("[", index)) if pos != -1]
        if not next_positions:
            break
        start = min(next_positions)
        try:
            obj, end = decoder.raw_decode(text[start:])
        except Exception:
            index = start + 1
            continue
        key = (start, start + end)
        if key not in seen:
            seen.add(key)
            yield obj
        index = start + max(end, 1)

for obj in scanned_json_candidates(evidence_text):
    for candidate in candidate_payloads(obj):
        if payload_matches(candidate):
            print("匹配到同一 JSON callback body，taskId 和 files[] 与 outbox 完全一致")
            sys.exit(0)

print(
    "未找到同一 callback body 同时匹配 taskId 和完整 files[]；"
    f"expected_task_id={task_id} expected_file_ids={expected_file_ids!r} expected_urls={expected_urls!r}"
)
sys.exit(1)
PY
    )"; then
      UPSTREAM_RESULT="UPSTREAM_CONFIRMED"
      pass_case "上游证据通过：$evidence_result"
      return
    fi
    if (( $(date +%s) >= deadline )); then
      UPSTREAM_RESULT="UPSTREAM_NOT_CONFIRMED"
      fail_case "上游证据未通过：$evidence_result"
      return
    fi
    log "尚未匹配到上游 callback body，继续等待证据文件：$UPSTREAM_EVIDENCE_FILE"
    sleep 1
  done
}

run_read_only_inspect() {
  inspect_context
  load_gateway_env
  inspect_triggers
  finalize
  if [[ "$FAIL_COUNT" -gt 0 ]]; then
    log "只读预检结果：失败，失败项=$FAIL_COUNT，警告项=$WARN_COUNT"
    exit 1
  fi
  log "只读预检结果：完成，失败项=0，警告项=$WARN_COUNT，跳过项=$SKIP_COUNT"
}

run_live_e2e() {
  guard_live_mode
  inspect_context
  load_gateway_env
  guard_callback_mode
  inspect_triggers
  load_api_key_if_allowed
  build_curl_auth_headers
  start_port_forward_if_needed

  section "Gateway 健康检查"
  local health_resp="$OUT_DIR/health.json"
  local health_stderr="$OUT_DIR/health.stderr"
  local result curl_exit health_code
  result="$(curl_run "$health_resp" "$health_stderr" "${curl_auth_headers[@]}" "${BASE_URL}/api/v1/health")"
  curl_exit="$(printf '%s' "$result" | cut -f1)"
  health_code="$(printf '%s' "$result" | cut -f2)"
  log "health HTTP=$health_code curl_exit=$curl_exit 响应：$(tr '\n' ' ' <"$health_resp")"
  [[ "$curl_exit" == "0" && "$health_code" =~ ^2 ]] || die "Gateway health 非 2xx 或 curl 失败，停止 live-e2e。"
  pass_case "Gateway health 可访问"

  section "准备并上传测试文件"
  prepare_upload_file
  upload_file
  assert_workspace_file

  if [[ "$RUN_NEGATIVE" == "true" ]]; then
    run_negative_tests
  else
    skip_case "失败注入默认关闭；如需执行请显式传 --run-negative。"
  fi

  section "提交正常任务：携带合法 files[]"
  local submit_result curl_code submit_code submit_resp
  submit_result="$(submit_chat "normal-file" "$USER_ID" "$UPLOADED_FILE_ID")"
  curl_code="$(printf '%s' "$submit_result" | cut -f1)"
  submit_code="$(printf '%s' "$submit_result" | cut -f2)"
  submit_resp="$(printf '%s' "$submit_result" | cut -f3)"
  TASK_ID="$(extract_task_id "$submit_resp")"
  if [[ "$curl_code" == "0" && "$submit_code" =~ ^2 && -n "$TASK_ID" ]]; then
    pass_case "任务提交成功，task_id=$TASK_ID"
  else
    die "正常任务提交失败或未返回 task_id"
  fi

  section "等待 task.completed callback outbox"
  if ! poll_completed_outbox "$TASK_ID"; then
    fail_case "超时后仍未生成已投递/死亡的 task.completed outbox"
  fi

  local task_file="$OUT_DIR/gateway-task-final.json"
  local outbox_file="$OUT_DIR/completed-outbox.json"
  local task_query_file="$OUT_DIR/gateway-task-public-final.json"
  local task_query_stderr="$OUT_DIR/gateway-task-public-final.stderr"
  print_recent_logs "$TASK_ID"

  section "判定 Gateway callback 和文件字段"
  if [[ ! -s "$outbox_file" ]]; then
    fail_case "缺少 completed outbox 证据文件"
  else
    local status last_error delivered_at payload_status payload_stage reply_text files_result result_records_file file_source_detail
    status="$(json_get_file "$outbox_file" status)"
    last_error="$(json_get_file "$outbox_file" last_error)"
    delivered_at="$(json_get_file "$outbox_file" delivered_at)"
    payload_status="$(json_get_file "$outbox_file" payload_status)"
    payload_stage="$(json_get_file "$outbox_file" payload_stage)"
    reply_text="$(json_get_file "$outbox_file" reply_text)"
    if [[ "$status" == "delivered" && -z "$last_error" && -n "$delivered_at" ]]; then
      GATEWAY_RESULT="GATEWAY_CALLBACK_OK"
      pass_case "Gateway 侧 callback delivered，last_error 为空，delivered_at 有值"
    else
      GATEWAY_RESULT="GATEWAY_CALLBACK_FAILED"
      fail_case "Gateway 侧 callback 状态不满足：status=$status last_error=${last_error:-<空>} delivered_at=${delivered_at:-<空>}"
    fi
    [[ "$payload_status" == "completed" ]] || fail_case "payload status 不是 completed：${payload_status:-<空>}"
    [[ "$payload_stage" == "done" ]] || warn_case "payload stage 不是 done：${payload_stage:-<空>}"
    [[ -n "$reply_text" ]] || warn_case "reply_text 为空；若本次只验 callback 文件字段，可接受但不能判文本结果完整。"
    if files_result="$(validate_files_payload "$outbox_file")"; then
      pass_case "callback files[] 字段完整，size 为整数或整数字符串"
    else
      fail_case "callback files[] 字段校验失败：$files_result"
    fi
    result_records_file="$OUT_DIR/result-workspace-files.json"
    if result_workspace_files_json "$outbox_file" >"$result_records_file"; then
      chmod 600 "$result_records_file"
      log "结果 workspace_files 证据：$(cat "$result_records_file")"
    else
      fail_case "查询结果 workspace_files 失败，不能验证真实结果文件元数据。"
      printf '[]\n' >"$result_records_file"
      chmod 600 "$result_records_file"
    fi
    file_source_detail="$OUT_DIR/file-source-validation.stderr"
    FILE_SOURCE_RESULT="$(compute_file_source "$task_file" "$outbox_file" "$result_records_file" 2>"$file_source_detail")"
    chmod 600 "$file_source_detail"
    if [[ -s "$file_source_detail" ]]; then
      log "真实结果文件校验详情：$(tr '\n' '; ' <"$file_source_detail")"
    fi
    case "$FILE_SOURCE_RESULT" in
      REAL_RESULT)
        FILE_SOURCE_RESULT="REAL_RESULT_FILE_OK"
        pass_case "真实结果文件链路成立：callback file_id 集合、workspace_files 元数据与任务归属一致"
        ;;
      REAL_RESULT_METADATA_MISMATCH)
        fail_case "真实结果文件元数据校验失败：file_id 集合匹配，但 workspace_files/callback 元数据不一致"
        ;;
      DEPRECATED_HOTFIX_INJECTED)
        FILE_SOURCE_RESULT="DEPRECATED_HOTFIX_INJECTED"
        fail_case "callback files[] 来自已废弃 hotfix 注入，不是真实 result_workspace_file_id"
        ;;
      REAL_RESULT_MISMATCH)
        fail_case "真实结果文件集合与 callback file_id 集合不一致"
        ;;
      REAL_RESULT_MISSING)
        fail_case "真实 result_workspace_file_id 存在，但 callback 未返回可验证文件元信息"
        ;;
      UNKNOWN_INJECTED)
        fail_case "callback 有 files[]，但来源不是 result_workspace_file_id"
        ;;
      NO_OUTPUT_FILE)
        fail_case "没有真实结果文件，callback 按当前契约不会注入假 files[]"
        ;;
    esac
    if [[ "$REQUIRE_REAL_RESULT" == "true" && "$FILE_SOURCE_RESULT" != "REAL_RESULT_FILE_OK" ]]; then
      fail_case "--require-real-result 已开启，但当前不是 REAL_RESULT_FILE_OK"
    fi
    if [[ "$FILE_SOURCE_RESULT" == "REAL_RESULT_FILE_OK" ]]; then
      verify_callback_downloads "$outbox_file" "$result_records_file" || fail_case "callback download_url 下载校验失败"
    fi
    if fetch_public_task_payload "$TASK_ID" "$task_query_file" "$task_query_stderr"; then
      chmod 600 "$task_query_file" "$task_query_stderr"
      local compare_result
      if compare_result="$(compare_task_query_with_callback "$task_query_file" "$outbox_file")"; then
        pass_case "fallback query 与 callback 公共 payload 完全一致"
      else
        fail_case "fallback query 与 callback 公共 payload 不一致：$compare_result"
      fi
    else
      fail_case "查询 fallback task 失败，不能验证 GET /api/v1/tasks/{task_id} 与 callback 一致性"
    fi
    confirm_upstream_evidence "$TASK_ID" "$outbox_file"
  fi

  finalize
  if [[ "$FAIL_COUNT" -gt 0 ]]; then
    log "live-e2e 结果：失败，失败项=$FAIL_COUNT，警告项=$WARN_COUNT，跳过项=$SKIP_COUNT"
    exit 1
  fi
  log "live-e2e 结果：Gateway 侧完成，gateway_result=$GATEWAY_RESULT file_source_result=$FILE_SOURCE_RESULT upstream_result=$UPSTREAM_RESULT"
}

main() {
  require_cmd kubectl
  require_cmd curl
  require_cmd python3

  section "运行参数"
  log "mode=$([[ "$LIVE_E2E" == "true" ]] && echo live-e2e || echo read-only-inspect)"
  log "namespace=${NS:-<空>}"
  log "user_id=${USER_ID:-<空>}"
  log "out_dir=$OUT_DIR"
  log "run_negative=$RUN_NEGATIVE"
  log "require_real_result=$REQUIRE_REAL_RESULT"
  log "upstream_evidence_wait_seconds=$UPSTREAM_EVIDENCE_WAIT_SECONDS"
  log "allow_prod_callback=$ALLOW_PROD_CALLBACK"
  log "callback_delivery_mode=$CALLBACK_DELIVERY_MODE"
  log "allow_read_gateway_key=$ALLOW_READ_GATEWAY_KEY"
  log "allow_mysql_root=$ALLOW_MYSQL_ROOT"

  if [[ "$LIVE_E2E" == "true" ]]; then
    run_live_e2e
  else
    run_read_only_inspect
  fi
}

main "$@"
