#!/usr/bin/env bash
set -euo pipefail

# 用途：测试 minimax 模型是否能通过 OpenAI-compatible 接口连通。
# 运行位置：请在能访问内网模型地址的服务器上执行。
# 默认只测试 minimax-m2.7，不测试其它模型。
#
# 示例：
#   bash scripts/test_minimax_model_connectivity.sh
#
# 如需临时覆盖参数：
#   MINIMAX_BASE_URL=http://21.65.80.131:8012/v1 \
#   MINIMAX_API_KEY='***' \
#   MINIMAX_MODEL=minimax-m2.7 \
#   bash scripts/test_minimax_model_connectivity.sh

MINIMAX_BASE_URL="${MINIMAX_BASE_URL:-http://21.65.80.131:8012/v1}"
MINIMAX_API_KEY="${MINIMAX_API_KEY:-***}"
MINIMAX_MODEL="${MINIMAX_MODEL:-minimax-m2.7}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-30}"

RESPONSE_FILE="${RESPONSE_FILE:-/tmp/minimax-model-connectivity-response.json}"
HTTP_CODE_FILE="${RESPONSE_FILE}.http_code"

CHAT_COMPLETIONS_URL="${MINIMAX_BASE_URL%/}/chat/completions"
AUTHORIZATION_HEADER="${MINIMAX_API_KEY}"
if [[ "${AUTHORIZATION_HEADER,,}" != bearer\ * ]]; then
  AUTHORIZATION_HEADER="Bearer ${AUTHORIZATION_HEADER}"
fi

REQUEST_BODY=$(cat <<JSON
{
  "model": "${MINIMAX_MODEL}",
  "messages": [
    {
      "role": "user",
      "content": "只回复 OK"
    }
  ],
  "temperature": 0,
  "max_tokens": 16
}
JSON
)

echo "开始测试 minimax 模型联通性"
echo "模型名称: ${MINIMAX_MODEL}"
echo "接口地址: ${CHAT_COMPLETIONS_URL}"
echo "响应文件: ${RESPONSE_FILE}"

HTTP_CODE=$(curl -sS \
  --connect-timeout "${TIMEOUT_SECONDS}" \
  --max-time "${TIMEOUT_SECONDS}" \
  -o "${RESPONSE_FILE}" \
  -w "%{http_code}" \
  -X POST "${CHAT_COMPLETIONS_URL}" \
  -H "Content-Type: application/json" \
  -H "Authorization: ${AUTHORIZATION_HEADER}" \
  -d "${REQUEST_BODY}")

printf "%s\n" "${HTTP_CODE}" >"${HTTP_CODE_FILE}"

if [[ "${HTTP_CODE}" != 2* ]]; then
  echo "联通性测试失败: HTTP ${HTTP_CODE}" >&2
  echo "响应内容:" >&2
  cat "${RESPONSE_FILE}" >&2
  echo >&2
  exit 1
fi

echo "联通性测试通过: HTTP ${HTTP_CODE}"
echo "响应内容:"
cat "${RESPONSE_FILE}"
echo
