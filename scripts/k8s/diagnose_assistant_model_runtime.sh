#!/usr/bin/env bash
set -euo pipefail

# Diagnose whether assistant runtime uses the expected LLM endpoint/key.
# Set APPLY_FIX=1 to update DB source config and restart assistants.

NS="${NS:-ironagent-kf}"
ADMIN_BASE_URL="${ADMIN_BASE_URL:-${ADMIN:-http://21.44.192.81:30808}}"
ASSISTANT_IDS="${ASSISTANT_IDS:-test-assistant-1001 test-assistant-1002}"
EXPECTED_MODEL="${EXPECTED_MODEL:-minimax-m2.7}"
EXPECTED_PROVIDER_ID="${EXPECTED_PROVIDER_ID:-minimax}"
EXPECTED_BASE_URL="${EXPECTED_BASE_URL:-http://21.65.80.131:8012/v1}"
EXPECTED_API_KEY="${EXPECTED_API_KEY:-***}"
EXPECTED_AUTH_HEADER="${EXPECTED_AUTH_HEADER:-false}"
PROBE_AUTH_HEADER_MODE="${PROBE_AUTH_HEADER_MODE:-bearer}"
APPLY_FIX="${APPLY_FIX:-0}"
WAIT_AFTER_RESTART_SECONDS="${WAIT_AFTER_RESTART_SECONDS:-20}"
EXPECTED_BEARER_AUTHORIZATION="${EXPECTED_API_KEY}"
case "$(printf '%s' "$EXPECTED_BEARER_AUTHORIZATION" | tr '[:upper:]' '[:lower:]')" in
  bearer\ *) ;;
  *) EXPECTED_BEARER_AUTHORIZATION="Bearer ${EXPECTED_BEARER_AUTHORIZATION}" ;;
esac

RUN_TS="$(date +%Y%m%d%H%M%S)"
LOG_DIR="${LOG_DIR:-./assistant-model-runtime-${RUN_TS}}"
LOG_FILE="${LOG_DIR}/run.log"
mkdir -p "$LOG_DIR"
chmod 700 "$LOG_DIR"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"

if [ "${IRON_AGENT_MODEL_RUNTIME_LOG_WRAPPED:-}" != "1" ]; then
  export IRON_AGENT_MODEL_RUNTIME_LOG_WRAPPED=1
  log_pipe="${LOG_DIR}/run.pipe"
  rm -f "$log_pipe"
  mkfifo "$log_pipe"
  tee -a "$LOG_FILE" <"$log_pipe" &
  tee_pid="$!"
  set +e
  bash "$0" "$@" >"$log_pipe" 2>&1
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

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "缺少命令：$1"
}

need_cmd kubectl
need_cmd python3
need_cmd curl

read_internal_token() {
  if [ -n "${IRON_AGENT_INTERNAL_SERVICE_TOKEN:-}" ]; then
    printf '%s' "$IRON_AGENT_INTERNAL_SERVICE_TOKEN"
    return 0
  fi
  kubectl -n "$NS" get secret control-plane-shared-secret \
    -o jsonpath='{.data.IRON_AGENT_INTERNAL_SERVICE_TOKEN}' 2>/dev/null \
    | base64 -d 2>/dev/null || true
}

TOKEN="$(read_internal_token)"

assistant_sql_list() {
  local quoted=""
  local id
  for id in $ASSISTANT_IDS; do
    if [ -n "$quoted" ]; then
      quoted="${quoted},"
    fi
    quoted="${quoted}\"${id}\""
  done
  printf '%s' "$quoted"
}

run_mysql() {
  local sql="$1"
  kubectl -n "$NS" exec mysql-0 -- sh -lc "mysql -uroot -p\"\$MYSQL_ROOT_PASSWORD\" \"\$MYSQL_DATABASE\"" <<SQL
$sql
SQL
}

extract_iron_agent_summary() {
  python3 -c '
import json, sys
payload = json.load(sys.stdin)
primary = (((payload.get("agents") or {}).get("defaults") or {}).get("model") or {}).get("primary", "")
providers = ((payload.get("models") or {}).get("providers") or {})
for provider_id, config in providers.items():
    headers = config.get("headers") if isinstance(config.get("headers"), dict) else {}
    authorization = headers.get("Authorization")
    if isinstance(authorization, str) and authorization:
        auth_shape = "bearer" if authorization.lower().startswith("bearer ") else "raw"
        auth_len = len(authorization)
    elif authorization is None:
        auth_shape = "missing"
        auth_len = 0
    else:
        auth_shape = type(authorization).__name__
        auth_len = 0
    api_key = config.get("apiKey", "")
    if api_key == "***":
        api_key_shape = "masked"
    elif isinstance(api_key, str) and api_key:
        api_key_shape = "set"
    elif api_key is None:
        api_key_shape = "missing"
    else:
        api_key_shape = "empty"
    print("primary={}".format(primary))
    print("provider_id={}".format(provider_id))
    print("baseUrl={}".format(config.get("baseUrl", "")))
    print("apiKey_shape={}".format(api_key_shape))
    print("authHeader={}".format(config.get("authHeader", "")))
    print("authorization_header_shape={}".format(auth_shape))
    print("authorization_header_len={}".format(auth_len))
    if config.get("authHeader") is True and authorization:
        print("authHeader_warning=authHeader_true_overrides_headers_authorization")
    break
if not providers:
    print("primary={}".format(primary))
    print("provider_id=")
    print("baseUrl=")
    print("apiKey_shape=")
    print("authHeader=")
    print("authorization_header_shape=")
    print("authorization_header_len=0")
'
}

find_pod() {
  local assistant_id="$1"
  kubectl -n "$NS" get pods -o name \
    | grep "ironagent-assistant-${assistant_id}" \
    | sed 's#pod/##' \
    | head -1
}

probe_from_pod() {
  local assistant_id="$1"
  local pod="$2"
  local out_file="${LOG_DIR}/${assistant_id}.pod-model-probe.json"
  log "从助手容器内探测模型：assistant=${assistant_id}, pod=${pod}"
  kubectl -n "$NS" exec "$pod" -c ironagent-gateway -- sh -lc "
    set -- -H 'Content-Type: application/json'
    case '${PROBE_AUTH_HEADER_MODE}' in
      raw)
        set -- \"\$@\" -H 'Authorization: ${EXPECTED_API_KEY}'
        ;;
      bearer)
        set -- \"\$@\" -H 'Authorization: ${EXPECTED_BEARER_AUTHORIZATION}'
        ;;
      api-key)
        set -- \"\$@\" -H 'api-key: ${EXPECTED_API_KEY}'
        ;;
      raw-and-api-key)
        set -- \"\$@\" -H 'api-key: ${EXPECTED_API_KEY}' -H 'Authorization: ${EXPECTED_API_KEY}'
        ;;
      *)
        echo 'unsupported PROBE_AUTH_HEADER_MODE=${PROBE_AUTH_HEADER_MODE}' >&2
        exit 2
        ;;
    esac
    curl -sS --max-time 30 \
      -o /tmp/ironagent-model-probe.json \
      -w 'HTTP_STATUS=%{http_code}\\nTOTAL_TIME=%{time_total}\\n' \
      -X POST '${EXPECTED_BASE_URL%/}/chat/completions' \
      \"\$@\" \
      --data '{\"model\":\"${EXPECTED_MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"只回复 OK\"}],\"max_tokens\":16,\"temperature\":0}'
    cat /tmp/ironagent-model-probe.json
  " >"$out_file" 2>&1 || true
  sed -n '1,80p' "$out_file"
  log "容器内模型探测日志：${out_file}"
}

log "namespace=${NS}"
log "admin=${ADMIN_BASE_URL}"
log "assistant_ids=${ASSISTANT_IDS}"
log "expected=${EXPECTED_PROVIDER_ID}/${EXPECTED_MODEL} ${EXPECTED_BASE_URL} apiKey 字面值长度=$(printf '%s' "$EXPECTED_API_KEY" | wc -c | tr -d ' ')"
log "expected_authHeader=${EXPECTED_AUTH_HEADER}"
log "probe_auth_header_mode=${PROBE_AUTH_HEADER_MODE}"
log "apply_fix=${APPLY_FIX}"
log "日志目录=${LOG_DIR}"

ids="$(assistant_sql_list)"

log "检查数据库源配置"
run_mysql "
SELECT assistant_id,
       capability_profile,
       JSON_UNQUOTE(JSON_EXTRACT(iron_agent_config, '\$.primary_model')) AS primary_model,
       JSON_UNQUOTE(JSON_EXTRACT(iron_agent_config, '\$.provider_id')) AS provider_id,
       JSON_UNQUOTE(JSON_EXTRACT(iron_agent_config, '\$.provider_base_url')) AS base_url,
       JSON_UNQUOTE(JSON_EXTRACT(iron_agent_config, '\$.provider_api_key')) AS api_key
FROM assistants
WHERE assistant_id IN (${ids});
" | tee "${LOG_DIR}/db-before.txt"

if [ "$APPLY_FIX" = "1" ]; then
  log "开始修正数据库源配置"
  run_mysql "
UPDATE assistants
SET iron_agent_config = JSON_SET(
      iron_agent_config,
      '\$.primary_model', '${EXPECTED_MODEL}',
      '\$.auth_mode', 'provider_api_key',
      '\$.provider_id', '${EXPECTED_PROVIDER_ID}',
      '\$.provider_base_url', '${EXPECTED_BASE_URL}',
      '\$.provider_api_key', '${EXPECTED_API_KEY}'
    ),
    updated_at = NOW()
WHERE assistant_id IN (${ids});

SELECT assistant_id,
       capability_profile,
       JSON_UNQUOTE(JSON_EXTRACT(iron_agent_config, '\$.primary_model')) AS primary_model,
       JSON_UNQUOTE(JSON_EXTRACT(iron_agent_config, '\$.provider_id')) AS provider_id,
       JSON_UNQUOTE(JSON_EXTRACT(iron_agent_config, '\$.provider_base_url')) AS base_url,
       JSON_UNQUOTE(JSON_EXTRACT(iron_agent_config, '\$.provider_api_key')) AS api_key
FROM assistants
WHERE assistant_id IN (${ids});
" | tee "${LOG_DIR}/db-after-fix.txt"

  if [ -z "$TOKEN" ]; then
    warn "没有读取到 IRON_AGENT_INTERNAL_SERVICE_TOKEN，跳过重启。"
  else
    for assistant_id in $ASSISTANT_IDS; do
      log "repair 助手运行态 ${assistant_id}"
      repair_body="${LOG_DIR}/${assistant_id}.repair.json"
      repair_status="$(
        curl -sS -o "$repair_body" -w '%{http_code}' \
          -X POST "${ADMIN_BASE_URL%/}/api/v1/admin/assistants/${assistant_id}/repair" \
          -H "X-IronAgent-Internal-Token: ${TOKEN}" \
          -H "Accept: application/json"
      )"
      cat "$repair_body"
      echo
      log "repair 接口状态：assistant=${assistant_id}, http_status=${repair_status}, response=${repair_body}"
      case "$repair_status" in
        2*) ;;
        *) warn "repair 接口非 2xx：assistant=${assistant_id}, http_status=${repair_status}，继续尝试 restart" ;;
      esac

      log "重启助手 ${assistant_id}"
      restart_body="${LOG_DIR}/${assistant_id}.restart.json"
      restart_status="$(
        curl -sS -o "$restart_body" -w '%{http_code}' \
          -X POST "${ADMIN_BASE_URL%/}/api/v1/admin/assistants/${assistant_id}/restart" \
        -H "X-IronAgent-Internal-Token: ${TOKEN}" \
          -H "Accept: application/json"
      )"
      cat "$restart_body"
      echo
      log "重启接口状态：assistant=${assistant_id}, http_status=${restart_status}, response=${restart_body}"
      case "$restart_status" in
        2*) ;;
        *) warn "重启接口非 2xx：assistant=${assistant_id}, http_status=${restart_status}，继续检查运行态并保留日志" ;;
      esac
    done
    log "等待 Pod 重启 ${WAIT_AFTER_RESTART_SECONDS} 秒"
    sleep "$WAIT_AFTER_RESTART_SECONDS"
  fi
fi

for assistant_id in $ASSISTANT_IDS; do
  log "检查助手运行态配置：${assistant_id}"
  pod="$(find_pod "$assistant_id" || true)"
  if [ -z "$pod" ]; then
    warn "未找到助手 Pod：${assistant_id}"
    continue
  fi
  log "pod=${pod}"

  secret="ironagent-assistant-${assistant_id}-secret"
  if kubectl -n "$NS" get secret "$secret" >/dev/null 2>&1; then
    log "K8s Secret 配置摘要：${secret}"
    kubectl -n "$NS" get secret "$secret" -o jsonpath='{.data.ironagent\.json}' \
      | base64 -d \
      | tee "${LOG_DIR}/${assistant_id}.secret-ironagent.json" \
      | extract_iron_agent_summary
  else
    warn "未找到 K8s Secret：${secret}"
  fi

  log "容器 ironagent.json 配置摘要"
  kubectl -n "$NS" exec "$pod" -c ironagent-gateway -- \
    sh -lc 'cat /home/node/.ironagent/ironagent.json' \
    | tee "${LOG_DIR}/${assistant_id}.runtime-ironagent.json" \
    | extract_iron_agent_summary

  probe_from_pod "$assistant_id" "$pod"
done

log "完成。完整日志：${LOG_FILE}"
