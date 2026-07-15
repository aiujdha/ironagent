-- ============================================================================
-- IronAgent 增量升级 SQL:028 -> 029
-- 适用:线上 alembic_version = 028_workspace_sync_jobs 的已有库
-- 后端:MySQL 8.x,字符集 utf8mb4 / utf8mb4_unicode_ci
-- 内容:029 放宽 workspace_files 的长路径/对象引用列到 TEXT
-- 背景:SQLite 历史元数据可能包含超过旧 varchar(1024/2048/4096) 的深层路径,
--       migrate_workspace_files_to_mysql 会因 MySQL 1406 Data too long 中断。
-- 幂等:ALTER TABLE MODIFY 可重复执行;alembic_version 按 WHERE 精确更新
-- 在线影响:ALTER TABLE workspace_files 会触碰既有表;建议低峰执行并先备份。
-- 执行:必须用 `mysql -uroot -p ironagent < 本文件`(批处理遇错即停),勿用交互 source
-- ============================================================================

SET NAMES utf8mb4;

-- ---- 前置断言:起点必须是单行 028(或已是 029 的重入),否则中止 ----
DO (SELECT IF(
       (SELECT COUNT(*) FROM alembic_version) = 1
   AND (SELECT version_num FROM alembic_version)
         IN ('028_workspace_sync_jobs', '029_workspace_file_long_path_columns'),
       0,
       (SELECT 1 UNION ALL SELECT 2)));   -- 非单行 028/029 -> 标量子查询多行 -> ERROR 1242 中止

-- ---- 029:放宽 workspace_files 长路径元数据列 ----
UPDATE workspace_files SET source_ref = '' WHERE source_ref IS NULL;
UPDATE workspace_files SET storage_path = '' WHERE storage_path IS NULL;
UPDATE workspace_files SET object_key = '' WHERE object_key IS NULL;
UPDATE workspace_files SET object_url = '' WHERE object_url IS NULL;

ALTER TABLE workspace_files
  MODIFY filename TEXT NOT NULL,
  MODIFY relative_path TEXT NOT NULL,
  MODIFY source_ref TEXT NOT NULL,
  MODIFY storage_key TEXT NULL,
  MODIFY storage_path TEXT NOT NULL,
  MODIFY object_key TEXT NOT NULL,
  MODIFY object_url TEXT NOT NULL;

-- ---- 更新 alembic_version 到 029(028 起点更新;029 重入时匹配 0 行、保持 029)----
UPDATE alembic_version
   SET version_num='029_workspace_file_long_path_columns'
 WHERE version_num='028_workspace_sync_jobs';

-- ---- 末尾自检 ----
SELECT version_num AS final_version,
       IF(version_num='029_workspace_file_long_path_columns', 'OK', 'CHECK') AS gate
  FROM alembic_version;
