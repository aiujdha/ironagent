#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NS="${NS:-ironagent}"
BASE_URL="${BASE_URL:-http://192.0.2.10:30808}"
USER_ID="${USER_ID:-}"
API_KEY="${API_KEY:-}"
OUT_DIR="${OUT_DIR:-/tmp/ironagent-callback-selftest-$(date +%Y%m%d%H%M%S)}"
POLL_SECONDS="${POLL_SECONDS:-240}"
POLL_INTERVAL="${POLL_INTERVAL:-5}"
CONNECT_TIMEOUT="${CONNECT_TIMEOUT:-5}"
MAX_TIME="${MAX_TIME:-60}"
SINCE="${SINCE:-30m}"
KUBECTL="${KUBECTL:-kubectl}"
ADMIN_TARGET="${ADMIN_TARGET:-deploy/admin}"
GATEWAY_DEPLOY="${GATEWAY_DEPLOY:-deploy/go-gateway}"
GATEWAY_SECRET="${GATEWAY_SECRET:-go-gateway-secret}"
DB_URL_KEY="${DB_URL_KEY:-GO_GATEWAY_DATABASE_URL}"
PYTHON_BIN="${PYTHON_BIN:-/app/.venv/bin/python}"
SHOW_PACKETS_SCRIPT="${SHOW_PACKETS_SCRIPT:-${SCRIPT_DIR}/show_callback_packets_cn.sh}"
MESSAGE="${MESSAGE:-}"

RUN_ID=""
UPLOAD_FILE=""
UPLOAD_FILE_ID=""
TASK_ID=""
FINAL_STATUS=""
FINAL_STAGE=""
FINAL_ERROR=""
FINAL_FILES_LEN="0"
RESULT_FILE_ID=""
RESULT_DOWNLOAD_STATUS=""
PACKET_DIR=""
PACKET_REPORT=""
PACKET_STATUS="not_run"
RUN_LOG=""

usage() {
  cat <<'EOF'
用法：
  bash scripts/k8s/self_test_callback_on_server_cn.sh --user-id USER_ID [选项]

用途：
  在服务器上自己跑一次真实 callback 自测：
    1. GET /healthz、/readyz
    2. POST /api/v1/files/upload 上传测试输入
    3. POST /api/v1/chat 提交短任务，要求 runtime 生成结果文件
    4. GET /api/v1/tasks/{task_id} 轮询到终态
    5. 下载 task files[] 中的结果文件
    6. 调用 show_callback_packets_cn.sh 检查 outbox、实际 callback 请求包、上游响应包

重要约束：
  当前公网 /api/v1/chat 不读取请求体 callback_url；callback 地址来自
  GO_GATEWAY_DEFAULT_CALLBACK_URL。这个脚本不会 patch 配置，只验证当前线上默认 callback。

常用选项：
  --user-id USER_ID          必填。必须是已绑定可用助手的测试用户。
  --base-url URL            Gateway 地址，默认 http://192.0.2.10:30808。
  --namespace NS            K8s namespace，默认 ironagent。
  --out-dir DIR             证据目录，默认 /tmp/ironagent-callback-selftest-*。
  --poll-seconds N          等待任务终态秒数，默认 240。
  --poll-interval N         轮询间隔秒数，默认 5。
  --message TEXT            覆盖默认测试 prompt。
  --api-key KEY             可选，发送 X-API-Key；默认走 public northbound。
  --gateway-deploy TARGET   默认 deploy/go-gateway。
  --admin-target TARGET     默认 deploy/admin。
  --gateway-secret NAME     默认 go-gateway-secret。
  --python-bin PATH         admin 容器内 Python，默认 /app/.venv/bin/python。
  --show-packets-script F   默认 scripts/k8s/show_callback_packets_cn.sh。
  --since DURATION          抓 go-gateway 日志时间窗口，默认 30m。
  -h, --help                显示帮助。

服务器示例：
  KUBECTL='sudo KUBECONFIG=/etc/kubernetes/admin.conf kubectl' \
  bash scripts/k8s/self_test_callback_on_server_cn.sh \
    --namespace ironagent \
    --user-id callback-format-user-20260530104753

输出：
  REPORT.md                         总结报告
  summary.env                       机器可读摘要
  callback-packets/callback-packets.txt  callback 请求包/响应包主报告
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

log() {
  local line
  line="[$(date '+%F %T')] $*"
  echo "$line" >&2
  if [[ -n "${RUN_LOG:-}" ]]; then
    echo "$line" >>"$RUN_LOG"
  fi
}

k() {
  ${KUBECTL} "$@"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "缺少命令：$1"
}

json_get_file() {
  local file="$1"
  shift
  python3 - "$file" "$@" <<'PY'
import json
import sys

path = sys.argv[1]
keys = sys.argv[2:]
try:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    print("")
    raise SystemExit(0)

def get_from(obj, names):
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
    if isinstance(obj, dict) and isinstance(obj.get("data"), dict):
        for name in names:
            if name in obj["data"]:
                return obj["data"][name]
    return None

value = get_from(data, keys)
if isinstance(value, (dict, list)):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
elif value is None:
    print("")
else:
    print(value)
PY
}

json_task_file_count() {
  local file="$1"
  python3 - "$file" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    print("0")
    raise SystemExit(0)
if isinstance(data, dict) and isinstance(data.get("data"), dict):
    data = data["data"]
files = data.get("files") if isinstance(data, dict) else None
print(len(files) if isinstance(files, list) else 0)
PY
}

json_first_result_file() {
  local file="$1"
  python3 - "$file" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    raise SystemExit(0)
if isinstance(data, dict) and isinstance(data.get("data"), dict):
    data = data["data"]
files = data.get("files") if isinstance(data, dict) else None
if not isinstance(files, list) or not files:
    raise SystemExit(0)
first = files[0] if isinstance(files[0], dict) else {}
print(first.get("fileId") or first.get("file_id") or first.get("id") or "")
print(first.get("download_url") or first.get("downloadUrl") or "")
PY
}

pretty_json_file() {
  local src="$1"
  local dst="$2"
  python3 - "$src" "$dst" <<'PY' || cp "$src" "$dst"
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
PY
}

curl_common_headers() {
  if [[ -n "$API_KEY" ]]; then
    printf '%s\0%s\0' "-H" "X-API-Key: ${API_KEY}"
  fi
}

curl_get() {
  local url="$1"
  local prefix="$2"
  local status curl_exit
  curl_exit=0
  mapfile -d '' -t auth_headers < <(curl_common_headers)
  status="$(
    curl -sS -o "${prefix}.body" -D "${prefix}.headers" -w "%{http_code}" \
      --connect-timeout "$CONNECT_TIMEOUT" --max-time "$MAX_TIME" \
      "${auth_headers[@]}" \
      "$url"
  )" || curl_exit=$?
  printf '%s\n' "$status" >"${prefix}.status"
  printf '%s\n' "$curl_exit" >"${prefix}.curl_exit"
  pretty_json_file "${prefix}.body" "${prefix}.pretty.json" >/dev/null 2>&1 || true
  [[ "$curl_exit" == "0" ]]
}

curl_json_post() {
  local url="$1"
  local body_file="$2"
  local prefix="$3"
  local status curl_exit
  curl_exit=0
  mapfile -d '' -t auth_headers < <(curl_common_headers)
  status="$(
    curl -sS -o "${prefix}.body" -D "${prefix}.headers" -w "%{http_code}" \
      --connect-timeout "$CONNECT_TIMEOUT" --max-time "$MAX_TIME" \
      "${auth_headers[@]}" \
      -H "Content-Type: application/json" \
      --data-binary "@${body_file}" \
      "$url"
  )" || curl_exit=$?
  printf '%s\n' "$status" >"${prefix}.status"
  printf '%s\n' "$curl_exit" >"${prefix}.curl_exit"
  pretty_json_file "${prefix}.body" "${prefix}.pretty.json" >/dev/null 2>&1 || true
  [[ "$curl_exit" == "0" ]]
}

curl_upload() {
  local prefix="$1"
  local status curl_exit
  curl_exit=0
  mapfile -d '' -t auth_headers < <(curl_common_headers)
  status="$(
    curl -sS -o "${prefix}.body" -D "${prefix}.headers" -w "%{http_code}" \
      --connect-timeout "$CONNECT_TIMEOUT" --max-time "$MAX_TIME" \
      "${auth_headers[@]}" \
      -X POST "${BASE_URL}/api/v1/files/upload" \
      -F "user_id=${USER_ID}" \
      -F "file=@${UPLOAD_FILE};type=text/plain"
  )" || curl_exit=$?
  printf '%s\n' "$status" >"${prefix}.status"
  printf '%s\n' "$curl_exit" >"${prefix}.curl_exit"
  pretty_json_file "${prefix}.body" "${prefix}.pretty.json" >/dev/null 2>&1 || true
  [[ "$curl_exit" == "0" ]]
}

write_chat_request() {
  local out="$1"
  python3 - "$out" "$RUN_ID" "$USER_ID" "$UPLOAD_FILE_ID" "$MESSAGE" <<'PY'
import json
import sys

out, run_id, user_id, file_id, message = sys.argv[1:6]
payload = {
    "request_id": f"callback-selftest-{run_id}",
    "user_id": user_id,
    "conversation_id": f"callback-selftest-{run_id}",
    "message": message,
    "files": [file_id],
}
with open(out, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
PY
}

poll_task() {
  local deadline now poll_idx prefix status stage err files_len line
  deadline=$((SECONDS + POLL_SECONDS))
  poll_idx=0
  : >"${OUT_DIR}/polls.jsonl"

  while [[ "$SECONDS" -lt "$deadline" ]]; do
    sleep "$POLL_INTERVAL"
    poll_idx=$((poll_idx + 1))
    prefix="${OUT_DIR}/05-task-poll-$(printf '%02d' "$poll_idx")"
    curl_get "${BASE_URL}/api/v1/tasks/${TASK_ID}" "$prefix" >/dev/null || true
    status="$(json_get_file "${prefix}.body" status)"
    stage="$(json_get_file "${prefix}.body" stage)"
    err="$(json_get_file "${prefix}.body" errorCode error_code errorMessage error errorReason)"
    files_len="$(json_task_file_count "${prefix}.body")"
    now=$((poll_idx * POLL_INTERVAL))
    line="$(python3 - "$poll_idx" "$now" "$(cat "${prefix}.status")" "$status" "$stage" "$files_len" "$err" <<'PY'
import json
import sys

poll, age_s, http_status, status, stage, files_len, error = sys.argv[1:8]
print(json.dumps({
    "poll": int(poll),
    "age_s": int(age_s),
    "http_status": http_status,
    "status": status,
    "stage": stage,
    "files_len": int(files_len or 0),
    "error": error,
}, ensure_ascii=False))
PY
)"
    echo "$line" >>"${OUT_DIR}/polls.jsonl"
    log "poll=${poll_idx} age=${now}s http=$(cat "${prefix}.status") status=${status:-<empty>} stage=${stage:-<empty>} files=${files_len} error=${err:-}"

    if [[ "$status" =~ ^(completed|failed|cancelled|canceled)$ || "$stage" =~ ^(done|error|failed)$ ]]; then
      cp "${prefix}.body" "${OUT_DIR}/06-final-task.body"
      cp "${prefix}.status" "${OUT_DIR}/06-final-task.status"
      cp "${prefix}.headers" "${OUT_DIR}/06-final-task.headers"
      [[ -f "${prefix}.pretty.json" ]] && cp "${prefix}.pretty.json" "${OUT_DIR}/06-final-task.pretty.json"
      FINAL_STATUS="$status"
      FINAL_STAGE="$stage"
      FINAL_ERROR="$err"
      FINAL_FILES_LEN="$files_len"
      return 0
    fi
  done

  log "任务在 ${POLL_SECONDS}s 内未进入终态，做最后一次查询。"
  curl_get "${BASE_URL}/api/v1/tasks/${TASK_ID}" "${OUT_DIR}/06-final-task" >/dev/null || true
  FINAL_STATUS="$(json_get_file "${OUT_DIR}/06-final-task.body" status)"
  FINAL_STAGE="$(json_get_file "${OUT_DIR}/06-final-task.body" stage)"
  FINAL_ERROR="$(json_get_file "${OUT_DIR}/06-final-task.body" errorCode error_code errorMessage error errorReason)"
  FINAL_FILES_LEN="$(json_task_file_count "${OUT_DIR}/06-final-task.body")"
}

download_result_file() {
  local result_info download_url
  if [[ ! -f "${OUT_DIR}/06-final-task.body" ]]; then
    return 0
  fi
  result_info="$(json_first_result_file "${OUT_DIR}/06-final-task.body" || true)"
  RESULT_FILE_ID="$(printf '%s\n' "$result_info" | sed -n '1p')"
  download_url="$(printf '%s\n' "$result_info" | sed -n '2p')"
  [[ -n "$download_url" ]] || return 0
  if [[ "$download_url" == /* ]]; then
    download_url="${BASE_URL}${download_url}"
  fi
  curl_get "$download_url" "${OUT_DIR}/07-result-file" >/dev/null || true
  RESULT_DOWNLOAD_STATUS="$(cat "${OUT_DIR}/07-result-file.status" 2>/dev/null || true)"
  log "结果文件下载：file_id=${RESULT_FILE_ID:-} http=${RESULT_DOWNLOAD_STATUS:-}"
}

run_packet_inspection() {
  PACKET_DIR="${OUT_DIR}/callback-packets"
  PACKET_REPORT="${PACKET_DIR}/callback-packets.txt"
  if [[ ! -f "$SHOW_PACKETS_SCRIPT" ]]; then
    PACKET_STATUS="skipped_missing_script"
    log "未找到 show_callback_packets_cn.sh，跳过 DB callback 抓包：$SHOW_PACKETS_SCRIPT"
    return 0
  fi
  log "检查 callback outbox/request/response：task_id=${TASK_ID}"
  if KUBECTL="$KUBECTL" bash "$SHOW_PACKETS_SCRIPT" \
      --namespace "$NS" \
      --task-id "$TASK_ID" \
      --admin-target "$ADMIN_TARGET" \
      --gateway-deploy "$GATEWAY_DEPLOY" \
      --gateway-secret "$GATEWAY_SECRET" \
      --python-bin "$PYTHON_BIN" \
      --out-dir "$PACKET_DIR" \
      --since "$SINCE"; then
    PACKET_STATUS="ok"
  else
    PACKET_STATUS="failed"
  fi
}

write_summary() {
  local callback_delivered terminal_event_seen completed_files_seen delivery_files_seen packet_conclusion
  callback_delivered="unknown"
  terminal_event_seen="unknown"
  completed_files_seen="unknown"
  delivery_files_seen="unknown"
  packet_conclusion=""
  if [[ -f "$PACKET_REPORT" ]]; then
    if grep -Eq 'status=delivered|response_status_code=2[0-9][0-9]' "$PACKET_REPORT"; then
      callback_delivered="yes"
    else
      callback_delivered="no"
    fi
    if grep -Eq 'event_type=task\.(completed|failed|cancelled|canceled)' "$PACKET_REPORT"; then
      terminal_event_seen="yes"
    else
      terminal_event_seen="no"
    fi
    completed_files_seen="$(grep -m1 '^completed_event_files_seen=' "$PACKET_REPORT" | cut -d= -f2- || true)"
    delivery_files_seen="$(grep -m1 '^delivery_request_files_seen=' "$PACKET_REPORT" | cut -d= -f2- || true)"
    packet_conclusion="$(grep -m1 '^结论=' "$PACKET_REPORT" | cut -d= -f2- || true)"
  fi

  {
    echo "RUN_ID=${RUN_ID}"
    echo "OUT_DIR=${OUT_DIR}"
    echo "BASE_URL=${BASE_URL}"
    echo "NAMESPACE=${NS}"
    echo "USER_ID=${USER_ID}"
    echo "UPLOAD_FILE_ID=${UPLOAD_FILE_ID}"
    echo "TASK_ID=${TASK_ID}"
    echo "FINAL_STATUS=${FINAL_STATUS}"
    echo "FINAL_STAGE=${FINAL_STAGE}"
    echo "FINAL_ERROR=${FINAL_ERROR}"
    echo "FINAL_FILES_LEN=${FINAL_FILES_LEN}"
    echo "RESULT_FILE_ID=${RESULT_FILE_ID}"
    echo "RESULT_DOWNLOAD_STATUS=${RESULT_DOWNLOAD_STATUS}"
    echo "PACKET_STATUS=${PACKET_STATUS}"
    echo "CALLBACK_DELIVERED=${callback_delivered}"
    echo "TERMINAL_EVENT_SEEN=${terminal_event_seen}"
    echo "COMPLETED_EVENT_FILES_SEEN=${completed_files_seen}"
    echo "DELIVERY_REQUEST_FILES_SEEN=${delivery_files_seen}"
  } >"${OUT_DIR}/summary.env"

  cat >"${OUT_DIR}/REPORT.md" <<EOF
# IronAgent Callback 服务器自测报告

测试时间：$(date '+%F %T')

## 结论

- /upload 返回 file_id：${UPLOAD_FILE_ID:-<空>}
- /chat 返回 task_id：${TASK_ID:-<空>}
- 任务终态：${FINAL_STATUS:-<空>}/${FINAL_STAGE:-<空>}
- 任务错误：${FINAL_ERROR:-<空>}
- task files[] 数量：${FINAL_FILES_LEN:-0}
- 结果文件下载 HTTP：${RESULT_DOWNLOAD_STATUS:-<未下载>}
- callback 抓包状态：${PACKET_STATUS}
- callback delivered/2xx：${callback_delivered}
- terminal callback event：${terminal_event_seen}
- completed payload files[]：${completed_files_seen}
- actual callback request files[]：${delivery_files_seen}

${packet_conclusion:+抓包结论：${packet_conclusion}}

## 关键 ID

- run_id: \`${RUN_ID}\`
- user_id: \`${USER_ID}\`
- upload_file_id: \`${UPLOAD_FILE_ID:-}\`
- task_id: \`${TASK_ID:-}\`
- result_file_id: \`${RESULT_FILE_ID:-}\`

## 证据文件

- 运行摘要：\`${OUT_DIR}/summary.env\`
- 任务轮询：\`${OUT_DIR}/polls.jsonl\`
- 最终任务响应：\`${OUT_DIR}/06-final-task.pretty.json\`
- callback 抓包报告：\`${PACKET_REPORT:-}\`
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user-id)
      USER_ID="${2:-}"
      shift 2
      ;;
    --base-url)
      BASE_URL="${2:-}"
      shift 2
      ;;
    --namespace|-n)
      NS="${2:-}"
      shift 2
      ;;
    --out-dir)
      OUT_DIR="${2:-}"
      shift 2
      ;;
    --poll-seconds)
      POLL_SECONDS="${2:-}"
      shift 2
      ;;
    --poll-interval)
      POLL_INTERVAL="${2:-}"
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
    --gateway-deploy)
      GATEWAY_DEPLOY="${2:-}"
      shift 2
      ;;
    --admin-target)
      ADMIN_TARGET="${2:-}"
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
    --show-packets-script)
      SHOW_PACKETS_SCRIPT="${2:-}"
      shift 2
      ;;
    --since)
      SINCE="${2:-}"
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

[[ -n "$USER_ID" ]] || {
  usage >&2
  die "必须传 --user-id。注意 user_id=111 当前可能无可用助手。"
}

BASE_URL="${BASE_URL%/}"
RUN_ID="$(date +%Y%m%d%H%M%S)-$$"
mkdir -p "$OUT_DIR"
chmod 700 "$OUT_DIR"
RUN_LOG="${OUT_DIR}/run.log"
: >"$RUN_LOG"
chmod 600 "$RUN_LOG"

need_cmd curl
need_cmd python3
need_cmd bash

if [[ -z "$MESSAGE" ]]; then
  MESSAGE="callback 自测 run_id=${RUN_ID}。请读取 files[] 上传文件，生成结果文件 callback_selftest_${RUN_ID}.txt，内容写 callback selftest ${RUN_ID}，并把该文件作为任务结果 files 返回。最后只回复一行简短摘要。"
fi

log "输出目录：$OUT_DIR"
log "Gateway：$BASE_URL"
log "Namespace：$NS"
log "User：$USER_ID"

log "预检 healthz/readyz"
curl_get "${BASE_URL}/healthz" "${OUT_DIR}/00-healthz" >/dev/null || true
curl_get "${BASE_URL}/readyz" "${OUT_DIR}/00-readyz" >/dev/null || true
log "healthz=$(cat "${OUT_DIR}/00-healthz.status") readyz=$(cat "${OUT_DIR}/00-readyz.status")"

if command -v "${KUBECTL%% *}" >/dev/null 2>&1; then
  {
    echo "namespace=${NS}"
    echo "gateway_deploy=${GATEWAY_DEPLOY}"
    echo "gateway_image=$(k -n "$NS" get "$GATEWAY_DEPLOY" -o jsonpath='{.spec.template.spec.containers[*].image}' 2>/dev/null || true)"
    echo
    k -n "$NS" exec "$GATEWAY_DEPLOY" -- sh -c \
      'env | grep -E "^(GO_GATEWAY_DEFAULT_CALLBACK_URL|GO_GATEWAY_CALLBACK_ALLOWED_HOSTS|GO_GATEWAY_CALLBACK_ALLOW_PRIVATE_HOSTS|GO_GATEWAY_CALLBACK_FIRE_AND_FORGET|GO_GATEWAY_CALLBACK_ACCEPT_ANY_RESPONSE)=" | sort' \
      2>/dev/null || true
  } >"${OUT_DIR}/00-gateway-callback-env.txt"
else
  log "kubectl 不在 PATH 中，稍后 callback 抓包可能失败；可设置 KUBECTL='sudo KUBECONFIG=/etc/kubernetes/admin.conf kubectl'"
fi

UPLOAD_FILE="${OUT_DIR}/upload-input-${RUN_ID}.txt"
{
  echo "run_id=${RUN_ID}"
  echo "user_id=${USER_ID}"
  echo "created_at=$(date '+%F %T')"
  echo "purpose=ironagent callback self test"
} >"$UPLOAD_FILE"
chmod 600 "$UPLOAD_FILE"

log "上传测试文件"
curl_upload "${OUT_DIR}/01-upload" >/dev/null || true
UPLOAD_FILE_ID="$(json_get_file "${OUT_DIR}/01-upload.body" file_id fileId id)"
log "upload_http=$(cat "${OUT_DIR}/01-upload.status") file_id=${UPLOAD_FILE_ID:-<empty>}"
[[ -n "$UPLOAD_FILE_ID" ]] || {
  write_summary
  die "上传没有返回 file_id，详见 ${OUT_DIR}/01-upload.body"
}

log "校验上传文件 metadata/download"
curl_get "${BASE_URL}/api/v1/files/${UPLOAD_FILE_ID}?user_id=${USER_ID}" "${OUT_DIR}/02-file-meta" >/dev/null || true
curl_get "${BASE_URL}/api/v1/files/${UPLOAD_FILE_ID}/download?user_id=${USER_ID}" "${OUT_DIR}/03-file-download" >/dev/null || true
log "file_meta=$(cat "${OUT_DIR}/02-file-meta.status") file_download=$(cat "${OUT_DIR}/03-file-download.status")"

write_chat_request "${OUT_DIR}/04-chat-request.json"
log "提交 /api/v1/chat"
curl_json_post "${BASE_URL}/api/v1/chat" "${OUT_DIR}/04-chat-request.json" "${OUT_DIR}/04-chat" >/dev/null || true
TASK_ID="$(json_get_file "${OUT_DIR}/04-chat.body" task_id taskId id)"
log "chat_http=$(cat "${OUT_DIR}/04-chat.status") task_id=${TASK_ID:-<empty>}"
[[ -n "$TASK_ID" ]] || {
  write_summary
  die "/chat 没有返回 task_id，详见 ${OUT_DIR}/04-chat.body"
}

poll_task
download_result_file
run_packet_inspection
write_summary

log "完成。报告：${OUT_DIR}/REPORT.md"
log "callback 抓包报告：${PACKET_REPORT:-<none>}"
