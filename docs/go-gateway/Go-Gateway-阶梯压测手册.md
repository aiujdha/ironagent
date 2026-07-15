# Go Gateway 阶梯压测手册

更新日期：2026-05-25

本文用于在内网、预生产或生产仿真环境中验证 Go Gateway、Redis、MySQL、task queue 和 callback/outbox 在阶梯并发下的承载能力。

压测目标不是单纯追求接口返回快，而是确认：

- `/api/v1/chat` 在目标并发下能按预期接收、背压或失败。
- Redis admission limit 正常工作，且 Redis 本身没有 rejected/failed calls。
- MySQL 主连接池和 callback/outbox 小连接池不会把 `max_connections` 打满。
- callback outbox 最终能追平，accepted/cancelled/progress 等事件不会无故 pending 或 dead。
- 压测结束后 task queue 能回到 `active=0`。

## 1. 测试结论口径

阶梯压测必须先定义目标，否则 429 会被误判。

推荐分成两种目标：

| 目标 | 说明 | 800 并发出现 429 是否失败 |
| --- | --- | --- |
| 保护型 | 系统超过容量后主动背压，避免 MySQL/Redis/Gateway 被打爆 | 否，只要 429 明确、队列能清空、无 5xx 和 callback 丢失 |
| 全接收型 | 指定并发下所有请求都必须返回 202，并最终处理掉 | 是，需要调大队列、限流、worker、DB/callback 预算后复测 |

当前推荐验收口径：

- `500` 并发：尽量全部接收并完成 callback。
- `800` 并发：允许 `429 queue_full` 或限流背压，但不允许 MySQL max connection 新增、不允许 Gateway 重启、不允许 outbox 丢事件。
- `1000+` 并发：用于找破坏边界，必须在可回滚环境执行。

## 2. 压测前准备

### 2.1 环境变量

在能访问集群和 Gateway 的机器上设置：

```bash
export NS=ironagent
export K="sudo kubectl --kubeconfig=/etc/kubernetes/admin.conf"
export GATEWAY_URL="http://192.0.2.10:30808"
export TEST_USER_ID="12064912"
export OUT_DIR="output/stair-load-$(date +%Y%m%d%H%M%S)"
mkdir -p "${OUT_DIR}"
```

如果是在本地通过公网 NodePort 压测，`K` 可以指向远端控制面：

```bash
export CONTROL_HOST="ubuntu@110.40.168.67"
export REMOTE_K="sudo kubectl --kubeconfig=/etc/kubernetes/admin.conf"
```

### 2.2 确认测试用户可用

测试用户必须存在，并绑定一个可用 assistant。先做一个 smoke：

```bash
request_id="stair-smoke-$(date +%s)"
curl -sS -X POST "${GATEWAY_URL}/api/v1/chat" \
  -H "Content-Type: application/json" \
  --data-binary @- <<JSON
{
  "request_id": "${request_id}",
  "user_id": "${TEST_USER_ID}",
  "conversation_id": "${request_id}",
  "message": "stair load smoke",
  "files": []
}
JSON
```

期望返回 `202` 和 `task_id`。

公开 cancel 接口当前只接受空 JSON：

```bash
curl -sS -X POST "${GATEWAY_URL}/api/v1/tasks/${TASK_ID}/cancel" \
  -H "Content-Type: application/json" \
  --data-binary '{}'
```

如果 smoke 返回：

- `user not found`：测试用户不存在，需要先创建用户并绑定助手。
- `用户无可用助手`：用户有绑定但助手不可用或已删除。
- `queue_full`：队列里已有积压，先清空或等待 worker 处理。

### 2.3 记录部署参数

```bash
${K} -n "${NS}" get deploy go-gateway -o jsonpath='{.spec.replicas}{"\n"}'
${K} -n "${NS}" get deploy go-gateway -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'

${K} -n "${NS}" get configmap go-gateway-config -o jsonpath='
queue={.data.GO_GATEWAY_TASK_QUEUE_MAX_PENDING}{"\n"}
rate={.data.GO_GATEWAY_CHAT_RATE_LIMIT_MAX}/{.data.GO_GATEWAY_CHAT_RATE_LIMIT_WINDOW}{"\n"}
db_primary={.data.GO_GATEWAY_DATABASE_MAX_OPEN_CONNS}/{.data.GO_GATEWAY_DATABASE_MAX_IDLE_CONNS}{"\n"}
db_callback={.data.GO_GATEWAY_CALLBACK_DATABASE_MAX_OPEN_CONNS}/{.data.GO_GATEWAY_CALLBACK_DATABASE_MAX_IDLE_CONNS}{"\n"}
callback_store_timeout={.data.GO_GATEWAY_CALLBACK_STORE_TIMEOUT}{"\n"}
callback_enqueue={.data.GO_GATEWAY_CALLBACK_ENQUEUE_MAX_IN_FLIGHT}{"\n"}
callback_delivery_concurrency={.data.GO_GATEWAY_CALLBACK_DELIVERY_CONCURRENCY}{"\n"}
'
```

必须记录这些值，否则压测结果无法解释。

## 3. 连接预算

压测前必须先计算 MySQL 全局连接预算。

公式：

```text
gateway_replicas * (
  GO_GATEWAY_DATABASE_MAX_OPEN_CONNS
  + GO_GATEWAY_CALLBACK_DATABASE_MAX_OPEN_CONNS
)
+ admin/security/runtime/ops 预留连接
<= mysql_max_connections * 0.8
```

示例：

```text
MySQL max_connections = 151
安全水位 = 151 * 0.8 = 120

Gateway 单副本 = 40 + 8 = 48
Gateway 两副本 = 96

如果 admin/security/运维预留 25，
两副本总预算 = 96 + 25 = 121，已经贴近或超过安全水位。
```

压测前采样：

```bash
${K} -n "${NS}" exec -i mysql-0 -- sh -lc 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -N' <<'SQL'
SHOW GLOBAL VARIABLES LIKE 'max_connections';
SHOW GLOBAL STATUS WHERE Variable_name IN (
  'Threads_connected',
  'Threads_running',
  'Connection_errors_max_connections'
);
SQL
```

停止条件：

- `Connection_errors_max_connections` 增加。
- `Threads_connected >= max_connections * 0.8`。
- `Threads_running` 长时间高于 CPU 核数或持续升高。

## 4. 阶梯矩阵

推荐按下面顺序执行，不要直接从 800 开始：

| 阶段 | 并发 | 轮数 | 轮间隔 | 目标 |
| --- | ---: | ---: | ---: | --- |
| smoke | 1 | 1 | - | 验证脚本、用户和 callback |
| warmup | 20 | 1 | 60s | 预热连接池和缓存 |
| low | 100 | 1-3 | 120s | 验证低并发延迟 |
| mid | 300 | 1-3 | 180s | 观察 DB pool wait |
| target | 500 | 3 | 300s | 目标并发验收 |
| stress | 800 | 1-3 | 300s | 验证背压和恢复 |
| breakpoint | 1000+ | 1 | 300s | 找边界，仅限可回滚环境 |

每一档都必须记录：

- create：`202`、`429`、`5xx`、timeout 数量。
- cancel：`200`、`4xx`、`5xx`、timeout 数量。
- create/cancel p50、p95、p99、max。
- task queue 最终是否 `active=0`。
- outbox 本轮 prefix 的 accepted/cancelled 是否全部 delivered。
- MySQL max connection error 是否新增。
- Redis rejected/failed calls 是否新增。
- Pod 是否重启。

## 5. 压测脚本

以下脚本执行一个并发阶梯：同时创建 N 个 `/api/v1/chat`，然后取消所有返回 202 的任务，并把结果写成 JSON。

运行方式：

```bash
export CONCURRENCY=500
export PREFIX="codex-stair-${CONCURRENCY}-$(date +%Y%m%d%H%M%S)"
node - <<'JS'
(async () => {
const fs = require("fs");
const path = require("path");

const base = process.env.GATEWAY_URL;
const userId = process.env.TEST_USER_ID;
const concurrency = Number(process.env.CONCURRENCY || "100");
const prefix = process.env.PREFIX || `stair-${concurrency}-${Date.now()}`;
const outDir = process.env.OUT_DIR || "output/stair-load";
const timeoutMs = Number(process.env.HTTP_TIMEOUT_MS || "45000");

if (!base || !userId) {
  throw new Error("GATEWAY_URL and TEST_USER_ID are required");
}
fs.mkdirSync(outDir, { recursive: true });

function percentile(values, p) {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const index = Math.min(sorted.length - 1, Math.ceil((p / 100) * sorted.length) - 1);
  return sorted[index];
}

function summarize(items) {
  const durations = items.filter((x) => typeof x.ms === "number").map((x) => x.ms);
  const statuses = {};
  const errorCodes = {};
  for (const item of items) {
    statuses[item.status ?? "ERR"] = (statuses[item.status ?? "ERR"] || 0) + 1;
    if (item.errorCode) errorCodes[item.errorCode] = (errorCodes[item.errorCode] || 0) + 1;
    if (item.errorName) errorCodes[item.errorName] = (errorCodes[item.errorName] || 0) + 1;
  }
  return {
    total: items.length,
    statuses,
    errorCodes,
    avgMs: durations.length ? durations.reduce((a, b) => a + b, 0) / durations.length : 0,
    p50Ms: percentile(durations, 50),
    p95Ms: percentile(durations, 95),
    p99Ms: percentile(durations, 99),
    maxMs: durations.length ? Math.max(...durations) : 0,
  };
}

async function postJSON(url, payload) {
  const started = performance.now();
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(timeoutMs),
    });
    const text = await res.text();
    let json = null;
    try { json = JSON.parse(text); } catch {}
    return {
      status: res.status,
      ms: performance.now() - started,
      json,
      errorCode: json?.error?.code || "",
      body: text.slice(0, 1000),
    };
  } catch (err) {
    return {
      status: "ERR",
      ms: performance.now() - started,
      errorName: err?.name || "Error",
      errorMessage: err?.message || String(err),
    };
  }

console.log(JSON.stringify({
  event: "start",
  prefix,
  concurrency,
  userId,
  startedAt: new Date().toISOString(),
}));

const createStarted = performance.now();
const create = await Promise.all(Array.from({ length: concurrency }, (_, index) => {
  const n = String(index + 1).padStart(4, "0");
  const requestId = `${prefix}-${n}`;
  return postJSON(`${base}/api/v1/chat`, {
    request_id: requestId,
    user_id: userId,
    conversation_id: `${prefix}-conv-${n}`,
    message: `stair load probe ${n}`,
    files: [],
  }).then((result) => ({
    ...result,
    index: index + 1,
    requestId,
    taskId: result.json?.task_id || result.json?.taskId || "",
  }));
const createElapsedMs = performance.now() - createStarted;
const accepted = create.filter((item) => item.status === 202 && item.taskId);

console.log(JSON.stringify({
  event: "created",
  elapsedMs: createElapsedMs,
  accepted: accepted.length,
  summary: summarize(create),
}));

const cancelStarted = performance.now();
const cancel = await Promise.all(accepted.map((item) => {
  return postJSON(`${base}/api/v1/tasks/${encodeURIComponent(item.taskId)}/cancel`, {})
    .then((result) => ({
      ...result,
      index: item.index,
      requestId: item.requestId,
      taskId: item.taskId,
    }));
const cancelElapsedMs = performance.now() - cancelStarted;

console.log(JSON.stringify({
  event: "cancelled",
  elapsedMs: cancelElapsedMs,
  attempted: cancel.length,
  summary: summarize(cancel),
}));

const result = {
  prefix,
  base,
  userId,
  concurrency,
  finishedAt: new Date().toISOString(),
  createElapsedMs,
  cancelElapsedMs,
  createSummary: summarize(create),
  cancelSummary: summarize(cancel),
  acceptedTaskIds: accepted.map((item) => item.taskId),
  create,
  cancel,
};
const file = path.join(outDir, `${prefix}.json`);
fs.writeFileSync(file, JSON.stringify(result, null, 2));
console.log(JSON.stringify({ event: "written", file }));
})();
JS
```

批量跑阶梯：

```bash
for c in 20 100 300 500 800; do
  export CONCURRENCY="$c"
  export PREFIX="codex-stair-${c}-$(date +%Y%m%d%H%M%S)"
  node scripts_or_heredoc_above
  sleep 180
done
```

如果不想改成独立脚本，就逐档执行上一段 heredoc。

## 6. 每档压测前后采样

### 6.1 Gateway readiness

```bash
curl -sS -m 5 "${GATEWAY_URL}/healthz"
curl -sS -m 5 "${GATEWAY_URL}/readyz"
```

验收：

- `/readyz` 返回 healthy。
- `task_queue=up`。
- runtime scheduler 中 ready runtime 数符合预期。

### 6.2 Pod 状态

```bash
${K} -n "${NS}" get pods --no-headers
${K} -n "${NS}" get deploy,statefulset
```

验收：

- 核心 Pod 无新增重启。
- `go-gateway`、`mysql`、`redis-ha`、`redis-sentinel`、`security-service` 全部 Running。

### 6.3 MySQL 快照

```bash
${K} -n "${NS}" exec -i mysql-0 -- sh -lc 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -N' <<'SQL'
SHOW GLOBAL VARIABLES LIKE 'max_connections';
SHOW GLOBAL STATUS WHERE Variable_name IN (
  'Threads_connected',
  'Threads_running',
  'Connection_errors_max_connections'
);

SELECT status, COUNT(*)
FROM heinu.gateway_task_queue
GROUP BY status
ORDER BY status;

SELECT status, COUNT(*)
FROM heinu.gateway_event_outbox
GROUP BY status
ORDER BY status;
SQL
```

### 6.4 本轮 outbox 追平查询

压测脚本的 request_id 都以 `PREFIX` 开头，可以按 prefix 查询本轮 callback。

```bash
export PREFIX="codex-stair-500-20260525093000"

${K} -n "${NS}" exec -i mysql-0 -- sh -lc 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" heinu -N' <<SQL
SELECT
  event_type,
  status,
  COUNT(*) AS count,
  ROUND(AVG(TIMESTAMPDIFF(MICROSECOND, created_at, COALESCE(delivered_at, updated_at))/1000000), 3) AS avg_s,
  ROUND(MAX(TIMESTAMPDIFF(MICROSECOND, created_at, COALESCE(delivered_at, updated_at))/1000000), 3) AS max_s
FROM gateway_event_outbox
WHERE JSON_UNQUOTE(JSON_EXTRACT(payload_json, '$.request_id')) LIKE CONCAT('${PREFIX}', '%')
GROUP BY event_type, status
ORDER BY event_type, status;
SQL
```

验收：

- `task.accepted` 的 delivered 数等于 create `202` 数。
- cancel 阶段执行后，`task.cancelled` 的 delivered 数等于 cancel `200` 数。
- 本轮 prefix 不应出现 `pending` 长时间不动。
- 本轮 prefix 不应出现 `dead`，除非 callback receiver 故障注入正在执行。

### 6.5 Redis 快照

如果使用 Sentinel 主从，先确认 master：

```bash
${K} -n "${NS}" exec deploy/redis-sentinel -- sh -lc '
  REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli -p 26379 --raw sentinel get-master-addr-by-name mymaster
'
```

查看 Redis commandstats：

```bash
${K} -n "${NS}" exec redis-ha-1 -- sh -lc '
  REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli --user "$REDIS_USERNAME" info commandstats \
    | grep -E "cmdstat_(get|set|incr|expire|eval|evalsha)"
'
```

查看 Redis 健康：

```bash
${K} -n "${NS}" exec redis-ha-1 -- sh -lc '
  REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli --user "$REDIS_USERNAME" info stats \
    | grep -E "rejected_connections|expired_keys|evicted_keys|instantaneous_ops_per_sec"
  REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli --user "$REDIS_USERNAME" info memory \
    | grep -E "used_memory_human|maxmemory_human|mem_fragmentation_ratio"
  REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli --user "$REDIS_USERNAME" info persistence \
    | grep -E "aof_enabled|aof_last_write_status|rdb_last_bgsave_status"
  REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli --user "$REDIS_USERNAME" info replication \
    | grep -E "^(role|connected_slaves|master_host):"
'
```

验收：

- `rejected_calls=0`。
- `failed_calls=0`。
- `rejected_connections=0`。
- 非预期情况下 `evicted_keys` 不增长。
- AOF 写状态为 `ok`。

### 6.6 Gateway DB pool metrics

如果 debug API 已配置，可通过 HMAC/API key 访问 `/api/v1/metrics`。不要打印 secret，只在控制面内生成签名请求。

```bash
python3 - <<'PY'
import base64
import hashlib
import hmac
import json
import subprocess
import time
import uuid
import urllib.request

ns = "ironagent"
gateway_url = "http://192.0.2.10:30808"
raw = subprocess.check_output(
    [
        "sudo", "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf",
        "-n", ns, "get", "secret", "go-gateway-secret", "-o", "json",
    ],
    text=True,
)
data = json.loads(raw)["data"]
scoped = base64.b64decode(data["GO_GATEWAY_SCOPED_API_KEYS"]).decode()
secret = base64.b64decode(data["GO_GATEWAY_HMAC_SECRET"]).decode()
debug_key = ""
for entry in scoped.split(";"):
    if ":" not in entry:
        continue
    key, scopes = entry.split(":", 1)
    if "debug" in [scope.strip() for scope in scopes.split(",")]:
        debug_key = key
        break

path = "/api/v1/metrics"
body = b""
timestamp = str(int(time.time()))
nonce = str(uuid.uuid4())
client_id = "stair-load"
payload = ".".join([
    client_id,
    timestamp,
    nonce,
    "GET",
    path,
    hashlib.sha256(body).hexdigest(),
])
signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
request = urllib.request.Request(
    gateway_url + path,
    headers={
        "X-API-Key": debug_key,
        "X-Client-ID": client_id,
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": signature,
    },
)
with urllib.request.urlopen(request, timeout=10) as response:
    metrics = json.loads(response.read().decode())
    print(json.dumps({
        "db_pools": metrics.get("db_pools"),
        "task_queue": metrics.get("task_queue"),
    }, ensure_ascii=False, indent=2, sort_keys=True))
PY
```

重点看：

- `db_pools.primary.WaitCount`
- `db_pools.primary.WaitDuration`
- `db_pools.callback_outbox.WaitCount`
- `db_pools.callback_outbox.WaitDuration`
- `task_queue.active`

解释：

- `WaitCount` 增加说明连接池开始排队，不等于 MySQL 被打满。
- `WaitDuration` 持续高速增长说明 pool 参数、SQL、worker 或并发目标需要调整。
- callback_outbox 的 wait 高，说明 callback 入队、claim 或 mark 受到小池限制。

## 7. 停止条件

任一条件触发时，立即停止后续阶梯：

| 条件 | 处理 |
| --- | --- |
| `/readyz` unhealthy 或超时 | 停止压测，保留现场 |
| Gateway Pod 新增重启 | 停止压测，抓日志 |
| MySQL `Connection_errors_max_connections` 增加 | 停止压测，降低 Gateway pool 或并发 |
| MySQL `Threads_connected >= max_connections * 0.8` | 停止压测，重新做连接预算 |
| create 出现非预期 5xx 超过 1% | 停止压测，分析错误码 |
| accepted callback 长时间未 delivered | 停止压测，查 outbox/receiver/DB pool |
| outbox dead 新增且非故障注入 | 停止压测，查 callback receiver |
| Redis rejected/failed calls 增加 | 停止压测，查 Redis CPU/内存/连接 |
| task queue active 10 分钟不下降 | 停止压测，查 worker 和 runtime |

## 8. 结果判读

### 8.1 `429 queue_full`

如果并发超过 `GO_GATEWAY_TASK_QUEUE_MAX_PENDING`，`429 queue_full` 是正确背压，不应直接判为失败。

判定方式：

- 保护型目标：可接受。
- 全接收型目标：不可接受，需要扩容和调参。

### 8.2 `429 rate limit`

说明 Redis admission limit 生效。

处理：

- 如果这是预期保护，记录即可。
- 如果目标是全接收，提高 `GO_GATEWAY_CHAT_RATE_LIMIT_MAX`，同时重算 DB/worker/outbox 预算。

### 8.3 create 202 但 accepted callback 没有 delivered

这是高风险问题。

检查：

```sql
SELECT event_id, task_id, event_type, status, attempts, last_error, locked_by, locked_until, created_at, updated_at
FROM gateway_event_outbox
WHERE JSON_UNQUOTE(JSON_EXTRACT(payload_json, '$.request_id')) LIKE CONCAT('<PREFIX>', '%')
ORDER BY created_at
LIMIT 50;
```

可能原因：

- callback receiver 不可达或慢。
- callback/outbox DB pool 等待过高。
- outbox worker 未运行。
- outbox 租约 stuck。

### 8.4 MySQL max connection error 新增

说明连接预算失败。

处理方向：

- 降低 Gateway 主池和 callback/outbox 池。
- 降低 Gateway 副本数。
- 降低并发或提高背压阈值策略。
- 提高 MySQL `max_connections` 之前，先确认 CPU/内存也能承载。

### 8.5 Redis 正常但 MySQL 压力仍高

这通常说明 Redis 只承担 admission/cache，而任务创建、outbox、审计仍会写 MySQL。

处理方向：

- 确认 high-QPS health/runtime 是否仍打 MySQL。
- 降低 callback enqueue concurrency。
- 优化 outbox/queue 索引。
- 做慢 SQL 和 DB pool wait 分析。

## 9. Soak 测试

阶梯压测后必须做长稳 soak。

推荐：

| 场景 | 配置 |
| --- | --- |
| 轻量 soak | 30 分钟，每轮 100 并发，间隔 60 秒 |
| 目标 soak | 60 分钟，每轮 300-500 并发，间隔 60-120 秒 |
| 高峰 soak | 60 分钟，每轮接近业务峰值，保留背压 |

soak 脚本逻辑：

```bash
for round in $(seq 1 30); do
  export CONCURRENCY=100
  export PREFIX="codex-soak-r${round}-$(date +%Y%m%d%H%M%S)"
  # 执行第 5 节脚本
  sleep 60
done
```

soak 重点不是单轮 p99，而是趋势：

- `Threads_connected` 是否回落。
- DB pool `WaitDuration` 是否持续恶化。
- Redis memory 是否持续上涨。
- outbox pending/dead 是否持续累积。
- task queue active 是否周期性回到 0。
- Pod 是否重启。

## 10. 故障注入

故障注入必须在 canary 或维护窗口执行。

### 10.1 Redis Sentinel failover

```bash
${K} -n "${NS}" exec deploy/redis-sentinel -- sh -lc '
  REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli -p 26379 sentinel failover mymaster
'
```

期望：

- Gateway 不重启。
- failover 后 `/api/v1/chat` 继续可用。
- Redis commandstats 无 rejected/failed。
- 如果配置为 chat admission fail-closed，短暂失败应返回明确 429/错误，而不是打爆 MySQL。

### 10.2 MySQL 不可用

推荐只在隔离环境做：

- canary Gateway 指向不可达 DB endpoint。
- 或短时间阻断 MySQL service。

期望：

- `/api/v1/chat` 不返回虚假的 `202`。
- `task.accepted` outbox 写失败时 fail closed。
- pending outbox 不丢。
- MySQL 恢复后 worker 继续处理。

### 10.3 callback receiver 慢或 500

让 callback receiver：

- 返回 500。
- sleep 3-10 秒后返回。
- 临时不可达。

期望：

- outbox retry。
- 达到 max attempts 后进入 dead-letter。
- 主任务 DB pool 不被 callback 拖垮。

## 11. 调参建议

### 11.1 想让 800 并发全部接收

必须同时调整，而不是只改一个参数：

- `GO_GATEWAY_TASK_QUEUE_MAX_PENDING >= 800`
- `GO_GATEWAY_CHAT_RATE_LIMIT_MAX >= 800`
- task worker claim/interval 与 runtime capacity 匹配
- callback delivery concurrency 与 callback receiver 能力匹配
- MySQL 主池、callback/outbox 池、Gateway 副本数重新做连接预算
- Redis CPU、内存、网络和 Sentinel failover 重新验证

风险：

- 只调高 queue/rate limit，会把压力后移到 MySQL、worker 或 outbox。
- 只调高 DB pool，可能把 MySQL `max_connections` 打满。
- 只调高 callback delivery concurrency，可能导致 callback receiver 或 callback/outbox 小池排队。

### 11.2 想保护系统不被打爆

保守配置：

- 保持 `GO_GATEWAY_TASK_QUEUE_MAX_PENDING` 在 worker 可清空的范围。
- 保持 `GO_GATEWAY_CHAT_RATE_LIMIT_MAX` 小于真实 DB/worker 峰值能力。
- callback/outbox 使用独立小池。
- outbox enqueue 使用短 timeout 和并发上限。
- 允许超过容量时返回 `429 queue_full`。

## 12. 报告模板

每次压测后把下面内容写入测试报告。

```markdown
## YYYY-MM-DD 阶梯压测

环境：

- Gateway：
- Namespace：
- Gateway 镜像：
- Gateway 副本：
- MySQL max_connections：
- primary DB pool：
- callback/outbox DB pool：
- Redis 模式：
- queue max pending：
- chat rate limit：
- 测试用户：

阶梯结果：

| 并发 | create 202 | create 429 | create 5xx/timeout | create p95/p99 | cancel 200 | cancel p95/p99 | 结论 |
| ---- | ---------- | ---------- | ------------------ | -------------- | ---------- | -------------- | ---- |
| 100 | | | | | | | |
| 300 | | | | | | | |
| 500 | | | | | | | |
| 800 | | | | | | | |

outbox：

| Prefix | event_type | delivered | pending | dead | avg delivery | max delivery |
| ------ | ---------- | --------- | ------- | ---- | ------------ | ------------ |

MySQL：

- Threads_connected before/after/max：
- Threads_running before/after/max：
- Connection_errors_max_connections before/after：
- DB pool primary WaitCount/WaitDuration：
- DB pool callback_outbox WaitCount/WaitDuration：

Redis：

- INCR/EXPIRE/GET/SET 增量：
- rejected/failed calls：
- evicted_keys：
- memory：
- replication/failover：

结论：

- 是否达到 500 并发目标：
- 800 并发是全接收还是背压：
- 是否出现 MySQL 连接风险：
- 是否出现 callback/outbox 积压：
- 下一步调参建议：
```

## 13. 最小验收标准

推荐上线前至少满足：

- 100/300 阶梯：全部 `202`，无非预期 5xx。
- 500 阶梯：满足业务目标；如果目标是全接收，应 `500/500` 返回 `202`。
- 800 阶梯：允许背压，但不允许 Gateway/MySQL/Redis 崩溃。
- 压测后 task queue `active=0`。
- 本轮 prefix 的 accepted/cancelled callback 全 delivered。
- MySQL `Connection_errors_max_connections` 不新增。
- Redis rejected/failed calls 不新增。
- 30-60 分钟 soak 后 DB pool wait、outbox pending、Redis memory 不持续恶化。
