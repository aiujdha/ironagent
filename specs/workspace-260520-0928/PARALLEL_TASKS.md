# workspace-260520-0928: 专家并行任务与提示词

- 模块：workspace
- 创建日期：2026-05-20
- 状态：draft
- 任务记录：completed-for-spec-draft
- 写入范围：仅 specs 文档；不改业务代码、不运行真实环境变更。

本文件记录本轮 5 个子代理并行任务的分工、提示词和主线程合并决议。后续如进入实现阶段，可继续按这些边界拆 Phase 1/2/3 任务。

## 并行任务分配

| 子代理 | 负责文件 | 专家角色 | 目标 |
| ------ | -------- | -------- | ---- |
| Hubble | `PRODUCT.md` | 产品与规格治理专家 | 收敛产品范围、Phase 边界、行为验收和非目标。 |
| Mill | `TECH.md` | 后端架构与数据一致性专家 | 固化 DB-only 查询、job/status、索引表、同步模式、权限和性能约束。 |
| Beauvoir | `DECISIONS.md` | 架构决策与风险控制专家 | 把关键争议点转为明确决策、拒绝项和验收口径。 |
| Ramanujan | `REVIEW_ROUNDS.md` | 对抗复审专家 | 复盘 P0/P1、非干扰条件、集成风险和 ready gate。 |
| Godel | `TEST_REPORT.md` | 验收与证据专家 | 建立实现后必须填充的测试、性能、权限、迁移、回滚证据矩阵。 |

## 提示词 1：PRODUCT.md

```text
你是产品与规格治理专家。只修改 specs/workspace-260520-0928/PRODUCT.md，不修改其它文件，也不写业务代码。

目标：复审并修订管理后台工作区索引化与同步任务化 spec 的产品侧内容，使其能指导后续落地。

必须覆盖：
1. 明确问题是否属实：当前展示路径不能继续依赖全助手扫描、COS list、workspace scan、legacy hydrate。
2. 固定 Phase 1/2/3 边界：Phase 1 为 UI 不传 sync=true、后端 DB-only 分页查询、单助手刷新 job/status；Phase 2 为能力安装索引；Phase 3 为 COS import/reconcile/backfill/orphan/degraded。
3. 明确 500-2000 助手规模下的行为验收，尤其分页、时效性、旧索引保留和权限边界。
4. 明确非目标：不重写审批 apply、不改市场 catalog、不改变公开 API、不引入新基础设施、不承诺 COS 人工写入实时可见。
5. 保持 spec 状态为 draft，不能写成 ready 或 verified。

输出：直接编辑 PRODUCT.md，并在最终说明列出你改动的重点和仍需实现期验证的风险。
```

## 提示词 2：TECH.md

```text
你是后端架构与数据一致性专家。只修改 specs/workspace-260520-0928/TECH.md，不修改其它文件，也不写业务代码。

目标：把 workspace 展示优化方案固化为可实现的技术设计，避免后续实现继续走扫描桶再展示的慢路径。

必须覆盖：
1. 定义纯 DB-only 查询入口，禁止默认 GET 调用 sync、COS list、workspace scan、legacy hydrate 或会产生副作用的旧 list_files。
2. 定义接口契约：workspace-files summary、单助手 summary、workspace-sync-jobs create/get、workspace-items。
3. 定义 job/status 语义和同步模式：index_only、reconcile、cos_import；普通刷新不得 mark missing 或删除对象。
4. 定义权限矩阵，尤其后台全局索引不能被用户侧 user_id 参数复用。
5. 定义数据结构、索引、迁移、backfill、worker 配额、监控、性能验收和回滚。
6. 对 500-2000 助手规模给出硬指标：2000 助手、200000 文件记录、20000 能力记录、GET COS list=0、SQL 次数上限、p95 目标。
7. 保持 spec 状态为 draft，不声明实现已完成。

输出：直接编辑 TECH.md，并在最终说明列出关键接口、表/索引、性能指标和实现期风险。
```

## 提示词 3：DECISIONS.md

```text
你是架构决策与风险控制专家。只修改 specs/workspace-260520-0928/DECISIONS.md，不修改其它文件，也不写业务代码。

目标：把专家讨论中的关键争议点沉淀为清晰决策，后续实现不得绕开这些决策。

必须覆盖：
1. 展示数据源默认 DB-only，不接受 GET 扫 COS、扫 workspace、全助手 fan-out 或 legacy hydrate。
2. 所有刷新、导入、校验、修复必须是显式 job/status，不允许 sync=true 或 force=true 绕过任务机制直接扫描。
3. 一致性承诺是最终一致，页面必须展示 last_synced_at、sync_status、sync_error、stale 或等价字段。
4. 能力安装索引是目标架构必做项，Phase 2 落地，不是可选缓存。
5. 缺失、删除、orphan、degraded 必须保守处理，普通 index_only 不删除、不最终标记 missing。
6. 首期不引入 Redis、外部 MQ、K8s informer、runtime operator。
7. 明确 Phase 1/2/3 完成口径和不能越级声明 verified 的规则。
8. 明确权限边界和迁移/回填/回滚要求。

输出：直接编辑 DECISIONS.md，并在最终说明列出已接受决策和明确拒绝项。
```

## 提示词 4：REVIEW_ROUNDS.md

```text
你是对抗复审专家。只修改 specs/workspace-260520-0928/REVIEW_ROUNDS.md，不修改其它文件，也不写业务代码。

目标：从产品、后端、SRE、安全、前端、集成风险角度复审当前 spec 是否符合需要，是否会干扰其它功能，并记录修订结果。

必须覆盖：
1. 列出原始 P0/P1 问题和修订后的处理状态。
2. 说明非干扰条件：不重写审批 apply、不改市场 catalog、不改变公开 API、不把后台全局索引暴露给用户侧、不把 Phase 1 做成全量扫描。
3. 分轮记录产品范围、技术路径、安全前端、运维验收、集成风险的发现和修订结果。
4. 明确 ready gate：当前只能说明规格层 P0 关闭，仍需用户确认后才能从 draft 改 ready。
5. 不新增任何测试通过、实现完成或线上验证结论。

输出：直接编辑 REVIEW_ROUNDS.md，并在最终说明列出剩余实现期风险。
```

## 提示词 5：TEST_REPORT.md

```text
你是验收与证据专家。只修改 specs/workspace-260520-0928/TEST_REPORT.md，不修改其它文件，也不写业务代码。

目标：建立实现后可填充的验收报告骨架，防止后续只凭代码存在就把 spec 标记为通过。

必须覆盖：
1. 当前所有结论保持 pending/unverified，不得写 passed、verified、local-verified 或 deployment-verified。
2. 为 PRODUCT.md 的 B1-B20 建立行为覆盖矩阵，列出每项实现后必须提供的证据。
3. 建立性能矩阵：2000 助手、200000 workspace_files、20000 能力记录、GET COS list=0、workspace scan=0、fan-out=0、SQL 次数、EXPLAIN、p95、响应体规模。
4. 建立权限、安全、迁移、backfill、回滚和非干扰矩阵。
5. 记录当前 dirty status caveat：本报告只覆盖 specs 文档骨架，不能证明代码、部署或线上状态。

输出：直接编辑 TEST_REPORT.md，并在最终说明列出验收前必须补充的证据类型。
```

## 主线程合并决议

- 文档状态保持 `draft`；用户确认后才可改为 `ready`。
- `page_size` 口径固定为默认 50、最大 200；`page_size>200` 返回 400，不做静默钳制。
- `sync=true` 只保留兼容解析；服务端不得执行同步、COS list、workspace scan 或全助手 fan-out。
- `workspace-items?force=true` 只返回当前索引和 `refresh_required=true`；GET 内不得创建 job，不得扫描 COS。
- 默认展示口径固定为 DB-only 索引 + 明确时效性字段；刷新、全量校验、COS import、reconcile 均为显式 job/status。
- Phase 1 只做工作区文件展示路径和单助手刷新任务化；能力安装索引为 Phase 2；COS import/reconcile/backfill/orphan/degraded 为 Phase 3。
