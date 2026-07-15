# P7 Deployment Test Runbook

更新日期：2026-06-11

## 1. 目标

本手册优先用于测试环境验证 Go Gateway 的 500-2000 runtime 分发能力；真实生产/多 Gateway 环境在测试环境通过后再补实测。

覆盖范围：

- MySQL 持久化 runtime registry。
- MySQL task queue。
- MySQL webhook outbox。
- scheduler 选择延迟。
- task 创建 QPS。
- callback 故障注入。
- Prometheus metrics 抓取。

不覆盖范围：

- 真实 assistant 容器内部执行质量。
- Python southbound 业务正确性。
- 对外 IM Webhook 正式联调。

## 2. 前置条件

当前阶段按测试环境优先执行，详细拆分见：

- `../archive/historical/p7-test-environment-plan.md`（已归档）
- `../archive/historical/local-downstream-ironagent-runbook.md`（已归档）

对接边界：

- 上游请求、响应、状态字段以已提供的 IronAgent / 小熊猫接口文档为准。
- 下游验证可以启动本地 IronAgent，并把 Gateway 指向 `security-service:8000` 与 `admin:8501`。
- 不要把本地 southbound 的 `claim_token`、collaboration task、workspace-files 原始结构暴露给上游。

Go Gateway 启动时必须启用数据库：

```bash
GO_GATEWAY_DATABASE_URL='mysql+pymysql://user:pass@host:3306/heinu?charset=utf8mb4'
GO_GATEWAY_TASK_WORKER_ENABLED=true
GO_GATEWAY_TASK_QUEUE_MAX_PENDING=10000
GO_GATEWAY_SCOPED_API_KEYS='<public-api-key>:public;<runtime-api-key>:runtime;<debug-api-key>:debug;<admin-api-key>:admin,public'
GO_GATEWAY_HMAC_SECRET='<hmac-secret>'
GO_GATEWAY_INTERNAL_SERVICE_TOKEN='<internal-service-token>'
```

注意：

- `<...-api-key>` 必须替换为随机生成的密钥。gateway 启动时会拒绝 `public-key`、`runtime-key`、`debug-key`、`admin-key`、`proxy-key`、`change-me` 这类弱默认值。
- `<admin-api-key>` 需要同时带 `admin,public` 两个 scope，供第 5 节 chat-load 使用（见该节说明）。

如果使用本地 IronAgent 作为下游：

```bash
GO_GATEWAY_SECURITY_BASE_URL=http://127.0.0.1:8000
GO_GATEWAY_ADMIN_BASE_URL=http://127.0.0.1:8501
```

如果只做本地无 HMAC 快速验证，可以临时改用：

```bash
GO_GATEWAY_API_KEYS=dev-key
GO_GATEWAY_HMAC_SECRET=
```

启动前必须已执行迁移：

```bash
alembic upgrade head
```

## 3. 工具

构建：

```bash
cd go-gateway
go build ./cmd/p7tool
```

帮助：

```bash
./p7tool
```

## 4. Runtime Registry 压测

注册 2000 个 runtime：

```bash
./p7tool runtime-seed \
  -gateway http://127.0.0.1:8080 \
  -api-key '<runtime-api-key>' \
  -client-id p7-runtime-seed \
  -hmac-secret '<hmac-secret>' \
  -count 2000 \
  -concurrency 100 \
  -capacity 10 \
  -capabilities analysis,chat
```

验收：

- `go_gateway_runtime_registered_total` 接近 2000。
- `go_gateway_runtime_ready_total` 接近 2000。
- `go_gateway_runtime_capacity_total` 接近 `2000 * capacity`。
- `/readyz` 返回 200。

## 5. Chat 创建压测

创建 1000 个任务，目标 runtime 由 scheduler 选择：

注意：`chat-load` 提交的是内部完整字段（`source_assistant_id`、`input_object_id`、`required_capabilities` 等），走 `/api/v1/chat` 的内部验证入口，API key 必须同时具备 `public` 与 `admin`（或 `internal`）scope。整改后的 public `/chat` 契约只接受 `request_id`/`user_id`/`conversation_id`/`message`/`files`/`requires_result_file`/`timeout_seconds`，未知字段会被直接拒绝，因此纯 `public` scope 的 key 不能用于 chat-load。

```bash
./p7tool chat-load \
  -gateway http://127.0.0.1:8080 \
  -api-key '<admin-api-key>' \
  -client-id p7-loadtest \
  -hmac-secret '<hmac-secret>' \
  -tasks 1000 \
  -concurrency 50 \
  -source-assistant-id demo-main \
  -input-object-id '<真实 input_object_id>' \
  -required-capabilities analysis
```

验收：

- HTTP 2xx 占比稳定。
- `go_gateway_task_queue_active` 不持续单调上升。
- `go_gateway_scheduler_decisions_total` 随请求增长。
- `go_gateway_scheduler_latency_seconds_max` 不应持续放大。
- `go_gateway_southbound_errors_total` 不应快速增长。

## 6. Callback 故障注入

启动正常 callback：

```bash
./p7tool callback-sim -listen :18081 -behavior ack
```

启动 500 注入：

```bash
./p7tool callback-sim -listen :18081 -behavior status_500
```

启动无 ACK 兼容验证：

```bash
./p7tool callback-sim -listen :18081 -behavior no_ack
```

启动超时注入：

```bash
./p7tool callback-sim -listen :18081 -behavior timeout
```

带 callback 压测：

```bash
./p7tool chat-load \
  -gateway http://127.0.0.1:8080 \
  -api-key '<admin-api-key>' \
  -client-id p7-loadtest \
  -hmac-secret '<hmac-secret>' \
  -tasks 100 \
  -concurrency 10 \
  -source-assistant-id demo-main \
  -input-object-id '<真实 input_object_id>' \
  -callback-url http://127.0.0.1:18081/callback
```

注意：

- 本地 callback URL 需要 `GO_GATEWAY_CALLBACK_ALLOW_PRIVATE_HOSTS=true`。
- 测试环境如果使用 localhost/private callback，需要显式开启 private hosts。
- 生产环境必须配置 `GO_GATEWAY_CALLBACK_ALLOWED_HOSTS`，不要打开 private hosts。

验收：

- HTTP 2xx 场景：`go_gateway_webhook_events_total{result="delivered"}` 增长，dead-letter 不增长。
- 500/timeout 场景：`failed_attempt`、`retry_scheduled`、`dead_letter` 应按预期增长。
- dead-letter replay 后：`replay` 增长，事件重新进入 pending 投递。

## 7. Metrics 抓取

Prometheus 抓取端点：

```text
GET /metrics
GET /api/v1/metrics/prometheus
```

需要 debug scope：

```bash
scripts/go-gateway/hmac_request.py \
  --url http://127.0.0.1:8080 \
  --method GET \
  --path /metrics \
  --api-key '<debug-api-key>' \
  --client-id p7-debug \
  --secret '<hmac-secret>'
```

核心 dashboard：

| 面板 | 指标 |
| --- | --- |
| Gateway readiness | `go_gateway_health_ready` |
| Task queue depth | `go_gateway_task_queue_active`, `go_gateway_task_queue_queued`, `go_gateway_task_queue_retry` |
| Runtime pool | `go_gateway_runtime_registered_total`, `go_gateway_runtime_ready_total`, `go_gateway_runtime_in_flight_total`, `go_gateway_runtime_capacity_total` |
| Scheduler latency | `go_gateway_scheduler_latency_seconds_sum / go_gateway_scheduler_latency_seconds_count`, `go_gateway_scheduler_latency_seconds_max` |
| Southbound errors | `go_gateway_southbound_errors_total`, `go_gateway_southbound_requests_total` |
| Webhook lifecycle | `go_gateway_webhook_events_total`, `go_gateway_webhook_dead_letters_total` |
| File traffic | `go_gateway_file_transfer_total`, `go_gateway_file_transfer_bytes_total` |

更完整的 PromQL 和告警建议见：

- `p7-dashboard-and-alerts.md`

## 8. 当前判定口径

测试环境可通过：

- 2000 runtime seed 成功。
- 1000 task chat-load 成功率稳定。
- queue active 在 worker 开启后可回落。
- scheduler latency 没有持续恶化。
- callback 故障能进入 retry/dead-letter/replay。

测试环境不可通过：

- queue active 持续上涨且无回落。
- southbound error 快速增长。
- runtime ready 明显低于 seed 数。
- callback 失败后没有 dead-letter。
- replay 后事件没有重新投递。
