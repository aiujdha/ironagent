#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ADMIN_BASE="${ADMIN_BASE:-http://192.0.2.10:30808}"
TOKEN="${IRON_AGENT_INTERNAL_SERVICE_TOKEN:-${ADMIN_TOKEN:-change-me}}"
RUN_ID="${RUN_ID:-gw20-$(date +%Y%m%d%H%M%S)}"
COUNT="${COUNT:-20}"
OUT_DIR="${OUT_DIR:-output/${RUN_ID}-server-setup}"
CASES_FILE="${CASES_FILE:-${OUT_DIR}/cases.json}"
CAPABILITY_PROFILE="${CAPABILITY_PROFILE:-work_no_exec}"
ALLOW_DANGEROUS_PROFILE="${ALLOW_DANGEROUS_PROFILE:-0}"
MODEL_ID="${MODEL_ID:-dashscope-coding-minimax-m2-5}"
PRIMARY_MODEL="${PRIMARY_MODEL:-MiniMax-M2.5}"
PROVIDER_ID="${PROVIDER_ID:-dashscope-coding}"
PROVIDER_BASE_URL="${PROVIDER_BASE_URL:-https://coding.dashscope.aliyuncs.com/v1}"
PROVIDER_API_KEY="${PROVIDER_API_KEY:-}"
START_RUNTIME="${START_RUNTIME:-true}"
CURL_MAX_TIME="${CURL_MAX_TIME:-60}"

usage() {
  cat <<'EOF'
用法：
  scripts/k8s/create_gateway_20_proof_assistants.sh [环境变量]

用途：
  批量创建 20 个 Gateway 并发证明用用户/助手，输出 cases.json 给
  scripts/e2e/prove_gateway_20_assistants.py 使用。

常用环境变量：
  ADMIN_BASE=http://192.0.2.10:30808
  IRON_AGENT_INTERNAL_SERVICE_TOKEN=...
  RUN_ID=gw20-20260607120000
  COUNT=20
  MODEL_ID=dashscope-coding-minimax-m2-5
  CAPABILITY_PROFILE=work_no_exec

如果不想使用模型注册表，可清空 MODEL_ID 并传直连 provider：
  MODEL_ID=''
  PRIMARY_MODEL='MiniMax-M2.5'
  PROVIDER_ID='dashscope-coding'
  PROVIDER_BASE_URL='https://coding.dashscope.aliyuncs.com/v1'
  PROVIDER_API_KEY='sk-...'

输出：
  $OUT_DIR/cases.json
  $OUT_DIR/create-XX.json
  $OUT_DIR/repair-XX.json
  $OUT_DIR/restart-XX.json
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ "$CAPABILITY_PROFILE" == "dangerous" && "$ALLOW_DANGEROUS_PROFILE" != "1" ]]; then
  echo "CAPABILITY_PROFILE=dangerous requires ALLOW_DANGEROUS_PROFILE=1" >&2
  exit 2
fi

mkdir -p "$OUT_DIR"
chmod 700 "$OUT_DIR"

python3 - "$CASES_FILE" "$RUN_ID" "$COUNT" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
run_id = sys.argv[2]
count = int(sys.argv[3])
rows = [
    {
        "case_id": f"a{i:02d}",
        "user_id": f"{run_id}-user-{i:02d}",
        "assistant_id": f"{run_id}-assistant-{i:02d}",
    }
    for i in range(1, count + 1)
]
path.parent.mkdir(parents=True, exist_ok=True)
json.dump({"cases": rows}, path.open("w"), ensure_ascii=False, indent=2)
path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
print(path)
PY

echo "run_id=$RUN_ID"
echo "admin_base=$ADMIN_BASE"
echo "cases_file=$CASES_FILE"
echo "model_id=${MODEL_ID:-<direct-provider>}"
echo "provider=${PROVIDER_ID}/${PRIMARY_MODEL}"
echo "provider_base_url=$PROVIDER_BASE_URL"
echo "provider_api_key_present=$([[ -n "$PROVIDER_API_KEY" ]] && echo yes || echo no)"

for i in $(seq -w 1 "$COUNT"); do
  user_id="${RUN_ID}-user-${i}"
  assistant_id="${RUN_ID}-assistant-${i}"
  echo "[$(date '+%F %T')] create $user_id -> $assistant_id"

  create_cmd=(
    python3 scripts/create_user_and_assistant.py
    --admin-base "$ADMIN_BASE"
    --token "$TOKEN"
    --user-id "$user_id"
    --display-name "GW20 Proof User $i"
    --employee-id "$user_id"
    --assistant-id "$assistant_id"
    --assistant-name "GW20 Proof Assistant $i"
    --capability-profile "$CAPABILITY_PROFILE"
    --workspace-slug "$assistant_id"
  )
  if [[ -n "$MODEL_ID" ]]; then
    create_cmd+=(--model-id "$MODEL_ID")
  else
    create_cmd+=(
      --primary-model "$PRIMARY_MODEL"
      --provider-id "$PROVIDER_ID"
      --provider-base-url "$PROVIDER_BASE_URL"
      --provider-api-key "$PROVIDER_API_KEY"
    )
  fi

  "${create_cmd[@]}" >"${OUT_DIR}/create-${i}.json"

  if [[ "$START_RUNTIME" == "true" ]]; then
    curl -sS -o "${OUT_DIR}/repair-${i}.json" -w '%{http_code}\n' --max-time "$CURL_MAX_TIME" \
      -X POST "${ADMIN_BASE%/}/api/v1/admin/assistants/${assistant_id}/repair" \
      -H "X-IronAgent-Internal-Token: ${TOKEN}" \
      -H "X-Admin-Token: ${TOKEN}" >"${OUT_DIR}/repair-${i}.status" || true
    curl -sS -o "${OUT_DIR}/restart-${i}.json" -w '%{http_code}\n' --max-time "$CURL_MAX_TIME" \
      -X POST "${ADMIN_BASE%/}/api/v1/admin/assistants/${assistant_id}/restart" \
      -H "X-IronAgent-Internal-Token: ${TOKEN}" \
      -H "X-Admin-Token: ${TOKEN}" >"${OUT_DIR}/restart-${i}.status" || true
  fi
done

{
  echo "RUN_ID=$RUN_ID"
  echo "OUT_DIR=$OUT_DIR"
  echo "CASES_FILE=$CASES_FILE"
  echo "ADMIN_BASE=$ADMIN_BASE"
} | tee "${OUT_DIR}/run.env"

echo "完成。下一步先等 /readyz 中 ready >= ${COUNT}，再运行 prove_gateway_20_assistants.py。"
