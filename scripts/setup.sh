#!/usr/bin/env bash
set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install it first."
  exit 1
fi

uv sync
cp -n .env.example .env || true
echo "Setup complete."
