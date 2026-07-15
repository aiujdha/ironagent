#!/usr/bin/env bash
set -euo pipefail

NS="${NS:-ironagent-kf}"
ADMIN_BASE="${ADMIN_BASE:-${ADMIN:-http://192.0.2.10:30808}}"
MODEL_NAME="${MODEL_NAME:-minimax-m2.7}"
MODEL_PROVIDER="${MODEL_PROVIDER:-minimax}"
MODEL_URL="${MODEL_URL:-http://21.65.80.131:8012/v1}"
MODEL_STATUS="${MODEL_STATUS:-active}"
MODEL_API_KEY="${MODEL_API_KEY:-${MINIMAX_API_KEY:-}}"
TOKEN="${IRON_AGENT_INTERNAL_SERVICE_TOKEN:-}"
PROBE_AFTER="${PROBE_AFTER:-1}"
TEST_PROVIDER="${TEST_PROVIDER:-0}"
ALLOW_PLACEHOLDER_API_KEY="${ALLOW_PLACEHOLDER_API_KEY:-0}"
DRY_RUN="${DRY_RUN:-0}"

log() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*"
}

die() {
  log "[错误] $*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "缺少命令：$1"
}

mask_secret() {
  local value="$1"
  local len=${#value}
  if [ "$len" -le 6 ]; then
    printf '******'
    return
  fi
  printf '%s***%s' "${value:0:3}" "${value: -3}"
}

usage() {
  cat <<'EOF'
用途：新增或更新后台“模型认证”中的 minimax-m2.7 配置。

默认值：
  MODEL_NAME=minimax-m2.7
  MODEL_PROVIDER=minimax
  MODEL_URL=http://21.65.80.131:8012/v1
  ADMIN_BASE=http://192.0.2.10:30808
  NS=ironagent-kf

运行示例：
  MINIMAX_API_KEY='真实 API Key' \
  bash scripts/k8s/add_minimax_model_auth.sh

如不在集群机器上运行，请显式传入内部 token：
  IRON_AGENT_INTERNAL_SERVICE_TOKEN='...' \
  MINIMAX_API_KEY='真实 API Key' \
  ADMIN_BASE='http://192.0.2.10:30808' \
  bash scripts/k8s/add_minimax_model_auth.sh

可选：
  PROBE_AFTER=0      跳过后台模型配置探活
  TEST_PROVIDER=1    额外直连 MODEL_URL/chat/completions 做真实模型调用
  DRY_RUN=1          只打印将写入的配置，不调用后台 API
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

need_cmd curl
need_cmd python3

if [ -z "$MODEL_API_KEY" ]; then
  die "未提供 API Key。请用 MINIMAX_API_KEY='真实 API Key' 或 MODEL_API_KEY='真实 API Key' 传入。"
fi

if [ "$MODEL_API_KEY" = "***" ] && [ "$ALLOW_PLACEHOLDER_API_KEY" != "1" ]; then
  die "当前 API Key 是占位符 ***。如确实要写入字面值 ***，请加 ALLOW_PLACEHOLDER_API_KEY=1。"
fi

export MODEL_NAME MODEL_PROVIDER MODEL_URL MODEL_STATUS MODEL_API_KEY

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

MODELS_JSON="$WORKDIR/models.json"
PAYLOAD_JSON="$WORKDIR/payload.json"
RESPONSE_JSON="$WORKDIR/response.json"
HTTP_CODE_FILE="$WORKDIR/http_code"

python3 - "$PAYLOAD_JSON" <<'PY'
import json
import os
import sys

payload_path = sys.argv[1]
payload = {
    "name": os.environ["MODEL_NAME"].strip(),
    "provider": os.environ["MODEL_PROVIDER"].strip(),
    "url": os.environ["MODEL_URL"].strip(),
    "apiKey": os.environ["MODEL_API_KEY"].strip(),
    "status": os.environ["MODEL_STATUS"].strip() or "active",
}
with open(payload_path, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=False)
PY

log "[信息] 模型名称：${MODEL_NAME}"
log "[信息] 供应商：${MODEL_PROVIDER}"
log "[信息] 模型地址：${MODEL_URL}"
log "[信息] 管理后台：${ADMIN_BASE%/}"
log "[信息] API Key：$(mask_secret "$MODEL_API_KEY")"

if [ "$DRY_RUN" = "1" ]; then
  log "[DRY_RUN] 将写入以下配置（apiKey 已隐藏）："
  python3 - "$PAYLOAD_JSON" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
payload["apiKey"] = "******"
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
  exit 0
fi

if [ -z "$TOKEN" ]; then
  if command -v kubectl >/dev/null 2>&1; then
    TOKEN="$(kubectl -n "$NS" get secret control-plane-shared-secret \
      -o jsonpath='{.data.IRON_AGENT_INTERNAL_SERVICE_TOKEN}' 2>/dev/null \
      | base64 -d 2>/dev/null || true)"
  fi
fi

if [ -z "$TOKEN" ]; then
  die "没有读取到 IRON_AGENT_INTERNAL_SERVICE_TOKEN。请在集群机器上运行，或先 export IRON_AGENT_INTERNAL_SERVICE_TOKEN=..."
fi

api_request() {
  local method="$1"
  local path="$2"
  local data_file="${3:-}"
  local url="${ADMIN_BASE%/}${path}"
  local code

  if [ -n "$data_file" ]; then
    code="$(curl -sS \
      -o "$RESPONSE_JSON" \
      -w '%{http_code}' \
      -X "$method" "$url" \
      -H "X-IronAgent-Internal-Token: ${TOKEN}" \
      -H 'Accept: application/json' \
      -H 'Content-Type: application/json' \
      --data-binary "@${data_file}")"
  else
    code="$(curl -sS \
      -o "$RESPONSE_JSON" \
      -w '%{http_code}' \
      -X "$method" "$url" \
      -H "X-IronAgent-Internal-Token: ${TOKEN}" \
      -H 'Accept: application/json')"
  fi

  printf '%s\n' "$code" >"$HTTP_CODE_FILE"
  if [[ "$code" != 2* ]]; then
    log "[错误] ${method} ${url} 失败：HTTP ${code}" >&2
    cat "$RESPONSE_JSON" >&2 || true
    echo >&2
    exit 1
  fi

  cat "$RESPONSE_JSON"
}

log "[信息] 拉取当前模型认证列表"
api_request GET "/api/v1/admin/models" >"$MODELS_JSON"

EXISTING_MODEL_ID="$(
  python3 - "$MODELS_JSON" <<'PY'
import json
import os
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
name = os.environ["MODEL_NAME"].strip()
provider = os.environ["MODEL_PROVIDER"].strip()
url = os.environ["MODEL_URL"].strip()
items = payload.get("items", [])
matches = [
    item for item in items
    if item.get("name") == name and item.get("provider") == provider
]
exact = [item for item in matches if (item.get("url") or "").strip() == url]
target = (exact or matches or [None])[0]
print((target or {}).get("id", ""))
PY
)"

if [ -n "$EXISTING_MODEL_ID" ]; then
  log "[信息] 已存在模型认证 id=${EXISTING_MODEL_ID}，执行更新"
  api_request PUT "/api/v1/admin/models/${EXISTING_MODEL_ID}" "$PAYLOAD_JSON"
  MODEL_ID="$EXISTING_MODEL_ID"
else
  log "[信息] 未找到同名供应商模型，执行新增"
  CREATE_RESPONSE="$(api_request POST "/api/v1/admin/models" "$PAYLOAD_JSON")"
  printf '%s\n' "$CREATE_RESPONSE" >"$RESPONSE_JSON"
  MODEL_ID="$(python3 - "$RESPONSE_JSON" <<'PY'
import json
import sys

print(json.load(open(sys.argv[1], encoding="utf-8")).get("id", ""))
PY
)"
  [ -n "$MODEL_ID" ] || die "新增成功响应中没有 id"
fi

log "[信息] 模型认证 id=${MODEL_ID}"

if [ "$PROBE_AFTER" = "1" ]; then
  log "[信息] 执行后台模型配置探活（该探活只校验后台配置字段）"
  api_request POST "/api/v1/admin/models/${MODEL_ID}/probe"
fi

if [ "$TEST_PROVIDER" = "1" ]; then
  PROVIDER_RESPONSE="$WORKDIR/provider-response.json"
  PROVIDER_BODY="$WORKDIR/provider-request.json"
  python3 - "$PROVIDER_BODY" <<'PY'
import json
import os
import sys

payload = {
    "model": os.environ["MODEL_NAME"].strip(),
    "messages": [{"role": "user", "content": "只回复 OK"}],
    "temperature": 0,
    "max_tokens": 16,
}
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=False)
PY
  PROVIDER_AUTHORIZATION="${MODEL_API_KEY}"
  case "$(printf '%s' "$PROVIDER_AUTHORIZATION" | tr '[:upper:]' '[:lower:]')" in
    bearer\ *) ;;
    *) PROVIDER_AUTHORIZATION="Bearer ${PROVIDER_AUTHORIZATION}" ;;
  esac
  log "[信息] 直连模型接口测试：${MODEL_URL%/}/chat/completions"
  PROVIDER_CODE="$(curl -sS \
    --connect-timeout 30 \
    --max-time 30 \
    -o "$PROVIDER_RESPONSE" \
    -w '%{http_code}' \
    -X POST "${MODEL_URL%/}/chat/completions" \
    -H 'Content-Type: application/json' \
    -H "Authorization: ${PROVIDER_AUTHORIZATION}" \
    --data-binary "@${PROVIDER_BODY}")"
  if [[ "$PROVIDER_CODE" != 2* ]]; then
    log "[错误] 直连模型接口失败：HTTP ${PROVIDER_CODE}" >&2
    cat "$PROVIDER_RESPONSE" >&2 || true
    echo >&2
    exit 1
  fi
  log "[信息] 直连模型接口通过：HTTP ${PROVIDER_CODE}"
  cat "$PROVIDER_RESPONSE"
  echo
fi

log "[完成] 模型认证已新增或更新：${MODEL_ID}"
