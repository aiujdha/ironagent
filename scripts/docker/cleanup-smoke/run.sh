#!/usr/bin/env bash
set -euo pipefail

cd /repo

export IRONAGENT_STATE_DIR="/tmp/ironagent-test"
export IRONAGENT_CONFIG_PATH="${IRONAGENT_STATE_DIR}/ironagent.json"

echo "==> Build"
pnpm build

echo "==> Seed state"
mkdir -p "${IRONAGENT_STATE_DIR}/credentials"
mkdir -p "${IRONAGENT_STATE_DIR}/agents/main/sessions"
echo '{}' >"${IRONAGENT_CONFIG_PATH}"
echo 'creds' >"${IRONAGENT_STATE_DIR}/credentials/marker.txt"
echo 'session' >"${IRONAGENT_STATE_DIR}/agents/main/sessions/sessions.json"

echo "==> Reset (config+creds+sessions)"
pnpm ironagent reset --scope config+creds+sessions --yes --non-interactive

test ! -f "${IRONAGENT_CONFIG_PATH}"
test ! -d "${IRONAGENT_STATE_DIR}/credentials"
test ! -d "${IRONAGENT_STATE_DIR}/agents/main/sessions"

echo "==> Recreate minimal config"
mkdir -p "${IRONAGENT_STATE_DIR}/credentials"
echo '{}' >"${IRONAGENT_CONFIG_PATH}"

echo "==> Uninstall (state only)"
pnpm ironagent uninstall --state --yes --non-interactive

test ! -d "${IRONAGENT_STATE_DIR}"

echo "OK"
