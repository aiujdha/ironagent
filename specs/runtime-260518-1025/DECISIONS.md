# runtime-260518-1025: Runtime 状态一致性与多节点收敛规则 - 决策记录

- 模块：runtime
- 创建日期：2026-05-18 10:25
- 最近修改日期：2026-05-18
- 状态：ready

## D1. 一致性承诺

接受：runtime 状态是 `probe + persisted observed state + SSE/polling + Gateway shared DB/heartbeat/TTL` 的最终一致模型。

拒绝：不得承诺“runtime 状态无缝实时同步到每个节点”、“所有节点强一致”、“所有页面和 Gateway 立即可见”。

理由：当前 Python Admin、Go Gateway、Admin UI 都是请求触发、轮询、heartbeat 或 shared DB 收敛；没有 watch/pubsub/K8s informer/runtime operator。

## D2. 首期架构边界

首期修复不引入 Redis、pubsub、K8s informer、runtime operator、完整 leader election、分布式锁或生产级 HA。

允许在当前模型上补齐：

- desired state、observed state、sync state、freshness。
- pending operation guard。
- saved config 与 applied runtime 的最小等价证据。
- Admin SSE/fallback 可见状态和 degraded/stale 提示。
- Gateway shared MySQL 正例与无 DB negative case。

长期能力如 runtime sidecar、RWX 存储、MySQL HA、多 Admin leader election、事件总线，另开 S3 spec。

## D3. 状态词表

`sync_state` 规范词表固定为：

- `current`
- `pending`
- `stale`
- `degraded`
- `failed`
- `unknown`

不使用 `synced` 作为新规范值。兼容旧 payload 时可以映射 `synced -> current`，但 UI/API 新输出应使用规范词表。

旧生命周期 `status` 继续兼容：

- `pending`
- `running`
- `stopped`
- `starting`
- `stopping`
- `restarting`
- `paused`
- `failed`

## D4. Saved vs Applied

能力、workspace refresh、policy change 可以先实现最小等价语义：

- `restart_required`
- `pending_reload`
- `applied_after_restart`
- `reload_failed`

如果 runtime 不能回报 generation/hash/readiness，不能把 B7/B8 标为 verified。实现可以先标 partial，并在 TEST_REPORT 中写明 generation/hash 证据缺口。

## D5. Gateway Registry

多 Gateway runtime registry 只有在以下前提全部满足时，才能声明最终一致：

- 所有实例共享同一个 `GO_GATEWAY_DATABASE_URL`。
- Alembic migration 已执行，runtime registry 表结构完整。
- 所有实例共享 `GO_GATEWAY_PROXY_SESSION_SECRET`、`GO_GATEWAY_HMAC_SECRET`、scoped API keys、callback/presign secrets。
- runtime heartbeat interval 小于 `GO_GATEWAY_RUNTIME_HEARTBEAT_TTL` 并留有抖动余量。
- heartbeat 不得无意覆盖 `in_flight`、`drain_mode`、人工 `last_error`、capacity 等调度字段。

无 DB 多 Gateway 必须作为 unsupported/negative case。

## D6. Browser Proxy Heartbeat

浏览器 control/proxy session 的访问或懒注册 heartbeat 不能作为 runtime 健康证明。

如果 proxy 失败，系统必须标记 degraded/unhealthy，或至少不得把 stale/down runtime 恢复成 healthy/running。

## D7. Admin UI

Admin 操作按钮的成功 toast 只能表示 command accepted。

最终 healthy/current 必须由后续 SSE/fallback/full bootstrap 的 observed state 证明。

SSE 失败后 fallback 必须覆盖当前可见助手状态，或明确显示 partial/stale。HTTP 200 本身不能作为页面已同步的证据。

## D8. 真实环境

本 spec 首轮不触碰真实环境、不做部署、不打镜像。需要线上验证或发布时，另开 deploy/ops 确认。
