# IronAgent 数据库运维 SQL

生产部署若不便直接执行 `alembic`,可由运维手动执行本目录下的 SQL。

> ⚠️ 注意:**不能**用 `alembic upgrade head --sql`(offline 模式)生成这套 SQL —— 多个 repair/backfill
> 迁移含 `inspect()` 等运行时数据逻辑,offline 没有真实连接会直接崩溃(只能渲染到第 4 个迁移)。
> 因此这里的 SQL 是「空库实际跑完 `alembic upgrade head` 后 `mysqldump` 导出最终 schema」得到的,
> 已经过 round-trip 验证(用导出的 SQL 在全新空库重建,29 张表 + 外键 + `alembic_version` 完全一致)。

## `ironagent_schema_full.sql` —— 全新部署(空库)

- 对应 alembic head:`029_workspace_file_long_path_columns`
- 后端:MySQL 8.x,字符集 `utf8mb4` / `utf8mb4_unicode_ci`
- 内容:29 张表的完整 DDL(含索引、外键)+ `alembic_version` 版本标记

### 用法

```bash
# 1) 建库(若尚未创建)
mysql -uroot -p -e "CREATE DATABASE ironagent CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"

# 2) 执行 schema(文件已带 FOREIGN_KEY_CHECKS=0,无需关心建表顺序)
mysql -uroot -p ironagent < scripts/sql/ironagent_schema_full.sql
```

执行后库即为 head `029`,且 `alembic_version` 已写入对应版本号,后续可正常用 `alembic upgrade` 增量升级。

## 增量升级(已有数据的库)

**不要**用上面的全新 schema 去覆盖已有数据的库,要用对应区间的增量 SQL。

### `ironagent_incremental_021_to_027.sql` —— 021 → 027(**线上当前适用**,生产加固版)

适用线上 `alembic_version = 021_repair_legacy_assistant_ironagent_config` 的库(**线上实测当前就是 021**)。
区间含 022~027:`022` 建 5 张 collaboration 表(`shared_workspace_objects` / `shared_object_permissions` /
`collaboration_tasks` / `collaboration_claims` / `collaboration_events`,纯 DDL、无数据回填);`023~027`
与下方 022→027 加固版**逐句相同**(含 3 处数据回填 + `ROW_NUMBER` 去重 + 归一化口径 + 前置断言)。

- 前置断言起点放宽到 **021~027**(支持任一中间步骤重入);其余加固点与 022→027 完全一致(见下一小节)。
- **已验证**(真实 MySQL 8.4,干净 021 起点 + 脏数据):collaboration 5 表 + 7 索引从无到有建成、
  gateway 3 表 + 唯一索引 + `capacity_wait` 4 列全建成、`workspace_files` 归一化去重正确、最终到 027;
  幂等重跑无误;错误起点(020)被前置断言 `ERROR 1242` 拦截。

```bash
mysqldump ... ironagent > backup_before_021_to_027.sql                     # 1) 先备份!
mysql -uroot -p ironagent -e "SELECT version_num FROM alembic_version;"     # 2) 预期 021
mysql -uroot -p ironagent < scripts/sql/ironagent_incremental_021_to_027.sql  # 3) 必须用重定向,勿用交互 source
# 末尾应输出 final_version=027 / gate=OK
```

> ⚠️ 含数据迁移(023/025 回填),**执行前务必备份**。**上线前预检清单**见下方 022→027 小节(对 021→027 同样适用;
> collaboration 表那段是纯 DDL,无额外预检)。执行方式必须是 `mysql < 文件`(批处理遇错即停)。

### `ironagent_incremental_022_to_027.sql` —— 022 → 027(区间子集,022 起点适用,生产加固版)

适用 `alembic_version = 022_collaboration_repository_tables` 的库(若线上已在 022;线上当前是 021,请用上面的 021→027)。区间含 023/024/025/026/027,
其中 **3 处数据回填**(纯 DDL 表达不了,本 SQL 用等价 SQL 复现):`023` 把 `gateway_runtimes` 去重后
`INSERT...SELECT` 进 `gateway_runtime_instances`;`025` 对 `workspace_files` 去重 + 回填
`active_path_key = SHA2(...)`,顺序严格「加列 → 去重 → 回填 → 建唯一索引」。

**本版经多维上线 review + 真实 MySQL 实测加固**(初版在生产脏数据下会 `ERROR 1062` 上线失败):

1. 顶部 `SET NAMES utf8mb4` + 两道前置断言(MySQL 8.0+、起点必须单行 022/027),不满足以 `ERROR 1242` 中止;
2. `023` 用 `ROW_NUMBER()` 去重:`gateway_runtimes` 多行规整后映射到同一 `runtime_id`(多 assistant 复用 pod、runtime_id 空)不再撞主键,保留 `updated_at` 最新一行;
3. `025` 去重与 `active_path_key` **统一归一化口径**(`REGEXP_REPLACE` 去首尾所有空白 ≡ Python `strip`,含 tab/换行/NBSP/全角 + 反斜杠转斜杠 + 去前导斜杠):凡归一化后撞 key 的活跃行(如 `a.txt`/`/a.txt`、`dir\b.txt`/`dir/b.txt`)归同组去重,消除"建唯一索引 1062"失败。

**已验证**:① 脏数据上 alembic 崩(1062 停 024)、本 SQL **成功到 027** 且去重正确;② 干净数据上
`gateway_runtime_instances`/`workspace_files` **逐行 md5 ≡ alembic**、最终 schema 一致;③ 幂等重跑无误;④ 断言能拦错误起点。

> **与 alembic 的已知偏差(已评审接受)**:脏数据上本 SQL 比 alembic 更稳——alembic 按原始 filename 分组
> 会在建唯一索引时崩,本 SQL 按归一化分组多去重一行让索引建得起来(干净数据两者一致)。alembic 025/023
> 源码本身有同样缺陷,建议另行修复迁移源码。

#### 上线前预检(逐条在生产库执行)

```sql
-- 0) 环境与起点(SQL 断言也会拦,但先确认避免白跑)
SELECT VERSION();                                   -- 必须 8.0+
SELECT version_num FROM alembic_version;            -- 必须单行 022

-- 1) 必查:gateway_runtimes 是否有 NOT NULL 列为 NULL(本 SQL 不兜底,会 INSERT 1048 失败)
SELECT COUNT(*) FROM gateway_runtimes
WHERE TRIM(COALESCE(assistant_id,''))<>'' AND (status IS NULL OR capacity IS NULL OR updated_at IS NULL);
--   返回 >0 必须先在源表修复,再执行迁移。

-- 2) 知情:哪些 runtime 会被去重(同一规整 runtime_id 多行 → 保留 updated_at 最新)
SELECT COALESCE(NULLIF(TRIM(runtime_id),''),TRIM(assistant_id)) AS k, COUNT(*) n, GROUP_CONCAT(assistant_id)
FROM gateway_runtimes WHERE TRIM(COALESCE(assistant_id,''))<>'' GROUP BY k HAVING n>1;

-- 3) 知情:哪些 workspace_files 活跃行会被去重(归一化后撞 key → 保留最新、其余标 deleted)
SELECT assistant_id, bucket,
       TRIM(LEADING '/' FROM REGEXP_REPLACE(REPLACE(filename,'\\','/'),'^[[:space:]]+|[[:space:]]+$','')) AS norm,
       COUNT(*) n, GROUP_CONCAT(file_id)
FROM workspace_files WHERE COALESCE(deleted,0)=0
  AND TRIM(LEADING '/' FROM REGEXP_REPLACE(REPLACE(COALESCE(filename,''),'\\','/'),'^[[:space:]]+|[[:space:]]+$',''))<>''
GROUP BY assistant_id, bucket, norm HAVING n>1;

-- 4) 规模评估(全表 UPDATE 的锁/耗时/主从延迟)
SELECT COUNT(*) total, SUM(COALESCE(deleted,0)=0) active FROM workspace_files;

-- 5) 保险:若这三张新表已存在(非干净 022),核对索引是否齐全
SELECT table_name, GROUP_CONCAT(DISTINCT index_name) idx FROM information_schema.statistics
WHERE table_schema=DATABASE()
  AND table_name IN ('gateway_runtime_instances','gateway_runtime_leases','gateway_conversation_routes')
GROUP BY table_name;
```

#### 执行

```bash
mysqldump ... ironagent > backup_before_022_to_027.sql          # 1) 先备份!
mysql -uroot -p ironagent < scripts/sql/ironagent_incremental_022_to_027.sql   # 2) 必须用重定向,勿用交互 source
# 末尾应输出 final_version=027 / gate=OK
```

> ⚠️ 含数据迁移,**执行前务必备份**。`workspace_files` 去重/回填是全表 UPDATE,大表低峰执行、关注主从延迟。
> 执行方式必须是 `mysql < 文件`(批处理遇错即停);交互式 `source` 遇错会继续、断言失效。

### `ironagent_incremental_025_to_027.sql` —— 025 → 027(区间子集,纯 DDL)

适用线上 `alembic_version = 025_workspace_files_active_path_key` 的库。026/027 是后加的两个迁移
(gateway 队列背压索引 + `gateway_task_queue` 的 capacity-wait 列),**纯 DDL、无数据回填**。

- **幂等**:每步查 `information_schema`,已存在则跳过,可安全重复执行;
- 末尾把 `alembic_version` 更新到 027(兼容当前为 025 或 026 的库);
- **已验证**:把验证库 downgrade 到 025 → 执行本 SQL → 列/索引/`alembic_version` 与 `alembic upgrade head`
  完全一致,且重复执行无误。

```bash
# 执行前确认当前版本应为 025
mysql -uroot -p ironagent -e "SELECT version_num FROM alembic_version;"
mysql -uroot -p ironagent < scripts/sql/ironagent_incremental_025_to_027.sql
```

### `ironagent_incremental_027_to_028.sql` —— 027 → 028(区间子集,纯 DDL,生产加固版)

适用线上 `alembic_version = 027_gateway_queue_capacity_wait_state` 的库。028 是 workspace 元数据迁
MySQL（战役一)新增的 `workspace_sync_jobs` 表(1 唯一索引 + 4 普通索引),**纯 DDL、无数据回填**
（历史 sync job 默认不迁,新 job 切后端后重新入库)。

> ⚠️ **线上当前实测在 021**,需先跑 `021_to_027` 升到 027,再跑本文件到 028。

- 顶部 `SET NAMES utf8mb4` + **前置断言**(起点必须单行 027,或已是 028 的重入),否则 `ERROR 1242` 中止
  ——避免在 021 等错误起点上凭空建表 + 版本错位;
- **幂等**:`CREATE TABLE IF NOT EXISTS` + `alembic_version` 按 `WHERE` 精确更新(重入匹配 0 行);
- DDL 与 `ironagent_schema_full.sql` 的 `workspace_sync_jobs` 逐字一致,与 `028` alembic 迁移同源;
- 上线前建议在验证库复跑:schema_full 载入 → 模拟回 027 → 跑本 SQL → `final_version=028 / gate=OK`、
  表 + 索引建成;幂等重跑无误;起点改 021 应被前置断言 `ERROR 1242` 拦截。

```bash
mysqldump ... ironagent > backup_before_027_to_028.sql                      # 1) 先备份
mysql -uroot -p ironagent -e "SELECT version_num FROM alembic_version;"      # 2) 预期 027
mysql -uroot -p ironagent < scripts/sql/ironagent_incremental_027_to_028.sql # 3) 末尾应输出 final_version=028 / gate=OK
```

### `ironagent_incremental_028_to_029.sql` —— 028 → 029(区间子集,workspace_files 长路径兼容)

适用线上 `alembic_version = 028_workspace_sync_jobs` 的库。029 把 `workspace_files` 中可能承载历史深层路径或对象引用的列
(`filename` / `relative_path` / `source_ref` / `storage_key` / `storage_path` / `object_key` / `object_url`)放宽到 `TEXT`。

- 背景:SQLite 历史库不限制 `TEXT` 长度,迁到 MySQL 旧 `varchar(1024/2048/4096)` 时可能出现 `ERROR 1406 Data too long`；
- **幂等**:`ALTER TABLE ... MODIFY` 可重复执行,`alembic_version` 只从 028 更新到 029；
- 在线影响:会 `ALTER TABLE workspace_files`,建议备份后低峰执行,迁移失败中途已写入的 `workspace_files` 行不要删除,重跑脚本会按主键继续补齐。

```bash
mysqldump ... ironagent > backup_before_028_to_029.sql                      # 1) 先备份
mysql -uroot -p ironagent -e "SELECT version_num FROM alembic_version;"      # 2) 预期 028
mysql -uroot -p ironagent < scripts/sql/ironagent_incremental_028_to_029.sql # 3) 末尾应输出 final_version=029 / gate=OK
```

### 其他版本区间(尤其 < 025)

若线上不是 025,区间可能纳入含**数据回填/修复**逻辑的迁移(如 `007` backfill、各 `repair`),
offline `--sql` 不可用。可靠生成方法:把一个验证库 downgrade 到线上版本 → 开 `general_log` →
`alembic upgrade head` 捕获实际执行的 SQL;含数据逻辑的迁移需逐条人工核对其等价 `UPDATE`。
若条件允许,直接 `alembic upgrade head` 仍是最省心的方式(它按 `alembic_version` 只跑缺失的增量)。

## 迁移有更新后,如何重新生成本文件

```bash
# 起一个空 MySQL(用项目的 Dockerfile.mysql 镜像)
docker run -d --name tmp-mysql -p 13306:3306 \
  -e MYSQL_ROOT_PASSWORD=root -e MYSQL_DATABASE=ironagent \
  -e MYSQL_USER=ironagent -e MYSQL_PASSWORD=ironagent \
  ironagent-mysql:8.4-fips --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci

# 在空库上实际跑迁移(online,不能用 --sql)
IRON_AGENT_DATABASE_URL='mysql+pymysql://ironagent:ironagent@127.0.0.1:13306/ironagent?charset=utf8mb4' \
  uv run alembic -c migrations/alembic.ini upgrade head

# 导出最终 schema,并在末尾追加 alembic_version 标记
docker exec tmp-mysql mysqldump -uironagent -pironagent \
  --no-data --skip-comments --skip-add-drop-table --no-tablespaces ironagent \
  > scripts/sql/ironagent_schema_full.sql
HEAD=$(docker exec tmp-mysql mysql -uironagent -pironagent ironagent -N \
  -e "SELECT version_num FROM alembic_version;")
echo "INSERT INTO alembic_version (version_num) VALUES ('$HEAD');" \
  >> scripts/sql/ironagent_schema_full.sql

docker rm -f tmp-mysql
```
