# P7 Dashboard And Alerts

更新日期：2026-04-26

## 1. Dashboard 面板

### Gateway Readiness

PromQL：

```promql
go_gateway_health_ready
```

判定：

- `1` 表示 ready。
- `0` 表示 `/readyz` 也应返回 503。

### Task Create QPS

PromQL：

```promql
sum(rate(go_gateway_task_create_total[5m])) by (result, status, stage)
```

判定：

- `result="accepted"` 是正常新增任务。
- `result="replayed"` 是幂等重放。
- `result="failed"` 持续升高需要排查鉴权、参数、southbound 或队列背压。

### Task Create Latency

PromQL：

```promql
sum(rate(go_gateway_task_create_latency_seconds_sum[5m]))
/
sum(rate(go_gateway_task_create_latency_seconds_count[5m]))
```

Bucket：

```promql
sum(rate(go_gateway_task_create_latency_seconds_bucket[5m])) by (le)
```

### Task Queue Depth

PromQL：

```promql
go_gateway_task_queue_active
go_gateway_task_queue_queued
go_gateway_task_queue_retry
```

判定：

- `active` 持续上涨且不回落，说明 worker 吞吐不足或 southbound 变慢。
- `retry` 持续上涨，说明 worker 执行 southbound 链路失败。

### Runtime Pool

PromQL：

```promql
go_gateway_runtime_registered_total
go_gateway_runtime_ready_total
go_gateway_runtime_unhealthy_total
go_gateway_runtime_in_flight_total
go_gateway_runtime_capacity_total
```

判定：

- `ready / registered` 长期低于预期，说明 heartbeat 或 runtime readiness 有问题。
- `in_flight / capacity` 接近 1，说明 runtime 池接近满载。

### Scheduler Latency

PromQL：

```promql
sum(rate(go_gateway_scheduler_latency_seconds_sum[5m]))
/
sum(rate(go_gateway_scheduler_latency_seconds_count[5m]))
```

Bucket：

```promql
sum(rate(go_gateway_scheduler_latency_seconds_bucket[5m])) by (le, outcome, reason)
```

判定：

- `outcome="rejected"` 持续增长，优先看 `reason`。
- `reason="no_eligible_runtime"` 表示 runtime 不足、draining、unhealthy、能力不匹配或满载。

### Southbound Latency And Error Rate

PromQL：

```promql
sum(rate(go_gateway_southbound_errors_total[5m])) by (component, method, path, status_class)
/
sum(rate(go_gateway_southbound_requests_total[5m])) by (component, method, path, status_class)
```

Latency：

```promql
sum(rate(go_gateway_southbound_latency_seconds_sum[5m])) by (component, path)
/
sum(rate(go_gateway_southbound_latency_seconds_count[5m])) by (component, path)
```

Bucket：

```promql
sum(rate(go_gateway_southbound_latency_seconds_bucket[5m])) by (le, component, path)
```

### Webhook Lifecycle

PromQL：

```promql
sum(rate(go_gateway_webhook_events_total[5m])) by (result, event_type)
go_gateway_webhook_dead_letters_total
```

判定：

- `failed_attempt` 增长说明 callback 返回非 2xx 或超时。
- `retry_scheduled` 增长说明已进入重试。
- `dead_letter` 增长说明超过最大投递次数。
- `replay` 增长说明有人工或运维重放。

### File Transfer

PromQL：

```promql
sum(rate(go_gateway_file_transfer_total[5m])) by (direction)
sum(rate(go_gateway_file_transfer_bytes_total[5m])) by (direction)
```

判定：

- `presigned_upload` / `presigned_download` 应是主要文件字节流。
- `direct_upload` / `direct_download` 如果持续很高，说明外部仍在走兼容接口。

## 2. 建议告警

### Gateway Not Ready

```promql
go_gateway_health_ready == 0
```

持续时间：

```text
1m
```

### Queue Backlog Growing

```promql
go_gateway_task_queue_active > 1000
```

持续时间：

```text
5m
```

### No Ready Runtime

```promql
go_gateway_runtime_ready_total == 0
```

持续时间：

```text
1m
```

### Southbound Error Rate High

```promql
(
  sum(rate(go_gateway_southbound_errors_total[5m]))
  /
  sum(rate(go_gateway_southbound_requests_total[5m]))
) > 0.05
```

持续时间：

```text
5m
```

### Webhook Dead Letter Appears

```promql
increase(go_gateway_webhook_dead_letters_total[5m]) > 0
```

持续时间：

```text
0m
```

### Scheduler Rejects Too Many Tasks

```promql
sum(rate(go_gateway_scheduler_decisions_total{outcome="rejected"}[5m])) > 1
```

持续时间：

```text
5m
```

## 3. 维护口径

- 当前 metrics 是轻量 text format 输出，不依赖 Prometheus client。
- 如果后续接入正式 Prometheus client，应保留现有 metric name，避免 dashboard 失效。
- 如果要新增 label，先评估 cardinality，禁止把 task_id、user_id、callback_url 放入 label。
