#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/iron_agent_smoke_common.sh
source "${SCRIPT_DIR}/lib/iron_agent_smoke_common.sh"

usage() {
  cat <<'EOF'
Usage:
  scripts/k8s/create_assistant.sh --user-id <user_id> [options]

Purpose:
  Create an assistant through the admin API, bind it to user_id, then repair/start
  the assistant runtime and print the launch URL.

Required creation field:
  --user-id <value>                 User ID to create or bind. Required.

Assistant creation fields:
  --assistant-id <value>            Assistant ID. Default: smoke-<timestamp>
  --name <value>                    Assistant name. Default: Smoke Assistant <assistant_id>
  --capability-profile <value>      Capability profile. Default: chat
  --notes <value>                   Assistant notes. Default: local smoke timestamp
  --model-id <value>                Model registry ID. Default: empty
  --primary-model <value>           Primary model. Default: minimax-m2.7
  --provider-id <value>             Provider ID. Default: minimax
  --provider-base-url <value>       Provider base URL. Default: http://21.65.80.131:8012/v1
  --provider-api-key <value>        Provider API key. Default: empty
  --workspace-slug <value>          Workspace slug. Default: assistant_id

Environment-compatible aliases:
  ASSISTANT_ID, NAME, CAPABILITY_PROFILE, USER_ID or ASSISTANT_USER_ID, NOTES,
  MODEL_ID, PRIMARY_MODEL, PROVIDER_ID, PROVIDER_BASE_URL, PROVIDER_API_KEY,
  WORKSPACE_SLUG, NS, GATEWAY_URL, WAIT_SECONDS.

Examples:
  scripts/k8s/create_assistant.sh \
    --user-id emp001 \
    --assistant-id emp001-chat \
    --name "emp001 chat assistant" \
    --capability-profile chat \
    --primary-model minimax-m2.7 \
    --provider-id minimax \
    --provider-base-url http://21.65.80.131:8012/v1

  USER_ID=emp001 NAME="测试助手" scripts/k8s/create_assistant.sh
EOF
}

ASSISTANT_ID="${ASSISTANT_ID:-smoke-$(date +%Y%m%d%H%M%S)}"
NAME="${NAME:-Smoke Assistant ${ASSISTANT_ID}}"
CAPABILITY_PROFILE="${CAPABILITY_PROFILE:-chat}"
ASSISTANT_USER_ID="${ASSISTANT_USER_ID:-${USER_ID:-}}"
WORKSPACE_SLUG="${WORKSPACE_SLUG:-${ASSISTANT_ID}}"

NOTES="${NOTES:-local terminal smoke test created at $(date '+%Y-%m-%d %H:%M:%S')}"
MODEL_ID="${MODEL_ID:-}"
PRIMARY_MODEL="${PRIMARY_MODEL:-minimax-m2.7}"
PROVIDER_ID="${PROVIDER_ID:-minimax}"
PROVIDER_BASE_URL="${PROVIDER_BASE_URL:-http://21.65.80.131:8012/v1}"
PROVIDER_API_KEY="${PROVIDER_API_KEY:-}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --assistant-id)
      ASSISTANT_ID="${2:-}"
      shift 2
      ;;
    --name)
      NAME="${2:-}"
      shift 2
      ;;
    --capability-profile)
      CAPABILITY_PROFILE="${2:-}"
      shift 2
      ;;
    --user-id)
      ASSISTANT_USER_ID="${2:-}"
      shift 2
      ;;
    --notes)
      NOTES="${2:-}"
      shift 2
      ;;
    --model-id)
      MODEL_ID="${2:-}"
      shift 2
      ;;
    --primary-model)
      PRIMARY_MODEL="${2:-}"
      shift 2
      ;;
    --provider-id)
      PROVIDER_ID="${2:-}"
      shift 2
      ;;
    --provider-base-url)
      PROVIDER_BASE_URL="${2:-}"
      shift 2
      ;;
    --provider-api-key)
      PROVIDER_API_KEY="${2:-}"
      shift 2
      ;;
    --workspace-slug)
      WORKSPACE_SLUG="${2:-}"
      shift 2
      ;;
    --namespace|-n)
      NS="${2:-}"
      shift 2
      ;;
    --gateway-url)
      GATEWAY_URL="${2:-}"
      shift 2
      ;;
    --wait-seconds)
      WAIT_SECONDS="${2:-}"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[ -n "$ASSISTANT_USER_ID" ] || {
  usage >&2
  echo >&2
  echo "ERROR: --user-id or ASSISTANT_USER_ID/USER_ID is required." >&2
  exit 2
}
[ -n "$ASSISTANT_ID" ] || {
  echo "ERROR: assistant_id must not be empty." >&2
  exit 2
}
[ -n "$NAME" ] || {
  echo "ERROR: name must not be empty." >&2
  exit 2
}
[ -n "$CAPABILITY_PROFILE" ] || {
  echo "ERROR: capability_profile must not be empty." >&2
  exit 2
}
[ -n "$WORKSPACE_SLUG" ] || WORKSPACE_SLUG="$ASSISTANT_ID"

init_iron_agent_smoke "create-assistant"

write_create_body() {
  local out_file="$1"
  if [ -n "$PROVIDER_BASE_URL" ] && ! printf '%s' "$PROVIDER_BASE_URL" | grep -Eq '^https?://'; then
    PROVIDER_BASE_URL="http://${PROVIDER_BASE_URL}"
  fi

  python3 - "$out_file" "$ASSISTANT_ID" "$NAME" "$CAPABILITY_PROFILE" "$ASSISTANT_USER_ID" "$NOTES" "$MODEL_ID" "$PRIMARY_MODEL" "$PROVIDER_ID" "$PROVIDER_BASE_URL" "$PROVIDER_API_KEY" "$WORKSPACE_SLUG" <<'PY'
import json
import sys

(
    out_file,
    assistant_id,
    name,
    capability_profile,
    user_id,
    notes,
    model_id,
    primary_model,
    provider_id,
    provider_base_url,
    provider_api_key,
    workspace_slug,
) = sys.argv[1:13]

payload = {
    "assistant_id": assistant_id,
    "name": name,
    "capability_profile": capability_profile,
    "user_id": user_id,
    "notes": notes,
    "model_id": model_id,
    "primary_model": primary_model,
    "provider_id": provider_id,
    "provider_base_url": provider_base_url,
    "provider_api_key": provider_api_key,
    "workspace_slug": workspace_slug,
}
with open(out_file, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
}

log "Log file: ${LOG_FILE}"
log "This script does not print secret values."
log "Required binding user_id: ${ASSISTANT_USER_ID}"

check_basic_dependencies
check_namespace
load_required_secrets
ensure_gateway_access
print_readyz

set_step "create assistant"
CREATE_BODY="$(mktemp)"
CREATE_RESPONSE="$(mktemp)"
write_create_body "$CREATE_BODY"

log "Create parameters: assistant_id=${ASSISTANT_ID}, name=${NAME}, capability_profile=${CAPABILITY_PROFILE}, user_id=${ASSISTANT_USER_ID}, model_id=${MODEL_ID:-<empty>}, primary_model=${PRIMARY_MODEL}, provider_id=${PROVIDER_ID:-<empty>}, provider_base_url=${PROVIDER_BASE_URL:-<empty>}, workspace_slug=${WORKSPACE_SLUG}"
if admin_request POST /api/v1/admin/assistants "$CREATE_RESPONSE" "$CREATE_BODY"; then
  print_json_file "$CREATE_RESPONSE"
else
  if grep -q "assistant name already exists\\|assistant id already exists" "$CREATE_RESPONSE"; then
    warn "Assistant already exists; continuing with repair/start."
  else
    die "Assistant creation failed."
  fi

repair_and_start_assistant
wait_for_assistant_pod_ready
get_assistant_launch_url

set_step "final health check"
print_readyz

log "Create assistant route completed."
print_route_summary
