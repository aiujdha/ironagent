#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

NS="${NS:-ironagent}"
APP_LABEL="${APP_LABEL:-app=go-gateway}"
DEPLOY="${DEPLOY:-go-gateway}"
CONTAINER="${CONTAINER:-go-gateway}"
KUBECTL="${KUBECTL:-}"
PUBLIC_URL="${PUBLIC_URL:-}"
OUT_DIR="${OUT_DIR:-}"
SHOW_SECRET_VALUES="${SHOW_SECRET_VALUES:-false}"
SHOW_REDIS_PASSWORD="${SHOW_REDIS_PASSWORD:-false}"
WORKER_FALLBACK="${WORKER_FALLBACK:-true}"

usage() {
  cat <<'EOF'
用法：
  scripts/k8s/show_go_gateway_live_config_cn.sh [选项]

用途：
  只读查看线上 Go Gateway 的关键运行配置和 /readyz body。
  优先用 kubectl 从控制面读取 Deployment/ConfigMap/Pod；如果当前机器是 worker、
  没有可用 kubectl，则自动用 crictl/nerdctl fallback 到当前运行容器。

常用选项：
  --namespace NS            K8s namespace，默认 ironagent。
  --app-label LABEL         Pod label selector，默认 app=go-gateway。
  --deploy NAME             Deployment 名称，默认 go-gateway。
  --container NAME          容器名，默认 go-gateway。
  --public-url URL          额外 curl 公网 /readyz，例如 http://192.0.2.10:30808。
  --out-dir DIR             保存完整输出文件；默认只打印到 stdout。
  --no-worker-fallback      kubectl 不可用时不尝试 crictl/nerdctl。
  --show-secret-values      打印 Redis username 明文；默认只显示是否存在和长度。
  --show-redis-password     打印 Redis password 明文；默认只显示是否存在和长度。
  -h, --help                显示帮助。

控制面服务器示例：
  KUBECTL='sudo KUBECONFIG=/etc/kubernetes/admin.conf /usr/local/bin/kubectl' \
  bash scripts/k8s/show_go_gateway_live_config_cn.sh --namespace ironagent \
    --public-url http://192.0.2.10:30808

worker 服务器示例：
  bash scripts/k8s/show_go_gateway_live_config_cn.sh --public-url http://192.0.2.10:30808

输出重点：
  - /readyz head/body：直接看 readiness 失败组件名。
  - Pod/ReplicaSet：确认是否新 pod、旧 RS 是否 0。
  - Deployment env/envFrom：确认配置来自 ConfigMap、Secret 还是显式 env。
  - Pod 进程环境：这是 Go Gateway 最终生效值。
  - Secret shape：确认 Redis username/password 是否仍被 secret 注入。
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

log() {
  printf '\n=== %s ===\n' "$*"
}

warn() {
  echo "WARN: $*" >&2
}

have() {
  command -v "$1" >/dev/null 2>&1
}

mask_env() {
  sed -E 's/(PASSWORD|SECRET|TOKEN|DATABASE_URL|HMAC_SECRET|PRESIGN_SECRET|SCOPED_API_KEYS|API_KEYS)=.*/\1=***MASKED***/'
}

selected_go_gateway_env_re='^(GO_GATEWAY_(ADMIN_BASE_URL|SECURITY_BASE_URL|CONTROL_UI_BASE_URL|PUBLIC_BASE_URL|CONFIG_FILE|REDIS_|RUNTIME_|TASK_|CALLBACK_|CHAT_RATE|PROXY_MAX|HTTP_TIMEOUT|MODE|LISTEN_ADDR|PUBLIC_AUTH_DISABLED|ADMIN_AUTH_DISABLED))'

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace|-n)
      NS="${2:-}"
      shift 2
      ;;
    --app-label)
      APP_LABEL="${2:-}"
      shift 2
      ;;
    --deploy)
      DEPLOY="${2:-}"
      shift 2
      ;;
    --container)
      CONTAINER="${2:-}"
      shift 2
      ;;
    --public-url)
      PUBLIC_URL="${2:-}"
      shift 2
      ;;
    --out-dir)
      OUT_DIR="${2:-}"
      shift 2
      ;;
    --show-secret-values)
      SHOW_SECRET_VALUES="true"
      shift
      ;;
    --show-redis-password)
      SHOW_REDIS_PASSWORD="true"
      shift
      ;;
    --no-worker-fallback)
      WORKER_FALLBACK="false"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "未知参数：$1"
      ;;
  esac
done

[[ -n "$NS" ]] || die "--namespace 不能为空"
[[ -n "$APP_LABEL" ]] || die "--app-label 不能为空"
[[ -n "$DEPLOY" ]] || die "--deploy 不能为空"
[[ -n "$CONTAINER" ]] || die "--container 不能为空"

if [[ -n "$OUT_DIR" ]]; then
  mkdir -p "$OUT_DIR"
  chmod 700 "$OUT_DIR"
  exec > >(tee "$OUT_DIR/go-gateway-live-config.txt") 2> >(tee "$OUT_DIR/go-gateway-live-config.stderr" >&2)
fi

resolve_kubectl() {
  if [[ -n "$KUBECTL" ]]; then
    return 0
  fi
  if have kubectl; then
    KUBECTL="kubectl"
    return 0
  fi
  if [[ -x /usr/local/bin/kubectl ]]; then
    KUBECTL="/usr/local/bin/kubectl"
    return 0
  fi
  if [[ -x /tmp/kubectl ]]; then
    KUBECTL="/tmp/kubectl"
    return 0
  fi
  return 1
}

k() {
  # KUBECTL intentionally supports values with flags, for example:
  # KUBECTL='sudo KUBECONFIG=/etc/kubernetes/admin.conf /usr/local/bin/kubectl'
  # shellcheck disable=SC2086
  ${KUBECTL} "$@"
}

kubectl_usable() {
  resolve_kubectl || return 1
  k version --client=true >/dev/null 2>&1 || return 1
  k get namespace "$NS" >/dev/null 2>&1 || return 1
  return 0
}

print_public_readyz() {
  [[ -n "$PUBLIC_URL" ]] || return 0
  log "public /readyz: $PUBLIC_URL"
  curl -sS -i --max-time 10 "${PUBLIC_URL%/}/readyz" || true
}

print_secret_shape() {
  local secret_name="$1"
  local user_b64 pass_b64 user pass

  user_b64="$(k -n "$NS" get secret "$secret_name" -o jsonpath='{.data.REDIS_USERNAME}' 2>/dev/null || true)"
  pass_b64="$(k -n "$NS" get secret "$secret_name" -o jsonpath='{.data.REDIS_PASSWORD}' 2>/dev/null || true)"

  if [[ -n "$user_b64" ]]; then
    user="$(printf '%s' "$user_b64" | base64 -d 2>/dev/null || true)"
  else
    user=""
  fi
  if [[ -n "$pass_b64" ]]; then
    pass="$(printf '%s' "$pass_b64" | base64 -d 2>/dev/null || true)"
  else
    pass=""
  fi

  if [[ "$SHOW_SECRET_VALUES" == "true" ]]; then
    printf 'REDIS_USERNAME_present=%s value=%s length=%s\n' "$([[ -n "$user" ]] && echo yes || echo no)" "$user" "${#user}"
  else
    printf 'REDIS_USERNAME_present=%s length=%s\n' "$([[ -n "$user" ]] && echo yes || echo no)" "${#user}"
  fi
  if [[ "$SHOW_REDIS_PASSWORD" == "true" ]]; then
    printf 'REDIS_PASSWORD_present=%s value=%s length=%s\n' "$([[ -n "$pass" ]] && echo yes || echo no)" "$pass" "${#pass}"
  else
    printf 'REDIS_PASSWORD_present=%s length=%s\n' "$([[ -n "$pass" ]] && echo yes || echo no)" "${#pass}"
  fi
}

kubectl_mode() {
  local pod
  pod="$(k -n "$NS" get pod -l "$APP_LABEL" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  [[ -n "$pod" ]] || die "没有找到 Pod：namespace=$NS selector=$APP_LABEL"

  log "context"
  date '+local_time=%F %T %Z'
  hostname 2>/dev/null || true
  echo "kubectl=$KUBECTL"
  k config current-context 2>/dev/null || true

  log "pod wide"
  k -n "$NS" get pod -l "$APP_LABEL" -o wide

  log "deployment rollout"
  k -n "$NS" describe deploy "$DEPLOY" | sed -n '/OldReplicaSets/,$p' || true
  echo
  k -n "$NS" get rs -l "$APP_LABEL" -o wide || true

  log "pod image/status"
  k -n "$NS" get pod "$pod" -o json | jq -r --arg container "$CONTAINER" '
    .metadata.name,
    (.metadata.labels // {}),
    (.metadata.annotations // {}),
    (.spec.containers[] | select(.name == $container) | {image, envFrom, env, volumeMounts}),
    (.status.containerStatuses[] | select(.name == $container) | {image, imageID, containerID, ready, restartCount, started})
  ' 2>/dev/null || k -n "$NS" get pod "$pod" -o yaml

  log "readyz from pod"
  k -n "$NS" exec "$pod" -- sh -lc '
    rm -f /tmp/ready-body /tmp/ready-head
    wget -S -O /tmp/ready-body http://127.0.0.1:8080/readyz 2>/tmp/ready-head || true
    echo "=== ready head ==="
    cat /tmp/ready-head
    echo "=== ready body ==="
    cat /tmp/ready-body
    echo
  '

  log "process env selected from pod"
  k -n "$NS" exec "$pod" -- sh -lc "printenv | sort | grep -E '$selected_go_gateway_env_re' || true" | mask_env

  log "go-gateway-config selected"
  if k -n "$NS" get cm go-gateway-config >/dev/null 2>&1; then
    if have jq; then
      k -n "$NS" get cm go-gateway-config -o json | jq -r '
        .data
        | to_entries
        | sort_by(.key)[]
        | select(.key | test("^(GO_GATEWAY_(ADMIN_BASE_URL|SECURITY_BASE_URL|CONTROL_UI_BASE_URL|PUBLIC_BASE_URL|CONFIG_FILE|REDIS_|RUNTIME_|TASK_|CALLBACK_|CHAT_RATE|PROXY_MAX|HTTP_TIMEOUT|MODE|LISTEN_ADDR|PUBLIC_AUTH_DISABLED|ADMIN_AUTH_DISABLED))"))
        | "\(.key)=\(.value)"
      ' | mask_env
    else
      k -n "$NS" get cm go-gateway-config -o yaml | grep -E 'GO_GATEWAY_' | mask_env || true
    fi
  else
    warn "ConfigMap go-gateway-config 不存在"
  fi

  log "go-gateway file config selected"
  if k -n "$NS" get cm go-gateway-file-config >/dev/null 2>&1; then
    if have jq; then
      k -n "$NS" get cm go-gateway-file-config -o jsonpath='{.data.go-gateway\.json}' | jq -r '
        to_entries
        | sort_by(.key)[]
        | select(.key | test("^(listen_addr|mode|security_base_url|admin_base_url|http_timeout|redis_|public_base_url|default_callback_url|callback_|runtime_|task_|proxy_|chat_rate|max_upload|log_level)"))
        | "\(.key)=\(.value | tostring)"
      ' | mask_env
    else
      k -n "$NS" get cm go-gateway-file-config -o jsonpath='{.data.go-gateway\.json}' || true
      echo
    fi
  else
    warn "ConfigMap go-gateway-file-config 不存在"
  fi

  log "redis secret shape"
  print_secret_shape control-plane-shared-secret

  log "service/endpoints"
  k -n "$NS" get svc admin security-service ironagent-callback-receiver redis-sentinel redis-ha-headless go-gateway -o wide 2>/dev/null || true
  echo
  k -n "$NS" get endpoints admin security-service ironagent-callback-receiver redis-sentinel go-gateway -o wide 2>/dev/null || true

  print_public_readyz
}

find_worker_container_id() {
  local cid
  if have crictl; then
    cid="$(sudo crictl ps -a 2>/dev/null | awk -v name="$CONTAINER" '$0 ~ name {print $1; exit}')"
    [[ -n "$cid" ]] && {
      echo "$cid"
      return 0
    }
  fi
  if have nerdctl; then
    cid="$(sudo nerdctl -n k8s.io ps 2>/dev/null | awk -v name="$CONTAINER" '$0 ~ name {print $1; exit}')"
    [[ -n "$cid" ]] && {
      echo "$cid"
      return 0
    }
  fi
  return 1
}

worker_exec() {
  local cid="$1"
  shift
  if have crictl; then
    sudo crictl exec "$cid" "$@"
  else
    sudo nerdctl -n k8s.io exec "$cid" "$@"
  fi
}

worker_mode() {
  local cid="$1"

  log "context"
  date '+local_time=%F %T %Z'
  hostname 2>/dev/null || true
  echo "mode=worker-fallback"
  echo "container_id=$cid"

  log "worker pod/container list"
  if have crictl; then
    sudo crictl pods 2>/dev/null | grep -E "POD|$DEPLOY|$CONTAINER" || true
    echo
    sudo crictl ps -a 2>/dev/null | grep -E "CONTAINER|$DEPLOY|$CONTAINER" || true
  fi
  if have nerdctl; then
    echo
    sudo nerdctl -n k8s.io ps 2>/dev/null | grep -E "CONTAINER|$DEPLOY|$CONTAINER" || true
  fi

  log "readyz from running container"
  worker_exec "$cid" sh -lc '
    rm -f /tmp/ready-body /tmp/ready-head
    wget -S -O /tmp/ready-body http://127.0.0.1:8080/readyz 2>/tmp/ready-head || true
    echo "=== ready head ==="
    cat /tmp/ready-head
    echo "=== ready body ==="
    cat /tmp/ready-body
    echo
  '

  log "process env selected from running container"
  worker_exec "$cid" sh -lc "tr '\0' '\n' </proc/1/environ | sort | grep -E '$selected_go_gateway_env_re' || true" | mask_env

  log "mounted /etc/ironagent/go-gateway.json selected"
  if worker_exec "$cid" sh -lc 'test -r /etc/ironagent/go-gateway.json' >/dev/null 2>&1; then
    if have jq; then
      worker_exec "$cid" sh -lc 'cat /etc/ironagent/go-gateway.json' | jq -r '
        to_entries
        | sort_by(.key)[]
        | select(.key | test("^(listen_addr|mode|security_base_url|admin_base_url|http_timeout|redis_|public_base_url|default_callback_url|callback_|runtime_|task_|proxy_|chat_rate|max_upload|log_level)"))
        | "\(.key)=\(.value | tostring)"
      ' | mask_env
    else
      worker_exec "$cid" sh -lc 'cat /etc/ironagent/go-gateway.json' | mask_env
    fi
  else
    warn "容器内没有可读 /etc/ironagent/go-gateway.json"
  fi

  log "worker image truth"
  if have crictl; then
    sudo crictl inspect "$cid" 2>/dev/null | jq -r '
      .status.image.image,
      .status.imageRef,
      .status.createdAt,
      .status.startedAt,
      (.info.runtimeSpec.process.args // [])
    ' 2>/dev/null || sudo crictl inspect "$cid" 2>/dev/null || true
  fi
  if have nerdctl; then
    sudo nerdctl -n k8s.io images 2>/dev/null | grep -E "REPOSITORY|go-gateway" || true
  fi

  print_public_readyz
}

main() {
  if kubectl_usable; then
    kubectl_mode
    return
  fi

  warn "kubectl 不可用或无法访问 namespace=$NS，尝试 worker fallback"
  if [[ "$WORKER_FALLBACK" != "true" ]]; then
    die "kubectl 不可用，且已禁用 worker fallback"
  fi

  local cid
  cid="$(find_worker_container_id || true)"
  [[ -n "$cid" ]] || die "没有找到运行中的 $CONTAINER 容器；请在控制面设置 KUBECTL，或在运行 go-gateway 的 worker 上执行"
  worker_mode "$cid"
}

main "$@"
