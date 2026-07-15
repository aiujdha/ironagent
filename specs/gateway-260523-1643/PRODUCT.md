# gateway-260523-1643: Gateway 数据库连接池保护与 Redis 预备方案

- 模块：gateway
- 创建日期：2026-05-23 16:43
- 编写人：Codex
- 最近修改人：Codex
- 最近修改日期：2026-05-24
- 等级：S2
- 状态：implemented
- 状态说明：当前连接池配置、callback/outbox 独立 DB pool、callback outbox 短超时与入队并发上限、DB pool 指标、既有队列背压路径、Redis HA StatefulSet/PV/PVC + 官方 Sentinel、Gateway Redis assistant cache 和 chat admission limit 已有实现证据；按用户 health 改造、runtime status Redis 化、callback latest cache、DB 故障期 callback emergency buffer 均未实现。

## 背景

现场验证中出现过数据库连接被打满后 Gateway、callback 和 health 行为同时失效的问题。callback 本身需要持久化任务、outbox 事件、投递结果和重试状态，如果所有请求都无限制地占用 MySQL 连接，600 个并发 chat 请求或页面轮询会把同一个数据库实例拖垮，最终表现为 callback 有时成功、有时失败，或者 health 变成 degraded。

本 spec 固化三个层次的目标：当前提交落地 MySQL 连接池硬上限，并把 callback/outbox 拆到独立受限 DB pool，同时给 outbox 入队加短超时和并发上限，防止 Gateway 自身无限制扩大连接或让 callback 抢占全部主链路连接；现有任务队列背压必须纳入 P0 验收；Redis 当前只承担 `/api/v1/chat` 前置 admission limit、助手绑定/可用性短 TTL 缓存等非权威降压职责，后续 runtime status、health 聚合和 callback 只读快照仍需独立设计，不能替代 MySQL 中任务与 callback outbox 的权威事实。

## 摘要

问题：Gateway 对 MySQL 连接缺少显式上限，流量放大时可能把数据库连接打满，连带影响 callback、health 和管理页面。期望结果：Gateway 默认以有限连接池访问 MySQL，callback/outbox 使用独立受限连接池，outbox 入队使用短超时和并发上限，超出能力时在 Gateway 内部限流或失败退避，而不是继续扩张数据库连接；Redis 作为当前 P0 降压组件，只做短 TTL cache 和 admission limit，不做任务/outbox 权威事实源。

## 问题

- MySQL `max_connections` 有固定上限，历史现场值约为 151；如果 Gateway、管理页面、runtime 轮询和 callback worker 共享同一库，未限制的连接增长会互相挤占。即使单实例池已有限制，也必须避免 callback/outbox 高峰占满主任务链路连接。
- 单个 Gateway 默认 40 个连接不等于集群安全；多副本、Admin、runtime loop、migration 和排障连接必须合并计算全局连接预算。
- callback/outbox 即使拆出小池，也可能被自身高并发入队、claim、mark 或 dead-letter 查询打满；必须给入队链路加短超时、并发上限和可观测错误，而不是让请求线程长期等待 callback DB pool。
- callback outbox 需要可靠持久化，不能简单改为内存或 Redis 后就认为投递可靠。
- Redis 能降低 health/runtime 状态读取频率，但不能解决所有写路径；如果把任务最终态或 callback outbox 放进 Redis 作为唯一事实，会引入丢事件、乱序和恢复困难。
- 上游 callback 是否真正消费成功，不能只看 Gateway 日志的 `delivered`；还需要能区分请求已发出、上游 HTTP 返回、上游业务落库三层证据。

## 参与方

- 管理员：查看 health、管理页面和 callback 投递状态。
- API 调用方：通过 `/api/v1/chat` 或等价接口提交任务。
- 助手 runtime：上报任务状态、最终文本和文件信息。
- Workspace：承载最终态文件和 download_url。
- 内部服务：Gateway、MySQL、Redis、callback worker、runtime registry。

## 生效时机

- 连接池配置：Gateway 容器启动时生效，需要重启或重新部署。
- 队列背压：`GO_GATEWAY_TASK_QUEUE_MAX_PENDING` 大于 0 且 task worker 模式启用时生效。
- callback/outbox：本次不迁移存储介质，仍以 MySQL 为权威事实。
- Redis P0：Gateway 可选接入 Redis，用于 `assistant_binding:{user_id}`、`assistant_available:{assistant_id}` 短 TTL 缓存和 `/api/v1/chat` 前置限流；Redis 支持 standalone 与 Sentinel 模式，配置在 Gateway 启动时生效。
- 回滚：回滚镜像或配置后恢复旧行为；也可通过配置调整连接池上限，但不建议取消上限。

## 权限与安全边界

- 本次不改变公开 API 鉴权、admin 鉴权、callback host 白名单或 API key 语义。
- 连接池配置只能由部署配置或环境变量控制，不能由普通 API 调用方设置。
- Redis 只允许存放可重建、可过期、非权威状态和 admission 计数；任务创建、最终态、callback outbox、审计仍由 MySQL 保持权威。
- health 如果后续按 `user_id` 查询，只能在已认证和已授权用户范围内返回该用户相关助手状态，不能把 `user_id` 当成授权依据。
- Redis 中的用户助手可见集合只能作为已鉴权后的派生缓存；权限版本、助手绑定、禁用、删除或角色变化必须触发失效或回源校验。

## 非目标

- 本 spec 当前提交不实现 runtime status Redis 化、按用户 health Redis 聚合、callback latest cache 或 Redis Stream emergency buffer。
- 本 spec 当前提交不迁移 `gateway_tasks`、`gateway_event_outbox` 或 callback 投递表。
- 本 spec 当前提交不改变 callback payload 格式；`files[]` 的生产逻辑由已有 callback 最终态链路继续负责。
- 本 spec 当前提交不承诺 600 个 chat 请求都立即执行；连接池保护后的正确行为是限流、排队、失败退避或返回明确错误，而不是把数据库打满。
- 本 spec 当前不承诺 MySQL 不可用期间仍能可靠发送 callback；无 Redis Stream emergency buffer 时，outbox 不可写必须 fail closed。

## 行为

B1. 当 Gateway 使用 MySQL 时，必须设置默认数据库连接池上限，默认最大打开连接数为 40，默认最大空闲连接数为 10，默认连接最大生命周期为 30 分钟，默认空闲连接最大保留时间为 5 分钟。

B2. 当部署需要调整数据库连接池时，必须同时支持配置文件和环境变量；非法配置必须在启动校验阶段失败，而不是运行中隐式忽略。

B3. 当并发请求超过 Gateway 与数据库当前能力时，系统应优先在 Gateway 内部形成等待、退避或明确失败，不得通过无限制创建 MySQL 连接冲击数据库。

B4. 当 callback 需要投递任务状态时，`gateway_event_outbox` 仍然是本阶段的权威事件源；Redis 不得替代 outbox 的可靠投递、重试、dead-letter 和去重语义。

B5. 当 Redis 接入应用链路时，只允许缓存助手绑定、助手可用性、runtime status、health 聚合、用户助手可见集合或 callback 最近快照等可重建数据，以及用于 admission control 的短窗口计数；Redis 数据必须带 TTL，丢失后不得影响任务最终态和 callback 可靠投递。当前已实现 `assistant_binding`、`assistant_available` 和 chat rate limit，其他 Redis 用途仍为 planned/deferred。

B6. 当 Redis 不可用时，Gateway 必须继续通过 MySQL 完成任务创建、状态持久化和 callback outbox 写入；但如果显式启用 `GO_GATEWAY_CHAT_RATE_LIMIT_ENABLED=true` 且 `GO_GATEWAY_CHAT_RATE_LIMIT_FAIL_CLOSED=true`，chat admission limit 可返回明确的 `429` 以保护 MySQL。

B7. 当 callback 日志显示投递成功时，语义是 Gateway 已完成 HTTP 请求并获得符合当前策略的返回；是否被上游业务处理，必须由上游日志、断点、业务表或抓包进一步确认。后续文档和指标应优先使用 `transport_delivered` 表达该语义。

B8. 当 callback payload 包含最终文件时，`files[]` 中必须包含 `download_url`，并保持现有字段结构兼容；当前连接池保护不得改变该 payload。

B9. 当 health 后续优化为按用户查询时，必须只查询当前 `user_id` 可见助手的 runtime health，不能默认扫描全量助手；但本次提交不改 health 接口行为。该行为当前为 planned/doc-only。

B10. 当需要回滚本次连接池保护时，必须保留 callback/outbox 数据不变；回滚动作不得删除 MySQL 表、Redis key 或工作区文件。

B11. 当进入部署验收时，必须计算全局 MySQL 连接预算：`gateway_replicas * (gateway_primary_max_open_conns + gateway_callback_outbox_max_open_conns) + admin_budget + runtime_or_health_budget + migration_or_ops_budget + reserved_connections <= mysql_max_connections * safety_factor`。验收报告必须记录副本数、MySQL `max_connections`、最终池参数和预留连接数。

B12. 当 task worker 模式启用且 `GO_GATEWAY_TASK_QUEUE_MAX_PENDING > 0` 时，`admitting + queued + processing + retry` 达到阈值后，新任务必须返回明确背压错误，例如 `429 queue_full`；阈值必须按 worker 吞吐、DB 写入能力、callback 积压和可接受排队时间确定。

B13. 当 MySQL 不可用或 `gateway_tasks`、`gateway_event_outbox` 任一权威写入失败时，Gateway 不得返回“任务已接收”或“callback 已可靠入队”。任务创建必须 fail closed 并返回可重试错误；callback worker 必须暂停、退避或保留 pending，不得只写日志丢事件，也不得在 `MarkDelivered` 失败时声称状态已闭环。

B14. 当 outbox 事件重试失败达到上限时，必须进入可查询的 dead-letter 终态，而不只是日志。outbox 状态机至少要能表达 `pending`、`in_flight` 或租约锁定、`delivered`、`retry`、`dead`，并记录 attempts、next_attempt_at、locked_by、locked_until、last_error、delivered_at、dead_at 或等价字段。

B15. 当 callback 上游返回 HTTP 2xx 时，Gateway 可以标记传输投递成功；这不等价于上游业务落库成功。验收如果要证明业务消费，必须提供上游业务表、接收端日志或抓包证据。

B16. 当发布连接池保护时，Gateway 必须暴露或记录 Go `sql.DB.Stats()` 等价指标，至少包括 `MaxOpenConnections`、`OpenConnections`、`InUse`、`Idle`、`WaitCount`、`WaitDuration`、`MaxIdleClosed`、`MaxLifetimeClosed`；MySQL 侧仍需观测 `Threads_connected`、`Threads_running`、`Connection_errors_max_connections`。

B17. 当 health/runtime 状态查询流量较高时，必须有状态流量预算：禁止 public health 默认全量扫描，优先按用户/助手范围查询；fallback polling 必须有间隔、jitter 或其他抑制手段；DB 不可用时 `/health`、`/readyz` 必须能体现 degraded 或 unavailable，而不是返回误导性 healthy。

B18. 当实施 Redis Phase R1 runtime status 与 health 缓存时，必须另开独立 spec。缓存值必须携带 `observed_at`、`source`、`expires_at` 或等价字段；读取方必须按 `last_heartbeat_at + GO_GATEWAY_RUNTIME_HEARTBEAT_TTL` 重新判定 stale，Redis TTL 不得延长 runtime 的权威健康有效期。

B19. 当实施 Redis Phase R2 callback 最近快照或用户视图缓存时，必须另开独立 spec。`callback-latest` 只能由 MySQL/outbox 派生，`user-assistants` key 必须包含租户、主体、权限 scope 或 authz version 等隔离维度，并定义 miss、eviction、Redis timeout、回源和失效规则。

B20. 当要求“数据库短暂不可用时 callback 仍能推送”时，必须另开 S3 Redis Stream emergency buffer spec。该 spec 必须解决 AOF/HA、容量、幂等键、顺序、重复投递、MySQL 回灌、审计补偿、dead-letter、重放和丢弃策略；本 spec 不允许把 Redis Stream 作为隐含实现。

B21. 当 Gateway 使用 MySQL outbox 时，callback/outbox 必须使用独立且有上限的 DB pool，例如 `GO_GATEWAY_CALLBACK_DATABASE_MAX_OPEN_CONNS`；主任务 pool 和 callback/outbox pool 必须分别可观测，并一并纳入全局连接预算。

B22. 当 callback/outbox 使用 MySQL store 时，outbox 入队必须受 `GO_GATEWAY_CALLBACK_STORE_TIMEOUT` 和 `GO_GATEWAY_CALLBACK_ENQUEUE_MAX_IN_FLIGHT` 约束；默认值分别为 `2s` 和 `4`，且默认入队并发低于 callback/outbox pool `max_open=8`。入队超过能力时必须快速返回入队失败并记录可观测事件，不得长期占用请求线程等待 callback/outbox DB pool；outbox 投递默认并发为 `GO_GATEWAY_CALLBACK_DELIVERY_CONCURRENCY=4`，claim 数量不得超过实际投递并发，同一 `task_id` 的事件必须串行投递；Prometheus scrape 不得为了 dead-letter 计数主动查询持久化 outbox。

B23. 当验证 overlay 需要集群内 Redis 时，Redis 必须像 MySQL 一样以 `StatefulSet + Service + PV + PVC` 形式部署；生产型验证应使用 `redis-ha` StatefulSet、Headless Service、官方 Redis Sentinel Service/Deployment、每副本 PVC 和 `Retain` PV。数据目录挂载到 `/data`，开启 AOF `appendfsync everysec`，凭证从 Kubernetes Secret 注入。Gateway HA 模式必须通过 `GO_GATEWAY_REDIS_MODE=sentinel`、`GO_GATEWAY_REDIS_SENTINEL_ADDRS` 和 `GO_GATEWAY_REDIS_SENTINEL_MASTER` 接入；普通 `redis:6379` Service 不得作为故障转移后的权威写入口。

## 待确认问题

- Redis key TTL、容量上限、淘汰策略和监控阈值，需要结合真实 QPS 与助手数量压测后最终定值。
- P0 部署验收时的 Gateway 副本数、MySQL `max_connections`、Admin/runtime 连接预算和安全系数，需要由部署侧确认。
- `GO_GATEWAY_TASK_QUEUE_MAX_PENDING` 线上默认值需要压测后确认；`10000` 只能作为配置存在证据，不能直接视为合理生产阈值。
- `GO_GATEWAY_CALLBACK_ENQUEUE_MAX_IN_FLIGHT` 是否应固定为 4 或进一步下调，需要结合 callback 上游延迟、outbox worker 吞吐和 MySQL wait 指标压测后确认。
