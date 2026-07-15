#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

NS="${NS:-ironagent}"
IMAGE_TAG="${IMAGE_TAG:-localhost/go-gateway:capacity-wait-rootfix-20260610-r6-485bb604}"
IMAGE_TAR="${IMAGE_TAR:-output/images/go-gateway-capacity-wait-rootfix-20260610-r6-485bb604.tar}"
PER_ASSISTANT_LIMIT="${PER_ASSISTANT_LIMIT:-2}"
CAPACITY_WAIT_MAX_ATTEMPTS="${CAPACITY_WAIT_MAX_ATTEMPTS:-3}"
CAPACITY_WAIT_TIMEOUT="${CAPACITY_WAIT_TIMEOUT:-5m}"
CAPACITY_WAIT_CALLBACK_INTERVAL="${CAPACITY_WAIT_CALLBACK_INTERVAL:-5m}"
ASSISTANT_ID="${ASSISTANT_ID:-wangchen}"
CALLBACK_HOST="${CALLBACK_HOST:-10.191.60.75:8135}"
MYSQL_POD="${MYSQL_POD:-mysql-0}"
KUBECTL="${KUBECTL:-kubectl}"
CTR_CMD="${CTR_CMD:-sudo ctr}"
APPLY="false"
IMPORT_IMAGE="true"
DIAGNOSE="false"
ALLOW_MYSQL_ROOT="false"
APPLY_DB_INDEXES="false"

usage() {
  cat <<'EOF'
用法：
  scripts/k8s/deploy_go_gateway_capacity_rootfix_r6_cn.sh [--apply] [选项]

用途：
  部署 go-gateway r6 根因修复镜像，并用 env 覆盖热修参数。
  默认是 dry-run，只打印将执行的命令；只有传 --apply 才会修改集群。

默认热修参数：
  GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_MAX_ATTEMPTS=3
  GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_TIMEOUT=5m
  GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_CALLBACK_INTERVAL=5m
  GO_GATEWAY_TASK_QUEUE_MAX_PENDING_PER_ASSISTANT=2

选项：
  --apply                         实际执行 kubectl/ctr 命令。
  --namespace NS                  K8s namespace，默认 ironagent。
  --image-tag TAG                 镜像 tag。
  --image-tar PATH                本地 image tar 路径。
  --per-assistant-limit N         单 assistant 活跃队列上限，默认 2。
  --capacity-wait-max-attempts N  runtime capacity 等待次数上限，默认 3。
  --capacity-wait-timeout D       runtime capacity 等待总时长上限，默认 5m。
  --capacity-wait-callback-interval D  retrying callback 最小间隔，默认 5m。
  --no-import                     不导入 image tar，只 set image。
  --apply-db-indexes              使用 MySQL root 幂等创建入闸查询索引。
  --diagnose                      rollout 后运行只读诊断脚本。
  --mysql-pod POD                 MySQL Pod，默认 mysql-0。
  --allow-mysql-root              允许诊断 SELECT；配合 --apply-db-indexes 时允许 DDL 建索引。
  --assistant-id ID               诊断 assistant_id，默认 wangchen。
  --callback-host HOST            诊断 callback_host，默认 10.191.60.75:8135。
  -h, --help                      显示帮助。

示例：
  KUBECTL='sudo KUBECONFIG=/etc/kubernetes/admin.conf kubectl' \
  bash scripts/k8s/deploy_go_gateway_capacity_rootfix_r6_cn.sh --apply --diagnose --allow-mysql-root
EOF
}

q() {
  printf '%q' "$1"
}

log() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*" >&2
}

run_shell() {
  local command="$1"
  printf '+ %s\n' "$command" >&2
  if [[ "$APPLY" == "true" ]]; then
    eval "$command"
  fi
}

apply_db_indexes() {
  local sql
  sql="$(cat <<'SQL'
SET @schema := DATABASE();

SELECT COUNT(*) INTO @table_count
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = @schema AND TABLE_NAME = 'gateway_tasks';
SELECT COUNT(*) INTO @idx_count
FROM INFORMATION_SCHEMA.STATISTICS
WHERE TABLE_SCHEMA = @schema
  AND TABLE_NAME = 'gateway_tasks'
  AND INDEX_NAME = 'ix_gateway_tasks_target_assistant_updated';
SET @ddl := IF(
  @table_count > 0 AND @idx_count = 0,
  'ALTER TABLE gateway_tasks ADD INDEX ix_gateway_tasks_target_assistant_updated (target_assistant_id, updated_at)',
  'SELECT ''ix_gateway_tasks_target_assistant_updated exists or gateway_tasks missing'' AS info'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SELECT COUNT(*) INTO @table_count
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = @schema AND TABLE_NAME = 'gateway_task_queue';
SELECT COUNT(*) INTO @idx_count
FROM INFORMATION_SCHEMA.STATISTICS
WHERE TABLE_SCHEMA = @schema
  AND TABLE_NAME = 'gateway_task_queue'
  AND INDEX_NAME = 'ix_gateway_task_queue_status_task';
SET @ddl := IF(
  @table_count > 0 AND @idx_count = 0,
  'ALTER TABLE gateway_task_queue ADD INDEX ix_gateway_task_queue_status_task (status, task_id)',
  'SELECT ''ix_gateway_task_queue_status_task exists or gateway_task_queue missing'' AS info'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
SQL
)"
  {
    printf '+ %s -n %q exec -i %q -- sh -lc %q <<SQL\n' \
      "$KUBECTL" "$NS" "$MYSQL_POD" \
      'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --default-character-set=utf8mb4 -uroot "$MYSQL_DATABASE"'
    printf '%s\nSQL\n' "$sql"
  } >&2
  if [[ "$APPLY" == "true" ]]; then
    $KUBECTL -n "$NS" exec -i "$MYSQL_POD" -- sh -lc \
      'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --default-character-set=utf8mb4 -uroot "$MYSQL_DATABASE"' <<<"$sql"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)
      APPLY="true"
      shift
      ;;
    --namespace|-n)
      NS="${2:-}"
      shift 2
      ;;
    --image-tag)
      IMAGE_TAG="${2:-}"
      shift 2
      ;;
    --image-tar)
      IMAGE_TAR="${2:-}"
      shift 2
      ;;
    --per-assistant-limit)
      PER_ASSISTANT_LIMIT="${2:-}"
      shift 2
      ;;
    --capacity-wait-max-attempts)
      CAPACITY_WAIT_MAX_ATTEMPTS="${2:-}"
      shift 2
      ;;
    --capacity-wait-timeout)
      CAPACITY_WAIT_TIMEOUT="${2:-}"
      shift 2
      ;;
    --capacity-wait-callback-interval)
      CAPACITY_WAIT_CALLBACK_INTERVAL="${2:-}"
      shift 2
      ;;
    --no-import)
      IMPORT_IMAGE="false"
      shift
      ;;
    --apply-db-indexes)
      APPLY_DB_INDEXES="true"
      shift
      ;;
    --diagnose)
      DIAGNOSE="true"
      shift
      ;;
    --mysql-pod)
      MYSQL_POD="${2:-}"
      shift 2
      ;;
    --allow-mysql-root)
      ALLOW_MYSQL_ROOT="true"
      shift
      ;;
    --assistant-id)
      ASSISTANT_ID="${2:-}"
      shift 2
      ;;
    --callback-host)
      CALLBACK_HOST="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: 未知参数：$1" >&2
      exit 1
      ;;
  esac
done

[[ -n "$NS" ]] || { echo "ERROR: namespace 不能为空" >&2; exit 1; }
[[ -n "$MYSQL_POD" ]] || { echo "ERROR: mysql pod 不能为空" >&2; exit 1; }
[[ "$PER_ASSISTANT_LIMIT" =~ ^[0-9]+$ ]] || { echo "ERROR: --per-assistant-limit 必须是非负整数" >&2; exit 1; }
[[ "$CAPACITY_WAIT_MAX_ATTEMPTS" =~ ^[0-9]+$ ]] || { echo "ERROR: --capacity-wait-max-attempts 必须是非负整数" >&2; exit 1; }
if [[ "$APPLY_DB_INDEXES" == "true" && "$ALLOW_MYSQL_ROOT" != "true" ]]; then
  echo "ERROR: --apply-db-indexes 需要同时传 --allow-mysql-root" >&2
  exit 1
fi

if [[ "$APPLY" != "true" ]]; then
  log "dry-run：只打印命令，不修改集群。需要实际执行请加 --apply。"
fi

if [[ "$IMPORT_IMAGE" == "true" ]]; then
  if [[ -f "$IMAGE_TAR" ]]; then
    run_shell "$CTR_CMD -n k8s.io images import $(q "$IMAGE_TAR")"
  else
    log "未找到 image tar，跳过导入：$IMAGE_TAR"
  fi
fi

run_shell "$KUBECTL -n $(q "$NS") get deploy/go-gateway"

if [[ "$APPLY_DB_INDEXES" == "true" ]]; then
  apply_db_indexes
fi

run_shell "$KUBECTL -n $(q "$NS") set env deploy/go-gateway --overwrite \
GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_MAX_ATTEMPTS=$(q "$CAPACITY_WAIT_MAX_ATTEMPTS") \
GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_TIMEOUT=$(q "$CAPACITY_WAIT_TIMEOUT") \
GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_CALLBACK_INTERVAL=$(q "$CAPACITY_WAIT_CALLBACK_INTERVAL") \
GO_GATEWAY_TASK_QUEUE_MAX_PENDING_PER_ASSISTANT=$(q "$PER_ASSISTANT_LIMIT")"

run_shell "$KUBECTL -n $(q "$NS") set image deploy/go-gateway go-gateway=$(q "$IMAGE_TAG")"
run_shell "$KUBECTL -n $(q "$NS") rollout status deploy/go-gateway --timeout=300s"
run_shell "$KUBECTL -n $(q "$NS") get deploy/go-gateway -o wide"

if [[ "$DIAGNOSE" == "true" ]]; then
  diagnose_args=(
    --namespace "$NS"
    --assistant-id "$ASSISTANT_ID"
    --callback-host "$CALLBACK_HOST"
  )
  if [[ "$ALLOW_MYSQL_ROOT" == "true" ]]; then
    diagnose_args+=(--allow-mysql-root)
  fi
  quoted_args=()
  for arg in "${diagnose_args[@]}"; do
    quoted_args+=("$(q "$arg")")
  done
  run_shell "KUBECTL=$(q "$KUBECTL") bash scripts/k8s/diagnose_runtime_callback_retry_cn.sh ${quoted_args[*]}"
fi
