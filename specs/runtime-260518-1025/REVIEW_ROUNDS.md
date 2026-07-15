# runtime-260518-1025: Runtime 状态一致性与多节点收敛规则 - 三轮复审

- Workspace: `/Users/example/Downloads/ironagent`
- Scope: spec 固化，不做业务代码修复，不触碰真实环境。
- Result: passed for `ready` spec gate after this file, `DECISIONS.md`, `PROJECT_LOG.md`, and conservative `TEST_REPORT.md` are present.

## 输入

| 专家 | 审查面 | 结果摘要 |
| ---- | ------ | -------- |
| Runtime 后端 | Python Admin、AssistantService、Docker/K8s provisioner | 当前是 probe 写回和轮询收敛；Docker healthz、pending guard、残留 runtime 恢复、reload generation/hash 是缺口。 |
| Go Gateway | registry、MySQL store、heartbeat、proxy session | 共享 DB 是最终一致前提；heartbeat 覆盖、secret 不一致、DB degraded、proxy 失败恢复 running 是风险。 |
| Admin UI | SSE、fallback、fast bootstrap、页面展示 | fallback 不能只证明 HTTP 200；必须展示 per-assistant freshness 或 partial/stale。 |
| 运维/安全反方 | 部署、RBAC、存储、证据边界 | 禁止强实时/强一致/生产 HA 表述；真实环境另开 deploy/ops。 |
| 规格治理反方 | specs 结构、ready gate、验收证据 | 复审证据必须进入 specs；TEST_REPORT 只能是 not-run 证据账本，不能当验证。 |

## Round 1: 产品范围复审

结论：通过。

通过条件：

- 删除“实时同步到每个节点”“无缝实时”“强一致”“生产 HA”等过强口径。
- 明确节点对象不能混用：Admin 实例、Gateway 实例、runtime Pod、K8s worker、浏览器页面是不同一致性域。
- 定义一致性级别：实时、近实时、最终一致、不可保证。
- 新建独立 S3 runtime spec，不把旧 `admin-260514-2331` B11 直接改写成本 spec。
- 无 DB 多 Gateway、密钥不一致、local PV/RWO、Pod 网络不可达、未迁移等写为 negative/degraded。

拦截项已处理：

- `PRODUCT.md` 只承诺可观测、可失败关闭、带 freshness 的最终一致。
- 旧 admin/runtime/gateway specs 只加窄 cross-reference，不重写既有语义。

## Round 2: 技术对抗复审

结论：通过，附带首期范围收窄。

通过条件：

- 状态模型区分 desired、observed、sync、freshness、saved config、applied runtime。
- `sync_state` 词表固定为 `current/pending/stale/degraded/failed/unknown`。
- Pending lifecycle/reload 必须有 operation guard，stale probe 不得覆盖。
- Deleted/disabled/soft-deleted assistant 不得因残留容器或 heartbeat 复活。
- Docker 与 K8s probe 都要以 gateway `/healthz` 或等价探活证明 healthy running。
- Go Gateway shared DB 模式要保护 `in_flight/drain_mode/last_error/capacity`，不能被缺省 heartbeat 清空。
- Browser proxy heartbeat 不得作为 runtime 健康证明。
- Repair 与 refresh 必须区分；repair 可能 reprovision。
- 自动 correction 写系统/runtime 日志；人工 lifecycle/capability/policy 动作写正式审计。

首期实现必须聚焦：

- Runtime 状态字段和写回语义。
- Admin SSE/fallback 可见状态与 partial/stale。
- Gateway registry 正负例和 heartbeat 覆盖语义。
- 验收证据和 secret 脱敏。

不进入首期：

- Redis/pubsub/watch。
- K8s informer/operator。
- 完整多 Admin leader election。
- 生产级 HA、RWX 存储、MySQL HA。

## Round 3: 验收与写回复审

结论：通过，`PRODUCT.md` / `TECH.md` 可以为 `ready`；`TEST_REPORT.md` 不能作为 verified 证明。

通过条件：

- 每个 B 项有验证路径。
- `TEST_REPORT.md` 明确 `not-run`，只作为证据账本。
- 任何 `implemented` / `verified` 都必须绑定当前 branch、target HEAD、dirty status、命令/API/日志/截图。
- 旧 `admin-260514-2331` B11、旧 `runtime-260513-1949` partial report、旧 `gateway-260514-1929` code-present report 都不能自动证明本 spec。
- 缺浏览器、Docker/K8s、MySQL、多 Gateway、multi Admin 证据时，对应项只能标 `partial/code-present`、`unverified`、`blocked`。

## 最终接受口径

Runtime 状态同步不是实时广播到每个节点。首期目标是在明确部署前提下，提供可观测、可失败关闭、带 freshness 的 runtime 状态收敛：Admin 侧通过 probe 和健康循环写回 observed state，Gateway 侧通过 shared MySQL registry、runtime heartbeat 和 TTL 在多实例间最终一致。未配置共享 DB、共享密钥、可达网络、可用 PVC/RBAC/迁移时，不承诺多节点一致。
