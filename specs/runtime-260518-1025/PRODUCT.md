# runtime-260518-1025: Runtime 状态一致性与多节点收敛规则

- 模块：runtime
- 创建日期：2026-05-18 10:25
- 编写人：kolbaaa14-gif
- 最近修改人：kolbaaa14-gif
- 最近修改日期：2026-05-18
- 等级：S3
- 状态：ready

## 背景

现阶段 IronAgent 不能把 runtime 状态描述为无条件实时或全节点瞬时可见。当前代码事实是：Python Admin 侧通过 Docker/K8s probe 和健康循环写回 observed runtime state；Go Gateway 侧通过 runtime registry、heartbeat、TTL，以及可选 shared MySQL 在多个 Gateway 实例之间收敛。

本 spec 定义首期可接受的 runtime 状态一致性模型：不追求强实时，不承诺所有节点立即同步，而是提供可观测、可失败关闭、带 freshness 的最终一致状态视图。

## 摘要

问题：当前后台页面、Python runtime probe、Go Gateway registry、多节点部署和状态验收口径混在一起，容易把“轮询/heartbeat 最终一致”误说成过强实时模型。

期望结果：明确区分 desired state、observed state、sync state、freshness、saved config 与 applied runtime；在未满足 shared DB、shared secrets、heartbeat、RBAC、网络和存储前提时，系统必须显示 degraded/unknown/failed，而不是继续宣称 runtime 已同步或健康。

## 一致性级别

| 级别 | 本 spec 允许承诺 | 本 spec 不允许承诺 |
| ---- | ---------------- | ------------------ |
| 实时 | 单次操作请求可以返回该操作触发后的当前结果 | 不保证所有页面、Gateway、Admin、runtime Pod、K8s worker 同时可见 |
| 近实时 | Admin 10 秒健康循环、SSE 快照、4 秒 fallback poll、Gateway runtime sync 周期内逐步收敛 | 不保证固定延迟；DB、K8s API、网络、probe 失败都会延迟或失败 |
| 最终一致 | shared MySQL + shared secrets + heartbeat + TTL + 无本地 registry cache 时，Gateway runtime registry 可收敛 | 不保证无 DB 多 Gateway、多 Admin 无协调、本地 PV/hostPath 跨节点场景 |
| 不可保证 | 无 DB 多 Gateway、密钥不一致、Pod 网络不可达、未执行迁移、local PV 跨节点、hostPath 跨节点 | 不能写成支持或已完成 |

## 参与方

- 管理员：需要看到 runtime 当前状态、状态 freshness、失败原因和是否需要等待/重试。
- Admin 服务：负责探测 runtime、写回 observed state、提供 SSE/polling 状态视图。
- Go Gateway：负责 runtime registry、heartbeat、TTL、调度和控制页代理。
- Assistant Runtime：承载助手容器或 Pod，并暴露 gateway `/healthz` 或等价健康面。
- K8s/Docker 运行环境：提供容器状态、Pod phase、Pod IP、healthz、存储和网络事实。
- Workspace 与 skill/plugin：能力安装、workspace refresh、policy change 可能要求 runtime reload/restart。
- 运维人员：配置 shared DB、shared secrets、RBAC、StorageClass、镜像和负载均衡健康检查。

## 生效时机

- 发布后，新状态模型只对新代码路径和新验收报告生效。
- 未实现前，本 spec 只作为 ready 规格，不代表当前系统已支持全部行为；`TEST_REPORT.md` 当前只能作为 not-run 证据账本。
- Admin 操作后可以立即触发 probe 或 refresh，但最终 runtime 状态必须以后续 observed state 和 freshness 为准。
- Go Gateway 多实例一致性只有在所有实例满足 shared DB、shared secrets、heartbeat 和 migration 前提后才成立。

## 权限与安全边界

- runtime 状态同步不得暴露内部服务凭证、gateway token、runtime token、cookie、API key、workspace 内部敏感路径或内部 stack trace。
- 自动 health correction 写系统事件日志或 runtime 日志；人工生命周期、能力安装/升级/禁用/卸载、policy apply/reload 写正式审计。
- 普通用户不能通过状态接口扩大可见 runtime 范围。
- deleted、soft-deleted、disabled assistant 不得被残留容器或 Gateway heartbeat 自动复活。

## 非目标

- 本 spec 不实现强实时广播。
- 本 spec 不引入 Redis、pubsub、K8s informer、runtime operator、leader election 或分布式锁。
- 本 spec 不声明完整生产 HA。
- 本 spec 不改变 `gateway-260514-1929` 的 control session 鉴权语义。
- 本 spec 不重写 `runtime-260513-1949` 的能力策略“重启后生效”产品语义。
- 本 spec 首轮实现不得触碰真实环境，除非另开 deploy/ops 确认。

## 行为

B1. 当系统展示助手 runtime 状态时，必须区分 persisted desired state 与 observed runtime/container state；数据库状态不能单独作为 runtime 实际健康的证明。

B2. Admin 可见的生命周期状态必须继续兼容 `pending`、`running`、`stopped`、`starting`、`stopping`、`restarting`、`paused`、`failed`；更细的同步语义必须通过 `sync_state`、`observed_state`、`last_observed_at`、`last_reconciled_at`、`last_runtime_error` 或等价字段表达。`sync_state` 新规范值为 `current`、`pending`、`stale`、`degraded`、`failed`、`unknown`；旧 `synced` 只能兼容映射为 `current`。

B3. 当数据库显示助手应为 running，但当前 probe 发现容器/Pod missing、unhealthy、不可达或 healthz 失败时，系统必须失败关闭并显示 degraded/failed/starting，不得继续报告健康 running。

B4. 当 stopped、deleted、soft-deleted 或 disabled assistant 仍残留运行容器时，系统不得从容器事实自动复活助手；必须停止/隔离 runtime 或显示 drift/failure。

B5. 当 start、stop、restart、delete、repair、reload 正在进行时，reconciler 必须保留 pending operation 与关联上下文；stale health check 不得覆盖进行中的操作状态。

B6. 当 runtime 证据 stale、不可用或 contradictory 时，UI/API 必须展示 freshness 和不确定性，不得宣称 current、healthy 或 applied。

B7. 当 workspace refresh、capability install/upgrade/disable/uninstall、policy change 需要 runtime reload/restart 时，系统必须区分 saved config 与 applied runtime generation/hash/readiness。

B8. 当 reload/restart 成功时，系统只有在 observed runtime 证明目标 generation/hash 或等价状态已经生效后，才能清除 pending sync/reload。

B9. 当 reload/restart/reconciliation 失败时，Admin 必须展示明确失败状态和错误摘要；人工动作写正式审计，自动纠偏写系统/runtime 日志。

B10. 当 assistant 有 active task 时，restart/reload 必须按已确认的非中断规则处理：等待、延迟或标记 pending，不得静默杀任务后宣称同步成功。

B11. Admin SSE/WebSocket 推送和 polling fallback 必须带 freshness metadata，使页面能区分 current、fallback、stale、degraded、error。

B12. SSE 失败后的 4 秒 fallback 不得只证明 HTTP 请求成功；它必须更新可见 assistant status，或明确提示 fallback 为 partial/stale。

B13. 当页面 hidden/background 时，前端可暂停 fallback polling，但必须在恢复可见后刷新状态，并提示最后同步时间或 stale 状态。

B14. Go Gateway 多节点 runtime registry 只有在所有实例配置同一个 `GO_GATEWAY_DATABASE_URL`、共享必要 secrets、执行 migration、持续 heartbeat 且 TTL 配置合理时，才能声明最终一致。

B15. 未配置 shared DB 的多 Gateway 部署必须被视为 unsupported/negative case；每个实例的 in-memory registry 不得被描述为跨节点共享。

B16. K8s runtime 只有在 Pod phase、Pod IP、gateway `/healthz`、RBAC、网络策略和 workspace 存储前提都满足时，才能显示为 current running；Pod phase 为 Running 但 `/healthz` 不可达时不能报告 healthy running。

B17. 多 Admin 副本运行时，必须配置单写者、leader election、分布式锁、CAS/version guard 或明确禁止多副本；否则不能声明 runtime observed state strict consistency。

B18. 所有 runtime 状态同步证据必须包含当前分支、目标 HEAD、工作区状态、命令/API/日志/截图来源；旧 `admin-260514-2331` B11 证据不能作为本 spec verified 证明。

B19. Go Gateway heartbeat、runtime sync 或 browser proxy session 不能无证据覆盖调度事实；`in_flight`、`drain_mode`、人工 `last_error`、capacity、deleted/disabled 状态必须被保留或校验，proxy 失败不得把 stale/down runtime 恢复成 healthy/running。

## 已确认决策

- 本 spec 作为新的 S3 runtime spec 固化，spec-id 为 `runtime-260518-1025`。
- `PRODUCT.md` / `TECH.md` 经 3 轮复审后进入 `ready`；复审记录固化在 `REVIEW_ROUNDS.md`。
- `TEST_REPORT.md` 保留为 not-run 证据账本，不作为验收证明。
- 关键取舍固化在 `DECISIONS.md`，执行日志固化在 `PROJECT_LOG.md`。
- 后续修复必须先按本 spec 实现，再用当前有效 `TEST_REPORT.md` 证明 `implemented` 或 `verified`。

## 待确认问题

- 无。长期架构中的 runtime sidecar/operator、K8s informer、Redis/pubsub、RWX 生产存储、MySQL HA 和多 Admin leader election 细节不在本次最小修复范围内；如需要展开，另开 S3 spec。
