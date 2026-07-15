# IronAgent Gateway 可观测性 / 日志排查 Wiki

> 目的:运维和开发**只看线上日志**就能定位问题。本文覆盖一个请求/任务从进入网关到回调结束的**全链路日志事件、字段、关联 id 和常见故障排查路径**。
> 所有事件名、字段、reason code 均取自真实代码,可直接 grep。

---

## 0. 全链路总览

```
                          ┌─────────────────────────── 同步响应路径 ───────────────────────────┐
 客户端                                                                                          客户端
   │  POST /api/v1/chat                                                                            ▲
   ▼                                                                                               │ 202 Accepted
 ① HTTP Ingress + 鉴权          ② 建任务 + 准入            ③ 队列 Worker            ④ Runtime WS 执行
 router.go / middleware.go      CreateChatTask            ProcessQueuedTasks       ironagent_ws.go
 ─────────────────────          ─────────────────         ──────────────────       ─────────────────
 · withUpstreamRequestID        · 准入三关:               · 认领→执行→重试/终态     · 拨号→握手→chat.send
 · auth 鉴权                       rate-limit/全局/        · 失败归因(四轴)           · 超时/卡死/in_flight
 · gateway_auth_rejected          per-assistant 容量      · 终态: terminal_failed    · chat.abort 取消
                                · 铸 task_id(gw-xxx)                                · ★归因诞生地★
                                · timeout→DeadlineSource
        │                              │                          │                        │
        └──────────────────────────────┴──────────┬───────────────┴────────────────────────┘
                                                   ▼
                                    ⑤ Callback / Webhook 投递(异步旁路)
                                    webhook/dispatcher.go (事务性 outbox)
                                    · 入队→推送→成功 / dead-letter
                                    · task.accepted/retrying/completed/failed
```

**关键:① 是同步路径(立即返回 202),③④⑤ 是异步后台。一个任务的"接收成功"日志在 ①,但真正的执行结果在 ③④,回调在 ⑤。**

---

## 1. 关联 id 体系(怎么把一个任务的全链路串起来)★最重要★

| id | 何时产生 | 跨哪些段 | 用途 |
|---|---|---|---|
| `upstream_request_id` | ① 客户端 `X-Request-ID`/`X-Correlation-ID`/`?request_id`,网关采纳(不生成) | **仅 ①同步路径** | 关联到调用方的 trace。**不传到 ③④⑤** |
| `request_id` | 客户端在 body 里传的业务 id | ①②③⑤ | 客户端侧幂等/关联 |
| `task_id` | ② `CreateChatTask` 成功后铸造,格式 `gw-<32hex>` | **②③④⑤ 全程**(异步主键) | **串联全链路的主 id** |
| `southbound_task_id` | runtime/southbound 侧的任务 id(`task.ID`) | ④⑤ + ③的部分日志 | 关联 runtime 侧自己的日志 |
| `run_id` | ④ `chat.send` 时 = idempotencyKey = `request.RequestID` | 仅 ④ WS 内 | 把一个 run 的 chat 帧串起来 |
| `event_id` | ⑤ 每个 callback 事件的唯一 id(hex) | 仅 ⑤ | 关联客户端 ACK + dead-letter 重放 |

### 串链路的标准方法

```bash
# 从客户端的 X-Request-ID 出发(只能到同步路径):
upstream_request_id="<X-Request-ID>"          # → 拿到 request_id + task_id(在"提交任务callback入队失败"或需桥接)

# 【推荐】从 task_id(gw-xxx)出发,覆盖异步全程:
task_id="gw-xxxxxxxx"                          # ②③④⑤ 都 key 在它上面

# 关联 runtime 侧:
southbound_task_id="<southbound-id>"           # ④的 WS 日志 + runtime 自己的日志

# 关联某个回调:
event_id="<hex>"                               # ⑤ 入队→推送→成功/dead-letter→ACK
```

> ⚠️ **已知断点**(见 §6):`upstream_request_id` 不桥接到 `task_id`,且没有单行日志同时带三个 id。从客户端 trace 进异步链路要靠 `request_id` 中转(而它是可选字段)。

---

## 2. 故障归因四轴字典(每条失败/重试/成功日志都带)

attribution 改造的核心:每个结局都打四个字段,回答"是谁的问题"。

| 字段 | 取值 | 含义 |
|---|---|---|
| **`fault_domain`** | `UPSTREAM` / `GATEWAY` / `RUNTIME` / `INDETERMINATE` / `NONE` | 哪一方的锅。`NONE`=成功;`INDETERMINATE`=判不了(兜底,绝不甩锅给 runtime) |
| **`reason_code`** | 见下表(domain.sub 格式) | 细分原因 |
| **`fault_owner`** | `upstream` / `gateway` / `runtime` | **谁做的判定**(可与 domain 不同:如空回复 domain=RUNTIME 但 owner=gateway,因为是网关的策略判的) |
| **`attribution_confidence`** | `high` / `medium` / `low` | 置信度。`low`=字符串匹配/兜底 INDETERMINATE |

### `reason_code` 全 18 值

| reason_code | 归属 | 含义 |
|---|---|---|
| `ok` | NONE | 成功 |
| `runtime.exec_error` | RUNTIME | runtime 发了 error 帧(自报执行错) |
| `runtime.connect_rejected` | RUNTIME | runtime 拒绝握手(token/协议/scope) |
| `runtime.chat_rejected` | RUNTIME | runtime 拒绝 chat.send |
| `runtime.upgrade_4xx` | RUNTIME | WS 升级被 runtime 以 4xx 拒 |
| `runtime.upgrade_5xx` | RUNTIME | WS 升级被 runtime 以 5xx 拒 |
| `runtime.stale_no_progress` | RUNTIME | runtime 长时间无进度(卡死) |
| `runtime.empty_reply` | RUNTIME | runtime 跑完但回复为空(owner=gateway) |
| `runtime.declared_failed` | RUNTIME | runtime 在结果里自报失败 |
| `gateway.deadline_self` | GATEWAY | 网关默认超时把任务掐了 |
| `gateway.runtime_dial_failed` | GATEWAY | 网关连不上 runtime(网络/拒连/DNS/TLS) |
| `gateway.in_flight_unreachable` | GATEWAY | 同任务上次还在跑,这次连不上它 |
| `gateway.capacity_unavailable` | GATEWAY | 无可用 runtime 容量 |
| `gateway.southbound_active_timeout` | GATEWAY | 等上一个 southbound 任务终结超时 |
| `gateway.runtime_unconfigured` / `gateway.runtime_dial_failed` | GATEWAY | 没有可拨的 runtime 端点 |
| `gateway.result_sync_failed` | GATEWAY | 结果文件同步失败 |
| `upstream.timeout_budget_too_short` | UPSTREAM | 调用方给的超时太短导致超时 |
| `undetermined` | INDETERMINATE | 判不了(兜底) |

> **fail-closed 原则:任何判不了的故障 → `INDETERMINATE`,owner 默认 `gateway`,绝不默认甩锅 runtime。**

---

## 3. 客户端可见 enum 字典(进 callback payload + 任务 API)

> 做闭集白名单校验的 webhook/API 消费方,必须支持这两个字段的所有取值(本次新增 `runtime_in_flight` / `runtime_ws.in_flight`)。

### `failed_reason_code`(任务终态失败码,写 `Task.FailedReasonCode`)
`queue_worker_error` · `gateway_task_stale_progress` · `gateway_task_active_southbound_timeout` · `runtime_capacity_timeout` · `runtime_capacity_unavailable` · `runtime_in_flight` ⭐ · `runtime_execution_failed` · `runtime_empty_reply` · `runtime_connection_failed` · `runtime_gateway_unavailable` · `runtime_result_file_failed` · `queue_lease_heartbeat_failed` · `collaboration_claim_heartbeat_failed` · `TASK_ACTIVATION_FAILED`

### `error_location`(出错位置)
`queue_worker` · `runtime_ws.stale_progress` · `runtime_ws.deadline` · `runtime_ws.in_flight` ⭐ · `runtime_ws.connect` · `runtime_ws.wait_final` · `southbound_task.poll_timeout` · `runtime_registry.capacity` · `runtime_registry.gateway_url` · `runtime_result_files.sync`

---

## 4. 分段日志事件表

### ① HTTP Ingress + 鉴权

| 事件名 | 级别 | 触发 / 定位 |
|---|---|---|
| `gateway_auth_rejected` | WARN | 鉴权失败(401/403/413/500)。`fault_domain`/`status_code`/`upstream_request_id` 定位是坏凭证(UPSTREAM)还是网关 nonce-store 故障(GATEWAY,500) |
| `提交任务开始` | INFO | 进入 service 层,带 `upstream_request_id`。某 request_id 没这行=更早死在 decode/admission |
| `提交任务请求校验失败` | WARN | body 解码失败(400/413),只有 `error` |
| `提交任务admission拒绝` | WARN | 准入拒(429 queue_full),最接近"限流"信号,一堆=网关/队列饱和 |
| `提交任务callback地址校验失败` | WARN | callback URL 非法/SSRF 拦截 |
| `提交任务文件引用校验失败` | WARN | 引用的文件不存在/跨用户(422/403) |
| `提交任务失败` | WARN | `CreateChatTask` 失败,配 `提交任务开始` 看 error |
| `提交任务callback入队失败` | WARN | 任务建了但 callback 入队失败→补偿取消,503 |
| `提交任务队列激活失败` | WARN | 任务建了但激活失败,`TASK_ACTIVATION_FAILED` |
| `提交任务已接收` | INFO | ★成功终点★,带 `task_id`(gw-xxx) |

### ② 建任务 + 准入

| 事件名 | 级别 | 触发 / 定位 |
|---|---|---|
| `gateway_task_accepted` | INFO | ★任务被完整接收★,给出 gw-xxx + `timeout_seconds`(有效超时) |
| `gateway_task_assistant_queue_full` | WARN | **某 assistant** 队列满(429)。**唯一打日志的准入拒**(全局队满只返回 429 不打日志) |
| `gateway_task_admit_failed` | WARN | DB 原子建任务失败(存储层,非饱和) |
| `gateway_task_enqueue_failed` / `_record_write_failed` | WARN | 队列写/任务记录写失败(含补偿取消) |
| `gateway_task_activated` / `_activate_failed` | INFO/WARN | admitting→queued 激活成功/失败 |

### ③ 队列 Worker

| 事件名 | 级别 | 触发 / 定位 |
|---|---|---|
| `gateway_task_queue_claimed` | INFO | worker 认领了任务(给 request_id↔task_id 映射),**worker 活着的信号** |
| `gateway_task_queue_claim_failed` | WARN | 认领失败(队列/DB 级,worker 完全拉不到活) |
| `gateway_task_attempt_started` | INFO | 每次尝试开始,`attempt_token` 钉住这次租约 |
| `gateway_task_attempt_completed` | INFO | 尝试成功 |
| `gateway_task_attempt_failed` | WARN | ★主失败行★,`retry=true`还会试/`false`终态,`terminal_reason_code` |
| `gateway_task_attempt_deferred_for_capacity` | WARN | 容量背压(没崩没失败),`capacity_wait_count`涨=runtime 队不够 |
| `gateway_task_attempt_capacity_wait_exhausted` | WARN | 容量等待预算耗尽→终态(`runtime_capacity_timeout`) |
| `gateway_task_attempt_deferred_for_active_southbound` | WARN | 等上一个 southbound 任务结束(良性延后) |
| `gateway_task_retry_marked` | INFO | 重试已持久化+全归因 |
| `gateway_queued_task_terminal_failed` | WARN | ★★终态三连查这个★★ 带完整四轴 + `failed_reason_code` |
| `gateway_task_progress_stale` | WARN | runtime 卡死(无进度),`last_progress_at`看跑到哪 |
| `gateway_task_queue_lease_heartbeat_failed` | WARN | worker 丢了 DB 租约(进程/DB/被偷) |
| `gateway_collaboration_claim_heartbeat_failed` | WARN | 丢了 southbound claim 租约,`assistant_id`定位 |

### ④ Runtime WS 执行(归因诞生地)

> 注:WS 执行器自身的生命周期日志用**中文 msg**;结果分类的 `gateway_runtime_*` 在 service.go。

| 事件名 | 级别 | 触发 / 定位 |
|---|---|---|
| `gateway_runtime_execute_start` | INFO | 一次 runtime 尝试基线,`runtime_id`/`gateway_url`/`timeout_ms`/`stale_timeout_ms` |
| `gateway_runtime_gateway_default_deadline_widened` | INFO | A1 兜底放宽,`backstop_ms`,看谁占 worker 久 |
| `运行时WebSocket连接失败` | WARN | 拨号失败。4xx/5xx→RUNTIME(runtime 拒升级);纯拨号错→GATEWAY(连不上) |
| `运行时WebSocket握手失败` | WARN | 握手/鉴权拒(token/协议) |
| `运行时报告任务已在执行中(in_flight)...` | WARN | in_flight:上次还在跑连不上,快速失败可重试(`gateway.in_flight_unreachable`) |
| `运行时聊天错误事件` | WARN | runtime 发 error 帧(`runtime.exec_error`) |
| `运行时聊天已主动取消` / `...取消失败` | WARN | chat.abort 成功/失败,`cancel_reason`=stale/deadline |
| `gateway_runtime_execute_failed` | WARN | ★主分诊行★ 四轴定位谁的锅 + `retryable` |
| `gateway_runtime_execute_stale` | WARN | runtime 静默(卡死) |
| `gateway_runtime_empty_reply` | WARN | 空回复(domain=RUNTIME,owner=gateway) |
| `gateway_runtime_result_indicates_failure` | WARN | runtime 自报失败(owner=runtime) |
| `gateway_runtime_execute_completed` | INFO | ★唯一成功标记★(只真成功) |
| `gateway_runtime_gateway_unavailable` / `_assistant_lookup_failed` / `_route_lookup_failed` | WARN | 没端点可拨 / 查不到 assistant / 路由解析失败 |

### ⑤ Callback / Webhook 投递

| 事件名 | 级别 | 触发 / 定位 |
|---|---|---|
| `callback事件已入队` | INFO | ★回调追踪起点★,`mode=outbox`持久化/`memory`仅内存,给 `event_id` |
| `callback事件入队失败` | WARN | 没进 outbox=客户端永远收不到(通常 DB 问题) |
| `开始推送callback事件` | INFO | 每次 POST 尝试,数它=尝试次数 |
| `callback事件推送成功` | INFO | ★成功★,`callback_response_ack_ok`=接收端确认,`_duration_ms`=延迟 |
| `callback事件推送失败，将重试` | WARN | 瞬时失败重试,`failure_reason`(upstream_5xx/timeout/...),`next_attempt_at` |
| `callback事件推送失败，进入dead-letter` | WARN | ★永久放弃★,重试耗尽,客户端没收到,可按 `event_id` 重放 |
| `callback事件被接收端明确拒收...` | WARN | 接收端明确拒(`receiver_limit`),重试无用,改接收端 |
| `callback retrying backlog已合并` | INFO | 旧 retrying 被新事件 supersede(故意,防刷屏)。客户端报"少了 retrying"的良性原因 |
| `callback ACK已确认` / `不匹配` / `无效` | INFO/WARN | 接收端回 ACK 的确认/不匹配/格式错 |
| `webhook enqueue failed` | WARN | handler 侧某 `event_type` 入队失败,先查这个看哪类事件没发出 |

---

## 5. 常见故障排查 Playbook

### A. "某任务卡住不动 / 没结果"
```
1. task_id="gw-xxx" → 看最后一条事件:
   · 停在 gateway_task_attempt_started 后无下文 → 卡在 ④ runtime
   · gateway_task_progress_stale → runtime 卡死(看 last_progress_at)
   · gateway_runtime_gateway_default_deadline_widened → 在兜底窗口里跑(最长 backstop_ms)
2. gateway_queued_task_terminal_failed → 看 fault_domain 定责
```

### B. "任务失败,是谁的锅?"
```
grep msg="gateway_queued_task_terminal_failed" task_id="gw-xxx"
→ fault_domain: UPSTREAM(调用方超时给太短) / GATEWAY(网关连不上/超时/容量) /
                RUNTIME(runtime 拒绝/执行错/卡死) / INDETERMINATE(判不了,查 error 原文)
→ failed_reason_code + reason_code 给细分;error_location 给出错位置
```

### C. "回调没收到"
```
1. callback事件已入队 task_id="gw-xxx" → 有没有入队?
   · 没有 → 查 callback事件入队失败 / webhook enqueue failed(handler 侧)
2. 入队了 → 开始推送callback事件 → 推送成功? 还是
   · callback事件推送失败，进入dead-letter → 永久失败,看 failure_reason,按 event_id 重放(步骤见 §10.5)
   · callback retrying backlog已合并 → 被新事件取代(良性)
⚠️ 盲区:若 outbox 后台 worker 整体挂了,无任何日志(见 §6)——查不到入队后的推送行就要怀疑这个
```

### D. "鉴权失败排查"
```
grep msg="gateway_auth_rejected" upstream_request_id="<X-Request-ID>"
→ status_code 401(凭证/签名/时间戳/replay)/403(scope)/413(超大)/500(网关 nonce-store)
⚠️ 盲区:~10 种 401 原因都塌缩成 reason_code=upstream.unauthorized,
        具体原因(replay/时钟偏移/坏签名)只在 error 原文里(见 §6)
```

### E. "容量不够 / 任务一直在等"
```
grep msg="gateway_task_attempt_deferred_for_capacity" → capacity_wait_count 持续涨 = runtime 队不够,扩容
grep msg="gateway_task_assistant_queue_full" → 某 assistant 队满(429),扩/排空那一个
grep msg="gateway_task_attempt_capacity_wait_exhausted" → 等够久了被终态(runtime_capacity_timeout)
```

> 日志侧查不动时(pod 重启日志断流、需要核对持久化现场、上游坚称没收到回调),转 **§10 DB 侧 SQL 排障**。

---

## 6. 盲区修复状态(本次已补的可观测性)

> 此前可观测性审计发现 10 个盲区。**本次(commit `43faee47`)用结构化日志补齐了大部分**(纯加日志、不改逻辑)。下表是现状 + 现在怎么查。

| 原盲区 | 状态 | 现在怎么查 |
|---|---|---|
| Outbox 后台 worker 静默失败 | ✅ 已修 | `callback_outbox_process_failed`(带 `consecutive_failures`+`error`)——outbox 挂了立刻可见,不再"回调全无、零日志" |
| 僵尸 runtime 回收无日志 | ✅ 已修 | `runtime_heartbeat_expired`(assistant_id/runtime_id/last_heartbeat_at)+ `runtime_heartbeat_expired_sweep`(聚合 count) |
| 同步 runtime 代理拒绝无日志 | ✅ 已修 | `runtime_proxy_rejected`(每个非 2xx:assistant_id/proxy_kind/reason_code/status_code,含熔断打开) |
| 无 `duration_ms` | ✅ 已修 | `duration_ms` 进 `gateway_runtime_execute_completed`/`_failed`/`_empty_reply`/`_result_indicates_failure`;`attempt_duration_ms` 进 `gateway_task_attempt_completed`/`_failed`——**一眼区分快速失败 vs 超时** |
| `upstream_request_id` 不桥接 task_id | ✅ 已修 | `提交任务已接收` 现在带 `upstream_request_id`,**单行串 upstream↔request↔task_id** |
| auth 失败塌缩成一个 reason_code | ✅ 已修 | `gateway_auth_rejected` 加 `auth_reason`(`auth.replayed_nonce`/`auth.timestamp_skew`/`auth.bad_signature`/`auth.invalid_api_key`/...)——replay/时钟偏移/坏 key 可区分告警 |
| collaboration claim-recovery 缺 gw task_id | ✅ 已修 | recovery 日志已加 task id,可接回任务主链 |
| stale 事件缺 runtime 关联 | 🔶 部分修 | 加了 `target_assistant_id`;`runtime_id`/`lease_id` 不在该日志点 scope(需改签名),仍按 task_id 回查 `gateway_runtime_execute_start` |
| in_flight 无收敛失败事件 | 🔶 部分修 | in_flight warn 加 `runtime_reported_run_id`(识别卡死的 prior run);终态本就被 `failed_reason_code=runtime_in_flight` 覆盖,无需额外终态事件 |
| 结果文件同步家族未文档化 | 📄 仅文档 | 代码已有 `gateway_runtime_result_files_sync_*`/`结果文件*` 事件,本 wiki 未展开(可后续补一节) |

> **剩余未做(都需改函数签名/plumb,超出"纯加日志"范围,留 follow-up):** outbox backlog 计数 gauge(store 无 count 方法)、`route_lease_renew_failed` 的 task_id、`progress_stale` 的 runtime_id/lease_id、registry register/deregister 日志。

---

## 7. 配置字典(超时 / 容量相关)

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `GO_GATEWAY_RUNTIME_EXECUTION_TIMEOUT` | 10m | 客户端没传 `timeout_seconds` 时的默认执行超时(gateway_default 基准) |
| `GO_GATEWAY_TASK_WORKER_GATEWAY_DEFAULT_DEADLINE_BACKSTOP` | 20m | A1 兜底:gateway_default 任务最长能跑多久(0=禁用)。⭐本次新增 |
| `GO_GATEWAY_TASK_WORKER_STALE_PROGRESS_TIMEOUT` | 600s | runtime 多久无进度算卡死(`runtime.stale_no_progress`)。0=禁用(那 backstop 是唯一兜底) |
| `GO_GATEWAY_TASK_WORKER_ACTIVE_SOUTHBOUND_DEFER_TIMEOUT` | 60m | 已有 southbound task 持续 active/claimed 时,defer 重查的墙钟上限(按 gateway task `created_at` 计时);0=禁用 |
| `GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_MAX_ATTEMPTS` | 100 | 容量等待最多几次再终态(0=该轴不限) |
| `GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_TIMEOUT` | 30m | 容量等待最长时间(0=不限)。**不能和 MAX_ATTEMPTS 同时为 0**(否则永不终止,启动会拒) |
| `GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_CALLBACK_INTERVAL` | 5m | 容量等待进度回调的最小间隔(节流) |
| `GO_GATEWAY_TASK_WORKER_MAX_ATTEMPTS` | 3 | 普通失败的最大重试次数 |

### 超时优先级
```
客户端 timeout_seconds > 0  →  client_supplied  →  硬上限,A1 不介入
客户端没传(=0)             →  gateway_default  →  实际超时取 RUNTIME_EXECUTION_TIMEOUT,
                                                    再被 A1 放宽到 DEADLINE_BACKSTOP
```

---

## 8. k8s 生产环境排查命令 ★运维入口★

> 生产部署在 k8s,namespace `ironagent`,go-gateway **单副本**(replicas=1)但单 pod 内 **16 并发 worker**。日志是 **slog JSON 直接打到 stdout**,用 `kubectl logs` + `jq` 查。

### 8.0 部署速查(命令里要用的固定参数)

| 资源 | 值 |
|---|---|
| namespace | `ironagent` |
| go-gateway Deployment / Pod 选择 | `deploy/go-gateway` 或 `-l app=go-gateway` |
| go-gateway 容器名 | `go-gateway`(单容器,通常不用 `-c`) |
| go-gateway Service / 端口 | `go-gateway` : `8080`(http) |
| 健康检查 | liveness `/healthz` · readiness `/readyz` |
| 配置 ConfigMap | `go-gateway-config`(环境变量) · `go-gateway-file-config`(能力文件) |
| Secret | `go-gateway-secret` |
| callback-receiver | `deploy/ironagent-callback-receiver`(容器 `receiver`,端口 `8099`) |
| 日志格式 | slog JSON,关键字段 `time` / `level` / `msg` / `task_id` / `fault_domain` 等 |

### 8.1 Pod 健康 / 状态

```bash
# pod 是否在跑、重启次数(CrashLoop?)、就绪
kubectl -n ironagent get pods -l app=go-gateway -o wide

# 详情:看事件、OOMKilled、重启原因、探针失败
kubectl -n ironagent describe pod -l app=go-gateway

# 资源占用(需 metrics-server)
kubectl -n ironagent top pod -l app=go-gateway
```

### 8.2 看日志(基础)—— 单副本 16 并发,日志量大,务必限流

```bash
# 实时跟随(排查进行中的问题)
kubectl -n ironagent logs -f deploy/go-gateway

# 最近 N 行 / 时间窗(别裸 logs,会刷屏)
kubectl -n ironagent logs deploy/go-gateway --tail=500
kubectl -n ironagent logs deploy/go-gateway --since=15m

# ★pod 崩溃/重启后,看崩溃前那个容器的日志(查 panic / OOM 现场)★
kubectl -n ironagent logs deploy/go-gateway --previous
```

### 8.3 过滤日志(⚠️ 线上无 jq,用 grep)

> 线上 kubectl 环境**没有 `jq`**。日志是 slog **紧凑 JSON**(`"key":"value"`,键值之间无空格),所以直接用 `grep '"key":"value"'` 精确命中字段,子串误匹配风险很低。统计用 `grep -o ... | sort | uniq -c`,数值比较用 `awk`——这些 busybox 都自带。

```bash
# 只看 WARN/ERROR(快速扫异常)
kubectl -n ironagent logs deploy/go-gateway --since=30m | grep -E '"level":"(WARN|ERROR)"'

# 看某个事件
kubectl -n ironagent logs deploy/go-gateway --since=30m | grep '"msg":"gateway_queued_task_terminal_failed"'

# 把一行 JSON 拆开方便读(无 jq 的粗暴美化:逗号换行)
kubectl -n ironagent logs deploy/go-gateway --since=10m | grep '"task_id":"<TASK_ID>"' | tr ',' '\n'

# 统计某字段取值分布(grep -o 抽字段值 + uniq)
kubectl -n ironagent logs deploy/go-gateway --since=30m \
  | grep '"msg":"gateway_queued_task_terminal_failed"' \
  | grep -o '"fault_domain":"[^"]*"' | sort | uniq -c | sort -rn
```

### 8.4 按场景排查(§5 playbook 的 kubectl 落地版)

把 `<TASK_ID>` 换成 `gw-xxx`,`--since` 按需调。

```bash
# A. 追踪某任务全链路(按 task_id 串所有事件)
kubectl -n ironagent logs deploy/go-gateway --since=1h | grep '"task_id":"<TASK_ID>"'

# B. 某任务终态失败是谁的锅(组合 grep)
kubectl -n ironagent logs deploy/go-gateway --since=1h \
  | grep '"msg":"gateway_queued_task_terminal_failed"' | grep '"task_id":"<TASK_ID>"'

# C. 全局终态失败按归因方分布(一眼看是谁在拖后腿)
kubectl -n ironagent logs deploy/go-gateway --since=1h \
  | grep '"msg":"gateway_queued_task_terminal_failed"' \
  | grep -o '"fault_domain":"[^"]*"' | sort | uniq -c | sort -rn

# D. 回调没到:先看该任务回调轨迹,再看 outbox worker 是否挂了
kubectl -n ironagent logs deploy/go-gateway --since=1h \
  | grep '"task_id":"<TASK_ID>"' | grep '"msg":"callback'
kubectl -n ironagent logs deploy/go-gateway --since=30m \
  | grep '"msg":"callback_outbox_process_failed"'      # ←有输出=outbox 后台挂了!

# E. 鉴权失败(整体看)
kubectl -n ironagent logs deploy/go-gateway --since=30m | grep '"msg":"gateway_auth_rejected"'
# 按细分原因统计(replay/时钟偏移/坏key 一眼看分布)
kubectl -n ironagent logs deploy/go-gateway --since=30m \
  | grep '"msg":"gateway_auth_rejected"' \
  | grep -o '"auth_reason":"[^"]*"' | sort | uniq -c | sort -rn

# F. 慢任务/耗时排序(awk 抽 duration_ms,倒序看 top20;区分快速失败 vs 超时)
kubectl -n ironagent logs deploy/go-gateway --since=1h | grep '"duration_ms":' \
  | awk -F'"duration_ms":' '{ d=$2+0; match($0,/"task_id":"[^"]*"/); print d"\t"substr($0,RSTART,RLENGTH) }' \
  | sort -rn | head -20

# G. 容量背压:哪些任务在反复等容量(扩容信号)
kubectl -n ironagent logs deploy/go-gateway --since=30m \
  | grep '"msg":"gateway_task_attempt_deferred_for_capacity"' \
  | grep -o '"task_id":"[^"]*"' | sort | uniq -c | sort -rn

# H. 僵尸 runtime 被回收(纯心跳超时;路由租约过期是另一个事件 runtime_route_lease_expired)
kubectl -n ironagent logs deploy/go-gateway --since=1h | grep '"msg":"runtime_heartbeat_expired"'

# I. 被 A1 兜底放宽的长任务(谁在长占 worker)
kubectl -n ironagent logs deploy/go-gateway --since=2h \
  | grep '"msg":"gateway_runtime_gateway_default_deadline_widened"'

# J. 某用户/员工的所有失败(定位受影响的人;失败/终态日志现在直接带 user_id + employee_no)
kubectl -n ironagent logs deploy/go-gateway --since=1h | grep '"user_id":"<USER_ID>"' | grep '"level":"WARN"'
kubectl -n ironagent logs deploy/go-gateway --since=1h | grep '"employee_no":"<EMP_NO>"'

# K. 哪个用户失败最多(某来源批量出问题的信号)
kubectl -n ironagent logs deploy/go-gateway --since=1h \
  | grep '"msg":"gateway_queued_task_terminal_failed"' \
  | grep -o '"user_id":"[^"]*"' | sort | uniq -c | sort -rn
```

> 发布者字段:`user_id`(真实发起用户)、`employee_no`(员工号)现在直接打在 `gateway_queued_task_terminal_failed`、`gateway_runtime_execute_failed/_stale`、`gateway_runtime_empty_reply`、`gateway_task_progress_stale` 以及所有 worker 尝试事件上——**一条失败日志就能同时回答"谁发的 + 谁的锅"**,不用再按 task_id 回查接收日志。
> (注:`gateway_runtime_result_files_sync_*` 这组 result-file 事件的 `user_id` 仍是 `created_by` 语义,值可能与真实 `user_id` 不同,排查那组时以 task_id 关联为准。)

### 8.5 callback-receiver(看实际投出去的回调内容)

> receiver 把收到的 callback body **原样打印到 stdout**,是核对"网关到底发了什么"的地方。

```bash
# 最近回调
kubectl -n ironagent logs deploy/ironagent-callback-receiver --since=30m

# 某任务的回调 body(含新枚举 runtime_in_flight / 四轴 attribution)
kubectl -n ironagent logs deploy/ironagent-callback-receiver --since=1h | grep "<TASK_ID>"
```

### 8.6 配置 / 运维操作

```bash
# 看线上生效的关键 config(确认 backstop / 超时 / 并发)
kubectl -n ironagent get configmap go-gateway-config -o yaml \
  | grep -E "BACKSTOP|EXECUTION_TIMEOUT|STALE_PROGRESS|MAX_ATTEMPTS|CONCURRENCY"

# 进 pod 看实际环境变量(config + secret 合并后的真实值)
kubectl -n ironagent exec deploy/go-gateway -- env | grep GO_GATEWAY_TASK_WORKER

# 健康探针手动测(port-forward 到本地)
kubectl -n ironagent port-forward svc/go-gateway 8080:8080 &
curl -s localhost:8080/readyz; curl -s localhost:8080/healthz

# 改完 configmap 后让其生效(滚动重启;单副本会有短暂中断,注意 11m drain)
kubectl -n ironagent rollout restart deploy/go-gateway
kubectl -n ironagent rollout status deploy/go-gateway
```

### 8.7 注意事项

- **单副本**:所有任务日志集中在一个 pod(排查方便);但 pod 重启日志会断流,崩溃现场用 `--previous`。滚动重启有 `DRAIN_TIMEOUT=11m` 的优雅排空,别强杀。
- **16 并发 worker + info 级**:日志量大,**永远带 `--since`/`--tail`**,别裸 `kubectl logs`。
- **JSON 日志、线上无 jq**:用 `grep '"key":"value"'` 精确匹配(slog 紧凑 JSON 无空格);分布统计 `grep -o '"key":"[^"]*"' | sort | uniq -c | sort -rn`;数值比较/排序用 `awk`。核心过滤字段:`msg`(事件名,最稳)、`level`、`task_id`、`fault_domain`、`user_id`。
- **复杂分析拉到本地再用 jq**:`kubectl logs deploy/go-gateway --since=1h > gw.log` 下载到有 jq 的机器分析——应急用线上 grep 快,复杂排序/聚合本地 jq 准。
- **如果接了 ELK / Loki / Grafana**:上面的 grep 条件可翻译成平台查询(如 Loki LogQL `{app="go-gateway"} | json | msg="gateway_queued_task_terminal_failed"`),用平台查历史 + 告警比 `kubectl logs` 更适合长期。`kubectl logs` 适合实时和应急。

## 9. 环境变量完整字典(99 个 `GO_GATEWAY_*`)

> 全部从 `internal/config/config.go` 提取——默认值追到 `Load()`、校验约束追到 `Validate()`,**逐个核对、未编造**。标记:**⚠️=真必填**(无默认值、不设则启动直接失败)。
> **注入方式**:普通项在 configmap `go-gateway-config`,密钥在 secret `go-gateway-secret`;容器 entrypoint 还对部分 `TASK_*`/`CALLBACK_*`/`RUNTIME_*` 设了**兜底默认**(见 `go-gateway/Dockerfile`,configmap 未设时才生效)。**「默认」列写的是 `Load()` 里的代码默认,不含 entrypoint/configmap 覆盖。**
> Python 控制面的 `IRON_AGENT_*` 变量完整字典见仓库根 `docs/environment-variables.md`,本节不重复。

### 9.0 上线必设速查

**🔒 8 个密钥**(放 secret `go-gateway-secret`,**切勿进 configmap 或日志**):

| 密钥 | 用途 |
|---|---|
| `GO_GATEWAY_API_KEYS` 或 `GO_GATEWAY_SCOPED_API_KEYS` | 对外 API 鉴权(二选一至少有一个;**拒绝弱默认值** change-me/admin-key/public-key 等) |
| `GO_GATEWAY_INTERNAL_SERVICE_TOKEN` | 内部服务间鉴权(审批闭环);可由 `IRONAGENT_*`/`IRON_AGENT_*` 同名变量兜底 |
| `GO_GATEWAY_HMAC_SECRET` | HMAC 请求签名校验 |
| `GO_GATEWAY_REDIS_PASSWORD` | Redis 密码 |
| `GO_GATEWAY_CALLBACK_SECRET` | 回调(webhook)签名 |
| `GO_GATEWAY_PROXY_SESSION_SECRET` | 同步 runtime 代理会话 |
| `GO_GATEWAY_PRESIGN_SECRET` | 预签名 URL |

**⚠️ 真必填(无默认值,不设启动直接失败)**:下表标 ⚠️ 的——`API_KEYS` 或 `SCOPED_API_KEYS`(二选一)、`INTERNAL_SERVICE_TOKEN`。
> 其余变量即使 `Validate()` 有约束(如各超时/连接数"必须为正"),都**有默认值兜底**,不设也能启动——故不标 ⚠️;但生产环境建议在 configmap 里显式配置关键项。
> 提示:`TASK_WORKER_ENABLED` 代码默认 **false**,但线上 configmap 设为 **true**——所以那一组 `TASK_WORKER_*` 的校验在生产是生效的。
### 9.1 任务 Worker / 队列 / 进度回调

| 变量 | 类型 | 默认 | 必填 | 含义 |
|---|---|---|---|---|
| `GO_GATEWAY_TASK_WORKER_ENABLED` | bool | `false` | · | 是否启用任务队列 worker(总开关) · **约束**:为 true 时才会校验其余 TASK_WORKER_* 配置 |
| `GO_GATEWAY_TASK_WORKER_ID` | string | `go-gateway-task-worker` | · | 任务 worker 的标识 ID(用于租约归属) |
| `GO_GATEWAY_TASK_WORKER_CONCURRENCY` | int | `1` | · | worker 并发处理任务的协程数 · **约束**:启用 worker 时必须为正数(>0) |
| `GO_GATEWAY_TASK_WORKER_INTERVAL` | duration | `250ms` | · | worker 轮询任务队列的间隔 · **约束**:启用 worker 时必须为正数(>0) |
| `GO_GATEWAY_TASK_WORKER_CLAIM_LIMIT` | int | `1` | · | 单次轮询最多认领的任务数 · **约束**:启用 worker 时必须为正数(>0) |
| `GO_GATEWAY_TASK_WORKER_LEASE_DURATION` | duration | `30s` | · | 任务租约时长(持有任务的有效期) · **约束**:启用 worker 时必须为正数(>0) |
| `GO_GATEWAY_TASK_WORKER_DRAIN_TIMEOUT` | duration | `11m` | · | 优雅停机时等待在途任务排空的超时 · **约束**:启用 worker 时必须非负(>=0) |
| `GO_GATEWAY_TASK_WORKER_GATEWAY_DEFAULT_DEADLINE_BACKSTOP` | duration | `20m` | · | 任务无显式截止时的网关兜底执行上限 · **约束**:启用 worker 时必须非负(>=0) |
| `GO_GATEWAY_TASK_WORKER_MAX_ATTEMPTS` | int | `3` | · | 任务失败后的最大尝试次数 · **约束**:启用 worker 时必须为正数(>0) |
| `GO_GATEWAY_TASK_WORKER_RETRY_DELAY` | duration | `1s` | · | 任务重试前的等待延迟 |
| `GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_MAX_ATTEMPTS` | int | `100` | · | 等待运行时容量时的最大重试次数 · **约束**:启用 worker 时必须非负(>=0);且不能与 CAPACITY_WAIT_TIMEOUT 同时为 0 |
| `GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_TIMEOUT` | duration | `30m` | · | 等待运行时容量的总超时上限 · **约束**:启用 worker 时必须非负(>=0);且不能与 CAPACITY_WAIT_MAX_ATTEMPTS 同时为 0 |
| `GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_CALLBACK_INTERVAL` | duration | `5m` | · | 容量等待期间回调通知的间隔 · **约束**:启用 worker 时必须非负(>=0) |
| `GO_GATEWAY_TASK_WORKER_STALE_PROGRESS_TIMEOUT` | duration | `600s` | · | 任务进度停滞多久后判定为僵死的超时 · **约束**:启用 worker 时必须非负(>=0) |
| `GO_GATEWAY_TASK_WORKER_ACTIVE_SOUTHBOUND_DEFER_TIMEOUT` | duration | `60m` | · | 已有 southbound task 保持 active/claimed 时,worker defer 重查的墙钟上限;超过后终态失败为 `runtime_active_southbound_timeout`;0 表示禁用 · **约束**:启用 worker 时必须非负(>=0) |
| `GO_GATEWAY_TASK_QUEUE_MAX_PENDING` | int | `0` | · | 队列全局待处理任务上限(0 表示不限) |
| `GO_GATEWAY_TASK_QUEUE_MAX_PENDING_PER_ASSISTANT` | int | `0` | · | 每个助手的待处理任务上限(0 表示不限) · **约束**:启用 worker 时必须非负(>=0) |
| `GO_GATEWAY_TASK_PROGRESS_CALLBACK_ENABLED` | bool | `false` | · | 是否启用任务进度回调 · **约束**:为 true 时才会校验其余 PROGRESS_CALLBACK_* 配置 |
| `GO_GATEWAY_TASK_PROGRESS_CALLBACK_BATCH_SIZE` | int | `5` | · | 进度回调每批上报的步骤数 · **约束**:启用进度回调时必须为正数(>0) |
| `GO_GATEWAY_TASK_PROGRESS_CALLBACK_INTERVAL` | duration | `90s` | · | 进度回调上报的间隔 · **约束**:启用进度回调时必须为正数(>0) |
| `GO_GATEWAY_TASK_PROGRESS_CALLBACK_STEP_MAX_CHARS` | int | `150` | · | 单条进度步骤文本的最大字符数 · **约束**:启用进度回调时必须为正数(>0) |
| `GO_GATEWAY_TASK_PROGRESS_CALLBACK_STATE_TTL` | duration | `30m` | · | 进度回调状态的缓存存活时间 · **约束**:启用进度回调时必须为正数(>0) |
### 9.2 回调 / Outbox

| 变量 | 类型 | 默认 | 必填 | 含义 |
|---|---|---|---|---|
| `GO_GATEWAY_DEFAULT_CALLBACK_URL` | string | `(none)` | · | 默认 webhook 回调投递地址 · **约束**:设置后必须同时配置 GO_GATEWAY_CALLBACK_ALLOWED_HOSTS |
| `GO_GATEWAY_CALLBACK_CLIENT_ID` | string | `ironagent-go-gateway` | · | 回调请求标识的客户端 ID |
| `GO_GATEWAY_CALLBACK_SECRET` | string | `(none)` | · | 回调签名用的密钥 |
| `GO_GATEWAY_CALLBACK_WORKER_ID` | string | `(none)` | · | 回调 outbox worker 实例标识 |
| `GO_GATEWAY_CALLBACK_ALLOWED_HOSTS` | list | `(none)` | · | 回调目标主机白名单(防 SSRF) · **约束**:当设置了 DEFAULT_CALLBACK_URL 时必填,逗号分隔 |
| `GO_GATEWAY_CALLBACK_ALLOW_PRIVATE_HOSTS` | bool | `false` | · | 是否允许回调到私网/内网地址 |
| `GO_GATEWAY_CALLBACK_FIRE_AND_FORGET` | bool | `false` | · | 回调是否只投递不等结果 |
| `GO_GATEWAY_CALLBACK_ACCEPT_ANY_RESPONSE` | bool | `false` | · | 是否接受任意 HTTP 响应为成功 |
| `GO_GATEWAY_CALLBACK_REQUIRED` | bool | `false` | · | 回调是否为任务完成的必要条件 |
| `GO_GATEWAY_CALLBACK_OUTBOX_WORKER_ENABLED` | bool | `true` | · | 是否启用事务性 outbox 投递 worker |
| `GO_GATEWAY_CALLBACK_RETRY_DELAY` | duration | `250ms` | · | 回调失败后首次重试的延迟 |
| `GO_GATEWAY_CALLBACK_MAX_RETRY_DELAY` | duration | `30s` | · | 回调重试退避的延迟上限 |
| `GO_GATEWAY_CALLBACK_STORE_TIMEOUT` | duration | `2s` | · | 回调 store 持久化操作超时 · **约束**:必须为正数 |
| `GO_GATEWAY_CALLBACK_HTTP_TIMEOUT` | duration | `10s` | · | 单次回调 HTTP 请求超时 · **约束**:必须为正数 |
| `GO_GATEWAY_CALLBACK_WORKER_INTERVAL` | duration | `250ms` | · | outbox worker 轮询投递的间隔 |
| `GO_GATEWAY_CALLBACK_LEASE_DURATION` | duration | `30s` | · | worker 抢占回调记录的租约时长 · **约束**:非 fire-and-forget 时必须大于 CALLBACK_HTTP_TIMEOUT |
| `GO_GATEWAY_CALLBACK_MAX_ATTEMPTS` | int | `3` | · | 单条回调的最大投递尝试次数 |
| `GO_GATEWAY_CALLBACK_QUEUE_SIZE` | int | `1024` | · | 回调内存队列容量 |
| `GO_GATEWAY_CALLBACK_ENQUEUE_MAX_IN_FLIGHT` | int | `4` | · | 入队阶段并发处理上限 · **约束**:必须为正数 |
| `GO_GATEWAY_CALLBACK_DELIVERY_CONCURRENCY` | int | `4` | · | 回调投递的并发数 · **约束**:必须为正数 |
| `GO_GATEWAY_CALLBACK_PENDING_LIMIT` | int | `100` | · | 单批拉取待投递回调的数量上限 |
| `GO_GATEWAY_CALLBACK_DATABASE_MAX_OPEN_CONNS` | int | `8` | · | 回调专用 DB 连接池最大打开连接数 · **约束**:必须为正数 |
| `GO_GATEWAY_CALLBACK_DATABASE_MAX_IDLE_CONNS` | int | `2` | · | 回调专用 DB 连接池最大空闲连接数 · **约束**:必须非负,且不大于 CALLBACK_DATABASE_MAX_OPEN_CONNS |
### 9.3 Redis

| 变量 | 类型 | 默认 | 必填 | 含义 |
|---|---|---|---|---|
| `GO_GATEWAY_REDIS_MODE` | string | `standalone` | · | Redis 连接模式,取值 standalone 或 sentinel · **约束**:不可为空,且必须是 standalone 或 sentinel,否则报错 |
| `GO_GATEWAY_REDIS_ADDR` | string | `(none)` | · | standalone 模式下的 Redis 服务器地址 · **约束**:MODE=standalone 且 Redis 已启用时必填,否则报错 |
| `GO_GATEWAY_REDIS_SENTINEL_ADDRS` | list | `(none)` | · | sentinel 模式下的哨兵节点地址列表 · **约束**:MODE=sentinel 且 Redis 已启用时不可为空,否则报错 |
| `GO_GATEWAY_REDIS_SENTINEL_MASTER` | string | `(none)` | · | sentinel 模式下的主节点名称 · **约束**:MODE=sentinel 且 Redis 已启用时不可为空,否则报错 |
| `GO_GATEWAY_REDIS_USERNAME` | string | `(none)` | · | Redis 连接用户名(ACL) |
| `GO_GATEWAY_REDIS_PASSWORD` | string | `(none)` | · | Redis 连接密码(密钥,勿明文记录) |
| `GO_GATEWAY_REDIS_DB` | int | `0` | · | 选用的 Redis 数据库编号 · **约束**:Redis 已启用时必须非负,否则报错 |
| `GO_GATEWAY_REDIS_KEY_PREFIX` | string | `go-gateway:` | · | Redis 键名统一前缀 |
| `GO_GATEWAY_REDIS_TIMEOUT` | duration | `500ms` | · | Redis 操作超时时间 · **约束**:Redis 已启用时必须为正值,否则报错 |
| `GO_GATEWAY_REDIS_MAX_OPEN_CONNS` | int | `16` | · | Redis 连接池最大连接数 · **约束**:Redis 已启用时必须为正值,否则报错 |
| `GO_GATEWAY_REDIS_MAX_IDLE_CONNS` | int | `8` | · | Redis 连接池最大空闲连接数 · **约束**:Redis 已启用时必须非负,且不得大于 MAX_OPEN_CONNS |
| `GO_GATEWAY_REDIS_REQUIRED` | bool | `false` | · | 是否强制要求 Redis 连接可用 · **约束**:为 true 时 Redis 必须已启用(配置了地址),否则报错 |
### 9.4 Runtime 池 / 同步代理熔断

| 变量 | 类型 | 默认 | 必填 | 含义 |
|---|---|---|---|---|
| `GO_GATEWAY_RUNTIME_BOOTSTRAP_ASSISTANTS` | list | `(none)` | · | 启动时预注册到 runtime 池的助手 ID 列表 · **约束**:非空时要求 SYNC_INTERVAL 为正值,否则报错 |
| `GO_GATEWAY_RUNTIME_DEFAULT_CAPACITY` | int | `1` | · | runtime 实例默认并发容量 |
| `GO_GATEWAY_RUNTIME_DISCOVER_ASSISTANTS` | bool | `false` | · | 是否自动发现并同步 runtime 助手 · **约束**:开启时要求 SYNC_INTERVAL 为正值,否则报错 |
| `GO_GATEWAY_RUNTIME_EXECUTION_ENABLED` | bool | `false` | · | 是否启用 runtime 执行能力 |
| `GO_GATEWAY_RUNTIME_EXECUTION_TIMEOUT` | duration | `10m0s` | · | 单次 runtime 执行的超时时长 · **约束**:必须为正值,否则报错 |
| `GO_GATEWAY_RUNTIME_HEARTBEAT_TTL` | duration | `1m0s` | · | runtime 注册表心跳存活过期时间 |
| `GO_GATEWAY_RUNTIME_SYNC_INTERVAL` | duration | `30s` | · | runtime 池同步轮询间隔 · **约束**:启用预注册或自动发现时必须为正值 |
| `GO_GATEWAY_PROXY_CIRCUIT_OPEN_DURATION` | duration | `30s` | · | 代理熔断器打开后的保持时长 · **约束**:当 FAILURE_THRESHOLD 大于 0 时必须为正值 |
| `GO_GATEWAY_PROXY_FAILURE_THRESHOLD` | int | `3` | · | 触发代理熔断的连续失败次数阈值 · **约束**:必须为非负值,0 表示禁用熔断 |
| `GO_GATEWAY_PROXY_MAX_IN_FLIGHT` | int | `0` | · | 同步代理并发请求上限 · **约束**:必须为非负值,0 表示不限制 |
| `GO_GATEWAY_PROXY_SESSION_SECRET` | string | `(none)` | · | 代理会话签名密钥 |
| `GO_GATEWAY_PROXY_SESSION_TTL` | duration | `30m0s` | · | 代理会话有效期 |
### 9.5 数据库 / 聊天限流

| 变量 | 类型 | 默认 | 必填 | 含义 |
|---|---|---|---|---|
| `GO_GATEWAY_DATABASE_URL` | string | `(none)` | · | 主数据库连接 DSN/URL |
| `GO_GATEWAY_DATABASE_MAX_OPEN_CONNS` | int | `40` | · | 主库连接池最大打开连接数 · **约束**:必须为正数(<=0 拒绝);且 MaxIdleConns 不得超过本值 |
| `GO_GATEWAY_DATABASE_MAX_IDLE_CONNS` | int | `10` | · | 主库连接池最大空闲连接数 · **约束**:必须非负(<0 拒绝);且不得大于 GO_GATEWAY_DATABASE_MAX_OPEN_CONNS |
| `GO_GATEWAY_DATABASE_CONN_MAX_LIFETIME` | duration | `30m` | · | 主库连接最大存活时长 · **约束**:必须非负(<0 拒绝);0 表示不限制 |
| `GO_GATEWAY_DATABASE_CONN_MAX_IDLE_TIME` | duration | `5m` | · | 主库连接最大空闲时长 · **约束**:必须非负(<0 拒绝);0 表示不限制 |
| `GO_GATEWAY_CHAT_RATE_LIMIT_ENABLED` | bool | `false` | · | 是否启用聊天接口限流 · **约束**:为 true 时必须已启用 Redis,否则报错 |
| `GO_GATEWAY_CHAT_RATE_LIMIT_FAIL_CLOSED` | bool | `true` | · | 限流器故障时是否拒绝请求(fail-closed) |
| `GO_GATEWAY_CHAT_RATE_LIMIT_MAX` | int | `0` | · | 限流窗口内允许的最大请求数 · **约束**:仅当限流启用时校验:必须为正数(<=0 拒绝) |
| `GO_GATEWAY_CHAT_RATE_LIMIT_WINDOW` | duration | `1s` | · | 限流统计的时间窗口长度 · **约束**:仅当限流启用时校验:必须为正数(<=0 拒绝) |
### 9.6 服务器 / 鉴权 / URL / 杂项

| 变量 | 类型 | 默认 | 必填 | 含义 |
|---|---|---|---|---|
| `GO_GATEWAY_ADMIN_AUTH_DISABLED` | bool | `false` | · | 是否关闭管理端接口鉴权 |
| `GO_GATEWAY_ADMIN_BASE_URL` | string | `http://127.0.0.1:8501` | · | 管理端(决策面)服务基址 URL · **约束**:不能为空,否则报错 |
| `GO_GATEWAY_API_KEYS` | list | `(none)` | ⚠️ | 网关公共 API 访问密钥列表(逗号分隔) · **约束**:与 SCOPED_API_KEYS 至少有一个非空;不得含弱默认密钥(public-key/admin-key/change-me 等) |
| `GO_GATEWAY_ASSISTANT_AVAILABILITY_CACHE_TTL` | duration | `30s` | · | 助手可用性缓存的存活时间 · **约束**:启用 Redis 时必须为正 |
| `GO_GATEWAY_ASSISTANT_CACHE_TTL` | duration | `60s` | · | 助手信息缓存的存活时间 · **约束**:启用 Redis 时必须为正 |
| `GO_GATEWAY_CONFIG_FILE` | string | `/etc/ironagent/go-gateway.json` | · | JSON 配置文件路径 · **约束**:显式指定时文件需可读,否则报错 |
| `GO_GATEWAY_HMAC_ALLOWED_SKEW` | duration | `5m` | · | HMAC 签名允许的时间偏移窗口 |
| `GO_GATEWAY_HMAC_SECRET` | string | `(none)` | · | 密钥:HMAC 请求签名校验密钥 |
| `GO_GATEWAY_HTTP_TIMEOUT` | duration | `10s` | · | 出站 HTTP 请求超时时间 |
| `GO_GATEWAY_INTERNAL_SERVICE_TOKEN` | string | `(required)` | ⚠️ | 密钥:内部服务间调用令牌(审批闭环用) · **约束**:不能为空,否则报错 |
| `GO_GATEWAY_LISTEN_ADDR` | string | `:8080` | · | HTTP 服务监听地址 |
| `GO_GATEWAY_LOG_LEVEL` | string | `info` | · | 应用日志级别 · **约束**:只能为 debug/info/warn/error 或数字,否则报错 |
| `GO_GATEWAY_MAX_UPLOAD_BYTES` | int | `52428800` | · | 单次上传最大字节数(默认50MB) |
| `GO_GATEWAY_MODE` | string | `polling` | · | 网关运行模式 · **约束**:只能为 polling,否则报错 |
| `GO_GATEWAY_PRESIGN_SECRET` | string | `(none)` | · | 密钥:预签名 URL 签发密钥 |
| `GO_GATEWAY_PRESIGN_TTL` | duration | `15m` | · | 预签名 URL 有效期 |
| `GO_GATEWAY_PROCESS_ROLE` | string | `all` | · | 进程角色(全功能/API/任务/回调) · **约束**:只能为 all/api/task-worker/callback-worker |
| `GO_GATEWAY_PUBLIC_AUTH_DISABLED` | bool | `false` | · | 是否关闭公共接口鉴权 |
| `GO_GATEWAY_PUBLIC_BASE_URL` | string | `(none)` | · | 对外公开的网关基址 URL |
| `GO_GATEWAY_SCOPED_API_KEYS` | list | `(none)` | ⚠️ | 带作用域的 API 密钥列表(key:scope) · **约束**:格式须为 key:scope[,scope];与 API_KEYS 至少有一个非空;不得含弱默认密钥 |
| `GO_GATEWAY_SECURITY_BASE_URL` | string | `http://127.0.0.1:8000` | · | 安全决策面服务基址 URL · **约束**:不能为空,否则报错 |

---

## 10. DB 侧 SQL 排障 + dead-letter replay

> 日志只回答"发生过什么",任务/回调的**持久化现场**在 MySQL。上游反馈"没收到 callback"或"任务一直没完成"时,按下面顺序查库,不要先猜 runtime 或 callback。
> (源自 5-28 整改手册,已归档至 `docs/archive/historical/upload-chat-callback-remediation-manual.md`;以下 SQL 的表名/字段已对照 migrations `010_go_gateway_task_store` / `018_gateway_webhook_delivery_packets` / `022_collaboration_repository_tables` 逐一核实。)

### 10.1 按 request_id 查接收状态

```sql
SELECT gt.task_id, gt.request_id, gt.user_id, gt.status, gt.stage,
       gt.southbound_task_id, gt.callback_url, gt.created_at, gt.updated_at,
       gik.idempotency_key
FROM gateway_tasks gt
LEFT JOIN gateway_idempotency_keys gik ON gik.task_id = gt.task_id
WHERE gt.request_id = '<REQUEST_ID>' OR gik.idempotency_key = '<REQUEST_ID>'
ORDER BY gt.created_at DESC;
```

判断:

- 没有记录:请求未被接受,回看 HTTP 同步错误(§4 ①)。
- 有记录但 `callback_url` 为空:callback 配置缺失或未启用。
- status 长期 `pending_claim/processing`:继续查 queue 和 southbound。

### 10.2 查 queue

```sql
SELECT task_id, status, attempts, last_error, next_attempt_at,
       locked_by, locked_until, created_at, updated_at
FROM gateway_task_queue
WHERE task_id = '<TASK_ID>';

SELECT status, COUNT(*) AS cnt
FROM gateway_task_queue
GROUP BY status;
```

判断:

- `admitting` 长期存在:202 持久化屏障失败(任务建了但没激活)。
- `queued` 长期存在:worker 未消费或容量不足(对照 §5-E)。
- `processing` 且 `locked_until` 已过:worker crash 或长任务锁过期。
- `retry` 持续增长:下游或 runtime 不稳定(对照 §2 归因)。

### 10.3 查 southbound task 和 claim

```sql
SELECT task_id, southbound_task_id, status, stage, task_json, updated_at
FROM gateway_tasks
WHERE task_id = '<TASK_ID>';

SELECT id, status, result_object_id, result_workspace_file_id,
       started_at, completed_at, failed_at,
       failed_reason_code, failed_reason_text
FROM collaboration_tasks
WHERE id = '<SOUTHBOUND_TASK_ID>';

SELECT id, task_id, assistant_id, status, claimed_at, lease_expires_at
FROM collaboration_claims
WHERE task_id = '<SOUTHBOUND_TASK_ID>'
ORDER BY claimed_at DESC;
```

判断:

- `southbound_task_id` 为空:失败在创建 collaboration task 前。
- claim 的 `lease_expires_at` 已过:按 claim 租约过期处理,不伪造 completed。
- collaboration completed 但 gateway 没更新:查 queue worker 保存结果(`saveQueuedTaskResult`)。

### 10.4 查 callback outbox / delivery / dead-letter

```sql
SELECT event_id, task_id, event_seq, event_type, status, attempts,
       last_error, next_attempt_at, locked_by, locked_until,
       delivered_at, acked_at, created_at, updated_at
FROM gateway_event_outbox
WHERE task_id = '<TASK_ID>'
ORDER BY event_seq;

SELECT event_id, callback_url, attempt, status, error,
       response_status_code, response_body_truncated, duration_ms, created_at
FROM gateway_webhook_deliveries
WHERE event_id = '<EVENT_ID>'
ORDER BY attempt;

SELECT event_id, task_id, event_type, callback_url, attempts, last_error, failed_at
FROM gateway_dead_letters
WHERE task_id = '<TASK_ID>'
ORDER BY failed_at DESC;
```

判断:

- `task.completed` 未生成:主任务还没终态化(回到 10.2/10.3)。
- `task.completed` pending/sending:callback worker 或 outbox lease 问题(对照 §5-C 的 `callback_outbox_process_failed`)。
- `task.completed` dead:上游 receiver 或网络问题,修复后按 10.5 replay。
- delivered 但上游说没收到:查 `gateway_webhook_deliveries.request_packet/response_packet/response_status_code` 和上游日志。

### 10.5 dead-letter replay 手册

replay 前置检查:

```sql
SELECT event_id, task_id, event_type, callback_url, attempts, last_error, failed_at
FROM gateway_dead_letters
WHERE task_id = '<TASK_ID>'
ORDER BY failed_at DESC;

SELECT event_id, task_id, event_seq, event_type, status, attempts,
       last_error, delivered_at, acked_at
FROM gateway_event_outbox
WHERE event_id = '<EVENT_ID>';
```

执行原则:

1. 先确认上游 receiver 已恢复。
2. 确认上游按 `taskId + status + stage + event_seq` 幂等(重复 replay 不产生业务副作用)。
3. 不直接改 `gateway_event_outbox.payload_json`:已 delivered 的事件不会因为改 payload 自动重发;修复后必须走显式 replay。
4. 用 replay 接口(封装 `ReplayDeadLetter()`),不要手工改库:

```bash
# 列出 dead-letter(需 debug scope key)
curl -sS -H "X-API-Key: $DEBUG_KEY" "$GATEWAY/api/v1/webhooks/dead-letter"

# 重放单条(需 admin scope key;只接受 outbox status='dead' 的事件)
curl -sS -X POST -H "X-API-Key: $ADMIN_KEY" \
  "$GATEWAY/api/v1/webhooks/dead-letter/<EVENT_ID>/replay"
```

5. replay 行为:outbox 该事件重置为 `pending`(attempts 归零),`gateway_dead_letters` 中该行**被删除**——历史审计靠 `gateway_webhook_deliveries` 的逐 attempt 记录。
6. replay 后复查:`gateway_event_outbox.status`(应走向 delivered)、`gateway_webhook_deliveries`(新 attempt 的 `response_status_code`)、`gateway_dead_letters`(确认未再次死信)。

---

*本文由 gateway 源码自动抽取生成,事件名/字段/取值均可直接 grep。如代码变更,以代码为准。*
