#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

NS="${NS:-ironagent}"
MYSQL_POD="${MYSQL_POD:-mysql-0}"
GATEWAY_DEPLOY="${GATEWAY_DEPLOY:-deploy/go-gateway}"
OUT_DIR="${OUT_DIR:-}"
RECENT_MINUTES="${RECENT_MINUTES:-120}"
HEARTBEAT_TTL_SECONDS="${HEARTBEAT_TTL_SECONDS:-60}"
ALLOW_MYSQL_ROOT="false"
STRICT="false"

RUN_ID="$(date +%Y%m%d%H%M%S)-$$"
if [[ -z "$OUT_DIR" ]]; then
  OUT_DIR="/tmp/ironagent-go-gateway-parallel-safety-${RUN_ID}"
fi
RUN_LOG=""
SUMMARY_FILE=""
MYSQL_STDERR=""
FAIL_COUNT=0
WARN_COUNT=0
SKIP_COUNT=0

usage() {
  cat <<'EOF'
用法：
  scripts/k8s/verify_go_gateway_parallel_safety_cn.sh --namespace ironagent --allow-mysql-root

用途：
  只读检查 Go Gateway 并行 runtime 改造上线前后的关键安全面：
  - 新迁移表/列/索引是否存在。
  - gateway_runtime_instances / gateway_runtime_leases 是否有过载或 lease 泄漏。
  - 不同助手 capacity=1 时是否具备独立实例槽位。
  - workspace_files 是否存在 active 路径重复、active_path_key 缺失。
  - 结果文件是否疑似投递到错误助手或错误用户。
  - 最近是否有大量 scheduler at_capacity，辅助判断 ClaimLimit/worker/capacity 配置。

安全边界：
  默认不读 MySQL。必须显式传 --allow-mysql-root 才会用 mysql-0 内的
  MYSQL_ROOT_PASSWORD 做只读 SELECT。脚本不执行 UPDATE/DELETE/INSERT。

选项：
  --namespace NS              K8s namespace，默认 ironagent。
  --mysql-pod POD             MySQL Pod，默认 mysql-0。
  --gateway-deploy REF        Go Gateway workload，默认 deploy/go-gateway。
  --recent-minutes N          最近任务/结果检查窗口，默认 120。
  --heartbeat-ttl-seconds N   runtime 心跳可用窗口，默认 60。
  --out-dir DIR               保存证据目录，默认 /tmp/ironagent-go-gateway-parallel-safety-*。
  --allow-mysql-root          允许读取 mysql-0 中 MYSQL_ROOT_PASSWORD 做只读 SELECT。
  --strict                    warning 也作为失败退出。
  -h, --help                  显示帮助。

通过标准：
  fail_count=0 表示结构和强一致约束通过。
  warn_count>0 需要人工看 summary；常见 warning 包括当前没有并发任务、近期 at_capacity 较多。
EOF
}

init_output() {
  mkdir -p "$OUT_DIR"
  chmod 700 "$OUT_DIR"
  RUN_LOG="$OUT_DIR/run.log"
  SUMMARY_FILE="$OUT_DIR/summary.txt"
  MYSQL_STDERR="$OUT_DIR/mysql.stderr"
  : >"$RUN_LOG"
  : >"$SUMMARY_FILE"
  : >"$MYSQL_STDERR"
  chmod 600 "$RUN_LOG" "$SUMMARY_FILE" "$MYSQL_STDERR"
}

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$RUN_LOG" >&2
}

section() {
  log ""
  log "===== $* ====="
}

pass_case() {
  log "通过：$*"
}

warn_case() {
  WARN_COUNT=$((WARN_COUNT + 1))
  log "警告：$*"
}

fail_case() {
  FAIL_COUNT=$((FAIL_COUNT + 1))
  log "失败：$*"
}

skip_case() {
  SKIP_COUNT=$((SKIP_COUNT + 1))
  log "跳过：$*"
}

finalize() {
  {
    echo "run_id=$RUN_ID"
    echo "namespace=$NS"
    echo "mysql_pod=$MYSQL_POD"
    echo "gateway_deploy=$GATEWAY_DEPLOY"
    echo "recent_minutes=$RECENT_MINUTES"
    echo "heartbeat_ttl_seconds=$HEARTBEAT_TTL_SECONDS"
    echo "allow_mysql_root=$ALLOW_MYSQL_ROOT"
    echo "strict=$STRICT"
    echo "fail_count=$FAIL_COUNT"
    echo "warn_count=$WARN_COUNT"
    echo "skip_count=$SKIP_COUNT"
    echo "artifacts=$OUT_DIR"
  } >"$SUMMARY_FILE"
  chmod 600 "$SUMMARY_FILE"
  log "汇总文件：$SUMMARY_FILE"
  log "证据目录：$OUT_DIR"
}

on_err() {
  local line="$1"
  local cmd="$2"
  local code="$3"
  fail_case "脚本异常退出：line=$line exit=$code cmd=$cmd"
  finalize
  exit "$code"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    fail_case "缺少命令：$1"
    finalize
    exit 1
  }
}

require_positive_int() {
  local name="$1"
  local value="$2"
  if ! [[ "$value" =~ ^[0-9]+$ ]] || [[ "$value" -le 0 ]]; then
    fail_case "$name 必须是正整数，当前值：$value"
    finalize
    exit 1
  fi
}

require_arg() {
  local name="$1"
  local value="${2:-}"
  if [[ -z "$value" || "$value" == --* ]]; then
    echo "参数 $name 缺少值" >&2
    usage >&2
    exit 2
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace|-n)
      require_arg "$1" "${2:-}"
      NS="$2"
      shift 2
      ;;
    --mysql-pod)
      require_arg "$1" "${2:-}"
      MYSQL_POD="$2"
      shift 2
      ;;
    --gateway-deploy)
      require_arg "$1" "${2:-}"
      GATEWAY_DEPLOY="$2"
      shift 2
      ;;
    --recent-minutes)
      require_arg "$1" "${2:-}"
      RECENT_MINUTES="$2"
      shift 2
      ;;
    --heartbeat-ttl-seconds)
      require_arg "$1" "${2:-}"
      HEARTBEAT_TTL_SECONDS="$2"
      shift 2
      ;;
    --out-dir)
      require_arg "$1" "${2:-}"
      OUT_DIR="$2"
      shift 2
      ;;
    --allow-mysql-root)
      ALLOW_MYSQL_ROOT="true"
      shift
      ;;
    --strict)
      STRICT="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "未知参数：$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

init_output
trap 'on_err "$LINENO" "$BASH_COMMAND" "$?"' ERR

require_cmd kubectl
require_positive_int "RECENT_MINUTES" "$RECENT_MINUTES"
require_positive_int "HEARTBEAT_TTL_SECONDS" "$HEARTBEAT_TTL_SECONDS"

mysql_raw() {
  local sql="$1"
  if [[ "$ALLOW_MYSQL_ROOT" != "true" ]]; then
    return 78
  fi
  kubectl -n "$NS" exec -i "$MYSQL_POD" -- sh -lc \
    'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --default-character-set=utf8mb4 -uroot "$MYSQL_DATABASE" --batch --raw --skip-column-names' \
    <<<"$sql" 2>>"$MYSQL_STDERR"
}

mysql_table() {
  local name="$1"
  local sql="$2"
  local file="$OUT_DIR/${name}.txt"
  if [[ "$ALLOW_MYSQL_ROOT" != "true" ]]; then
    skip_case "未传 --allow-mysql-root，跳过 DB 查询：$name"
    return 78
  fi
  kubectl -n "$NS" exec -i "$MYSQL_POD" -- sh -lc \
    'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --default-character-set=utf8mb4 -uroot "$MYSQL_DATABASE" --table' \
    <<<"$sql" >"$file" 2>>"$MYSQL_STDERR"
  chmod 600 "$file"
  sed 's/^/  /' "$file" | tee -a "$RUN_LOG" >&2
}

scalar() {
  local sql="$1"
  local value
  value="$(mysql_raw "$sql" | tail -n 1 | tr -d '\r' || true)"
  printf '%s' "${value:-}"
}

check_zero() {
  local label="$1"
  local sql="$2"
  local value
  value="$(scalar "$sql")"
  if [[ "$value" == "0" ]]; then
    pass_case "$label = 0"
  else
    fail_case "$label = ${value:-UNKNOWN}"
  fi
}

check_one() {
  local label="$1"
  local sql="$2"
  local value
  value="$(scalar "$sql")"
  if [[ "$value" == "1" ]]; then
    pass_case "$label 存在"
  else
    fail_case "$label 不存在或数量异常：${value:-UNKNOWN}"
  fi
}

check_mysql_version() {
  section "MySQL 版本检查"
  local version
  version="$(scalar "SELECT VERSION();")"
  if [[ "$version" =~ ^8\. ]]; then
    pass_case "MySQL version=$version，支持 runtime 选槽所需的 SKIP LOCKED"
  else
    fail_case "MySQL version=${version:-UNKNOWN}；本改造要求 MySQL 8.x，避免 SKIP LOCKED 不可用导致调度失败"
  fi
}

print_gateway_env() {
  section "Go Gateway 当前运行环境"
  if kubectl -n "$NS" get "$GATEWAY_DEPLOY" >/dev/null 2>&1; then
    kubectl -n "$NS" exec "$GATEWAY_DEPLOY" -- sh -lc \
      'tr "\0" "\n" </proc/1/environ | egrep "^(GO_GATEWAY_TASK_WORKER_ENABLED|GO_GATEWAY_TASK_WORKER_CLAIM_LIMIT|GO_GATEWAY_TASK_WORKER_CONCURRENCY|GO_GATEWAY_RUNTIME_DEFAULT_CAPACITY|GO_GATEWAY_PROCESS_ROLE|GO_GATEWAY_DATABASE_MAX_OPEN_CONNS|GO_GATEWAY_CALLBACK_DATABASE_MAX_OPEN_CONNS)=" | sort' \
      >"$OUT_DIR/go-gateway-env.txt" 2>/dev/null || true
    if [[ -s "$OUT_DIR/go-gateway-env.txt" ]]; then
      sed 's/^/  /' "$OUT_DIR/go-gateway-env.txt" | tee -a "$RUN_LOG" >&2
      local enabled concurrency claim_limit runtime_capacity
      enabled="$(grep '^GO_GATEWAY_TASK_WORKER_ENABLED=' "$OUT_DIR/go-gateway-env.txt" | tail -n 1 | cut -d= -f2- || true)"
      concurrency="$(grep '^GO_GATEWAY_TASK_WORKER_CONCURRENCY=' "$OUT_DIR/go-gateway-env.txt" | tail -n 1 | cut -d= -f2- || true)"
      claim_limit="$(grep '^GO_GATEWAY_TASK_WORKER_CLAIM_LIMIT=' "$OUT_DIR/go-gateway-env.txt" | tail -n 1 | cut -d= -f2- || true)"
      runtime_capacity="$(grep '^GO_GATEWAY_RUNTIME_DEFAULT_CAPACITY=' "$OUT_DIR/go-gateway-env.txt" | tail -n 1 | cut -d= -f2- || true)"
      if [[ "$enabled" == "true" ]]; then
        if ! [[ "${concurrency:-}" =~ ^[0-9]+$ ]] || [[ "${concurrency:-0}" -le 1 ]]; then
          fail_case "GO_GATEWAY_TASK_WORKER_CONCURRENCY=${concurrency:-缺失}；这会让队列 worker 全局串行"
        else
          pass_case "GO_GATEWAY_TASK_WORKER_CONCURRENCY=$concurrency，可跨助手并行处理"
        fi
        if [[ "${claim_limit:-}" == "1" ]]; then
          pass_case "GO_GATEWAY_TASK_WORKER_CLAIM_LIMIT=1，避免单个 worker 预取过多任务"
        elif [[ -n "${claim_limit:-}" ]]; then
          warn_case "GO_GATEWAY_TASK_WORKER_CLAIM_LIMIT=$claim_limit；上线初期建议保持 1"
        fi
        if [[ "${runtime_capacity:-}" == "1" ]]; then
          pass_case "GO_GATEWAY_RUNTIME_DEFAULT_CAPACITY=1，同一助手单槽串行"
        fi
      else
        warn_case "GO_GATEWAY_TASK_WORKER_ENABLED=${enabled:-缺失}；当前实例不执行队列任务"
      fi
    else
      warn_case "未读取到 Go Gateway 进程环境，可能容器无 sh/tr/egrep 或权限受限"
    fi
  else
    warn_case "未找到 $GATEWAY_DEPLOY，跳过进程环境检查"
  fi
}

check_schema() {
  section "迁移结构检查"
  check_one "gateway_runtime_instances 表" \
    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = 'gateway_runtime_instances';"
  check_one "gateway_runtime_leases 表" \
    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = 'gateway_runtime_leases';"
  check_one "gateway_conversation_routes 表" \
    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = 'gateway_conversation_routes';"
  check_one "workspace_files.active_path_key 列" \
    "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'workspace_files' AND column_name = 'active_path_key';"
  check_one "workspace_files active path 唯一索引" \
    "SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema = DATABASE() AND table_name = 'workspace_files' AND index_name = 'uq_workspace_files_active_path';"
}

check_runtime_state() {
  section "Runtime 实例和 lease 检查"
  mysql_table "runtime-instances-summary" "
SELECT
  assistant_id,
  COUNT(*) AS instances,
  SUM(capacity) AS capacity_sum,
  SUM(in_flight) AS in_flight_sum,
  SUM(CASE WHEN LOWER(status) IN ('ready', 'running', 'healthy')
            AND drain_mode = 0
            AND (last_heartbeat_at IS NULL OR last_heartbeat_at >= DATE_SUB(NOW(), INTERVAL ${HEARTBEAT_TTL_SECONDS} SECOND))
            AND (COALESCE(internal_base_url, '') <> '' OR COALESCE(gateway_url, '') <> '')
           THEN 1 ELSE 0 END) AS available_instances,
  MAX(last_heartbeat_at) AS newest_heartbeat
FROM gateway_runtime_instances
GROUP BY assistant_id
ORDER BY assistant_id;
"
  check_zero "runtime instance in_flight 超过 capacity" "
SELECT COUNT(*) FROM gateway_runtime_instances
WHERE COALESCE(in_flight, 0) > COALESCE(capacity, 0);
"
  check_zero "active runtime lease 已过期但未释放" "
SELECT COUNT(*) FROM gateway_runtime_leases
WHERE status = 'active' AND lease_expires_at < NOW();
"
  local expired_recent
  expired_recent="$(scalar "
SELECT COUNT(*)
FROM gateway_runtime_leases
WHERE status = 'expired'
  AND updated_at >= DATE_SUB(NOW(), INTERVAL ${RECENT_MINUTES} MINUTE);
  ")"
  if [[ "${expired_recent:-0}" == "0" ]]; then
    pass_case "最近 ${RECENT_MINUTES} 分钟没有 runtime lease 过期回收"
  else
    warn_case "最近 ${RECENT_MINUTES} 分钟 runtime lease 过期回收=${expired_recent}；槽位已回收，但要排查 worker/runtime 是否异常退出"
  fi
  check_zero "active runtime lease 找不到 runtime instance" "
SELECT COUNT(*)
FROM gateway_runtime_leases l
LEFT JOIN gateway_runtime_instances i ON i.runtime_id = l.runtime_id
WHERE l.status = 'active' AND i.runtime_id IS NULL;
"
  check_zero "runtime instance in_flight 与 active lease 数不一致" "
SELECT COUNT(*)
FROM (
  SELECT
    i.runtime_id,
    i.in_flight,
    COALESCE(l.active_lease_count, 0) AS active_lease_count
  FROM gateway_runtime_instances i
  LEFT JOIN (
    SELECT runtime_id, COUNT(*) AS active_lease_count
    FROM gateway_runtime_leases
    WHERE status = 'active' AND lease_expires_at >= NOW()
    GROUP BY runtime_id
  ) l ON l.runtime_id = i.runtime_id
  WHERE COALESCE(i.in_flight, 0) <> COALESCE(l.active_lease_count, 0)
) mismatch;
"
  check_zero "gateway_runtimes 聚合 capacity/in_flight 与实例表不一致" "
SELECT COUNT(*)
FROM (
  SELECT
    assistant_id,
    SUM(CASE
      WHEN drain_mode = 0
       AND LOWER(status) IN ('ready', 'running', 'healthy')
       AND (COALESCE(internal_base_url, '') <> '' OR COALESCE(gateway_url, '') <> '')
      THEN 1 ELSE 0 END) AS available_count,
    SUM(CASE
      WHEN drain_mode = 0
       AND LOWER(status) IN ('ready', 'running', 'healthy')
       AND (COALESCE(internal_base_url, '') <> '' OR COALESCE(gateway_url, '') <> '')
      THEN capacity ELSE 0 END) AS available_capacity,
    SUM(CASE
      WHEN drain_mode = 0
       AND LOWER(status) IN ('ready', 'running', 'healthy')
       AND (COALESCE(internal_base_url, '') <> '' OR COALESCE(gateway_url, '') <> '')
      THEN in_flight ELSE 0 END) AS available_in_flight,
    SUM(capacity) AS total_capacity,
    SUM(in_flight) AS total_in_flight
  FROM gateway_runtime_instances
  GROUP BY assistant_id
) i
JOIN gateway_runtimes r ON r.assistant_id = i.assistant_id
WHERE r.capacity <> CASE WHEN i.available_count > 0 THEN i.available_capacity ELSE i.total_capacity END
   OR r.in_flight <> CASE WHEN i.available_count > 0 THEN i.available_in_flight ELSE i.total_in_flight END;
"
  local parallel_candidates
  parallel_candidates="$(scalar "
SELECT COUNT(*)
FROM (
	  SELECT assistant_id
	  FROM gateway_runtime_instances
	  WHERE LOWER(status) IN ('ready', 'running', 'healthy')
	    AND drain_mode = 0
	    AND capacity >= 1
	    AND (last_heartbeat_at IS NULL OR last_heartbeat_at >= DATE_SUB(NOW(), INTERVAL ${HEARTBEAT_TTL_SECONDS} SECOND))
	    AND (COALESCE(internal_base_url, '') <> '' OR COALESCE(gateway_url, '') <> '')
	  GROUP BY assistant_id
	) candidates;
")"
  if [[ "${parallel_candidates:-0}" -ge 2 ]]; then
    pass_case "至少 ${parallel_candidates} 个助手具备独立 runtime 槽位，满足跨助手并行的前置条件"
  else
    warn_case "可用助手 runtime 槽位少于 2 个；当前状态不能证明跨助手并行"
  fi
}

check_workspace_files() {
  section "结果文件准确性检查"
  check_zero "workspace_files active 自然路径重复" "
SELECT COUNT(*)
FROM (
  SELECT assistant_id, bucket, filename, COUNT(*) AS c
  FROM workspace_files
  WHERE COALESCE(deleted, 0) = 0
  GROUP BY assistant_id, bucket, filename
  HAVING COUNT(*) > 1
) dup;
"
  check_zero "workspace_files active_path_key 缺失" "
SELECT COUNT(*)
FROM workspace_files
WHERE COALESCE(deleted, 0) = 0
  AND (active_path_key IS NULL OR active_path_key = '');
"
  check_zero "workspace_files active_path_key 重复" "
SELECT COUNT(*)
FROM (
  SELECT active_path_key, COUNT(*) AS c
  FROM workspace_files
  WHERE active_path_key IS NOT NULL AND active_path_key <> ''
  GROUP BY active_path_key
  HAVING COUNT(*) > 1
) dup;
"
  mysql_table "recent-result-file-scope-mismatch" "
SELECT
  w.file_id,
  w.owner_user_id,
  t.user_id AS task_user_id,
  w.assistant_id,
  t.target_assistant_id,
  w.source_ref,
  w.direction,
  w.file_role,
  w.relative_path,
  w.updated_at
FROM workspace_files w
JOIN gateway_tasks t ON t.task_id = w.source_ref
WHERE w.updated_at >= DATE_SUB(NOW(), INTERVAL ${RECENT_MINUTES} MINUTE)
  AND COALESCE(w.deleted, 0) = 0
  AND w.source_type IN ('assistant_result', 'assistant_output')
  AND (
    w.direction <> 'outbound'
    OR w.file_role <> 'result'
    OR (t.target_assistant_id IS NOT NULL AND w.assistant_id <> t.target_assistant_id)
    OR (t.user_id IS NOT NULL AND t.user_id <> '' AND w.owner_user_id <> t.user_id)
  )
ORDER BY w.updated_at DESC
LIMIT 50;
"
  local mismatch_count
  mismatch_count="$(scalar "
SELECT COUNT(*)
FROM workspace_files w
JOIN gateway_tasks t ON t.task_id = w.source_ref
WHERE w.updated_at >= DATE_SUB(NOW(), INTERVAL ${RECENT_MINUTES} MINUTE)
  AND COALESCE(w.deleted, 0) = 0
  AND w.source_type IN ('assistant_result', 'assistant_output')
  AND (
    w.direction <> 'outbound'
    OR w.file_role <> 'result'
    OR (t.target_assistant_id IS NOT NULL AND w.assistant_id <> t.target_assistant_id)
    OR (t.user_id IS NOT NULL AND t.user_id <> '' AND w.owner_user_id <> t.user_id)
  );
")"
  if [[ "${mismatch_count:-0}" == "0" ]]; then
    pass_case "最近 ${RECENT_MINUTES} 分钟未发现结果文件错用户/错助手/错方向"
  else
    fail_case "最近 ${RECENT_MINUTES} 分钟结果文件 scope mismatch = $mismatch_count"
  fi
}

check_conversation_routes() {
  section "会话路由检查"
  mysql_table "conversation-routes-summary" "
SELECT assistant_id, COUNT(*) AS route_count, MIN(created_at) AS first_bound, MAX(updated_at) AS last_bound
FROM gateway_conversation_routes
GROUP BY assistant_id
ORDER BY route_count DESC, assistant_id
LIMIT 50;
"
  check_zero "gateway_conversation_routes 空助手绑定" "
SELECT COUNT(*) FROM gateway_conversation_routes
WHERE assistant_id IS NULL OR assistant_id = '';
"
  local route_without_runtime
  route_without_runtime="$(scalar "
SELECT COUNT(*)
FROM gateway_conversation_routes r
LEFT JOIN gateway_runtime_instances i ON i.assistant_id = r.assistant_id
WHERE i.assistant_id IS NULL;
  ")"
  if [[ "${route_without_runtime:-0}" == "0" ]]; then
    pass_case "gateway_conversation_routes 均能找到 runtime 实例记录"
  else
    warn_case "gateway_conversation_routes 中有 ${route_without_runtime} 条当前找不到 runtime 实例；可能是助手停机，需要人工确认"
  fi
}

check_queue_and_outbox() {
  section "队列、容量和 callback/outbox 观测"
  mysql_table "recent-at-capacity" "
SELECT
  t.target_assistant_id,
  q.status AS queue_status,
  COUNT(*) AS task_count,
  MAX(q.updated_at) AS last_seen
FROM gateway_task_queue q
JOIN gateway_tasks t ON t.task_id = q.task_id
WHERE q.updated_at >= DATE_SUB(NOW(), INTERVAL ${RECENT_MINUTES} MINUTE)
  AND q.last_error LIKE '%at_capacity%'
GROUP BY t.target_assistant_id, q.status
ORDER BY task_count DESC, t.target_assistant_id
LIMIT 50;
"
  local at_capacity_count
  at_capacity_count="$(scalar "
SELECT COUNT(*)
FROM gateway_task_queue
WHERE updated_at >= DATE_SUB(NOW(), INTERVAL ${RECENT_MINUTES} MINUTE)
  AND last_error LIKE '%at_capacity%';
")"
  if [[ "${at_capacity_count:-0}" == "0" ]]; then
    pass_case "最近 ${RECENT_MINUTES} 分钟没有 scheduler at_capacity 记录"
  else
    warn_case "最近 ${RECENT_MINUTES} 分钟 scheduler at_capacity=${at_capacity_count}；同助手 capacity=1 时可能正常，但要结合队列延迟看"
  fi
  mysql_table "queue-status-summary" "
SELECT status, COUNT(*) AS count, MIN(created_at) AS oldest, MAX(updated_at) AS newest
FROM gateway_task_queue
GROUP BY status
ORDER BY count DESC, status;
"
  mysql_table "outbox-status-summary" "
SELECT status, COUNT(*) AS count, MIN(created_at) AS oldest, MAX(updated_at) AS newest
FROM gateway_event_outbox
GROUP BY status
ORDER BY count DESC, status;
"
  local dead_recent
  dead_recent="$(scalar "
SELECT COUNT(*)
FROM gateway_dead_letters
WHERE failed_at >= DATE_SUB(NOW(), INTERVAL ${RECENT_MINUTES} MINUTE);
")"
  if [[ "${dead_recent:-0}" == "0" ]]; then
    pass_case "最近 ${RECENT_MINUTES} 分钟没有 dead letter"
  else
    fail_case "最近 ${RECENT_MINUTES} 分钟 dead letter=${dead_recent}"
  fi
}

section "基础环境"
kubectl get namespace "$NS" >/dev/null
pass_case "namespace 可访问：$NS"
print_gateway_env

if [[ "$ALLOW_MYSQL_ROOT" != "true" ]]; then
  skip_case "未传 --allow-mysql-root；仅完成 K8s/Gateway 环境检查，没有 DB 证据"
  finalize
  exit 0
fi

kubectl -n "$NS" get pod "$MYSQL_POD" >/dev/null
pass_case "MySQL Pod 可访问：$MYSQL_POD"

check_mysql_version
check_schema
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  fail_case "迁移结构检查未通过，跳过后续依赖新表/新列的 DB 检查"
  finalize
  exit 1
fi
check_runtime_state
check_workspace_files
check_conversation_routes
check_queue_and_outbox

finalize

if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
if [[ "$STRICT" == "true" && "$WARN_COUNT" -gt 0 ]]; then
  exit 1
fi
exit 0
