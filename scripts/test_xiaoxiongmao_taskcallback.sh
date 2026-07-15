#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/test_xiaoxiongmao_taskcallback.sh --url URL [options]

Purpose:
  Send one smoke-test callback request to Xiaoxiongmao taskcallback endpoint.

URL:
  You can pass the full callback URL:
    http://10.0.0.12:8080/intelligent-assistant/h5/iron/taskcallback

  Or just the base URL, and the script will append the standard path:
    http://10.0.0.12:8080

Options:
  --url URL                  Callback endpoint or base URL.
  --task-id ID               Default: task-callback-smoke-<timestamp>
  --status STATUS            Default: completed
  --stage STAGE              Default: finished
  --reply TEXT               Default: callback smoke ok
  --file-url URL             Optional download_url for the sample file.
  --token TOKEN              Optional Bearer token.
  --header HEADER            Extra curl header. Can be repeated.
  --timeout SECONDS          Default: 10
  --connect-timeout SECONDS  Default: 3
  --insecure                 Pass -k to curl for self-signed HTTPS.
  --verbose                  Print curl verbose logs.
  -h, --help                 Show this help.

Examples:
  scripts/test_xiaoxiongmao_taskcallback.sh \
    --url http://10.0.0.12:8080

  TASK_ID=task-001 STATUS=processing STAGE=tool_running \
    scripts/test_xiaoxiongmao_taskcallback.sh \
    --url http://10.0.0.12:8080/intelligent-assistant/h5/iron/taskcallback
EOF
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing required command: $1" >&2
    exit 2
  fi
}

normalize_callback_url() {
  local raw="$1"
  raw="${raw%/}"
  if [[ "$raw" == */intelligent-assistant/h5/iron/taskcallback ]]; then
    printf '%s\n' "$raw"
  else
    printf '%s/intelligent-assistant/h5/iron/taskcallback\n' "$raw"
  fi
}

url="${TASKCALLBACK_URL:-}"
task_id="${TASK_ID:-task-callback-smoke-$(date +%Y%m%d%H%M%S)}"
status="${STATUS:-completed}"
stage="${STAGE:-finished}"
reply_text="${REPLY_TEXT:-callback smoke ok}"
file_url="${FILE_DOWNLOAD_URL:-https://files.example.com/taskcallback-smoke-result.txt}"
timeout_seconds="${TIMEOUT_SECONDS:-10}"
connect_timeout_seconds="${CONNECT_TIMEOUT_SECONDS:-3}"
insecure=0
verbose=0
extra_headers=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)
      url="${2:-}"
      shift 2
      ;;
    --task-id)
      task_id="${2:-}"
      shift 2
      ;;
    --status)
      status="${2:-}"
      shift 2
      ;;
    --stage)
      stage="${2:-}"
      shift 2
      ;;
    --reply)
      reply_text="${2:-}"
      shift 2
      ;;
    --file-url)
      file_url="${2:-}"
      shift 2
      ;;
    --token)
      extra_headers+=("Authorization: Bearer ${2:-}")
      shift 2
      ;;
    --header)
      extra_headers+=("${2:-}")
      shift 2
      ;;
    --timeout)
      timeout_seconds="${2:-}"
      shift 2
      ;;
    --connect-timeout)
      connect_timeout_seconds="${2:-}"
      shift 2
      ;;
    --insecure)
      insecure=1
      shift
      ;;
    --verbose)
      verbose=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$url" ]]; then
  echo "missing --url or TASKCALLBACK_URL" >&2
  usage >&2
  exit 2
fi

require_cmd curl
require_cmd python3

callback_url="$(normalize_callback_url "$url")"
payload_file="$(mktemp)"
response_file="$(mktemp)"
trap 'rm -f "$payload_file" "$response_file"' EXIT

TASK_ID_VALUE="$task_id" \
STATUS_VALUE="$status" \
STAGE_VALUE="$stage" \
REPLY_TEXT_VALUE="$reply_text" \
FILE_DOWNLOAD_URL_VALUE="$file_url" \
python3 - "$payload_file" <<'PY'
import json
import os
import sys

payload = {
    "taskId": os.environ["TASK_ID_VALUE"],
    "status": os.environ["STATUS_VALUE"],
    "stage": os.environ["STAGE_VALUE"],
    "replyText": os.environ["REPLY_TEXT_VALUE"],
    "files": [
        {
            "fileId": "file-smoke-001",
            "fileName": "taskcallback-smoke-result.txt",
            "size": "17",
            "mimeType": "text/plain",
            "download_url": os.environ["FILE_DOWNLOAD_URL_VALUE"],
        }
    ],
    "errorCode": "",
    "errorMessage": "",
    "errorReason": "",
}

with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
PY

curl_args=(
  -sS
  -X POST "$callback_url"
  -H "Content-Type: application/json"
  --connect-timeout "$connect_timeout_seconds"
  --max-time "$timeout_seconds"
  --data-binary "@$payload_file"
  -o "$response_file"
  -w "%{http_code}"
)

if [[ "$insecure" -eq 1 ]]; then
  curl_args=(-k "${curl_args[@]}")
fi

if [[ "$verbose" -eq 1 ]]; then
  curl_args=(-v "${curl_args[@]}")
fi

if ((${#extra_headers[@]} > 0)); then
  for header in "${extra_headers[@]}"; do
    curl_args+=(-H "$header")
  done
fi

echo "callback_url=$callback_url"
echo "task_id=$task_id"
echo "status=$status"
echo "stage=$stage"
echo "request_body=$(cat "$payload_file")"

http_code="$(curl "${curl_args[@]}")"
response_body="$(cat "$response_file")"

echo "http_status=$http_code"
echo "response_body=$response_body"

if [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
  echo "result=success"
  exit 0
fi

echo "result=failed"
exit 1
