#!/usr/bin/env bash
set -euo pipefail

# 请在能够访问 IronAgent Admin API 的服务器或内网环境中运行。
# 示例：
#   ADMIN_BASE=http://127.0.0.1:30808 ADMIN_TOKEN=change-me \
#     bash scripts/create_test_assistants_1001_1002.sh

ADMIN_BASE="${ADMIN_BASE:-http://127.0.0.1:30808}"
ADMIN_TOKEN="${ADMIN_TOKEN:-change-me}"

# 以下模型参数复用 test_minimax_model_connectivity.sh 中已验证通过的信息。
PRIMARY_MODEL="${PRIMARY_MODEL:-minimax-m2.7}"
PROVIDER_ID="${PROVIDER_ID:-minimax}"
PROVIDER_BASE_URL="${PROVIDER_BASE_URL:-http://21.65.80.131:8012/v1}"
PROVIDER_API_KEY="${PROVIDER_API_KEY:-***}"
CAPABILITY_PROFILE="${CAPABILITY_PROFILE:-chat}"

request_json() {
  local method="$1"
  local path="$2"
  local body="$3"
  local output_file="$4"
  local code

  code=$(curl -sS \
    -o "${output_file}" \
    -w "%{http_code}" \
    -X "${method}" "${ADMIN_BASE%/}${path}" \
    -H "Content-Type: application/json" \
    -H "X-IronAgent-Internal-Token: ${ADMIN_TOKEN}" \
    -H "X-Admin-Token: ${ADMIN_TOKEN}" \
    -d "${body}")
  printf "%s" "${code}"
}

fail_response() {
  local label="$1"
  local code="$2"
  local output_file="$3"

  echo "${label}: failed with HTTP ${code}" >&2
  cat "${output_file}" >&2
  echo >&2
  exit 1
}

create_or_update_user() {
  local user_id="$1"

  local body
  body=$(cat <<JSON
{
  "user_id": "${user_id}",
  "display_name": "${user_id}",
  "department": "",
  "employee_id": "",
  "role": "user"
}
JSON
)

  local code response_file
  response_file="/tmp/ironagent-user-${user_id}.json"
  code=$(request_json POST "/api/v1/admin/users" "${body}" "${response_file}")
  if [[ "${code}" == 2* ]]; then
    echo "user ${user_id}: created"
    return
  fi
  if [[ "${code}" != "409" ]]; then
    fail_response "user ${user_id}" "${code}" "${response_file}"
  fi

  code=$(request_json PUT "/api/v1/admin/users/${user_id}" "${body}" "${response_file}")
  if [[ "${code}" != 2* ]]; then
    fail_response "user ${user_id} update" "${code}" "${response_file}"
  fi
  echo "user ${user_id}: updated"
}

bind_existing_assistant() {
  local user_id="$1"
  local assistant_id="$2"
  local assistant_name="$3"

  local body
  body=$(cat <<JSON
{
  "assistant_id": "${assistant_id}",
  "alias": "${assistant_name}",
  "source_system": "server-script"
}
JSON
)

  local code response_file
  response_file="/tmp/ironagent-bind-${user_id}-${assistant_id}.json"
  code=$(request_json POST "/api/v1/admin/users/${user_id}/assistants/bind" "${body}" "${response_file}")
  if [[ "${code}" != 2* ]]; then
    fail_response "assistant ${assistant_id} bind" "${code}" "${response_file}"
  fi
  echo "assistant ${assistant_id}: already exists, bound to user ${user_id}"
}

create_or_bind_assistant() {
  local user_id="$1"
  local assistant_id="$2"
  local assistant_name="$3"

  local body response_file
  response_file="/tmp/ironagent-assistant-${assistant_id}.json"
  body=$(cat <<JSON
{
  "assistant_id": "${assistant_id}",
  "name": "${assistant_name}",
  "capability_profile": "${CAPABILITY_PROFILE}",
  "user_id": "${user_id}",
  "notes": "",
  "primary_model": "${PRIMARY_MODEL}",
  "provider_id": "${PROVIDER_ID}",
  "provider_base_url": "${PROVIDER_BASE_URL}",
  "provider_api_key": "${PROVIDER_API_KEY}",
  "workspace_slug": "${assistant_id}"
}
JSON
)

  local code
  code=$(request_json POST "/api/v1/admin/assistants" "${body}" "${response_file}")
  if [[ "${code}" == 2* ]]; then
    echo "assistant ${assistant_id} (${assistant_name}): created and bound to user ${user_id}"
    return
  fi
  if [[ "${code}" != "409" ]] || ! grep -q "assistant id already exists" "${response_file}"; then
    fail_response "assistant ${assistant_id}" "${code}" "${response_file}"
  fi

  bind_existing_assistant "${user_id}" "${assistant_id}" "${assistant_name}"
}

create_or_update_user "12064912"
create_or_bind_assistant "12064912" "1001" "test00011"

create_or_update_user "12059433"
create_or_bind_assistant "12059433" "1002" "test00012"

echo "done"
