package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"
)

const defaultConfigFile = "/etc/ironagent/go-gateway.json"

const (
	ProcessRoleAll            = "all"
	ProcessRoleAPI            = "api"
	ProcessRoleTaskWorker     = "task-worker"
	ProcessRoleCallbackWorker = "callback-worker"
)

type Config struct {
	ConfigFile                               string
	ListenAddr                               string
	Mode                                     string
	ProcessRole                              string
	SecurityBaseURL                          string
	AdminBaseURL                             string
	InternalServiceToken                     string
	DatabaseURL                              string
	DatabaseMaxOpenConns                     int
	DatabaseMaxIdleConns                     int
	DatabaseConnMaxLifetime                  time.Duration
	DatabaseConnMaxIdleTime                  time.Duration
	CallbackDatabaseMaxOpenConns             int
	CallbackDatabaseMaxIdleConns             int
	RedisMode                                string
	RedisAddr                                string
	RedisSentinelAddrs                       []string
	RedisSentinelMaster                      string
	RedisUsername                            string
	RedisPassword                            string
	RedisDB                                  int
	RedisKeyPrefix                           string
	RedisTimeout                             time.Duration
	RedisMaxOpenConns                        int
	RedisMaxIdleConns                        int
	RedisRequired                            bool
	AssistantCacheTTL                        time.Duration
	AssistantAvailabilityCacheTTL            time.Duration
	ChatRateLimitEnabled                     bool
	ChatRateLimitMax                         int
	ChatRateLimitWindow                      time.Duration
	ChatRateLimitFailClosed                  bool
	HTTPTimeout                              time.Duration
	APIKeys                                  []string
	ScopedAPIKeys                            []ScopedAPIKey
	HMACSecret                               string
	HMACAllowedSkew                          time.Duration
	PublicAuthDisabled                       bool
	AdminAuthDisabled                        bool
	PublicBaseURL                            string
	DefaultCallbackURL                       string
	CallbackClientID                         string
	CallbackSecret                           string
	CallbackHTTPTimeout                      time.Duration
	CallbackAllowedHosts                     []string
	CallbackAllowPrivate                     bool
	CallbackFireAndForget                    bool
	CallbackAcceptAnyResponse                bool
	CallbackRequired                         bool
	CallbackWorkerID                         string
	CallbackMaxAttempts                      int
	CallbackRetryDelay                       time.Duration
	CallbackMaxRetryDelay                    time.Duration
	CallbackQueueSize                        int
	CallbackStoreTimeout                     time.Duration
	CallbackEnqueueMaxInFlight               int
	CallbackDeliveryConcurrency              int
	CallbackWorkerInterval                   time.Duration
	CallbackPendingLimit                     int
	CallbackLeaseDuration                    time.Duration
	CallbackOutboxWorkerEnabled              bool
	PresignSecret                            string
	PresignTTL                               time.Duration
	MaxUploadBytes                           int64
	ProxySessionSecret                       string
	ProxySessionTTL                          time.Duration
	ProxyMaxInFlight                         int
	ProxyFailureThreshold                    int
	ProxyCircuitOpenDuration                 time.Duration
	RuntimeDefaultCapacity                   int
	RuntimeHeartbeatTTL                      time.Duration
	RuntimeBootstrapAssistants               []string
	RuntimeDiscoverAssistants                bool
	RuntimeSyncInterval                      time.Duration
	RuntimeExecutionEnabled                  bool
	RuntimeExecutionTimeout                  time.Duration
	TaskWorkerEnabled                        bool
	TaskWorkerID                             string
	TaskWorkerConcurrency                    int
	TaskWorkerInterval                       time.Duration
	TaskWorkerClaimLimit                     int
	TaskWorkerLeaseDuration                  time.Duration
	TaskWorkerDrainTimeout                   time.Duration
	TaskWorkerGatewayDefaultDeadlineBackstop time.Duration
	TaskWorkerMaxAttempts                    int
	TaskWorkerRetryDelay                     time.Duration
	TaskWorkerCapacityWaitMaxAttempts        int
	TaskWorkerCapacityWaitTimeout            time.Duration
	TaskWorkerCapacityWaitCallbackInterval   time.Duration
	TaskWorkerStaleProgressTimeout           time.Duration
	TaskWorkerActiveSouthboundDeferTimeout   time.Duration
	TaskProgressCallbackEnabled              bool
	TaskProgressCallbackBatchSize            int
	TaskProgressCallbackInterval             time.Duration
	TaskProgressCallbackStepChars            int
	TaskProgressCallbackStateTTL             time.Duration
	TaskQueueMaxPending                      int
	TaskQueueMaxPendingPerAssistant          int
	LogLevel                                 slog.Level
}

type ScopedAPIKey struct {
	Key    string   `json:"key"`
	Scopes []string `json:"scopes"`
}

func Load() (Config, error) {
	cfg := Config{
		ListenAddr:                               ":8080",
		Mode:                                     "polling",
		ProcessRole:                              ProcessRoleAll,
		SecurityBaseURL:                          "http://127.0.0.1:8000",
		AdminBaseURL:                             "http://127.0.0.1:8501",
		DatabaseMaxOpenConns:                     40,
		DatabaseMaxIdleConns:                     10,
		DatabaseConnMaxLifetime:                  30 * time.Minute,
		DatabaseConnMaxIdleTime:                  5 * time.Minute,
		CallbackDatabaseMaxOpenConns:             8,
		CallbackDatabaseMaxIdleConns:             2,
		RedisMode:                                "standalone",
		RedisKeyPrefix:                           "go-gateway:",
		RedisTimeout:                             500 * time.Millisecond,
		RedisMaxOpenConns:                        16,
		RedisMaxIdleConns:                        8,
		AssistantCacheTTL:                        60 * time.Second,
		AssistantAvailabilityCacheTTL:            30 * time.Second,
		ChatRateLimitWindow:                      time.Second,
		ChatRateLimitFailClosed:                  true,
		HTTPTimeout:                              10 * time.Second,
		HMACAllowedSkew:                          5 * time.Minute,
		PublicAuthDisabled:                       false,
		CallbackClientID:                         "ironagent-go-gateway",
		CallbackHTTPTimeout:                      10 * time.Second,
		CallbackMaxAttempts:                      3,
		CallbackAcceptAnyResponse:                false,
		CallbackRetryDelay:                       250 * time.Millisecond,
		CallbackMaxRetryDelay:                    30 * time.Second,
		CallbackQueueSize:                        1024,
		CallbackStoreTimeout:                     2 * time.Second,
		CallbackEnqueueMaxInFlight:               4,
		CallbackDeliveryConcurrency:              4,
		CallbackWorkerInterval:                   250 * time.Millisecond,
		CallbackPendingLimit:                     100,
		CallbackLeaseDuration:                    30 * time.Second,
		CallbackOutboxWorkerEnabled:              true,
		PresignTTL:                               15 * time.Minute,
		MaxUploadBytes:                           50 * 1024 * 1024,
		ProxySessionTTL:                          30 * time.Minute,
		ProxyFailureThreshold:                    3,
		ProxyCircuitOpenDuration:                 30 * time.Second,
		RuntimeDefaultCapacity:                   1,
		RuntimeHeartbeatTTL:                      60 * time.Second,
		RuntimeSyncInterval:                      30 * time.Second,
		RuntimeExecutionTimeout:                  10 * time.Minute,
		TaskWorkerID:                             "go-gateway-task-worker",
		TaskWorkerConcurrency:                    1,
		TaskWorkerInterval:                       250 * time.Millisecond,
		TaskWorkerClaimLimit:                     1,
		TaskWorkerLeaseDuration:                  30 * time.Second,
		TaskWorkerDrainTimeout:                   11 * time.Minute,
		TaskWorkerGatewayDefaultDeadlineBackstop: 20 * time.Minute,
		TaskWorkerMaxAttempts:                    3,
		TaskWorkerRetryDelay:                     time.Second,
		TaskWorkerCapacityWaitMaxAttempts:        100,
		TaskWorkerCapacityWaitTimeout:            30 * time.Minute,
		TaskWorkerCapacityWaitCallbackInterval:   5 * time.Minute,
		TaskWorkerStaleProgressTimeout:           600 * time.Second,
		TaskWorkerActiveSouthboundDeferTimeout:   60 * time.Minute,
		TaskProgressCallbackBatchSize:            5,
		TaskProgressCallbackInterval:             90 * time.Second,
		TaskProgressCallbackStepChars:            150,
		TaskProgressCallbackStateTTL:             30 * time.Minute,
		LogLevel:                                 slog.LevelInfo,
	}

	configFile := strings.TrimSpace(os.Getenv("GO_GATEWAY_CONFIG_FILE"))
	if configFile == "" {
		configFile = defaultConfigFile
	}
	if err := loadFileIfPresent(configFile, &cfg); err != nil {
		return Config{}, err
	}
	cfg.ConfigFile = configFile

	if err := applyEnv(&cfg); err != nil {
		return Config{}, err
	}

	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

func (c Config) Validate() error {
	if c.Mode != "polling" {
		return fmt.Errorf("unsupported GO_GATEWAY_MODE %q: phase 1 only supports polling", c.Mode)
	}
	switch c.ProcessRole {
	case ProcessRoleAll, ProcessRoleAPI, ProcessRoleTaskWorker, ProcessRoleCallbackWorker:
	default:
		return fmt.Errorf("unsupported GO_GATEWAY_PROCESS_ROLE %q", c.ProcessRole)
	}
	if c.SecurityBaseURL == "" {
		return fmt.Errorf("GO_GATEWAY_SECURITY_BASE_URL is required")
	}
	if c.AdminBaseURL == "" {
		return fmt.Errorf("GO_GATEWAY_ADMIN_BASE_URL is required")
	}
	if c.DatabaseMaxOpenConns <= 0 {
		return fmt.Errorf("GO_GATEWAY_DATABASE_MAX_OPEN_CONNS must be positive")
	}
	if c.DatabaseMaxIdleConns < 0 {
		return fmt.Errorf("GO_GATEWAY_DATABASE_MAX_IDLE_CONNS must be non-negative")
	}
	if c.DatabaseMaxOpenConns > 0 && c.DatabaseMaxIdleConns > c.DatabaseMaxOpenConns {
		return fmt.Errorf("GO_GATEWAY_DATABASE_MAX_IDLE_CONNS must be less than or equal to GO_GATEWAY_DATABASE_MAX_OPEN_CONNS")
	}
	if c.CallbackDatabaseMaxOpenConns <= 0 {
		return fmt.Errorf("GO_GATEWAY_CALLBACK_DATABASE_MAX_OPEN_CONNS must be positive")
	}
	if c.CallbackDatabaseMaxIdleConns < 0 {
		return fmt.Errorf("GO_GATEWAY_CALLBACK_DATABASE_MAX_IDLE_CONNS must be non-negative")
	}
	if c.CallbackDatabaseMaxOpenConns > 0 && c.CallbackDatabaseMaxIdleConns > c.CallbackDatabaseMaxOpenConns {
		return fmt.Errorf("GO_GATEWAY_CALLBACK_DATABASE_MAX_IDLE_CONNS must be less than or equal to GO_GATEWAY_CALLBACK_DATABASE_MAX_OPEN_CONNS")
	}
	if c.RedisMode == "" {
		return fmt.Errorf("GO_GATEWAY_REDIS_MODE is required")
	}
	if c.RedisMode != "standalone" && c.RedisMode != "sentinel" {
		return fmt.Errorf("unsupported GO_GATEWAY_REDIS_MODE %q", c.RedisMode)
	}
	if c.RedisRequired && !c.RedisEnabled() {
		return fmt.Errorf("redis connection is required when GO_GATEWAY_REDIS_REQUIRED is true")
	}
	if c.RedisEnabled() {
		if c.RedisTimeout <= 0 {
			return fmt.Errorf("GO_GATEWAY_REDIS_TIMEOUT must be positive")
		}
		if c.RedisMaxOpenConns <= 0 {
			return fmt.Errorf("GO_GATEWAY_REDIS_MAX_OPEN_CONNS must be positive")
		}
		if c.RedisMaxIdleConns < 0 {
			return fmt.Errorf("GO_GATEWAY_REDIS_MAX_IDLE_CONNS must be non-negative")
		}
		if c.RedisMaxIdleConns > c.RedisMaxOpenConns {
			return fmt.Errorf("GO_GATEWAY_REDIS_MAX_IDLE_CONNS must be less than or equal to GO_GATEWAY_REDIS_MAX_OPEN_CONNS")
		}
		if c.RedisDB < 0 {
			return fmt.Errorf("GO_GATEWAY_REDIS_DB must be non-negative")
		}
		if c.AssistantCacheTTL <= 0 {
			return fmt.Errorf("GO_GATEWAY_ASSISTANT_CACHE_TTL must be positive")
		}
		if c.AssistantAvailabilityCacheTTL <= 0 {
			return fmt.Errorf("GO_GATEWAY_ASSISTANT_AVAILABILITY_CACHE_TTL must be positive")
		}
		if c.RedisMode == "standalone" && c.RedisAddr == "" {
			return fmt.Errorf("GO_GATEWAY_REDIS_ADDR is required when GO_GATEWAY_REDIS_MODE=standalone")
		}
		if c.RedisMode == "sentinel" {
			if len(c.RedisSentinelAddrs) == 0 {
				return fmt.Errorf("GO_GATEWAY_REDIS_SENTINEL_ADDRS is required when GO_GATEWAY_REDIS_MODE=sentinel")
			}
			if c.RedisSentinelMaster == "" {
				return fmt.Errorf("GO_GATEWAY_REDIS_SENTINEL_MASTER is required when GO_GATEWAY_REDIS_MODE=sentinel")
			}
		}
	}
	if c.ChatRateLimitEnabled {
		if !c.RedisEnabled() {
			return fmt.Errorf("redis connection is required when GO_GATEWAY_CHAT_RATE_LIMIT_ENABLED is true")
		}
		if c.ChatRateLimitMax <= 0 {
			return fmt.Errorf("GO_GATEWAY_CHAT_RATE_LIMIT_MAX must be positive when chat rate limit is enabled")
		}
		if c.ChatRateLimitWindow <= 0 {
			return fmt.Errorf("GO_GATEWAY_CHAT_RATE_LIMIT_WINDOW must be positive when chat rate limit is enabled")
		}
	}
	if c.CallbackStoreTimeout <= 0 {
		return fmt.Errorf("GO_GATEWAY_CALLBACK_STORE_TIMEOUT must be positive")
	}
	if c.CallbackHTTPTimeout <= 0 {
		return fmt.Errorf("GO_GATEWAY_CALLBACK_HTTP_TIMEOUT must be positive")
	}
	if !c.CallbackFireAndForget && c.CallbackLeaseDuration <= c.CallbackHTTPTimeout {
		return fmt.Errorf("GO_GATEWAY_CALLBACK_LEASE_DURATION must be greater than GO_GATEWAY_CALLBACK_HTTP_TIMEOUT when callback fire-and-forget is disabled")
	}
	if c.CallbackEnqueueMaxInFlight <= 0 {
		return fmt.Errorf("GO_GATEWAY_CALLBACK_ENQUEUE_MAX_IN_FLIGHT must be positive")
	}
	if c.CallbackDeliveryConcurrency <= 0 {
		return fmt.Errorf("GO_GATEWAY_CALLBACK_DELIVERY_CONCURRENCY must be positive")
	}
	if c.DatabaseConnMaxLifetime < 0 {
		return fmt.Errorf("GO_GATEWAY_DATABASE_CONN_MAX_LIFETIME must be non-negative")
	}
	if c.DatabaseConnMaxIdleTime < 0 {
		return fmt.Errorf("GO_GATEWAY_DATABASE_CONN_MAX_IDLE_TIME must be non-negative")
	}
	if c.InternalServiceToken == "" {
		return fmt.Errorf("GO_GATEWAY_INTERNAL_SERVICE_TOKEN is required for approval closure")
	}
	if c.ProxyMaxInFlight < 0 {
		return fmt.Errorf("GO_GATEWAY_PROXY_MAX_IN_FLIGHT must be non-negative")
	}
	if c.ProxyFailureThreshold < 0 {
		return fmt.Errorf("GO_GATEWAY_PROXY_FAILURE_THRESHOLD must be non-negative")
	}
	if c.ProxyFailureThreshold > 0 && c.ProxyCircuitOpenDuration <= 0 {
		return fmt.Errorf("GO_GATEWAY_PROXY_CIRCUIT_OPEN_DURATION must be positive when proxy circuit breaker is enabled")
	}
	if c.RuntimeExecutionTimeout <= 0 {
		return fmt.Errorf("GO_GATEWAY_RUNTIME_EXECUTION_TIMEOUT must be positive")
	}
	if c.DefaultCallbackURL != "" && len(c.CallbackAllowedHosts) == 0 {
		return fmt.Errorf("GO_GATEWAY_CALLBACK_ALLOWED_HOSTS is required when GO_GATEWAY_DEFAULT_CALLBACK_URL is set")
	}
	if len(c.APIKeys) == 0 && len(c.ScopedAPIKeys) == 0 {
		return fmt.Errorf("GO_GATEWAY_API_KEYS or GO_GATEWAY_SCOPED_API_KEYS must contain at least one API key")
	}
	if err := validateNoWeakDefaultKeys(c.APIKeys, c.ScopedAPIKeys); err != nil {
		return err
	}
	if c.TaskWorkerEnabled {
		if c.TaskWorkerConcurrency <= 0 {
			return fmt.Errorf("GO_GATEWAY_TASK_WORKER_CONCURRENCY must be positive")
		}
		if c.TaskWorkerInterval <= 0 {
			return fmt.Errorf("GO_GATEWAY_TASK_WORKER_INTERVAL must be positive")
		}
		if c.TaskWorkerClaimLimit <= 0 {
			return fmt.Errorf("GO_GATEWAY_TASK_WORKER_CLAIM_LIMIT must be positive")
		}
		if c.TaskWorkerLeaseDuration <= 0 {
			return fmt.Errorf("GO_GATEWAY_TASK_WORKER_LEASE_DURATION must be positive")
		}
		if c.TaskWorkerDrainTimeout < 0 {
			return fmt.Errorf("GO_GATEWAY_TASK_WORKER_DRAIN_TIMEOUT must be non-negative")
		}
		if c.TaskWorkerGatewayDefaultDeadlineBackstop < 0 {
			return fmt.Errorf("GO_GATEWAY_TASK_WORKER_GATEWAY_DEFAULT_DEADLINE_BACKSTOP must be non-negative")
		}
		if c.TaskWorkerMaxAttempts <= 0 {
			return fmt.Errorf("GO_GATEWAY_TASK_WORKER_MAX_ATTEMPTS must be positive")
		}
		if c.TaskWorkerCapacityWaitMaxAttempts < 0 {
			return fmt.Errorf("GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_MAX_ATTEMPTS must be non-negative")
		}
		if c.TaskWorkerCapacityWaitTimeout < 0 {
			return fmt.Errorf("GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_TIMEOUT must be non-negative")
		}
		if c.TaskWorkerCapacityWaitCallbackInterval < 0 {
			return fmt.Errorf("GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_CALLBACK_INTERVAL must be non-negative")
		}
		if c.TaskWorkerActiveSouthboundDeferTimeout < 0 {
			return fmt.Errorf("GO_GATEWAY_TASK_WORKER_ACTIVE_SOUTHBOUND_DEFER_TIMEOUT must be non-negative")
		}
		if c.TaskWorkerCapacityWaitMaxAttempts == 0 && c.TaskWorkerCapacityWaitTimeout == 0 {
			return fmt.Errorf("GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_MAX_ATTEMPTS and GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_TIMEOUT must not both be zero: a capacity-waiting task would never terminate (set at least one bound)")
		}
		if c.TaskQueueMaxPendingPerAssistant < 0 {
			return fmt.Errorf("GO_GATEWAY_TASK_QUEUE_MAX_PENDING_PER_ASSISTANT must be non-negative")
		}
		if c.TaskWorkerStaleProgressTimeout < 0 {
			return fmt.Errorf("GO_GATEWAY_TASK_WORKER_STALE_PROGRESS_TIMEOUT must be non-negative")
		}
		if c.TaskProgressCallbackEnabled {
			if c.TaskProgressCallbackBatchSize <= 0 {
				return fmt.Errorf("GO_GATEWAY_TASK_PROGRESS_CALLBACK_BATCH_SIZE must be positive when progress callback is enabled")
			}
			if c.TaskProgressCallbackInterval <= 0 {
				return fmt.Errorf("GO_GATEWAY_TASK_PROGRESS_CALLBACK_INTERVAL must be positive when progress callback is enabled")
			}
			if c.TaskProgressCallbackStepChars <= 0 {
				return fmt.Errorf("GO_GATEWAY_TASK_PROGRESS_CALLBACK_STEP_MAX_CHARS must be positive when progress callback is enabled")
			}
			if c.TaskProgressCallbackStateTTL <= 0 {
				return fmt.Errorf("GO_GATEWAY_TASK_PROGRESS_CALLBACK_STATE_TTL must be positive when progress callback is enabled")
			}
		}
	}
	if (len(c.RuntimeBootstrapAssistants) > 0 || c.RuntimeDiscoverAssistants) && c.RuntimeSyncInterval <= 0 {
		return fmt.Errorf("GO_GATEWAY_RUNTIME_SYNC_INTERVAL must be positive when runtime bootstrap or discovery is enabled")
	}
	return nil
}

func (c Config) RedisEnabled() bool {
	if c.RedisMode == "sentinel" {
		return len(c.RedisSentinelAddrs) > 0 || c.RedisAddr != ""
	}
	return c.RedisAddr != ""
}

func (c Config) RunsTaskWorker() bool {
	return c.TaskWorkerEnabled && (c.ProcessRole == ProcessRoleAll || c.ProcessRole == ProcessRoleTaskWorker)
}

func (c Config) RunsCallbackOutboxWorker() bool {
	return c.CallbackOutboxWorkerEnabled && (c.ProcessRole == ProcessRoleAll || c.ProcessRole == ProcessRoleCallbackWorker)
}

func (c Config) RunsRuntimeSync() bool {
	if c.RuntimeSyncInterval <= 0 {
		return false
	}
	return c.ProcessRole == ProcessRoleAll || c.ProcessRole == ProcessRoleAPI || c.ProcessRole == ProcessRoleTaskWorker
}

type fileConfig struct {
	ListenAddr                               *string        `json:"listen_addr"`
	Mode                                     *string        `json:"mode"`
	ProcessRole                              *string        `json:"process_role"`
	SecurityBaseURL                          *string        `json:"security_base_url"`
	AdminBaseURL                             *string        `json:"admin_base_url"`
	InternalServiceToken                     *string        `json:"internal_service_token"`
	DatabaseURL                              *string        `json:"database_url"`
	DatabaseMaxOpenConns                     *int           `json:"database_max_open_conns"`
	DatabaseMaxIdleConns                     *int           `json:"database_max_idle_conns"`
	DatabaseConnMaxLifetime                  *string        `json:"database_conn_max_lifetime"`
	DatabaseConnMaxIdleTime                  *string        `json:"database_conn_max_idle_time"`
	CallbackDatabaseMaxOpenConns             *int           `json:"callback_database_max_open_conns"`
	CallbackDatabaseMaxIdleConns             *int           `json:"callback_database_max_idle_conns"`
	RedisMode                                *string        `json:"redis_mode"`
	RedisAddr                                *string        `json:"redis_addr"`
	RedisSentinelAddrs                       []string       `json:"redis_sentinel_addrs"`
	RedisSentinelMaster                      *string        `json:"redis_sentinel_master"`
	RedisUsername                            *string        `json:"redis_username"`
	RedisPassword                            *string        `json:"redis_password"`
	RedisDB                                  *int           `json:"redis_db"`
	RedisKeyPrefix                           *string        `json:"redis_key_prefix"`
	RedisTimeout                             *string        `json:"redis_timeout"`
	RedisMaxOpenConns                        *int           `json:"redis_max_open_conns"`
	RedisMaxIdleConns                        *int           `json:"redis_max_idle_conns"`
	RedisRequired                            *bool          `json:"redis_required"`
	AssistantCacheTTL                        *string        `json:"assistant_cache_ttl"`
	AssistantAvailabilityCacheTTL            *string        `json:"assistant_availability_cache_ttl"`
	ChatRateLimitEnabled                     *bool          `json:"chat_rate_limit_enabled"`
	ChatRateLimitMax                         *int           `json:"chat_rate_limit_max"`
	ChatRateLimitWindow                      *string        `json:"chat_rate_limit_window"`
	ChatRateLimitFailClosed                  *bool          `json:"chat_rate_limit_fail_closed"`
	HTTPTimeout                              *string        `json:"http_timeout"`
	APIKeys                                  []string       `json:"api_keys"`
	ScopedAPIKeys                            []ScopedAPIKey `json:"scoped_api_keys"`
	HMACSecret                               *string        `json:"hmac_secret"`
	HMACAllowedSkew                          *string        `json:"hmac_allowed_skew"`
	PublicAuthDisabled                       *bool          `json:"public_auth_disabled"`
	AdminAuthDisabled                        *bool          `json:"admin_auth_disabled"`
	PublicBaseURL                            *string        `json:"public_base_url"`
	DefaultCallbackURL                       *string        `json:"default_callback_url"`
	CallbackClientID                         *string        `json:"callback_client_id"`
	CallbackSecret                           *string        `json:"callback_secret"`
	CallbackHTTPTimeout                      *string        `json:"callback_http_timeout"`
	CallbackAllowedHosts                     []string       `json:"callback_allowed_hosts"`
	CallbackAllowPrivate                     *bool          `json:"callback_allow_private_hosts"`
	CallbackFireAndForget                    *bool          `json:"callback_fire_and_forget"`
	CallbackAcceptAnyResponse                *bool          `json:"callback_accept_any_response"`
	CallbackRequired                         *bool          `json:"callback_required"`
	CallbackWorkerID                         *string        `json:"callback_worker_id"`
	CallbackMaxAttempts                      *int           `json:"callback_max_attempts"`
	CallbackRetryDelay                       *string        `json:"callback_retry_delay"`
	CallbackMaxRetryDelay                    *string        `json:"callback_max_retry_delay"`
	CallbackQueueSize                        *int           `json:"callback_queue_size"`
	CallbackStoreTimeout                     *string        `json:"callback_store_timeout"`
	CallbackEnqueueMaxInFlight               *int           `json:"callback_enqueue_max_in_flight"`
	CallbackDeliveryConcurrency              *int           `json:"callback_delivery_concurrency"`
	CallbackWorkerInterval                   *string        `json:"callback_worker_interval"`
	CallbackPendingLimit                     *int           `json:"callback_pending_limit"`
	CallbackLeaseDuration                    *string        `json:"callback_lease_duration"`
	CallbackOutboxWorkerEnabled              *bool          `json:"callback_outbox_worker_enabled"`
	PresignSecret                            *string        `json:"presign_secret"`
	PresignTTL                               *string        `json:"presign_ttl"`
	MaxUploadBytes                           *int64         `json:"max_upload_bytes"`
	ProxySessionSecret                       *string        `json:"proxy_session_secret"`
	ProxySessionTTL                          *string        `json:"proxy_session_ttl"`
	ProxyMaxInFlight                         *int           `json:"proxy_max_in_flight"`
	ProxyFailureThreshold                    *int           `json:"proxy_failure_threshold"`
	ProxyCircuitOpenDuration                 *string        `json:"proxy_circuit_open_duration"`
	RuntimeDefaultCapacity                   *int           `json:"runtime_default_capacity"`
	RuntimeHeartbeatTTL                      *string        `json:"runtime_heartbeat_ttl"`
	RuntimeBootstrapAssistants               []string       `json:"runtime_bootstrap_assistants"`
	RuntimeDiscoverAssistants                *bool          `json:"runtime_discover_assistants"`
	RuntimeSyncInterval                      *string        `json:"runtime_sync_interval"`
	RuntimeExecutionEnabled                  *bool          `json:"runtime_execution_enabled"`
	RuntimeExecutionTimeout                  *string        `json:"runtime_execution_timeout"`
	TaskWorkerEnabled                        *bool          `json:"task_worker_enabled"`
	TaskWorkerID                             *string        `json:"task_worker_id"`
	TaskWorkerConcurrency                    *int           `json:"task_worker_concurrency"`
	TaskWorkerInterval                       *string        `json:"task_worker_interval"`
	TaskWorkerClaimLimit                     *int           `json:"task_worker_claim_limit"`
	TaskWorkerLeaseDuration                  *string        `json:"task_worker_lease_duration"`
	TaskWorkerDrainTimeout                   *string        `json:"task_worker_drain_timeout"`
	TaskWorkerGatewayDefaultDeadlineBackstop *string        `json:"task_worker_gateway_default_deadline_backstop"`
	TaskWorkerMaxAttempts                    *int           `json:"task_worker_max_attempts"`
	TaskWorkerRetryDelay                     *string        `json:"task_worker_retry_delay"`
	TaskWorkerCapacityWaitMaxAttempts        *int           `json:"task_worker_capacity_wait_max_attempts"`
	TaskWorkerCapacityWaitTimeout            *string        `json:"task_worker_capacity_wait_timeout"`
	TaskWorkerCapacityWaitCallbackInterval   *string        `json:"task_worker_capacity_wait_callback_interval"`
	TaskWorkerStaleProgressTimeout           *string        `json:"task_worker_stale_progress_timeout"`
	TaskWorkerActiveSouthboundDeferTimeout   *string        `json:"task_worker_active_southbound_defer_timeout"`
	TaskProgressCallbackEnabled              *bool          `json:"task_progress_callback_enabled"`
	TaskProgressCallbackBatchSize            *int           `json:"task_progress_callback_batch_size"`
	TaskProgressCallbackInterval             *string        `json:"task_progress_callback_interval"`
	TaskProgressCallbackStepChars            *int           `json:"task_progress_callback_step_max_chars"`
	TaskProgressCallbackStateTTL             *string        `json:"task_progress_callback_state_ttl"`
	TaskQueueMaxPending                      *int           `json:"task_queue_max_pending"`
	TaskQueueMaxPendingPerAssistant          *int           `json:"task_queue_max_pending_per_assistant"`
	LogLevel                                 *string        `json:"log_level"`
}

func loadFileIfPresent(path string, cfg *Config) error {
	if strings.TrimSpace(path) == "" {
		return nil
	}
	content, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) && path == defaultConfigFile && os.Getenv("GO_GATEWAY_CONFIG_FILE") == "" {
			return nil
		}
		return fmt.Errorf("read GO_GATEWAY_CONFIG_FILE %q: %w", path, err)
	}
	var fileCfg fileConfig
	if err := json.Unmarshal(content, &fileCfg); err != nil {
		return fmt.Errorf("parse GO_GATEWAY_CONFIG_FILE %q: %w", path, err)
	}
	return applyFileConfig(fileCfg, cfg)
}

func applyFileConfig(fileCfg fileConfig, cfg *Config) error {
	setString(fileCfg.ListenAddr, &cfg.ListenAddr)
	setString(fileCfg.Mode, &cfg.Mode)
	setString(fileCfg.ProcessRole, &cfg.ProcessRole)
	setString(fileCfg.SecurityBaseURL, &cfg.SecurityBaseURL)
	setString(fileCfg.AdminBaseURL, &cfg.AdminBaseURL)
	setString(fileCfg.InternalServiceToken, &cfg.InternalServiceToken)
	setString(fileCfg.DatabaseURL, &cfg.DatabaseURL)
	setString(fileCfg.RedisMode, &cfg.RedisMode)
	setString(fileCfg.RedisAddr, &cfg.RedisAddr)
	setString(fileCfg.RedisSentinelMaster, &cfg.RedisSentinelMaster)
	setString(fileCfg.RedisUsername, &cfg.RedisUsername)
	setString(fileCfg.RedisPassword, &cfg.RedisPassword)
	setString(fileCfg.RedisKeyPrefix, &cfg.RedisKeyPrefix)
	setString(fileCfg.HMACSecret, &cfg.HMACSecret)
	setString(fileCfg.PublicBaseURL, &cfg.PublicBaseURL)
	setString(fileCfg.DefaultCallbackURL, &cfg.DefaultCallbackURL)
	setString(fileCfg.CallbackClientID, &cfg.CallbackClientID)
	setString(fileCfg.CallbackSecret, &cfg.CallbackSecret)
	setString(fileCfg.CallbackWorkerID, &cfg.CallbackWorkerID)
	setString(fileCfg.PresignSecret, &cfg.PresignSecret)
	setString(fileCfg.ProxySessionSecret, &cfg.ProxySessionSecret)
	setString(fileCfg.TaskWorkerID, &cfg.TaskWorkerID)
	if fileCfg.APIKeys != nil {
		cfg.APIKeys = compactStrings(fileCfg.APIKeys)
	}
	if fileCfg.ScopedAPIKeys != nil {
		cfg.ScopedAPIKeys = normalizeScopedAPIKeys(fileCfg.ScopedAPIKeys)
	}
	if fileCfg.CallbackAllowedHosts != nil {
		cfg.CallbackAllowedHosts = compactStrings(fileCfg.CallbackAllowedHosts)
	}
	if fileCfg.RedisSentinelAddrs != nil {
		cfg.RedisSentinelAddrs = compactStrings(fileCfg.RedisSentinelAddrs)
	}
	if fileCfg.RuntimeBootstrapAssistants != nil {
		cfg.RuntimeBootstrapAssistants = compactStrings(fileCfg.RuntimeBootstrapAssistants)
	}
	if fileCfg.CallbackAllowPrivate != nil {
		cfg.CallbackAllowPrivate = *fileCfg.CallbackAllowPrivate
	}
	if fileCfg.CallbackFireAndForget != nil {
		cfg.CallbackFireAndForget = *fileCfg.CallbackFireAndForget
	}
	if fileCfg.CallbackAcceptAnyResponse != nil {
		cfg.CallbackAcceptAnyResponse = *fileCfg.CallbackAcceptAnyResponse
	}
	if fileCfg.CallbackRequired != nil {
		cfg.CallbackRequired = *fileCfg.CallbackRequired
	}
	if fileCfg.CallbackOutboxWorkerEnabled != nil {
		cfg.CallbackOutboxWorkerEnabled = *fileCfg.CallbackOutboxWorkerEnabled
	}
	if fileCfg.PublicAuthDisabled != nil {
		cfg.PublicAuthDisabled = *fileCfg.PublicAuthDisabled
	}
	if fileCfg.AdminAuthDisabled != nil {
		cfg.AdminAuthDisabled = *fileCfg.AdminAuthDisabled
	}
	if fileCfg.CallbackMaxAttempts != nil {
		cfg.CallbackMaxAttempts = *fileCfg.CallbackMaxAttempts
	}
	if fileCfg.CallbackQueueSize != nil {
		cfg.CallbackQueueSize = *fileCfg.CallbackQueueSize
	}
	if fileCfg.CallbackEnqueueMaxInFlight != nil {
		cfg.CallbackEnqueueMaxInFlight = *fileCfg.CallbackEnqueueMaxInFlight
	}
	if fileCfg.CallbackDeliveryConcurrency != nil {
		cfg.CallbackDeliveryConcurrency = *fileCfg.CallbackDeliveryConcurrency
	}
	if fileCfg.CallbackPendingLimit != nil {
		cfg.CallbackPendingLimit = *fileCfg.CallbackPendingLimit
	}
	if fileCfg.MaxUploadBytes != nil {
		cfg.MaxUploadBytes = *fileCfg.MaxUploadBytes
	}
	if fileCfg.ProxyMaxInFlight != nil {
		cfg.ProxyMaxInFlight = *fileCfg.ProxyMaxInFlight
	}
	if fileCfg.ProxyFailureThreshold != nil {
		cfg.ProxyFailureThreshold = *fileCfg.ProxyFailureThreshold
	}
	if fileCfg.RuntimeDefaultCapacity != nil {
		cfg.RuntimeDefaultCapacity = *fileCfg.RuntimeDefaultCapacity
	}
	if fileCfg.RuntimeDiscoverAssistants != nil {
		cfg.RuntimeDiscoverAssistants = *fileCfg.RuntimeDiscoverAssistants
	}
	if fileCfg.RuntimeExecutionEnabled != nil {
		cfg.RuntimeExecutionEnabled = *fileCfg.RuntimeExecutionEnabled
	}
	if fileCfg.TaskWorkerEnabled != nil {
		cfg.TaskWorkerEnabled = *fileCfg.TaskWorkerEnabled
	}
	if fileCfg.TaskProgressCallbackEnabled != nil {
		cfg.TaskProgressCallbackEnabled = *fileCfg.TaskProgressCallbackEnabled
	}
	if fileCfg.TaskWorkerClaimLimit != nil {
		cfg.TaskWorkerClaimLimit = *fileCfg.TaskWorkerClaimLimit
	}
	if fileCfg.TaskWorkerConcurrency != nil {
		cfg.TaskWorkerConcurrency = *fileCfg.TaskWorkerConcurrency
	}
	if fileCfg.TaskWorkerMaxAttempts != nil {
		cfg.TaskWorkerMaxAttempts = *fileCfg.TaskWorkerMaxAttempts
	}
	if fileCfg.TaskWorkerCapacityWaitMaxAttempts != nil {
		cfg.TaskWorkerCapacityWaitMaxAttempts = *fileCfg.TaskWorkerCapacityWaitMaxAttempts
	}
	if fileCfg.TaskQueueMaxPending != nil {
		cfg.TaskQueueMaxPending = *fileCfg.TaskQueueMaxPending
	}
	if fileCfg.TaskQueueMaxPendingPerAssistant != nil {
		cfg.TaskQueueMaxPendingPerAssistant = *fileCfg.TaskQueueMaxPendingPerAssistant
	}
	if fileCfg.TaskProgressCallbackBatchSize != nil {
		cfg.TaskProgressCallbackBatchSize = *fileCfg.TaskProgressCallbackBatchSize
	}
	if fileCfg.TaskProgressCallbackStepChars != nil {
		cfg.TaskProgressCallbackStepChars = *fileCfg.TaskProgressCallbackStepChars
	}
	if fileCfg.DatabaseMaxOpenConns != nil {
		cfg.DatabaseMaxOpenConns = *fileCfg.DatabaseMaxOpenConns
	}
	if fileCfg.DatabaseMaxIdleConns != nil {
		cfg.DatabaseMaxIdleConns = *fileCfg.DatabaseMaxIdleConns
	}
	if err := setDuration(fileCfg.DatabaseConnMaxLifetime, &cfg.DatabaseConnMaxLifetime, "database_conn_max_lifetime"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.DatabaseConnMaxIdleTime, &cfg.DatabaseConnMaxIdleTime, "database_conn_max_idle_time"); err != nil {
		return err
	}
	if fileCfg.CallbackDatabaseMaxOpenConns != nil {
		cfg.CallbackDatabaseMaxOpenConns = *fileCfg.CallbackDatabaseMaxOpenConns
	}
	if fileCfg.CallbackDatabaseMaxIdleConns != nil {
		cfg.CallbackDatabaseMaxIdleConns = *fileCfg.CallbackDatabaseMaxIdleConns
	}
	if fileCfg.RedisDB != nil {
		cfg.RedisDB = *fileCfg.RedisDB
	}
	if fileCfg.RedisMaxOpenConns != nil {
		cfg.RedisMaxOpenConns = *fileCfg.RedisMaxOpenConns
	}
	if fileCfg.RedisMaxIdleConns != nil {
		cfg.RedisMaxIdleConns = *fileCfg.RedisMaxIdleConns
	}
	if fileCfg.RedisRequired != nil {
		cfg.RedisRequired = *fileCfg.RedisRequired
	}
	if fileCfg.ChatRateLimitEnabled != nil {
		cfg.ChatRateLimitEnabled = *fileCfg.ChatRateLimitEnabled
	}
	if fileCfg.ChatRateLimitMax != nil {
		cfg.ChatRateLimitMax = *fileCfg.ChatRateLimitMax
	}
	if fileCfg.ChatRateLimitFailClosed != nil {
		cfg.ChatRateLimitFailClosed = *fileCfg.ChatRateLimitFailClosed
	}
	if err := setDuration(fileCfg.RedisTimeout, &cfg.RedisTimeout, "redis_timeout"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.AssistantCacheTTL, &cfg.AssistantCacheTTL, "assistant_cache_ttl"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.AssistantAvailabilityCacheTTL, &cfg.AssistantAvailabilityCacheTTL, "assistant_availability_cache_ttl"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.ChatRateLimitWindow, &cfg.ChatRateLimitWindow, "chat_rate_limit_window"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.RuntimeHeartbeatTTL, &cfg.RuntimeHeartbeatTTL, "runtime_heartbeat_ttl"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.RuntimeSyncInterval, &cfg.RuntimeSyncInterval, "runtime_sync_interval"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.RuntimeExecutionTimeout, &cfg.RuntimeExecutionTimeout, "runtime_execution_timeout"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.TaskWorkerInterval, &cfg.TaskWorkerInterval, "task_worker_interval"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.TaskWorkerLeaseDuration, &cfg.TaskWorkerLeaseDuration, "task_worker_lease_duration"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.TaskWorkerDrainTimeout, &cfg.TaskWorkerDrainTimeout, "task_worker_drain_timeout"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.TaskWorkerGatewayDefaultDeadlineBackstop, &cfg.TaskWorkerGatewayDefaultDeadlineBackstop, "task_worker_gateway_default_deadline_backstop"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.TaskWorkerRetryDelay, &cfg.TaskWorkerRetryDelay, "task_worker_retry_delay"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.TaskWorkerCapacityWaitTimeout, &cfg.TaskWorkerCapacityWaitTimeout, "task_worker_capacity_wait_timeout"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.TaskWorkerActiveSouthboundDeferTimeout, &cfg.TaskWorkerActiveSouthboundDeferTimeout, "task_worker_active_southbound_defer_timeout"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.TaskWorkerCapacityWaitCallbackInterval, &cfg.TaskWorkerCapacityWaitCallbackInterval, "task_worker_capacity_wait_callback_interval"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.TaskWorkerStaleProgressTimeout, &cfg.TaskWorkerStaleProgressTimeout, "task_worker_stale_progress_timeout"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.TaskProgressCallbackInterval, &cfg.TaskProgressCallbackInterval, "task_progress_callback_interval"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.TaskProgressCallbackStateTTL, &cfg.TaskProgressCallbackStateTTL, "task_progress_callback_state_ttl"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.HTTPTimeout, &cfg.HTTPTimeout, "http_timeout"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.HMACAllowedSkew, &cfg.HMACAllowedSkew, "hmac_allowed_skew"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.CallbackRetryDelay, &cfg.CallbackRetryDelay, "callback_retry_delay"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.CallbackMaxRetryDelay, &cfg.CallbackMaxRetryDelay, "callback_max_retry_delay"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.CallbackStoreTimeout, &cfg.CallbackStoreTimeout, "callback_store_timeout"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.CallbackHTTPTimeout, &cfg.CallbackHTTPTimeout, "callback_http_timeout"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.CallbackWorkerInterval, &cfg.CallbackWorkerInterval, "callback_worker_interval"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.CallbackLeaseDuration, &cfg.CallbackLeaseDuration, "callback_lease_duration"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.PresignTTL, &cfg.PresignTTL, "presign_ttl"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.ProxySessionTTL, &cfg.ProxySessionTTL, "proxy_session_ttl"); err != nil {
		return err
	}
	if err := setDuration(fileCfg.ProxyCircuitOpenDuration, &cfg.ProxyCircuitOpenDuration, "proxy_circuit_open_duration"); err != nil {
		return err
	}
	if fileCfg.LogLevel != nil {
		level, err := parseLogLevel(*fileCfg.LogLevel)
		if err != nil {
			return err
		}
		cfg.LogLevel = level
	}
	normalize(cfg)
	return nil
}

func applyEnv(cfg *Config) error {
	if value, ok := envValue("GO_GATEWAY_LISTEN_ADDR"); ok {
		cfg.ListenAddr = value
	}
	if value, ok := envValue("GO_GATEWAY_MODE"); ok {
		cfg.Mode = value
	}
	if value, ok := envValue("GO_GATEWAY_PROCESS_ROLE"); ok {
		cfg.ProcessRole = value
	}
	if value, ok := envValue("GO_GATEWAY_SECURITY_BASE_URL"); ok {
		cfg.SecurityBaseURL = value
	}
	if value, ok := envValue("GO_GATEWAY_ADMIN_BASE_URL"); ok {
		cfg.AdminBaseURL = value
	}
	if value, ok := envValue("GO_GATEWAY_INTERNAL_SERVICE_TOKEN"); ok {
		cfg.InternalServiceToken = value
	} else if value, ok := envValueAny(
		"IRONAGENT_INTERNAL_SERVICE_TOKEN",
		"IRON_AGENT_INTERNAL_SERVICE_TOKEN",
	); ok {
		cfg.InternalServiceToken = value
	}
	if value, ok := envValue("GO_GATEWAY_DATABASE_URL"); ok {
		cfg.DatabaseURL = value
	} else if value, ok := envValueAny(
		"IRONAGENT_DATABASE_URL",
		"IRON_AGENT_DATABASE_URL",
	); ok {
		cfg.DatabaseURL = value
	}
	if value, ok := envValue("GO_GATEWAY_REDIS_MODE"); ok {
		cfg.RedisMode = value
	}
	if value, ok := envValue("GO_GATEWAY_REDIS_ADDR"); ok {
		cfg.RedisAddr = value
	}
	if value, ok := envValue("GO_GATEWAY_REDIS_SENTINEL_ADDRS"); ok {
		cfg.RedisSentinelAddrs = splitCSV(value)
	}
	if value, ok := envValue("GO_GATEWAY_REDIS_SENTINEL_MASTER"); ok {
		cfg.RedisSentinelMaster = value
	}
	if value, ok := envValue("GO_GATEWAY_REDIS_USERNAME"); ok {
		cfg.RedisUsername = value
	}
	if value, ok := envValue("GO_GATEWAY_REDIS_PASSWORD"); ok {
		cfg.RedisPassword = value
	}
	if value, ok := envValue("GO_GATEWAY_REDIS_KEY_PREFIX"); ok {
		cfg.RedisKeyPrefix = value
	}
	if value, ok := envValue("GO_GATEWAY_HMAC_SECRET"); ok {
		cfg.HMACSecret = value
	}
	if value, ok := envValue("GO_GATEWAY_PUBLIC_BASE_URL"); ok {
		cfg.PublicBaseURL = value
	}
	if value, ok := envValue("GO_GATEWAY_DEFAULT_CALLBACK_URL"); ok {
		cfg.DefaultCallbackURL = value
	}
	if value, ok := envValue("GO_GATEWAY_PROXY_SESSION_SECRET"); ok {
		cfg.ProxySessionSecret = value
	}
	if value, ok := envValue("GO_GATEWAY_CALLBACK_CLIENT_ID"); ok {
		cfg.CallbackClientID = value
	}
	if value, ok := envValue("GO_GATEWAY_CALLBACK_SECRET"); ok {
		cfg.CallbackSecret = value
	}
	if value, ok := envValue("GO_GATEWAY_CALLBACK_WORKER_ID"); ok {
		cfg.CallbackWorkerID = value
	}
	if value, ok := envValue("GO_GATEWAY_PRESIGN_SECRET"); ok {
		cfg.PresignSecret = value
	}
	if value, ok := envValue("GO_GATEWAY_TASK_WORKER_ID"); ok {
		cfg.TaskWorkerID = value
	}
	if value, ok := envValue("GO_GATEWAY_API_KEYS"); ok {
		cfg.APIKeys = splitCSV(value)
	}
	if value, ok := envValue("GO_GATEWAY_SCOPED_API_KEYS"); ok {
		scoped, err := parseScopedAPIKeys(value)
		if err != nil {
			return err
		}
		cfg.ScopedAPIKeys = scoped
	}
	if value, ok := envValue("GO_GATEWAY_CALLBACK_ALLOWED_HOSTS"); ok {
		cfg.CallbackAllowedHosts = splitCSV(value)
	}
	if value, ok := envValue("GO_GATEWAY_RUNTIME_BOOTSTRAP_ASSISTANTS"); ok {
		cfg.RuntimeBootstrapAssistants = splitCSV(value)
	}
	if err := boolEnv("GO_GATEWAY_CALLBACK_ALLOW_PRIVATE_HOSTS", &cfg.CallbackAllowPrivate); err != nil {
		return err
	}
	if err := boolEnv("GO_GATEWAY_CALLBACK_FIRE_AND_FORGET", &cfg.CallbackFireAndForget); err != nil {
		return err
	}
	if err := boolEnv("GO_GATEWAY_CALLBACK_ACCEPT_ANY_RESPONSE", &cfg.CallbackAcceptAnyResponse); err != nil {
		return err
	}
	if err := boolEnv("GO_GATEWAY_CALLBACK_REQUIRED", &cfg.CallbackRequired); err != nil {
		return err
	}
	if err := boolEnv("GO_GATEWAY_CALLBACK_OUTBOX_WORKER_ENABLED", &cfg.CallbackOutboxWorkerEnabled); err != nil {
		return err
	}
	if err := boolEnv("GO_GATEWAY_PUBLIC_AUTH_DISABLED", &cfg.PublicAuthDisabled); err != nil {
		return err
	}
	if err := boolEnv("GO_GATEWAY_ADMIN_AUTH_DISABLED", &cfg.AdminAuthDisabled); err != nil {
		return err
	}
	if err := boolEnv("GO_GATEWAY_TASK_WORKER_ENABLED", &cfg.TaskWorkerEnabled); err != nil {
		return err
	}
	if err := boolEnv("GO_GATEWAY_TASK_PROGRESS_CALLBACK_ENABLED", &cfg.TaskProgressCallbackEnabled); err != nil {
		return err
	}
	if err := boolEnv("GO_GATEWAY_RUNTIME_DISCOVER_ASSISTANTS", &cfg.RuntimeDiscoverAssistants); err != nil {
		return err
	}
	if err := boolEnv("GO_GATEWAY_RUNTIME_EXECUTION_ENABLED", &cfg.RuntimeExecutionEnabled); err != nil {
		return err
	}
	if err := boolEnv("GO_GATEWAY_REDIS_REQUIRED", &cfg.RedisRequired); err != nil {
		return err
	}
	if err := boolEnv("GO_GATEWAY_CHAT_RATE_LIMIT_ENABLED", &cfg.ChatRateLimitEnabled); err != nil {
		return err
	}
	if err := boolEnv("GO_GATEWAY_CHAT_RATE_LIMIT_FAIL_CLOSED", &cfg.ChatRateLimitFailClosed); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_HTTP_TIMEOUT", &cfg.HTTPTimeout); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_REDIS_TIMEOUT", &cfg.RedisTimeout); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_ASSISTANT_CACHE_TTL", &cfg.AssistantCacheTTL); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_ASSISTANT_AVAILABILITY_CACHE_TTL", &cfg.AssistantAvailabilityCacheTTL); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_CHAT_RATE_LIMIT_WINDOW", &cfg.ChatRateLimitWindow); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_HMAC_ALLOWED_SKEW", &cfg.HMACAllowedSkew); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_CALLBACK_RETRY_DELAY", &cfg.CallbackRetryDelay); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_CALLBACK_MAX_RETRY_DELAY", &cfg.CallbackMaxRetryDelay); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_CALLBACK_STORE_TIMEOUT", &cfg.CallbackStoreTimeout); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_CALLBACK_HTTP_TIMEOUT", &cfg.CallbackHTTPTimeout); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_CALLBACK_WORKER_INTERVAL", &cfg.CallbackWorkerInterval); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_CALLBACK_LEASE_DURATION", &cfg.CallbackLeaseDuration); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_PRESIGN_TTL", &cfg.PresignTTL); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_PROXY_SESSION_TTL", &cfg.ProxySessionTTL); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_PROXY_CIRCUIT_OPEN_DURATION", &cfg.ProxyCircuitOpenDuration); err != nil {
		return err
	}
	if err := intEnv("GO_GATEWAY_CALLBACK_MAX_ATTEMPTS", &cfg.CallbackMaxAttempts); err != nil {
		return err
	}
	if err := intEnv("GO_GATEWAY_CALLBACK_QUEUE_SIZE", &cfg.CallbackQueueSize); err != nil {
		return err
	}
	if err := intEnv("GO_GATEWAY_CALLBACK_ENQUEUE_MAX_IN_FLIGHT", &cfg.CallbackEnqueueMaxInFlight); err != nil {
		return err
	}
	if err := intEnv("GO_GATEWAY_CALLBACK_DELIVERY_CONCURRENCY", &cfg.CallbackDeliveryConcurrency); err != nil {
		return err
	}
	if err := intEnv("GO_GATEWAY_CALLBACK_PENDING_LIMIT", &cfg.CallbackPendingLimit); err != nil {
		return err
	}
	if err := intEnv("GO_GATEWAY_DATABASE_MAX_OPEN_CONNS", &cfg.DatabaseMaxOpenConns); err != nil {
		return err
	}
	if err := intEnv("GO_GATEWAY_DATABASE_MAX_IDLE_CONNS", &cfg.DatabaseMaxIdleConns); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_DATABASE_CONN_MAX_LIFETIME", &cfg.DatabaseConnMaxLifetime); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_DATABASE_CONN_MAX_IDLE_TIME", &cfg.DatabaseConnMaxIdleTime); err != nil {
		return err
	}
	if err := intEnv("GO_GATEWAY_CALLBACK_DATABASE_MAX_OPEN_CONNS", &cfg.CallbackDatabaseMaxOpenConns); err != nil {
		return err
	}
	if err := intEnv("GO_GATEWAY_CALLBACK_DATABASE_MAX_IDLE_CONNS", &cfg.CallbackDatabaseMaxIdleConns); err != nil {
		return err
	}
	if err := intEnv("GO_GATEWAY_REDIS_DB", &cfg.RedisDB); err != nil {
		return err
	}
	if err := intEnv("GO_GATEWAY_REDIS_MAX_OPEN_CONNS", &cfg.RedisMaxOpenConns); err != nil {
		return err
	}
	if err := intEnv("GO_GATEWAY_REDIS_MAX_IDLE_CONNS", &cfg.RedisMaxIdleConns); err != nil {
		return err
	}
	if err := intEnv("GO_GATEWAY_CHAT_RATE_LIMIT_MAX", &cfg.ChatRateLimitMax); err != nil {
		return err
	}
	if err := intEnv("GO_GATEWAY_PROXY_MAX_IN_FLIGHT", &cfg.ProxyMaxInFlight); err != nil {
		return err
	}
	if err := intEnv("GO_GATEWAY_PROXY_FAILURE_THRESHOLD", &cfg.ProxyFailureThreshold); err != nil {
		return err
	}
	if err := int64Env("GO_GATEWAY_MAX_UPLOAD_BYTES", &cfg.MaxUploadBytes); err != nil {
		return err
	}
	if err := intEnv("GO_GATEWAY_RUNTIME_DEFAULT_CAPACITY", &cfg.RuntimeDefaultCapacity); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_RUNTIME_HEARTBEAT_TTL", &cfg.RuntimeHeartbeatTTL); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_RUNTIME_SYNC_INTERVAL", &cfg.RuntimeSyncInterval); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_RUNTIME_EXECUTION_TIMEOUT", &cfg.RuntimeExecutionTimeout); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_TASK_WORKER_INTERVAL", &cfg.TaskWorkerInterval); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_TASK_WORKER_LEASE_DURATION", &cfg.TaskWorkerLeaseDuration); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_TASK_WORKER_DRAIN_TIMEOUT", &cfg.TaskWorkerDrainTimeout); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_TASK_WORKER_GATEWAY_DEFAULT_DEADLINE_BACKSTOP", &cfg.TaskWorkerGatewayDefaultDeadlineBackstop); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_TASK_WORKER_RETRY_DELAY", &cfg.TaskWorkerRetryDelay); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_TIMEOUT", &cfg.TaskWorkerCapacityWaitTimeout); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_TASK_WORKER_ACTIVE_SOUTHBOUND_DEFER_TIMEOUT", &cfg.TaskWorkerActiveSouthboundDeferTimeout); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_CALLBACK_INTERVAL", &cfg.TaskWorkerCapacityWaitCallbackInterval); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_TASK_WORKER_STALE_PROGRESS_TIMEOUT", &cfg.TaskWorkerStaleProgressTimeout); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_TASK_PROGRESS_CALLBACK_INTERVAL", &cfg.TaskProgressCallbackInterval); err != nil {
		return err
	}
	if err := durationEnv("GO_GATEWAY_TASK_PROGRESS_CALLBACK_STATE_TTL", &cfg.TaskProgressCallbackStateTTL); err != nil {
		return err
	}
	if err := intEnv("GO_GATEWAY_TASK_WORKER_CLAIM_LIMIT", &cfg.TaskWorkerClaimLimit); err != nil {
		return err
	}
	if err := intEnv("GO_GATEWAY_TASK_WORKER_CONCURRENCY", &cfg.TaskWorkerConcurrency); err != nil {
		return err
	}
	if err := intEnv("GO_GATEWAY_TASK_WORKER_MAX_ATTEMPTS", &cfg.TaskWorkerMaxAttempts); err != nil {
		return err
	}
	if err := intEnv("GO_GATEWAY_TASK_WORKER_CAPACITY_WAIT_MAX_ATTEMPTS", &cfg.TaskWorkerCapacityWaitMaxAttempts); err != nil {
		return err
	}
	if err := intEnv("GO_GATEWAY_TASK_QUEUE_MAX_PENDING", &cfg.TaskQueueMaxPending); err != nil {
		return err
	}
	if err := intEnv("GO_GATEWAY_TASK_QUEUE_MAX_PENDING_PER_ASSISTANT", &cfg.TaskQueueMaxPendingPerAssistant); err != nil {
		return err
	}
	if err := intEnv("GO_GATEWAY_TASK_PROGRESS_CALLBACK_BATCH_SIZE", &cfg.TaskProgressCallbackBatchSize); err != nil {
		return err
	}
	if err := intEnv("GO_GATEWAY_TASK_PROGRESS_CALLBACK_STEP_MAX_CHARS", &cfg.TaskProgressCallbackStepChars); err != nil {
		return err
	}
	if value, ok := envValue("GO_GATEWAY_LOG_LEVEL"); ok {
		level, err := parseLogLevel(value)
		if err != nil {
			return err
		}
		cfg.LogLevel = level
	}
	normalize(cfg)
	return nil
}

func setString(source *string, target *string) {
	if source != nil {
		*target = strings.TrimSpace(*source)
	}
}

func setDuration(source *string, target *time.Duration, field string) error {
	if source == nil {
		return nil
	}
	value, err := time.ParseDuration(strings.TrimSpace(*source))
	if err != nil {
		return fmt.Errorf("%s: %w", field, err)
	}
	*target = value
	return nil
}

func normalize(cfg *Config) {
	cfg.ProcessRole = strings.ToLower(strings.TrimSpace(cfg.ProcessRole))
	if cfg.ProcessRole == "" {
		cfg.ProcessRole = ProcessRoleAll
	}
	cfg.SecurityBaseURL = strings.TrimRight(strings.TrimSpace(cfg.SecurityBaseURL), "/")
	cfg.AdminBaseURL = strings.TrimRight(strings.TrimSpace(cfg.AdminBaseURL), "/")
	cfg.PublicBaseURL = strings.TrimRight(strings.TrimSpace(cfg.PublicBaseURL), "/")
	cfg.DefaultCallbackURL = strings.TrimSpace(cfg.DefaultCallbackURL)
	cfg.InternalServiceToken = strings.TrimSpace(cfg.InternalServiceToken)
	cfg.DatabaseURL = strings.TrimSpace(cfg.DatabaseURL)
	cfg.RedisMode = strings.ToLower(strings.TrimSpace(cfg.RedisMode))
	cfg.RedisAddr = strings.TrimSpace(cfg.RedisAddr)
	cfg.RedisSentinelAddrs = compactStrings(cfg.RedisSentinelAddrs)
	cfg.RedisSentinelMaster = strings.TrimSpace(cfg.RedisSentinelMaster)
	cfg.RedisUsername = strings.TrimSpace(cfg.RedisUsername)
	cfg.RedisKeyPrefix = strings.TrimSpace(cfg.RedisKeyPrefix)
	cfg.HMACSecret = strings.TrimSpace(cfg.HMACSecret)
	cfg.CallbackClientID = strings.TrimSpace(cfg.CallbackClientID)
	cfg.CallbackSecret = strings.TrimSpace(cfg.CallbackSecret)
	cfg.CallbackWorkerID = strings.TrimSpace(cfg.CallbackWorkerID)
	cfg.PresignSecret = strings.TrimSpace(cfg.PresignSecret)
	cfg.ProxySessionSecret = strings.TrimSpace(cfg.ProxySessionSecret)
	cfg.TaskWorkerID = strings.TrimSpace(cfg.TaskWorkerID)
	cfg.APIKeys = compactStrings(cfg.APIKeys)
	cfg.ScopedAPIKeys = normalizeScopedAPIKeys(cfg.ScopedAPIKeys)
	cfg.CallbackAllowedHosts = compactStrings(cfg.CallbackAllowedHosts)
	cfg.RuntimeBootstrapAssistants = compactStrings(cfg.RuntimeBootstrapAssistants)
}

func compactStrings(values []string) []string {
	out := make([]string, 0, len(values))
	for _, value := range values {
		trimmed := strings.TrimSpace(value)
		if trimmed != "" {
			out = append(out, trimmed)
		}
	}
	return out
}

func normalizeScopedAPIKeys(values []ScopedAPIKey) []ScopedAPIKey {
	out := make([]ScopedAPIKey, 0, len(values))
	for _, value := range values {
		key := strings.TrimSpace(value.Key)
		if key == "" {
			continue
		}
		scopes := compactStrings(value.Scopes)
		for i, scope := range scopes {
			scopes[i] = strings.ToLower(scope)
		}
		if len(scopes) == 0 {
			scopes = []string{"public"}
		}
		out = append(out, ScopedAPIKey{Key: key, Scopes: scopes})
	}
	return out
}

func validateNoWeakDefaultKeys(apiKeys []string, scopedAPIKeys []ScopedAPIKey) error {
	weak := map[string]string{
		"public-key":  "public",
		"runtime-key": "runtime",
		"debug-key":   "debug",
		"admin-key":   "admin",
		"proxy-key":   "proxy",
		"change-me":   "placeholder",
	}
	for _, key := range apiKeys {
		if label, ok := weak[strings.ToLower(strings.TrimSpace(key))]; ok {
			return fmt.Errorf("GO_GATEWAY_API_KEYS contains weak default %q (%s scope); use a generated secret", key, label)
		}
	}
	for _, item := range scopedAPIKeys {
		if label, ok := weak[strings.ToLower(strings.TrimSpace(item.Key))]; ok {
			return fmt.Errorf("GO_GATEWAY_SCOPED_API_KEYS contains weak default %q (%s scope); use a generated secret", item.Key, label)
		}
	}
	return nil
}

func parseScopedAPIKeys(raw string) ([]ScopedAPIKey, error) {
	if strings.TrimSpace(raw) == "" {
		return nil, nil
	}
	entries := strings.Split(raw, ";")
	scoped := make([]ScopedAPIKey, 0, len(entries))
	for _, entry := range entries {
		entry = strings.TrimSpace(entry)
		if entry == "" {
			continue
		}
		key, rawScopes, ok := strings.Cut(entry, ":")
		if !ok {
			return nil, fmt.Errorf("GO_GATEWAY_SCOPED_API_KEYS entry %q must be key:scope[,scope]", entry)
		}
		key = strings.TrimSpace(key)
		if key == "" {
			return nil, fmt.Errorf("GO_GATEWAY_SCOPED_API_KEYS entry %q has empty key", entry)
		}
		scopes := compactStrings(strings.Split(rawScopes, ","))
		if len(scopes) == 0 {
			return nil, fmt.Errorf("GO_GATEWAY_SCOPED_API_KEYS entry %q has empty scopes", entry)
		}
		scoped = append(scoped, ScopedAPIKey{Key: key, Scopes: scopes})
	}
	return normalizeScopedAPIKeys(scoped), nil
}

func envValue(key string) (string, bool) {
	value, ok := os.LookupEnv(key)
	if !ok {
		return "", false
	}
	return strings.TrimSpace(value), true
}

func envValueAny(keys ...string) (string, bool) {
	for _, key := range keys {
		if value, ok := envValue(key); ok {
			return value, true
		}
	}
	return "", false
}

func durationEnv(key string, target *time.Duration) error {
	value, ok := envValue(key)
	if !ok {
		return nil
	}
	parsed, err := time.ParseDuration(value)
	if err != nil {
		return fmt.Errorf("%s: %w", key, err)
	}
	*target = parsed
	return nil
}

func intEnv(key string, target *int) error {
	value, ok := envValue(key)
	if !ok {
		return nil
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return fmt.Errorf("%s: %w", key, err)
	}
	*target = parsed
	return nil
}

func boolEnv(key string, target *bool) error {
	value, ok := envValue(key)
	if !ok {
		return nil
	}
	parsed, err := strconv.ParseBool(value)
	if err != nil {
		return fmt.Errorf("%s: %w", key, err)
	}
	*target = parsed
	return nil
}

func int64Env(key string, target *int64) error {
	value, ok := envValue(key)
	if !ok {
		return nil
	}
	parsed, err := strconv.ParseInt(value, 10, 64)
	if err != nil {
		return fmt.Errorf("%s: %w", key, err)
	}
	*target = parsed
	return nil
}

func splitCSV(raw string) []string {
	parts := strings.Split(raw, ",")
	values := make([]string, 0, len(parts))
	for _, part := range parts {
		trimmed := strings.TrimSpace(part)
		if trimmed != "" {
			values = append(values, trimmed)
		}
	}
	return values
}

func parseLogLevel(raw string) (slog.Level, error) {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "", "info":
		return slog.LevelInfo, nil
	case "debug":
		return slog.LevelDebug, nil
	case "warn", "warning":
		return slog.LevelWarn, nil
	case "error":
		return slog.LevelError, nil
	default:
		if numeric, err := strconv.Atoi(raw); err == nil {
			return slog.Level(numeric), nil
		}
		return slog.LevelInfo, fmt.Errorf("invalid GO_GATEWAY_LOG_LEVEL %q", raw)
	}
}
