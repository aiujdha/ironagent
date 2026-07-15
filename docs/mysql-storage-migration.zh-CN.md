# IronAgent MySQL 存储迁移说明

这份文档说明当前 IronAgent 如何把核心存储切到 MySQL，并验证控制面、审批、审计、用户绑定和 workspace 文件元数据已经落在数据库里。

## 1. 当前状态

当前默认核心存储已经切到 MySQL：

- `assistants` -> `assistants`
- `approvals` -> `approvals`
- `audit` -> `audit_events`
- `users` -> `users`
- `user_assistant_bindings` -> `user_assistant_bindings`

对上述核心存储，JSON 文件存储现在只保留为兼容旧数据和迁移脚本输入来源，不再是默认主路径。

workspace 文件相关存储也默认走 MySQL：

- `workspace_files` / `workspace_file_events` 元数据默认写入 MySQL；如需临时回退，可显式设置 `IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND=sqlite`。
- `workspace_sync_jobs` 默认写入 MySQL；显式 `sqlite` 时才使用 `IRON_AGENT_WORKSPACE_SYNC_JOB_SQLITE_PATH`。
- `external_push_requests` 由 workspace file repository 统一承载，MySQL 后端写 `external_push_requests` 表。

## 2. 关键环境变量

至少确认这两个配置：

```env
IRON_AGENT_STORAGE_BACKEND=mysql
IRON_AGENT_DATABASE_URL=mysql+pymysql://<user>:<password>@<host>:<port>/<db>?charset=utf8mb4
```

如果你使用仓库根目录的 `.env`，服务默认会从这里读取配置。

workspace 文件元数据相关还有一个变量，默认值为 `mysql`：

```env
IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND=mysql
```

取值为 `sqlite` 时，控制面会回退到共享 SQLite 元数据文件，provisioner 也会把 SQLite 元数据目录挂载进 assistant runtime 容器（挂载点由 `IRON_AGENT_RUNTIME_WORKSPACE_FILE_SQLITE_MOUNT_PATH` 控制，默认 `/home/node/.ironagent/workspace-metadata`）。默认 `mysql` 时不会注入这组 SQLite metadata env。

## 3. 初始化数据库

本地或手工初始化时执行 Alembic：

```bash
PYTHONPATH=$(pwd) uv run alembic -c migrations/alembic.ini upgrade head
```

Docker/K8s 部署时，如果 `admin` 容器里的 `IRON_AGENT_AUTO_MIGRATE=1`，启动前会自动执行同一条迁移命令。
如果生产 overlay 显式设置了下面的值，就不会自动迁移，发布时必须手工执行 Alembic：

```env
IRON_AGENT_AUTO_MIGRATE=0
```

生产发布前至少确认版本已到最新 head：

```bash
kubectl -n ironagent exec deploy/admin -- sh -lc '
  cd /app &&
  PYTHONPATH=/app .venv/bin/alembic -c migrations/alembic.ini upgrade head &&
  PYTHONPATH=/app .venv/bin/alembic -c migrations/alembic.ini current
'
```

建表完成后，数据库里应该至少看到：

核心表：

- `assistants`
- `approvals`
- `audit_events`
- `users`
- `user_assistant_bindings`
- `workspace_files`
- `workspace_file_events`
- `workspace_sync_jobs`
- `external_push_requests`
- `alembic_version`

gateway 表（go-gateway 任务/队列/运行时/路由）：

- `gateway_tasks`
- `gateway_task_events`
- `gateway_task_queue`
- `gateway_event_outbox`
- `gateway_event_sequences`
- `gateway_dead_letters`
- `gateway_idempotency_keys`
- `gateway_hmac_nonces`
- `gateway_webhook_deliveries`
- `gateway_runtimes`
- `gateway_runtime_leases`
- `gateway_runtime_heartbeats`
- `gateway_runtime_instances`
- `gateway_conversation_routes`

协作表：

- `collaboration_tasks`
- `collaboration_events`
- `collaboration_claims`
- `shared_workspace_objects`
- `shared_object_permissions`

## 4. 初始化默认助手

如果需要补默认 `demo-main` 助手，执行：

```bash
uv run python scripts/seed.py
```

## 5. 从旧 SQLite workspace 文件元数据导入

如果环境曾经使用共享 SQLite 保存 workspace 文件元数据，切到默认 MySQL 前必须先迁移这三类数据：

- `workspace_files`
- `workspace_file_events`
- `external_push_requests` 后续新写入由 MySQL repository 承载，历史外部 push 请求若仍保存在 JSON，可按业务需要另行留档；workspace 文件列表和事件以 SQLite 迁移为准。

先 dry-run，确认源库和目标库数量：

```bash
kubectl -n ironagent exec deploy/admin -- sh -lc '
  cd /app &&
  PYTHONPATH=/app \
  .venv/bin/python -m scripts.migrate_workspace_files_to_mysql \
    --sqlite-path /workspace-root/control-plane/data/workspace_files/workspace-files.sqlite \
    --dry-run
'
```

dry-run 通过后执行真实迁移：

```bash
kubectl -n ironagent exec deploy/admin -- sh -lc '
  cd /app &&
  PYTHONPATH=/app \
  .venv/bin/python -m scripts.migrate_workspace_files_to_mysql \
    --sqlite-path /workspace-root/control-plane/data/workspace_files/workspace-files.sqlite
'
```

迁移脚本默认只补齐缺失的 `file_id` / `event_id`。若目标 MySQL 已有同 ID 但内容不同的行，脚本会拒绝覆盖并报 conflict；先确认冲突来源，不要删除已经写入 MySQL 的行。确需用 SQLite 覆盖目标行时，必须显式追加危险参数 `--overwrite-existing`。

迁移后执行 shadow compare。输出应为 `shadow-read OK`；在线迁移期间，MySQL 可能出现 agent runtime 新写入的 `extra_in_mysql`，默认不作为阻断。若出现 `missing_in_mysql` 或非白名单字段差异，先排查再切换 `IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND=mysql`。如果环境已停写并要求严格全量一致，可追加 `--strict-extra-mysql`。

```bash
kubectl -n ironagent exec deploy/admin -- sh -lc '
  cd /app &&
  PYTHONPATH=/app \
  .venv/bin/python -m scripts.compare_workspace_files_backends \
    --sqlite-path /workspace-root/control-plane/data/workspace_files/workspace-files.sqlite
'
```

默认 MySQL 元数据路径还会被 `/readyz` 检查:当 `IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND=mysql` 时，admin 与 security-service 会要求 `workspace_files` / `workspace_file_events` / `workspace_sync_jobs` 表存在，并且 `alembic_version` 至少到 `029_workspace_file_long_path_columns`。

## 6. 从旧 JSON 导入

如果你手头还有旧文件：

- `assistants.json`
- `approvals.json`
- `audit.jsonl`

先把它们放到当前配置对应的位置，然后执行：

```bash
uv run python scripts/migrate_json_to_mysql.py
```

这个脚本会：

- 逐条 upsert `assistants`
- 逐条 create/save `approvals`
- 逐条写入 `audit_events`

## 7. 最小验收

建议按下面顺序验收：

1. 访问后台，确认助手列表能正常显示
2. 创建或更新一个 assistant，确认 `assistants` 表有变更
3. 触发一次需要审批的请求，确认 `approvals` 表写入记录
4. 触发一次 post-execute hook，确认 `audit_events` 表写入记录

也可以直接查库做烟测：

```sql
select count(*) from assistants;
select count(*) from approvals;
select count(*) from audit_events;
select version_num from alembic_version;
```

workspace 文件元数据的烟测默认查 MySQL：

```sql
select count(*) from workspace_files;
select count(*) from workspace_file_events;
select count(*) from workspace_sync_jobs;
```

如果本环境是从 SQLite workspace 元数据迁过来的，count 烟测之后还要保留一次 shadow compare 结果作为验收证据：

```bash
kubectl -n ironagent exec deploy/admin -- sh -lc '
  cd /app &&
  PYTHONPATH=/app \
  .venv/bin/python -m scripts.compare_workspace_files_backends \
    --sqlite-path /workspace-root/control-plane/data/workspace_files/workspace-files.sqlite
'
```

当前期望的 `alembic_version.version_num` 是最新 head：

```sql
select version_num from alembic_version;
-- 029_workspace_file_long_path_columns
```

## 8. 已知事项

- 当前环境里的 `pytest` 可能会在启动 capture 阶段异常退出；如果遇到这个问题，优先参考实际连库结果、`ruff`、`mypy`、`compileall` 和手工烟测。
- `market/catalog` 目前仍保留文件存储，没有并入这次核心 MySQL 迁移范围。
- 如果页面工作空间区域出现 `TypeError: Failed to fetch`，先检查后台日志是否有 `doesn't exist` 一类的缺表报错。默认 MySQL 元数据路径依赖 `workspace_files` / `workspace_file_events` / `workspace_sync_jobs` 表；如果缺表，说明当前镜像或部署流程没有正确执行 Alembic migration。
