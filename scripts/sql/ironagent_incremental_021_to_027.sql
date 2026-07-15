-- ============================================================================
-- IronAgent 增量升级 SQL:021 -> 027(生产加固版,经上线 review)
-- 适用:线上 alembic_version = 021_repair_legacy_assistant_ironagent_config 的已有库(线上当前 021)
-- 区间:022(collaboration 5 表,纯 DDL)+ 023~027(含 3 处数据回填,已加固)
-- 后端:MySQL 8.0+(用到 ROW_NUMBER 窗口函数;低于 8.0 会被前置断言拦截)
--
-- 【相对初版的加固(基于多维 review + 真实 MySQL 实测)】
-- 1. 顶部 SET NAMES utf8mb4 + 两道前置断言(MySQL 8.0+;起点必须是单行 021~027 之一),
--    不满足立即以 ERROR 1242 中止,避免在错误环境/起点上做半迁移。
-- 2. 023 回填改 ROW_NUMBER 去重:gateway_runtimes 多行规整后映射到同一 runtime_id 时
--    (多 assistant 复用同一 runtime pod、runtime_id 为空)不再撞主键 1062;保留 updated_at 最新一行。
-- 3. 025 去重与 active_path_key 统一用"归一化口径":REGEXP_REPLACE 去首尾所有空白(≡ Python strip,
--    含 tab/换行/NBSP/全角空格)+ 反斜杠转斜杠 + 去前导斜杠。凡归一化后会算出相同 key 的活跃行
--    都归同一组去重,彻底消除"原始 filename 不同但归一化后撞 key 导致 CREATE UNIQUE INDEX 1062"的上线失败。
-- 4. active_path_key 的归一化口径同时对齐运行时 database_models.py 的 Python 计算,避免 key 漂移。
--
-- 【与 alembic 的已知偏差(已评审接受)】
-- - 023/025 去重在"脏数据"(空白/斜杠/反斜杠差异)上比 alembic 更稳:alembic 按原始值分组会在建
--   唯一索引时 1062 失败;本 SQL 按归一化分组多去重一行让索引建得起来。干净数据上两者逐行一致。
-- - 023 保留行用 updated_at DESC 确定性选择(alembic 是不确定的物理顺序"第一行")。
-- - alembic 025/023 源码本身存在同样的口径不一致问题,建议另行修复迁移源码。
--
-- 【上线前必须做】见 README"上线前预检"清单,逐条在生产库跑预检 SQL;含数据迁移,务必先备份。
-- 幂等:可安全重复执行(中途失败原地重跑亦可)。
-- 【执行方式】务必用  mysql -u.. -p.. <库名> < 本文件  (批处理模式默认"遇错即停",
--   前置断言/任一步报错才能拦住、不致继续半迁移);切勿在交互式 mysql 里用 source 执行
--   (交互模式默认"遇错继续",断言会失效)。
-- ============================================================================

SET NAMES utf8mb4;

-- ---- 前置断言 0:MySQL 必须 >= 8.0(否则下方 ROW_NUMBER 会半途报错、留下半迁移)----
DO (SELECT IF(CAST(SUBSTRING_INDEX(VERSION(), '.', 1) AS UNSIGNED) >= 8,
              0,
              (SELECT 1 UNION ALL SELECT 2)));   -- < 8.0 -> 标量子查询多行 -> ERROR 1242 中止

-- ---- 前置断言 1:起点必须是单行 021~027(线上 021;支持任一中间步骤重入),否则中止 ----
DO (SELECT IF(
       (SELECT COUNT(*) FROM alembic_version) = 1
   AND (SELECT version_num FROM alembic_version) IN (
         '021_repair_legacy_assistant_ironagent_config',
         '022_collaboration_repository_tables',
         '023_gateway_runtime_instances',
         '024_gateway_conversation_routes',
         '025_workspace_files_active_path_key',
         '026_gateway_assistant_queue_backpressure_indexes',
         '027_gateway_queue_capacity_wait_state'),
       0,
       (SELECT 1 UNION ALL SELECT 2)));          -- 非单行 021~027 -> ERROR 1242 中止

-- ========================== 022:collaboration 仓储表(021 -> 022,纯 DDL)==========================
CREATE TABLE IF NOT EXISTS shared_workspace_objects (
  id                       VARCHAR(64)   NOT NULL,
  object_type              VARCHAR(32)   NOT NULL,
  filename                 VARCHAR(255)  NOT NULL,
  content_type             VARCHAR(128),
  size_bytes               INTEGER       NOT NULL,
  sha256                   VARCHAR(64)   NOT NULL,
  storage_backend          VARCHAR(32)   NOT NULL,
  storage_bucket           VARCHAR(128)  NOT NULL,
  storage_region           VARCHAR(64)   NOT NULL,
  storage_endpoint         VARCHAR(255)  NOT NULL,
  storage_key              VARCHAR(1024) NOT NULL,
  storage_etag             VARCHAR(128)  NOT NULL,
  storage_version_id       VARCHAR(255)  NOT NULL,
  source_assistant_id      VARCHAR(64)   NOT NULL,
  source_workspace_file_id VARCHAR(64),
  parent_object_id         VARCHAR(64),
  derived_from_task_id     VARCHAR(64),
  visibility_scope         VARCHAR(32)   NOT NULL,
  access_mode              VARCHAR(32)   NOT NULL,
  retention_policy         VARCHAR(32)   NOT NULL,
  expires_at               DATETIME,
  last_accessed_at         DATETIME,
  status                   VARCHAR(32)   NOT NULL,
  created_by               VARCHAR(64)   NOT NULL,
  created_at               DATETIME      NOT NULL,
  deleted_at               DATETIME,
  idempotency_key          VARCHAR(128),
  meta_json                JSON          NOT NULL,
  CONSTRAINT pk_shared_workspace_objects PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS shared_object_permissions (
  id              VARCHAR(64) NOT NULL,
  object_id       VARCHAR(64) NOT NULL,
  grantee_type    VARCHAR(32) NOT NULL,
  grantee_id      VARCHAR(64) NOT NULL,
  permission_type VARCHAR(32) NOT NULL,
  created_by      VARCHAR(64) NOT NULL,
  created_at      DATETIME    NOT NULL,
  expires_at      DATETIME,
  status          VARCHAR(32) NOT NULL,
  CONSTRAINT pk_shared_object_permissions PRIMARY KEY (id),
  KEY ix_shared_object_permissions_object_id (object_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS collaboration_tasks (
  id                       VARCHAR(64)  NOT NULL,
  root_task_id             VARCHAR(64),
  parent_task_id           VARCHAR(64),
  depth                    INTEGER      NOT NULL,
  max_depth                INTEGER      NOT NULL,
  source_assistant_id      VARCHAR(64)  NOT NULL,
  target_assistant_id      VARCHAR(64)  NOT NULL,
  input_object_id          VARCHAR(64)  NOT NULL,
  instruction              TEXT         NOT NULL,
  status                   VARCHAR(32)  NOT NULL,
  approval_mode            VARCHAR(32)  NOT NULL,
  approval_id              VARCHAR(64),
  result_object_id         VARCHAR(64),
  result_workspace_file_id VARCHAR(64),
  idempotency_key          VARCHAR(128),
  budget_limit             INTEGER,
  timeout_seconds          INTEGER      NOT NULL,
  created_by               VARCHAR(64)  NOT NULL,
  created_at               DATETIME     NOT NULL,
  accepted_at              DATETIME,
  started_at               DATETIME,
  completed_at             DATETIME,
  failed_at                DATETIME,
  failed_reason_code       VARCHAR(64),
  failed_reason_text       TEXT,
  meta_json                JSON         NOT NULL,
  CONSTRAINT pk_collaboration_tasks PRIMARY KEY (id),
  KEY ix_collaboration_tasks_root_task_id (root_task_id),
  KEY ix_collaboration_tasks_source_assistant_id (source_assistant_id),
  KEY ix_collaboration_tasks_target_assistant_id (target_assistant_id),
  KEY ix_collaboration_tasks_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS collaboration_claims (
  id               VARCHAR(64)  NOT NULL,
  task_id          VARCHAR(64)  NOT NULL,
  assistant_id     VARCHAR(64)  NOT NULL,
  claim_token      VARCHAR(128) NOT NULL,
  claimed_at       DATETIME     NOT NULL,
  lease_expires_at DATETIME     NOT NULL,
  status           VARCHAR(32)  NOT NULL,
  CONSTRAINT pk_collaboration_claims PRIMARY KEY (id),
  KEY ix_collaboration_claims_task_id (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS collaboration_events (
  id          VARCHAR(64) NOT NULL,
  task_id     VARCHAR(64) NOT NULL,
  event_type  VARCHAR(64) NOT NULL,
  actor_type  VARCHAR(32) NOT NULL,
  actor_id    VARCHAR(64) NOT NULL,
  detail_json JSON        NOT NULL,
  created_at  DATETIME    NOT NULL,
  CONSTRAINT pk_collaboration_events PRIMARY KEY (id),
  KEY ix_collaboration_events_task_id (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ========================== 023:gateway runtime 实例 / 租约 ==========================
CREATE TABLE IF NOT EXISTS gateway_runtime_instances (
  runtime_id        VARCHAR(191)  NOT NULL,
  assistant_id      VARCHAR(191)  NOT NULL,
  pod_name          VARCHAR(191),
  internal_base_url VARCHAR(1024),
  gateway_url       VARCHAR(1024),
  status            VARCHAR(64)   NOT NULL,
  capacity          INTEGER       NOT NULL,
  in_flight         INTEGER       NOT NULL DEFAULT '0',
  capabilities_json JSON,
  drain_mode        BOOL          NOT NULL DEFAULT false,
  last_heartbeat_at DATETIME,
  last_error        TEXT,
  version           BIGINT        NOT NULL DEFAULT '1',
  updated_at        DATETIME      NOT NULL,
  CONSTRAINT pk_gateway_runtime_instances PRIMARY KEY (runtime_id),
  KEY ix_gateway_runtime_instances_assistant_id (assistant_id),
  KEY ix_gateway_runtime_instances_status (status),
  KEY ix_gateway_runtime_instances_claim (assistant_id, status, drain_mode, in_flight, capacity),
  KEY ix_gateway_runtime_instances_last_heartbeat_at (last_heartbeat_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS gateway_runtime_leases (
  lease_id         VARCHAR(191) NOT NULL,
  runtime_id       VARCHAR(191) NOT NULL,
  assistant_id     VARCHAR(191) NOT NULL,
  status           VARCHAR(32)  NOT NULL,
  acquired_at      DATETIME     NOT NULL,
  lease_expires_at DATETIME     NOT NULL,
  released_at      DATETIME,
  updated_at       DATETIME     NOT NULL,
  CONSTRAINT pk_gateway_runtime_leases PRIMARY KEY (lease_id),
  KEY ix_gateway_runtime_leases_runtime_id (runtime_id),
  KEY ix_gateway_runtime_leases_assistant_id (assistant_id),
  KEY ix_gateway_runtime_leases_status (status),
  KEY ix_gateway_runtime_leases_active_expiry (status, lease_expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 023 回填:gateway_runtimes -> gateway_runtime_instances
-- runtime_id 规整 = strip(runtime_id) 非空则取之,否则 strip(assistant_id);assistant_id 写入 strip 后的值。
-- ROW_NUMBER 去重:同一规整 runtime_id 只插 updated_at 最新一行,避免单条 INSERT...SELECT 撞主键。
INSERT INTO gateway_runtime_instances
  (runtime_id, assistant_id, pod_name, internal_base_url, gateway_url,
   status, capacity, in_flight, capabilities_json, drain_mode,
   last_heartbeat_at, last_error, version, updated_at)
SELECT eff_runtime_id, eff_assistant_id, pod_name, internal_base_url, gateway_url,
       status, capacity, in_flight, capabilities_json, drain_mode,
       last_heartbeat_at, last_error, version, updated_at
FROM (
  SELECT
    COALESCE(NULLIF(REGEXP_REPLACE(COALESCE(runtime_id, ''),   '^[[:space:]]+|[[:space:]]+$', ''), ''),
                    REGEXP_REPLACE(COALESCE(assistant_id, ''), '^[[:space:]]+|[[:space:]]+$', '')) AS eff_runtime_id,
    REGEXP_REPLACE(COALESCE(assistant_id, ''), '^[[:space:]]+|[[:space:]]+$', '')                  AS eff_assistant_id,
    pod_name, internal_base_url, gateway_url, status, capacity, in_flight,
    capabilities_json, drain_mode, last_heartbeat_at, last_error, version, updated_at,
    ROW_NUMBER() OVER (
      PARTITION BY COALESCE(NULLIF(REGEXP_REPLACE(COALESCE(runtime_id, ''),   '^[[:space:]]+|[[:space:]]+$', ''), ''),
                                   REGEXP_REPLACE(COALESCE(assistant_id, ''), '^[[:space:]]+|[[:space:]]+$', ''))
      ORDER BY updated_at DESC, assistant_id
    ) AS rn
  FROM gateway_runtimes
  WHERE REGEXP_REPLACE(COALESCE(assistant_id, ''), '^[[:space:]]+|[[:space:]]+$', '') <> ''
) ranked
WHERE ranked.rn = 1
  AND NOT EXISTS (
    SELECT 1 FROM gateway_runtime_instances i WHERE i.runtime_id = ranked.eff_runtime_id
  );

-- ========================== 024:gateway 会话路由 ==========================
CREATE TABLE IF NOT EXISTS gateway_conversation_routes (
  client_id       VARCHAR(191) NOT NULL,
  user_id         VARCHAR(191) NOT NULL,
  conversation_id VARCHAR(191) NOT NULL,
  assistant_id    VARCHAR(191) NOT NULL,
  created_at      DATETIME     NOT NULL,
  updated_at      DATETIME     NOT NULL,
  CONSTRAINT pk_gateway_conversation_routes PRIMARY KEY (client_id, user_id, conversation_id),
  KEY ix_gateway_conversation_routes_assistant_id (assistant_id),
  KEY ix_gateway_conversation_routes_updated_at (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ========================== 025:workspace_files active_path_key ==========================
-- (1) 加列
SET @s := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='workspace_files'
             AND column_name='active_path_key');
SET @q := IF(@s=0, 'ALTER TABLE workspace_files ADD COLUMN active_path_key VARCHAR(64) NULL', 'DO 0');
PREPARE st FROM @q; EXECUTE st; DEALLOCATE PREPARE st;

-- (2) 去重:按【归一化路径】分组(strip 所有空白 + 反斜杠转斜杠 + 去前导斜杠),
--     凡归一化后会算出同一 active_path_key 的活跃行归同组,保留 updated_at 最新,其余标 deleted。
UPDATE workspace_files wf
JOIN (
  SELECT file_id,
    ROW_NUMBER() OVER (
      PARTITION BY
        REGEXP_REPLACE(COALESCE(assistant_id, ''), '^[[:space:]]+|[[:space:]]+$', ''),
        REGEXP_REPLACE(COALESCE(bucket, ''),       '^[[:space:]]+|[[:space:]]+$', ''),
        TRIM(LEADING '/' FROM REGEXP_REPLACE(REPLACE(COALESCE(filename, ''), '\\', '/'),
                                             '^[[:space:]]+|[[:space:]]+$', ''))
      ORDER BY updated_at DESC, file_id DESC
    ) AS rn
  FROM workspace_files
  WHERE COALESCE(deleted, 0) = 0
) ranked ON ranked.file_id = wf.file_id AND ranked.rn > 1
SET
  wf.deleted         = 1,
  wf.active_path_key = NULL,
  wf.sync_status     = 'deleted_from_workspace',
  wf.storage_status  = CASE WHEN COALESCE(wf.storage_status, '') IN ('', 'pending')
                            THEN 'deleted_from_workspace' ELSE wf.storage_status END,
  wf.last_error      = CASE WHEN COALESCE(wf.last_error, '') = ''
                            THEN 'duplicate active workspace file row superseded during active path migration'
                            ELSE wf.last_error END,
  wf.updated_at      = UTC_TIMESTAMP();

-- (3) 回填 active_path_key:与去重同口径归一化;deleted 或归一化后任一为空 -> NULL。
UPDATE workspace_files
SET active_path_key = CASE
  WHEN COALESCE(deleted, 0) <> 0 THEN NULL
  WHEN REGEXP_REPLACE(COALESCE(assistant_id, ''), '^[[:space:]]+|[[:space:]]+$', '') = ''
    OR REGEXP_REPLACE(COALESCE(bucket, ''),       '^[[:space:]]+|[[:space:]]+$', '') = ''
    OR TRIM(LEADING '/' FROM REGEXP_REPLACE(REPLACE(COALESCE(filename, ''), '\\', '/'),
                                            '^[[:space:]]+|[[:space:]]+$', '')) = '' THEN NULL
  ELSE SHA2(CONCAT_WS(CHAR(0),
        REGEXP_REPLACE(COALESCE(assistant_id, ''), '^[[:space:]]+|[[:space:]]+$', ''),
        REGEXP_REPLACE(COALESCE(bucket, ''),       '^[[:space:]]+|[[:space:]]+$', ''),
        TRIM(LEADING '/' FROM REGEXP_REPLACE(REPLACE(COALESCE(filename, ''), '\\', '/'),
                                             '^[[:space:]]+|[[:space:]]+$', ''))
      ), 256)
END;

-- (4) 唯一索引(去重 + 回填后才能安全建立)
SET @s := (SELECT COUNT(*) FROM information_schema.statistics
           WHERE table_schema=DATABASE() AND table_name='workspace_files'
             AND index_name='uq_workspace_files_active_path');
SET @q := IF(@s=0, 'CREATE UNIQUE INDEX uq_workspace_files_active_path ON workspace_files (active_path_key)', 'DO 0');
PREPARE st FROM @q; EXECUTE st; DEALLOCATE PREPARE st;

-- ========================== 026:gateway 队列背压索引 ==========================
SET @s := (SELECT COUNT(*) FROM information_schema.statistics
           WHERE table_schema=DATABASE() AND table_name='gateway_tasks'
             AND index_name='ix_gateway_tasks_target_assistant_updated');
SET @q := IF(@s=0, 'CREATE INDEX ix_gateway_tasks_target_assistant_updated ON gateway_tasks (target_assistant_id, updated_at)', 'DO 0');
PREPARE st FROM @q; EXECUTE st; DEALLOCATE PREPARE st;

SET @s := (SELECT COUNT(*) FROM information_schema.statistics
           WHERE table_schema=DATABASE() AND table_name='gateway_task_queue'
             AND index_name='ix_gateway_task_queue_status_task');
SET @q := IF(@s=0, 'CREATE INDEX ix_gateway_task_queue_status_task ON gateway_task_queue (status, task_id)', 'DO 0');
PREPARE st FROM @q; EXECUTE st; DEALLOCATE PREPARE st;

-- ========================== 027:gateway_task_queue capacity-wait 列 ==========================
SET @s := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='gateway_task_queue'
             AND column_name='capacity_wait_count');
SET @q := IF(@s=0, 'ALTER TABLE gateway_task_queue ADD COLUMN capacity_wait_count INTEGER NOT NULL DEFAULT 0', 'DO 0');
PREPARE st FROM @q; EXECUTE st; DEALLOCATE PREPARE st;

SET @s := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='gateway_task_queue'
             AND column_name='capacity_wait_first_at');
SET @q := IF(@s=0, 'ALTER TABLE gateway_task_queue ADD COLUMN capacity_wait_first_at DATETIME NULL', 'DO 0');
PREPARE st FROM @q; EXECUTE st; DEALLOCATE PREPARE st;

SET @s := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='gateway_task_queue'
             AND column_name='capacity_wait_last_at');
SET @q := IF(@s=0, 'ALTER TABLE gateway_task_queue ADD COLUMN capacity_wait_last_at DATETIME NULL', 'DO 0');
PREPARE st FROM @q; EXECUTE st; DEALLOCATE PREPARE st;

SET @s := (SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=DATABASE() AND table_name='gateway_task_queue'
             AND column_name='capacity_wait_last_callback_at');
SET @q := IF(@s=0, 'ALTER TABLE gateway_task_queue ADD COLUMN capacity_wait_last_callback_at DATETIME NULL', 'DO 0');
PREPARE st FROM @q; EXECUTE st; DEALLOCATE PREPARE st;

-- ========================== 更新 alembic_version -> 027 + 收尾校验 ==========================
-- 起点已由前置断言保证是单行 021~027,把任一中间起点更新到 027(已是 027 时匹配 0 行、保持 027)。
UPDATE alembic_version SET version_num='027_gateway_queue_capacity_wait_state'
 WHERE version_num IN ('021_repair_legacy_assistant_ironagent_config',
                       '022_collaboration_repository_tables',
                       '023_gateway_runtime_instances',
                       '024_gateway_conversation_routes',
                       '025_workspace_files_active_path_key',
                       '026_gateway_assistant_queue_backpressure_indexes');

-- 收尾校验:输出应为 1 行 027 / OK。运维务必肉眼确认。
SELECT version_num AS final_version,
       IF((SELECT COUNT(*) FROM alembic_version)=1
          AND version_num='027_gateway_queue_capacity_wait_state', 'OK', 'ABORT: 检查版本/行数') AS gate
FROM alembic_version;
