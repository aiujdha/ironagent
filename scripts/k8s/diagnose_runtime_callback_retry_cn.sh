#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

NS="${NS:-ironagent}"
MYSQL_POD="${MYSQL_POD:-mysql-0}"
GATEWAY_DEPLOY="${GATEWAY_DEPLOY:-deploy/go-gateway}"
ASSISTANT_ID="${ASSISTANT_ID:-wangchen}"
CALLBACK_HOST="${CALLBACK_HOST:-10.191.60.75:8135}"
TASK_ID="${TASK_ID:-}"
RECENT_MINUTES="${RECENT_MINUTES:-120}"
SINCE="${SINCE:-120m}"
OUT_DIR="${OUT_DIR:-}"
ALLOW_MYSQL_ROOT="false"

RUN_ID="$(date +%Y%m%d%H%M%S)-$$"
if [[ -z "$OUT_DIR" ]]; then
  OUT_DIR="/tmp/ironagent-runtime-callback-retry-${RUN_ID}"
fi
RUN_LOG=""
MYSQL_STDERR=""

usage() {
  cat <<'EOF'
用法：
  scripts/k8s/diagnose_runtime_callback_retry_cn.sh --allow-mysql-root [选项]

用途：
  只读核查 runtime 满载/未 ready 与 callback receiver 500 是否同时存在。
  默认核查 assistant_id=wangchen 与 callback_host=10.191.60.75:8135。

选项：
  --namespace NS           K8s namespace，默认 ironagent。
  --mysql-pod POD          MySQL Pod，默认 mysql-0。
  --gateway-deploy REF     Go Gateway workload，默认 deploy/go-gateway。
  --assistant-id ID        目标 assistant_id，默认 wangchen。
  --callback-host HOST     callback_host，默认 10.191.60.75:8135。
  --task-id ID             限定某个 task_id，默认查最近相关记录。
  --recent-minutes N       DB 最近窗口，默认 120。
  --since DURATION         go-gateway 日志窗口，默认 120m。
  --out-dir DIR            输出目录，默认 /tmp/ironagent-runtime-callback-retry-*。
  --allow-mysql-root       允许使用 mysql-0 内 MYSQL_ROOT_PASSWORD 只读 SELECT。
  -h, --help               显示帮助。

安全边界：
  只执行 SELECT 和 kubectl logs，不执行 UPDATE/DELETE/INSERT。

示例：
  KUBECTL='sudo KUBECONFIG=/etc/kubernetes/admin.conf kubectl' \
  bash scripts/k8s/diagnose_runtime_callback_retry_cn.sh \
    --namespace ironagent \
    --assistant-id wangchen \
    --callback-host 10.191.60.75:8135 \
    --allow-mysql-root
EOF
}

log() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*" | tee -a "$RUN_LOG" >&2
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

k() {
  ${KUBECTL:-kubectl} "$@"
}

mysql_table() {
  local name="$1"
  local sql="$2"
  local file="$OUT_DIR/${name}.txt"
  [[ "$ALLOW_MYSQL_ROOT" == "true" ]] || die "必须传 --allow-mysql-root 才会读取 MySQL"
  k -n "$NS" exec -i "$MYSQL_POD" -- sh -lc \
    'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --default-character-set=utf8mb4 -uroot "$MYSQL_DATABASE" --table' \
    <<<"$sql" >"$file" 2>>"$MYSQL_STDERR"
  chmod 600 "$file"
  {
    echo
    echo "===== $name ====="
    cat "$file"
  } | tee -a "$RUN_LOG" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace|-n)
      NS="${2:-}"
      shift 2
      ;;
    --mysql-pod)
      MYSQL_POD="${2:-}"
      shift 2
      ;;
    --gateway-deploy)
      GATEWAY_DEPLOY="${2:-}"
      shift 2
      ;;
    --assistant-id)
      ASSISTANT_ID="${2:-}"
      shift 2
      ;;
    --callback-host)
      CALLBACK_HOST="${2:-}"
      shift 2
      ;;
    --task-id)
      TASK_ID="${2:-}"
      shift 2
      ;;
    --recent-minutes)
      RECENT_MINUTES="${2:-}"
      shift 2
      ;;
    --since)
      SINCE="${2:-}"
      shift 2
      ;;
    --out-dir)
      OUT_DIR="${2:-}"
      shift 2
      ;;
    --allow-mysql-root)
      ALLOW_MYSQL_ROOT="true"
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
[[ -n "$MYSQL_POD" ]] || die "--mysql-pod 不能为空"
[[ -n "$ASSISTANT_ID" ]] || die "--assistant-id 不能为空"
[[ -n "$CALLBACK_HOST" ]] || die "--callback-host 不能为空"
[[ "$RECENT_MINUTES" =~ ^[0-9]+$ ]] || die "--recent-minutes 必须是正整数"

mkdir -p "$OUT_DIR"
chmod 700 "$OUT_DIR"
RUN_LOG="$OUT_DIR/report.txt"
MYSQL_STDERR="$OUT_DIR/mysql.stderr"
: >"$RUN_LOG"
: >"$MYSQL_STDERR"
chmod 600 "$RUN_LOG" "$MYSQL_STDERR"

log "输出目录：$OUT_DIR"
log "assistant_id=$ASSISTANT_ID callback_host=$CALLBACK_HOST task_id=${TASK_ID:-<auto>} recent_minutes=$RECENT_MINUTES"

task_filter_tasks=""
task_filter_queue=""
task_filter_outbox=""
task_filter_delivery=""
if [[ -n "$TASK_ID" ]]; then
  escaped_task_id="${TASK_ID//\'/\'\'}"
  task_filter_tasks=" AND task_id = '${escaped_task_id}'"
  task_filter_queue=" AND q.task_id = '${escaped_task_id}'"
  task_filter_outbox=" AND task_id = '${escaped_task_id}'"
  task_filter_delivery=" AND o.task_id = '${escaped_task_id}'"
fi

mysql_table "00-gateway-queue-indexes" "
SELECT TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME
FROM INFORMATION_SCHEMA.STATISTICS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME IN ('gateway_tasks', 'gateway_task_queue')
  AND INDEX_NAME IN (
    'ix_gateway_tasks_target_assistant_updated',
    'ix_gateway_task_queue_status_task',
    'ix_gateway_task_queue_status',
    'ix_gateway_task_queue_claim'
  )
ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX;
"

mysql_table "01-runtime-instance-${ASSISTANT_ID}" "
SELECT assistant_id, runtime_id, pod_name, status,
       capacity, in_flight, drain_mode,
       last_heartbeat_at, last_error, updated_at
FROM gateway_runtime_instances
WHERE BINARY assistant_id = BINARY '${ASSISTANT_ID//\'/\'\'}'\\G
"

mysql_table "02-runtime-active-leases-${ASSISTANT_ID}" "
SELECT l.lease_id, l.runtime_id, l.assistant_id, l.status,
       l.lease_expires_at, l.created_at, l.updated_at,
       i.status AS runtime_status, i.capacity, i.in_flight, i.last_heartbeat_at
FROM gateway_runtime_leases l
LEFT JOIN gateway_runtime_instances i ON i.runtime_id = l.runtime_id
WHERE BINARY l.assistant_id = BINARY '${ASSISTANT_ID//\'/\'\'}'
  AND l.status = 'active'
ORDER BY l.lease_expires_at ASC
LIMIT 50;
"

mysql_table "03-recent-tasks-${ASSISTANT_ID}" "
SELECT task_id, user_id, request_id, conversation_id,
       target_assistant_id, status, stage,
       failed_reason_code,
       LEFT(failed_reason_text, 300) AS failed_reason_text,
       JSON_UNQUOTE(JSON_EXTRACT(metadata_json, '$.queue_retry_reason_code')) AS queue_retry_reason_code,
       JSON_UNQUOTE(JSON_EXTRACT(metadata_json, '$.queue_error_location')) AS queue_error_location,
       JSON_UNQUOTE(JSON_EXTRACT(metadata_json, '$.queue_capacity_wait_count')) AS capacity_wait_count,
       JSON_UNQUOTE(JSON_EXTRACT(metadata_json, '$.queue_capacity_wait_last_callback_at')) AS capacity_wait_last_callback_at,
       updated_at
FROM gateway_tasks
WHERE BINARY target_assistant_id = BINARY '${ASSISTANT_ID//\'/\'\'}'
  ${task_filter_tasks}
  AND updated_at >= NOW() - INTERVAL ${RECENT_MINUTES} MINUTE
ORDER BY updated_at DESC
LIMIT 30;
"

mysql_table "03b-active-queue-summary-${ASSISTANT_ID}" "
SELECT q.status,
       COUNT(*) AS count,
       MIN(q.updated_at) AS first_updated_at,
       MAX(q.updated_at) AS last_updated_at
FROM gateway_task_queue q
JOIN gateway_tasks t ON t.task_id = q.task_id
WHERE BINARY t.target_assistant_id = BINARY '${ASSISTANT_ID//\'/\'\'}'
  AND q.status IN ('admitting', 'queued', 'processing', 'retry')
  ${task_filter_queue}
GROUP BY q.status
ORDER BY FIELD(q.status, 'admitting', 'queued', 'processing', 'retry');
"

mysql_table "04-task-queue-${ASSISTANT_ID}" "
SELECT q.task_id, q.status, q.attempts, q.next_attempt_at,
       q.locked_by, q.locked_until,
       LEFT(q.last_error, 500) AS last_error,
       q.updated_at
FROM gateway_task_queue q
JOIN gateway_tasks t ON t.task_id = q.task_id
WHERE BINARY t.target_assistant_id = BINARY '${ASSISTANT_ID//\'/\'\'}'
  ${task_filter_queue}
ORDER BY q.updated_at DESC
LIMIT 30;
"

mysql_table "05-callback-outbox-${CALLBACK_HOST//[:\/]/_}" "
SELECT event_id, task_id, event_seq, event_type, status, attempts,
       callback_url,
       LEFT(last_error, 500) AS last_error,
       next_attempt_at, updated_at
FROM gateway_event_outbox
WHERE callback_url LIKE '%${CALLBACK_HOST//\'/\'\'}%'
  ${task_filter_outbox}
  AND updated_at >= NOW() - INTERVAL ${RECENT_MINUTES} MINUTE
ORDER BY updated_at DESC
LIMIT 50;
"

mysql_table "05b-callback-retrying-backlog-summary" "
SELECT
  event_type,
  status,
  LEFT(COALESCE(last_error, ''), 80) AS last_error_prefix,
  COUNT(*) AS count,
  MIN(updated_at) AS first_updated_at,
  MAX(updated_at) AS last_updated_at
FROM gateway_event_outbox
WHERE callback_url LIKE '%${CALLBACK_HOST//\'/\'\'}%'
  AND event_type = 'task.retrying'
  AND updated_at >= NOW() - INTERVAL ${RECENT_MINUTES} MINUTE
  ${task_filter_outbox}
GROUP BY event_type, status, LEFT(COALESCE(last_error, ''), 80)
ORDER BY count DESC, last_updated_at DESC;
"

mysql_table "06-callback-500-exceed-max-call-times" "
SELECT d.event_id, o.task_id, o.event_seq, o.event_type,
       d.attempt, d.status, d.response_status_code,
       LEFT(d.error, 500) AS error,
       LEFT(d.response_body, 800) AS response_body,
       d.created_at
FROM gateway_webhook_deliveries d
LEFT JOIN gateway_event_outbox o ON o.event_id = d.event_id
WHERE d.response_status_code = 500
  AND (d.response_body LIKE '%exceed max call times%' OR d.error LIKE '%exceed max call times%')
  AND d.created_at >= NOW() - INTERVAL ${RECENT_MINUTES} MINUTE
  ${task_filter_delivery}
ORDER BY d.created_at DESC
LIMIT 50;
"

log "抓取 go-gateway 日志：$GATEWAY_DEPLOY since=$SINCE"
LOG_FILE="$OUT_DIR/go-gateway-filtered.log"
{
  k -n "$NS" logs "$GATEWAY_DEPLOY" --since="$SINCE" --all-containers=true 2>&1 || true
} | grep -E "${ASSISTANT_ID}|${CALLBACK_HOST}|task.retrying|runtime_capacity|at_capacity|exceed max call times|callback_response_status_code.?[:=].?500" >"$LOG_FILE" || true
chmod 600 "$LOG_FILE"

{
  echo
  echo "===== go-gateway-filtered.log ====="
  sed -n '1,200p' "$LOG_FILE"
  echo
  echo "完成。完整证据目录：$OUT_DIR"
} | tee -a "$RUN_LOG" >&2
