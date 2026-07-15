# gateway-260523-1643: Gateway 数据库连接池保护与 Redis 预备方案 - 验收报告

- spec-id：gateway-260523-1643
- 测试日期：2026-05-23
- 复审更新日期：2026-05-24
- 验收分支：`codex/hotfix-phase1-merge-admin-p0`
- 验收基线 commit：`207fc771`
- 验收目标 commit：`46ad2da3`
- target HEAD：`46ad2da3`
- current HEAD：`46ad2da3`
- dirty status：dirty；相关改动为 Gateway 连接池配置、部署配置、测试和本 spec；当前复审继续修改本 spec；无关未跟踪目录为 `output/`、`outputs/`、`tmp/docs/` 等。
- 验收人：Codex
- 测试范围：配置加载、MySQL store 连接池设置入口、callback outbox store 短超时与入队并发上限、DB pool stats metrics、部署配置、Redis HA PV/PVC + 官方 Sentinel 部署清单、Gateway Redis assistant cache、chat admission limit、spec 固化、专家复审缺口归档、Go Gateway 单测、K8s 部署、Sentinel failover、真实 `/api/v1/chat` 500 并发压测、100/300/600 阶梯压测、约 29 分钟 soak、callback outbox 统计
- 测试方式：静态检查、JSON 配置解析、diff 检查、只读复审、Go module 单测、Kustomize 渲染、K8s rollout、Redis/Sentinel Pod 内探测、真实 HTTP 压测、MySQL/Redis/Gateway metrics 采样、Chrome 管理页回归
- 环境边界：本地仓库 `/Users/example/Downloads/ironagent`；目标 K8s namespace `ironagent`；Gateway NodePort `http://192.0.2.10:30808`；单副本 Gateway；未做 MySQL 故障注入和多副本预算验证
- 结论：pass-with-known-risks
- 分级结论：deployed-and-load-verified

## 时效性

本报告只对 `46ad2da3` 及当前 spec 修订有效。如果当前分支 HEAD 不包含该 commit，或后续 spec/commit 已经修正本报告中的失败项，先更新本报告，再拆后续任务。

## 总结论

- 已通过：代码中已出现 MySQL 主连接池默认配置、callback/outbox 独立 DB pool 默认配置、callback outbox store 默认保护、文件/env 配置、启动校验、MySQL store 初始化设置、部署 configmap 默认值、Redis HA StatefulSet/PV/PVC + 官方 Sentinel、Gateway `go-redis/v9` Sentinel failover client、Gateway Redis assistant cache、chat admission limit。
- 部分通过：配置 JSON、主池/callback outbox by-pool metrics、callback enqueue backpressure 单测和 Go Gateway 单测通过；真实 K8s 已部署并通过 Sentinel 手工 failover 后 500 并发 `/api/v1/chat` 创建/取消压测；MySQL 故障注入、60 分钟以上 sustained soak、多副本预算仍未验证。
- 未通过：无已发现代码级失败项。
- 未验证：MySQL 故障注入、DB pool stats 告警、上游业务落库确认。
- 阻塞：无本地 Go toolchain 阻塞；MySQL 当前是单 StatefulSet，未在无维护窗口情况下做破坏性断库注入。
- 复审结论：当前 P0 可声明“主池与 callback/outbox 池隔离、outbox 入队有短超时和并发上限、Redis assistant cache/chat admission limit 已部署，并通过 500 并发、100/300/600 阶梯、约 29 分钟 soak、Redis Sentinel failover 验证”；仍不能声明“多副本生产连接预算和 MySQL 故障注入闭环完成”。

## 关联提交

| 类型 | commit | 说明 |
| ---- | ------ | ---- |
| 基线 | `207fc771` | 修改前 HEAD |
| 当前目标 | `46ad2da3 + working tree` | DB 连接池保护、callback/outbox 独立池与 spec 所在当前工作树 |
| 集成 | 无 | 未做跨分支集成 |
| 验收 | `46ad2da3 + working tree` | 静态验收、单测和专家复审 |
| 部署 | `localhost/go-gateway:redis-ha-20260524-r2` | 已滚动到 `ironagent/go-gateway` |

## 命令结果

| 命令 | 结果 | 说明 |
| ---- | ---- | ---- |
| `git status --short --branch` | `## codex/hotfix-phase1-merge-admin-p0`，存在无关未跟踪目录 | 分支确认 |
| `git rev-parse --short HEAD` | `46ad2da3` | 当前 HEAD 确认 |
| `rg -n "DatabaseMaxOpenConns|CallbackDatabaseMaxOpenConns|CallbackStoreTimeout|CallbackEnqueueMaxInFlight|GO_GATEWAY_CALLBACK_STORE_TIMEOUT|NewMySQLStoreFromURLWithPool|DBPoolConfig" go-gateway deployment specs -S` | 命中新配置、store、server 和部署配置 | 代码入口确认 |
| `rg -n "TaskQueueMaxPending|GO_GATEWAY_TASK_QUEUE_MAX_PENDING|QueueFullError|queue_full|StatusTooManyRequests" go-gateway` | 命中既有队列背压路径 | 反压入口确认 |
| `command -v go || true; command -v gofmt || true` | `/opt/homebrew/bin/go`、`/opt/homebrew/bin/gofmt` | 本地 Go toolchain 可用 |
| `python3 -m json.tool go-gateway/config.example.json` | `ok` | JSON 配置语法检查通过 |
| `python3` 解析 `deployment/kustomize/ironagent/base/go-gateway-file-configmap.yaml` 内嵌 `go-gateway.json` | `ok` | 文件型 configmap 内嵌 JSON 语法检查通过 |
| `git diff --check` | 无输出，exit 0 | 空白/补丁检查通过 |
| `go test ./go-gateway/...` | `pattern ./go-gateway/...: directory prefix go-gateway does not contain main module or its selected dependencies` | 根目录不是 Go module，命令不适用 |
| `cd go-gateway && go test ./internal/config` | passed | 主池、callback/outbox 池、callback store timeout、入队并发上限的配置文件、环境变量、非法值和 `max_open=0` 拒绝测试通过 |
| `cd go-gateway && go test ./internal/webhook -run 'TestOutboxDispatchAsync(ProcessesAndAcknowledges\|RejectsWhenEnqueueBackpressureFull\|StoreTimeout)'` | passed | outbox 入队、入队并发拒绝和 store timeout 测试通过 |
| `cd go-gateway && go test ./internal/httpapi -run 'Test(PrometheusMetricsEndpoint\|JSONMetricsEndpointIncludesDBPoolStats\|TaskQueueBackpressureRejectsNewTasks)'` | passed | 主池/callback outbox by-pool metrics 与队列背压路由测试通过 |
| `cd go-gateway && go test ./...` | passed | Go Gateway module 单测通过 |
| `kubectl kustomize deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport` | passed | 渲染包含 `GO_GATEWAY_REDIS_MODE=sentinel`、`GO_GATEWAY_REDIS_SENTINEL_ADDRS=redis-sentinel:26379`、chat rate limit、`redis-ha`、`redis-sentinel` 和 `redis:7.2.10-alpine` |
| `kubectl -n ironagent rollout status deployment/go-gateway` | passed | `localhost/go-gateway:redis-ha-20260524-r2` 成功滚动 |
| `curl http://192.0.2.10:30808/readyz` | `200`，约 `12ms` | Gateway ready，task queue/southbound 正常 |
| Redis Pod `PING` 与 `INFO server` | `PONG`，`redis_version:7.2.10` | 集群内 Redis 可用且镜像版本固定 |
| Redis Sentinel `SENTINEL get-master-addr-by-name mymaster` | passed | Sentinel 能返回当前 master，Gateway 日志显示 go-redis 发现 master |
| r2 smoke `/api/v1/chat` + cancel | `202` + `200` | 新镜像基础链路可用 |
| Chrome 管理页 `http://192.0.2.10:30808/admin/#assistants` | URL 与 `GATEWAY TOKEN` 可见 | 助手页面显示回归通过 |
| `gofmt -w ...` | passed | 本轮修改的 Go 文件已格式化 |
| `kubectl kustomize deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport >/tmp/ironagent-prod-k8s-single-entry-nodeport.yaml` | passed | Redis HA Service、Headless Service、Sentinel Service/Deployment、StatefulSet、PV、PVC 可随 overlay 渲染 |
| `rg -n "redis-ha\|redis-sentinel\|redis-data-redis-ha\|image: redis:7.2.10-alpine" /tmp/ironagent-prod-k8s-single-entry-nodeport.yaml` | 命中 Redis HA、Sentinel、PV/PVC 和镜像 | Redis HA PV/PVC + Sentinel 部署清单渲染确认 |
| `README.md` Redis deployment check | documented | 记录集群内 `rollout status`、PVC/PV 检查和 `PING/SET EX/GET/DEL` 验证命令；提醒不要提交真实 Redis 密码 |
| `kubectl apply --dry-run=client --validate=false -f /tmp/ironagent-prod-k8s-single-entry-nodeport.yaml` | failed: local API discovery tried `localhost:8080` and was unavailable | 当前本地没有可用 K8s context，未完成 client dry-run |
| `redis-cli -h redis.example.internal -p 21724 --no-auth-warning PING` | `Connection reset by peer` | 外部腾讯云 Redis 当前本地环境无法完成明文协议握手；未记录密码 |
| `redis-cli --tls -h redis.example.internal -p 21724 --no-auth-warning PING` | `SSL_connect failed: Connection reset by peer` | 当前端口也不是可用 TLS Redis 协议入口；未记录密码 |
| `redis-cli --user <redis-user> --askpass -h redis.example.internal -p 21724 --no-auth-warning PING` | `Connection reset by peer` | 按 Redis ACL 用户名认证方式尝试失败；未记录密码 |
| `redis-cli --askpass -h redis.example.internal -p 21724 --no-auth-warning PING` | `Connection reset by peer` | 按腾讯云兼容的 user-at-password 方式尝试失败；未记录密码 |
| Python TCP/RESP probe to `redis.example.internal:21724` | DNS 解析到 `192.0.2.12`；TCP 可建立但远端立即关闭 | 更像网络/VPC/白名单/端口入口问题，不是普通 Redis `NOAUTH` 响应 |
| 2026-05-24 线上只读复核 `/readyz` | `200 healthy`，`registered=8 ready=8` | 当前 Gateway 健康 |
| 2026-05-24 线上只读复核 Deployment | `localhost/go-gateway:redis-ha-20260524-r2`，`imagePullPolicy=Never` | 当前服务器仍运行 Redis HA 版本 |
| 2026-05-24 线上只读复核 Redis/Sentinel Pods | `redis-ha` 2/2 ready，`redis-sentinel` 3/3 ready | Redis HA 与 Sentinel 仍在线 |
| 2026-05-24 线上只读复核 Gateway Redis 配置 | `GO_GATEWAY_REDIS_MODE=sentinel`，`GO_GATEWAY_REDIS_SENTINEL_ADDRS=redis-sentinel:26379`，`GO_GATEWAY_REDIS_SENTINEL_MASTER=mymaster` | Gateway 仍通过 Sentinel 接入 |
| 2026-05-24 线上只读复核 Sentinel master | `10.244.144.93:6379` | 当前 master 为 `redis-ha-1` Pod IP |
| 2026-05-24 连接预算采样 | MySQL `max_connections=151`，`Threads_connected=15-25`，`Connection_errors_max_connections=5849` 未新增 | 单副本 Gateway 当前水位可控 |
| 2026-05-24 真实 `/api/v1/chat` 100 并发阶梯 | create `100/100` 202，cancel `100/100` 200；create p95 `735.9ms`，cancel p95 `579.9ms` | 低阶梯通过 |
| 2026-05-24 真实 `/api/v1/chat` 300 并发阶梯 | create `300/300` 202，cancel `300/300` 200；create p95 `2280.7ms`，cancel p95 `2000.0ms` | 中阶梯通过 |
| 2026-05-24 真实 `/api/v1/chat` 600 并发阶梯 | create `582/600` 202、`18/600` 429 `queue_full`；cancel `582/582` 200 | `GO_GATEWAY_TASK_QUEUE_MAX_PENDING=500` 背压生效 |
| 2026-05-24 约 29 分钟 soak | 30 轮，每轮 100 并发；create `3000/3000` 202，cancel `3000/3000` 200 | soak 未出现 429/5xx |
| 2026-05-24 soak 中 Redis Sentinel failover | master `10.244.144.93:6379 -> 10.244.144.79:6379`，后续 Sentinel 收敛回 `10.244.144.93:6379` | Gateway 未重启，后续轮次无失败 |

## 行为覆盖矩阵

| 行为 | 当前结论 | 证据 |
| ---- | -------- | ---- |
| B1 | code-present | `go-gateway/internal/config/config.go` 默认 `40/10/30m/5m` |
| B2 | code-present | `fileConfig`、`applyFileConfig()`、`applyEnv()` 和 `Validate()` 增加连接池配置处理 |
| B3 | code-present | `go-gateway/internal/taskstore/mysql.go` 在 `PingContext()` 前调用 `applyDBPoolConfig()` |
| B4 | code-present | `go-gateway/cmd/server/main.go` 将 `outbox = sqlOutbox`，底层仍为 MySQL `SQLStore`，未迁移 outbox 事实源 |
| B5 | deployed/partial | Redis assistant cache 与 chat admission limit 已接入并部署；runtime/health/callback latest 仍未实现 |
| B6 | deployed/partial | MySQL tasks/outbox 仍为权威；chat admission fail-closed 可返回 `429` 保护 MySQL |
| B7 | doc-only | `PRODUCT.md` B7/B15 明确 delivered 语义；术语统一仍待后续实现或文档收敛 |
| B8 | code-present | 本次 diff 不涉及 callback payload 组装逻辑 |
| B9 | planned/doc-only | health 按用户查询列为后续目标；当前提交不改 health 接口行为 |
| B10 | code-present | 无 schema 迁移和数据删除逻辑 |
| B11 | partial/live-budget-baseline | 已记录真实副本数、MySQL `max_connections=151`、Gateway 主池+callback outbox 池 `40+8`、当前连接来源；多副本预算仍需部署侧确认 |
| B12 | live-verified/threshold-needs-policy | `TestTaskQueueBackpressureRejectsNewTasks` 覆盖 `429 queue_full`；600 并发真实 `/api/v1/chat` 中 `18/600` 返回 `429 queue_full`，证明线上阈值生效 |
| B13 | unverified | 未注入 MySQL 写失败验证任务创建和 callback worker fail-closed |
| B14 | partial/code-present | 代码已有 outbox retry/dead-letter 入口；本报告未验证完整状态机、租约、replay 和 DB 故障恢复 |
| B15 | doc-only/unverified | delivered 语义已写入；未用上游业务表或接收端日志证明业务消费 |
| B16 | deployed/alert-unverified | `SQLStore.DBPoolStats()` 暴露 `sql.DB.Stats()`；JSON metrics 和 Prometheus by-pool metrics 测试覆盖；线上采集已用于压测，告警未验证 |
| B17 | planned/unverified | health/runtime 状态流量预算和 fallback jitter 未验收 |
| B18 | planned/deferred | Redis R1 必须另开独立 spec |
| B19 | planned/deferred | Redis R2 必须另开独立 spec |
| B20 | deferred | Redis Stream emergency buffer 必须另开 S3 spec，本 spec 不实现 |
| B21 | deployed | MySQL 模式下 `cmd/server/main.go` 单独创建 callback/outbox `SQLStore`；线上 metrics 暴露 `callback_outbox` pool |
| B22 | deployed/partial | `dispatcher.go` 对 MySQL outbox 入队加 store timeout 和入队 semaphore；500 并发压测未出现 enqueue failed；故障注入未验证 |
| B23 | deployed/failover-verified | `redis-ha` StatefulSet、`redis-sentinel` Deployment/Service、每副本 PV/PVC 已在 `ironagent` 可用；Gateway 已配置 `GO_GATEWAY_REDIS_MODE=sentinel` 并通过 Sentinel 发现 master；soak 中手工 failover 后无失败 |

## 2026-05-24 Live Redis HA / go-redis 验收

部署：

- 镜像：`localhost/go-gateway:redis-ha-20260524-r2`
- Redis：`redis-ha` StatefulSet 2 副本，镜像 `redis:7.2.10-alpine`，AOF `appendfsync everysec`，每副本 PVC。
- Sentinel：`redis-sentinel` Deployment 3 副本，使用官方 `redis-server --sentinel`，master 名称 `mymaster`。
- Gateway 配置：`GO_GATEWAY_REDIS_MODE=sentinel`，`GO_GATEWAY_REDIS_SENTINEL_ADDRS=redis-sentinel:26379`，`GO_GATEWAY_REDIS_SENTINEL_MASTER=mymaster`，`GO_GATEWAY_CHAT_RATE_LIMIT_ENABLED=true`，`GO_GATEWAY_CHAT_RATE_LIMIT_MAX=600`，`GO_GATEWAY_CHAT_RATE_LIMIT_WINDOW=1s`
- Gateway Redis client：`github.com/redis/go-redis/v9`，Sentinel 模式使用 `redis.NewFailoverClient`；不保留自研 Sentinel 发现逻辑。
- 注意：`redis:6379` 兼容 Service 在 failover 后可能指向副本，不能作为 HA 写入口；Gateway 只通过 Sentinel 连接。

Failover：

- 手工执行 `SENTINEL failover mymaster` 后，master 从 `redis-ha-0` 切换到 `redis-ha-1`。
- Gateway 未重启，failover 后 smoke `/api/v1/chat` 返回 `202`，cancel 返回 `200`。

真实 `/api/v1/chat` 压测：

- Prefix：`codex-goredis-ha-load-20260524121514`
- 并发：500，任务创建 `500/500` 返回 `202`
- 创建耗时：总 `3848.0ms`，avg `2819.2ms`，p50 `3248.5ms`，p95 `3637.9ms`，p99 `3682.8ms`
- 取消：`500/500` 返回 `200`，总 `3306.6ms`，avg `2150.9ms`，p95 `3126.5ms`，p99 `3201.4ms`

DB/Redis/callback 观测：

- MySQL `Connection_errors_max_connections=5849` 未新增；`Threads_connected=24`，`Threads_running=2`
- 当前线上 Gateway callback/outbox 配置为 `GO_GATEWAY_CALLBACK_STORE_TIMEOUT=10s`、`GO_GATEWAY_CALLBACK_ENQUEUE_MAX_IN_FLIGHT=8`；这仍是有界小池保护，但比代码默认 `2s/4` 更宽松，后续需要结合 `callback_outbox` pool wait 指标单独调优。
- Redis commandstats 当前 master：`GET=3`，`INCR=1003`，`EXPIRE=501`，`SET=8`
- Outbox：`gateway_tasks count=500`，`task.accepted delivered=500`，`task.cancelled delivered=500`，`task.progress delivered=1`
- Outbox latency：`task.accepted` avg `3.238s`、max `5s`；`task.cancelled` avg `4.864s`、max `6s`；`task.progress` avg/max `5s`
- 压测后 task queue active 为 `0`

## 2026-05-24 连接预算 / 阶梯压测 / Soak

连接预算基线：

- 当前副本数：`go-gateway=1`、`admin=1`、`security-service=1`、`ironagent-gateway=1`、`ironagent-cli=1`、`ironagent-callback-receiver=1`。
- MySQL：`max_connections=151`；按 80% 安全水位计算，可用预算约 `120`。
- Gateway 单副本硬预算：primary pool `40` + callback/outbox pool `8` = `48`。
- 当前连接观测：压测前 `Threads_connected=15`，阶梯压测后最高采样 `25`，soak 后回落到 `16`；`Connection_errors_max_connections=5849` 全程未新增。
- 连接来源采样：security-service 约 `8-9`，Gateway 约 `3-12`，admin 约 `3`，另有 root 查询和 event scheduler。
- 预算判断：单副本按当前观测可控；若扩到 2 个 Gateway 副本，`2 * (40 + 8) = 96`，再叠加 Admin/security/runtime/ops 预留后会贴近或超过 80% 安全水位。生产多副本前必须降低单副本池参数或提高 MySQL `max_connections`。

真实 `/api/v1/chat` 阶梯压测：

| 并发 | create 结果 | create p95/p99 | cancel 结果 | cancel p95/p99 | 结论 |
| ---- | ----------- | -------------- | ----------- | -------------- | ---- |
| 100 | `100/100` 202 | `735.9ms / 745.0ms` | `100/100` 200 | `579.9ms / 593.6ms` | 通过 |
| 300 | `300/300` 202 | `2280.7ms / 2309.5ms` | `300/300` 200 | `2000.0ms / 2046.7ms` | 通过 |
| 600 | `582/600` 202，`18/600` 429 | `5102.2ms / 5167.0ms` | `582/582` 200 | `3826.4ms / 3884.1ms` | 背压生效 |

阶梯压测后的 outbox 统计：

| Prefix | event_type | delivered | avg delivery | max delivery |
| ------ | ---------- | --------- | ------------ | ------------ |
| `codex-stair-100-20260524-1238` | `task.accepted` | 100 | `0.810s` | `1.000s` |
| `codex-stair-100-20260524-1238` | `task.cancelled` | 100 | `1.050s` | `2.000s` |
| `codex-stair-300-20260524-1239` | `task.accepted` | 300 | `2.003s` | `3.000s` |
| `codex-stair-300-20260524-1239` | `task.cancelled` | 300 | `2.637s` | `3.000s` |
| `codex-stair-600-20260524-1240` | `task.accepted` | 582 | `3.595s` | `5.000s` |
| `codex-stair-600-20260524-1240` | `task.cancelled` | 582 | `4.918s` | `6.000s` |

Soak：

- Prefix：`codex-soak-30m-20260524-1241`
- 模式：30 轮，每轮 100 并发真实 `/api/v1/chat` 创建，随后取消；总耗时 `1742.1s`。
- create：`3000/3000` 返回 202；avg `802.9ms`，p95 `1253.2ms`，p99 `1564.5ms`，max `1704.5ms`。
- cancel：`3000/3000` 返回 200；avg `654.8ms`，p95 `1055.1ms`，p99 `1220.9ms`，max `1492.6ms`。
- Redis/Sentinel fault injection：第 10 轮后执行 `SENTINEL failover mymaster`，master 从 `10.244.144.93:6379` 切到 `10.244.144.79:6379`；后续 Sentinel 又收敛回 `10.244.144.93:6379`。Gateway 未重启，第 11-30 轮无 429/5xx。
- Soak outbox：`task.accepted delivered=3000`，avg `0.746s`，max `2.000s`；`task.cancelled delivered=3000`，avg `0.902s`，max `2.000s`。
- Soak 后 Gateway metrics：primary pool `WaitCount=31722`、`WaitDuration=3943.758s`；callback/outbox pool `WaitCount=4689`、`WaitDuration=12.710s`；`task_queue active=0`。
- Soak 后 Redis commandstats：`GET=1370`、`INCR=5006`、`EXPIRE=4504`、`SET=42`，无 rejected/failed calls。

MySQL 故障注入评估：

- 当前 MySQL 是单 `mysql` StatefulSet，无已验证 standby。直接 kill Pod、阻断 service 或改 Secret 会影响 Admin、security-service、Gateway 和助手 runtime。
- 本轮未执行破坏性 MySQL 故障注入。建议下一步二选一：
  - 在隔离 namespace 或 canary Gateway 上把 DB endpoint 指向不可达地址，验证 `/api/v1/chat` fail-closed、outbox enqueue 失败、worker claim/mark 失败路径。
  - 申请维护窗口，在生产集群短暂注入 MySQL service/network 故障，并预先记录回滚命令、可接受影响时间和观察指标。

## 2026-05-24 全量测试与专家对抗修复

专家对抗结论：

- P0-1：`/api/v1/chat` 原先只按 client/user 限流，高基数 `user_id` 可绕过单桶限流；队列容量检查也发生在助手解析之后。
- P0-2：`task.accepted` callback outbox 入队失败时，创建接口仍可能返回 `202`，与 fail-closed 目标冲突。
- P1-1：callback outbox worker 原先串行投递，慢 callback 会放大全局 outbox 排队时间。
- P1-2：queue/outbox claim 查询缺少组合索引。

本轮修复：

- `go-gateway/internal/core/service.go`：chat admission 改为 `global + client/user/anonymous` 双桶限流；队列容量检查前移到助手解析和任务入库前。
- `go-gateway/internal/rediscache/client.go`：rate limit 从 `INCR + EXPIRE` 两步改为 Lua 原子脚本，避免异常窗口留下无 TTL key。
- `go-gateway/internal/httpapi/router.go`：`task.accepted` outbox 入队成为返回 `202` 前的强制步骤；失败时返回 `503 callback_enqueue_failed`，并尝试取消刚创建的 queued task。
- `go-gateway/internal/webhook/dispatcher.go`：新增 `callback_delivery_concurrency`，默认 `4`，outbox claim 后并发投递。
- `migrations/versions/017_gateway_queue_outbox_indexes.py`：新增 queue/outbox claim 和 stuck recovery 组合索引。
- `src/workspace_files/service.py`：允许 post-execute workspace sync 在 assistant store 缺记录但 workspace 目录存在时继续扫描，修复集成测试回归。
- 部署默认值：`GO_GATEWAY_CALLBACK_STORE_TIMEOUT=2s`、`GO_GATEWAY_CALLBACK_ENQUEUE_MAX_IN_FLIGHT=4`、`GO_GATEWAY_CALLBACK_DELIVERY_CONCURRENCY=4`，与代码默认值对齐。

本轮新增/复跑测试：

| 命令 | 结果 | 说明 |
| ---- | ---- | ---- |
| `cd go-gateway && go test -count=1 ./...` | passed | Go Gateway 全量单测通过 |
| `uv run pytest tests/ -v --tb=short` | `534 passed, 252 warnings` | Python 全量测试通过；原 `post_execute` workspace sync 失败已修复 |
| `kubectl kustomize deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport >/tmp/ironagent-k8s-render.yaml` | passed | K8s overlay 渲染通过 |
| `git diff --check && ! rg -n '^(<<<<<<<\|=======\|>>>>>>>)' .` | passed | 空白和冲突标记检查通过 |
| `uv run ruff check migrations/versions/017_gateway_queue_outbox_indexes.py src/workspace_files/service.py` | passed | 本轮 Python 修改文件 ruff 通过 |
| `uv run ruff check src tests scripts migrations/versions/017_gateway_queue_outbox_indexes.py --statistics --output-format=concise` | failed：119 errors | 全仓既有 lint 债，主要为 line-too-long、B008、import/order、unused import；本轮未扩大处理范围 |
| `uv run mypy src` | failed：204 errors in 36 files | 全仓既有类型债；本轮未扩大处理范围 |

本轮未做：

- 未重新打镜像和部署本轮代码；上一节线上压测仍对应 `localhost/go-gateway:redis-ha-20260524-r2`。
- 未做 MySQL 破坏性故障注入。
- 未做 60 分钟 sustained 500 并发完整业务链路压测。
- 未验证迁移 `017_gateway_queue_outbox_indexes.py` 在线上 MySQL 的实际执行时间和锁影响。

## 2026-05-24 二次专家复审与修复

专家对抗新增结论：

- P0-1：`task.accepted` outbox 入队失败后的“再取消”不是事务级闭环；任务已处于可 claim 的 `queued` 状态时，worker 可能先于补偿取消抢跑。
- P0-2：public `/api/v1/chat` 在 service 内才做 admission，文件引用校验等下游调用可能先于限流发生。
- P0-3：callback subscription 仍是 Gateway 内存态；Gateway 重启后 queued worker 完成任务时可能找不到订阅，导致最终 callback 不发。
- P1-1：outbox delivery concurrency 直接并发会破坏同一 task 的事件顺序；一次 claim 过多事件也会增加租约过期和重复投递窗口。
- P1-2：MarkDelivered/MarkDead/MarkAttempt 未校验 `locked_by`，租约丢失后仍可能覆盖后续 worker 状态。

二次修复：

- `go-gateway/internal/core/service.go`：新增 `admitting` 队列状态。`CreateChatTask` 只把任务写入不可 claim 的 `admitting`；HTTP handler 在 `task.accepted` outbox 入队成功后调用 `ActivateQueuedTask`，才转换为 `queued`。`TaskQueueStats.active` 和容量判断纳入 `admitting`。
- `go-gateway/internal/httpapi/router.go`：在 callback URL DNS 校验和 public 文件校验前执行 `CheckChatAdmission`，并用 context 标记避免 service 内重复限流；`task.accepted` 入队失败或队列激活失败时用 bounded background context 补偿取消，不再依赖客户端 request context。
- `go-gateway/internal/httpapi/router.go`：queued worker 派发最终 callback 时，如果内存 subscription 丢失，会从持久化 `GatewayTaskRecord.CallbackURL`、`RequestID`、`ConversationID`、`UserID`、`EmployeeNo` 恢复订阅。
- `go-gateway/internal/webhook/dispatcher.go`：`ProcessPending` 的 claim limit 取 `min(pending_limit, delivery_concurrency)`，避免 claim 大量事件后长时间持有租约；并按 `task_id` 分 lane，同一任务串行，不同任务并行。
- `go-gateway/internal/taskstore/mysql_webhook.go`：`MarkAttempt`、`MarkDelivered`、`MarkDead` 只允许当前 `status='sending' AND locked_by=?` 的 worker 更新；租约丢失时返回错误，不覆盖其他 worker 状态。
- `migrations/versions/017_gateway_queue_outbox_indexes.py`：补充 `locked_by,status,created_at` 与 `status,created_at,event_seq` 索引，支撑 claim 后回读和 outbox 顺序查询。

二次新增回归：

- `TestTaskQueueAdmittingTaskWaitsForActivation`：直接创建的 admitting 任务不会被 worker claim，激活后才进入 queued。
- `TestTaskQueueWorkerRecoversCallbackSubscriptionFromStore`：模拟 Gateway 重启后 subscription 丢失，queued worker 仍能从持久任务记录恢复 callback 并发送 progress。
- `TestOutboxProcessPendingCapsClaimLimitToDeliveryConcurrency`：验证 outbox claim limit 被 delivery concurrency 限制。
- `TestOutboxProcessPendingSerializesSameTaskLane`：验证同一 task 的 outbox 事件在 delivery concurrency > 1 时仍串行投递。

二次验证：

| 命令 | 结果 | 说明 |
| ---- | ---- | ---- |
| `cd go-gateway && go test -count=1 ./...` | passed | Go Gateway 全量单测通过 |
| `uv run pytest tests/ -v --tb=short` | `534 passed, 252 warnings` | Python 全量测试通过 |
| `uv run ruff check migrations/versions/017_gateway_queue_outbox_indexes.py src/workspace_files/service.py` | passed | 本轮 Python 变更文件 ruff 通过 |
| `kubectl kustomize deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport` | passed | K8s overlay 渲染通过 |
| `git diff --check` | passed | 空白检查通过 |
| `uv run ruff check src tests scripts` | failed：119 errors | 既有 lint 债，未因本轮扩大范围 |
| `uv run mypy src` | failed：204 errors in 36 files | 既有类型债，未因本轮扩大范围 |

二次复审后仍未关闭：

- 还没有把 queue capacity 做成 Redis/DB 原子 reservation；当前是“前置 admission + admitting 不可 claim”，可避免 callback 未入 outbox 的任务被执行，但不能证明高并发下 `TaskQueueStats -> EnqueueTask` 没有轻微超卖。
- 还没有做真实 MySQL 故障注入；`MarkDelivered` 写失败、claim 失败、outbox enqueue SQL 失败的线上表现仍需 canary 或隔离 namespace 验证。
- 还没有部署本轮镜像并重复 500 并发真实 `/api/v1/chat`、页面观测、DB/Redis/outbox 指标联动压测。

## 2026-05-25 真实 `/api/v1/chat` 800 并发批次

环境：

- Gateway：`http://192.0.2.10:30808`
- Namespace：`ironagent`
- 运行镜像：`localhost/go-gateway:redis-ha-20260524-r2`
- Gateway 副本数：`1`
- `GO_GATEWAY_TASK_QUEUE_MAX_PENDING=500`
- `GO_GATEWAY_CHAT_RATE_LIMIT_MAX=600`，window `1s`
- Redis master：`redis-ha-1`，`redis-ha-0` 为 replica
- 测试用户：`12064912`，绑定助手 `1001`
- Prefix：`codex-soak-800-20260525-20260525002334`
- 结果文件：`output/soak-800-20260525/codex-soak-800-20260525-20260525002334.json`

批次结果：

| 阶段 | 请求数 | 成功 | 背压/失败 | avg | p50 | p95 | p99 | max |
| ---- | ------ | ---- | --------- | --- | --- | --- | --- | --- |
| create | 800 | `570` 返回 202 | `230` 返回 429 `queue_full` | `3274.7ms` | `3969.9ms` | `4728.8ms` | `4789.4ms` | `4823.3ms` |
| cancel | 570 | `570` 返回 200 | 0 | `1824.6ms` | `1818.4ms` | `2856.4ms` | `2932.1ms` | `2962.7ms` |

outbox 追平结果：

| event_type | status | count | avg delivery | max delivery |
| ---------- | ------ | ----- | ------------ | ------------ |
| `task.accepted` | `delivered` | 570 | `4.056s` | `6.000s` |
| `task.cancelled` | `delivered` | 570 | `6.042s` | `7.000s` |

压测后状态：

- `/readyz`：healthy，`task_queue=up`，`registered=8 ready=8 draining=0 unhealthy=0`
- MySQL：`Threads_connected=24`，`Threads_running=2`，`Connection_errors_max_connections=5849`，未新增 max connection error
- task queue：`active=0`，累计 `cancelled=7061`、`completed=66`、`failed=3`
- outbox：累计 `delivered=14176`、`dead=8`；本次 prefix 无 pending/dead
- Gateway DB pool metrics：primary `WaitCount=36694`、`WaitDuration=6432.180s`；callback_outbox `WaitCount=5313`、`WaitDuration=15.388s`
- Redis commandstats 增量：`INCR +800`、`EXPIRE +800`、`GET +247`、`SET +4`，rejected/failed calls 均为 0
- Pod 状态：`go-gateway`、`mysql`、`redis-ha`、`redis-sentinel`、`security-service` 均 Running；本次未观察到新增重启

结论：

- 当前线上配置下，800 并发不会全部接收；`queue_max_pending=500` 与 Redis chat admission 共同触发背压，表现为 `570/800` accepted、`230/800` `429 queue_full`。
- accepted 的 570 个任务均能取消，callback outbox 在 7 秒内完成 accepted/cancelled 传输投递。
- 主任务链路和 callback/outbox 小池没有打爆 MySQL；MySQL `Connection_errors_max_connections` 未新增。
- 该结果证明“800 并发下可背压并清空队列”，不能证明“800 并发全部实时处理”。若目标是 800 全接收，需要提高 `GO_GATEWAY_TASK_QUEUE_MAX_PENDING`、限流阈值、worker/DB/callback 吞吐，并重新做连接预算。

## 2026-05-25 新版本部署后接口测试与阶梯压测

环境：

- Gateway：`http://192.0.2.10:30808`
- Namespace：`ironagent`
- 运行镜像：`localhost/go-gateway:callback-flow-20260525-r1-46ad2da3`
- Admin 镜像：`localhost/ironagent-admin:callback-flow-20260525-r1-46ad2da3`
- Security Service 镜像：`localhost/ironagent-security-service:callback-flow-20260525-r1-46ad2da3`
- 测试用户：`12064912`，绑定助手 `1001`
- 阶梯压测 prefix：`codex-newver-load-20260525115444`

接口测试结果：

| 用例 | 结果 |
| ---- | ---- |
| `GET /readyz` | 200 |
| `GET /api/v1/health?user_id=12064912` | 200 |
| `GET /api/v1/health` 缺少 `user_id` | 400 |
| `POST /api/v1/chat` 缺少 `request_id` | 400 |
| `POST /api/v1/chat` 未知字段 `assistant_id` | 400 |
| `POST /api/v1/chat` 引用不存在文件 | 422 |
| `POST /api/v1/chat` 正常创建 | 202 |
| `POST /api/v1/chat` 完全相同 payload 幂等重放 | 202，返回同一个 `task_id` |
| `POST /api/v1/chat` 同 `request_id` 不同 payload | 400 |
| `GET /api/v1/tasks/{task_id}` | 200 |
| `POST /api/v1/tasks/{task_id}/cancel` | 200 |
| `GET /api/v1/tasks/{task_id}` 取消后查询 | 200，状态 `cancelled` |
| `POST /api/v1/files/upload` | 200 |
| `GET /api/v1/files/{file_id}` | 200 |
| `GET /api/v1/files/{file_id}/download` | 200 |

阶梯压测结果：

| 并发 | 创建结果 | 创建 P95 | 取消结果 | 取消 P95 |
| ---- | -------- | -------- | -------- | -------- |
| 100 | `100` 返回 202 | `742.9ms` | `100` 返回 200 | `619.1ms` |
| 300 | `300` 返回 202 | `2624.9ms` | `300` 返回 200 | `1740.7ms` |
| 500 | `500` 返回 202 | `4641.4ms` | `500` 返回 200 | `2936.6ms` |
| 800 | `598` 返回 202，`202` 返回 429 | `5497.8ms` | `598` 返回 200 | `3377.0ms` |

callback outbox 追平结果：

| 阶梯 | event_type | status | count | avg delivery | max delivery |
| ---- | ---------- | ------ | ----- | ------------ | ------------ |
| c100 | `task.accepted` | `delivered` | 100 | `0.880s` | `2.000s` |
| c100 | `task.cancelled` | `delivered` | 100 | `4.500s` | `8.000s` |
| c300 | `task.accepted` | `delivered` | 300 | `2.470s` | `7.000s` |
| c300 | `task.cancelled` | `delivered` | 300 | `13.697s` | `17.000s` |
| c500 | `task.accepted` | `delivered` | 500 | `12.598s` | `21.000s` |
| c500 | `task.cancelled` | `delivered` | 500 | `28.574s` | `42.000s` |
| c800 | `task.accepted` | `delivered` | 598 | `46.236s` | `64.000s` |
| c800 | `task.cancelled` | `delivered` | 598 | `80.102s` | `97.000s` |

压测后状态：

- `/readyz`：healthy。
- Pod 状态：`go-gateway`、`admin`、`security-service` 均 Running，restart count 均为 0。
- MySQL：`Threads_connected=19`，`Threads_running=2`，`Connection_errors_max_connections=5849`，未新增 max connection error。
- Redis：`rejected_connections=0`，`evicted_keys=0`，`total_error_replies=4` 未变化。
- task queue：本次 prefix `1498` 个 accepted task 全部 `cancelled`，全局 active 为 0。
- outbox：本次 prefix `2996` 条事件全部 `delivered`，无 pending/dead；全局 outbox 累计 `delivered=17178`、`dead=8`。
- Gateway DB pool metrics：primary `open=10 in_use=0 max=40`；callback_outbox `open=2 in_use=0 max=8`。

发现：

- 未绑定用户调用 `/api/v1/chat` 当前返回 `404 southbound_error`，接口契约期望是 `400 bad_request`。
- 800 并发触发预期背压：`202` 个 `429 queue_full`。
- 800 并发期间出现 `107` 条 `public API failure audit append failed`，错误为调用 `security-service /api/v1/audit/log` 超时。该问题未阻断 chat 接收、取消和 callback 投递，但说明失败审计链路在高并发拒绝场景下需要异步化或容量治理。
- callback 最终全部 delivered，但连续阶梯压测下 c800 的 `task.cancelled` 最大投递延迟达到 `97s`。如果上游要求秒级通知，需要继续提高 callback 出站吞吐或明确 best-effort callback 语义。

## 不应重复派发

- 不应再按“给 Gateway 增加 MySQL 连接池基础配置”重复拆任务；本次已在配置、启动和部署默认值中落地。
- 不应把 Redis 当作任务/outbox 权威事实源；当前只实现 assistant cache 和 chat admission limit。
- 不应把 `implemented` 读成“数据库保护生产闭环”；当前仍缺本轮代码部署后压测、多副本全局连接预算、MySQL 故障注入和 60 分钟以上 sustained soak。

## 未完成或待验证

- 验证 callback 最终态 `files[].download_url` 与上游业务落库。
- 多副本上线前记录最终全局连接预算：Gateway 副本数、MySQL `max_connections`、主池 `max_open`、callback/outbox 池 `max_open`、Admin/runtime/ops 预留连接和安全系数。
- 当前 soak 约 29 分钟且通过；正式生产前建议再做 60 分钟 soak，并提高接近真实业务的 sustained QPS。
- 注入 MySQL 不可用或写失败，验证任务创建、outbox enqueue、claim、MarkDelivered/MarkDead 的 fail-closed 行为；本轮只用单测覆盖了 `task.accepted` outbox enqueue 失败不返回 `202`。
- 本轮迁移新增组合索引，生产执行前需要在同版本 MySQL 上 dry-run/EXPLAIN，并确认 DDL 锁影响窗口。
- 后续如接入 runtime status、health、callback latest 或 Redis Stream，必须先补对应 spec 和压测验收。

## 后续动作

- 当前镜像已完成构建和部署；下一步优先补 P0 连接预算验收表，并在部署监控中接入 DB pool stats 与 `enqueue_failed` 告警。
- 单独拆 `gateway Redis runtime-health cache`、`gateway callback/user visibility cache`、`Redis Stream emergency callback buffer` 三类后续 spec，避免 planned 能力混入当前 implemented 范围。
