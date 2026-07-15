# workspace-260520-0928: 管理后台工作区索引化与同步任务化 - 技术方案

- 模块：workspace
- 创建日期：2026-05-20 09:28
- 编写人：kolbaaa14-gif
- 最近修改人：kolbaaa14-gif
- 最近修改日期：2026-05-20 09:51
- 等级：S3
- 状态：draft

## 背景

- 产品 spec：`./PRODUCT.md`
- 当前事实源文档：`specs/admin-260514-2331/PRODUCT.md`、`specs/admin-260514-2331/TECH.md`、`specs/runtime-260518-1025/PRODUCT.md`、`specs/runtime-260518-1025/TECH.md`
- 当前验收边界：旧 `TEST_REPORT.md` 不能作为本 spec 的验收证明；本 spec 实现后必须新增或更新 `./TEST_REPORT.md`。
- 当前代码入口：`src/admin_web/app.py`、`src/admin_web/static/index.html`、`src/workspace_files/sync_service.py`、`src/workspace_files/service.py`、`src/workspace_files/repository.py`、`src/assistants/service.py`、`src/market/service.py`
- 当前限制：工作区文件页切换会触发 `sync=true`；后端 summary 会对所有助手同步并优先查 COS 摘要；旧 `WorkspaceFileService.list_files()` 可能触发 legacy COS hydrate；助手已安装能力汇总会扫描 COS `skills/plugins` 前缀并读取本地 workspace 目录推断启用状态。

## 影响范围

- Admin：文件页、助手详情、能力列表、刷新按钮、任务状态展示。
- Security Service：workspace file 查询和同步入口需要与后台索引模型兼容；用户侧路由不得复用后台全局索引能力。
- Assistant Runtime：不改变运行方式；runtime 生成结果文件首期通过 scanner/index job 补偿写索引，不要求改 runtime 输出协议。
- Workspace：扫描从页面路径移出，改为任务化、增量化和 mode 分流。
- Market Skill/Plugin：安装、复用、卸载、启用、禁用后必须更新能力安装索引。
- UI：默认展示 DB 索引，显示 stale/sync 状态，刷新变成任务状态流。
- Deploy/Ops：需要迁移/回填索引，并监控同步任务失败、堆积、COS list 次数和慢查询。

## 修改方案

- 修改点：
  - 新增纯 DB 索引查询入口；禁止后台页面 summary 复用会触发 COS hydrate 或 workspace scan 的旧函数。
  - 文件页默认请求从 `sync=true` 改为纯查询。
  - `/api/v1/admin/workspace-files/summary` 改为 DB-only 分页查询，支持筛选。
  - 新增同步任务创建和查询接口，用于刷新当前助手、批量刷新和 COS 校验导入。
  - 助手已安装能力列表默认使用能力安装索引 + `workspace_files` 聚合，不实时扫描 COS。
  - 市场安装、复用、启用、禁用、卸载链路同步写能力安装索引。
  - 普通 `index_only` 同步不标记 missing；`reconcile` 任务才处理缺失和清理候选。
- Phase 1 必须先落地的代码边界：
  - 新增独立的 DB-only 查询服务，例如 `WorkspaceIndexQueryService` 或等价 repository 查询方法；Admin summary 路由只能调用该入口。
  - 新入口只允许访问 `workspace_files`、同步任务状态表、可见助手过滤结果和必要聚合查询；不得调用对象存储 gateway、workspace 目录 scanner 或 `WorkspaceFileService` 的 legacy hydrate 路径。
  - `GET` 查询链路必须能被 monkeypatch 证明没有调用 `WorkspaceSyncService.async_sync_assistant_workspace()`、`WorkspaceSyncService.async_sync_assistant_workspace_scopes()`、`WorkspaceFileService.scan_workspace_results()`、`WorkspaceFileService.list_cos_workspace_summary_records()`、`WorkspaceFileService.list_cos_capability_package_records()`、`WorkspaceFileService._hydrate_legacy_object_storage_records()`。
  - 全局 summary 不得按助手 `asyncio.gather` fan-out 拉取每个助手文件；只能先得到可见助手范围，再用一次或少量带分页/聚合的 SQL 查询返回。
- 不修改的范围：
  - 不改变公开业务 API 契约。
  - 不改变 runtime 容器加载 `skills/plugins` 的目录语义。
  - 不引入外部 MQ、Redis、K8s informer 或 runtime operator。
  - 不重写审批 apply 链路，只补索引状态和验收。
- 兼容性处理：
  - `workspace_files` 继续兼容已有记录。
  - 能力安装索引缺失时，Phase 2 只展示 degraded/stale 或空索引状态，不得通过 GET 或 COS 扫描补齐。
  - 历史索引回填属于 Phase 3 backfill。
  - 页面必须能在索引缺失或同步失败时显示 degraded/stale，而不是空白或阻塞。

## 实现记录

- 实现分支：
- 基线 commit：
- 实现 commit：
- 集成 commit：
- 关联行为：
- 未包含范围：

## 验收记录

- 验收报告：`./TEST_REPORT.md`
- 验收分支：
- 验收目标 commit：
- 当前结论：未验收

## 方案变更

### 阶段与行为映射

| Phase | 范围 | 行为 | 不得声称完成的范围 |
| ----- | ---- | ---- | ------------------ |
| Phase 1 | DB-only 文件查询、刷新 job/status 语义、基础权限和 UI 时效性 | B1, B2, B6, B8, B12, B13, B14, B15, B20 | 能力安装索引、COS import、reconcile、orphan cleanup |
| Phase 2 | 能力安装索引、能力写路径矩阵、能力页兼容 | B3, B4, B5, B15 | 全量 COS import、删除候选清理 |
| Phase 3 | 批量任务、COS import、reconcile、backfill、orphan/degraded 修复 | B7, B9, B10, B11, B16, B17, B18, B19 | 生产级外部 MQ/Redis/operator |

### 接口

#### `GET /api/v1/admin/workspace-files/summary`

- 用途：后台工作区文件全局 summary；首屏和筛选都走这个接口。
- 只读纯 DB 索引；请求成功时 COS list 次数必须为 0，workspace scan 次数必须为 0，同步 job 创建次数必须为 0。
- 支持查询参数：
  - `assistant_id`：可选，限定单助手；未传表示管理员可见助手集合。
  - `bucket`：可选，`temp`、`persistent`、`skills`、`plugins`。
  - `source_type`：可选，例如 `assistant_result`、`assistant_output`、`capability_package`、`workspace_scan`。
  - `sync_status`：可选，例如 `pending`、`synced`、`synced_to_workspace`、`failed`、`missing`。
  - `updated_after`：可选，ISO8601 时间。
  - `query`：可选，服务端全局搜索 `filename`、`relative_path`、能力 slug/name；不能退化为当前页搜索。
  - `page`：默认 1，最小 1。
  - `page_size`：默认 50，最大 200；超过 200 必须返回 400，不做静默钳制。
  - `view`：默认 `flat`；允许 `flat` 或 `tree`，树形语义见“树形与分页契约”。
  - `parent`：树形视图可选，限定当前目录前缀。
- 兼容参数：
  - `sync=true` 可以暂时保留解析，但服务端不得执行阻塞式同步、COS list、workspace scan 或全助手 fan-out。
  - 如果收到 `sync=true`，响应 `compat.sync_requested=true`、`compat.sync_ignored=true`，并建议前端改用 `POST /api/v1/admin/workspace-sync-jobs`。
- 响应必须包含分页元数据、全局索引状态、当前页数据和可选助手聚合。
- 禁止调用 `WorkspaceSyncService.async_sync_assistant_workspace`、`WorkspaceFileService.list_cos_workspace_summary_records`、`WorkspaceFileService.list_cos_capability_package_records`、`WorkspaceFileService._hydrate_legacy_object_storage_records`。
- 禁止直接复用当前会触发 legacy hydrate 的 `WorkspaceFileService.list_files()`，除非先实现并显式使用 `hydrate_legacy=False` 或直接走 repository 的 DB-only 查询。

示例响应字段：

```text
workspaceFiles[]:
  file_id, assistant_id, bucket, filename, relative_path, source_type,
  sync_status, sync_error, size_bytes, latestUpdatedAt, lastSyncedAt,
  isCapabilityPackage, packageSlug, packageType
pagination:
  page, page_size, total, has_next
indexStatus:
  last_indexed_at, sync_status, sync_error, stale, source="db"
assistantSummaries[]:
  assistant_id, last_synced_at, sync_status, files_count, failed_count,
  capability_count
compat:
  sync_requested, sync_ignored
```

#### `GET /api/v1/admin/assistants/{assistant_id}/workspace-files/summary`

- 用途：助手详情内的文件 summary；语义等价于全局 summary 加 `assistant_id` 过滤。
- 只读纯 DB 索引，限定单助手，并先校验助手存在和管理员可见性。
- 查询参数沿用全局 summary 的 `bucket`、`source_type`、`sync_status`、`updated_after`、`query`、`page`、`page_size`、`view`、`parent`。
- 不再通过 `sync=true` 隐式执行同步；如保留 `sync` 参数，必须标记 deprecated，默认 UI 不使用，服务端收到后不得执行阻塞式完整扫描。
- 不存在的助手返回 404；存在但不可见返回 403；权限缺失返回 401/403；三者不得泄露其它助手元数据。
- 响应在全局 summary 字段基础上增加：

```text
assistantSummary:
  assistant_id, last_synced_at, last_indexed_at, sync_status, sync_error,
  stale, files_count, capability_count, pending_job_id
```

#### `POST /api/v1/admin/workspace-sync-jobs`

- 用途：创建显式同步任务；页面刷新按钮、全量校验和 COS 导入都必须走这里。
- 创建任务后立即返回，不在请求线程内执行完整扫描。
- body 字段：

```text
assistant_id: string | null
scope_kind: assistant | bucket | capability | all_visible
scope_slug: string | null
mode: index_only | reconcile | cos_import
priority: low | normal | high
reason: string
idempotency_key: string | null
```

- `assistant_id` 为空只允许 `scope_kind=all_visible`，且需要全量刷新权限。
- `scope_kind=capability` 时 `scope_slug` 使用 `skills/<slug>` 或 `plugins/<slug>`。
- 当前助手刷新使用 `mode=index_only`。
- 全量刷新必须拆分或排队为受限批量任务，不得直接 `asyncio.gather` 扫所有助手。
- 同一 `assistant_id + scope_kind + scope_slug + mode` 已有 `pending/running` 时，返回已有 job，不能无限创建重复任务。
- 建议返回 202；重复幂等命中可返回 200 或 202，但必须带同一个 `job_id`。
- 响应字段：

```text
job_id, status, idempotency_key, mode, assistant_id, scope_kind, scope_slug,
created_at, pending_position, indexStatus
```

#### `GET /api/v1/admin/workspace-sync-jobs/{job_id}`

- 用途：前端轮询同步任务状态。
- 只返回调用方可见范围内的 job；不可见返回 403 或 404，但不得泄露 job 存在性给无权限主体。
- 返回任务状态、计数、错误、开始/结束时间、操作者和范围。
- 响应字段：

```text
job_id, status, mode, assistant_id, scope_kind, scope_slug, priority,
requested_by, requested_from, scanned_count, created_count, updated_count,
missing_count, deleted_count, orphan_count, error_code, error,
started_at, finished_at, created_at, updated_at
```

#### `GET /api/v1/admin/assistants/{assistant_id}/workspace-items`

- 用途：助手详情和能力页读取已安装 skills/plugins。
- Phase 2 后默认查 `assistant_workspace_items` 能力安装索引，并用 `workspace_files` 聚合补 `files_count`、`last_indexed_at`；不得默认扫描 COS `skills/plugins` 前缀或本地 workspace 目录。
- 查询参数：
  - `kind`：可选，`skills` 或 `plugins`。
  - `slug`：可选，限定单能力。
  - `force`：兼容旧前端；不得直接扫 COS，也不得在 GET 内创建刷新 job。收到后返回当前索引，并通过 `compat.refresh_required=true` 提示前端显式创建刷新任务。
- 响应字段：

```text
items[]:
  assistant_id, kind, slug, name, version, enabled, install_path,
  files_count, content_hash, source, sync_status, runtime_reload_status,
  runtime_reload_error, runtime_effective, last_indexed_at, last_synced_at,
  last_error
indexStatus:
  stale, sync_status, last_synced_at, pending_job_id
compat:
  force_requested, force_scan_disabled, refresh_required
```

- 能力安装、升级、启用、禁用、复用、卸载后，前端保留旧 installed 状态并轮询 job/status 或能力索引刷新结果。
- Phase 2 迁移期间如果索引缺失，接口可以返回空 items + `indexStatus.stale=true` + `refresh_required=true`，但不能回退成 GET 路径扫描。

### 接口鉴权矩阵

| 接口/能力 | 调用方 | 鉴权 Header/会话 | 权限 | 范围过滤 | 失败码 | 审计/事件 |
| --------- | ------ | ---------------- | ---- | -------- | ------ | --------- |
| `GET /api/v1/admin/workspace-files/summary` | 后台管理员 | 后台会话或 `X-IronAgent-Internal-Token` | admin read | 管理员可见助手集合；首期超级管理员为全部 | 401/403 | 查询可不进正式审计，失败进系统日志 |
| `GET /api/v1/admin/assistants/{assistant_id}/workspace-files/summary` | 后台管理员 | 后台会话或 `X-IronAgent-Internal-Token` | admin read | 指定助手必须在可见集合 | 401/403/404 | 查询可不进正式审计，失败进系统日志 |
| `GET /api/v1/admin/assistants/{assistant_id}/workspace-items` | 后台管理员 | 后台会话或 `X-IronAgent-Internal-Token` | admin read | 指定助手必须在可见集合 | 401/403/404 | 查询可不进正式审计，失败进系统日志 |
| `POST /api/v1/admin/workspace-sync-jobs` 单助手 `index_only` | 后台管理员 | 后台会话或 `X-IronAgent-Internal-Token` | admin write 或 workspace refresh | 指定助手必须在可见集合 | 401/403/404/409 | 记录 job 创建、完成、失败 |
| `POST /api/v1/admin/workspace-sync-jobs` 全量刷新 | 超级管理员/内部服务 | 后台会话或 `X-IronAgent-Internal-Token` | admin ops | 管理员可见助手集合；首期超级管理员为全部 | 401/403/409 | 记录 job 创建、完成、失败和范围 |
| `POST /api/v1/admin/workspace-sync-jobs` reconcile | 超级管理员/内部服务 | 后台会话或 `X-IronAgent-Internal-Token` | admin ops + destructive candidate | 指定助手或受控批量范围 | 401/403/409 | 正式审计或系统事件，含 missing/delete candidate |
| `POST /api/v1/admin/workspace-sync-jobs` COS import | 超级管理员/内部服务 | 后台会话或 `X-IronAgent-Internal-Token` | admin ops | 指定助手或 scope | 401/403/409 | 正式审计或系统事件，含 COS prefix |
| `GET /api/v1/admin/workspace-sync-jobs/{job_id}` | 后台管理员/内部服务 | 后台会话或 `X-IronAgent-Internal-Token` | admin read | job 范围必须落在可见助手集合 | 401/403/404 | 查询失败进系统日志 |
| 用户侧文件查询 | 终端用户/API 调用方 | 维持现有公开业务 API 认证边界；不得复用后台全局索引接口 | user read | 必须通过用户-助手绑定过滤 | 401/403/404 | 按公开 API 规则 |

鉴权实现要求：

- 不允许通过 `user_id`、`assistant_id` 或 `scope_slug` 参数提升权限。
- 后台会话与内部 token 可以共用可见助手过滤函数，但返回错误必须区分未认证和无权限。
- 全量刷新、reconcile、COS import 属于运维/维护动作，不得只靠普通 read 权限放行。
- `GET` 查询路径即使鉴权失败也不得提前启动同步任务或扫描。

### 状态与持久化

现有 `workspace_files` 继续作为文件索引主表。DB-only 查询至少需要：

- `assistant_id + deleted + updated_at + file_id`
- `assistant_id + bucket + deleted + updated_at + file_id`
- `bucket + deleted + updated_at + file_id`
- `source_type + deleted + updated_at + file_id`
- `sync_status + deleted + updated_at + file_id`
- `updated_at + file_id`
- `assistant_id + bucket + filename + deleted`

实现拆分：

- repository 层新增分页查询方法，例如 `alist_workspace_summary_page(filters, visible_assistant_ids, page, page_size)`。
- 查询方法返回 `{items, total, assistant_summaries, index_status}`，不要让路由层自行加载全部记录后内存分页。
- `query` 搜索首期可使用 `filename LIKE` / `relative_path LIKE` / `source_type` 精确过滤；如果使用全文索引，必须保留普通 SQL fallback。
- 可见助手过滤必须在 SQL 条件中完成；不能先查全部再在 Python 里过滤。
- 全局 summary 的 SQL 预算建议：
  - 1 次权限/可见助手范围查询。
  - 1 次当前页 `workspace_files` 查询。
  - 1 次 `COUNT(*)`。
  - 1 次助手聚合 summary。
  - 1 次 pending/running job 状态查询。
  - 合计不超过 5 次 SQL；如果实现用 join/CTE 合并更少也可以。

唯一约束和去重：

- 目标活跃文件唯一键：`assistant_id + bucket + filename`。
- 迁移前必须先按 `assistant_id + bucket + filename + deleted=false` 去重，保留 `updated_at` 最新、再按 `file_id` 稳定排序的记录。
- 被去重记录不得物理删除；标记 `deleted=true`，写入 `last_error` 或事件说明 `dedupe_migration`。
- 如果数据库支持 partial unique index，使用 `UNIQUE(assistant_id, bucket, filename) WHERE deleted=false`。
- 如果数据库不支持 partial unique index，可以用生成列、`assistant_id + bucket + filename + deleted` 唯一键，或 repository upsert guard 保证活跃记录唯一；选择哪种必须在实现 PR 里说明。
- 直接加唯一键前必须有迁移测试覆盖历史重复记录、重复 migration、并发 upsert。

迁移顺序：

1. 先加非唯一查询索引，确保旧读写仍可运行。
2. 执行重复记录检查：

```sql
SELECT assistant_id, bucket, filename, COUNT(*) AS n
FROM workspace_files
WHERE deleted = false
GROUP BY assistant_id, bucket, filename
HAVING COUNT(*) > 1;
```

3. 对重复记录执行可重入去重 migration。
4. 增加活跃唯一约束或 repository upsert guard。
5. 增加 `assistant_workspace_items` 和 `workspace_sync_jobs` 表。
6. Phase 3 运行 backfill，但页面打开不得依赖 backfill 完成。

新增能力安装索引表或等价持久化结构，Phase 2 必做：

```text
assistant_workspace_items
- assistant_id
- kind: skills/plugins
- slug
- name
- version
- enabled
- install_path
- files_count
- content_hash
- source: market/manual/sync/backfill
- sync_status
- runtime_reload_status
- runtime_reload_error
- runtime_effective
- last_indexed_at
- last_synced_at
- last_error
- created_at
- updated_at
- deleted
```

索引：

- 唯一或 upsert guard：`assistant_id + kind + slug + deleted`
- 查询索引：`assistant_id + deleted + updated_at`、`kind + deleted + updated_at`、`sync_status + deleted + updated_at`
- runtime 状态查询索引：`assistant_id + runtime_reload_status + updated_at`
- 迁移/backfill 幂等键：`assistant_id + kind + slug`

新增同步任务表或等价持久化结构：

```text
workspace_sync_jobs
- job_id
- idempotency_key
- assistant_id
- scope_kind
- scope_slug
- mode: index_only/reconcile/cos_import
- status: pending/running/succeeded/failed/cancelled
- priority
- requested_by
- requested_from
- scanned_count
- created_count
- updated_count
- missing_count
- deleted_count
- orphan_count
- error_code
- error
- started_at
- finished_at
- created_at
- updated_at
```

任务索引：

- `status + priority + created_at`
- `assistant_id + status + created_at`
- `idempotency_key + status`
- `mode + status + created_at`
- `scope_kind + scope_slug + status + created_at`

幂等：

- 文件唯一键：`assistant_id + bucket + filename`。
- 能力唯一键：`assistant_id + kind + slug`。
- 同步任务幂等键：`assistant_id + scope_kind + scope_slug + mode + pending/running`。
- `idempotency_key` 如果由客户端传入，必须先按调用方和范围校验，不能让不同管理员复用同一个 key 访问不可见 job。

任务状态流转：

| 当前状态 | 允许进入 | 触发 |
| -------- | -------- | ---- |
| pending | running/cancelled/failed | worker claim、用户取消、过期恢复 |
| running | succeeded/failed/cancelled | worker 完成、异常、管理员取消 |
| succeeded | 无 | 终态 |
| failed | pending 或无 | 显式重试创建新 job，或按重试策略重排 |
| cancelled | 无 | 终态 |

### 写路径矩阵

| 写路径 | 触发入口 | 必须写入 | 事务/补偿边界 | 失败状态 |
| ------ | -------- | -------- | -------------- | -------- |
| market install/upgrade | `MarketService` 安装成功后 | `assistant_workspace_items`，相关 `workspace_files` capability package 记录，runtime reload 状态 | 包解压/复制成功后写索引；DB 写失败必须记录 degraded/orphan，后续 backfill 可修复 | 安装成功但 reload 失败时保留索引并标记 `runtime_reload_status=failed` |
| workspace-items toggle | Admin toggle API | 更新 `assistant_workspace_items.enabled`、`runtime_reload_status`、审计 | 目录移动和索引更新要有明确先后；任一步失败不得让 UI 显示为 fully effective | 目录移动成功但 reload 失败时标记运行态未生效 |
| workspace-items reuse | Admin reuse API | 目标助手 `assistant_workspace_items`，目标 scope 的 `workspace_files`，runtime reload 状态，审计或系统事件 | 复制成功但 DB 失败记 degraded；DB 成功但复制失败记 failed，不假装 installed | 复制成功但 sync/reload 失败时保留索引并标记 failed |
| workspace-items remove | Admin delete API | `assistant_workspace_items.deleted=true`，相关 `workspace_files` deleted 或 uninstall 状态，审计 | 先标记删除意图，再执行目录/对象清理；对象清理失败进入 cleanup candidate | 文件删除部分失败时记录 `last_error` 和 cleanup 候选 |
| approval apply | 现有审批 apply 链路 | 复用现有 `apply_approval_resolution()` 写入 `workspace_files` 和事件；只补索引状态验收 | 不另起不兼容写入路径；失败不清空旧文件索引 | apply 失败返回 warning，旧记录不被清空 |
| runtime/index scan | 受控 scanner 或 job worker | `workspace_files` upsert，必要时更新 assistant summary | `index_only` 只 upsert；`reconcile` 才处理 missing | 对象存储失败标记 `sync_status=failed` |
| cos_import | 显式 job | `workspace_files` 或 `assistant_workspace_items` backfill/upsert | COS list 只在 job worker 内发生；GET 路径禁止调用 | COS list 失败标记 job failed，不清空旧索引 |

写路径统一要求：

- 每个写路径都必须设置 `source_type`、`source_ref`、`sync_status`、`last_synced_at` 或明确说明为什么为空。
- 对象写成功但 DB 写失败：必须记录 `orphan/degraded` 结构化日志或持久补偿记录，后续 reconcile/cos_import 可恢复。
- DB 写成功但对象/目录写失败：必须保留索引并标记 failed，不得让页面显示为 synced/effective。
- 能力类写路径必须同步维护 `assistant_workspace_items` 与 `workspace_files` 的关联，不允许只改目录后等待页面扫描兜底。

### Runtime 语义

- 能力启用/禁用仍然通过目录移动或当前 runtime 支持的机制生效。
- 索引写入成功不等于 runtime 已生效；必须保留 `runtime_reload_status`。
- Runtime reload 失败时，索引显示为 saved，但运行态显示 failed/pending_reload/restart_required 等状态。
- 不要求运行中任务被中断；是否需要重启由现有 runtime reload 语义决定。

### Workspace 同步模式

- mode 必须显式传入，禁止继续依赖 `mark_missing` 默认值表达业务语义。

| mode | 允许扫描来源 | 允许写入 | 禁止行为 | 适用入口 |
| ---- | ------------ | -------- | -------- | -------- |
| `index_only` | 指定助手或 scope 的 workspace 目录；后续可扩展指定对象前缀但不得默认 COS list | upsert 发现的 `workspace_files` / 必要 summary | 不处理 missing，不删除 blob，不标记最终 deleted，不全助手 fan-out | 刷新当前助手、市场安装后的单 scope 同步 |
| `reconcile` | 指定助手、scope 或受控批量范围的 workspace/COS | upsert、missing candidate、delete candidate、reconcile 事件 | 不直接物理删除；未满足连续确认和 24 小时窗口不得清理 | 全量校验、维护任务 |
| `cos_import` | 指定助手或指定 scope 的 COS 前缀 | 从 COS 元数据 backfill/upsert DB 索引 | 不作为页面 GET 实时查询，不清空旧索引 | 人工写 COS 后的显式导入 |

- 当前 `WorkspaceSyncService.async_sync_assistant_workspace()` 默认 `mark_missing=True`，应视为 reconcile-like，不能直接接到 Phase 1 的“刷新当前助手”按钮。
- 当前 `WorkspaceFileService.scan_workspace_results()` 有缺失即删除记录行为，也必须分流为 `index_only` / `reconcile`，不能作为普通页面刷新路径。
- 市场安装后只同步新安装的能力 scope，避免全量扫描助手 workspace。
- Phase 1 可以用现有 `async_sync_assistant_workspace_scopes(..., mark_missing=False)` 的思想实现单 scope `index_only`，但必须包装成 job 并补任务状态；不能让 API 请求线程等待完整扫描。
- Phase 3 才允许把 COS import/reconcile 接到运维入口；实现前 UI 不得展示“已支持全量 COS 校验”。

### UI 状态机

| UI 状态 | 进入条件 | 页面行为 | 后端请求 | 退出条件 |
| ------- | -------- | -------- | -------- | -------- |
| `idle` | 页面未加载或无筛选变化 | 不展示 loading 覆盖层 | 无 | 用户进入文件页 |
| `loading_page` | 首次打开或筛选/分页变化 | 展示表格骨架或局部 loading；不清空旧页直到新页返回 | `GET .../summary`，不带 `sync=true` | 返回 `loaded` 或 `load_failed` |
| `loaded_fresh` | `indexStatus.stale=false` | 展示当前页、分页、助手聚合 | 无 | 用户刷新/筛选 |
| `loaded_stale` | `indexStatus.stale=true` 或有 `sync_error` | 展示旧索引和 stale/error 标识 | 无 | 用户刷新或 job 成功 |
| `refresh_pending` | 点击刷新当前助手后返回 pending job | 按钮禁用，列表保留旧索引 | `POST /workspace-sync-jobs`，然后轮询 `GET /workspace-sync-jobs/{job_id}` | job running/succeeded/failed |
| `refresh_running` | job running | 展示进度计数；允许浏览和翻页 | 继续轮询 job，必要时重拉 summary | job succeeded/failed/cancelled |
| `refresh_succeeded` | job succeeded | 重新拉取当前筛选页，更新 `last_synced_at` | `GET .../summary` | 回到 loaded |
| `refresh_failed` | job failed/cancelled | 保留旧索引，展示错误和重试按钮 | 无自动重试，用户显式重试 | 用户重试或换页 |
| `bulk_job_running` | 全量刷新/校验任务运行 | 展示全量任务 banner，不阻塞文件页浏览 | 轮询 job 或 job 列表 | job 终态 |

UI 约束：

- 文件页首次打开：立即展示 DB 当前页和索引状态，不自动刷新。
- 刷新当前助手：按钮进入 `pending/running`，接口返回 `job_id`，前端轮询 job；列表保留旧索引。
- 刷新成功：重新拉取当前筛选页，显示最新 `last_synced_at`。
- 刷新失败：保留旧索引，展示 `sync_error`，允许重试。
- 全量刷新：必须区别于当前助手刷新，显示排队/运行状态和影响范围，不阻塞文件页浏览。
- 能力页 `force=true` 兼容：不得清空旧 installed；GET 只返回索引状态和 `refresh_required=true`，UI 继续展示旧能力并引导或触发显式刷新任务。
- 任何状态都不得因 GET 失败触发隐式同步。

### 树形与分页契约

- 文件页列表分页不再等价于“全量文件树”；默认 `view=flat` 返回当前页文件记录。
- `view=tree` 时，分页对象是当前 `parent` 下的兄弟节点，不是整棵树所有文件。
- `page_size` 默认 50，最大 200；树节点和文件节点都计入 page size。
- 根节点/助手目录摘要必须来自 DB summary 聚合，不要求返回所有文件明细。
- 搜索必须是服务端搜索或明确标记为当前页搜索；不能在只拿当前页时假装全局搜索。
- `assistantIds` 如果继续返回，只表示当前管理员可见助手或当前筛选范围，不表示已加载所有文件。
- 文件夹计数必须来自 DB 聚合或标记为估算/未加载。
- `parent` 必须归一化为相对路径前缀，禁止 `..`、绝对路径和跨 bucket 前缀。
- 树节点建议字段：

```text
nodes[]:
  node_id, assistant_id, bucket, path, name,
  type: assistant | bucket | directory | file | capability,
  parent_path, children_loaded, children_count, files_count,
  latest_updated_at, sync_status, stale, is_estimated
pagination:
  page, page_size, total, has_next
```

- 稳定排序：
  - 根层按 `assistant_id` 或助手名称稳定排序。
  - bucket 层按 `temp`、`persistent`、`skills`、`plugins` 固定顺序。
  - 同级目录优先于文件；目录按名称升序；文件默认按 `updated_at DESC, file_id DESC`。
- 如果目录计数依赖聚合 SQL 成本过高，允许首期返回 `children_count=null`、`is_estimated=true`，但 UI 必须展示为未加载/估算，不能显示精确数量。

### Worker 配额与任务规则

- 默认全局 worker 并发上限建议为 4，可配置。
- 单助手同一 mode/scope 同时只能有 1 个 pending/running job。
- COS import 默认并发上限建议为 2，COS list RPS 需要配置上限；GET 查询路径 COS list 次数必须为 0。
- 单个 job 默认超时建议为 10 分钟，可按 mode 配置。
- 失败重试默认最多 3 次，指数退避；reconcile 和 delete candidate 清理不得自动无限重试。
- running 超时任务在 worker 重启后必须恢复、重试或标记 failed。
- 建议配置项：

```text
WORKSPACE_SYNC_WORKER_CONCURRENCY=4
WORKSPACE_SYNC_PER_ASSISTANT_CONCURRENCY=1
WORKSPACE_SYNC_COS_IMPORT_CONCURRENCY=2
WORKSPACE_SYNC_COS_LIST_RPS=5
WORKSPACE_SYNC_JOB_TIMEOUT_SECONDS=600
WORKSPACE_SYNC_JOB_MAX_RETRIES=3
WORKSPACE_SYNC_RECONCILE_MISSING_CONFIRMATIONS=2
WORKSPACE_SYNC_RECONCILE_DELETE_HOLD_HOURS=24
```

- worker claim 必须使用 DB 锁、状态 CAS 或等价机制，避免多 worker 同时执行同一 job。
- worker 处理一批全量任务时必须按助手拆分子任务或分页扫描，不能把 2000 助手一次性加载到内存并无限并发。
- 停止 worker 不得影响 GET summary 读取已有 DB 索引。

### 监控与告警

| 指标 | 建议名称 | 建议阈值 | 动作 |
| ---- | -------- | -------- | ---- |
| pending job 数 | `workspace_sync_jobs_pending` | > 100 持续 10 分钟 | 告警，暂停全量任务入口 |
| running 超时任务数 | `workspace_sync_jobs_timed_out` | > 0 | 告警并执行恢复/失败标记 |
| job 失败率 | `workspace_sync_job_failure_ratio` | > 5% 持续 15 分钟 | 告警，保留旧索引 |
| DB summary p95 | `workspace_summary_db_duration_ms_p95` | > 800ms 持续 10 分钟 | 告警并检查索引/慢 SQL |
| 单次 summary SQL 次数 | `workspace_summary_sql_count` | > 5 | 视为回归 |
| GET 查询 COS list 次数 | `workspace_summary_get_cos_list_total` | > 0 | 视为回归 |
| 全助手 fan-out scan | `workspace_summary_fanout_scan_total` | > 0 | 视为回归 |
| stale 助手占比 | `workspace_index_stale_assistant_ratio` | > 20% 持续 30 分钟 | 告警，检查 worker 堆积 |
| orphan/degraded 记录数 | `workspace_index_degraded_records` | > 0 持续 30 分钟 | 告警并触发维护任务 |

日志要求：

- GET summary 日志必须包含 `route`、`page_size`、`filters`、`sql_count`、`cos_list_count=0`、`workspace_scan_count=0`。
- job 日志必须包含 `job_id`、`mode`、`scope_kind`、`scope_slug`、`assistant_id`、计数和错误码。
- 不能只依赖前端耗时；后端要能单独证明 DB 查询、COS list 和扫描次数。

### 性能验收基准

本地或测试环境必须提供可复现的模拟数据：

- 2000 个助手。
- 每个助手至少 100 条 `workspace_files` 记录，总计 200000 条。
- 每个助手至少 10 条能力安装索引记录，总计 20000 条。
- 默认 `page_size=50`，最大 `page_size=200`。

验收目标：

- 文件 summary GET 不触发 COS list，不触发 workspace scan，不按助手 fan-out。
- 单次默认分页查询 SQL 次数不超过 5 次。
- DB 查询必须提供 EXPLAIN 或等价证据，命中目标索引。
- 本地测试 p95 不超过 800ms；如果环境性能不足，必须在 `TEST_REPORT.md` 中记录硬件、DB 类型和偏差原因。
- `GET /api/v1/admin/workspace-files/summary?page=1&page_size=50` 在 2000 助手、200000 文件记录、20000 能力记录下返回成功，响应只含当前页，不含全量文件数组。
- `GET /api/v1/admin/assistants/{assistant_id}/workspace-files/summary?page_size=200` 必须命中助手+deleted+updated 索引，不触发其它助手扫描。
- `GET /api/v1/admin/assistants/{assistant_id}/workspace-items?force=true` 在 20000 能力记录下不得 COS list、不得创建 job；如索引 stale，返回状态和 `refresh_required=true`。
- 性能测试必须 monkeypatch 或计数对象存储 `list` 方法，证明 GET COS list=0；如果计数不可用，测试应让 COS list 抛异常并证明 GET 仍成功。
- SQL 次数不超过 5 的口径包含权限范围、当前页、count、聚合、job 状态；不包含连接池初始化和 migration 检查。

### 权限与安全边界

- 所有 DB 查询必须经过统一可见助手过滤。
- 后台全局查询只允许内部服务 token 或后台会话授权。
- 用户/助手侧查询必须限制 `assistant_id` 归属，并且不得调用后台全局索引 API。
- 同步任务创建必须记录操作者和来源。
- 全量任务、reconcile、对象删除候选必须比普通查询有更严格的操作边界和审计。

### 部署与回滚

- 发布计划：
  - Phase 1：UI 不传 `sync=true`，后端 summary DB-only 分页查询，单助手刷新 job/status 语义。
  - Phase 2：能力安装索引、能力写路径矩阵和能力页兼容。
  - Phase 3：COS import/reconcile/backfill 任务化、orphan/degraded 修复、清理候选保护。
- 迁移计划：
  - migration 必须可重复运行。
  - 增加唯一约束前必须执行去重检查。
  - backfill 必须支持断点续跑和重复执行。
  - 上线不要求全量 COS 扫描才能打开页面。
- 回滚计划：
  - UI 回滚：恢复旧按钮行为，但不得在 500-2000 助手环境默认启用全量同步。
  - 后端回滚：保留旧 summary 兼容接口，但通过配置关闭默认 sync。
  - DB 回滚：新增索引表可保留不读；旧 `workspace_files` 不做破坏性迁移。
  - 同步任务回滚：停止 worker，不影响已有 DB 展示索引。
  - 证明回滚成功：页面可打开、DB 查询返回、同步任务不再新增、无全助手扫描日志。

## 回滚方式

- 如何撤回配置：关闭新 job worker 和新 UI 刷新入口，保留 DB-only summary 或回到只读旧索引。
- 是否需要重启容器：后端路由/worker 配置变更需要重启对应 Admin/Security Service。
- 是否需要恢复数据库/文件：正常回滚不删除新增表；如 migration 失败，只回滚未完成 migration，不删除已有 `workspace_files`。
- 如何证明回滚成功：`GET /api/v1/admin/workspace-files/summary` 可返回，GET 路径 COS list 次数为 0，无 running job 堆积。

## 实现拆任务建议

Phase 1 拆分：

1. DB-only 查询层：新增 summary filters/page models、repository SQL、EXPLAIN 证据和 SQL 次数计数。
2. Admin summary 路由：全局 summary 和单助手 summary 改走 DB-only 入口，保留 `sync` 兼容但不执行扫描。
3. Job API 骨架：新增 `workspace_sync_jobs` 持久化、create/get 路由、幂等键和状态流转，不要求先完成 COS import。
4. `index_only` worker：支持单助手/单 scope job，包装现有无 missing 标记的扫描能力，记录计数和错误。
5. UI 文件页：去掉默认 `sync=true`，接入分页、stale 状态、刷新 job 轮询和失败保留旧索引。
6. Phase 1 验收：构造 2000/200000 数据，证明 GET COS list=0、SQL <=5、page_size 50/200、生效鉴权和 job/status。

Phase 2 拆分：

1. `assistant_workspace_items` 表、repository、migration 和写路径骨架；不包含历史数据补齐。
2. market install/upgrade、toggle、reuse、remove 写路径同步维护能力索引和 runtime reload 状态。
3. `workspace-items` GET 改为索引优先，`force=true` 不扫描、不建 job，返回 `refresh_required` 状态。
4. 能力索引与 `workspace_files` 聚合对齐测试，覆盖 runtime reload failed/pending/succeeded。

Phase 3 拆分：

1. `reconcile` job：missing candidate、连续确认、24 小时保留窗口和清理审计。
2. `cos_import` job：限定助手/scope 前缀导入，GET 路径禁止 COS list 的回归测试。
3. backfill job：从已有 `workspace_files`、受控 workspace/COS 来源补齐历史索引，支持断点续跑和重复执行。
4. orphan/degraded 记录和维护修复任务。
5. 全量刷新队列限流、worker 重启恢复和运维监控阈值。

## 测试与证据

| 行为 | 验证方式 | 证据 |
| ---- | -------- | ---- |
| B1 | 前端单测或浏览器检查，切换文件页不请求 `sync=true`；后端 monkeypatch 断言不调用 sync/COS/hydrate | Network 记录、pytest 输出 |
| B2 | API 单测覆盖分页和筛选参数，EXPLAIN 命中索引 | pytest 输出、EXPLAIN |
| B3 | 单测证明 `workspace-items` 默认不调用 COS scanner，读取能力安装索引 | monkeypatch 断言 |
| B4 | install/toggle/reuse/remove 单测检查能力索引更新和 reload 状态 | pytest 输出和 DB 记录 |
| B5 | 上传、审批 apply、runtime/index scan 单测检查 `workspace_files` upsert | pytest 输出 |
| B6 | 创建单助手 sync job，接口立即返回 job id，不阻塞完整扫描 | curl/API 单测 |
| B7 | 批量任务 worker 并发上限测试 | 日志或单测计数 |
| B8 | 任务状态迁移、幂等、超时恢复测试 | DB 记录 |
| B9 | index-only 同步缺失文件不删除、不标记最终删除 | 单测 |
| B10 | reconcile 缺失文件只进入 missing/候选清理，满足连续确认和 24 小时窗口 | 单测 |
| B11 | COS import 只有显式任务触发，GET 路径 COS list 次数为 0 | 单测或日志 |
| B12 | 页面展示 stale/sync 状态和旧索引提示 | 浏览器截图 |
| B13 | 同步失败保留旧索引 | 单测 |
| B14 | 2000 助手模拟分页查询不触发扫描，p95/SQL/COS list 达标 | 压测脚本输出 |
| B15 | 权限不足返回 401/403 且不泄露数据；用户侧不能复用后台全局索引 API | API 单测 |
| B16 | backfill 兼容已有 `workspace_files`，重复运行不产生重复索引 | 迁移测试 |
| B17 | worker 重启后任务恢复/失败标记 | 单测或集成测试 |
| B18 | 对象写成功 DB 写失败记录 orphan/degraded 到结构化日志或补偿队列 | 单测 |
| B19 | DB 写成功对象写失败标记 failed | 单测 |
| B20 | Phase 1 同时完成 UI、DB-only 查询和 job/status；只改 UI 不算通过 | diff、测试输出 |

## 风险与缓解

- 风险：页面变快但数据陈旧。
  - 缓解：显示 `last_synced_at`、`sync_status`、`sync_error`，并提供刷新当前助手任务。
- 风险：同步任务堆积。
  - 缓解：幂等键、并发上限、超时、重试次数、任务状态和监控。
- 风险：DB 与 COS 双索引不一致。
  - 缓解：页面默认只信 DB；COS 只通过显式 import/reconcile 更新 DB。
- 风险：误删对象存储内容。
  - 缓解：普通同步不删除；reconcile 连续确认并满足 24 小时窗口后才进入清理。
- 风险：能力启用状态和 runtime 实际状态分叉。
  - 缓解：能力安装索引记录 saved 状态，runtime reload 状态单独记录。
- 风险：权限过滤遗漏导致跨助手泄露。
  - 缓解：统一查询入口和可见助手过滤；单测覆盖 unauthorized/forbidden。
- 风险：迁移期间新旧逻辑混用。
  - 缓解：分阶段开关，旧接口兼容但 UI 默认走新路径；TEST_REPORT 按 Phase 标记。

## 后续事项

- 本 spec 经用户确认后可将状态从 `draft` 改为 `ready`。
- `TEST_REPORT.md` 已提供验收骨架；实现后必须按当前 HEAD 补证据。
- 若决定调整 Phase 1 范围，必须同步更新 `PRODUCT.md`、`DECISIONS.md` 和本文件的行为映射。
