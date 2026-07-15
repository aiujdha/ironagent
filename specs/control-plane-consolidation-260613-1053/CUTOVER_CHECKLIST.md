# 战役一切换清单 · workspace 元数据 SQLite → MySQL(CUTOVER_CHECKLIST）

> 配套 `TECH-battle1-data.md` 的五阶段(P1 备份 → P2 接口补齐 → P3 影子读 → P4 切闸 → P5 退役)。
> 本变更提供工厂路由 / sync job DB store + 028 / 迁移器 / 影子读比对工具,
> 本文是**生产切换的取证 + 步骤 + 回滚**。
>
> ⚠️ **本文是规划清单,不是授权。** 取证项的判断、切闸时机、漂移裁决,留给懂线上、有数据、对后果负责的人。
> 标 🔬 = 须现场取证(kubectl/MySQL/华为云),代码侧定不了;标 ✅ = 已回代码/配置核实。

## 0. 当前基线(已核实)

- ✅ `IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND` 默认值已改为 **mysql**;prod overlay 也显式设置 `IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND=mysql`。部署本变更即默认切到 MySQL,若需延后切换必须显式设为 `sqlite`。
- 🔬 prod 控制面 `IRON_AGENT_DATABASE_URL` 是否指向 in-cluster MySQL需现场复核;workspace 文件元数据默认写 MySQL。
- ✅ prod 三个 SQLite 路径在 `control-plane-shared.env:29-31`(workspace-files / item-index / sync-jobs)。
- ✅ prod 连接池已显式设 `DB_POOL_SIZE=8 + DB_MAX_OVERFLOW=8`(`control-plane-shared.env:7-8`);MySQL `--max-connections=512`(`mysql.yaml:48`)。
- 🔬 **prod alembic head 需现场确认**。若仍在 `021`,需先跑 `021→027`,再跑本变更新增的 `027→028`;028(workspace_sync_jobs)在升级前不会存在。

---

## 1. 取证清单(切闸前必须知道)

| # | 取什么 | 为什么 | 怎么取 |
|---|---|---|---|
| **A** 🔬🔴 | **assistant-workspaces PVC 真实后端 = local-PV 还是华为 SFS/NFS?admin/security 两 pod 同节点吗?** | 风险地基。**仓库 overlay 把它打成 local-PV**(`workspace-pvc-local-patch.yaml`:`storageClassName: ironagent-local`),但记忆记录用户说生产是 **SFS/NFS**——**直接矛盾**。local-PV(单节点)→ SQLite WAL 安全、不急切;NFS(跨节点)→ 已在损坏边缘、切换是正确性修复。 | `kubectl describe pvc assistant-workspaces`(看 PV/provisioner);`kubectl get pods -o wide`(admin/security 同节点?) |
| **B** 🔬 | **三个 SQLite 库 `PRAGMA integrity_check`(分别) + 各表 `COUNT(*)`(含/不含 deleted) + events 量级** | 若 A=NFS 跨节点写,损坏可能已静默发生——**损坏的源迁过去就是损坏的目标**。量级决定迁移窗口(events 量大,迁移器按需可分批)。 | 对三个 `.db` 分别 `PRAGMA integrity_check`;`SELECT COUNT(*)` 各表;数 `push_requests.json` 行 |
| **C** 🔬 | **复核 MySQL `max_connections=512` 实际生效 + `Threads_connected` 基线 + 数据盘余量** | 现状已显式设 512 + 8/8 池(非默认 151)。按 R2 复核:3 池/进程 × 16 × 2 进程 ≈ 96 < 512,不触顶。加 workspace 表后的存储增量。 | `SHOW VARIABLES LIKE 'max_connections'`(确认 512);`SHOW STATUS LIKE 'Threads_connected'`;盘余量 |
| **D** 🔬 | **规划 prod `021 → 028` 完整升级路径** | 028 新建 `workspace_sync_jobs` 表;其中两步是硬前提:① `025`(active_path_key 唯一索引=DB 层行级隔离)② `028`(建表)。本变更新增 `027→028` 增量 SQL。 | 确认 prod head；按序跑 `021_to_027` → `027_to_028`（或 `alembic upgrade head`）；每步前备份,并在验证库复跑 `027→028` |
| **E** 🔬🔴 | **mysqldump 备份产物落点 ≠ MySQL 数据盘,且实测可恢复** | CronJob 已部署(每天 2am），但产物落 `assistant-workspaces` PVC(`mysql-backup-cronjob.yaml:73`)。**若与 MySQL 数据同处一个 local-PV → 同盘同 blast radius，节点/盘坏两者皆失**（CronJob 自带注释明写这是待确认门）。 | 确认备份落点 PVC 后端 ≠ MySQL local-PV；`kubectl get cronjob,jobs`看真跑过;取一份产物在测试库恢复验行数 |
| **F** 🔬 | SFS 类型(容量型/Turbo)+ IOPS（**仅当 A 确认是 SFS 才相关**） | 迁移期从 SFS-SQLite 全量读的性能 + 现状元数据风暴费用 | 华为云 SFS 控制台 |

> A/F 互斥:先定 A,F 才可能相关。E 不过关则 P5 退役不可做（回滚链断裂）。

---

## 2. P3 影子读校验（不切写，sqlite 仍权威）

**目的**:证明 MySQL（迁移器灌入）与现役 SQLite 读出的数据一致，再切闸。

1. **【前置门】**先把 prod MySQL schema 升到 `028`（取证 D）——否则迁移器写 `workspace_sync_jobs`（028 才有的表）/依赖 `active_path_key` 唯一索引（025）会报错或丢隔离。
2. 跑迁移器：`python -m scripts.migrate_workspace_files_to_mysql`（幂等、含软删、自带 COUNT 对账）。
3. **值级比对**:`python -m scripts.compare_workspace_files_backends`（本变更新增）。
   - 它已内置 **8 个文件字段读语义差异白名单**（不是数据错）：`sync_status`（MySQL `_normalize_sync_status` 归一）/ `direction`（空时按 file_role 推导）/ `storage_provider`（SQLite 表无该列恒派生）/ `checksum`（双列 fallback）/ `storage_status` / `storage_path` / `object_url` / `storage_error`（SQLite 单边 fallback）；**datetime 全部归一 UTC 再比**（MySQL DATETIME 读回 naive）。
   - 默认只报**真实差异**(其余字段不一致 / 缺行 / 多行) + 退出码 1 阻断;`--show-known` 可把白名单差异也打出来人工核对。
4. 抽样 admin 工作区/文件页:sqlite 后端 vs mysql 后端返回一致（注意断点2/3 的已知 parity 差异）。
5. **判据**:`compare` 的 `missing_in_mysql` / `extra_in_mysql` / 非白名单 `value_mismatches` 全为零，才进 P4。任何 `MISSING` = 迁移丢数,必须查清。

---

## 3. P4 切闸（单次变更窗口,可回滚）

1. 低峰 maintenance 窗口;确认取证 A–E 过关 + mysqldump 已跑且可恢复（E）。
2. **【硬前置】**MySQL schema 已在 `028`(应在停 scan、低峰前就升好并预演,见 P3 步骤 1）。
3. **停 scan loop**:`IRON_AGENT_WORKSPACE_RESULT_SCAN_ENABLED` 由 `true`→`false`(✅ 查实 prod 现为 true)——冻结源、停新无主文件扫描写。
4. 跑**迁移器最终增量** + `compare` 复核（catch up 停 scan 前的写）。
5. **切 BACKEND**:确认 `control-plane-shared.env` 为 `IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND=mysql` 并 apply。若上线策略要求先部署代码但暂缓切换,必须临时显式设置 `IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND=sqlite`。
6. **重启加载了该配置的所有控制面进程**:admin + security(control-data)——按 base 里 replicas=1 的控制面 Deployment 列全,别漏。
7. 验证:
   - 写一个文件 → MySQL `workspace_files` 有行;`/readyz` 的 workspace_files 可写检查绿
   - admin 文件列表正常;push 写 `external_push_requests`（非 `push_requests.json`）；无 `AttributeError`（断点3 item_index 应优雅降级返回旧聚合,不崩）
   - dangerous/onlyread 等既有用例不回归
8. **SQLite 数据不删**（P5 才删）= 分钟级回滚靶。

---

## 4. P5 退役（观察 1–2 周后，过【不可逆分水岭】）

> ⚠️ **P4 前回滚/暂缓切换**:`BACKEND` 显式改为 `sqlite` + 重启,SQLite 原样保留,零数据恢复。
> **P5 退役后回滚**:需从 mysqldump + SQLite 备份恢复(灾难级)。**P5 前置 gate = 取证 E 的 mysqldump 落独立后端且实测可恢复。**

- 删三个 SQLite 文件 + `src/workspace_files/sqlite_repository.py`
- 删配置键:三个 `*_SQLITE_PATH`(`control-plane-shared.env:29-31`)+ `src/shared/config.py` 对应约 8 字段
- 删 `admin_index.py:159,224` 的 `isinstance(SQLiteWorkspaceFileRepository)` 快路径分支(TECH:62)
- `item_index`:改为从 MySQL 重建的进程内缓存,退役 `workspace-item-index.sqlite`(TECH:47)
- 退役 `push_requests.json`（push 台账已写 `external_push_requests`，TECH:56）
- sync_jobs 历史默认不搬(短生命周期终态),只建表 + 切后端

---

## 5. 回滚

**触发条件**(任一即回滚):
- 切 mysql 后 `/readyz` 的 MySQL workspace_files 可写检查失败(TECH:103)
- `compare` 影子读/对账出现**非白名单**差异
- 连接池触顶告警(现状 512 不应触顶,但需有阈值)
- push 台账写 `external_push_requests` 失败(B7)

**步骤**:`IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND=sqlite` + 重启控制面 → 分钟级回到 SQLite。

**⚠️ 回滚的两个坑**:
1. **回滚靶有效期递减**:切 mysql 后 MySQL 接受新写;切回 sqlite **不会把这段增量回流 SQLite**——切后运行越久,回滚数据缺口越大。需定回滚时间窗(观察期内回滚要么重跑 MySQL→SQLite 反向增量,要么接受缺口)。反向脚本:`scripts/migrate_workspace_files_to_sqlite.py`(已有)。
2. **回滚验证**:切回后再跑一次 `compare`（确认无新偏移）+ 文件总数 count 对照切换前基线 + `/readyz` 绿。

---

## 6. 交付物索引

| 阶段 | 工具 | 状态 |
|---|---|---|
| P2 工厂路由 | `src/workspace_files/repository.py` `_resolve_metadata_backend` | 本变更提供 |
| P2 sync job store + 建表 | `sync_jobs.py` + `migrations/versions/028_workspace_sync_jobs.py` | 本变更提供 |
| P3/P4 迁移器 | `scripts/migrate_workspace_files_to_mysql.py` | 本变更提供 |
| P3 影子读比对 | `scripts/compare_workspace_files_backends.py` | 本变更提供 |
| 回滚反向迁移 | `scripts/migrate_workspace_files_to_sqlite.py` | ✅ 既有 |
| 027→028 增量 SQL | `scripts/sql/ironagent_incremental_027_to_028.sql` | 本变更提供,上线前需在验证库复跑 |
