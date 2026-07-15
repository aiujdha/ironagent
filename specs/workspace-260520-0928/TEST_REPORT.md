# workspace-260520-0928: 管理后台工作区索引化与同步任务化 - 验收报告

- spec-id：workspace-260520-0928
- 报告类型：Phase 1 本地单测证据
- 报告修订日期：2026-05-20
- 测试日期：2026-05-20
- 验收分支：`codex/admin-260514-2331-reaccept-final`
- 验收基线 commit：`1a3d9397`
- 验收目标 commit：未提交工作树
- target HEAD：`1a3d9397` + dirty
- current HEAD：`1a3d9397`
- dirty status：dirty。当前分支存在大量其它未审查改动；本报告只覆盖本轮 Phase 1 相关文件和测试命令，不证明其它 dirty 文件正确，也不证明线上状态。
- 验收人：Codex
- 测试范围：本轮实现验收准备只覆盖 Phase 1：B1、B2、B6、B8、B12、B13、B14、B15、B20；B1-B20 全量矩阵继续保留为后续 Phase 证据占位。
- 测试方式：本地单元测试 + diff whitespace 检查
- 环境边界：本地工作树；未做浏览器 Network、真实 HTTP、SQL EXPLAIN、规模压测、迁移、回滚或线上验证。
- 总结论：Phase 1 本地单测通过；完整验收仍为 partial
- 分级结论：partial-pass

## 填写规则

- 本报告当前全部结论保持 `pending` / `unverified`。
- 实现后只能把有证据支撑的单项从 `unverified` 更新为实际结论；不得因为代码存在就标记通过。
- 每个行为项必须填写分支、commit、命令、样本数据规模、关键日志、HTTP 请求/响应、SQL/EXPLAIN 或 UI 截图等证据位置。
- 性能、迁移、回滚必须单独给证据；不能被单元测试通过替代。
- 正式验收必须先重查 dirty status。本报告当前只覆盖 specs 文档骨架，不能覆盖当前工作区其它未审查改动。

## 总结论

| 分类 | 当前结论 | 说明 |
| ---- | -------- | ---- |
| B1-B20 行为 | partial-pass | Phase 1 相关 B1/B2/B6/B8/B12/B13/B14/B20 有本地单测证据；Phase 2/3 和真实环境证据未覆盖 |
| 性能与规模 | unverified | 未构造 2000 助手、200000 文件记录、20000 能力记录数据 |
| 权限与隔离 | unverified | 未验证 401/403、主体过滤和 `user_id` 旁路 |
| 迁移与 backfill | unverified | 未验证空表、已有数据、重复记录、可重入和失败续跑 |
| 回滚 | unverified | 未验证回滚后无新增 job、无全助手扫描、旧索引可读 |
| UI 状态 | partial-pass | 前端代码断言覆盖默认不传 `sync=true`、索引状态字段和单助手刷新 job 创建；未做浏览器截图或 Network 验证 |
| 阻塞 | pending | `PRODUCT.md` 当前仍为 `draft`，本报告不修改 spec 状态 |

## 本轮 Phase 1 边界

- 本轮为 Phase 1 实现收集了本地单元测试证据，但不覆盖完整性能、迁移、浏览器和线上验收。
- 本轮 Phase 1 覆盖 `PRODUCT.md` 的 B1、B2、B6、B8、B12、B13、B14、B15、B20。
- 本轮不覆盖 Phase 2 能力安装索引、能力写路径矩阵、能力页兼容、市场安装后能力索引更新，也不覆盖 B3、B4、B5 的验收结论。
- 本轮不覆盖 Phase 3 backfill、reconcile、COS import、orphan/degraded 修复、删除候选清理、批量全量校验任务，也不覆盖 B7、B9、B10、B11、B16、B17、B18、B19 的验收结论。
- 本报告可以作为本地 Phase 1 单测证据；不能作为部署或线上验收证明。

## 关联提交

| 类型 | commit | 说明 |
| ---- | ------ | ---- |
| 基线 | 未执行 | 待填写基线 commit |
| 实现 | 未提交工作树 | 待提交后回填实现 commit |
| 集成 | 未执行 | 待填写集成 commit |
| 验收 | 未执行 | 待填写验收报告更新 commit |

## 待执行命令与证据

| 类别 | 命令或证据 | 当前结论 | 实现后必须填写 |
| ---- | ---------- | -------- | -------------- |
| Git 状态 | `git status --short` | unverified | 分支、HEAD、dirty 文件清单、无关改动说明 |
| 行为测试 | `.venv/bin/python -m pytest tests/unit/test_workspace_admin_index.py tests/unit/test_workspace_sync_jobs.py tests/unit/test_admin_workspace_phase1_routes.py tests/unit/test_admin_workspace_phase1_ui.py -q` | partial-pass | 15 passed；仅 FastAPI `on_event` deprecation warnings |
| HTTP 验收 | 待实现后补充 curl/浏览器请求 | unverified | 请求、响应、状态码、鉴权 Header |
| SQL 证据 | 待实现后补充 SQL 计数和 EXPLAIN | unverified | SQL 次数、慢查询、EXPLAIN 输出 |
| 性能压测 | 待实现后补充脚本和样本规模 | unverified | 数据规模、p95、错误率、资源边界 |
| UI 证据 | 待实现后补充截图或录屏 | unverified | 文件页、能力页、刷新任务状态、失败状态 |
| 迁移回滚 | 待实现后补充 migration/backfill/rollback 命令 | unverified | 执行记录、重复执行记录、失败续跑记录 |

## Phase 1 本轮实现后主线程必须收集的证据

| 证据项 | 当前结论 | 主线程必须填写 |
| ------ | -------- | -------------- |
| UI 不传 `sync=true` | partial-pass | `tests/unit/test_admin_workspace_phase1_ui.py` 断言默认 endpoint 为 `/api/v1/admin/workspace-files/summary`，不存在 `?sync=true`。 |
| GET COS list=0 | partial-pass | `tests/unit/test_admin_workspace_phase1_routes.py` monkeypatch 禁止 `WorkspaceFileService.list_cos_workspace_summary_records()`，默认 summary 通过 DB index stub。 |
| workspace scan=0 | partial-pass | 本轮单测覆盖默认 GET 不调用 sync service 和 workspace file service；未覆盖所有扫描入口。 |
| `list_files` legacy hydrate=0 | partial-pass | `tests/unit/test_admin_workspace_phase1_routes.py` monkeypatch 禁止 `WorkspaceFileService.list_files()`，默认 summary 未触发。 |
| POST job 立即返回 | partial-pass | `tests/unit/test_admin_workspace_phase1_routes.py` 断言 POST 返回 `job_id/status/indexStatus/workspaceFiles/assistantSummary`，且不执行 sync。 |
| `page_size>200` 返回 400 | partial-pass | `tests/unit/test_admin_workspace_phase1_routes.py` 断言 `page_size=201` 返回 400 和固定错误。 |

## B1-B20 行为覆盖矩阵

| 行为 | PRODUCT.md 对齐点 | 当前结论 | 实现后必须填写的证据 |
| ---- | ---------------- | -------- | -------------------- |
| B1 | 管理员打开工作区文件页时必须直接查询纯 DB 索引并分页返回；不得默认触发全助手 workspace 同步、实时 COS list、legacy COS hydrate 或本地 workspace 递归扫描。 | unverified | UI 请求不带 `sync=true`；summary 请求/响应；调用链日志证明 sync/COS/hydrate/workspace scan 为 0；DB-only 查询代码路径和测试输出。 |
| B2 | 按助手、bucket、来源类型、同步状态、更新时间筛选时必须在 DB 查询层完成过滤和分页；默认 `page_size<=50`，最大 `page_size<=200`，`page_size>200` 必须返回 400。 | unverified | 各筛选参数 HTTP 响应；分页元数据；超大 `page_size` 返回 400；SQL 条件和 EXPLAIN 命中索引。 |
| B3 | 助手已安装能力列表或详情能力展示必须优先读取能力安装索引和 `workspace_files` 聚合；不得默认扫描 COS `skills/plugins` 前缀；`force=true` 不得在 GET 内创建 job。 | unverified | `workspace-items` 请求/响应；能力安装索引读取证据；COS `skills/plugins` list 次数为 0；`force=true` 返回 `refresh_required` 且 job 创建次数为 0；市场 catalog 列表不混入本验收。 |
| B4 | 能力安装、升级、复用、卸载、启用、禁用成功后必须更新该助手能力安装索引；runtime reload 失败不得回滚索引，必须标记未生效或 reload 失败。 | unverified | 六类操作前后 DB 记录；runtime reload 成功和失败样例；审计/事件记录；失败时索引保留且运行态标记正确。 |
| B5 | 文件上传、审批 apply 写入 workspace、runtime scanner 发现结果文件、市场安装能力包时必须写入或更新 `workspace_files`；审批写入必须复用现有审批 apply 链路。 | unverified | 四条写路径的 DB upsert 证据；审批 apply 调用链；重复写入幂等证据；失败状态记录。 |
| B6 | 管理员点击“刷新当前助手”时必须创建单助手同步任务，立即返回 `job_id`、`status` 和当前索引状态；不得阻塞等待完整扫描完成。 | unverified | POST 创建 job 响应；返回耗时；旧索引仍可展示；job 查询状态流；无阻塞完整扫描日志。 |
| B7 | 管理员点击“刷新全部助手”或“全量校验”时必须创建后台批量任务并限流执行；不得对所有助手无上限并发扫描。 | unverified | 批量 job 记录；并发上限配置和实际日志；任务排队证据；无无限 `asyncio.gather` 或等价 fan-out。 |
| B8 | 同步任务必须记录 `pending/running/succeeded/failed/cancelled` 或等价状态，并记录开始/结束时间、范围、扫描数、更新数、错误；同一 scope 重复点击不得创建无限 running 任务。 | unverified | job 表状态迁移；计数字段；错误字段；重复点击同 scope 的幂等键和返回结果；超时/取消恢复证据。 |
| B9 | 普通 `index_only` 同步扫描不到历史文件时不得删除对象存储 blob 或把记录标记为最终删除；普通同步只 upsert 已发现文件，只有显式 reconcile 才能标记 missing 或进入删除候选。 | unverified | `index_only` 缺失样本前后 DB/COS 对比；无 deleted/missing 最终状态；reconcile 与 index_only 模式分流证据。 |
| B10 | reconcile 发现缺失时必须至少连续 2 次确认缺失并超过 24 小时保留窗口后，才能进入对象清理；清理必须可审计。 | unverified | 两次确认记录；时间窗口证据；delete candidate 到 cleanup 的状态流；审计/系统事件；未到窗口时不清理。 |
| B11 | COS 中人工直接写入的文件或能力目录默认不保证实时显示；必须通过显式 `cos_import` 或 reconcile 任务进入 DB 索引。 | unverified | 默认 GET 不触发 COS list；人工 COS 写入后页面仍显示旧索引；显式 `cos_import` job 后索引更新；reconcile 证据。 |
| B12 | DB 索引与 COS 或本地 workspace 不一致时，页面必须展示 `last_synced_at`、`sync_status`、`sync_error`、`stale` 或“展示旧索引”提示，不得假装实时。 | unverified | UI 截图；API 字段；失败/过期/刷新中三类状态；文案不声称实时同步。 |
| B13 | 同步任务失败时页面必须保留旧索引并展示失败状态；失败不得导致列表为空或误删既有索引。 | unverified | 构造失败任务；失败前后列表一致性；旧索引保留；错误提示；DB 未误删。 |
| B14 | 查询 500-2000 个助手规模数据时接口必须支持分页并避免一次返回所有助手全部文件；GET 查询 COS list 次数必须为 0，全助手 fan-out 同步次数必须为 0。 | unverified | 2000 助手、200000 文件记录样本；分页响应；响应体大小；COS list=0；fan-out=0；p95 和 SQL 次数。 |
| B15 | 权限不足的查询或同步任务创建必须拒绝并返回明确错误；不得泄露其它助手文件或能力元数据；新增 DB 索引查询层不得被任意 `user_id` 参数直接复用。 | unverified | 无鉴权、弱鉴权、越权 assistant_id、任意 `user_id` 参数样例；401/403 响应；无数据泄露；审计/日志。 |
| B16 | 迁移上线时已有 `workspace_files` 数据必须兼容读取；缺失能力安装索引可通过可重入 backfill 任务生成；不得要求上线时同步扫描全部对象存储才能打开页面。 | unverified | 旧数据 migration 前后读取；backfill 首跑和重复执行；页面打开 COS list=0；无全对象存储扫描。 |
| B17 | 后台服务或同步 worker 重启时，未完成任务必须可恢复、可重试或标记失败；不得永久卡在 running。 | unverified | running 任务重启前后状态；恢复/重试/失败标记；超时处理；无永久 running 样本。 |
| B18 | 对象存储写入成功但 DB 写入失败时，必须在结构化日志、补偿队列或等价持久介质记录 orphan object 或 metadata degraded 状态，后续校验任务可修复。 | unverified | 注入 DB 写失败；对象存在证明；orphan/degraded 记录；后续 reconcile/cos_import 修复证据。 |
| B19 | DB 写入成功但对象存储写入失败时，必须保留索引记录并标记 `sync_status=failed` 或等价状态，页面不得显示为已同步。 | unverified | 注入对象写失败；DB 记录保留；`sync_status=failed`；UI 失败提示；后续重试证据。 |
| B20 | Phase 1 必须同时完成 UI 不传 `sync=true`、后端 DB-only 分页查询和刷新 job/status 语义；只取消前端 `sync=true` 但后端仍 fan-out/COS list 不算完成。 | unverified | Phase 1 聚合证据：UI 请求、后端调用链、DB-only 查询、job/status、COS list=0、fan-out=0；三项缺一不得通过。 |

## 性能验收矩阵

| 指标 | 验收口径 | 当前结论 | 实现后必须填写的证据 |
| ---- | -------- | -------- | -------------------- |
| 2000 个助手 | 测试环境必须构造或导入 2000 个可查询助手，覆盖有文件、有能力、无数据、失败状态样本。 | unverified | 数据生成命令、助手总数 SQL、样本分布、清理方式。 |
| 200000 条 `workspace_files` | 至少 200000 条文件索引记录，覆盖 bucket、source_type、sync_status、deleted、更新时间分布。 | unverified | 插入/导入命令、总数 SQL、索引统计、分页查询样例。 |
| 20000 条能力记录 | 至少 20000 条能力安装索引或等价记录，覆盖 skills/plugins、enabled/deleted、reload 成功/失败。 | unverified | 数据生成命令、总数 SQL、能力页查询样例。 |
| GET COS list=0 | 所有默认 GET 查询，包括全局 summary、单助手 summary、workspace-items，COS list 调用次数必须为 0。 | unverified | mock/spy 计数、结构化日志、测试断言、请求清单。 |
| workspace scan=0 | 所有默认 GET 查询不得触发本地 workspace 递归扫描或 legacy hydrate。 | unverified | spy 计数、调用链日志、禁止函数断言。 |
| 无全助手 fan-out | 页面默认 GET 和单助手刷新不得对所有助手 fan-out；全量任务必须排队限流。 | unverified | fan-out 调用计数、任务队列日志、并发上限证据。 |
| SQL 次数 | 默认 summary 查询 SQL 次数必须有上限；目标不超过 5 次，超出必须说明原因并给优化记录。 | unverified | SQL 计数日志、查询列表、超限原因、修复或接受说明。 |
| EXPLAIN | 关键查询必须给 EXPLAIN，证明命中目标索引而非全表扫描。 | unverified | EXPLAIN 输出、使用索引名、行数估算、慢查询样例。 |
| p95 | 2000 助手、200000 文件、20000 能力记录规模下，默认 GET p95 目标不超过 800ms；未达标必须记录偏差原因。 | unverified | 压测命令、样本数、p50/p95/p99、错误率、机器规格。 |
| 响应体规模 | 默认分页不得一次返回全部助手全部文件；默认 `page_size<=50`，最大 `page_size<=200`，`page_size>200` 返回 400。 | unverified | 响应体大小、分页字段、`page_size>200` 返回 HTTP 400 样例。 |

## 性能证据填充表

| 接口或路径 | 样本规模 | SQL 次数 | COS list | workspace scan | fan-out | EXPLAIN | p95 | 结论 |
| ---------- | -------- | -------- | -------- | -------------- | ------- | ------- | --- | ---- |
| `GET /api/v1/admin/workspace-files/summary` | 未执行 | 未执行 | 未执行 | 未执行 | 未执行 | 未执行 | 未执行 | unverified |
| `GET /api/v1/admin/assistants/{assistant_id}/workspace-files/summary` | 未执行 | 未执行 | 未执行 | 未执行 | 未执行 | 未执行 | 未执行 | unverified |
| `GET /api/v1/admin/assistants/{assistant_id}/workspace-items` | 未执行 | 未执行 | 未执行 | 未执行 | 未执行 | 未执行 | 未执行 | unverified |
| `POST /api/v1/admin/workspace-sync-jobs` 单助手 `index_only` | 未执行 | 未执行 | 未执行 | 未执行 | 未执行 | 未执行 | 未执行 | unverified |
| `POST /api/v1/admin/workspace-sync-jobs` 批量任务 | 未执行 | 未执行 | 未执行 | 未执行 | 未执行 | 未执行 | 未执行 | unverified |

## 权限与安全矩阵

| 场景 | 预期 | 当前结论 | 实现后必须填写的证据 |
| ---- | ---- | -------- | -------------------- |
| 未登录访问全局文件 summary | 返回 401/403，不返回文件元数据。 | unverified | 请求、响应、日志。 |
| 普通用户访问后台全局索引 | 返回 401/403，不返回其它助手文件或能力元数据。 | unverified | 用户身份、请求、响应、无泄露证明。 |
| 管理员访问不可见助手 | 返回 403 或明确错误，不返回数据。 | unverified | 可见性配置、请求、响应。 |
| 任意传入 `user_id` 试图复用后台索引 | 不得绕过认证主体和助手可见性过滤。 | unverified | 请求矩阵、响应、服务端主体解析日志。 |
| 创建同步任务权限不足 | 拒绝并记录明确错误。 | unverified | 单助手、全量、reconcile、cos_import 请求样例。 |
| 用户侧文件 API | 维持现有公开业务 API 边界，不扩大后台索引权限。 | unverified | 用户侧接口回归证据。 |

## 迁移与回滚矩阵

| 场景 | 验收口径 | 当前结论 | 实现后必须填写的证据 |
| ---- | -------- | -------- | -------------------- |
| 空表 migration | 空 `workspace_files`、空能力索引、空 job 表时 migration 成功，页面返回空页和索引状态，不触发 COS list。 | unverified | migration 命令、表结构、页面/API 响应、COS list=0。 |
| 已有数据 migration | 既有 `workspace_files` 数据升级后兼容读取，分页和筛选不破坏旧记录。 | unverified | migration 前后数量、样本记录、API 响应、回归命令。 |
| 重复记录去重 | 历史重复 `assistant_id + bucket + filename + deleted=false` 记录必须先去重，再加唯一约束或 upsert guard。 | unverified | 重复样本、去重规则、去重前后数量、约束验证。 |
| backfill 可重入 | 能力安装索引 backfill 多次执行结果一致，不重复创建能力或文件索引。 | unverified | 连续两次或多次执行日志、记录数量对比、幂等键。 |
| backfill 失败续跑 | backfill 中途失败后再次执行可从已完成部分继续或安全重算，不要求清库。 | unverified | 故障注入点、失败日志、续跑日志、最终数量。 |
| migration 失败回滚 | 迁移失败后旧表可读，旧页面或降级路径不触发新增 job。 | unverified | 回滚命令、旧接口读取、job 表数量不增加。 |
| 回滚后无新增 job | 回滚到旧代码或关闭新功能后，后台 GET 不创建 `workspace_sync_jobs`。 | unverified | 回滚前后 job 数、GET 请求日志、无新增 job 断言。 |
| 回滚后无全助手扫描 | 回滚或降级路径不得因为页面打开触发全助手 workspace scan。 | unverified | scan 计数、日志、COS list 计数、fan-out 计数。 |
| 回滚后旧索引保留 | 回滚不应清空旧 `workspace_files`，失败状态也不得误删既有索引。 | unverified | 回滚前后记录数量、页面/API 响应。 |

## 回归与非目标确认

| 项目 | 当前结论 | 实现后必须填写 |
| ---- | -------- | -------------- |
| 不引入 Redis、外部 MQ、K8s informer 或 runtime operator | unverified | diff 检查和配置检查 |
| 不改变公开业务 API 契约 | unverified | 公开 API 回归结果 |
| 不改变 runtime 输出协议 | unverified | runtime 相关测试或说明 |
| 不把 COS 人工写入作为页面实时一致性承诺 | unverified | B11 证据 |
| 不把旧 `TEST_REPORT.md` 当成本 spec 验收证明 | unverified | 本报告独立证据清单 |

## 非干扰矩阵

| 场景 | 验收口径 | 当前结论 | 实现后必须填写的证据 |
| ---- | -------- | -------- | -------------------- |
| 默认 GET 查询 | 不创建同步任务、不写 DB、不触发 COS list、不触发 workspace scan、不触发全助手 fan-out。 | unverified | GET 前后 job 数、DB 写计数、COS list 计数、workspace scan 计数、fan-out 计数。 |
| 单助手刷新 | 只创建单助手 `index_only` job，不阻塞页面，不影响其它助手索引。 | unverified | job scope、受影响 assistant_id、其它助手记录前后对比、返回耗时。 |
| 全量刷新或全量校验 | 必须走显式后台任务和限流，不得由页面默认查询隐式触发。 | unverified | 操作入口、job 记录、并发上限、默认 GET 无新增 job 证据。 |
| 权限失败请求 | 不创建 job、不写索引、不暴露其它助手元数据。 | unverified | 401/403 请求前后 job 数、索引记录数、响应体检查。 |
| 失败同步任务 | 保留旧索引，不清空列表，不误删对象或能力记录。 | unverified | 失败前后记录数量、UI/API 响应、对象存储对比。 |
| 回滚或关闭新功能 | 不创建新增 job，不触发全助手扫描，不改变旧 `workspace_files` 可读性。 | unverified | 回滚前后 job 数、scan 计数、旧接口读取证据。 |

## 未完成或待验证

- B1-B20 全部待实现和验证。
- 2000 助手、200000 文件记录、20000 能力记录的性能样本未构造。
- GET COS list=0、workspace scan=0、无 fan-out、SQL 次数、EXPLAIN、p95 均未验证。
- 空表、已有数据、重复记录去重、backfill 可重入、失败续跑、回滚后无新增 job、回滚后无全助手扫描均未验证。
- 当前 dirty status 只做了报告骨架层面的记录；正式验收必须重新检查。

## 后续动作

- 主线程确认 `PRODUCT.md`、`TECH.md` 和本报告骨架后，再决定是否把 spec 状态推进到 `ready`。
- 实现阶段每完成一个 Phase，只更新有证据的行为项；未实现 Phase 不得标记为已验收。
- 正式验收时先补齐 Git 状态、commit、测试命令和证据路径，再修改任何结论。
