# runtime-260518-1025: Runtime 状态一致性与多节点收敛规则 - 技术方案

- 模块：runtime
- 创建日期：2026-05-18 10:25
- 编写人：kolbaaa14-gif
- 最近修改人：kolbaaa14-gif
- 最近修改日期：2026-05-18
- 等级：S3
- 状态：ready

## 背景

- 产品 spec：`./PRODUCT.md`
- 相关既有 specs：
  - `specs/admin-260514-2331/{PRODUCT,TECH,TEST_REPORT}.md`
  - `specs/runtime-260513-1949/{PRODUCT,TECH,TEST_REPORT}.md`
  - `specs/gateway-260514-1929/{PRODUCT,TECH,TEST_REPORT}.md`
- 本 spec 内部治理文件：
  - `DECISIONS.md`
  - `PROJECT_LOG.md`
  - `REVIEW_ROUNDS.md`
  - `TEST_REPORT.md`
- 当前代码入口：
  - `src/assistants/service.py`
  - `src/runtime/provisioner.py`
  - `src/runtime/kubernetes.py`
  - `src/admin_web/app.py`
  - `src/admin_web/static/index.html`
  - `go-gateway/internal/runtimepool/registry.go`
  - `go-gateway/internal/taskstore/mysql_runtime.go`
  - `go-gateway/cmd/server/main.go`
  - `go-gateway/internal/config/config.go`

编码前必须重新核实目标分支代码。本 spec 不能替代当前代码证据。

## 影响范围

- Admin runtime status list/detail、SSE、polling fallback、lifecycle buttons。
- Assistant persistence 中 runtime observed fields、timestamps、last error、reload/readiness。
- Python runtime provisioner 的 Docker/containerd/K8s probe 和 healthz 判定。
- Go Gateway runtime registry、heartbeat、TTL、capacity/in-flight、shared MySQL。
- Workspace refresh、capability reload、policy apply/restart 的 saved-vs-applied 状态。
- 系统日志、正式审计、验收报告和部署前检查。

## 修改方案

首期修复不换架构，只在现有 probe/heartbeat/shared-DB 模型上补齐状态语义、fallback 合并和验收门槛。

必须改或补齐的方向：

- 定义 desired state、observed state、sync state、freshness 和 saved/applied generation/hash。
- 将 `AssistantService.sync_runtime_views()` 作为 Python observed runtime 写回主路径之一，底层 provisioner 只返回 probe，不直接写 repository。
- Docker `ensure_runtime()` 与 K8s probe 必须统一健康口径；不能只因解析到 gateway endpoint 就直接写 healthy `running`。
- Admin SSE 与 fallback 状态 payload 必须能更新 assistant status，并暴露 freshness/degraded/error。
- Go Gateway shared MySQL 模式下以 `gateway_runtimes` 为当前 registry 权威，以 `gateway_runtime_heartbeats` 为历史证据。
- Go Gateway heartbeat/proxy/runtime sync 不得清空 `in_flight`、`drain_mode`、人工 `last_error`、capacity，或绕过 deleted/disabled 校验。
- 无 DB 多 Gateway 必须作为 negative case 验收。
- Pending lifecycle/reload 不得被 stale probe 覆盖。
- Deleted/disabled assistant 不得因残留容器或 heartbeat 自动复活。

不修改的范围：

- 不新增 Redis/pubsub/watch。
- 不新增 K8s informer/operator。
- 不引入多 Admin leader election 的完整实现，除非实现阶段确认必须支持 Admin 多副本；否则写成禁止或单写者前提。
- 不修改公开业务 API 安全策略。
- 不触碰真实环境。

## 首期实现范围

| 分类 | 范围 | 说明 |
| ---- | ---- | ---- |
| 必做 | Python observed state 写回、Docker/K8s healthz 口径、pending guard、deleted/disabled fail-closed | 先保证状态不误报 healthy/current。 |
| 必做 | Admin SSE/fallback payload 与前端合并展示 freshness/degraded/partial/stale | HTTP 200 不能替代 DOM 状态证据。 |
| 必做 | Gateway heartbeat 覆盖语义、shared DB 正例、无 DB negative case、secret mismatch negative case | 多节点只在前提满足时成立。 |
| 可等价实现 | saved/applied generation/hash | 若 runtime 不能回报 generation/hash，可先用 `restart_required`、`pending_reload`、`applied_after_restart` 等最小等价语义，但 B7/B8 不得标 verified。 |
| 不做 | Redis/pubsub/K8s informer/operator/完整 leader election/生产 HA/RWX/MySQL HA | 后续另开 S3 spec。 |

## 状态模型

实现可以新增字段，也可以使用等价字段表达，但必须覆盖以下语义：

| 语义 | 建议字段 | 说明 |
| ---- | -------- | ---- |
| 期望生命周期 | `desired_state` | operator 或系统希望 runtime 处于什么状态 |
| 观测状态 | `observed_state` | Docker/K8s/Gateway healthz 当前观测到的状态 |
| 同步状态 | `sync_state` | `current`、`pending`、`stale`、`degraded`、`failed`、`unknown` |
| 上次观测时间 | `last_observed_at` | runtime probe 成功或失败发生时间 |
| 上次收敛时间 | `last_reconciled_at` | observed state 写回或被确认的时间 |
| runtime 错误 | `last_runtime_error` | operator 可读、已脱敏错误摘要 |
| 配置版本 | `desired_generation` / `desired_hash` | 已保存 workspace/capability/policy 配置 |
| 生效版本 | `applied_generation` / `applied_hash` | runtime 已证明加载的配置版本 |

旧生命周期字段 `status` 必须继续兼容 `pending`、`running`、`stopped`、`starting`、`stopping`、`restarting`、`paused`、`failed`。旧 `syncState="synced"` 只能兼容映射为 `current`，新接口和新测试不得继续引入 `synced` 作为规范值。如果实现暂不新增完整字段，必须在 API/UI payload 中提供等价 freshness 和 degraded 信息，不能只靠 `status` 表达全部事实。

## Reconciliation Algorithm

- Reconciler 必须幂等：同一 probe 结果重复写入不得造成额外副作用。
- Reconciler 必须拒绝 stale observation：旧时间戳或不带 operation context 的 probe 不得覆盖新操作。
- Pending start/stop/restart/delete/repair/reload 必须带 operation id、started_at、target state 或等价 guard。
- Start/stop/restart/delete 人工动作可以立即写 desired state，但 observed state 需要 probe 或 runtime 回报确认。
- Deleted/soft-deleted/disabled assistant 遇到 running container 时，系统应停止/隔离 runtime 或显示 drift/failure，不得恢复为可用助手。
- Probe 失败时保留 last known values，但必须把 `sync_state` 或等价字段标为 degraded/stale/failed。
- Repair 必须与 refresh 区分；repair 可能 reprovision runtime，不得伪装成无副作用状态刷新。
- 残留 `ironagent.json`、残留容器、旧 heartbeat 只能作为 drift 证据，不得自动恢复 deleted/disabled/soft-deleted assistant 为可用状态。

## Python/Admin 语义

- Full bootstrap 和 SSE status snapshot 可以触发 `sync_runtime_views()`。
- 10 秒 health loop 是最低收敛规则，不是实时事件订阅。
- Fast bootstrap/fallback 可以为性能只 probe transient assistants，但必须在 UI 上标识 partial/stale，或改为 status-only endpoint 覆盖所有 assistant。
- SSE 与 fallback/status API 的字段契约至少包括旧 `status`、`observedStatus`、`desiredStatus`、`syncState`、`lastObservedAt`、`lastReconciledAt`、`lastRuntimeError`、`runtimeProbeDegraded`、`partial`、`pollingSource` 或等价字段。
- `runtimeProbeDegraded`、`lastError`、`lastStatusAt`、`observedStatus`、`syncState`、`lastRuntimeError` 或等价字段必须进入页面可见状态，不得只停留在 console。
- `mergeAssistantStatusPayload()` 或等价前端合并逻辑必须更新 per-assistant freshness 字段；不能只合并 `status/container/gateway`。
- 页面 hidden 时 fallback 可以暂停；恢复可见后必须触发状态刷新，并显示 last sync time。
- 操作 toast 只能表示命令 accepted；最终 runtime healthy 必须以后续 observed state 证明。
- 浏览器/控制页 SSE 鉴权必须验证同源 Cookie 或 control session 路径；内部服务凭证只允许作为 admin-only southbound/local 验证手段，不能作为浏览器方案，且必须覆盖远端/代理路径，不能只用本地 loopback 假定成立。

## Go Gateway 多节点语义

- 无 `GO_GATEWAY_DATABASE_URL`：runtime registry 是进程内 map，只支持单 Gateway 本地模式。
- 有 shared MySQL：`gateway_runtimes` 是当前 registry 权威；`gateway_runtime_heartbeats` 是历史证据，不是调度权威。
- 多 Gateway 必须共享：
  - `GO_GATEWAY_DATABASE_URL`
  - `GO_GATEWAY_PROXY_SESSION_SECRET`
  - `GO_GATEWAY_HMAC_SECRET`
  - scoped API keys
  - callback/presign 相关 secrets
- 所有实例必须执行同一套 migration。
- Heartbeat interval 必须小于 `GO_GATEWAY_RUNTIME_HEARTBEAT_TTL`；TTL 过期后 runtime 必须标为 `unhealthy` 并拒绝新调度。
- TTL 过期是 lazy expiration：由 `Get`、`Snapshot`、`Reserve`、`SelectTarget` 或 readiness/sync 触发，不是独立后台定时器，验收必须按这个语义设计。
- Heartbeat 必须明确是部分状态还是完整覆盖。部分状态必须 preserve `in_flight`、`drain_mode`、人工 `last_error`、capacity；完整覆盖则 heartbeat producer 必须携带全量字段并验证来源。
- Browser proxy session heartbeat 或 launch-ticket 懒注册不能作为健康证明；proxy 失败必须标 degraded/unhealthy，或至少不得把 stale/down runtime 恢复为 healthy/running。
- Gateway 不执行 migration，只依赖外部 Alembic；验收必须检查 runtime registry 表结构。
- DB/store 错误必须在 `readyz`、API 或日志中可观测，不得只表现为空 registry。
- `GO_GATEWAY_RUNTIME_SYNC_INTERVAL` 只表示从 Python control plane 拉取 runtime view 的周期，不是 Gateway 间同步机制。
- `LastDecision`、metrics、proxy circuit breaker 是实例本地事实，不得作为全局一致证据。
- 负载均衡健康检查必须区分 `/healthz` 与 `/readyz` 的用途；如果 scheduler/registry 不可用，`readyz` 应能阻止接收业务流量，或在 spec/部署中明确接受降级。

## K8s/Docker 与部署前提

- Docker/containerd probe 必须读取容器 state、health 和 gateway endpoint/healthz。
- K8s probe 必须读取 Pod phase、Pod IP，并探 gateway `/healthz`；Pod Running 但 `/healthz` 不可达时不得显示 healthy running。
- Admin 必须具备访问 Docker/containerd 或 K8s API 的权限。
- K8s service account 必须具备 runtime Pod/Secret/Service 相关 get/list/create/update/patch/delete 权限。
- 多节点生产不能依赖 hostPath 或 local PV + RWO；若继续使用本地盘，必须固定调度并承认不是多节点 HA。
- 如果要求 runtime Pod 可任意 worker 调度，必须使用 RWX StorageClass 或等价共享存储。
- Runtime image/init image/admin/security/gateway image tag 必须在目标节点一致可用。
- 集群时钟必须同步，heartbeat TTL、session expiry、audit time 都依赖时钟。

## Audit and Logs

- 人工 lifecycle、capability、workspace refresh、policy apply/reload 写正式审计。
- 自动 health loop、probe drift correction、TTL expiration 写系统/runtime 日志。
- 自动纠偏如果由人工动作触发并影响用户可见结果，必须能从审计 trace 或 system log 关联回原操作。
- 所有错误摘要必须脱敏，不暴露 token、cookie、secret、内部 stack trace 或 workspace 敏感路径。

## 回滚方式

- 回滚到旧状态显示时，必须保留旧 `status` 字段兼容读取。
- 如果新增字段，回滚时不得删除正式审计记录。
- 可以关闭新 UI freshness/degraded 展示，但不得继续把 stale 状态伪装为 current。
- Go Gateway 可以回退到单实例 in-memory registry，但多 Gateway 必须降级为 unsupported。
- Capability/policy reload 相关 generation/hash 字段如未被旧代码使用，应保持只读兼容或忽略。
- 回滚成功证据包括 Admin API 状态、页面状态、runtime/container 状态、Gateway registry、DB 当前行、系统日志和正式审计日志。

## 测试与证据

| 行为 | 验证方式 | 证据 |
| ---- | -------- | ---- |
| B1 | 构造 desired 与 observed 不一致的 assistant record。 | API/DB 记录显示两者可区分。 |
| B2 | 查询 Admin API/SSE/polling payload。 | 响应包含旧 `status` 与 freshness/sync 等价字段。 |
| B3 | DB 为 running，但 Docker/K8s probe 返回 missing/unhealthy/healthz fail。 | UI/API 显示 degraded/failed/starting，不显示 healthy running。 |
| B4 | deleted/disabled assistant 残留 running container。 | 系统显示 drift/failure 或停止隔离 runtime，不恢复助手。 |
| B5 | start/stop/restart/delete 进行中触发 health check。 | stale probe 不覆盖 pending operation。 |
| B6 | 模拟 probe timeout/DB/K8s API 不可用。 | UI/API 显示 stale/degraded，日志有失败原因。 |
| B7 | 安装 capability 或修改 policy 后不 reload runtime。 | saved config 与 applied generation/hash 或 `restart_required/pending_reload` 等价语义不一致。 |
| B8 | reload/restart 成功后 runtime 回报目标 generation/hash 或等价 readiness。 | pending reload 被清除，状态变 current；无 generation/hash 证据时不得 verified。 |
| B9 | 强制 reload/restart/reconciliation 失败。 | 页面失败状态、正式审计或系统日志按规则出现。 |
| B10 | 有 active task 时触发 restart/reload。 | 系统等待/延迟/标 pending，不静默杀任务。 |
| B11 | SSE 正常与失败两种路径抓包。 | 事件/polling payload 带 freshness metadata。 |
| B12 | 强制 SSE 失败并观察 4 秒 fallback。 | DOM 状态更新，或页面明确展示 partial/stale。 |
| B13 | 模拟页面 hidden 后恢复 visible。 | fallback 暂停后恢复刷新，页面展示最后同步时间。 |
| B14 | 启动双 Gateway + shared MySQL。 | A heartbeat，B 读取；B drain/unhealthy，A 拒绝调度；TTL 后两边一致 unhealthy。 |
| B15 | 启动双 Gateway 且无 DB。 | A 注册 runtime，B 不可见；作为 negative case 记录。 |
| B16 | K8s Pod Running 但 healthz 不通，再 healthz 可达。 | 前者非 healthy running，后者可变 current running。 |
| B17 | 多 Admin 副本或模拟并发写。 | 有单写者/guard，或部署明确禁止多副本。 |
| B18 | 编写 TEST_REPORT。 | 报告包含 branch、HEAD、dirty status、命令、API、日志、截图。 |
| B19 | Gateway heartbeat/proxy/runtime sync 覆盖语义。 | heartbeat 不清空 `in_flight/drain_mode`；proxy 失败不恢复 healthy；deleted/disabled 不能复活。 |

## 验收报告规则

- `TEST_REPORT.md` 当前是 not-run 证据账本，不是验收证明。
- 代码存在但缺 runtime/browser/DB/K8s 证据时，只能标 `partial/code-present`。
- `verified` 必须绑定当前 branch、目标 HEAD、工作区状态和每个 B 项证据。
- 缺 Docker/K8s/MySQL/多 Gateway/浏览器证据时，对应 B 项必须是 `blocked` 或 `unverified`。
- 旧 `admin-260514-2331` B11、旧 `runtime-260513-1949` partial report、旧 `gateway-260514-1929` code-present report 都不能自动证明本 spec。

## 风险与缓解

- 风险：4 秒 fallback HTTP 200 被误判为页面状态已更新。缓解：验收必须证明 DOM status 更新，或 UI 明确标 partial/stale。
- 风险：无 DB 多 Gateway 本地测试看似正常。缓解：必须加入 negative case，并在部署前检查 `GO_GATEWAY_DATABASE_URL`。
- 风险：Pod Running 被误当 runtime healthy。缓解：K8s probe 必须要求 gateway `/healthz`。
- 风险：多 Admin health loop 竞争写状态。缓解：实现单写者/guard，或部署禁止多副本。
- 风险：自动 repair 产生 reprovision 副作用。缓解：refresh 与 repair 分离，UI/API 明确动作含义。
- 风险：状态证据泄露 secret。缓解：browser-visible payload、日志、截图和 TEST_REPORT 都要做 secret scan。

## 后续事项

- 先按本 spec 做最小修复，再开验收。
- 如果后续要做完整 HA、runtime sidecar/operator、K8s informer、Redis/pubsub、RWX 存储和 MySQL HA，另开 S3 spec。
