# gateway-260523-1643: Gateway 数据库连接池保护与 Redis 预备方案 - 技术方案

- 模块：gateway
- 创建日期：2026-05-23 16:43
- 编写人：Codex
- 最近修改人：Codex
- 最近修改日期：2026-05-24
- 等级：S2
- 状态：implemented
- 状态说明：当前实现证据覆盖主连接池配置、callback/outbox 独立 DB pool、callback outbox 短超时与入队并发上限、DB pool 指标、既有队列背压入口、“不迁移 outbox”的边界、Redis HA StatefulSet/PV/PVC + 官方 Sentinel、Gateway Redis assistant cache 和 chat admission limit；按用户 health、runtime status Redis 化、callback latest cache 和 emergency buffer 均为 planned/deferred。

## 背景

- 产品 spec：`./PRODUCT.md`
- 当前事实源文档：本 spec 单独固化 Gateway DB 保护、Redis P0 cache/admission limit 和 Redis 后续边界。
- 当前代码入口：`go-gateway/internal/config/config.go`、`go-gateway/internal/taskstore/mysql.go`、`go-gateway/internal/core/service.go`、`go-gateway/internal/webhook/dispatcher.go`、`go-gateway/internal/httpapi/router.go`、`go-gateway/cmd/server/main.go`。
- 当前限制：本轮已在 `go-gateway` module 目录执行 Go 单测，并完成真实镜像构建、K8s 部署、500 并发 `/api/v1/chat` 压测和 callback outbox 统计；全局多副本连接预算、故障注入、Redis 容量/淘汰策略和长期 soak 仍未验证。

## 影响范围

- Go Gateway：新增 MySQL 主连接池配置、callback/outbox 独立 DB pool 配置、启动校验、MySQL store 初始化参数、Redis assistant cache 和 chat admission limit。
- Deploy/Ops：新增 configmap 和文件配置中的主池、callback/outbox 池默认值、Redis standalone/Sentinel 连接参数和 chat 限流参数；验证 overlay 包含 Redis HA StatefulSet、Headless Service、Sentinel Service、PV/PVC；部署验收必须补全局连接预算和 Redis/Sentinel 连通性。
- Task queue：既有 `GO_GATEWAY_TASK_QUEUE_MAX_PENDING`、worker claim limit、queue stats 和 `429 queue_full` 必须纳入 P0 验收。
- Callback：不改变 payload 和 outbox 存储；MySQL 模式下 callback/outbox 使用独立受限 DB pool，outbox 入队默认 `2s` 短超时和 `4` 并发上限，减少 callback 高峰抢占主任务链路连接和长期占满自身小池的风险；outbox 状态机和 DB fail-closed 语义必须补齐。
- Health：本次不改变接口逻辑；后续 Redis/按用户查询作为目标方案固化，但不得误写为已实现。

## 修改方案

- 已有代码修改点：
  - `Config` 新增 `DatabaseMaxOpenConns`、`DatabaseMaxIdleConns`、`DatabaseConnMaxLifetime`、`DatabaseConnMaxIdleTime`、`CallbackDatabaseMaxOpenConns`、`CallbackDatabaseMaxIdleConns`。
  - `Load()` 默认设置 `40/10/30m/5m`。
  - 配置文件支持 `database_max_open_conns`、`database_max_idle_conns`、`database_conn_max_lifetime`、`database_conn_max_idle_time`。
  - 环境变量支持 `GO_GATEWAY_DATABASE_MAX_OPEN_CONNS`、`GO_GATEWAY_DATABASE_MAX_IDLE_CONNS`、`GO_GATEWAY_DATABASE_CONN_MAX_LIFETIME`、`GO_GATEWAY_DATABASE_CONN_MAX_IDLE_TIME`。
  - callback/outbox 连接池默认 `8/2`，配置文件支持 `callback_database_max_open_conns`、`callback_database_max_idle_conns`，环境变量支持 `GO_GATEWAY_CALLBACK_DATABASE_MAX_OPEN_CONNS`、`GO_GATEWAY_CALLBACK_DATABASE_MAX_IDLE_CONNS`。
  - callback/outbox store 操作默认短超时 `2s`，配置文件支持 `callback_store_timeout`，环境变量支持 `GO_GATEWAY_CALLBACK_STORE_TIMEOUT`。
  - callback/outbox 入队默认最大并发 `4`，配置文件支持 `callback_enqueue_max_in_flight`，环境变量支持 `GO_GATEWAY_CALLBACK_ENQUEUE_MAX_IN_FLIGHT`。
  - callback/outbox delivery 默认并发 `4`，配置文件支持 `callback_delivery_concurrency`，环境变量支持 `GO_GATEWAY_CALLBACK_DELIVERY_CONCURRENCY`。
  - 启动校验拒绝主池或 callback/outbox 池 `max_open <= 0`，避免 Go `sql.DB` 退回无限连接语义。
  - MySQL store 新增 `DBPoolConfig` 和 `NewMySQLStoreFromURLWithPool()`，在 `PingContext()` 前设置连接池参数。
  - Server 启动时分别创建主任务 store 和 callback/outbox store；主 store 承担 tasks/queue/nonce/runtime，callback/outbox store 承担 `gateway_event_outbox`。
  - `/api/v1/metrics` 继续保留 `db_pool` 兼容字段，并新增 `db_pools.primary`、`db_pools.callback_outbox`；Prometheus 新增 `go_gateway_db_pool_by_pool_*{pool=...}`。
  - `Config` 新增 `RedisMode`、`RedisAddr`、`RedisSentinelAddrs`、`RedisSentinelMaster`、`RedisUsername`、`RedisPassword`、`RedisDB`、`RedisKeyPrefix`、`RedisTimeout`、`RedisMaxOpenConns`、`RedisMaxIdleConns`、`RedisRequired`、`AssistantCacheTTL`、`AssistantAvailabilityCacheTTL`、`ChatRateLimitEnabled`、`ChatRateLimitMax`、`ChatRateLimitWindow`、`ChatRateLimitFailClosed`。
  - `internal/rediscache` 使用 `github.com/redis/go-redis/v9`，standalone 模式走 `redis.NewClient`，Sentinel 模式走 `redis.NewFailoverClient`；不保留自研 Redis RESP client 或自研 Sentinel 发现逻辑。
  - `core.Service` 通过接口接入 Redis，不在业务层绑定具体 client；当前 Redis key 为 `go-gateway:assistant_binding:{user_id}`、`go-gateway:assistant_available:{assistant_id}`、`go-gateway:rate:chat:{client_or_user}:{window}`。
  - `/api/v1/chat` 在 MySQL 幂等查询和任务入队前执行 chat admission limit；限流命中或 fail-closed Redis 错误返回 `429 queue_full`。
  - `DispatchAsync()` 使用请求上下文而不是 `context.Background()` 入队 outbox；MySQL outbox 的 `NextEventSeq()`、`Enqueue()`、`ClaimPending()`、`MarkDelivered()`、`MarkAttempt()`、`MarkDead()`、ACK、replay 和 dead-letter 查询都受 store timeout 约束。
  - Prometheus 不再通过 `DeadLetters()` 查询持久化 outbox 计数；持久化模式下 dead-letter 总量应通过 outbox 表、事件指标或单独后台采集获取。
  - 部署 configmap 和文件 configmap 固化默认值。
  - `prod-k8s-single-entry-nodeport` overlay 新增 Redis `redis-ha` StatefulSet、`redis-ha-headless` Headless Service、`redis-sentinel` Service/Deployment、每副本 `redis-data-redis-ha-{0,1}` PVC 和静态 local PV。
  - Redis 与 Sentinel 镜像均固定为 `redis:7.2.10-alpine`；Redis 数据目录 `/data` 挂载 PVC，开启 AOF `appendonly yes` 与 `appendfsync everysec`；`REDIS_USERNAME`、`REDIS_PASSWORD` 从 `control-plane-shared-secret` 注入。
  - Gateway HA 模式配置 `GO_GATEWAY_REDIS_MODE=sentinel`、`GO_GATEWAY_REDIS_SENTINEL_ADDRS=redis-sentinel:26379`、`GO_GATEWAY_REDIS_SENTINEL_MASTER=mymaster`；`redis:6379` 仅保留兼容 Service，不作为故障转移后的权威写入口。
- 需要纳入 P0 验收但不属于本次新增代码的既有能力：
  - `GO_GATEWAY_TASK_QUEUE_MAX_PENDING` 达到阈值后返回 `429 queue_full`。
  - `GO_GATEWAY_TASK_WORKER_CLAIM_LIMIT` 限制单轮 worker claim 数量。
  - `GET /api/v1/task-queue`、`/api/v1/metrics`、`/metrics`、`/api/v1/metrics/prometheus` 暴露 queue/runtime/webhook 等基础指标。
- 不修改的范围：
  - 不迁移 `gateway_tasks` 和 `gateway_event_outbox`。
  - 不改 callback payload 格式。
  - 不改 callback fire-and-forget 语义。
  - 不引入 Redis Stream callback buffer，不迁移 outbox。
- 兼容性处理：
  - 旧的 `NewMySQLStoreFromURL()` 保留，默认走空 `DBPoolConfig`，便于旧测试或旧调用继续使用。
  - 配置文件和环境变量未设置时使用安全默认值。

## 实现记录

- 实现分支：`codex/hotfix-phase1-merge-admin-p0`
- 基线 commit：`207fc771`
- 当前复审目标：`46ad2da3 + working tree`
- 集成 commit：无
- 当前代码证据：B1、B2、B3、B4、B5 的 assistant cache/chat limit 部分、B6、B8、B10、B12 的背压入口、B16 的 DB pool stats 指标、B21 的 callback/outbox 独立 DB pool、B22 的 outbox store 短超时和入队并发上限、B23 的 Redis HA StatefulSet/PV/PVC + 官方 Sentinel 部署基础设施。
- 文档边界或 planned：B7、B9、B11、B13、B14、B15、B17、B18、B19、B20。
- 未包含范围：callback outbox 迁移、health 按用户查询改造、Redis runtime status/cache latest、Redis Stream emergency buffer、生产多副本连接预算验证、DB stats 告警。

## 验收记录

- 验收报告：`./TEST_REPORT.md`
- 验收分支：`codex/hotfix-phase1-merge-admin-p0`
- 验收目标：`46ad2da3 + working tree`
- 当前结论：partial / code-present

## 方案变更

### 接口

- API 变化：当前连接池提交无公开 API 变化。
- 已存在背压接口语义：task queue 达到 `GO_GATEWAY_TASK_QUEUE_MAX_PENDING` 时返回 `429 queue_full`；本 spec 要求把该路径纳入 P0 验收，而不是把反压只写成后续事项。
- Callback 变化：无；最终态 `files[]` 和 `download_url` 由既有链路继续生成。
- 请求/响应兼容性：无公开契约变更。

### 状态与持久化

- 状态机变化：当前代码不新增 outbox 状态机，但 spec 要求后续把 outbox 状态机验收化。
- 数据库/配置/文件变化：新增 Gateway 配置项；无数据库 schema 变更。
- 迁移或兼容行为：配置未显式设置时使用默认连接池保护；旧数据无需迁移。
- MySQL 权威边界：
  - `gateway_tasks` 是任务创建、最终态和 fallback 查询的权威事实。
  - `gateway_event_outbox` 是 callback 可靠投递、重试、dead-letter 和去重的权威事实。
  - Redis 只能缓存从权威事实派生的状态，不得作为任务或 outbox 的唯一事实源。
- DB fail-closed：
  - 任务与 outbox 权威写入失败时不得返回“任务已接收”。
  - callback worker 读写 outbox 失败时必须暂停、退避或保留 pending，不得只写日志丢事件。
  - HTTP 已发送但 `MarkDelivered` 失败时，不得声称可靠状态已闭环；后续恢复必须按 outbox 幂等重试处理。

### P0 保护门槛

P0 不是只设置单实例连接池。进入部署验收前必须同时满足：

1. 全局连接预算已记录：`gateway_replicas * (gateway_primary_max_open_conns + gateway_callback_outbox_max_open_conns) + admin_budget + runtime_or_health_budget + migration_or_ops_budget + reserved_connections <= mysql_max_connections * safety_factor`。
2. Gateway 主池和 callback/outbox 池参数已写入部署配置，并确认各自 `max_idle <= max_open`。
3. `GO_GATEWAY_CALLBACK_STORE_TIMEOUT`、`GO_GATEWAY_CALLBACK_ENQUEUE_MAX_IN_FLIGHT` 已按 callback 上游延迟、outbox worker 吞吐和 MySQL wait 指标确认。
4. `GO_GATEWAY_TASK_QUEUE_MAX_PENDING`、`GO_GATEWAY_TASK_WORKER_CLAIM_LIMIT`、`GO_GATEWAY_TASK_WORKER_INTERVAL` 已按压测结果确定。
5. 队列达到上限时新任务返回 `429 queue_full` 或等价明确错误。
6. `/readyz`、`/api/v1/health`、task queue metrics、outbox 状态和 MySQL 连接状态可以在压测中同时观测。

`GO_GATEWAY_TASK_QUEUE_MAX_PENDING=10000` 只能证明存在配置，不自动证明线上合理。最终阈值必须来自 worker 吞吐、DB 写入能力、callback 积压和可接受排队时间。

### Runtime 语义

- 变更何时生效：Gateway 重启或重新部署后生效。
- 是否支持热更新：不支持；连接池参数由启动配置决定。
- 助手容器是否必须重启：不需要。
- 对活跃会话或运行中任务的影响：部署重启期间按现有 K8s 滚动发布语义处理；任务/outbox 数据不变。
- health/runtime 状态流量：当前不改接口实现；后续必须禁止 public health 默认全量扫描，按用户或助手范围查询，并对 fallback polling 增加间隔、jitter 或等价抑制。

### Redis 后续目标

Redis 的目标是降读取压力、做前置 admission control、隔离非权威状态，不是替换 MySQL 的任务事实源。当前已接入 `/api/v1/chat` 的短窗口 rate limit，以及助手绑定/可用性短 TTL cache；下面能力仍必须拆成独立 spec 实施：

1. Phase R1：runtime status 与 health 缓存。
   - key：`go-gateway:runtime:v1:{assistant_id}`。
   - value 至少包含：`status`、`drain_mode`、`last_heartbeat_at`、`last_error`、`pod_name`、`updated_at`、`observed_at`、`source`、`expires_at` 或等价字段。
   - TTL：建议 90-180 秒，必须大于 runtime heartbeat 周期，且小于用户可接受的 health 陈旧时间。
   - 读取方必须按 `last_heartbeat_at + GO_GATEWAY_RUNTIME_HEARTBEAT_TTL` 重新判定 stale；Redis TTL 不得延长 MySQL registry 的健康有效期。
   - MySQL 仍保留 `gateway_runtimes` 作为启动恢复和管理审计事实源。
2. Phase R2：callback 最近快照与用户视图缓存。
   - key：`go-gateway:callback-latest:v1:{task_id}`，TTL 建议 24-72 小时；该值只能由 MySQL/outbox 派生。
   - key：`go-gateway:user-assistants:v1:{tenant_or_org}:{principal}:{scope_or_authz_version}:{user_id}` 或等价隔离维度，TTL 建议 30-120 秒。
   - 这些 key 只用于查询加速和排障展示，不参与 outbox 投递确认。
   - 必须定义 read miss、Redis timeout、eviction、write error、回源 MySQL、熔断窗口和响应中的 `source=redis/mysql/stale` 或等价标识。

禁止事项：

- 禁止把 callback outbox 的唯一事实放入 Redis。
- 禁止 Redis 不可用时阻断任务最终态持久化；若显式启用 chat admission fail-closed，则新建请求可返回 `429` 保护 MySQL。
- 禁止把 Redis 中的 health 缓存作为权限依据。
- 禁止使用无 TTL 的 runtime/callback 快照 key。
- 禁止在 R1/R2 中隐式使用 `XADD`、`XREADGROUP`、`XACK` 或 Redis Stream outbox。

### Redis Stream emergency buffer

本 spec 不实现也不暗含 Redis Stream emergency buffer。如果业务要求 MySQL 短暂不可用期间 callback 仍能推送，必须另开 S3 spec，至少定义：

- Redis AOF/HA、maxmemory、eviction policy、容量和 QPS 预算。
- `event_id` 幂等键、重复投递、顺序约束和乱序容忍。
- MySQL 恢复后的 outbox/delivery 回灌、审计补偿和冲突处理。
- dead-letter、重放、人工处理、过期丢弃和报警。
- DB 不可用、Redis 不可用、上游 callback 不可用三类故障组合的状态机。

### 权限与安全边界

- 认证变化：无。
- 授权变化：无。
- 谁可以操作：部署人员可以配置连接池；普通 API 调用方不能修改。
- 谁可以查看：按既有 Gateway/admin/API 规则。
- 谁不能触发：普通调用方不能触发 Redis flush、连接池变更或全局 health 扫描。
- 审计变化：无新增审计表。
- 租户/用户隔离变化：无。

### 部署与回滚

- 发布计划：构建新镜像，更新 Gateway deployment/configmap，滚动重启。
- 健康检查：部署后检查 `/api/v1/health`、`/readyz`、task queue、callback outbox 投递状态、MySQL `Threads_connected`、`Threads_running`、`Connection_errors_max_connections`。
- 可观测性：
  - MySQL：`SHOW GLOBAL STATUS LIKE 'Threads_connected'`、`Threads_running`、`Connection_errors_max_connections`。
  - Gateway DB pool：Go `sql.DB.Stats()` 等价指标，包括 `MaxOpenConnections`、`OpenConnections`、`InUse`、`Idle`、`WaitCount`、`WaitDuration`、`MaxIdleClosed`、`MaxLifetimeClosed`；同时区分 `primary` 与 `callback_outbox` pool。
  - Gateway：`开始推送callback事件`、`callback事件推送成功`、`callback事件推送失败`、`callback事件入队失败`、`delivery_status`、`queue_full`、task queue metrics、`go_gateway_webhook_events_total{result="enqueue_failed"}`。
  - Outbox：`gateway_event_outbox.status`、`attempts`、`last_error`、`next_attempt_at`、`locked_by`、`locked_until`、`delivered_at`、`payload_json.files`。

## 回滚方式

- 如何撤回配置：回滚镜像或还原 configmap 中新增连接池配置。
- 是否需要重启容器：需要。
- 是否需要恢复数据库/文件：不需要。
- 如何证明回滚成功：Gateway 重新启动，`/api/v1/health` 可访问，MySQL 连接数回到预期范围，任务和 outbox 表数据未丢失。

## 测试与证据

| 行为 | 验证方式 | 证据 |
| ---- | -------- | ---- |
| B1 | 静态检查 config 默认值和 server 初始化 | `Config.Load()` 默认值；`NewMySQLStoreFromURLWithPool()` 调用 |
| B2 | config 单测覆盖文件配置、环境变量和非法值 | `config_test.go` 新增断言 |
| B3 | 代码路径确认最大打开连接传入 `sql.DB.SetMaxOpenConns()` | `mysql.go` `applyDBPoolConfig()` |
| B4 | outbox 代码未迁移，仍由 MySQL store 承担 | `main.go` 中 `outbox = sqlOutbox`，底层仍为 MySQL `SQLStore` |
| B5 | Redis assistant cache 和 chat limit 已接入；runtime/health/callback latest 仍未实现 | `internal/rediscache`、`Service.SetAssistantResolutionCache()`、`Service.SetChatRateLimiter()` |
| B6 | Redis cache 不替代 MySQL 权威写；chat admission 可配置 fail-closed | MySQL tasks/outbox 仍为权威；限流错误返回 `429` |
| B7 | 文档明确 delivered 语义和上游二次确认边界 | `PRODUCT.md` B7/B15 |
| B8 | callback payload 代码未改动；本次只改连接池 | diff 未涉及 callback payload 组装 |
| B9 | health 按用户查询作为后续目标，不在本次实施 | `planned/doc-only`；后续独立 spec |
| B10 | 无 schema/数据迁移，回滚不触碰数据 | diff 无 migrations |
| B11 | 部署前计算全局连接预算 | 部署记录包含副本数、MySQL `max_connections`、预算和安全系数 |
| B12 | 队列达到上限返回 `429 queue_full`，且 accepted callback 持久化前任务处于不可 claim 的 `admitting` | `GO_GATEWAY_TASK_QUEUE_MAX_PENDING` 测试、API 响应、metrics、`TestTaskQueueAdmittingTaskWaitsForActivation` |
| B13 | 注入 MySQL 写失败 | 任务创建 fail closed；callback pending 保留或 worker 退避 |
| B14 | 模拟 callback 失败至上限 | outbox/dead-letter 状态、attempts、last_error、replay/resolve 证据 |
| B15 | 上游 HTTP 2xx 与业务落库分开验收 | Gateway delivered 日志加上上游业务表或接收端日志 |
| B16 | 暴露 DB pool stats | Prometheus/JSON 或日志包含 `WaitCount`、`WaitDuration` 等；by-pool 指标区分 `primary` 与 `callback_outbox` |
| B21 | callback/outbox 使用独立 DB pool | `cmd/server/main.go` 单独创建 `sqlOutbox`；config/env/deployment 默认值为 `8/2` |
| B22 | callback/outbox store 短超时、入队并发上限、投递并发、租约校验和同 task 串行 | `dispatcher.go` store timeout、非阻塞 enqueue semaphore、`enqueue_failed` 指标、delivery concurrency、claim limit、task lane；`mysql_webhook.go` `locked_by` 条件更新；config/env/deployment 默认 `2s/4/4` |
| B23 | Redis 以 HA StatefulSet + PV/PVC + 官方 Sentinel 形式部署并由 Gateway 配置接入 | `redis.yaml`、`redis-local-pv.yaml`、`GO_GATEWAY_REDIS_MODE=sentinel`、`GO_GATEWAY_REDIS_SENTINEL_ADDRS=redis-sentinel:26379` |
| B17 | 压测 health/runtime 状态查询 | QPS、DB 查询量、fallback interval/jitter、readyz degraded 证据 |
| B18 | Redis R1 独立 spec | runtime cache key、TTL、stale 判定和回源矩阵 |
| B19 | Redis R2 独立 spec | callback latest/user visibility cache 权限、失效和回源矩阵 |
| B20 | Redis Stream emergency buffer 独立 S3 spec | AOF/HA、幂等、回灌、DLQ、重放和审计补偿设计 |

## 风险与缓解

- 风险：最大连接数 40 可能仍需按线上实例、其他服务连接数和 MySQL `max_connections` 调整。
  - 缓解：保留环境变量和配置文件覆盖；部署后必须观察 `Threads_connected`、Gateway DB pool stats 与 Gateway 请求延迟。
- 风险：单实例连接池保护不等于集群级保护。
  - 缓解：P0 验收必须记录全局连接预算和副本数。
- 风险：连接池限制会把原本压到 MySQL 的压力转化为 Gateway 内部等待，极端并发下请求可能变慢或超时。
  - 缓解：把 queue max pending、worker claim limit、429/503 策略和 DB stats 纳入 P0 验收。
- 风险：callback/outbox 小池仍可能被自身入队、claim 和 mark 操作打满。
  - 缓解：outbox 入队增加短超时和默认 4 并发上限；claim limit 不超过实际 delivery concurrency；同一 task 串行投递；mark 状态时校验 `locked_by`；Prometheus 不在持久化模式下查询 dead-letter count；部署压测仍必须观察 `callback_outbox` pool `WaitCount/WaitDuration`。
- 风险：Redis 方案若后续误用为权威事实源，会削弱 callback 可靠性。
  - 缓解：本 spec 明确禁止 Redis 替代 outbox；R1/R2 必须独立 spec 并逐项映射本 spec。
- 风险：把 HTTP 2xx delivered 误解为上游业务成功。
  - 缓解：文档和验收中区分 `transport_delivered` 与 `business_persisted`。

## 后续事项

- 在构建环境重复执行 `cd go-gateway && go test ./...`。
- 部署前补全局连接预算，确认 Gateway 副本数、MySQL `max_connections`、Admin/runtime/运维预留连接和安全系数。
- 部署后压测 100/300/600 并发 chat，记录 Gateway 延迟、429/503 比例、DB pool stats、MySQL 连接数、outbox dead/delivered 比例和 `enqueue_failed` 计数。
- 部署接入 Go `sql.DB.Stats()` 指标，并设置 `WaitCount/WaitDuration` 告警。
- 设计 Phase R1 Redis runtime status 缓存时，先开独立 spec 和验收矩阵，再实施代码；不要复用当前 chat rate limit key 语义。
- 如需数据库不可用期间 callback emergency buffer，另开 S3 Redis Stream spec，不得挂在本 S2 连接池 spec 下隐式推进。
