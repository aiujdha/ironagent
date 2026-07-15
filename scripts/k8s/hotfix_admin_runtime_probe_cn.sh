#!/usr/bin/env bash
set -euo pipefail

NS="${NS:-ironagent}"
DEPLOY="${DEPLOY:-admin}"
CONTAINER="${CONTAINER:-admin}"
KUBECTL="${KUBECTL:-kubectl}"
HOTFIX_CM="${HOTFIX_CM:-admin-runtime-provisioner-hotfix}"
HOTFIX_VOLUME="${HOTFIX_VOLUME:-admin-runtime-provisioner-hotfix}"
SOURCE_FILE="${SOURCE_FILE:-src/runtime/provisioner.py}"
TARGET_FILE="${TARGET_FILE:-/app/src/runtime/provisioner.py}"
MODE="${1:-apply}"

log() {
  printf '[hotfix-admin-runtime-probe] %s\n' "$*" >&2
}

kubectl_cmd() {
  # KUBECTL intentionally supports values with flags, for example:
  # KUBECTL='sudo kubectl --kubeconfig=/etc/kubernetes/admin.conf'
  ${KUBECTL} "$@"
}

require_source_file() {
  if [[ ! -f "$SOURCE_FILE" ]]; then
    log "找不到源文件: $SOURCE_FILE"
    exit 1
  fi
  if ! grep -q 'return latest_probe' "$SOURCE_FILE"; then
    log "源文件缺少 _wait_for_runtime_probe 超时返回 latest_probe 的修复"
    exit 1
  fi
  if ! grep -q '/api/v1/health' "$SOURCE_FILE" || ! grep -q '/health"' "$SOURCE_FILE"; then
    log "源文件缺少 gateway 健康探测 fallback 修复"
    exit 1
  fi
}

apply_hotfix() {
  require_source_file

  log "检查 Deployment: namespace=$NS deploy=$DEPLOY container=$CONTAINER"
  kubectl_cmd -n "$NS" get deploy "$DEPLOY" >/dev/null

  log "创建/更新 ConfigMap: $HOTFIX_CM"
  kubectl_cmd -n "$NS" create configmap "$HOTFIX_CM" \
    "--from-file=provisioner.py=$SOURCE_FILE" \
    --dry-run=client -o yaml | kubectl_cmd apply -f -

  log "挂载热修文件到 $TARGET_FILE 并触发 admin 滚动重启"
  rollout_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  patch_file="$(mktemp)"
  cat >"$patch_file" <<PATCH
spec:
  template:
    metadata:
      annotations:
        ironagent.hotfix/admin-runtime-provisioner: "$rollout_stamp"
    spec:
      containers:
        - name: $CONTAINER
          volumeMounts:
            - name: $HOTFIX_VOLUME
              mountPath: $TARGET_FILE
              subPath: provisioner.py
              readOnly: true
      volumes:
        - name: $HOTFIX_VOLUME
          configMap:
            name: $HOTFIX_CM
            items:
              - key: provisioner.py
                path: provisioner.py
PATCH
  kubectl_cmd -n "$NS" patch deploy "$DEPLOY" --type=strategic --patch-file "$patch_file"
  rm -f "$patch_file"

  log "等待 rollout 完成"
  kubectl_cmd -n "$NS" rollout status "deploy/$DEPLOY" --timeout=300s

  log "校验 Pod 内热修文件内容"
  kubectl_cmd -n "$NS" exec -i "deploy/$DEPLOY" -c "$CONTAINER" -- python - <<'PY'
from pathlib import Path

path = Path("/app/src/runtime/provisioner.py")
text = path.read_text(encoding="utf-8")
checks = {
    "wait_returns_latest_probe": "return latest_probe" in text,
    "health_fallback_api_v1": '"/api/v1/health"' in text,
    "health_fallback_legacy": '"/health"' in text,
}
missing = [name for name, ok in checks.items() if not ok]
if missing:
    raise SystemExit(f"hotfix file check failed: {missing}")
print(f"hotfix file ok: {path}")
PY

  log "完成。现在可以重试新建助手。"
}

rollback_hotfix() {
  log "移除 admin Deployment 上的热修挂载"
  rollout_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  patch_file="$(mktemp)"
  cat >"$patch_file" <<PATCH
spec:
  template:
    metadata:
      annotations:
        ironagent.hotfix/admin-runtime-provisioner-rollback: "$rollout_stamp"
    spec:
      containers:
        - name: $CONTAINER
          volumeMounts:
            - name: $HOTFIX_VOLUME
              mountPath: $TARGET_FILE
              \$patch: delete
      volumes:
        - name: $HOTFIX_VOLUME
          \$patch: delete
PATCH
  kubectl_cmd -n "$NS" patch deploy "$DEPLOY" --type=strategic --patch-file "$patch_file"
  rm -f "$patch_file"

  log "等待 rollback rollout 完成"
  kubectl_cmd -n "$NS" rollout status "deploy/$DEPLOY" --timeout=300s
  log "如不再需要，可删除 ConfigMap: ${KUBECTL} -n $NS delete configmap $HOTFIX_CM"
}

case "$MODE" in
  apply)
    apply_hotfix
    ;;
  rollback)
    rollback_hotfix
    ;;
  *)
    log "用法: $0 [apply|rollback]"
    exit 2
    ;;
esac
