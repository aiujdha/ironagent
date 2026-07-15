-- ============================================================================
-- IronAgent 增量升级 SQL:025 -> 027
-- 适用:线上 alembic_version = 025_workspace_files_active_path_key 的已有库
-- 后端:MySQL 8.x
-- 内容:026(gateway 队列背压索引) + 027(gateway_task_queue capacity-wait 列)
--       纯 DDL,无数据回填;对已有行,新列按 DEFAULT/NULL 自动填充
-- 幂等:每步先查 information_schema,已存在则跳过(可安全重复执行)
-- 来源:空库 upgrade 到 025 后开 general_log 捕获 025->head 实际执行的 DDL
-- 在线影响:CREATE INDEX / ADD COLUMN 在 InnoDB 默认走 online DDL(INPLACE),
--           不阻塞读写;大表仍建议低峰执行。
-- ============================================================================

-- 执行前确认当前版本(预期 025):
--   SELECT version_num FROM alembic_version;

-- ---- 026:gateway 队列背压索引 ----
SET @s := (SELECT COUNT(*) FROM information_schema.statistics
           WHERE table_schema=DATABASE() AND table_name='gateway_tasks'
             AND index_name='ix_gateway_tasks_target_assistant_updated');
SET @q := IF(@s=0,
  'CREATE INDEX ix_gateway_tasks_target_assistant_updated ON gateway_tasks (target_assistant_id, updated_at)',
  'DO 0');
PREPARE st FROM @q; EXECUTE st; DEALLOCATE PREPARE st;

SET @s := (SELECT COUNT(*) FROM information_schema.statistics
           WHERE table_schema=DATABASE() AND table_name='gateway_task_queue'
             AND index_name='ix_gateway_task_queue_status_task');
SET @q := IF(@s=0,
  'CREATE INDEX ix_gateway_task_queue_status_task ON gateway_task_queue (status, task_id)',
  'DO 0');
PREPARE st FROM @q; EXECUTE st; DEALLOCATE PREPARE st;

-- ---- 027:gateway_task_queue capacity-wait 状态列 ----
SET @s := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='gateway_task_queue'
             AND column_name='capacity_wait_count');
SET @q := IF(@s=0,
  'ALTER TABLE gateway_task_queue ADD COLUMN capacity_wait_count INTEGER NOT NULL DEFAULT 0',
  'DO 0');
PREPARE st FROM @q; EXECUTE st; DEALLOCATE PREPARE st;

SET @s := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='gateway_task_queue'
             AND column_name='capacity_wait_first_at');
SET @q := IF(@s=0,
  'ALTER TABLE gateway_task_queue ADD COLUMN capacity_wait_first_at DATETIME NULL',
  'DO 0');
PREPARE st FROM @q; EXECUTE st; DEALLOCATE PREPARE st;

SET @s := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='gateway_task_queue'
             AND column_name='capacity_wait_last_at');
SET @q := IF(@s=0,
  'ALTER TABLE gateway_task_queue ADD COLUMN capacity_wait_last_at DATETIME NULL',
  'DO 0');
PREPARE st FROM @q; EXECUTE st; DEALLOCATE PREPARE st;

SET @s := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='gateway_task_queue'
             AND column_name='capacity_wait_last_callback_at');
SET @q := IF(@s=0,
  'ALTER TABLE gateway_task_queue ADD COLUMN capacity_wait_last_callback_at DATETIME NULL',
  'DO 0');
PREPARE st FROM @q; EXECUTE st; DEALLOCATE PREPARE st;

-- ---- 更新 alembic_version 到 027(兼容当前为 025 或 026 的库)----
UPDATE alembic_version
   SET version_num='027_gateway_queue_capacity_wait_state'
 WHERE version_num IN ('025_workspace_files_active_path_key',
                       '026_gateway_assistant_queue_backpressure_indexes');
