package config

import (
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestLoadFromConfigFile(t *testing.T) {
	cleanGatewayEnv(t)
	path := filepath.Join(t.TempDir(), "go-gateway.json")
	writeConfigFile(t, path, `{
		"listen_addr": ":18080",
		"mode": "polling",
		"process_role": "task-worker",
		"security_base_url": "http://security.local/",
		"admin_base_url": "http://admin.local/",
		"internal_service_token": "file-internal-service-token",
		"database_url": "mysql+pymysql://ironagent:ironagent@mysql:3306/ironagent?charset=utf8mb4",
		"database_max_open_conns": 30,
		"database_max_idle_conns": 8,
		"database_conn_max_lifetime": "20m",
		"database_conn_max_idle_time": "4m",
		"callback_database_max_open_conns": 7,
		"callback_database_max_idle_conns": 3,
		"redis_mode": "sentinel",
		"redis_addr": "redis:6379",
		"redis_sentinel_addrs": ["redis-sentinel:26379", " "],
		"redis_sentinel_master": "mymaster",
		"redis_username": "file-redis-user",
		"redis_password": "file-redis-password",
		"redis_db": 1,
		"redis_key_prefix": "file-gateway:",
		"redis_timeout": "300ms",
		"redis_max_open_conns": 12,
		"redis_max_idle_conns": 4,
		"redis_required": true,
		"assistant_cache_ttl": "45s",
		"assistant_availability_cache_ttl": "15s",
		"chat_rate_limit_enabled": true,
		"chat_rate_limit_max": 700,
		"chat_rate_limit_window": "2s",
		"chat_rate_limit_fail_closed": false,
		"api_keys": ["file-key", " "],
		"scoped_api_keys": [
			{"key": "public-file-key", "scopes": ["public", " "]},
			{"key": "debug-file-key", "scopes": ["debug"]}
		],
		"hmac_secret": "file-hmac",
		"hmac_allowed_skew": "30s",
		"public_auth_disabled": true,
		"admin_auth_disabled": true,
		"http_timeout": "2s",
		"public_base_url": "http://gateway.local/",
		"default_callback_url": "https://xiaoxiongmao.local/ironagent/callback",
		"callback_client_id": "file-client",
		"callback_secret": "file-callback-secret",
		"callback_http_timeout": "7s",
		"callback_allowed_hosts": ["xiaoxiongmao.local", ""],
			"callback_allow_private_hosts": true,
			"callback_fire_and_forget": true,
			"callback_accept_any_response": true,
			"callback_required": true,
			"callback_worker_id": "worker-a",
		"callback_max_attempts": 5,
		"callback_retry_delay": "100ms",
		"callback_max_retry_delay": "3s",
		"callback_queue_size": 2048,
		"callback_store_timeout": "750ms",
		"callback_enqueue_max_in_flight": 6,
		"callback_delivery_concurrency": 3,
		"callback_worker_interval": "150ms",
		"callback_pending_limit": 25,
		"callback_lease_duration": "45s",
		"callback_outbox_worker_enabled": false,
		"presign_secret": "file-presign",
		"presign_ttl": "10m",
		"max_upload_bytes": 12345,
		"proxy_session_secret": "file-proxy-session",
		"proxy_session_ttl": "20m",
		"proxy_max_in_flight": 123,
		"proxy_failure_threshold": 2,
		"proxy_circuit_open_duration": "5s",
		"runtime_default_capacity": 7,
		"runtime_heartbeat_ttl": "20s",
		"runtime_bootstrap_assistants": ["demo-main", " "],
			"runtime_discover_assistants": true,
			"runtime_sync_interval": "5s",
			"runtime_execution_enabled": true,
			"runtime_execution_timeout": "45m",
			"task_worker_enabled": true,
		"task_worker_id": "task-worker-a",
		"task_worker_concurrency": 4,
		"task_worker_interval": "200ms",
		"task_worker_claim_limit": 11,
		"task_worker_lease_duration": "25s",
		"task_worker_drain_timeout": "30s",
		"task_worker_max_attempts": 4,
		"task_worker_retry_delay": "2s",
		"task_worker_capacity_wait_max_attempts": 12,
		"task_worker_capacity_wait_timeout": "7m",
		"task_worker_capacity_wait_callback_interval": "3m",
		"task_worker_stale_progress_timeout": "45s",
		"task_progress_callback_enabled": true,
		"task_progress_callback_batch_size": 7,
		"task_progress_callback_interval": "4s",
		"task_progress_callback_step_max_chars": 180,
		"task_progress_callback_state_ttl": "2h",
		"task_queue_max_pending": 99,
		"task_queue_max_pending_per_assistant": 13,
		"log_level": "debug"
	}`)
	t.Setenv("GO_GATEWAY_CONFIG_FILE", path)

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.ConfigFile != path {
		t.Fatalf("ConfigFile = %q, want %q", cfg.ConfigFile, path)
	}
	if cfg.ListenAddr != ":18080" {
		t.Fatalf("ListenAddr = %q", cfg.ListenAddr)
	}
	if cfg.ProcessRole != ProcessRoleTaskWorker {
		t.Fatalf("ProcessRole = %q", cfg.ProcessRole)
	}
	if !cfg.RunsTaskWorker() {
		t.Fatal("RunsTaskWorker() = false, want true")
	}
	if cfg.RunsCallbackOutboxWorker() {
		t.Fatal("RunsCallbackOutboxWorker() = true, want false")
	}
	if !cfg.RunsRuntimeSync() {
		t.Fatal("RunsRuntimeSync() = false, want true")
	}
	if cfg.SecurityBaseURL != "http://security.local" {
		t.Fatalf("SecurityBaseURL = %q", cfg.SecurityBaseURL)
	}
	if cfg.AdminBaseURL != "http://admin.local" {
		t.Fatalf("AdminBaseURL = %q", cfg.AdminBaseURL)
	}
	if cfg.InternalServiceToken != "file-internal-service-token" {
		t.Fatalf("InternalServiceToken = %q", cfg.InternalServiceToken)
	}
	if cfg.DatabaseURL != "mysql+pymysql://ironagent:ironagent@mysql:3306/ironagent?charset=utf8mb4" {
		t.Fatalf("DatabaseURL = %q", cfg.DatabaseURL)
	}
	if cfg.DatabaseMaxOpenConns != 30 {
		t.Fatalf("DatabaseMaxOpenConns = %d", cfg.DatabaseMaxOpenConns)
	}
	if cfg.DatabaseMaxIdleConns != 8 {
		t.Fatalf("DatabaseMaxIdleConns = %d", cfg.DatabaseMaxIdleConns)
	}
	if cfg.DatabaseConnMaxLifetime != 20*time.Minute {
		t.Fatalf("DatabaseConnMaxLifetime = %v", cfg.DatabaseConnMaxLifetime)
	}
	if cfg.DatabaseConnMaxIdleTime != 4*time.Minute {
		t.Fatalf("DatabaseConnMaxIdleTime = %v", cfg.DatabaseConnMaxIdleTime)
	}
	if cfg.CallbackDatabaseMaxOpenConns != 7 {
		t.Fatalf("CallbackDatabaseMaxOpenConns = %d", cfg.CallbackDatabaseMaxOpenConns)
	}
	if cfg.CallbackDatabaseMaxIdleConns != 3 {
		t.Fatalf("CallbackDatabaseMaxIdleConns = %d", cfg.CallbackDatabaseMaxIdleConns)
	}
	if cfg.RedisMode != "sentinel" {
		t.Fatalf("RedisMode = %q", cfg.RedisMode)
	}
	if cfg.RedisAddr != "redis:6379" {
		t.Fatalf("RedisAddr = %q", cfg.RedisAddr)
	}
	if got, want := strings.Join(cfg.RedisSentinelAddrs, ","), "redis-sentinel:26379"; got != want {
		t.Fatalf("RedisSentinelAddrs = %q, want %q", got, want)
	}
	if cfg.RedisSentinelMaster != "mymaster" {
		t.Fatalf("RedisSentinelMaster = %q", cfg.RedisSentinelMaster)
	}
	if cfg.RedisUsername != "file-redis-user" {
		t.Fatalf("RedisUsername = %q", cfg.RedisUsername)
	}
	if cfg.RedisPassword != "file-redis-password" {
		t.Fatalf("RedisPassword = %q", cfg.RedisPassword)
	}
	if cfg.RedisDB != 1 {
		t.Fatalf("RedisDB = %d", cfg.RedisDB)
	}
	if cfg.RedisKeyPrefix != "file-gateway:" {
		t.Fatalf("RedisKeyPrefix = %q", cfg.RedisKeyPrefix)
	}
	if cfg.RedisTimeout != 300*time.Millisecond {
		t.Fatalf("RedisTimeout = %v", cfg.RedisTimeout)
	}
	if cfg.RedisMaxOpenConns != 12 {
		t.Fatalf("RedisMaxOpenConns = %d", cfg.RedisMaxOpenConns)
	}
	if cfg.RedisMaxIdleConns != 4 {
		t.Fatalf("RedisMaxIdleConns = %d", cfg.RedisMaxIdleConns)
	}
	if !cfg.RedisRequired {
		t.Fatal("RedisRequired = false, want true")
	}
	if cfg.AssistantCacheTTL != 45*time.Second {
		t.Fatalf("AssistantCacheTTL = %v", cfg.AssistantCacheTTL)
	}
	if cfg.AssistantAvailabilityCacheTTL != 15*time.Second {
		t.Fatalf("AssistantAvailabilityCacheTTL = %v", cfg.AssistantAvailabilityCacheTTL)
	}
	if !cfg.ChatRateLimitEnabled {
		t.Fatal("ChatRateLimitEnabled = false, want true")
	}
	if cfg.ChatRateLimitMax != 700 {
		t.Fatalf("ChatRateLimitMax = %d", cfg.ChatRateLimitMax)
	}
	if cfg.ChatRateLimitWindow != 2*time.Second {
		t.Fatalf("ChatRateLimitWindow = %v", cfg.ChatRateLimitWindow)
	}
	if cfg.ChatRateLimitFailClosed {
		t.Fatal("ChatRateLimitFailClosed = true, want false")
	}
	if got, want := strings.Join(cfg.APIKeys, ","), "file-key"; got != want {
		t.Fatalf("APIKeys = %q, want %q", got, want)
	}
	if len(cfg.ScopedAPIKeys) != 2 {
		t.Fatalf("ScopedAPIKeys length = %d", len(cfg.ScopedAPIKeys))
	}
	if cfg.ScopedAPIKeys[0].Key != "public-file-key" || strings.Join(cfg.ScopedAPIKeys[0].Scopes, ",") != "public" {
		t.Fatalf("ScopedAPIKeys[0] = %#v", cfg.ScopedAPIKeys[0])
	}
	if cfg.HMACAllowedSkew != 30*time.Second {
		t.Fatalf("HMACAllowedSkew = %v", cfg.HMACAllowedSkew)
	}
	if !cfg.PublicAuthDisabled {
		t.Fatal("PublicAuthDisabled = false, want true")
	}
	if !cfg.AdminAuthDisabled {
		t.Fatal("AdminAuthDisabled = false, want true")
	}
	if cfg.HTTPTimeout != 2*time.Second {
		t.Fatalf("HTTPTimeout = %v", cfg.HTTPTimeout)
	}
	if cfg.PublicBaseURL != "http://gateway.local" {
		t.Fatalf("PublicBaseURL = %q", cfg.PublicBaseURL)
	}
	if cfg.DefaultCallbackURL != "https://xiaoxiongmao.local/ironagent/callback" {
		t.Fatalf("DefaultCallbackURL = %q", cfg.DefaultCallbackURL)
	}
	if got, want := strings.Join(cfg.CallbackAllowedHosts, ","), "xiaoxiongmao.local"; got != want {
		t.Fatalf("CallbackAllowedHosts = %q, want %q", got, want)
	}
	if !cfg.CallbackAllowPrivate {
		t.Fatalf("CallbackAllowPrivate = false, want true")
	}
	if !cfg.CallbackFireAndForget {
		t.Fatalf("CallbackFireAndForget = false, want true")
	}
	if !cfg.CallbackAcceptAnyResponse {
		t.Fatalf("CallbackAcceptAnyResponse = false, want true")
	}
	if !cfg.CallbackRequired {
		t.Fatalf("CallbackRequired = false, want true")
	}
	if cfg.CallbackMaxAttempts != 5 {
		t.Fatalf("CallbackMaxAttempts = %d", cfg.CallbackMaxAttempts)
	}
	if cfg.CallbackWorkerID != "worker-a" {
		t.Fatalf("CallbackWorkerID = %q", cfg.CallbackWorkerID)
	}
	if cfg.CallbackRetryDelay != 100*time.Millisecond {
		t.Fatalf("CallbackRetryDelay = %v", cfg.CallbackRetryDelay)
	}
	if cfg.CallbackMaxRetryDelay != 3*time.Second {
		t.Fatalf("CallbackMaxRetryDelay = %v", cfg.CallbackMaxRetryDelay)
	}
	if cfg.CallbackHTTPTimeout != 7*time.Second {
		t.Fatalf("CallbackHTTPTimeout = %v", cfg.CallbackHTTPTimeout)
	}
	if cfg.CallbackQueueSize != 2048 {
		t.Fatalf("CallbackQueueSize = %d", cfg.CallbackQueueSize)
	}
	if cfg.CallbackStoreTimeout != 750*time.Millisecond {
		t.Fatalf("CallbackStoreTimeout = %v", cfg.CallbackStoreTimeout)
	}
	if cfg.CallbackEnqueueMaxInFlight != 6 {
		t.Fatalf("CallbackEnqueueMaxInFlight = %d", cfg.CallbackEnqueueMaxInFlight)
	}
	if cfg.CallbackDeliveryConcurrency != 3 {
		t.Fatalf("CallbackDeliveryConcurrency = %d", cfg.CallbackDeliveryConcurrency)
	}
	if cfg.CallbackWorkerInterval != 150*time.Millisecond {
		t.Fatalf("CallbackWorkerInterval = %v", cfg.CallbackWorkerInterval)
	}
	if cfg.CallbackPendingLimit != 25 {
		t.Fatalf("CallbackPendingLimit = %d", cfg.CallbackPendingLimit)
	}
	if cfg.CallbackLeaseDuration != 45*time.Second {
		t.Fatalf("CallbackLeaseDuration = %v", cfg.CallbackLeaseDuration)
	}
	if cfg.CallbackOutboxWorkerEnabled {
		t.Fatalf("CallbackOutboxWorkerEnabled = true, want false")
	}
	if cfg.PresignTTL != 10*time.Minute {
		t.Fatalf("PresignTTL = %v", cfg.PresignTTL)
	}
	if cfg.MaxUploadBytes != 12345 {
		t.Fatalf("MaxUploadBytes = %d", cfg.MaxUploadBytes)
	}
	if cfg.ProxySessionSecret != "file-proxy-session" {
		t.Fatalf("ProxySessionSecret = %q", cfg.ProxySessionSecret)
	}
	if cfg.ProxySessionTTL != 20*time.Minute {
		t.Fatalf("ProxySessionTTL = %v", cfg.ProxySessionTTL)
	}
	if cfg.ProxyMaxInFlight != 123 {
		t.Fatalf("ProxyMaxInFlight = %d", cfg.ProxyMaxInFlight)
	}
	if cfg.ProxyFailureThreshold != 2 {
		t.Fatalf("ProxyFailureThreshold = %d", cfg.ProxyFailureThreshold)
	}
	if cfg.ProxyCircuitOpenDuration != 5*time.Second {
		t.Fatalf("ProxyCircuitOpenDuration = %v", cfg.ProxyCircuitOpenDuration)
	}
	if cfg.RuntimeDefaultCapacity != 7 {
		t.Fatalf("RuntimeDefaultCapacity = %d", cfg.RuntimeDefaultCapacity)
	}
	if cfg.RuntimeHeartbeatTTL != 20*time.Second {
		t.Fatalf("RuntimeHeartbeatTTL = %v", cfg.RuntimeHeartbeatTTL)
	}
	if got, want := strings.Join(cfg.RuntimeBootstrapAssistants, ","), "demo-main"; got != want {
		t.Fatalf("RuntimeBootstrapAssistants = %q, want %q", got, want)
	}
	if !cfg.RuntimeDiscoverAssistants {
		t.Fatal("RuntimeDiscoverAssistants = false, want true")
	}
	if cfg.RuntimeSyncInterval != 5*time.Second {
		t.Fatalf("RuntimeSyncInterval = %v", cfg.RuntimeSyncInterval)
	}
	if !cfg.RuntimeExecutionEnabled {
		t.Fatalf("RuntimeExecutionEnabled = false, want true")
	}
	if cfg.RuntimeExecutionTimeout != 45*time.Minute {
		t.Fatalf("RuntimeExecutionTimeout = %v", cfg.RuntimeExecutionTimeout)
	}
	if !cfg.TaskWorkerEnabled {
		t.Fatalf("TaskWorkerEnabled = false, want true")
	}
	if cfg.TaskWorkerID != "task-worker-a" {
		t.Fatalf("TaskWorkerID = %q", cfg.TaskWorkerID)
	}
	if cfg.TaskWorkerConcurrency != 4 {
		t.Fatalf("TaskWorkerConcurrency = %d", cfg.TaskWorkerConcurrency)
	}
	if cfg.TaskWorkerInterval != 200*time.Millisecond {
		t.Fatalf("TaskWorkerInterval = %v", cfg.TaskWorkerInterval)
	}
	if cfg.TaskWorkerClaimLimit != 11 {
		t.Fatalf("TaskWorkerClaimLimit = %d", cfg.TaskWorkerClaimLimit)
	}
	if cfg.TaskWorkerLeaseDuration != 25*time.Second {
		t.Fatalf("TaskWorkerLeaseDuration = %v", cfg.TaskWorkerLeaseDuration)
	}
	if cfg.TaskWorkerDrainTimeout != 30*time.Second {
		t.Fatalf("TaskWorkerDrainTimeout = %v", cfg.TaskWorkerDrainTimeout)
	}
	if cfg.TaskWorkerMaxAttempts != 4 {
		t.Fatalf("TaskWorkerMaxAttempts = %d", cfg.TaskWorkerMaxAttempts)
	}
	if cfg.TaskWorkerRetryDelay != 2*time.Second {
		t.Fatalf("TaskWorkerRetryDelay = %v", cfg.TaskWorkerRetryDelay)
	}
	if cfg.TaskWorkerCapacityWaitMaxAttempts != 12 {
		t.Fatalf("TaskWorkerCapacityWaitMaxAttempts = %d", cfg.TaskWorkerCapacityWaitMaxAttempts)
	}
	if cfg.TaskWorkerCapacityWaitTimeout != 7*time.Minute {
		t.Fatalf("TaskWorkerCapacityWaitTimeout = %v", cfg.TaskWorkerCapacityWaitTimeout)
	}
	if cfg.TaskWorkerCapacityWaitCallbackInterval != 3*time.Minute {
		t.Fatalf("TaskWorkerCapacityWaitCallbackInterval = %v", cfg.TaskWorkerCapacityWaitCallbackInterval)
	}
	if cfg.TaskWorkerStaleProgressTimeout != 45*time.Second {
		t.Fatalf("TaskWorkerStaleProgressTimeout = %v", cfg.TaskWorkerStaleProgressTimeout)
	}
	if !cfg.TaskProgressCallbackEnabled {
		t.Fatalf("TaskProgressCallbackEnabled = false, want true")
	}
	if cfg.TaskProgressCallbackBatchSize != 7 {
		t.Fatalf("TaskProgressCallbackBatchSize = %d", cfg.TaskProgressCallbackBatchSize)
	}
	if cfg.TaskProgressCallbackInterval != 4*time.Second {
		t.Fatalf("TaskProgressCallbackInterval = %v", cfg.TaskProgressCallbackInterval)
	}
	if cfg.TaskProgressCallbackStepChars != 180 {
		t.Fatalf("TaskProgressCallbackStepChars = %d", cfg.TaskProgressCallbackStepChars)
	}
	if cfg.TaskProgressCallbackStateTTL != 2*time.Hour {
		t.Fatalf("TaskProgressCallbackStateTTL = %v", cfg.TaskProgressCallbackStateTTL)
	}
	if cfg.TaskQueueMaxPending != 99 {
		t.Fatalf("TaskQueueMaxPending = %d", cfg.TaskQueueMaxPending)
	}
	if cfg.TaskQueueMaxPendingPerAssistant != 13 {
		t.Fatalf("TaskQueueMaxPendingPerAssistant = %d", cfg.TaskQueueMaxPendingPerAssistant)
	}
	if cfg.LogLevel != slog.LevelDebug {
		t.Fatalf("LogLevel = %v", cfg.LogLevel)
	}
}

func TestLoadDefaultsTaskWorkerExecutionWindow(t *testing.T) {
	cleanGatewayEnv(t)
	t.Setenv("GO_GATEWAY_INTERNAL_SERVICE_TOKEN", "test-internal-token")
	t.Setenv("GO_GATEWAY_API_KEYS", "test-api-key")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.TaskWorkerClaimLimit != 1 {
		t.Fatalf("TaskWorkerClaimLimit = %d, want 1", cfg.TaskWorkerClaimLimit)
	}
	if cfg.ProcessRole != ProcessRoleAll {
		t.Fatalf("ProcessRole = %q, want all", cfg.ProcessRole)
	}
	if !cfg.CallbackOutboxWorkerEnabled {
		t.Fatal("CallbackOutboxWorkerEnabled = false, want true")
	}
	if cfg.TaskWorkerConcurrency != 1 {
		t.Fatalf("TaskWorkerConcurrency = %d, want 1", cfg.TaskWorkerConcurrency)
	}
	if cfg.TaskWorkerDrainTimeout != 11*time.Minute {
		t.Fatalf("TaskWorkerDrainTimeout = %v, want 11m", cfg.TaskWorkerDrainTimeout)
	}
	if cfg.RuntimeExecutionTimeout != 10*time.Minute {
		t.Fatalf("RuntimeExecutionTimeout = %v, want 10m", cfg.RuntimeExecutionTimeout)
	}
	if cfg.TaskWorkerMaxAttempts != 3 {
		t.Fatalf("TaskWorkerMaxAttempts = %d, want 3", cfg.TaskWorkerMaxAttempts)
	}
	if cfg.TaskWorkerCapacityWaitMaxAttempts != 100 {
		t.Fatalf("TaskWorkerCapacityWaitMaxAttempts = %d, want 100", cfg.TaskWorkerCapacityWaitMaxAttempts)
	}
	if cfg.TaskWorkerCapacityWaitTimeout != 30*time.Minute {
		t.Fatalf("TaskWorkerCapacityWaitTimeout = %v, want 30m", cfg.TaskWorkerCapacityWaitTimeout)
	}
	if cfg.TaskWorkerCapacityWaitCallbackInterval != 5*time.Minute {
		t.Fatalf("TaskWorkerCapacityWaitCallbackInterval = %v, want 5m", cfg.TaskWorkerCapacityWaitCallbackInterval)
	}
	if cfg.TaskWorkerStaleProgressTimeout != 600*time.Second {
		t.Fatalf("TaskWorkerStaleProgressTimeout = %v, want 600s", cfg.TaskWorkerStaleProgressTimeout)
	}
	if cfg.TaskProgressCallbackEnabled {
		t.Fatal("TaskProgressCallbackEnabled = true, want false")
	}
	if cfg.TaskProgressCallbackBatchSize != 5 {
		t.Fatalf("TaskProgressCallbackBatchSize = %d, want 5", cfg.TaskProgressCallbackBatchSize)
	}
	if cfg.TaskProgressCallbackInterval != 90*time.Second {
		t.Fatalf("TaskProgressCallbackInterval = %v, want 90s", cfg.TaskProgressCallbackInterval)
	}
	if cfg.TaskProgressCallbackStepChars != 150 {
		t.Fatalf("TaskProgressCallbackStepChars = %d, want 150", cfg.TaskProgressCallbackStepChars)
	}
	if cfg.TaskProgressCallbackStateTTL != 30*time.Minute {
		t.Fatalf("TaskProgressCallbackStateTTL = %v, want 30m", cfg.TaskProgressCallbackStateTTL)
	}
}

func TestLoadEnvOverridesConfigFile(t *testing.T) {
	cleanGatewayEnv(t)
	path := filepath.Join(t.TempDir(), "go-gateway.json")
	writeConfigFile(t, path, `{
		"internal_service_token": "file-internal-service-token",
		"database_url": "mysql+pymysql://file:file@mysql:3306/file",
		"api_keys": ["file-key"],
		"max_upload_bytes": 12345
	}`)
	t.Setenv("GO_GATEWAY_CONFIG_FILE", path)
	t.Setenv("GO_GATEWAY_INTERNAL_SERVICE_TOKEN", "env-internal-service-token")
	t.Setenv("GO_GATEWAY_PROCESS_ROLE", "callback-worker")
	t.Setenv("GO_GATEWAY_DATABASE_URL", "mysql+pymysql://env:env@mysql:3306/env")
	t.Setenv("GO_GATEWAY_DATABASE_MAX_OPEN_CONNS", "24")
	t.Setenv("GO_GATEWAY_DATABASE_MAX_IDLE_CONNS", "6")
	t.Setenv("GO_GATEWAY_DATABASE_CONN_MAX_LIFETIME", "15m")
	t.Setenv("GO_GATEWAY_DATABASE_CONN_MAX_IDLE_TIME", "2m")
	t.Setenv("GO_GATEWAY_CALLBACK_DATABASE_MAX_OPEN_CONNS", "5")
	t.Setenv("GO_GATEWAY_CALLBACK_DATABASE_MAX_IDLE_CONNS", "2")
	t.Setenv("GO_GATEWAY_REDIS_MODE", "sentinel")
	t.Setenv("GO_GATEWAY_REDIS_ADDR", "redis-env:6379")
	t.Setenv("GO_GATEWAY_REDIS_SENTINEL_ADDRS", "redis-sentinel-a:26379, redis-sentinel-b:26379")
	t.Setenv("GO_GATEWAY_REDIS_SENTINEL_MASTER", "envmaster")
	t.Setenv("GO_GATEWAY_REDIS_USERNAME", "env-redis-user")
	t.Setenv("GO_GATEWAY_REDIS_PASSWORD", "env-redis-password")
	t.Setenv("GO_GATEWAY_REDIS_DB", "2")
	t.Setenv("GO_GATEWAY_REDIS_KEY_PREFIX", "env-gateway:")
	t.Setenv("GO_GATEWAY_REDIS_TIMEOUT", "250ms")
	t.Setenv("GO_GATEWAY_REDIS_MAX_OPEN_CONNS", "10")
	t.Setenv("GO_GATEWAY_REDIS_MAX_IDLE_CONNS", "3")
	t.Setenv("GO_GATEWAY_REDIS_REQUIRED", "true")
	t.Setenv("GO_GATEWAY_ASSISTANT_CACHE_TTL", "55s")
	t.Setenv("GO_GATEWAY_ASSISTANT_AVAILABILITY_CACHE_TTL", "25s")
	t.Setenv("GO_GATEWAY_CHAT_RATE_LIMIT_ENABLED", "true")
	t.Setenv("GO_GATEWAY_CHAT_RATE_LIMIT_MAX", "800")
	t.Setenv("GO_GATEWAY_CHAT_RATE_LIMIT_WINDOW", "3s")
	t.Setenv("GO_GATEWAY_CHAT_RATE_LIMIT_FAIL_CLOSED", "false")
	t.Setenv("GO_GATEWAY_CALLBACK_STORE_TIMEOUT", "500ms")
	t.Setenv("GO_GATEWAY_CALLBACK_HTTP_TIMEOUT", "8s")
	t.Setenv("GO_GATEWAY_CALLBACK_ENQUEUE_MAX_IN_FLIGHT", "4")
	t.Setenv("GO_GATEWAY_CALLBACK_DELIVERY_CONCURRENCY", "2")
	t.Setenv("GO_GATEWAY_API_KEYS", "env-key-a, env-key-b")
	t.Setenv("GO_GATEWAY_SCOPED_API_KEYS", "public-env-key:public;ops-env-key:admin,debug")
	t.Setenv("GO_GATEWAY_PUBLIC_AUTH_DISABLED", "true")
	t.Setenv("GO_GATEWAY_ADMIN_AUTH_DISABLED", "true")
	t.Setenv("GO_GATEWAY_CALLBACK_FIRE_AND_FORGET", "true")
	t.Setenv("GO_GATEWAY_CALLBACK_ACCEPT_ANY_RESPONSE", "true")
	t.Setenv("GO_GATEWAY_CALLBACK_REQUIRED", "true")
	t.Setenv("GO_GATEWAY_CALLBACK_OUTBOX_WORKER_ENABLED", "false")
	t.Setenv("GO_GATEWAY_MAX_UPLOAD_BYTES", "67890")
	t.Setenv("GO_GATEWAY_PROXY_SESSION_SECRET", "env-proxy-session")
	t.Setenv("GO_GATEWAY_PROXY_SESSION_TTL", "7m")
	t.Setenv("GO_GATEWAY_PROXY_MAX_IN_FLIGHT", "456")
	t.Setenv("GO_GATEWAY_PROXY_FAILURE_THRESHOLD", "4")
	t.Setenv("GO_GATEWAY_PROXY_CIRCUIT_OPEN_DURATION", "9s")
	t.Setenv("GO_GATEWAY_RUNTIME_BOOTSTRAP_ASSISTANTS", "demo-main, demo-worker")
	t.Setenv("GO_GATEWAY_RUNTIME_DISCOVER_ASSISTANTS", "true")
	t.Setenv("GO_GATEWAY_RUNTIME_SYNC_INTERVAL", "3s")
	t.Setenv("GO_GATEWAY_RUNTIME_EXECUTION_ENABLED", "true")
	t.Setenv("GO_GATEWAY_RUNTIME_EXECUTION_TIMEOUT", "50m")
	t.Setenv("GO_GATEWAY_TASK_WORKER_ENABLED", "true")
	t.Setenv("GO_GATEWAY_TASK_WORKER_CONCURRENCY", "4")
	t.Setenv("GO_GATEWAY_TASK_WORKER_DRAIN_TIMEOUT", "35s")
	t.Setenv("GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_MAX_ATTEMPTS", "9")
	t.Setenv("GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_TIMEOUT", "6m")
	t.Setenv("GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_CALLBACK_INTERVAL", "2m")
	t.Setenv("GO_GATEWAY_TASK_PROGRESS_CALLBACK_ENABLED", "true")
	t.Setenv("GO_GATEWAY_TASK_PROGRESS_CALLBACK_BATCH_SIZE", "6")
	t.Setenv("GO_GATEWAY_TASK_PROGRESS_CALLBACK_INTERVAL", "4s")
	t.Setenv("GO_GATEWAY_TASK_PROGRESS_CALLBACK_STEP_MAX_CHARS", "180")
	t.Setenv("GO_GATEWAY_TASK_PROGRESS_CALLBACK_STATE_TTL", "2h")
	t.Setenv("GO_GATEWAY_TASK_QUEUE_MAX_PENDING_PER_ASSISTANT", "17")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.InternalServiceToken != "env-internal-service-token" {
		t.Fatalf("InternalServiceToken = %q", cfg.InternalServiceToken)
	}
	if cfg.ProcessRole != ProcessRoleCallbackWorker {
		t.Fatalf("ProcessRole = %q", cfg.ProcessRole)
	}
	if cfg.DatabaseURL != "mysql+pymysql://env:env@mysql:3306/env" {
		t.Fatalf("DatabaseURL = %q", cfg.DatabaseURL)
	}
	if cfg.DatabaseMaxOpenConns != 24 {
		t.Fatalf("DatabaseMaxOpenConns = %d", cfg.DatabaseMaxOpenConns)
	}
	if cfg.DatabaseMaxIdleConns != 6 {
		t.Fatalf("DatabaseMaxIdleConns = %d", cfg.DatabaseMaxIdleConns)
	}
	if cfg.DatabaseConnMaxLifetime != 15*time.Minute {
		t.Fatalf("DatabaseConnMaxLifetime = %v", cfg.DatabaseConnMaxLifetime)
	}
	if cfg.DatabaseConnMaxIdleTime != 2*time.Minute {
		t.Fatalf("DatabaseConnMaxIdleTime = %v", cfg.DatabaseConnMaxIdleTime)
	}
	if cfg.CallbackDatabaseMaxOpenConns != 5 {
		t.Fatalf("CallbackDatabaseMaxOpenConns = %d", cfg.CallbackDatabaseMaxOpenConns)
	}
	if cfg.CallbackDatabaseMaxIdleConns != 2 {
		t.Fatalf("CallbackDatabaseMaxIdleConns = %d", cfg.CallbackDatabaseMaxIdleConns)
	}
	if cfg.RedisMode != "sentinel" {
		t.Fatalf("RedisMode = %q", cfg.RedisMode)
	}
	if cfg.RedisAddr != "redis-env:6379" {
		t.Fatalf("RedisAddr = %q", cfg.RedisAddr)
	}
	if got, want := strings.Join(cfg.RedisSentinelAddrs, ","), "redis-sentinel-a:26379,redis-sentinel-b:26379"; got != want {
		t.Fatalf("RedisSentinelAddrs = %q, want %q", got, want)
	}
	if cfg.RedisSentinelMaster != "envmaster" {
		t.Fatalf("RedisSentinelMaster = %q", cfg.RedisSentinelMaster)
	}
	if cfg.RedisUsername != "env-redis-user" {
		t.Fatalf("RedisUsername = %q", cfg.RedisUsername)
	}
	if cfg.RedisPassword != "env-redis-password" {
		t.Fatalf("RedisPassword = %q", cfg.RedisPassword)
	}
	if cfg.RedisDB != 2 {
		t.Fatalf("RedisDB = %d", cfg.RedisDB)
	}
	if cfg.RedisKeyPrefix != "env-gateway:" {
		t.Fatalf("RedisKeyPrefix = %q", cfg.RedisKeyPrefix)
	}
	if cfg.RedisTimeout != 250*time.Millisecond {
		t.Fatalf("RedisTimeout = %v", cfg.RedisTimeout)
	}
	if cfg.RedisMaxOpenConns != 10 {
		t.Fatalf("RedisMaxOpenConns = %d", cfg.RedisMaxOpenConns)
	}
	if cfg.RedisMaxIdleConns != 3 {
		t.Fatalf("RedisMaxIdleConns = %d", cfg.RedisMaxIdleConns)
	}
	if !cfg.RedisRequired {
		t.Fatal("RedisRequired = false, want true")
	}
	if cfg.AssistantCacheTTL != 55*time.Second {
		t.Fatalf("AssistantCacheTTL = %v", cfg.AssistantCacheTTL)
	}
	if cfg.AssistantAvailabilityCacheTTL != 25*time.Second {
		t.Fatalf("AssistantAvailabilityCacheTTL = %v", cfg.AssistantAvailabilityCacheTTL)
	}
	if !cfg.ChatRateLimitEnabled {
		t.Fatal("ChatRateLimitEnabled = false, want true")
	}
	if cfg.ChatRateLimitMax != 800 {
		t.Fatalf("ChatRateLimitMax = %d", cfg.ChatRateLimitMax)
	}
	if cfg.ChatRateLimitWindow != 3*time.Second {
		t.Fatalf("ChatRateLimitWindow = %v", cfg.ChatRateLimitWindow)
	}
	if cfg.ChatRateLimitFailClosed {
		t.Fatal("ChatRateLimitFailClosed = true, want false")
	}
	if cfg.CallbackStoreTimeout != 500*time.Millisecond {
		t.Fatalf("CallbackStoreTimeout = %v", cfg.CallbackStoreTimeout)
	}
	if cfg.CallbackHTTPTimeout != 8*time.Second {
		t.Fatalf("CallbackHTTPTimeout = %v", cfg.CallbackHTTPTimeout)
	}
	if cfg.CallbackEnqueueMaxInFlight != 4 {
		t.Fatalf("CallbackEnqueueMaxInFlight = %d", cfg.CallbackEnqueueMaxInFlight)
	}
	if cfg.CallbackDeliveryConcurrency != 2 {
		t.Fatalf("CallbackDeliveryConcurrency = %d", cfg.CallbackDeliveryConcurrency)
	}
	if got, want := strings.Join(cfg.APIKeys, ","), "env-key-a,env-key-b"; got != want {
		t.Fatalf("APIKeys = %q, want %q", got, want)
	}
	if len(cfg.ScopedAPIKeys) != 2 {
		t.Fatalf("ScopedAPIKeys length = %d", len(cfg.ScopedAPIKeys))
	}
	if !cfg.PublicAuthDisabled {
		t.Fatal("PublicAuthDisabled = false, want true")
	}
	if !cfg.AdminAuthDisabled {
		t.Fatal("AdminAuthDisabled = false, want true")
	}
	if !cfg.CallbackFireAndForget {
		t.Fatal("CallbackFireAndForget = false, want true")
	}
	if !cfg.CallbackAcceptAnyResponse {
		t.Fatal("CallbackAcceptAnyResponse = false, want true")
	}
	if !cfg.CallbackRequired {
		t.Fatal("CallbackRequired = false, want true")
	}
	if cfg.CallbackOutboxWorkerEnabled {
		t.Fatal("CallbackOutboxWorkerEnabled = true, want false")
	}
	if got, want := cfg.ScopedAPIKeys[1].Key+":"+strings.Join(cfg.ScopedAPIKeys[1].Scopes, ","), "ops-env-key:admin,debug"; got != want {
		t.Fatalf("ScopedAPIKeys[1] = %q, want %q", got, want)
	}
	if cfg.MaxUploadBytes != 67890 {
		t.Fatalf("MaxUploadBytes = %d", cfg.MaxUploadBytes)
	}
	if cfg.ProxySessionSecret != "env-proxy-session" {
		t.Fatalf("ProxySessionSecret = %q", cfg.ProxySessionSecret)
	}
	if cfg.ProxySessionTTL != 7*time.Minute {
		t.Fatalf("ProxySessionTTL = %v", cfg.ProxySessionTTL)
	}
	if cfg.ProxyMaxInFlight != 456 {
		t.Fatalf("ProxyMaxInFlight = %d", cfg.ProxyMaxInFlight)
	}
	if cfg.ProxyFailureThreshold != 4 {
		t.Fatalf("ProxyFailureThreshold = %d", cfg.ProxyFailureThreshold)
	}
	if cfg.ProxyCircuitOpenDuration != 9*time.Second {
		t.Fatalf("ProxyCircuitOpenDuration = %v", cfg.ProxyCircuitOpenDuration)
	}
	if got, want := strings.Join(cfg.RuntimeBootstrapAssistants, ","), "demo-main,demo-worker"; got != want {
		t.Fatalf("RuntimeBootstrapAssistants = %q, want %q", got, want)
	}
	if !cfg.RuntimeDiscoverAssistants {
		t.Fatal("RuntimeDiscoverAssistants = false, want true")
	}
	if cfg.RuntimeSyncInterval != 3*time.Second {
		t.Fatalf("RuntimeSyncInterval = %v", cfg.RuntimeSyncInterval)
	}
	if !cfg.RuntimeExecutionEnabled {
		t.Fatal("RuntimeExecutionEnabled = false, want true")
	}
	if cfg.RuntimeExecutionTimeout != 50*time.Minute {
		t.Fatalf("RuntimeExecutionTimeout = %v", cfg.RuntimeExecutionTimeout)
	}
	if !cfg.TaskWorkerEnabled {
		t.Fatal("TaskWorkerEnabled = false, want true")
	}
	if cfg.TaskWorkerConcurrency != 4 {
		t.Fatalf("TaskWorkerConcurrency = %d", cfg.TaskWorkerConcurrency)
	}
	if cfg.TaskWorkerDrainTimeout != 35*time.Second {
		t.Fatalf("TaskWorkerDrainTimeout = %v", cfg.TaskWorkerDrainTimeout)
	}
	if cfg.TaskWorkerCapacityWaitMaxAttempts != 9 {
		t.Fatalf("TaskWorkerCapacityWaitMaxAttempts = %d", cfg.TaskWorkerCapacityWaitMaxAttempts)
	}
	if cfg.TaskWorkerCapacityWaitTimeout != 6*time.Minute {
		t.Fatalf("TaskWorkerCapacityWaitTimeout = %v", cfg.TaskWorkerCapacityWaitTimeout)
	}
	if cfg.TaskWorkerCapacityWaitCallbackInterval != 2*time.Minute {
		t.Fatalf("TaskWorkerCapacityWaitCallbackInterval = %v", cfg.TaskWorkerCapacityWaitCallbackInterval)
	}
	if cfg.RunsTaskWorker() {
		t.Fatal("RunsTaskWorker() = true, want false for callback-worker role")
	}
	if cfg.RunsCallbackOutboxWorker() {
		t.Fatal("RunsCallbackOutboxWorker() = true, want false when disabled")
	}
	if !cfg.TaskProgressCallbackEnabled {
		t.Fatal("TaskProgressCallbackEnabled = false, want true")
	}
	if cfg.TaskProgressCallbackBatchSize != 6 {
		t.Fatalf("TaskProgressCallbackBatchSize = %d", cfg.TaskProgressCallbackBatchSize)
	}
	if cfg.TaskProgressCallbackInterval != 4*time.Second {
		t.Fatalf("TaskProgressCallbackInterval = %v", cfg.TaskProgressCallbackInterval)
	}
	if cfg.TaskProgressCallbackStepChars != 180 {
		t.Fatalf("TaskProgressCallbackStepChars = %d", cfg.TaskProgressCallbackStepChars)
	}
	if cfg.TaskProgressCallbackStateTTL != 2*time.Hour {
		t.Fatalf("TaskProgressCallbackStateTTL = %v", cfg.TaskProgressCallbackStateTTL)
	}
	if cfg.TaskQueueMaxPendingPerAssistant != 17 {
		t.Fatalf("TaskQueueMaxPendingPerAssistant = %d", cfg.TaskQueueMaxPendingPerAssistant)
	}
}

func TestLoadFallsBackToIronAgentDatabaseURL(t *testing.T) {
	cleanGatewayEnv(t)
	t.Setenv("GO_GATEWAY_INTERNAL_SERVICE_TOKEN", "env-internal-service-token")
	t.Setenv("GO_GATEWAY_API_KEYS", "env-key")
	t.Setenv("IRON_AGENT_DATABASE_URL", "mysql+pymysql://ironagent:ironagent@mysql:3306/ironagent")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.DatabaseURL != "mysql+pymysql://ironagent:ironagent@mysql:3306/ironagent" {
		t.Fatalf("DatabaseURL = %q", cfg.DatabaseURL)
	}
	if cfg.PublicAuthDisabled {
		t.Fatal("PublicAuthDisabled = true, want false")
	}
}

func TestLoadPrefersIronagentDatabaseURLWithoutSeparator(t *testing.T) {
	cleanGatewayEnv(t)
	t.Setenv("GO_GATEWAY_INTERNAL_SERVICE_TOKEN", "env-internal-service-token")
	t.Setenv("GO_GATEWAY_API_KEYS", "env-key")
	t.Setenv("IRONAGENT_DATABASE_URL", "mysql+pymysql://ironagent:new@mysql:3306/ironagent")
	t.Setenv("IRON_AGENT_DATABASE_URL", "mysql+pymysql://ironagent:old@mysql:3306/ironagent")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.DatabaseURL != "mysql+pymysql://ironagent:new@mysql:3306/ironagent" {
		t.Fatalf("DatabaseURL = %q", cfg.DatabaseURL)
	}
}

func TestLoadDefaultsToStrictCallbackResponseHandling(t *testing.T) {
	cleanGatewayEnv(t)
	t.Setenv("GO_GATEWAY_INTERNAL_SERVICE_TOKEN", "env-internal-service-token")
	t.Setenv("GO_GATEWAY_API_KEYS", "env-key")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.CallbackAcceptAnyResponse {
		t.Fatal("CallbackAcceptAnyResponse = true, want false")
	}
	if cfg.CallbackRequired {
		t.Fatal("CallbackRequired = true, want false")
	}
	if cfg.CallbackHTTPTimeout != 10*time.Second {
		t.Fatalf("CallbackHTTPTimeout = %v, want 10s", cfg.CallbackHTTPTimeout)
	}
}

func TestLoadDefaultsToAuthenticatedPublicRoutes(t *testing.T) {
	cleanGatewayEnv(t)
	t.Setenv("GO_GATEWAY_INTERNAL_SERVICE_TOKEN", "env-internal-service-token")
	t.Setenv("GO_GATEWAY_API_KEYS", "env-key")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.PublicAuthDisabled {
		t.Fatal("PublicAuthDisabled = true, want false")
	}
}

func TestLoadUsesIronAgentInternalServiceTokenFallback(t *testing.T) {
	cleanGatewayEnv(t)
	t.Setenv("GO_GATEWAY_API_KEYS", "env-key")
	t.Setenv("IRON_AGENT_INTERNAL_SERVICE_TOKEN", "agent-internal-service-token")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.InternalServiceToken != "agent-internal-service-token" {
		t.Fatalf("InternalServiceToken = %q", cfg.InternalServiceToken)
	}
}

func TestLoadPrefersIronagentInternalServiceTokenWithoutSeparator(t *testing.T) {
	cleanGatewayEnv(t)
	t.Setenv("GO_GATEWAY_API_KEYS", "env-key")
	t.Setenv("IRONAGENT_INTERNAL_SERVICE_TOKEN", "agent-internal-service-token")
	t.Setenv("IRON_AGENT_INTERNAL_SERVICE_TOKEN", "old-agent-internal-service-token")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.InternalServiceToken != "agent-internal-service-token" {
		t.Fatalf("InternalServiceToken = %q", cfg.InternalServiceToken)
	}
}

func TestLoadRejectsInvalidDatabasePoolConfig(t *testing.T) {
	cleanGatewayEnv(t)
	t.Setenv("GO_GATEWAY_INTERNAL_SERVICE_TOKEN", "env-internal-service-token")
	t.Setenv("GO_GATEWAY_API_KEYS", "env-key")
	t.Setenv("GO_GATEWAY_DATABASE_MAX_OPEN_CONNS", "4")
	t.Setenv("GO_GATEWAY_DATABASE_MAX_IDLE_CONNS", "5")

	_, err := Load()
	if err == nil {
		t.Fatal("Load() error = nil, want invalid database pool config")
	}
	if !strings.Contains(err.Error(), "GO_GATEWAY_DATABASE_MAX_IDLE_CONNS") {
		t.Fatalf("Load() error = %v", err)
	}
}

func TestLoadRejectsUnboundedDatabasePoolConfig(t *testing.T) {
	cleanGatewayEnv(t)
	t.Setenv("GO_GATEWAY_INTERNAL_SERVICE_TOKEN", "env-internal-service-token")
	t.Setenv("GO_GATEWAY_API_KEYS", "env-key")
	t.Setenv("GO_GATEWAY_DATABASE_MAX_OPEN_CONNS", "0")

	_, err := Load()
	if err == nil {
		t.Fatal("Load() error = nil, want unbounded database pool config error")
	}
	if !strings.Contains(err.Error(), "GO_GATEWAY_DATABASE_MAX_OPEN_CONNS") {
		t.Fatalf("Load() error = %v", err)
	}
}

func TestLoadRejectsInvalidCallbackDatabasePoolConfig(t *testing.T) {
	cleanGatewayEnv(t)
	t.Setenv("GO_GATEWAY_INTERNAL_SERVICE_TOKEN", "env-internal-service-token")
	t.Setenv("GO_GATEWAY_API_KEYS", "env-key")
	t.Setenv("GO_GATEWAY_CALLBACK_DATABASE_MAX_OPEN_CONNS", "2")
	t.Setenv("GO_GATEWAY_CALLBACK_DATABASE_MAX_IDLE_CONNS", "3")

	_, err := Load()
	if err == nil {
		t.Fatal("Load() error = nil, want invalid callback database pool config")
	}
	if !strings.Contains(err.Error(), "GO_GATEWAY_CALLBACK_DATABASE_MAX_IDLE_CONNS") {
		t.Fatalf("Load() error = %v", err)
	}
}

func TestLoadRejectsUnboundedCallbackDatabasePoolConfig(t *testing.T) {
	cleanGatewayEnv(t)
	t.Setenv("GO_GATEWAY_INTERNAL_SERVICE_TOKEN", "env-internal-service-token")
	t.Setenv("GO_GATEWAY_API_KEYS", "env-key")
	t.Setenv("GO_GATEWAY_CALLBACK_DATABASE_MAX_OPEN_CONNS", "0")

	_, err := Load()
	if err == nil {
		t.Fatal("Load() error = nil, want unbounded callback database pool config error")
	}
	if !strings.Contains(err.Error(), "GO_GATEWAY_CALLBACK_DATABASE_MAX_OPEN_CONNS") {
		t.Fatalf("Load() error = %v", err)
	}
}

func TestLoadRejectsInvalidCallbackStoreBackpressureConfig(t *testing.T) {
	cleanGatewayEnv(t)
	t.Setenv("GO_GATEWAY_INTERNAL_SERVICE_TOKEN", "env-internal-service-token")
	t.Setenv("GO_GATEWAY_API_KEYS", "env-key")
	t.Setenv("GO_GATEWAY_CALLBACK_STORE_TIMEOUT", "0s")

	_, err := Load()
	if err == nil {
		t.Fatal("Load() error = nil, want invalid callback store timeout")
	}
	if !strings.Contains(err.Error(), "GO_GATEWAY_CALLBACK_STORE_TIMEOUT") {
		t.Fatalf("Load() error = %v", err)
	}

	cleanGatewayEnv(t)
	t.Setenv("GO_GATEWAY_INTERNAL_SERVICE_TOKEN", "env-internal-service-token")
	t.Setenv("GO_GATEWAY_API_KEYS", "env-key")
	t.Setenv("GO_GATEWAY_CALLBACK_ENQUEUE_MAX_IN_FLIGHT", "0")

	_, err = Load()
	if err == nil {
		t.Fatal("Load() error = nil, want invalid callback enqueue max in flight")
	}
	if !strings.Contains(err.Error(), "GO_GATEWAY_CALLBACK_ENQUEUE_MAX_IN_FLIGHT") {
		t.Fatalf("Load() error = %v", err)
	}
}

func TestLoadRejectsBothZeroCapacityWaitBounds(t *testing.T) {
	cleanGatewayEnv(t)
	t.Setenv("GO_GATEWAY_INTERNAL_SERVICE_TOKEN", "env-internal-service-token")
	t.Setenv("GO_GATEWAY_API_KEYS", "env-key")
	t.Setenv("GO_GATEWAY_TASK_WORKER_ENABLED", "true")
	t.Setenv("GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_MAX_ATTEMPTS", "0")
	t.Setenv("GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_TIMEOUT", "0s")

	// Both bounds zero would let a capacity-waiting task loop forever (capacityWaitTerminalReason
	// short-circuits to nil), so Load must reject it rather than ship a fail-open backpressure config.
	if _, err := Load(); err == nil {
		t.Fatal("Load() error = nil, want both-zero capacity-wait rejection")
	} else if !strings.Contains(err.Error(), "must not both be zero") {
		t.Fatalf("Load() error = %v, want both-zero rejection", err)
	}
}

func TestLoadRejectsInvalidCallbackHTTPTimeoutAndLease(t *testing.T) {
	cleanGatewayEnv(t)
	t.Setenv("GO_GATEWAY_INTERNAL_SERVICE_TOKEN", "env-internal-service-token")
	t.Setenv("GO_GATEWAY_API_KEYS", "env-key")
	t.Setenv("GO_GATEWAY_CALLBACK_HTTP_TIMEOUT", "0s")

	_, err := Load()
	if err == nil {
		t.Fatal("Load() error = nil, want invalid callback HTTP timeout")
	}
	if !strings.Contains(err.Error(), "GO_GATEWAY_CALLBACK_HTTP_TIMEOUT") {
		t.Fatalf("Load() error = %v", err)
	}

	cleanGatewayEnv(t)
	t.Setenv("GO_GATEWAY_INTERNAL_SERVICE_TOKEN", "env-internal-service-token")
	t.Setenv("GO_GATEWAY_API_KEYS", "env-key")
	t.Setenv("GO_GATEWAY_CALLBACK_HTTP_TIMEOUT", "30s")
	t.Setenv("GO_GATEWAY_CALLBACK_LEASE_DURATION", "30s")

	_, err = Load()
	if err == nil {
		t.Fatal("Load() error = nil, want callback lease/timeout mismatch")
	}
	if !strings.Contains(err.Error(), "GO_GATEWAY_CALLBACK_LEASE_DURATION") {
		t.Fatalf("Load() error = %v", err)
	}
}

func TestLoadRejectsWeakDefaultGatewayKeys(t *testing.T) {
	cleanGatewayEnv(t)
	t.Setenv("GO_GATEWAY_INTERNAL_SERVICE_TOKEN", "env-internal-service-token")
	t.Setenv("GO_GATEWAY_API_KEYS", "public-key")

	_, err := Load()
	if err == nil {
		t.Fatal("Load() error = nil, want weak API key rejection")
	}
	if !strings.Contains(err.Error(), "GO_GATEWAY_API_KEYS contains weak default") {
		t.Fatalf("Load() error = %v", err)
	}

	cleanGatewayEnv(t)
	t.Setenv("GO_GATEWAY_INTERNAL_SERVICE_TOKEN", "env-internal-service-token")
	t.Setenv("GO_GATEWAY_SCOPED_API_KEYS", "runtime-key:runtime")

	_, err = Load()
	if err == nil {
		t.Fatal("Load() error = nil, want weak scoped API key rejection")
	}
	if !strings.Contains(err.Error(), "GO_GATEWAY_SCOPED_API_KEYS contains weak default") {
		t.Fatalf("Load() error = %v", err)
	}
}

func TestLoadAcceptsSentinelRedisWithoutStandaloneAddr(t *testing.T) {
	cleanGatewayEnv(t)
	t.Setenv("GO_GATEWAY_INTERNAL_SERVICE_TOKEN", "env-internal-service-token")
	t.Setenv("GO_GATEWAY_API_KEYS", "env-key")
	t.Setenv("GO_GATEWAY_REDIS_MODE", "sentinel")
	t.Setenv("GO_GATEWAY_REDIS_SENTINEL_ADDRS", "redis-sentinel:26379")
	t.Setenv("GO_GATEWAY_REDIS_SENTINEL_MASTER", "mymaster")
	t.Setenv("GO_GATEWAY_REDIS_REQUIRED", "true")
	t.Setenv("GO_GATEWAY_CHAT_RATE_LIMIT_ENABLED", "true")
	t.Setenv("GO_GATEWAY_CHAT_RATE_LIMIT_MAX", "600")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.RedisAddr != "" {
		t.Fatalf("RedisAddr = %q, want empty", cfg.RedisAddr)
	}
	if got, want := strings.Join(cfg.RedisSentinelAddrs, ","), "redis-sentinel:26379"; got != want {
		t.Fatalf("RedisSentinelAddrs = %q, want %q", got, want)
	}
}

func TestLoadExplicitMissingConfigFileErrors(t *testing.T) {
	cleanGatewayEnv(t)
	t.Setenv("GO_GATEWAY_CONFIG_FILE", filepath.Join(t.TempDir(), "missing.json"))
	t.Setenv("GO_GATEWAY_INTERNAL_SERVICE_TOKEN", "env-internal-service-token")
	t.Setenv("GO_GATEWAY_API_KEYS", "env-key")

	_, err := Load()
	if err == nil {
		t.Fatal("Load() error = nil, want missing config error")
	}
	if !strings.Contains(err.Error(), "read GO_GATEWAY_CONFIG_FILE") {
		t.Fatalf("Load() error = %v", err)
	}
}

func writeConfigFile(t *testing.T, path string, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("write config file: %v", err)
	}
}

func cleanGatewayEnv(t *testing.T) {
	t.Helper()
	for _, item := range os.Environ() {
		key, _, _ := strings.Cut(item, "=")
		if !strings.HasPrefix(key, "GO_GATEWAY_") &&
			!strings.HasPrefix(key, "IRONAGENT_") &&
			!strings.HasPrefix(key, "IRON_AGENT_") {
			continue
		}
		oldValue, existed := os.LookupEnv(key)
		if err := os.Unsetenv(key); err != nil {
			t.Fatalf("unset %s: %v", key, err)
		}
		t.Cleanup(func() {
			if existed {
				_ = os.Setenv(key, oldValue)
				return
			}
			_ = os.Unsetenv(key)
		})
	}
}
