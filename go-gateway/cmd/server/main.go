package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"ironagent/go-gateway/internal/auth"
	"ironagent/go-gateway/internal/config"
	"ironagent/go-gateway/internal/core"
	"ironagent/go-gateway/internal/httpapi"
	"ironagent/go-gateway/internal/mcpapi"
	"ironagent/go-gateway/internal/presign"
	"ironagent/go-gateway/internal/rediscache"
	"ironagent/go-gateway/internal/runtimeexec"
	"ironagent/go-gateway/internal/runtimepool"
	"ironagent/go-gateway/internal/southbound"
	"ironagent/go-gateway/internal/taskstore"
	"ironagent/go-gateway/internal/webhook"
)

func main() {
	cfg, err := config.Load()
	if err != nil {
		slog.Error("load config failed", "error", err)
		os.Exit(1)
	}

	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level: cfg.LogLevel,
	}))

	sb := southbound.NewClient(southbound.Config{
		SecurityBaseURL:      cfg.SecurityBaseURL,
		AdminBaseURL:         cfg.AdminBaseURL,
		InternalServiceToken: cfg.InternalServiceToken,
		HTTPTimeout:          cfg.HTTPTimeout,
	})
	runtimes := runtimepool.NewRegistryWithStore(cfg.RuntimeDefaultCapacity, cfg.RuntimeHeartbeatTTL, nil, logger)
	memoryStore := taskstore.NewMemoryStore()
	var tasks core.TaskStore = memoryStore
	var taskQueue core.TaskQueue
	if cfg.TaskWorkerEnabled {
		taskQueue = memoryStore
	}
	var outbox webhook.OutboxStore = webhook.NewMemoryOutboxStore()
	var nonceStore auth.NonceStore
	dbPoolStatsProviders := map[string]core.DBPoolStatsProvider{}
	if cfg.DatabaseURL != "" {
		ctx, cancel := context.WithTimeout(context.Background(), cfg.HTTPTimeout)
		sqlTasks, err := taskstore.NewMySQLStoreFromURLWithPool(ctx, cfg.DatabaseURL, taskstore.DBPoolConfig{
			MaxOpenConns:    cfg.DatabaseMaxOpenConns,
			MaxIdleConns:    cfg.DatabaseMaxIdleConns,
			ConnMaxLifetime: cfg.DatabaseConnMaxLifetime,
			ConnMaxIdleTime: cfg.DatabaseConnMaxIdleTime,
		})
		cancel()
		if err != nil {
			logger.Error("init gateway task store failed", "error", err)
			os.Exit(1)
		}
		defer sqlTasks.Close()
		tasks = sqlTasks
		if cfg.TaskWorkerEnabled {
			taskQueue = sqlTasks
		}
		nonceStore = sqlTasks
		runtimes = runtimepool.NewRegistryWithStore(cfg.RuntimeDefaultCapacity, cfg.RuntimeHeartbeatTTL, sqlTasks, logger)

		callbackCtx, callbackCancel := context.WithTimeout(context.Background(), cfg.HTTPTimeout)
		sqlOutbox, err := taskstore.NewMySQLStoreFromURLWithPool(callbackCtx, cfg.DatabaseURL, taskstore.DBPoolConfig{
			MaxOpenConns:    cfg.CallbackDatabaseMaxOpenConns,
			MaxIdleConns:    cfg.CallbackDatabaseMaxIdleConns,
			ConnMaxLifetime: cfg.DatabaseConnMaxLifetime,
			ConnMaxIdleTime: cfg.DatabaseConnMaxIdleTime,
		})
		callbackCancel()
		if err != nil {
			logger.Error("init gateway callback outbox store failed", "error", err)
			os.Exit(1)
		}
		defer sqlOutbox.Close()
		outbox = sqlOutbox
		dbPoolStatsProviders["callback_outbox"] = sqlOutbox
	}
	service := core.NewServiceWithOptions(sb, runtimes, tasks)
	if cfg.TaskWorkerEnabled {
		service = core.NewServiceWithQueue(sb, runtimes, tasks, taskQueue)
		service.SetTaskQueueMaxPending(cfg.TaskQueueMaxPending)
		service.SetTaskQueueMaxPendingPerAssistant(cfg.TaskQueueMaxPendingPerAssistant)
	}
	service.SetDefaultTaskTimeout(cfg.RuntimeExecutionTimeout)
	service.SetRuntimeRouteResolver(runtimes)
	service.SetLogger(logger)
	var redisClient *rediscache.Client
	if cfg.RedisEnabled() {
		ctx, cancel := context.WithTimeout(context.Background(), cfg.RedisTimeout)
		client, err := rediscache.New(ctx, rediscache.Config{
			Mode:                     cfg.RedisMode,
			Addr:                     cfg.RedisAddr,
			SentinelAddrs:            cfg.RedisSentinelAddrs,
			SentinelMaster:           cfg.RedisSentinelMaster,
			Username:                 cfg.RedisUsername,
			Password:                 cfg.RedisPassword,
			DB:                       cfg.RedisDB,
			KeyPrefix:                cfg.RedisKeyPrefix,
			Timeout:                  cfg.RedisTimeout,
			MaxOpenConns:             cfg.RedisMaxOpenConns,
			MaxIdleConns:             cfg.RedisMaxIdleConns,
			AssistantResolutionTTL:   cfg.AssistantCacheTTL,
			AssistantAvailabilityTTL: cfg.AssistantAvailabilityCacheTTL,
			ChatRateLimitMax:         cfg.ChatRateLimitMax,
			ChatRateLimitWindow:      cfg.ChatRateLimitWindow,
			ChatRateLimitFailClosed:  cfg.ChatRateLimitFailClosed,
		})
		cancel()
		if err != nil {
			if cfg.RedisRequired || cfg.ChatRateLimitEnabled {
				logger.Error("init redis cache failed", "error", err)
				os.Exit(1)
			}
			logger.Warn("redis cache disabled after init failure", "error", err)
		} else {
			redisClient = client
			defer redisClient.Close()
			service.SetAssistantResolutionCache(redisClient)
			service.SetTaskProgressStore(redisClient)
			if cfg.ChatRateLimitEnabled {
				service.SetChatRateLimiter(redisClient)
			}
		}
	}
	if cfg.RuntimeExecutionEnabled {
		runtimeExecutor := runtimeexec.NewIronAgentWebSocketExecutor(logger)
		if cfg.TaskWorkerGatewayDefaultDeadlineBackstop > 0 {
			runtimeExecutor.DeadlineBackstop = cfg.TaskWorkerGatewayDefaultDeadlineBackstop
		}
		service.SetRuntimeExecutor(runtimeExecutor)
	}
	if cfg.RuntimeDiscoverAssistants {
		service.SetRuntimeRefresher(func(ctx context.Context, assistantID string) error {
			runtimeView, err := service.GetRuntime(ctx, core.RuntimeRequest{AssistantID: assistantID})
			if err != nil {
				return err
			}
			return recordRuntimeView(runtimes, runtimeView)
		})
	}
	callbackSecret := cfg.CallbackSecret
	if callbackSecret == "" {
		callbackSecret = cfg.HMACSecret
	}
	presignSecret := cfg.PresignSecret
	if presignSecret == "" {
		presignSecret = cfg.HMACSecret
	}
	proxySessionSecret := cfg.ProxySessionSecret
	if proxySessionSecret == "" {
		proxySessionSecret = cfg.HMACSecret
	}
	if proxySessionSecret == "" {
		proxySessionSecret = presignSecret
	}
	webhooks := webhook.NewDispatcher(webhook.Config{
		ClientID:            cfg.CallbackClientID,
		Secret:              callbackSecret,
		MaxAttempts:         cfg.CallbackMaxAttempts,
		RetryDelay:          cfg.CallbackRetryDelay,
		MaxRetryDelay:       cfg.CallbackMaxRetryDelay,
		HTTPTimeout:         cfg.CallbackHTTPTimeout,
		FireAndForget:       cfg.CallbackFireAndForget,
		AcceptAnyResponse:   cfg.CallbackAcceptAnyResponse,
		QueueSize:           cfg.CallbackQueueSize,
		StoreTimeout:        cfg.CallbackStoreTimeout,
		EnqueueMaxInFlight:  cfg.CallbackEnqueueMaxInFlight,
		DeliveryConcurrency: cfg.CallbackDeliveryConcurrency,
		Store:               outbox,
		WorkerInterval:      cfg.CallbackWorkerInterval,
		PendingLimit:        cfg.CallbackPendingLimit,
		LeaseDuration:       cfg.CallbackLeaseDuration,
		DisableOutboxWorker: !cfg.RunsCallbackOutboxWorker(),
		WorkerID:            cfg.CallbackWorkerID,
		Logger:              logger,
	})
	presigns := presign.NewManager(presign.Config{
		Secret: presignSecret,
		TTL:    cfg.PresignTTL,
	})

	router := httpapi.NewRouterWithOptions(
		logger,
		service,
		auth.NewMiddleware(auth.Config{
			APIKeys:                    cfg.APIKeys,
			ScopedAPIKeys:              scopedAuthKeys(cfg.ScopedAPIKeys),
			HMACSecret:                 cfg.HMACSecret,
			AllowedSkew:                cfg.HMACAllowedSkew,
			MaxBodyBytes:               cfg.MaxUploadBytes + (1 << 20),
			NonceStore:                 nonceStore,
			AllowPublicUnauthenticated: cfg.PublicAuthDisabled,
			Logger:                     logger,
		}),
		mcpapi.NewHandlerWithPresigner(logger, service, presigns, cfg.PublicBaseURL),
		httpapi.RouterOptions{
			Webhooks:                    webhooks,
			Presigns:                    presigns,
			RuntimeRegistry:             runtimes,
			PublicBaseURL:               cfg.PublicBaseURL,
			AdminBaseURL:                cfg.AdminBaseURL,
			AdminAuthDisabled:           cfg.AdminAuthDisabled,
			InternalServiceToken:        cfg.InternalServiceToken,
			SecurityBaseURL:             cfg.SecurityBaseURL,
			DefaultCallbackURL:          cfg.DefaultCallbackURL,
			CallbackRequired:            cfg.CallbackRequired,
			CallbackAllowedHosts:        cfg.CallbackAllowedHosts,
			CallbackAllowPrivate:        cfg.CallbackAllowPrivate,
			MaxUploadBytes:              cfg.MaxUploadBytes,
			ProxySessionSecret:          proxySessionSecret,
			ProxySessionTTL:             cfg.ProxySessionTTL,
			ProxyMaxInFlight:            cfg.ProxyMaxInFlight,
			ProxyFailureThreshold:       cfg.ProxyFailureThreshold,
			ProxyCircuitOpenDuration:    cfg.ProxyCircuitOpenDuration,
			DBPoolStatsProviders:        dbPoolStatsProviders,
			TaskProgressCallbackEnabled: cfg.TaskProgressCallbackEnabled,
		},
	)

	server := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           router,
		ReadHeaderTimeout: 5 * time.Second,
	}

	workerCtx, stopWorkers := context.WithCancel(context.Background())
	defer stopWorkers()
	if cfg.RunsRuntimeSync() {
		go runRuntimeSync(workerCtx, logger, service, runtimes, cfg)
	}
	var taskWorkers *sync.WaitGroup
	if cfg.RunsTaskWorker() {
		taskWorkers = startTaskWorkers(workerCtx, logger, service, cfg)
	}

	go func() {
		logger.Info(
			"go gateway listening",
			"addr", cfg.ListenAddr,
			"mode", cfg.Mode,
			"process_role", cfg.ProcessRole,
			"task_worker_enabled", cfg.RunsTaskWorker(),
			"task_worker_concurrency", cfg.TaskWorkerConcurrency,
			"callback_outbox_worker_enabled", cfg.RunsCallbackOutboxWorker(),
			"callback_required", cfg.CallbackRequired,
			"default_callback_url_configured", strings.TrimSpace(cfg.DefaultCallbackURL) != "",
			"callback_allowed_hosts_count", len(cfg.CallbackAllowedHosts),
			"callback_allow_private_hosts", cfg.CallbackAllowPrivate,
			"callback_fire_and_forget", cfg.CallbackFireAndForget,
			"callback_accept_any_response", cfg.CallbackAcceptAnyResponse,
			"task_progress_callback_enabled", cfg.TaskProgressCallbackEnabled,
			"task_progress_callback_batch_size", cfg.TaskProgressCallbackBatchSize,
			"task_progress_callback_interval", cfg.TaskProgressCallbackInterval.String(),
			"security_base_url", cfg.SecurityBaseURL,
			"admin_base_url", cfg.AdminBaseURL,
		)
		if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Error("server stopped unexpectedly", "error", err)
			os.Exit(1)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
	logger.Info("shutdown signal received, stopping background claims")
	stopWorkers()
	if taskWorkers != nil {
		waitTaskWorkers(logger, taskWorkers, cfg.TaskWorkerDrainTimeout)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := server.Shutdown(ctx); err != nil {
		logger.Error("shutdown failed", "error", err)
		os.Exit(1)
	}

	logger.Info("server stopped")
}

func scopedAuthKeys(values []config.ScopedAPIKey) []auth.ScopedAPIKey {
	keys := make([]auth.ScopedAPIKey, 0, len(values))
	for _, value := range values {
		keys = append(keys, auth.ScopedAPIKey{
			Key:    value.Key,
			Scopes: append([]string(nil), value.Scopes...),
		})
	}
	return keys
}

func runRuntimeSync(
	ctx context.Context,
	logger *slog.Logger,
	service *core.Service,
	runtimes *runtimepool.Registry,
	cfg config.Config,
) {
	syncOnce := func() {
		assistantIDs := map[string]struct{}{}
		for _, assistantID := range cfg.RuntimeBootstrapAssistants {
			assistantID = strings.TrimSpace(assistantID)
			if assistantID == "" {
				continue
			}
			assistantIDs[assistantID] = struct{}{}
		}
		for _, record := range runtimes.Snapshot() {
			assistantID := strings.TrimSpace(record.AssistantID)
			if assistantID == "" {
				continue
			}
			assistantIDs[assistantID] = struct{}{}
		}
		if cfg.RuntimeDiscoverAssistants {
			timeout := cfg.HTTPTimeout
			if timeout <= 0 {
				timeout = 10 * time.Second
			}
			requestCtx, cancel := context.WithTimeout(ctx, timeout)
			discoveredIDs, err := service.ListRuntimeAssistantIDs(requestCtx)
			cancel()
			if err != nil {
				logger.Warn("runtime assistant discovery failed", "error", err)
			}
			for _, assistantID := range discoveredIDs {
				assistantID = strings.TrimSpace(assistantID)
				if assistantID == "" {
					continue
				}
				assistantIDs[assistantID] = struct{}{}
			}
		}
		for assistantID := range assistantIDs {
			timeout := cfg.HTTPTimeout
			if timeout <= 0 {
				timeout = 10 * time.Second
			}
			requestCtx, cancel := context.WithTimeout(ctx, timeout)
			runtimeView, err := service.GetRuntime(requestCtx, core.RuntimeRequest{AssistantID: assistantID})
			cancel()
			if err != nil {
				var httpErr *southbound.HTTPError
				if errors.As(err, &httpErr) && httpErr.StatusCode == http.StatusNotFound {
					if deleteErr := runtimes.Delete(assistantID); deleteErr != nil {
						logger.Warn(
							"runtime registry delete failed",
							"assistant_id",
							assistantID,
							"error",
							deleteErr,
						)
						continue
					}
					logger.Info("runtime registry removed missing assistant", "assistant_id", assistantID)
					continue
				}
				logger.Warn("runtime sync failed", "assistant_id", assistantID, "error", err)
				continue
			}
			if err := recordRuntimeView(runtimes, runtimeView); err != nil {
				logger.Warn("runtime registry update failed", "assistant_id", assistantID, "error", err)
				continue
			}
			logger.Debug("runtime synced", "assistant_id", assistantID, "status", runtimeView.Status)
		}
	}

	syncOnce()
	ticker := time.NewTicker(cfg.RuntimeSyncInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			syncOnce()
		}
	}
}

func recordRuntimeView(registry *runtimepool.Registry, runtimeView core.RuntimeView) error {
	if registry == nil || strings.TrimSpace(runtimeView.AssistantID) == "" {
		return nil
	}
	record, ok := registry.Get(runtimeView.AssistantID)
	if !ok {
		record = runtimepool.RuntimeRecord{AssistantID: runtimeView.AssistantID}
	}
	record.RuntimeID = runtimeView.ContainerID
	record.PodName = runtimeView.ContainerName
	record.InternalBaseURL = runtimeView.GatewayURL
	record.GatewayURL = runtimeView.GatewayURL
	record.Status = runtimeView.Status
	record.LastError = runtimeView.LastError
	if runtimeViewAvailable(runtimeView) {
		record.DrainMode = false
	}
	record.UpdatedAt = time.Now().UTC()
	_, err := registry.Heartbeat(record)
	return err
}

func runtimeViewAvailable(runtimeView core.RuntimeView) bool {
	switch strings.ToLower(strings.TrimSpace(runtimeView.Status)) {
	case "ready", "running", "healthy":
		return strings.TrimSpace(runtimeView.GatewayURL) != ""
	default:
		return false
	}
}

func startTaskWorkers(ctx context.Context, logger *slog.Logger, service *core.Service, cfg config.Config) *sync.WaitGroup {
	concurrency := cfg.TaskWorkerConcurrency
	if concurrency <= 0 {
		concurrency = 1
	}
	var wg sync.WaitGroup
	for i := 0; i < concurrency; i++ {
		workerID := cfg.TaskWorkerID
		if concurrency > 1 {
			workerID = fmt.Sprintf("%s-%02d", cfg.TaskWorkerID, i+1)
		}
		wg.Add(1)
		go func(workerID string) {
			defer wg.Done()
			runTaskWorker(ctx, logger, service, cfg, workerID)
		}(workerID)
	}
	return &wg
}

func waitTaskWorkers(logger *slog.Logger, wg *sync.WaitGroup, timeout time.Duration) {
	done := make(chan struct{})
	go func() {
		defer close(done)
		wg.Wait()
	}()
	if timeout <= 0 {
		<-done
		logger.Info("task workers drained")
		return
	}
	select {
	case <-done:
		logger.Info("task workers drained")
	case <-time.After(timeout):
		logger.Warn("task worker drain timeout reached", "timeout", timeout.String())
	}
}

func runTaskWorker(ctx context.Context, logger *slog.Logger, service *core.Service, cfg config.Config, workerID string) {
	ticker := time.NewTicker(cfg.TaskWorkerInterval)
	defer ticker.Stop()
	options := core.TaskWorkerOptions{
		WorkerID:                         workerID,
		ClaimLimit:                       cfg.TaskWorkerClaimLimit,
		LeaseDuration:                    cfg.TaskWorkerLeaseDuration,
		MaxAttempts:                      cfg.TaskWorkerMaxAttempts,
		RetryDelay:                       cfg.TaskWorkerRetryDelay,
		CapacityWaitMaxAttempts:          cfg.TaskWorkerCapacityWaitMaxAttempts,
		CapacityWaitTimeout:              cfg.TaskWorkerCapacityWaitTimeout,
		CapacityWaitCallbackInterval:     cfg.TaskWorkerCapacityWaitCallbackInterval,
		StaleProgressTimeout:             cfg.TaskWorkerStaleProgressTimeout,
		ActiveSouthboundDeferTimeout:     cfg.TaskWorkerActiveSouthboundDeferTimeout,
		TaskProgressCallbackEnabled:      cfg.TaskProgressCallbackEnabled,
		TaskProgressCallbackBatchSize:    cfg.TaskProgressCallbackBatchSize,
		TaskProgressCallbackInterval:     cfg.TaskProgressCallbackInterval,
		TaskProgressCallbackStepMaxRunes: cfg.TaskProgressCallbackStepChars,
		TaskProgressCallbackStateTTL:     cfg.TaskProgressCallbackStateTTL,
	}
	for {
		processed, err := service.ProcessQueuedTasks(ctx, options)
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			logger.Error("task worker cycle failed", "worker_id", workerID, "error", err)
		} else if processed > 0 {
			logger.Debug("task worker processed queued tasks", "worker_id", workerID, "count", processed)
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}
