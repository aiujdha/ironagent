#!/usr/bin/env sh
set -eu

ROOT_DIR="${1:-.}"
KUSTOMIZE_DIR="$ROOT_DIR/deployment/kustomize/ironagent"
SINGLE_ENTRY_DIR="$KUSTOMIZE_DIR/overlays/prod-single-entry-nodeport"
NODEPORT_DIR="$KUSTOMIZE_DIR/overlays/prod-nodeport"
INGRESS_FILE="$KUSTOMIZE_DIR/overlays/prod-ingress/ingress.yaml"

fail() {
  echo "single-entry exposure check failed: $*" >&2
  exit 1
}

test -d "$SINGLE_ENTRY_DIR" || fail "missing $SINGLE_ENTRY_DIR"
test -d "$NODEPORT_DIR" || fail "missing $NODEPORT_DIR"
test -f "$INGRESS_FILE" || fail "missing $INGRESS_FILE"

check_nodeport_overlay() {
  overlay_name="$1"
  overlay_dir="$2"

  if grep -n "service-nodeports-gateway" "$overlay_dir/kustomization.yaml" >/dev/null; then
    fail "$overlay_name must not patch ironagent-gateway as NodePort"
  fi

  nodeport_count="$(grep -R "type: NodePort" "$overlay_dir" | wc -l | tr -d ' ')"
  if [ "$nodeport_count" != "1" ]; then
    fail "$overlay_name must contain exactly one NodePort service, got $nodeport_count"
  fi

  if ! grep -R "nodePort: 30808" "$overlay_dir" >/dev/null; then
    fail "$overlay_name must expose Go Gateway on nodePort 30808"
  fi

  if grep -R "nodePort:" "$overlay_dir" | grep -v "nodePort: 30808" >/dev/null; then
    fail "$overlay_name contains a non-Go-Gateway nodePort"
  fi
}

check_nodeport_overlay "prod-nodeport" "$NODEPORT_DIR"
check_nodeport_overlay "prod-single-entry-nodeport" "$SINGLE_ENTRY_DIR"

if grep -n "name: ironagent-gateway" "$INGRESS_FILE" >/dev/null; then
  fail "prod-ingress must not route public traffic to ironagent-gateway"
fi

if grep -n "number: 18789" "$INGRESS_FILE" >/dev/null; then
  fail "prod-ingress must not expose ironagent-gateway port 18789"
fi

go_gateway_backends="$(grep -n "name: go-gateway" "$INGRESS_FILE" | wc -l | tr -d ' ')"
if [ "$go_gateway_backends" -lt "1" ]; then
  fail "prod-ingress must route public traffic to go-gateway"
fi

echo "single-entry exposure check passed"
