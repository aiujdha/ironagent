#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

NS="${NS:-ironagent}"
JOB_NAME="${JOB_NAME:-ironagent-db-migrate-022}"
IMAGE="${IMAGE:-}"
KUBECTL="${KUBECTL:-kubectl}"
CONFIGMAP="${CONFIGMAP:-control-plane-shared-config}"
SECRET="${SECRET:-control-plane-shared-secret}"
IMAGE_PULL_POLICY="${IMAGE_PULL_POLICY:-IfNotPresent}"
DELETE_EXISTING="${DELETE_EXISTING:-true}"
TIMEOUT="${TIMEOUT:-300s}"

usage() {
  cat <<'EOF'
用法：
  IMAGE=localhost/ironagent-security-service:<new-tag> \
  KUBECTL='sudo KUBECONFIG=/etc/kubernetes/admin.conf kubectl' \
  bash scripts/k8s/run_security_alembic_job_cn.sh

用途：
  用新 admin/security 镜像在 K8s 内手动跑一次 Alembic：
    .venv/bin/alembic -c migrations/alembic.ini upgrade head

可配置环境变量：
  NS                 namespace，默认 ironagent
  JOB_NAME           Job 名称，默认 ironagent-db-migrate-022
  IMAGE              新 security-service/admin 镜像，必须显式传入，不允许 latest
  CONFIGMAP          envFrom ConfigMap，默认 control-plane-shared-config
  SECRET             envFrom Secret，默认 control-plane-shared-secret
  IMAGE_PULL_POLICY  默认 IfNotPresent；跨节点建议配合不可变 tag
  DELETE_EXISTING    true 时先删除同名旧 Job，默认 true
  TIMEOUT            等待 Job 完成超时，默认 300s
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ -z "${IMAGE}" ]]; then
  echo "错误：必须显式设置 IMAGE=...，并确保它是本次新镜像 tag 或 digest。" >&2
  usage >&2
  exit 2
fi

if [[ "${IMAGE}" == "latest" || "${IMAGE}" == *":latest" ]]; then
  echo "错误：拒绝使用 latest 跑数据库迁移；请传入不可变 tag 或 digest。" >&2
  exit 2
fi

k() {
  # KUBECTL intentionally supports values with flags, for example:
  # KUBECTL='sudo KUBECONFIG=/etc/kubernetes/admin.conf /usr/local/bin/kubectl'
  # shellcheck disable=SC2086
  ${KUBECTL} "$@"
}

if [[ "${DELETE_EXISTING}" == "true" ]]; then
  k -n "${NS}" delete job "${JOB_NAME}" --ignore-not-found=true >/dev/null
fi

tmp_yaml="$(mktemp)"
trap 'rm -f "${tmp_yaml}"' EXIT

cat >"${tmp_yaml}" <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: ${IMAGE}
          imagePullPolicy: ${IMAGE_PULL_POLICY}
          envFrom:
            - configMapRef:
                name: ${CONFIGMAP}
            - secretRef:
                name: ${SECRET}
          command:
            - sh
            - -lc
            - |
              set -Eeuo pipefail
              cd /app
              .venv/bin/alembic -c migrations/alembic.ini upgrade head
EOF

k -n "${NS}" apply -f "${tmp_yaml}"
if ! k -n "${NS}" wait --for=condition=complete "job/${JOB_NAME}" --timeout="${TIMEOUT}"; then
  status=$?
  echo "Alembic Job 未完成，输出 Job 状态和日志：" >&2
  k -n "${NS}" describe "job/${JOB_NAME}" >&2 || true
  k -n "${NS}" logs "job/${JOB_NAME}" --tail=-1 >&2 || true
  exit "${status}"
fi
k -n "${NS}" logs "job/${JOB_NAME}" --tail=-1
