#!/bin/sh
set -eu

# Create two intranet test assistants through IronAgent admin API.
# This script intentionally uses X-IronAgent-Internal-Token only.

NS="${NS:-ironagent-kf}"
ADMIN_BASE_URL="${ADMIN_BASE_URL:-http://21.44.192.81:30808}"
MODEL_NAME="${MODEL_NAME:-minimax-m2.7}"
PROVIDER_ID="${PROVIDER_ID:-minimax}"
PROVIDER_BASE_URL="${PROVIDER_BASE_URL:-http://21.65.80.131:8801/v1}"
PROVIDER_API_KEY="${PROVIDER_API_KEY:-000}"
CAPABILITY_PROFILE="${CAPABILITY_PROFILE:-dangerous}"
START_RUNTIME="${START_RUNTIME:-true}"

ASSISTANT_ID_1="${ASSISTANT_ID_1:-test-assistant-1001}"
ASSISTANT_NAME_1="${ASSISTANT_NAME_1:-测试助理1001}"
USER_ID_1="${USER_ID_1:-12064912}"

ASSISTANT_ID_2="${ASSISTANT_ID_2:-test-assistant-1002}"
ASSISTANT_NAME_2="${ASSISTANT_NAME_2:-测试助理1002}"
USER_ID_2="${USER_ID_2:-12059433}"

RUN_TS="$(date +%Y%m%d%H%M%S)"
LOG_DIR="${LOG_DIR:-./create-intranet-test-assistants-${RUN_TS}}"
LOG_FILE="${LOG_DIR}/run.log"
mkdir -p "$LOG_DIR"
chmod 700 "$LOG_DIR"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"
if [ "${IRON_AGENT_CREATE_ASSISTANTS_LOG_WRAPPED:-}" != "1" ]; then
  export IRON_AGENT_CREATE_ASSISTANTS_LOG_WRAPPED=1
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

write_user_body() {
  out_file="$1"
  user_id="$2"
  display_name="$3"

  cat >"$out_file" <<JSON
{
  "user_id": "${user_id}",
  "display_name": "${display_name}",
  "department": "内网测试",
  "employee_id": "${user_id}",
  "role": "user"
}
JSON
}

write_assistant_body() {
  out_file="$1"
  assistant_id="$2"
  assistant_name="$3"
  user_id="$4"

  cat >"$out_file" <<JSON
{
  "assistant_id": "${assistant_id}",
  "name": "${assistant_name}",
  "capability_profile": "${CAPABILITY_PROFILE}",
  "user_id": "${user_id}",
  "notes": "内网批量创建脚本 ${RUN_TS}",
  "model_id": "",
  "primary_model": "${MODEL_NAME}",
  "provider_id": "${PROVIDER_ID}",
  "provider_base_url": "${PROVIDER_BASE_URL}",
  "provider_api_key": "${PROVIDER_API_KEY}",
  "workspace_slug": "${assistant_id}"
}
JSON
}

ensure_user() {
  user_id="$1"
  display_name="$2"
  body="${LOG_DIR}/user-${user_id}.json"

  write_user_body "$body" "$user_id" "$display_name"
  if api_json POST /api/v1/admin/users "$body" "user-${user_id}-create"; then
    log "用户 ${user_id} 创建完成"
    return 0
  fi

  if grep -q 'already exists\|exists\|已存在' "${LOG_DIR}/user-${user_id}-create.response.json"; then
    log "用户 ${user_id} 已存在，执行更新以保证显示名一致"
    api_json PUT "/api/v1/admin/users/${user_id}" "$body" "user-${user_id}-update" \
      || die "用户 ${user_id} 更新失败"
    return 0
  fi

  die "用户 ${user_id} 创建失败"
}

create_or_bind_assistant() {
  assistant_id="$1"
  assistant_name="$2"
  user_id="$3"
  body="${LOG_DIR}/assistant-${assistant_id}.json"
  bind_body="${LOG_DIR}/assistant-${assistant_id}-bind.json"

  write_assistant_body "$body" "$assistant_id" "$assistant_name" "$user_id"
  if api_json POST /api/v1/admin/assistants "$body" "assistant-${assistant_id}-create"; then
    log "助手 ${assistant_id} / ${assistant_name} 创建并绑定完成"
  else
    if grep -q 'assistant id already exists' "${LOG_DIR}/assistant-${assistant_id}-create.response.json"; then
      warn "助手 ${assistant_id} 已存在，不覆盖模型配置，只补充绑定关系"
      cat >"$bind_body" <<JSON
{
  "assistant_id": "${assistant_id}",
  "alias": "${assistant_name}",
  "source_system": "intranet-test-assistant-script"
}
JSON
      api_json POST "/api/v1/admin/users/${user_id}/assistants/bind" "$bind_body" "assistant-${assistant_id}-bind" \
        || warn "助手 ${assistant_id} 绑定可能已存在，继续后续步骤"
    else
      die "助手 ${assistant_id} 创建失败。若是名称重复，请换 assistant_id/name 或先在后台删除旧助手。"
    fi

  if [ "$START_RUNTIME" = "true" ]; then
    api_json POST "/api/v1/admin/assistants/${assistant_id}/repair" "" "assistant-${assistant_id}-repair" \
      || warn "助手 ${assistant_id} repair 失败，继续尝试 restart"
    api_json POST "/api/v1/admin/assistants/${assistant_id}/restart" "" "assistant-${assistant_id}-restart" \
      || die "助手 ${assistant_id} restart 失败"
    log "助手 ${assistant_id} 已触发运行态重启"
  else
    log "START_RUNTIME=false，跳过运行态启动/重启"
  fi
}

log "开始创建内网测试助理"
log "Admin API: ${ADMIN_BASE_URL}"
log "Namespace: ${NS}"
log "模型：provider=${PROVIDER_ID}, model=${MODEL_NAME}, base_url=${PROVIDER_BASE_URL}, api_key=***"
log "能力白名单：${CAPABILITY_PROFILE}"
log "运行态启动：${START_RUNTIME}"

ensure_user "$USER_ID_1" "$ASSISTANT_NAME_1"
create_or_bind_assistant "$ASSISTANT_ID_1" "$ASSISTANT_NAME_1" "$USER_ID_1"

ensure_user "$USER_ID_2" "$ASSISTANT_NAME_2"
create_or_bind_assistant "$ASSISTANT_ID_2" "$ASSISTANT_NAME_2" "$USER_ID_2"

log "全部完成。日志目录：${LOG_DIR}"
log "已创建/绑定：${USER_ID_1} -> ${ASSISTANT_ID_1} (${ASSISTANT_NAME_1})"
log "已创建/绑定：${USER_ID_2} -> ${ASSISTANT_ID_2} (${ASSISTANT_NAME_2})"
