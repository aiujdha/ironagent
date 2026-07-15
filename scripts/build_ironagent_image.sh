#!/usr/bin/env bash
set -euo pipefail

IRON_AGENT_REPO_PATH="${1:-../ironagent-main}"
IRON_AGENT_IMAGE_TAG="${2:-ironagent-runtime:dev}"

if [[ ! -f "${IRON_AGENT_REPO_PATH}/Dockerfile" ]]; then
  echo "未找到 IronAgent Dockerfile: ${IRON_AGENT_REPO_PATH}/Dockerfile" >&2
  echo "请确认第一个参数指向本地 ironagent-main 仓库。" >&2
  exit 1
fi

echo "Building IronAgent image from: ${IRON_AGENT_REPO_PATH}"
echo "Target image tag: ${IRON_AGENT_IMAGE_TAG}"

docker build -t "${IRON_AGENT_IMAGE_TAG}" "${IRON_AGENT_REPO_PATH}"
