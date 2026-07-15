#!/bin/sh
set -eu

# Update the two intranet test assistants created for users 12064912 and 12059433.
# This intentionally uses X-IronAgent-Internal-Token only.

NS="${NS:-ironagent-kf}"
ADMIN_BASE_URL="${ADMIN_BASE_URL:-${ADMIN:-http://21.44.192.81:30808}}"

ASSISTANT_IDS="${ASSISTANT_IDS:-test-assistant-1001 test-assistant-1002}"
CAPABILITY_PROFILE="${CAPABILITY_PROFILE:-dangerous}"

MODEL_NAME="${MODEL_NAME:-minimax-m2.7}"
PROVIDER_ID="${PROVIDER_ID:-minimax}"
PROVIDER_BASE_URL="${PROVIDER_BASE_URL:-http://21.65.80.131:8801/v1}"
PROVIDER_API_KEY="${PROVIDER_API_KEY:-***}"

RUN_TS="$(date +%Y%m%d%H%M%S)"
LOG_DIR="${LOG_DIR:-./update-intranet-test-assistants-${RUN_TS}}"
LOG_FILE="${LOG_DIR}/run.log"
mkdir -p "$LOG_DIR"
chmod 700 "$LOG_DIR"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"

if [ "${IRON_AGENT_UPDATE_ASSISTANTS_LOG_WRAPPED:-}" != "1" ]; then
  export IRON_AGENT_UPDATE_ASSISTANTS_LOG_WRAPPED=1
  log_pipe="${LOG_DIR}/run.pipe"
  rm -f "$log_pipe"
  mkfifo "$log_pipe"
  tee -a "$LOG_FILE" <"$log_pipe" &
  tee_pid="$!"
  set +e
  sh "$0" "$@" >"$log_pipe" 2>&1
  script_status="$?"
  set -e
  wait "$tee_pid" || true
  rm -f "$log_pipe"
  exit "$script_status"
fi

log() {
  printf '[%s] [信息] %s\n' "$(date '+%F %T')" "$*"
}

warn() {
  printf '[%s] [警告] %s\n' "$(date '+%F %T')" "$*" >&2
}

die() {
  printf '[%s] [错误] %s\n' "$(date '+%F %T')" "$*" >&2
  exit 1
}

read_internal_token() {
  if [ -n "${IRON_AGENT_INTERNAL_SERVICE_TOKEN:-}" ]; then
    printf '%s' "$IRON_AGENT_INTERNAL_SERVICE_TOKEN"
    return 0
  fi

  if command -v kubectl >/dev/null 2>&1; then
    kubectl -n "$NS" get secret control-plane-shared-secret \
      -o jsonpath='{.data.IRON_AGENT_INTERNAL_SERVICE_TOKEN}' 2>/dev/null \
      | base64 -d 2>/dev/null || true
  fi
}

TOKEN="$(read_internal_token)"
[ -n "$TOKEN" ] || die "没有读取到 IRON_AGENT_INTERNAL_SERVICE_TOKEN。请在集群内执行，或先 export IRON_AGENT_INTERNAL_SERVICE_TOKEN=..."

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "缺少命令：$1"
}

need_cmd curl
need_cmd python3

api_json() {
  method="$1"
  path="$2"
  body_file="${3:-}"
  label="$4"
  out_file="${LOG_DIR}/${label}.response.json"

  log "请求 ${method} ${path}"
  if [ -n "$body_file" ]; then
    http_code="$(
      curl -sS -o "$out_file" -w '%{http_code}' \
        -X "$method" "${ADMIN_BASE_URL}${path}" \
        -H 'Accept: application/json' \
        -H 'Content-Type: application/json' \
        -H "X-IronAgent-Internal-Token: ${TOKEN}" \
        --data-binary "@${body_file}"
    )"
  else
    http_code="$(
      curl -sS -o "$out_file" -w '%{http_code}' \
        -X "$method" "${ADMIN_BASE_URL}${path}" \
        -H 'Accept: application/json' \
        -H "X-IronAgent-Internal-Token: ${TOKEN}"
    )"
  fi

  log "响应 ${method} ${path} -> HTTP ${http_code}，详情：${out_file}"
  if [ "$http_code" -lt 200 ] || [ "$http_code" -ge 300 ]; then
    warn "响应体："
    cat "$out_file" >&2 || true
    return 1
  fi
  return 0
}

write_update_body() {
  out_file="$1"
  cat >"$out_file" <<JSON
{
  "provision_runtime": true,
  "primary_model": "${MODEL_NAME}",
  "provider_id": "${PROVIDER_ID}",
  "provider_base_url": "${PROVIDER_BASE_URL}",
  "provider_api_key": "${PROVIDER_API_KEY}"
}
JSON
}

log "namespace=${NS}"
log "admin=${ADMIN_BASE_URL}"
log "assistant_ids=${ASSISTANT_IDS}"
log "capability_profile=${CAPABILITY_PROFILE}"
log "model=${PROVIDER_ID}/${MODEL_NAME}"
log "provider_base_url=${PROVIDER_BASE_URL}"
log "provider_api_key 字面值长度=$(printf '%s' "$PROVIDER_API_KEY" | wc -c | tr -d ' ')"
log "日志目录=${LOG_DIR}"

for assistant_id in $ASSISTANT_IDS; do
  body_file="${LOG_DIR}/${assistant_id}.update.json"
  write_update_body "$body_file"

  log "开始更新助手 ${assistant_id}"
  api_json "PUT" "/api/v1/admin/assistants/${assistant_id}/model-config" "$body_file" "${assistant_id}.update" \
    || die "更新助手 ${assistant_id} 失败"

  log "重启助手 ${assistant_id}"
  api_json "POST" "/api/v1/admin/assistants/${assistant_id}/restart" "" "${assistant_id}.restart" \
    || die "重启助手 ${assistant_id} 失败"
done

log "更新请求已提交。等待 Pod 重启后可检查运行态 ironagent.json。"

if command -v kubectl >/dev/null 2>&1; then
  for assistant_id in $ASSISTANT_IDS; do
    log "尝试查看 ${assistant_id} 的 Pod 状态"
    kubectl -n "$NS" get pods -o wide | grep "ironagent-assistant-${assistant_id}" || true
  done
fi

log "完成。完整日志：${LOG_FILE}"
