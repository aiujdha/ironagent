-- ============================================================================
-- IronAgent 增量升级 SQL:027 -> 028
-- 适用:线上 alembic_version = 027_gateway_queue_capacity_wait_state 的已有库
--       （注:线上当前实测在 021,需先跑 021_to_027 到 027,再跑本文件到 028）
-- 后端:MySQL 8.x,字符集 utf8mb4 / utf8mb4_unicode_ci
-- 内容:028（workspace_sync_jobs 表 + 1 唯一索引 + 4 普通索引)
--       纯 DDL、无数据回填(workspace_sync_jobs 是新建空表;历史 sync job 默认不迁)
-- 幂等:CREATE TABLE IF NOT EXISTS;alembic_version 按 WHERE 精确更新(重入匹配 0 行)
-- 来源:与 ironagent_schema_full.sql 的 workspace_sync_jobs DDL 逐字一致;
--       与 migrations/versions/028_workspace_sync_jobs.py 同源
-- 在线影响:CREATE TABLE 仅建新表,不触碰既有表,无锁、无读写阻塞
-- 加固点:顶部 SET NAMES + 前置断言(起点必须单行 027,或已是 028 的重入),
--         不满足以 ERROR 1242 中止——避免在 021 等错误起点上凭空建表 + 版本错位
-- 执行:必须用 `mysql -uroot -p ironagent < 本文件`(批处理遇错即停),勿用交互 source
-- ============================================================================

SET NAMES utf8mb4;

-- ---- 前置断言:起点必须是单行 027(或已是 028 的重入),否则中止 ----
DO (SELECT IF(
       (SELECT COUNT(*) FROM alembic_version) = 1
   AND (SELECT version_num FROM alembic_version)
         IN ('027_gateway_queue_capacity_wait_state', '028_workspace_sync_jobs'),
       0,
       (SELECT 1 UNION ALL SELECT 2)));   -- 非单行 027/028 -> 标量子查询多行 -> ERROR 1242 中止

-- ---- 028:workspace_sync_jobs 表（部分唯一索引用应用维护的 active_idempotency_key 普通列模拟)----
CREATE TABLE IF NOT EXISTS `workspace_sync_jobs` (
  `job_id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `idempotency_key` varchar(1024) COLLATE utf8mb4_unicode_ci NOT NULL,
  `active_idempotency_key` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `assistant_id` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `scope_kind` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `scope_slug` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `mode` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `status` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `priority` int NOT NULL,
  `requested_by` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `requested_from` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `scanned_count` int NOT NULL,
  `created_count` int NOT NULL,
  `updated_count` int NOT NULL,
  `missing_count` int NOT NULL,
  `deleted_count` int NOT NULL,
  `orphan_count` int NOT NULL,
  `error_code` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `error` text COLLATE utf8mb4_unicode_ci NOT NULL,
  `started_at` datetime DEFAULT NULL,
  `finished_at` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  PRIMARY KEY (`job_id`),
  UNIQUE KEY `uq_workspace_sync_jobs_active_idempotency` (`active_idempotency_key`),
  KEY `ix_workspace_sync_jobs_status_priority_created` (`status`,`priority`,`created_at`),
  KEY `ix_workspace_sync_jobs_assistant_status_created` (`assistant_id`,`status`,`created_at`),
  KEY `ix_workspace_sync_jobs_mode_status_created` (`mode`,`status`,`created_at`),
  KEY `ix_workspace_sync_jobs_scope_status_created` (`scope_kind`,`scope_slug`,`status`,`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- 更新 alembic_version 到 028(起点已由前置断言保证单行 027;028 重入时匹配 0 行、保持 028)----
UPDATE alembic_version
   SET version_num='028_workspace_sync_jobs'
 WHERE version_num='027_gateway_queue_capacity_wait_state';

-- ---- 末尾自检 ----
SELECT version_num AS final_version,
       IF(version_num='028_workspace_sync_jobs', 'OK', 'CHECK') AS gate
  FROM alembic_version;
