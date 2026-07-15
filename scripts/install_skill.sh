#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  install_skill.sh --assistant-id ASSISTANT_ID --skill-package /path/to/skill.zip [options]

Options:
  --admin-base URL       Admin API base URL. Default: ADMIN_BASE or http://127.0.0.1:8501
  --admin-token TOKEN    Internal admin token. Default: IRON_AGENT_INTERNAL_SERVICE_TOKEN
  --assistant-id ID      Assistant ID to install the skill to. Default: ASSISTANT_ID
  --skill-package PATH   Skill zip package path. Default: SKILL_PACKAGE
  --skill-slug SLUG      Skill slug. Default: SKILL_SLUG or package filename stem
  --skill-name NAME      Skill display name. Default: SKILL_NAME or slug
  --skill-owner OWNER    Skill owner. Default: SKILL_OWNER or e2e
  --skill-version VER    Skill version. Default: SKILL_VERSION or 0.1.0
  --tags TAGS            Skill tags. Default: e2e,test
  -h, --help             Show this help.

Environment fallback:
  ADMIN_BASE, IRON_AGENT_INTERNAL_SERVICE_TOKEN, ASSISTANT_ID, SKILL_PACKAGE,
  SKILL_SLUG, SKILL_NAME, SKILL_OWNER, SKILL_VERSION
EOF
}

ADMIN_BASE="${ADMIN_BASE:-http://127.0.0.1:8501}"
ADMIN_TOKEN="${IRON_AGENT_INTERNAL_SERVICE_TOKEN:-${ADMIN_TOKEN:-}}"
ASSISTANT_ID="${ASSISTANT_ID:-}"
SKILL_PACKAGE="${SKILL_PACKAGE:-}"
SKILL_SLUG="${SKILL_SLUG:-}"
SKILL_NAME="${SKILL_NAME:-}"
SKILL_OWNER="${SKILL_OWNER:-e2e}"
SKILL_VERSION="${SKILL_VERSION:-0.1.0}"
TAGS="${TAGS:-e2e,test}"

while [ $# -gt 0 ]; do
  case "$1" in
    --admin-base)
      ADMIN_BASE="${2:?missing value for --admin-base}"
      shift 2
      ;;
    --admin-token)
      ADMIN_TOKEN="${2:?missing value for --admin-token}"
      shift 2
      ;;
    --assistant-id)
      ASSISTANT_ID="${2:?missing value for --assistant-id}"
      shift 2
      ;;
    --skill-package)
      SKILL_PACKAGE="${2:?missing value for --skill-package}"
      shift 2
      ;;
    --skill-slug)
      SKILL_SLUG="${2:?missing value for --skill-slug}"
      shift 2
      ;;
    --skill-name)
      SKILL_NAME="${2:?missing value for --skill-name}"
      shift 2
      ;;
    --skill-owner)
      SKILL_OWNER="${2:?missing value for --skill-owner}"
      shift 2
      ;;
    --skill-version)
      SKILL_VERSION="${2:?missing value for --skill-version}"
      shift 2
      ;;
    --tags)
      TAGS="${2:?missing value for --tags}"
      shift 2
      ;;
    -h|--help)
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

if [ -z "$ADMIN_TOKEN" ]; then
  echo "Missing admin token. Set IRON_AGENT_INTERNAL_SERVICE_TOKEN or pass --admin-token." >&2
  exit 2
fi
if [ -z "$ASSISTANT_ID" ]; then
  echo "Missing assistant id. Set ASSISTANT_ID or pass --assistant-id." >&2
  exit 2
fi
if [ -z "$SKILL_PACKAGE" ]; then
  echo "Missing skill package. Set SKILL_PACKAGE or pass --skill-package." >&2
  exit 2
fi
if [ ! -f "$SKILL_PACKAGE" ]; then
  echo "Skill package not found: $SKILL_PACKAGE" >&2
  exit 2
fi
command -v curl >/dev/null 2>&1 || {
  echo "curl is required" >&2
  exit 2
}
command -v python3 >/dev/null 2>&1 || {
  echo "python3 is required to parse API responses" >&2
  exit 2
}

if [ -z "$SKILL_SLUG" ]; then
  filename="$(basename "$SKILL_PACKAGE")"
  SKILL_SLUG="${filename%.*}"
fi
if [ -z "$SKILL_NAME" ]; then
  SKILL_NAME="$SKILL_SLUG"
fi

request() {
  local method="$1"
  local url="$2"
  shift 2
  local body_file
  body_file="$(mktemp)"
  local status
  status="$(
    curl -sS -o "$body_file" -w "%{http_code}" \
      -X "$method" "$url" \
      -H "X-IronAgent-Internal-Token: $ADMIN_TOKEN" \
      "$@"
  )"
  local body
  body="$(cat "$body_file")"
  rm -f "$body_file"
  if [ "$status" -lt 200 ] || [ "$status" -ge 300 ]; then
    echo "HTTP $status $method $url" >&2
    echo "$body" >&2
    exit 1
  fi
  printf '%s' "$body"
}

echo "[1/3] upload skill package"
UPLOAD_JSON="$(
  request POST "$ADMIN_BASE/api/v1/market/skills/upload" \
    -F "slug=$SKILL_SLUG" \
    -F "display_name=$SKILL_NAME" \
    -F "owner=$SKILL_OWNER" \
    -F "version=$SKILL_VERSION" \
    -F "tags=$TAGS" \
    -F "file=@$SKILL_PACKAGE"
)"
printf '%s\n' "$UPLOAD_JSON"

SKILL_ID="$(
  printf '%s' "$UPLOAD_JSON" | python3 -c '
import json, sys
payload = json.load(sys.stdin)
print(payload.get("id") or payload.get("skill_id") or payload.get("skillId") or "")
'
)"
if [ -z "$SKILL_ID" ]; then
  echo "Cannot parse skill id from upload response." >&2
  exit 1
fi

echo "[2/3] approve skill: $SKILL_ID"
request POST "$ADMIN_BASE/api/v1/market/skills/$SKILL_ID/approve"
echo

echo "[3/3] install skill to assistant: $ASSISTANT_ID"
INSTALL_JSON="$(
  request POST "$ADMIN_BASE/api/v1/market/skills/$SKILL_ID/install" \
    -F "assistant_id=$ASSISTANT_ID"
)"
printf '%s\n' "$INSTALL_JSON"

echo "done: skill_id=$SKILL_ID assistant_id=$ASSISTANT_ID"
