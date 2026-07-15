-- ============================================================================
-- IronAgent 数据库初始化 SQL(全新部署 / 运维手动执行)
-- 对应 alembic head : 029_workspace_file_long_path_columns
-- 数据库后端         : MySQL 8.x,字符集 utf8mb4 / utf8mb4_unicode_ci
-- 生成方式           : 空库执行 `alembic upgrade head` 后 mysqldump --no-data
--                      (offline `--sql` 因 repair/backfill 迁移含运行时逻辑不可用)
-- 适用场景           : 全新部署(空库)。已有数据的库请用 alembic 增量升级,见文末。
-- 用法               : 在已建好的 utf8mb4 空库上执行;或取消下方建库语句注释。
-- ============================================================================

-- CREATE DATABASE IF NOT EXISTS ironagent CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
-- USE ironagent;


/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `alembic_version` (
  `version_num` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  PRIMARY KEY (`version_num`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `approvals` (
  `id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `tool_name` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `assistant_id` varchar(191) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `capability_profile` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `approval_scope` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `reason` text COLLATE utf8mb4_unicode_ci,
  `target` text COLLATE utf8mb4_unicode_ci,
  `status` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `resolution` varchar(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `requested_at` datetime NOT NULL,
  `expires_at` datetime NOT NULL,
  `resolved_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_approvals_assistant_id` (`assistant_id`),
  KEY `ix_approvals_approval_scope` (`approval_scope`),
  KEY `ix_approvals_requested_at` (`requested_at`),
  KEY `ix_approvals_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `assistants` (
  `assistant_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `name` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `capability_profile` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `notes` text COLLATE utf8mb4_unicode_ci NOT NULL,
  `iron_agent_config` json DEFAULT NULL,
  `workspace_config` json NOT NULL,
  `runtime` json NOT NULL,
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`assistant_id`),
  KEY `ix_assistants_name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `audit_events` (
  `id` int NOT NULL AUTO_INCREMENT,
  `request_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `hook_name` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `assistant_id` varchar(191) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `capability_profile` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `tool_name` varchar(191) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `decision` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `success` tinyint(1) DEFAULT NULL,
  `occurred_at` datetime NOT NULL,
  `payload` json NOT NULL,
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `ix_audit_events_request_hook` (`request_id`,`hook_name`),
  KEY `ix_audit_events_assistant_id` (`assistant_id`),
  KEY `ix_audit_events_hook_name` (`hook_name`),
  KEY `ix_audit_events_occurred_at` (`occurred_at`),
  KEY `ix_audit_events_request_id` (`request_id`),
  KEY `ix_audit_events_tool_name` (`tool_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `collaboration_claims` (
  `id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `task_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `assistant_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `claim_token` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL,
  `claimed_at` datetime NOT NULL,
  `lease_expires_at` datetime NOT NULL,
  `status` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_collaboration_claims_claim_token` (`claim_token`),
  KEY `ix_collaboration_claims_task_id` (`task_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `collaboration_events` (
  `id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `task_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `event_type` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `actor_type` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `actor_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `detail_json` json NOT NULL,
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_collaboration_events_task_id` (`task_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `collaboration_tasks` (
  `id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `root_task_id` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `parent_task_id` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `depth` int NOT NULL,
  `max_depth` int NOT NULL,
  `source_assistant_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `target_assistant_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `input_object_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `instruction` text COLLATE utf8mb4_unicode_ci NOT NULL,
  `status` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `approval_mode` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `approval_id` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `result_object_id` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `result_workspace_file_id` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `idempotency_key` varchar(128) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `budget_limit` int DEFAULT NULL,
  `timeout_seconds` int NOT NULL,
  `created_by` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `created_at` datetime NOT NULL,
  `accepted_at` datetime DEFAULT NULL,
  `started_at` datetime DEFAULT NULL,
  `completed_at` datetime DEFAULT NULL,
  `failed_at` datetime DEFAULT NULL,
  `failed_reason_code` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `failed_reason_text` text COLLATE utf8mb4_unicode_ci,
  `meta_json` json NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_collaboration_tasks_idempotency_key` (`idempotency_key`),
  KEY `ix_collaboration_tasks_root_task_id` (`root_task_id`),
  KEY `ix_collaboration_tasks_source_assistant_id` (`source_assistant_id`),
  KEY `ix_collaboration_tasks_target_assistant_id` (`target_assistant_id`),
  KEY `ix_collaboration_tasks_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `external_push_requests` (
  `push_id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `user_id` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT '',
  `assistant_id` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `filename` varchar(1024) COLLATE utf8mb4_unicode_ci NOT NULL,
  `bucket` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `source_type` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT '',
  `source_ref` varchar(1024) COLLATE utf8mb4_unicode_ci DEFAULT '',
  `status` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL,
  `approval_id` varchar(36) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `error` text COLLATE utf8mb4_unicode_ci NOT NULL,
  `created_at` datetime NOT NULL,
  `content_type` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '',
  `staged_path` varchar(2048) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `storage_backend` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'local',
  `storage_key` text COLLATE utf8mb4_unicode_ci,
  `storage_url` text COLLATE utf8mb4_unicode_ci,
  PRIMARY KEY (`push_id`),
  KEY `ix_external_push_requests_assistant_id` (`assistant_id`),
  KEY `ix_external_push_requests_approval_id` (`approval_id`),
  KEY `ix_external_push_requests_assistant_created` (`assistant_id`,`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `gateway_conversation_routes` (
  `client_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `user_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `conversation_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `assistant_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  PRIMARY KEY (`client_id`,`user_id`,`conversation_id`),
  KEY `ix_gateway_conversation_routes_assistant_id` (`assistant_id`),
  KEY `ix_gateway_conversation_routes_updated_at` (`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `gateway_dead_letters` (
  `event_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `task_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `event_seq` bigint NOT NULL,
  `event_type` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL,
  `callback_url` varchar(1024) COLLATE utf8mb4_unicode_ci NOT NULL,
  `attempts` int NOT NULL,
  `last_error` text COLLATE utf8mb4_unicode_ci NOT NULL,
  `payload_json` json NOT NULL,
  `failed_at` datetime NOT NULL,
  PRIMARY KEY (`event_id`),
  KEY `ix_gateway_dead_letters_task_id` (`task_id`),
  KEY `ix_gateway_dead_letters_failed_at` (`failed_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `gateway_event_outbox` (
  `event_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `task_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `event_seq` bigint NOT NULL,
  `event_type` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL,
  `callback_url` varchar(1024) COLLATE utf8mb4_unicode_ci NOT NULL,
  `payload_json` json NOT NULL,
  `status` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `attempts` int NOT NULL DEFAULT '0',
  `last_error` text COLLATE utf8mb4_unicode_ci,
  `next_attempt_at` datetime DEFAULT NULL,
  `locked_by` varchar(191) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `locked_until` datetime DEFAULT NULL,
  `delivered_at` datetime DEFAULT NULL,
  `acked_at` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  PRIMARY KEY (`event_id`),
  KEY `ix_gateway_event_outbox_task_id` (`task_id`),
  KEY `ix_gateway_event_outbox_status` (`status`),
  KEY `ix_gateway_event_outbox_next_attempt_at` (`next_attempt_at`),
  KEY `ix_gateway_event_outbox_locked_until` (`locked_until`),
  KEY `ix_gateway_event_outbox_claim` (`status`,`next_attempt_at`,`created_at`,`event_seq`),
  KEY `ix_gateway_event_outbox_stuck` (`status`,`locked_until`),
  KEY `ix_gateway_event_outbox_claim_order` (`status`,`created_at`,`event_seq`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `gateway_event_sequences` (
  `task_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `last_event_seq` bigint NOT NULL,
  `updated_at` datetime NOT NULL,
  PRIMARY KEY (`task_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `gateway_hmac_nonces` (
  `client_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `nonce` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `expires_at` datetime NOT NULL,
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`client_id`,`nonce`),
  KEY `ix_gateway_hmac_nonces_expires_at` (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `gateway_idempotency_keys` (
  `client_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `idempotency_key` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `task_id` varchar(191) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `request_hash` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL,
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`client_id`,`idempotency_key`),
  KEY `ix_gateway_idempotency_keys_task_id` (`task_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `gateway_runtime_heartbeats` (
  `id` int NOT NULL AUTO_INCREMENT,
  `assistant_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `runtime_id` varchar(191) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `pod_name` varchar(191) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `status` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `capacity` int NOT NULL,
  `in_flight` int NOT NULL,
  `last_error` text COLLATE utf8mb4_unicode_ci,
  `received_at` datetime NOT NULL,
  `internal_base_url` varchar(1024) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `gateway_url` varchar(1024) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_gateway_runtime_heartbeats_assistant_id` (`assistant_id`),
  KEY `ix_gateway_runtime_heartbeats_received_at` (`received_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `gateway_runtime_instances` (
  `runtime_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `assistant_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `pod_name` varchar(191) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `internal_base_url` varchar(1024) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `gateway_url` varchar(1024) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `status` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `capacity` int NOT NULL,
  `in_flight` int NOT NULL DEFAULT '0',
  `capabilities_json` json DEFAULT NULL,
  `drain_mode` tinyint(1) NOT NULL DEFAULT '0',
  `last_heartbeat_at` datetime DEFAULT NULL,
  `last_error` text COLLATE utf8mb4_unicode_ci,
  `version` bigint NOT NULL DEFAULT '1',
  `updated_at` datetime NOT NULL,
  PRIMARY KEY (`runtime_id`),
  KEY `ix_gateway_runtime_instances_assistant_id` (`assistant_id`),
  KEY `ix_gateway_runtime_instances_status` (`status`),
  KEY `ix_gateway_runtime_instances_claim` (`assistant_id`,`status`,`drain_mode`,`in_flight`,`capacity`),
  KEY `ix_gateway_runtime_instances_last_heartbeat_at` (`last_heartbeat_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `gateway_runtime_leases` (
  `lease_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `runtime_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `assistant_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `status` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `acquired_at` datetime NOT NULL,
  `lease_expires_at` datetime NOT NULL,
  `released_at` datetime DEFAULT NULL,
  `updated_at` datetime NOT NULL,
  PRIMARY KEY (`lease_id`),
  KEY `ix_gateway_runtime_leases_runtime_id` (`runtime_id`),
  KEY `ix_gateway_runtime_leases_assistant_id` (`assistant_id`),
  KEY `ix_gateway_runtime_leases_status` (`status`),
  KEY `ix_gateway_runtime_leases_active_expiry` (`status`,`lease_expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `gateway_runtimes` (
  `runtime_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `assistant_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `pod_name` varchar(191) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `status` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `capacity` int NOT NULL,
  `in_flight` int NOT NULL DEFAULT '0',
  `capabilities_json` json DEFAULT NULL,
  `drain_mode` tinyint(1) NOT NULL DEFAULT '0',
  `last_heartbeat_at` datetime DEFAULT NULL,
  `last_error` text COLLATE utf8mb4_unicode_ci,
  `version` bigint NOT NULL DEFAULT '1',
  `updated_at` datetime NOT NULL,
  `internal_base_url` varchar(1024) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `gateway_url` varchar(1024) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  PRIMARY KEY (`assistant_id`),
  KEY `ix_gateway_runtimes_runtime_id` (`runtime_id`),
  KEY `ix_gateway_runtimes_status` (`status`),
  KEY `ix_gateway_runtimes_last_heartbeat_at` (`last_heartbeat_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `gateway_task_events` (
  `id` int NOT NULL AUTO_INCREMENT,
  `task_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `event_type` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL,
  `payload_json` json NOT NULL,
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_gateway_task_events_task_id` (`task_id`),
  KEY `ix_gateway_task_events_event_type` (`event_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `gateway_task_queue` (
  `task_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `request_json` json NOT NULL,
  `request_hash` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL,
  `status` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `attempts` int NOT NULL DEFAULT '0',
  `last_error` text COLLATE utf8mb4_unicode_ci,
  `next_attempt_at` datetime DEFAULT NULL,
  `locked_by` varchar(191) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `locked_until` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  `attempt_token` varchar(191) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `attempt_started_at` datetime DEFAULT NULL,
  `last_progress_at` datetime DEFAULT NULL,
  `last_progress_status` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `last_progress_stage` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `last_progress_detail` longtext COLLATE utf8mb4_unicode_ci,
  `terminal_reason_code` varchar(191) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `terminal_reason_text` longtext COLLATE utf8mb4_unicode_ci,
  `terminal_detail_json` json DEFAULT NULL,
  `capacity_wait_count` int NOT NULL DEFAULT '0',
  `capacity_wait_first_at` datetime DEFAULT NULL,
  `capacity_wait_last_at` datetime DEFAULT NULL,
  `capacity_wait_last_callback_at` datetime DEFAULT NULL,
  PRIMARY KEY (`task_id`),
  KEY `ix_gateway_task_queue_status` (`status`),
  KEY `ix_gateway_task_queue_next_attempt_at` (`next_attempt_at`),
  KEY `ix_gateway_task_queue_locked_until` (`locked_until`),
  KEY `ix_gateway_task_queue_created_at` (`created_at`),
  KEY `ix_gateway_task_queue_claim` (`status`,`next_attempt_at`,`created_at`),
  KEY `ix_gateway_task_queue_stuck` (`status`,`locked_until`),
  KEY `ix_gateway_task_queue_claimed` (`locked_by`,`status`,`created_at`),
  KEY `ix_gateway_task_queue_progress_stale` (`status`,`last_progress_at`),
  KEY `ix_gateway_task_queue_attempt_token` (`attempt_token`),
  KEY `ix_gateway_task_queue_terminal_reason` (`terminal_reason_code`),
  KEY `ix_gateway_task_queue_status_task` (`status`,`task_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `gateway_tasks` (
  `task_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `client_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `request_id` varchar(191) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `conversation_id` varchar(191) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `user_id` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `employee_no` varchar(191) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `source_assistant_id` varchar(191) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `target_assistant_id` varchar(191) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `southbound_task_id` varchar(191) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `approval_id` varchar(191) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `status` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `stage` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `fallback_query_url` varchar(512) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `callback_url` varchar(1024) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `completed_at` datetime DEFAULT NULL,
  `failed_reason_code` varchar(191) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `failed_reason_text` text COLLATE utf8mb4_unicode_ci,
  `metadata_json` json DEFAULT NULL,
  `task_json` json NOT NULL,
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  PRIMARY KEY (`task_id`),
  KEY `ix_gateway_tasks_client_id` (`client_id`),
  KEY `ix_gateway_tasks_status` (`status`),
  KEY `ix_gateway_tasks_approval_id` (`approval_id`),
  KEY `ix_gateway_tasks_southbound_task_id` (`southbound_task_id`),
  KEY `ix_gateway_tasks_updated_at` (`updated_at`),
  KEY `ix_gateway_tasks_target_assistant_updated` (`target_assistant_id`,`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `gateway_webhook_deliveries` (
  `id` int NOT NULL AUTO_INCREMENT,
  `event_id` varchar(191) COLLATE utf8mb4_unicode_ci NOT NULL,
  `callback_url` varchar(1024) COLLATE utf8mb4_unicode_ci NOT NULL,
  `attempt` int NOT NULL,
  `status` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `error` text COLLATE utf8mb4_unicode_ci,
  `created_at` datetime NOT NULL,
  `delivery_mode` varchar(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `request_packet` longtext COLLATE utf8mb4_unicode_ci,
  `response_observed` tinyint(1) DEFAULT NULL,
  `response_status_code` int DEFAULT NULL,
  `response_status` varchar(128) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `response_content_type` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `response_content_length` bigint DEFAULT NULL,
  `response_body` longtext COLLATE utf8mb4_unicode_ci,
  `response_body_bytes` int DEFAULT NULL,
  `response_body_read_error` longtext COLLATE utf8mb4_unicode_ci,
  `response_body_truncated` tinyint(1) DEFAULT NULL,
  `response_packet` longtext COLLATE utf8mb4_unicode_ci,
  `duration_ms` bigint DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_gateway_webhook_delivery_attempt` (`event_id`,`attempt`),
  KEY `ix_gateway_webhook_deliveries_event_id` (`event_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `shared_object_permissions` (
  `id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `object_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `grantee_type` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `grantee_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `permission_type` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `created_by` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `created_at` datetime NOT NULL,
  `expires_at` datetime DEFAULT NULL,
  `status` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_shared_object_permissions_object_id` (`object_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `shared_workspace_objects` (
  `id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `object_type` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `filename` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `content_type` varchar(128) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `size_bytes` int NOT NULL,
  `sha256` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `storage_backend` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `storage_bucket` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL,
  `storage_region` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `storage_endpoint` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `storage_key` varchar(1024) COLLATE utf8mb4_unicode_ci NOT NULL,
  `storage_etag` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL,
  `storage_version_id` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `source_assistant_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `source_workspace_file_id` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `parent_object_id` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `derived_from_task_id` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `visibility_scope` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `access_mode` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `retention_policy` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `expires_at` datetime DEFAULT NULL,
  `last_accessed_at` datetime DEFAULT NULL,
  `status` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `created_by` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `created_at` datetime NOT NULL,
  `deleted_at` datetime DEFAULT NULL,
  `idempotency_key` varchar(128) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `meta_json` json NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_shared_workspace_objects_idempotency_key` (`idempotency_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `user_assistant_bindings` (
  `user_id` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `assistant_id` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `alias` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT '',
  `source_system` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT '',
  `bound_at` datetime NOT NULL,
  PRIMARY KEY (`user_id`,`assistant_id`),
  KEY `ix_user_assistant_bindings_assistant_id` (`assistant_id`),
  CONSTRAINT `fk_user_assistant_bindings_user_id_users` FOREIGN KEY (`user_id`) REFERENCES `users` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `users` (
  `user_id` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `display_name` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT '',
  `employee_id` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT '',
  `department` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT '',
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  `role` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'user',
  PRIMARY KEY (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `workspace_file_events` (
  `event_id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `file_id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `assistant_id` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `action` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL,
  `user_id` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT '',
  `source_type` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT '',
  `source_ref` varchar(1024) COLLATE utf8mb4_unicode_ci DEFAULT '',
  `detail` json NOT NULL,
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`event_id`),
  KEY `ix_workspace_file_events_file_id` (`file_id`),
  KEY `ix_workspace_file_events_assistant_id` (`assistant_id`),
  KEY `ix_workspace_file_events_assistant_created` (`assistant_id`,`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `workspace_files` (
  `file_id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `assistant_id` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `bucket` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL,
  `filename` text COLLATE utf8mb4_unicode_ci NOT NULL,
  `relative_path` text COLLATE utf8mb4_unicode_ci NOT NULL,
  `size_bytes` bigint DEFAULT '0',
  `content_type` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT '',
  `uploader_user_id` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT '',
  `source_type` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT '',
  `source_ref` text COLLATE utf8mb4_unicode_ci NOT NULL,
  `created_at` datetime NOT NULL,
  `updated_at` datetime NOT NULL,
  `deleted` tinyint(1) DEFAULT '0',
  `file_role` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'input',
  `source_file_id` varchar(36) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `storage_status` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'pending',
  `storage_error` text COLLATE utf8mb4_unicode_ci NOT NULL,
  `direction` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'inbound',
  `storage_backend` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'local',
  `object_key` text COLLATE utf8mb4_unicode_ci NOT NULL,
  `object_url` text COLLATE utf8mb4_unicode_ci NOT NULL,
  `checksum` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '',
  `sync_status` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'synced_to_workspace',
  `last_error` text COLLATE utf8mb4_unicode_ci NOT NULL,
  `storage_key` text COLLATE utf8mb4_unicode_ci,
  `storage_url` text COLLATE utf8mb4_unicode_ci,
  `origin` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'manual_import',
  `etag` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '',
  `workspace_path` text COLLATE utf8mb4_unicode_ci,
  `last_synced_at` datetime DEFAULT NULL,
  `scan_fingerprint` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '',
  `storage_provider` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'local',
  `storage_path` text COLLATE utf8mb4_unicode_ci NOT NULL,
  `checksum_sha256` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '',
  `sync_error` text COLLATE utf8mb4_unicode_ci NOT NULL,
  `workspace_last_modified_at` datetime DEFAULT NULL,
  `owner_user_id` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '',
  `active_path_key` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  PRIMARY KEY (`file_id`),
  UNIQUE KEY `uq_workspace_files_active_path` (`active_path_key`),
  KEY `ix_workspace_files_assistant_id` (`assistant_id`),
  KEY `ix_workspace_files_source_file_id` (`source_file_id`),
  KEY `ix_workspace_files_storage_status` (`storage_status`),
  KEY `ix_workspace_files_assistant_bucket` (`assistant_id`,`bucket`),
  KEY `ix_workspace_files_assistant_bucket_deleted` (`assistant_id`,`bucket`,`deleted`),
  KEY `ix_workspace_files_owner_user_id` (`owner_user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
CREATE TABLE `workspace_sync_jobs` (
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
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;


-- 标记 schema 版本:写入 alembic_version,使该库与 alembic 一致,
-- 避免日后 `alembic upgrade` 把已建表的迁移重跑而报错。
INSERT INTO alembic_version (version_num) VALUES ('029_workspace_file_long_path_columns');
