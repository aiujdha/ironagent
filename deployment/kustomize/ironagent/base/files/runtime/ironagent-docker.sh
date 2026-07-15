#!/bin/sh
set -eu

NERDCTL_BIN="/usr/local/bin/nerdctl"
NSENTER_BIN="/usr/bin/nsenter"
SOCKET_PATH="${IRON_AGENT_DOCKER_SOCKET_PATH:-/run/containerd/containerd.sock}"
CONTAINERD_NS="${CONTAINERD_NAMESPACE:-k8s.io}"

if [ ! -x "$NERDCTL_BIN" ]; then
  echo "missing nerdctl binary at $NERDCTL_BIN" >&2
  exit 127
fi

if [ ! -x "$NSENTER_BIN" ]; then
  echo "missing nsenter binary at $NSENTER_BIN" >&2
  exit 127
fi

if [ "$#" -gt 0 ] && [ "$1" = "run" ]; then
  shift
  FILTERED_ARGS=""
  while [ "$#" -gt 0 ]; do
    if [ "$1" != "--init" ]; then
      ESCAPED=$(printf "%s" "$1" | sed "s/'/'\\\\''/g")
      FILTERED_ARGS="$FILTERED_ARGS '$ESCAPED'"
    fi
    shift
  done
  if [ -n "$FILTERED_ARGS" ]; then
    eval "set -- $FILTERED_ARGS"
  else
    set --
  fi

  OLD_IFS=${IFS}
  IFS=,
  for server in ${IRON_AGENT_RUNTIME_DNS_SERVERS:-}; do
    trimmed=$(printf "%s" "$server" | tr -d '[:space:]')
    [ -n "$trimmed" ] || continue
    set -- --dns "$trimmed" "$@"
  done
  IFS=${OLD_IFS}

  exec "$NSENTER_BIN" -t 1 -m -u -i -n -p -- \
    "$NERDCTL_BIN" --address "$SOCKET_PATH" --namespace "$CONTAINERD_NS" run "$@"
fi

exec "$NSENTER_BIN" -t 1 -m -u -i -n -p -- \
  "$NERDCTL_BIN" --address "$SOCKET_PATH" --namespace "$CONTAINERD_NS" "$@"
