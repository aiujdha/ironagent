#!/usr/bin/env bash
set -Eeuo pipefail

DEFAULT_OVERLAY="deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport"
OVERLAY="${1:-$DEFAULT_OVERLAY}"
REQUIRED_IMAGE_TAG="${REQUIRED_IMAGE_TAG:-}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

FAIL_COUNT=0
WARN_COUNT=0
RENDERED_FILE="$(mktemp "${TMPDIR:-/tmp}/ironagent-k8s-preflight.XXXXXX.yaml")"
trap 'rm -f "$RENDERED_FILE"' EXIT

pass() {
  printf 'PASS %s\n' "$*"
}

warn() {
  WARN_COUNT=$((WARN_COUNT + 1))
  printf 'WARN %s\n' "$*" >&2
}

fail() {
  FAIL_COUNT=$((FAIL_COUNT + 1))
  printf 'FAIL %s\n' "$*" >&2
}

trim_scalar() {
  printf '%s' "$1" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//; s/^"//; s/"$//'
}

rendered_env_value() {
  local key="$1"
  local value
  value="$(sed -nE "s/^[[:space:]]*${key}:[[:space:]]*(.*)$/\\1/p" "$RENDERED_FILE" | head -n1 || true)"
  trim_scalar "$value"
}

rendered_json_string() {
  local key="$1"
  sed -nE "s/.*\"${key}\"[[:space:]]*:[[:space:]]*\"([^\"]*)\".*/\\1/p" "$RENDERED_FILE" | head -n1 || true
}

rendered_json_bool() {
  local key="$1"
  sed -nE "s/.*\"${key}\"[[:space:]]*:[[:space:]]*(true|false).*/\\1/p" "$RENDERED_FILE" | head -n1 || true
}

rendered_json_array() {
  local key="$1"
  sed -nE "s/.*\"${key}\"[[:space:]]*:[[:space:]]*(\\[[^]]*\\]).*/\\1/p" "$RENDERED_FILE" | head -n1 || true
}

compact_hosts_value() {
  printf '%s' "$1" | tr -d '[:space:]' | sed -E 's/^\[//; s/\]$//; s/"//g'
}

section() {
  printf '\n== %s ==\n' "$*"
}

find_kubectl() {
  if [ -n "${KUBECTL:-}" ]; then
    printf '%s\n' "$KUBECTL"
    return
  fi
  if command -v kubectl >/dev/null 2>&1; then
    command -v kubectl
    return
  fi
  if [ -x /usr/local/bin/kubectl ]; then
    printf '%s\n' /usr/local/bin/kubectl
    return
  fi
  printf '%s\n' kubectl
}

section "overlay"
if [ -d "$OVERLAY" ]; then
  pass "overlay exists: $OVERLAY"
else
  fail "overlay directory is missing: $OVERLAY"
fi

if [ -f "$OVERLAY/kustomization.yaml" ]; then
  pass "kustomization exists: $OVERLAY/kustomization.yaml"
else
  fail "kustomization is missing: $OVERLAY/kustomization.yaml"
fi

section "kubectl"
KUBECTL_BIN="$(find_kubectl)"
if command -v "$KUBECTL_BIN" >/dev/null 2>&1 || [ -x "$KUBECTL_BIN" ]; then
  pass "kubectl available: $KUBECTL_BIN"
  if "$KUBECTL_BIN" version --client=true >/dev/null 2>&1; then
    pass "kubectl client version is readable"
  else
    fail "kubectl client version failed"
  fi
else
  fail "kubectl is not available; set KUBECTL=/path/to/kubectl or add kubectl to PATH"
fi

CURRENT_CONTEXT=""
if CURRENT_CONTEXT="$("$KUBECTL_BIN" config current-context 2>/dev/null)"; then
  if [ -n "$CURRENT_CONTEXT" ]; then
    pass "kubectl current-context: $CURRENT_CONTEXT"
  else
    fail "kubectl current-context is empty"
  fi
else
  fail "kubectl current-context is not set"
fi

section "kustomize render"
if [ -f "$OVERLAY/kustomization.yaml" ]; then
  if "$KUBECTL_BIN" kustomize "$OVERLAY" >"$RENDERED_FILE"; then
    pass "kubectl kustomize rendered successfully"
  else
    fail "kubectl kustomize failed for overlay: $OVERLAY"
  fi
else
  fail "skip render because kustomization.yaml is missing"
fi

section "placeholder scan"
PLACEHOLDER_PATTERN='REPLACE_ME|CHANGE_ME|CHANGEME|replace-me|registry\.example\.com|v0\.0\.0-replace-me|user:password@mysql'
if [ -d "$OVERLAY" ]; then
  PLACEHOLDER_HITS="$(grep -RInEo "$PLACEHOLDER_PATTERN" "$OVERLAY" 2>/dev/null || true)"
  if [ -n "$PLACEHOLDER_HITS" ]; then
    fail "placeholder values remain in overlay files"
    printf '%s\n' "$PLACEHOLDER_HITS"
  else
    pass "no placeholder tokens found in overlay files"
  fi
else
  warn "overlay directory missing; overlay placeholder scan skipped"
fi

if [ -s "$RENDERED_FILE" ]; then
  RENDERED_PLACEHOLDER_HITS="$(grep -nEo "$PLACEHOLDER_PATTERN" "$RENDERED_FILE" 2>/dev/null || true)"
  if [ -n "$RENDERED_PLACEHOLDER_HITS" ]; then
    fail "placeholder values remain in rendered manifests"
    printf '%s\n' "$RENDERED_PLACEHOLDER_HITS"
  else
    pass "no placeholder tokens found in rendered manifests"
  fi
else
  warn "rendered manifest is empty or unavailable; rendered placeholder scan skipped"
fi

section "image and tag scan"
if [ -s "$RENDERED_FILE" ]; then
  IMAGE_LINES="$(grep -nE '^[[:space:]]*image:' "$RENDERED_FILE" || true)"
  if [ -z "$IMAGE_LINES" ]; then
    fail "no image fields found in rendered manifests"
  else
    pass "image fields found in rendered manifests"
    printf '%s\n' "$IMAGE_LINES"
  fi

  BAD_IMAGE_LINES="$(printf '%s\n' "$IMAGE_LINES" | grep -E 'latest|replace-me|registry\.example\.com|:v0\.0\.0' || true)"
  if [ -n "$BAD_IMAGE_LINES" ]; then
    fail "image tags include latest/replace-me/example placeholders"
    printf '%s\n' "$BAD_IMAGE_LINES"
  else
    pass "rendered image tags do not include latest/replace-me/example placeholders"
  fi

  CONTROL_PLANE_IMAGE_LINES="$(printf '%s\n' "$IMAGE_LINES" | grep -E 'ironagent-(security-service|admin)|go-gateway' || true)"
  if [ -n "$REQUIRED_IMAGE_TAG" ]; then
    MISMATCHED_IMAGE_LINES="$(printf '%s\n' "$CONTROL_PLANE_IMAGE_LINES" | grep -Fv "$REQUIRED_IMAGE_TAG" || true)"
    if [ -n "$MISMATCHED_IMAGE_LINES" ]; then
      fail "control-plane images do not all use REQUIRED_IMAGE_TAG=$REQUIRED_IMAGE_TAG"
      printf '%s\n' "$MISMATCHED_IMAGE_LINES"
    else
      pass "control-plane images use REQUIRED_IMAGE_TAG=$REQUIRED_IMAGE_TAG"
    fi
  else
    warn "REQUIRED_IMAGE_TAG is not set; immutable release tag enforcement skipped"
  fi
else
  warn "rendered manifest is empty or unavailable; image scan skipped"
fi

section "single-entry NodePort scan"
if [ -s "$RENDERED_FILE" ]; then
  NODEPORT_LINES="$(grep -nE '^[[:space:]]*nodePort:' "$RENDERED_FILE" || true)"
  NODEPORT_COUNT="$(printf '%s\n' "$NODEPORT_LINES" | sed '/^$/d' | wc -l | tr -d ' ')"
  if [ "$NODEPORT_COUNT" = "1" ] && printf '%s\n' "$NODEPORT_LINES" | grep -q 'nodePort:[[:space:]]*30808'; then
    pass "exactly one NodePort found and it is 30808"
  else
    fail "expected exactly one NodePort 30808, found $NODEPORT_COUNT"
    if [ -n "$NODEPORT_LINES" ]; then
      printf '%s\n' "$NODEPORT_LINES"
    fi
  fi

  LEGACY_NODEPORT_LINES="$(grep -nE 'nodePort:[[:space:]]*3078[0-9]' "$RENDERED_FILE" || true)"
  if [ -n "$LEGACY_NODEPORT_LINES" ]; then
    fail "rendered manifest still appears to expose a legacy 3078x NodePort"
    printf '%s\n' "$LEGACY_NODEPORT_LINES"
  else
    pass "no legacy 3078x NodePort pattern found"
  fi
else
  warn "rendered manifest is empty or unavailable; NodePort scan skipped"
fi

section "callback config scan"
if [ -s "$RENDERED_FILE" ]; then
  CALLBACK_REQUIRED="$(rendered_env_value GO_GATEWAY_CALLBACK_REQUIRED)"
  if [ -z "$CALLBACK_REQUIRED" ]; then
    CALLBACK_REQUIRED="$(rendered_json_bool callback_required)"
  fi
  if [ -z "$CALLBACK_REQUIRED" ]; then
    CALLBACK_REQUIRED="false"
  fi
  CALLBACK_REQUIRED="$(printf '%s' "$CALLBACK_REQUIRED" | tr '[:upper:]' '[:lower:]')"

  CALLBACK_URL="$(rendered_env_value GO_GATEWAY_DEFAULT_CALLBACK_URL)"
  if [ -z "$CALLBACK_URL" ]; then
    CALLBACK_URL="$(rendered_json_string default_callback_url)"
  fi

  CALLBACK_ALLOWED_HOSTS="$(rendered_env_value GO_GATEWAY_CALLBACK_ALLOWED_HOSTS)"
  if [ -z "$CALLBACK_ALLOWED_HOSTS" ]; then
    CALLBACK_ALLOWED_HOSTS="$(rendered_json_array callback_allowed_hosts)"
  fi
  CALLBACK_ALLOWED_HOSTS_COMPACT="$(compact_hosts_value "$CALLBACK_ALLOWED_HOSTS")"

  CALLBACK_OUTBOX_WORKER_ENABLED="$(rendered_env_value GO_GATEWAY_CALLBACK_OUTBOX_WORKER_ENABLED)"
  if [ -z "$CALLBACK_OUTBOX_WORKER_ENABLED" ]; then
    CALLBACK_OUTBOX_WORKER_ENABLED="$(rendered_json_bool callback_outbox_worker_enabled)"
  fi
  CALLBACK_OUTBOX_WORKER_ENABLED="$(printf '%s' "${CALLBACK_OUTBOX_WORKER_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')"

  if [ "$CALLBACK_REQUIRED" = "true" ] && [ -z "$CALLBACK_URL" ]; then
    fail "callback_required=true but GO_GATEWAY_DEFAULT_CALLBACK_URL/default_callback_url is empty"
  elif [ -n "$CALLBACK_URL" ]; then
    pass "default callback URL configured"
  else
    warn "callback is not required and default callback URL is empty"
  fi

  if [ -n "$CALLBACK_URL" ] && [ -z "$CALLBACK_ALLOWED_HOSTS_COMPACT" ]; then
    fail "callback URL is configured but GO_GATEWAY_CALLBACK_ALLOWED_HOSTS/callback_allowed_hosts is empty"
  elif [ -n "$CALLBACK_ALLOWED_HOSTS_COMPACT" ]; then
    pass "callback allowed hosts configured"
  fi

  if [ "$CALLBACK_REQUIRED" = "true" ] && [ "$CALLBACK_OUTBOX_WORKER_ENABLED" != "true" ]; then
    fail "callback_required=true but callback outbox worker is disabled"
  else
    pass "callback outbox worker is enabled or callback is not required"
  fi
else
  warn "rendered manifest is empty or unavailable; callback config scan skipped"
fi

section "summary"
if [ "$WARN_COUNT" -gt 0 ]; then
  printf 'WARN count: %s\n' "$WARN_COUNT"
fi
if [ "$FAIL_COUNT" -gt 0 ]; then
  printf 'FAIL count: %s\n' "$FAIL_COUNT" >&2
  exit 1
fi

pass "preflight checks passed"
