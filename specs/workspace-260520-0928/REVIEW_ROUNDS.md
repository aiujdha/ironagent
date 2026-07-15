# workspace-260520-0928: 管理后台工作区索引化与同步任务化 - 专家复审记录

- 工作目录：`/private/tmp/ironagent-workspace-phase1-clean`
- 写入范围：仅复审记录；本轮不改业务代码、不触碰真实环境、不补造测试结论。
- 当前结论：原始 P0 已通过规格修订关闭，但状态仍保持 `draft`；必须等待用户确认后才可改为 `ready`，之后按 Phase 1 实现。

## 专家视角

| 专家 | 审查面 | 结果摘要 |
| ---- | ------ | -------- |
| 产品与规格治理 | specs 状态、阶段边界、待确认项 | 原稿方向正确，但待确认问题和 job 语义冲突阻止进入 ready；修订后已关闭待确认项并明确 Phase 1/2/3。 |
| 后端数据兼容 | Admin API、workspace_files、assistant workspace items | 原稿没有定义纯 DB 查询路径，且旧查询可能触发 COS hydrate；修订后明确 DB-only 入口和旧函数禁用边界。 |
| SRE/性能/迁移 | 500-2000 助手、worker 限流、迁移 | 原稿缺少硬指标；修订后补 2000 助手模拟数据、page size、p95、SQL 次数、COS list=0、worker 配额。 |
| 安全/前端 | 鉴权、跨助手泄露、UI 状态 | 原稿权限过粗且前端仍可能沿用阻塞式刷新；修订后补接口鉴权矩阵、用户侧禁止复用后台全局索引、UI job 状态机。 |
| 集成风险 | 审批 apply、市场 catalog、公开 API、后台/用户侧边界 | 原稿容易被误解为要重写多条既有链路；修订后明确只补索引写入和读取路径，不重写审批 apply、不改市场 catalog、不改公开 API、不把后台索引暴露给用户侧。 |

## 原始 P0/P1 与本次处理

| 级别 | 原始问题 | 本次处理 | 当前状态 |
| ---- | -------- | -------- | -------- |
| P0 | 待确认项未关闭，但 `DECISIONS.md` 已写成接受项，spec 不能直接进入 ready。 | 把能力安装索引、Phase 1 范围、默认展示最近同步状态整理为明确决策，并保留用户确认 gate。 | 规格 P0 已关闭；仍需用户确认后才能 ready。 |
| P0 | Phase 1 一处要求 job/status，另一处允许同步执行，刷新当前助手语义冲突。 | Phase 1 固定为 UI 不传 `sync=true`、后端 DB-only 分页、单助手刷新创建 job 并返回状态。 | 规格 P0 已关闭；实现期按 Phase 1 验证。 |
| P0 | 后台 summary 和旧查询函数可能继续 fan-out、COS list、legacy hydrate 或本地 workspace scan。 | 明确新增纯 DB 索引查询入口，并禁止 Phase 1 页面查询调用带副作用旧路径。 | 规格 P0 已关闭；测试仍为未执行。 |
| P0 | 新索引查询层如果被用户侧弱鉴权路径复用，会产生跨助手/跨用户泄露。 | 补鉴权矩阵，要求用户侧文件查询维持公开 API 认证边界，且不得复用后台全局索引接口。 | 规格 P0 已关闭；安全用例待实现后验证。 |
| P1 | 500-2000 助手规模、p95、SQL 次数、COS list 次数、worker 并发和迁移回滚缺硬指标。 | 补性能、任务、迁移、回滚和监控验收项，并写入 `TEST_REPORT.md` 的待验证骨架。 | 已转为验收项；当前不声明通过。 |
| P1 | 能力安装索引写路径、审批 apply、市场安装、runtime/index scan、COS import 的集成边界不清。 | 补写路径矩阵和非干扰条件，说明只补索引状态和验收，不重写既有业务链路。 | 已转为实现约束；Phase 2/3 继续验证。 |

## 非干扰条件

本次复审把方案收敛到索引化和同步任务化，不扩大到其它业务重构。后续实现必须同时满足：

- 不重写审批 apply；审批通过写 workspace 仍复用现有 apply 链路，只补 `workspace_files` 索引状态和验收。
- 不改市场 catalog；市场列表、发布流程和 catalog 查询不属于本 spec，只处理已安装能力的索引写入和展示读取。
- 不改变公开 API 契约；用户侧文件接口继续走原有认证和可见性规则。
- 不把后台全局索引暴露给用户侧；即使传 `user_id`，也不能绕过认证主体和助手归属过滤。
- 不把 Phase 1 做成全量扫描；页面默认读取 DB 索引，COS import、reconcile、全量刷新必须任务化、限流、可审计。

## Round 1: 产品范围复审

初审结论：方向通过，ready 不通过。

发现的问题：

- 待确认项没有关闭，但 `DECISIONS.md` 已经写成接受项。
- 刷新当前助手一处要求 job/status，另一处又允许同步执行，语义冲突。
- 500-2000 助手验收没有硬指标。

修订结果：

- 能力安装索引被明确为目标架构必做项，Phase 2 落地。
- Phase 1 明确为 UI 不传 `sync=true` + 后端 DB-only 分页 + 刷新 job/status 语义。
- 默认展示最近同步状态被明确接受，页面必须显示 stale/sync 状态。

## Round 2: 技术对抗复审

初审结论：方案可落地，但旧函数副作用必须显式规避。

发现的问题：

- 当前全局 summary 即使不传 `sync=true`，仍会全助手 fan-out 并优先查 COS 摘要。
- `WorkspaceFileService.list_files()` 可能触发 legacy COS hydrate，不能直接当 DB-only 查询。
- `WorkspaceSyncService.async_sync_assistant_workspace()` 默认 `mark_missing=True`，不能用于普通刷新。
- `WorkspaceFileService.scan_workspace_results()` 也存在缺失即删除记录的行为。
- 能力安装索引写路径不够具体。

修订结果：

- `TECH.md` 明确新增纯 DB 索引查询入口。
- 禁止 Phase 1 summary 调用 sync、COS list、legacy hydrate 或本地 workspace scan。
- 明确 `index_only`、`reconcile`、`cos_import` 三种同步模式。
- 补写路径矩阵，覆盖 install/upgrade、toggle、reuse、remove、approval apply、runtime/index scan、cos_import。

## Round 3: 安全与前端复审

初审结论：需要补鉴权矩阵和 UI 状态机。

发现的问题：

- 新 DB 索引查询层如果被弱鉴权 `user_id` 参数路径复用，会产生跨助手/跨用户泄露风险。
- 刷新任务体验不完整，旧 UI 是阻塞式 `?sync=true`。
- 审计要求偏原则化。
- 分页会改变现有文件树语义。

修订结果：

- 补接口鉴权矩阵，区分 admin 全局读、单助手读、workspace-items、单助手刷新、全量刷新、reconcile、COS import、用户侧文件查询。
- 明确用户侧不得复用后台全局索引 API。
- 补 UI 状态机：首次打开、刷新当前助手、刷新成功、刷新失败、全量刷新、能力页 `force=true` 兼容。
- 补树形与分页契约：根节点、搜索、`assistantIds`、文件夹计数都必须重新定义。

## Round 4: 运维与验收复审

初审结论：需要补性能、迁移、监控、TEST_REPORT 骨架。

发现的问题：

- 缺少 2000 助手规模数据、文件数、能力数、p95/p99、SQL 次数、COS list 次数。
- 缺少 worker 并发、超时、重试、running 回收规则。
- 缺少 migration/backfill 去重、可重入、失败续跑和回滚验收。
- `TEST_REPORT.md` 缺失。

修订结果：

- 补 2000 助手、200000 workspace file rows、20000 capability rows 的性能验收基准。
- 补默认 page size 50、最大 200、GET COS list=0、SQL 次数不超过 5、p95 目标。
- 补 worker 配额与任务规则。
- 补迁移和回滚计划。
- 新增 `TEST_REPORT.md` 骨架，当前结论为 not-run/unverified。

## Round 5: 集成风险复审

初审结论：方案可以推进，但必须避免把索引优化误落成既有链路重写。

发现的问题：

- 审批 apply 已有写入链路，若另起写入路径会制造不兼容状态。
- 市场 catalog 和已安装能力索引是两个问题，不能把本 spec 扩大成市场重做。
- 公开业务 API 已有调用方，索引化不能改变用户侧契约。
- 后台全局索引适合管理员页面，不适合给用户侧 API 直接复用。

修订结果：

- 明确审批通过写 workspace 必须复用现有 apply 链路，只补索引状态和验收。
- 明确市场 catalog 列表和发布流程不在本 spec 范围内。
- 明确不改变公开业务 API 契约，不扩大用户侧文件接口权限。
- 明确后台全局索引只能服务后台授权场景，用户侧必须继续按用户-助手绑定过滤。

## Ready Gate

当前文档只能说明“专家复审 P0 已在规格层关闭”，不能说明“实现完成”或“测试通过”。进入 ready 的 gate 是：

1. 用户确认本次修订后的 Phase 1/2/3 范围、非干扰条件和验收口径。
2. 用户确认后，才允许把相关 spec 状态从 `draft` 改为 `ready`。
3. `ready` 后先按 Phase 1 实现：UI 不传 `sync=true`、后端 summary DB-only 分页查询、单助手刷新 job/status。
4. Phase 1 实现后再用真实测试、截图、API 输出或 EXPLAIN 等证据更新 `TEST_REPORT.md`；本复审记录不新增任何测试通过结论。

## 第六子代理后执行模式记录

第六子代理后的 Phase 1 验收与文档子代理只补 `TEST_REPORT.md` 的 pending/unverified 证据清单和本文件的执行模式记录；不改业务代码，不运行或冒充真实测试结果，不修改 `PRODUCT.md` / `TECH.md` / `DECISIONS.md` 的 `draft` 或 ready gate 状态。主线程完成真实测试后，再按证据更新结论。

## Phase 2 执行模式记录

Phase 2 按 5 个 worker 并行 + 主线程汇总的模式推进。5 个 worker 分别处理能力安装索引模型、`workspace-items` DB-only 查询、能力写路径 helper、前端能力页文案/状态、验收记录与测试清单；主线程负责等待并行结果、解决冲突、补齐真实测试证据并决定是否提交。

本轮范围只覆盖能力安装索引及其后台展示链路：`assistant_workspace_items` 或等价索引、`workspace-items` 默认 DB-only、`force=true` 不扫描不建 job、install/upgrade/reuse/toggle/remove 后索引 helper、UI 索引状态文案。本轮不包含 Phase 3 reconcile/backfill、COS import、orphan/degraded 修复、删除候选清理或批量全量校验。

第六个汇总复审子代理确认 GET 路径已是 DB-only，`force=true` 不扫描不建 job，且未恢复 `X-Admin-Token`；同时指出两个落地缺口：写路径 helper 尚未接入真实业务链路，文档中“能力安装索引 + `workspace_files` 聚合”与本轮实现的独立 `assistant_workspace_items` 查询口径不完全一致。

主线程按最小必修项修正：新增 `assistant_workspace_items` SQLite 索引数据层，后台 `workspace-items` GET 只查该索引；market install/upgrade 共享安装路径写索引；后台 toggle/reuse/delete 成功后写索引；reload 失败保留索引并标记 `runtime_reload_status=failed`、`runtime_effective=false`。`workspace_files` 聚合、backfill、reconcile、COS import、orphan/degraded 修复、批量全量校验仍保留到 Phase 3。

本轮后台鉴权继续使用 `X-IronAgent-Internal-Token`，不得恢复 `X-Admin-Token`。测试证据记录在 `TEST_REPORT.md`；浏览器截图、真实 HTTP、SQL EXPLAIN 和规模压测未在本轮声明通过。

## Phase 3 执行模式记录

Phase 3 按 5 个 worker 并行 + 第 6 个汇总复审的模式推进。Worker 5 只负责 specs/test report 收敛，写入范围限定为 `TEST_REPORT.md` 和 `REVIEW_ROUNDS.md`；不改业务代码，不运行或冒充测试结果，不修改 `PRODUCT.md` / `TECH.md` / `DECISIONS.md`。

本轮 Phase 3 只做最小可落地骨架：`workspace_files` 聚合、backfill helper、`reconcile` / `cos_import` job mode skeleton、admin route skeleton。对象清理、真实 COS import、规模压测、SQL EXPLAIN、真实 HTTP、浏览器 Network、迁移回滚和线上验证留待后续，除非主线程实际补齐证据并回填。

B7/B9/B10/B11/B16/B18/B19 在本轮只能拆成 `partial` / `unverified`，不得写 full pass。`partial` 只表示骨架或代码层槽位覆盖一部分口径；第 6 个汇总复审确认真实测试、路由、日志、SQL 或截图证据后，才允许升级单项结论。

本轮后台鉴权继续保持 `X-IronAgent-Internal-Token`，不得恢复 `X-Admin-Token`。如需提到 `X-Admin-Token`，只能作为历史风险或禁止恢复项记录。

第 6 个汇总复审发现一个实现阻塞：Admin route 已允许 `reconcile/cos_import` 的 `capability` / `bucket` 等显式 scope，但 `WorkspaceSyncJobStore` 仍只允许 assistant 空 scope，导致真实 service 下这些请求会失败。主线程已按最小补丁修正：`index_only` 继续限定 assistant 空 scope；`reconcile/cos_import` 允许 `assistant/bucket/capability/all_visible` 等维护 scope，其中 `all_visible` 不允许 scope slug；并补真实 store 单测覆盖 `reconcile + capability`、`cos_import + bucket`、`all_visible` scope，以及非 `index_only` 不误入 index-only worker。

本轮最终测试证据已回填到 `TEST_REPORT.md`。对象清理、真实 COS import、SQL EXPLAIN、规模压测、浏览器 Network、迁移失败续跑和回滚仍未声明通过。

## 最终接受口径

本方案不是牺牲准确性换速度，而是把准确性从“页面实时扫描”转为“写路径索引 + 后台同步补偿 + 显式时效性”。后台页面默认展示最后一次可信索引状态；当前助手可手动刷新，全量刷新和 COS 校验必须任务化、限流、可审计。

当前修订已经关闭专家复审 P0，但状态仍保持 `draft`。用户确认后才可改为 `ready` 并进入 Phase 1 实现拆分；P1 已转成实现期验收项和风险约束，不能被写成已验证。
