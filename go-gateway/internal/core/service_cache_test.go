package core

import (
	"context"
	"errors"
	"net/http"
	"strings"
	"testing"
	"time"

	"ironagent/go-gateway/internal/southbound"
)

func TestResolveAssistantIDUsesSharedCache(t *testing.T) {
	ctx := context.Background()
	sb := &cacheTestSouthbound{}
	cache := &cacheTestAssistantCache{
		assistantByUser: map[string]string{"user-a": "assistant-a"},
	}
	service := NewService(sb)
	service.SetAssistantResolutionCache(cache)

	assistantID, err := service.resolveAssistantIDForUser(ctx, "user-a")
	if err != nil {
		t.Fatalf("resolveAssistantIDForUser() error = %v", err)
	}
	if assistantID != "assistant-a" {
		t.Fatalf("assistantID = %q, want assistant-a", assistantID)
	}
	if sb.listCalls != 0 || sb.getCalls != 0 {
		t.Fatalf("southbound calls = list:%d get:%d, want none", sb.listCalls, sb.getCalls)
	}
}

func TestResolveAssistantIDStoresSharedCache(t *testing.T) {
	ctx := context.Background()
	sb := &cacheTestSouthbound{
		bindings: map[string][]southbound.UserAssistantBinding{
			"user-a": {
				{UserID: "user-a", AssistantID: "assistant-a", BoundAt: southbound.Time{Time: time.Now().UTC()}},
			},
		},
		assistants: map[string]southbound.Assistant{
			"assistant-a": {AssistantID: "assistant-a"},
		},
	}
	cache := &cacheTestAssistantCache{assistantByUser: map[string]string{}, assistantAvailable: map[string]bool{}}
	service := NewService(sb)
	service.SetAssistantResolutionCache(cache)

	assistantID, err := service.resolveAssistantIDForUser(ctx, "user-a")
	if err != nil {
		t.Fatalf("resolveAssistantIDForUser() error = %v", err)
	}
	if assistantID != "assistant-a" {
		t.Fatalf("assistantID = %q, want assistant-a", assistantID)
	}
	if cache.assistantByUser["user-a"] != "assistant-a" {
		t.Fatalf("shared assistant cache = %#v", cache.assistantByUser)
	}
	available, ok := cache.assistantAvailable["assistant-a"]
	if !ok || !available {
		t.Fatalf("shared availability cache = %#v", cache.assistantAvailable)
	}
	if sb.listCalls != 1 || sb.getCalls != 1 {
		t.Fatalf("southbound calls = list:%d get:%d, want 1/1", sb.listCalls, sb.getCalls)
	}
}

func TestCreateChatTaskChecksRateLimitBeforeStores(t *testing.T) {
	ctx := context.Background()
	service := NewService(&cacheTestSouthbound{})
	limiter := &cacheTestRateLimiter{allowed: true, blockedKey: "global"}
	service.SetChatRateLimiter(limiter)

	_, err := service.CreateChatTask(ctx, ChatRequest{
		ClientID:  "client-a",
		RequestID: "request-a",
		UserID:    "user-a",
		Message:   "hello",
	})
	var queueErr *QueueFullError
	if !errors.As(err, &queueErr) {
		t.Fatalf("CreateChatTask() error = %T %v, want QueueFullError", err, err)
	}
	if got, want := strings.Join(limiter.keys, ","), "global"; got != want {
		t.Fatalf("rate limit keys = %q, want %q", got, want)
	}
}

func TestCreateChatTaskChecksQueueCapacityBeforeAssistantResolution(t *testing.T) {
	ctx := context.Background()
	sb := &cacheTestSouthbound{}
	service := NewServiceWithQueue(sb, nil, nil, cacheTestQueue{stats: TaskQueueStats{Queued: 1}})
	service.SetTaskQueueMaxPending(1)

	_, err := service.CreateChatTask(ctx, ChatRequest{
		UserID:  "user-a",
		Message: "hello",
	})
	var queueErr *QueueFullError
	if !errors.As(err, &queueErr) {
		t.Fatalf("CreateChatTask() error = %T %v, want QueueFullError", err, err)
	}
	if sb.listCalls != 0 || sb.getCalls != 0 {
		t.Fatalf("southbound calls = list:%d get:%d, want none", sb.listCalls, sb.getCalls)
	}
}

func TestCreateChatTaskRejectsTargetAssistantQueueFull(t *testing.T) {
	ctx := context.Background()
	queue := cacheTestQueue{
		assistantStats: map[string]TaskQueueStats{
			"assistant-full": {Queued: 1, Processing: 1},
		},
	}
	service := NewServiceWithQueue(&cacheTestSouthbound{}, nil, nil, queue)
	service.SetTaskQueueMaxPendingPerAssistant(2)

	_, err := service.CreateChatTask(ctx, ChatRequest{
		UserID:            "user-a",
		TargetAssistantID: "assistant-full",
		Message:           "hello",
	})
	var queueErr *QueueFullError
	if !errors.As(err, &queueErr) {
		t.Fatalf("CreateChatTask() error = %T %v, want QueueFullError", err, err)
	}
	if queueErr.Message != "target assistant task queue is full" {
		t.Fatalf("QueueFullError message = %q", queueErr.Message)
	}
}

func TestCheckChatAdmissionRejectsTargetAssistantQueueFull(t *testing.T) {
	ctx := context.Background()
	queue := cacheTestQueue{
		assistantStats: map[string]TaskQueueStats{
			"assistant-full": {Queued: 2},
		},
	}
	service := NewServiceWithQueue(&cacheTestSouthbound{}, nil, nil, queue)
	service.SetTaskQueueMaxPendingPerAssistant(2)

	err := service.CheckChatAdmission(ctx, ChatRequest{
		UserID:            "user-a",
		TargetAssistantID: "assistant-full",
		Message:           "hello",
	})
	var queueErr *QueueFullError
	if !errors.As(err, &queueErr) {
		t.Fatalf("CheckChatAdmission() error = %T %v, want QueueFullError", err, err)
	}
	if queueErr.Message != "target assistant task queue is full" {
		t.Fatalf("QueueFullError message = %q", queueErr.Message)
	}
}

func TestRuntimeStaleTimeoutRetriesQueuedTaskUntilFinalAttempt(t *testing.T) {
	t.Parallel()

	err := RuntimeStaleTimeoutError{
		Timeout:        5 * time.Minute,
		LastProgressAt: time.Now().UTC(),
		LastState:      "ack",
	}

	if !retryQueuedTaskError(err) {
		t.Fatal("RuntimeStaleTimeoutError should be retryable for queued task retry")
	}
	if terminal := queueTerminalReason(GatewayTaskQueueItem{Attempts: 1}, err, 5*time.Minute, true); terminal != nil {
		t.Fatalf("queueTerminalReason() with retry = %#v, want nil", terminal)
	}
	terminal := queueTerminalReason(GatewayTaskQueueItem{Attempts: 1}, err, 5*time.Minute, false)
	if terminal == nil {
		t.Fatal("queueTerminalReason() = nil")
	}
	if terminal.Code != "gateway_task_stale_progress" {
		t.Fatalf("terminal.Code = %q", terminal.Code)
	}
}

func TestRuntimeCapacityErrorStillRetriesQueuedTask(t *testing.T) {
	t.Parallel()

	if !retryQueuedTaskError(errors.New("runtime at capacity")) {
		t.Fatal("capacity errors should remain retryable")
	}
}

func TestRuntimeDeadlineExceededRetriesQueuedTask(t *testing.T) {
	t.Parallel()

	if !retryQueuedTaskError(context.DeadlineExceeded) {
		t.Fatal("runtime hard timeout should be retryable")
	}
}

func TestSecurityHTTP5xxRetriesQueuedTask(t *testing.T) {
	t.Parallel()

	err := &southbound.HTTPError{
		StatusCode: http.StatusInternalServerError,
		Method:     http.MethodPost,
		URL:        "http://security-service:8000/api/v1/collaboration/objects",
		Body:       "Internal Server Error",
	}

	if !retryQueuedTaskError(err) {
		t.Fatal("security-service 5xx should be retryable for queued tasks")
	}
}

func TestSecurityTransportErrorRetriesQueuedTask(t *testing.T) {
	t.Parallel()

	err := errors.New("POST http://security-service:8000/api/v1/collaboration/objects failed: dial tcp: connection reset by peer")

	if !retryQueuedTaskError(err) {
		t.Fatal("security-service transport errors should be retryable for queued tasks")
	}
}

func TestClaimSouthboundTaskRecoversCommittedClaimAfterTimeout(t *testing.T) {
	t.Parallel()

	sb := &claimRecoverySouthbound{
		firstErr: context.DeadlineExceeded,
		task: southbound.Task{
			ID:                "southbound-claim-timeout",
			TargetAssistantID: "assistant-a",
			Status:            "claimed",
		},
	}
	service := NewService(sb)

	claim, err := service.claimSouthboundTask(context.Background(), sb.task)
	if err != nil {
		t.Fatalf("claimSouthboundTask() error = %v", err)
	}
	if claim.Claim.ClaimToken != "token-recovered" {
		t.Fatalf("claim token = %q, want token-recovered", claim.Claim.ClaimToken)
	}
	if sb.claimCalls != 2 {
		t.Fatalf("claim calls = %d, want 2", sb.claimCalls)
	}
	if sb.getCalls != 1 {
		t.Fatalf("get task calls = %d, want 1", sb.getCalls)
	}
}

func TestResumeQueuedClaimedTaskWithoutStartReclaimsAndStarts(t *testing.T) {
	t.Parallel()

	sb := &claimRecoverySouthbound{
		task: southbound.Task{
			ID:                "southbound-claimed-no-start",
			TargetAssistantID: "assistant-a",
			Status:            "claimed",
		},
	}
	service := NewService(sb)

	view, err := service.resumeQueuedSouthboundTask(
		context.Background(),
		GatewayTaskQueueItem{},
		sb.task,
		TaskWorkerOptions{},
		&southboundExecutionHooks{},
		nil,
		nil,
	)
	if err != nil {
		t.Fatalf("resumeQueuedSouthboundTask() error = %v", err)
	}
	if view.Status != "running" {
		t.Fatalf("view status = %q, want running", view.Status)
	}
	if sb.claimCalls != 1 {
		t.Fatalf("claim calls = %d, want 1", sb.claimCalls)
	}
	if sb.startCalls != 1 {
		t.Fatalf("start calls = %d, want 1", sb.startCalls)
	}
}

func TestSecurityHTTP4xxDoesNotRetryQueuedTask(t *testing.T) {
	t.Parallel()

	err := &southbound.HTTPError{
		StatusCode: http.StatusBadRequest,
		Method:     http.MethodPost,
		URL:        "http://security-service:8000/api/v1/collaboration/objects",
		Body:       "bad request",
	}

	if retryQueuedTaskError(err) {
		t.Fatal("security-service 4xx should not be retried for queued tasks")
	}
}

func TestResolvedExternalSourceAssistantIDFitsSecuritySchema(t *testing.T) {
	t.Parallel()

	sourceAssistantID := resolveChatSourceAssistantID(ChatRequest{
		UserID: strings.Repeat("very-long-user-", 20),
	})

	if len(sourceAssistantID) > 64 {
		t.Fatalf("sourceAssistantID length = %d, want <= 64: %q", len(sourceAssistantID), sourceAssistantID)
	}
	if !strings.HasPrefix(sourceAssistantID, "external-") {
		t.Fatalf("sourceAssistantID = %q, want external prefix", sourceAssistantID)
	}
}

func TestQueuedTaskLeaseHeartbeatExtendsLease(t *testing.T) {
	queue := &heartbeatTestQueue{extended: make(chan struct{}, 1)}
	service := NewServiceWithQueue(&cacheTestSouthbound{}, nil, nil, queue)

	stop := service.startQueuedTaskLeaseHeartbeat(context.Background(), GatewayTaskQueueItem{
		TaskID:   "task-heartbeat",
		LockedBy: "worker-claim",
	}, 20*time.Millisecond, nil)
	defer stop()

	select {
	case <-queue.extended:
	case <-time.After(200 * time.Millisecond):
		t.Fatal("expected queued task lease heartbeat")
	}
	if queue.taskID != "task-heartbeat" || queue.lockedBy != "worker-claim" {
		t.Fatalf("unexpected heartbeat target task=%q locked_by=%q", queue.taskID, queue.lockedBy)
	}
}

func TestSouthboundClaimHeartbeatRenewsClaim(t *testing.T) {
	southboundFake := &claimHeartbeatSouthbound{renewed: make(chan southbound.RenewClaimRequest, 1)}
	service := NewService(southboundFake)

	stop := service.startSouthboundClaimHeartbeat(context.Background(), "task-heartbeat", "assistant-a", southbound.Claim{
		TaskID:         "task-heartbeat",
		AssistantID:    "assistant-a",
		ClaimToken:     "claim-token",
		LeaseExpiresAt: southbound.Time{Time: time.Now().UTC().Add(20 * time.Millisecond)},
	}, RuntimeReservation{}, nil)
	defer stop()

	select {
	case request := <-southboundFake.renewed:
		if request.AssistantID != "assistant-a" || request.ClaimToken != "claim-token" {
			t.Fatalf("unexpected renew request %#v", request)
		}
	case <-time.After(200 * time.Millisecond):
		t.Fatal("expected southbound claim heartbeat")
	}
}

func TestExecuteRuntimeTaskUsesRegistryRouteOverAssistantRuntime(t *testing.T) {
	t.Parallel()

	sb := &cacheTestSouthbound{
		assistants: map[string]southbound.Assistant{
			"assistant-a": {
				AssistantID: "assistant-a",
				Runtime: southbound.AssistantRuntime{
					GatewayURL:   "http://old-pod-ip:18789",
					GatewayToken: "runtime-token",
				},
			},
		},
	}
	executor := &recordingRuntimeExecutor{
		result: RuntimeExecutionResult{ReplyText: "done"},
	}
	service := NewService(sb)
	service.SetRuntimeExecutor(executor)
	service.SetRuntimeRouteResolver(staticRuntimeRouteResolver{
		route: RuntimeRoute{
			AssistantID:     "assistant-a",
			InternalBaseURL: "http://new-registry-route:18789",
			GatewayURL:      "http://public-registry-route:18789",
		},
		ok: true,
	})

	completed, err := service.executeRuntimeTask(context.Background(), southbound.Task{
		ID:                "task-route",
		TargetAssistantID: "assistant-a",
		Instruction:       "hello",
		TimeoutSeconds:    60,
	}, "claim-token", RuntimeReservation{}, nil)
	if err != nil {
		t.Fatalf("executeRuntimeTask() error = %v", err)
	}
	if completed.Status != "completed" {
		t.Fatalf("completed status = %q", completed.Status)
	}
	if executor.gatewayURL != "http://new-registry-route:18789" {
		t.Fatalf("runtime gateway url = %q, want registry route", executor.gatewayURL)
	}
	if len(sb.createdObjects) != 1 {
		t.Fatalf("created objects = %d, want 1", len(sb.createdObjects))
	}
	if sb.createdObjects[0].IdempotencyKey == "" {
		t.Fatal("runtime reply object should have idempotency key")
	}
}

// TestExecuteRuntimeTaskWiresGatewayTaskIDAndDeadlineSource pins the
// RuntimeExecutionRequest assembly: the gateway id (cross-layer log correlation)
// and the southbound id (runtime idempotency key) must land in the RIGHT fields,
// and DeadlineSource must be threaded. Swapping RequestID<->GatewayTaskID or
// dropping DeadlineSource previously shipped fully green.
func TestExecuteRuntimeTaskWiresGatewayTaskIDAndDeadlineSource(t *testing.T) {
	t.Parallel()

	sb := &cacheTestSouthbound{
		assistants: map[string]southbound.Assistant{
			"assistant-a": {
				AssistantID: "assistant-a",
				Runtime:     southbound.AssistantRuntime{GatewayURL: "http://rt:18789", GatewayToken: "tok"},
			},
		},
	}
	executor := &recordingRuntimeExecutor{result: RuntimeExecutionResult{ReplyText: "done"}}
	service := NewService(sb)
	service.SetRuntimeExecutor(executor)

	hooks := &southboundExecutionHooks{
		gatewayTaskID:  "gw-abc",
		deadlineSource: DeadlineSourceClientSupplied,
	}
	if _, err := service.executeRuntimeTask(context.Background(), southbound.Task{
		ID:                "sb-xyz",
		TargetAssistantID: "assistant-a",
		Instruction:       "hi",
		TimeoutSeconds:    60,
	}, "claim-token", RuntimeReservation{}, hooks); err != nil {
		t.Fatalf("executeRuntimeTask() error = %v", err)
	}
	if executor.request.GatewayTaskID != "gw-abc" {
		t.Errorf("request.GatewayTaskID = %q, want gw-abc (gateway id for log correlation)", executor.request.GatewayTaskID)
	}
	if executor.request.RequestID != "sb-xyz" {
		t.Errorf("request.RequestID = %q, want sb-xyz (southbound id / runtime idempotency key)", executor.request.RequestID)
	}
	if executor.request.DeadlineSource != DeadlineSourceClientSupplied {
		t.Errorf("request.DeadlineSource = %q, want %q", executor.request.DeadlineSource, DeadlineSourceClientSupplied)
	}
}

func TestExecuteRuntimeTaskUsesReservedRouteAndReleasesLease(t *testing.T) {
	t.Parallel()

	sb := &cacheTestSouthbound{
		assistants: map[string]southbound.Assistant{
			"assistant-a": {
				AssistantID: "assistant-a",
				Runtime: southbound.AssistantRuntime{
					GatewayURL:   "http://stale-assistant-route:18789",
					GatewayToken: "runtime-token",
				},
			},
		},
	}
	executor := &recordingRuntimeExecutor{
		result: RuntimeExecutionResult{ReplyText: "done"},
	}
	scheduler := &leaseRecordingScheduler{}
	service := NewServiceWithScheduler(sb, scheduler)
	service.SetRuntimeExecutor(executor)

	reservation := RuntimeReservation{
		LeaseID:     "lease-1",
		RuntimeID:   "runtime-a-1",
		AssistantID: "assistant-a",
		Route: RuntimeRoute{
			RuntimeID:       "runtime-a-1",
			AssistantID:     "assistant-a",
			InternalBaseURL: "http://runtime-a-1:18789",
			GatewayURL:      "http://runtime-a-1-public:18789",
		},
	}
	completed, err := service.executeRuntimeTask(context.Background(), southbound.Task{
		ID:                "task-reserved-route",
		TargetAssistantID: "assistant-a",
		Instruction:       "hello",
		TimeoutSeconds:    60,
	}, "claim-token", reservation, nil)
	if err != nil {
		t.Fatalf("executeRuntimeTask() error = %v", err)
	}
	if completed.Status != "completed" {
		t.Fatalf("completed status = %q", completed.Status)
	}
	if executor.gatewayURL != "http://runtime-a-1:18789" {
		t.Fatalf("runtime gateway url = %q, want reserved runtime route", executor.gatewayURL)
	}
	if scheduler.released.LeaseID != "lease-1" || scheduler.released.RuntimeID != "runtime-a-1" {
		t.Fatalf("released reservation = %#v", scheduler.released)
	}
	if scheduler.assistantReleaseCount != 0 {
		t.Fatalf("assistant-level release count = %d, want 0", scheduler.assistantReleaseCount)
	}
}

func TestResolveChatPreferredTargetUsesConversationRouteAndRejectsConflict(t *testing.T) {
	t.Parallel()

	store := &conversationRouteTestStore{
		record: ConversationRouteRecord{
			ClientID:       "client-a",
			UserID:         "user-a",
			ConversationID: "conv-a",
			AssistantID:    "assistant-sticky",
		},
	}
	service := NewServiceWithTaskStore(&cacheTestSouthbound{}, store)
	got, err := service.resolveChatPreferredTargetAssistantID(context.Background(), ChatRequest{
		ClientID:       "client-a",
		UserID:         "user-a",
		ConversationID: "conv-a",
	})
	if err != nil {
		t.Fatalf("resolveChatPreferredTargetAssistantID() error = %v", err)
	}
	if got != "assistant-sticky" {
		t.Fatalf("preferred target = %q, want conversation route", got)
	}

	_, err = service.resolveChatPreferredTargetAssistantID(context.Background(), ChatRequest{
		ClientID:          "client-a",
		UserID:            "user-a",
		ConversationID:    "conv-a",
		TargetAssistantID: "assistant-other",
	})
	if err == nil || !strings.Contains(err.Error(), "already bound") {
		t.Fatalf("conflicting explicit target error = %v, want already bound", err)
	}
}

func TestExecuteRuntimeTaskUsesDefaultTenMinuteTimeout(t *testing.T) {
	t.Parallel()

	sb := &cacheTestSouthbound{
		assistants: map[string]southbound.Assistant{
			"assistant-a": {
				AssistantID: "assistant-a",
				Runtime: southbound.AssistantRuntime{
					GatewayURL:   "http://runtime-route:18789",
					GatewayToken: "runtime-token",
				},
			},
		},
	}
	executor := &recordingRuntimeExecutor{
		result: RuntimeExecutionResult{ReplyText: "done"},
	}
	service := NewService(sb)
	service.SetRuntimeExecutor(executor)

	_, err := service.executeRuntimeTask(context.Background(), southbound.Task{
		ID:                "task-default-timeout",
		TargetAssistantID: "assistant-a",
		Instruction:       "hello",
	}, "claim-token", RuntimeReservation{}, nil)
	if err != nil {
		t.Fatalf("executeRuntimeTask() error = %v", err)
	}
	if executor.request.TimeoutMs != int((10*time.Minute)/time.Millisecond) {
		t.Fatalf("runtime timeout ms = %d, want 600000", executor.request.TimeoutMs)
	}
}

func TestExecuteRuntimeTaskRetriesWhenRegistryRouteLookupFails(t *testing.T) {
	t.Parallel()

	sb := &cacheTestSouthbound{
		assistants: map[string]southbound.Assistant{
			"assistant-a": {
				AssistantID: "assistant-a",
				Runtime: southbound.AssistantRuntime{
					GatewayURL:   "http://old-pod-ip:18789",
					GatewayToken: "runtime-token",
				},
			},
		},
	}
	executor := &recordingRuntimeExecutor{
		result: RuntimeExecutionResult{ReplyText: "done"},
	}
	service := NewService(sb)
	service.SetRuntimeExecutor(executor)
	service.SetRuntimeRouteResolver(staticRuntimeRouteResolver{err: errors.New("registry unavailable")})

	_, err := service.executeRuntimeTask(context.Background(), southbound.Task{
		ID:                "task-route-error",
		TargetAssistantID: "assistant-a",
		Instruction:       "hello",
		TimeoutSeconds:    60,
	}, "claim-token", RuntimeReservation{}, nil)
	if err == nil {
		t.Fatal("executeRuntimeTask() error = nil, want retryable route error")
	}
	if !retryQueuedTaskError(err) {
		t.Fatalf("route lookup error should be queue-retryable: %v", err)
	}
	if executor.gatewayURL != "" {
		t.Fatalf("runtime executor used stale gateway URL %q", executor.gatewayURL)
	}
	if len(sb.failedTasks) != 1 || sb.failedTasks[0].FailedReasonCode != "runtime_route_lookup_failed" {
		t.Fatalf("failed tasks = %#v, want runtime_route_lookup_failed", sb.failedTasks)
	}
}

func TestExecuteRuntimeTaskRetriesRuntimeTransportFailure(t *testing.T) {
	t.Parallel()

	sb := &cacheTestSouthbound{
		assistants: map[string]southbound.Assistant{
			"assistant-a": {
				AssistantID: "assistant-a",
				Runtime: southbound.AssistantRuntime{
					GatewayURL:   "http://runtime:18789",
					GatewayToken: "runtime-token",
				},
			},
		},
	}
	service := NewService(sb)
	service.SetRuntimeExecutor(&recordingRuntimeExecutor{err: errors.New("connection refused")})

	_, err := service.executeRuntimeTask(context.Background(), southbound.Task{
		ID:                "task-runtime-error",
		TargetAssistantID: "assistant-a",
		Instruction:       "hello",
		TimeoutSeconds:    60,
	}, "claim-token", RuntimeReservation{}, nil)
	if err == nil {
		t.Fatal("executeRuntimeTask() error = nil, want retryable runtime error")
	}
	if !retryQueuedTaskError(err) {
		t.Fatalf("runtime transport error should be queue-retryable: %v", err)
	}
	if len(sb.failedTasks) != 1 || sb.failedTasks[0].FailedReasonCode != "runtime_execution_failed" {
		t.Fatalf("failed tasks = %#v, want runtime_execution_failed", sb.failedTasks)
	}
}

func TestRuntimeProviderQuotaErrorUsesCapacityWait(t *testing.T) {
	t.Parallel()

	err := errors.New("runtime chat error: concurrency allocated quota exceeded. please try again later")
	if !retryQueuedTaskError(err) {
		t.Fatalf("provider quota error should be queue-retryable: %v", err)
	}
	if !capacityWaitQueuedTaskError(err) {
		t.Fatalf("provider quota error should defer for capacity recovery: %v", err)
	}
}

func TestNextCapacityWaitStateUsesQueueEpisodeState(t *testing.T) {
	t.Parallel()

	now := time.Now().UTC()
	firstAt := now.Add(-10 * time.Minute)
	callbackAt := now.Add(-time.Minute)
	state := nextCapacityWaitState(GatewayTaskQueueItem{
		Attempts:                   42,
		CapacityWaitCount:          7,
		CapacityWaitFirstAt:        &firstAt,
		CapacityWaitLastCallbackAt: &callbackAt,
	}, now)
	if state.Count != 8 {
		t.Fatalf("Count = %d, want 8", state.Count)
	}
	if state.FirstAt == nil || !state.FirstAt.Equal(firstAt) {
		t.Fatalf("FirstAt = %v, want %v", state.FirstAt, firstAt)
	}
	if !state.LastAt.Equal(now) {
		t.Fatalf("LastAt = %v, want %v", state.LastAt, now)
	}
	if state.LastCallbackAt == nil || !state.LastCallbackAt.Equal(callbackAt) {
		t.Fatalf("LastCallbackAt = %v, want %v", state.LastCallbackAt, callbackAt)
	}

	fresh := nextCapacityWaitState(GatewayTaskQueueItem{Attempts: 42}, now)
	if fresh.Count != 1 {
		t.Fatalf("fresh Count = %d, want 1", fresh.Count)
	}
	if fresh.FirstAt == nil || !fresh.FirstAt.Equal(now) {
		t.Fatalf("fresh FirstAt = %v, want %v", fresh.FirstAt, now)
	}
}

func TestCapacityDeferDetachesRootSouthboundTaskID(t *testing.T) {
	t.Parallel()

	ctx := context.Background()
	store := newDetachTestStore()
	service := NewServiceWithTaskStore(&cacheTestSouthbound{}, store)
	now := time.Now().UTC()
	if err := store.UpsertTask(ctx, GatewayTaskRecord{
		Task: TaskView{
			ID:                "gw-task-root-detach",
			Status:            "running",
			TargetAssistantID: "assistant-a",
			CreatedAt:         now,
			Metadata: map[string]any{
				"queue_status": "processing",
			},
		},
		SouthboundTaskID: "southbound-root-detach",
		CreatedAt:        now,
		UpdatedAt:        now,
	}); err != nil {
		t.Fatalf("UpsertTask() error = %v", err)
	}

	service.detachQueuedSouthboundTaskForCapacityDefer(ctx, southbound.Task{
		ID:                "gw-task-root-detach",
		RootTaskID:        "southbound-root-detach",
		TargetAssistantID: "assistant-a",
		MetaJSON: map[string]any{
			"gateway_task_id": "gw-task-root-detach",
		},
	}, errors.New("runtime chat error: concurrency allocated quota exceeded"))

	record, ok, err := store.GetTask(ctx, "gw-task-root-detach")
	if err != nil || !ok {
		t.Fatalf("GetTask() ok=%v err=%v", ok, err)
	}
	if record.SouthboundTaskID != "" {
		t.Fatalf("SouthboundTaskID = %q, want cleared", record.SouthboundTaskID)
	}
	if record.Task.Status != "running" {
		t.Fatalf("task status = %q, want running", record.Task.Status)
	}
	if got := record.Task.Metadata["queue_status"]; got != "retry" {
		t.Fatalf("queue_status = %#v, want retry", got)
	}
	if got := record.Task.Metadata["queue_capacity_deferred_root_task_id"]; got != "southbound-root-detach" {
		t.Fatalf("deferred root task id = %#v, want southbound-root-detach", got)
	}
}

func TestCapacityDeferRestoresInternalCancelledPublicTask(t *testing.T) {
	t.Parallel()

	ctx := context.Background()
	store := newDetachTestStore()
	service := NewServiceWithTaskStore(&cacheTestSouthbound{}, store)
	now := time.Now().UTC()
	if err := store.UpsertTask(ctx, GatewayTaskRecord{
		Task: TaskView{
			ID:                "gw-task-cancel-restore",
			Status:            "cancelled",
			TargetAssistantID: "assistant-a",
			CreatedAt:         now,
			FailedAt:          &now,
			FailedReasonCode:  "cancelled",
			FailedReasonText:  "deferred for runtime capacity recovery: runtime chat error: concurrency allocated quota exceeded",
			Metadata: map[string]any{
				"queue_status": "processing",
			},
		},
		SouthboundTaskID: "southbound-cancel-restore",
		CreatedAt:        now,
		UpdatedAt:        now,
	}); err != nil {
		t.Fatalf("UpsertTask() error = %v", err)
	}

	service.detachQueuedSouthboundTaskForCapacityDefer(ctx, southbound.Task{
		ID:                "gw-task-cancel-restore",
		RootTaskID:        "southbound-cancel-restore",
		TargetAssistantID: "assistant-a",
		MetaJSON: map[string]any{
			"gateway_task_id": "gw-task-cancel-restore",
		},
	}, errors.New("runtime chat error: concurrency allocated quota exceeded"))

	record, ok, err := store.GetTask(ctx, "gw-task-cancel-restore")
	if err != nil || !ok {
		t.Fatalf("GetTask() ok=%v err=%v", ok, err)
	}
	if record.SouthboundTaskID != "" {
		t.Fatalf("SouthboundTaskID = %q, want cleared", record.SouthboundTaskID)
	}
	if record.Task.Status != "running" {
		t.Fatalf("task status = %q, want running", record.Task.Status)
	}
	if record.Task.FailedAt != nil || record.Task.FailedReasonCode != "" || record.Task.FailedReasonText != "" {
		t.Fatalf("capacity defer cancellation was not cleared: %#v", record.Task)
	}
}

func TestSaveQueuedTaskResultPreservesQueueErrorLocation(t *testing.T) {
	t.Parallel()

	ctx := context.Background()
	store := newDetachTestStore()
	service := NewServiceWithTaskStore(&cacheTestSouthbound{}, store)
	now := time.Now().UTC()
	if err := store.UpsertTask(ctx, GatewayTaskRecord{
		Task: TaskView{
			ID:                "gw-task-error-location",
			Status:            "running",
			TargetAssistantID: "assistant-a",
			CreatedAt:         now,
			Metadata: map[string]any{
				"queue_error_location":    "runtime_ws.deadline",
				"queue_last_error":        "wait runtime chat final: context deadline exceeded",
				"queue_retry_reason_code": "runtime_execution_failed",
			},
		},
		SouthboundTaskID: "southbound-error-location",
		CreatedAt:        now,
		UpdatedAt:        now,
	}); err != nil {
		t.Fatalf("UpsertTask() error = %v", err)
	}

	saved, err := service.saveQueuedTaskResult(ctx, GatewayTaskQueueItem{
		TaskID: "gw-task-error-location",
		Request: ChatRequest{
			ClientID:          "default",
			RequestID:         "req-error-location",
			ConversationID:    "conv-error-location",
			UserID:            "user-a",
			TargetAssistantID: "assistant-a",
			SourceAssistantID: "external-default",
			ApprovalMode:      "none",
			IdempotencyKey:    "req-error-location",
			CallbackURL:       "http://callback.local/taskcallback",
		},
		RequestHash:  "hash-error-location",
		Attempts:     2,
		LockedBy:     "worker-a",
		AttemptToken: "attempt-a",
	}, TaskView{
		ID:                "southbound-error-location",
		Status:            "failed",
		SourceAssistantID: "external-default",
		TargetAssistantID: "assistant-a",
		CreatedAt:         now,
		FailedReasonCode:  "runtime_execution_failed",
		FailedReasonText:  "wait runtime chat final: context deadline exceeded",
	}, "southbound-error-location")
	if err != nil {
		t.Fatalf("saveQueuedTaskResult() error = %v", err)
	}
	if saved.Metadata["queue_error_location"] != "runtime_ws.deadline" {
		t.Fatalf("saved queue_error_location = %#v", saved.Metadata["queue_error_location"])
	}
	response := NewTaskResponse(saved)
	if response.ErrorLocation != "runtime_ws.deadline" {
		t.Fatalf("ErrorLocation = %q, want runtime_ws.deadline", response.ErrorLocation)
	}
}

func TestDrainingRuntimeSchedulerErrorUsesCapacityWait(t *testing.T) {
	t.Parallel()

	err := errors.New(`target assistant "assistant-a" rejected by scheduler: draining`)
	if !retryQueuedTaskError(err) {
		t.Fatalf("draining runtime error should be retryable: %v", err)
	}
	if !capacityWaitQueuedTaskError(err) {
		t.Fatalf("draining runtime error should defer for capacity recovery: %v", err)
	}
	if !runtimeSchedulerErrorRefreshable(err) {
		t.Fatalf("draining runtime error should trigger runtime refresh: %v", err)
	}
}

func TestExecuteRuntimeTaskRetriesRuntimeReplyObjectFiveHundred(t *testing.T) {
	t.Parallel()

	sb := &cacheTestSouthbound{
		assistants: map[string]southbound.Assistant{
			"assistant-a": {
				AssistantID: "assistant-a",
				Runtime: southbound.AssistantRuntime{
					GatewayURL:   "http://runtime:18789",
					GatewayToken: "runtime-token",
				},
			},
		},
		createObjectErr: &southbound.HTTPError{StatusCode: http.StatusInternalServerError},
	}
	service := NewService(sb)
	service.SetRuntimeExecutor(&recordingRuntimeExecutor{
		result: RuntimeExecutionResult{ReplyText: "done"},
	})

	_, err := service.executeRuntimeTask(context.Background(), southbound.Task{
		ID:                "task-object-error",
		TargetAssistantID: "assistant-a",
		Instruction:       "hello",
		TimeoutSeconds:    60,
	}, "claim-token", RuntimeReservation{}, nil)
	if err == nil {
		t.Fatal("executeRuntimeTask() error = nil, want retryable object creation error")
	}
	if !retryQueuedTaskError(err) {
		t.Fatalf("object creation 500 should be queue-retryable: %v", err)
	}
	if len(sb.failedTasks) != 1 || sb.failedTasks[0].FailedReasonCode != "runtime_result_object_failed" {
		t.Fatalf("failed tasks = %#v, want runtime_result_object_failed", sb.failedTasks)
	}
}

func TestQueuedStartDoesNotCompleteExistingRunningIdempotentTask(t *testing.T) {
	t.Parallel()

	service := NewService(&cacheTestSouthbound{})

	_, err := service.startCreatedTask(context.Background(), southbound.Task{
		ID:     "task-running",
		Status: "running",
	}, &southboundExecutionHooks{})
	if err == nil {
		t.Fatal("startCreatedTask() error = nil, want retryable running-task error")
	}
	if !retryQueuedTaskError(err) {
		t.Fatalf("running idempotent task should stay queue-retryable: %v", err)
	}
}

type cacheTestAssistantCache struct {
	assistantByUser    map[string]string
	assistantAvailable map[string]bool
}

func (c *cacheTestAssistantCache) GetAssistantForUser(_ context.Context, userID string) (string, bool, error) {
	assistantID, ok := c.assistantByUser[userID]
	return assistantID, ok, nil
}

func (c *cacheTestAssistantCache) SetAssistantForUser(_ context.Context, userID string, assistantID string) error {
	if c.assistantByUser == nil {
		c.assistantByUser = map[string]string{}
	}
	c.assistantByUser[userID] = assistantID
	return nil
}

func (c *cacheTestAssistantCache) GetAssistantAvailable(_ context.Context, assistantID string) (bool, bool, error) {
	available, ok := c.assistantAvailable[assistantID]
	return available, ok, nil
}

func (c *cacheTestAssistantCache) SetAssistantAvailable(_ context.Context, assistantID string, available bool) error {
	if c.assistantAvailable == nil {
		c.assistantAvailable = map[string]bool{}
	}
	c.assistantAvailable[assistantID] = available
	return nil
}

type cacheTestRateLimiter struct {
	allowed    bool
	err        error
	blockedKey string
	keys       []string
}

func (l *cacheTestRateLimiter) Allow(_ context.Context, key string) (bool, error) {
	l.keys = append(l.keys, key)
	if key == l.blockedKey {
		return false, l.err
	}
	return l.allowed, l.err
}

type cacheTestQueue struct {
	TaskQueue
	stats          TaskQueueStats
	assistantStats map[string]TaskQueueStats
}

func (q cacheTestQueue) TaskQueueStats(context.Context) (TaskQueueStats, error) {
	return q.stats, nil
}

func (q cacheTestQueue) TaskQueueStatsByTargetAssistant(_ context.Context, targetAssistantID string) (TaskQueueStats, error) {
	return q.assistantStats[strings.TrimSpace(targetAssistantID)], nil
}

type heartbeatTestQueue struct {
	TaskQueue
	extended chan struct{}
	taskID   string
	lockedBy string
}

func (q *heartbeatTestQueue) ExtendQueuedTaskLease(
	_ context.Context,
	taskID string,
	lockedBy string,
	_ time.Duration,
) (bool, error) {
	q.taskID = taskID
	q.lockedBy = lockedBy
	select {
	case q.extended <- struct{}{}:
	default:
	}
	return true, nil
}

type leaseRecordingScheduler struct {
	released              RuntimeReservation
	assistantReleaseCount int
}

func (s *leaseRecordingScheduler) SelectTarget(_ context.Context, request RuntimeScheduleRequest) (string, error) {
	return strings.TrimSpace(request.PreferredAssistantID), nil
}

func (s *leaseRecordingScheduler) Reserve(_ context.Context, _ string) error {
	return nil
}

func (s *leaseRecordingScheduler) Release(_ context.Context, _ string) {
	s.assistantReleaseCount++
}

func (s *leaseRecordingScheduler) ReserveRuntimeRoute(_ context.Context, assistantID string) (RuntimeReservation, error) {
	return RuntimeReservation{AssistantID: assistantID}, nil
}

func (s *leaseRecordingScheduler) RenewRuntimeRouteLease(_ context.Context, reservation RuntimeReservation) (RuntimeReservation, error) {
	return reservation, nil
}

func (s *leaseRecordingScheduler) ReleaseRuntimeRoute(_ context.Context, reservation RuntimeReservation) {
	s.released = reservation
}

type conversationRouteTestStore struct {
	record ConversationRouteRecord
}

type detachTestStore struct {
	records map[string]GatewayTaskRecord
}

func newDetachTestStore() *detachTestStore {
	return &detachTestStore{records: map[string]GatewayTaskRecord{}}
}

func (s *detachTestStore) FindByIdempotency(context.Context, string, string) (GatewayTaskRecord, bool, error) {
	return GatewayTaskRecord{}, false, nil
}

func (s *detachTestStore) FindByApprovalID(context.Context, string) (GatewayTaskRecord, bool, error) {
	return GatewayTaskRecord{}, false, nil
}

func (s *detachTestStore) GetTask(_ context.Context, taskID string) (GatewayTaskRecord, bool, error) {
	record, ok := s.records[strings.TrimSpace(taskID)]
	return record, ok, nil
}

func (s *detachTestStore) UpsertTask(_ context.Context, record GatewayTaskRecord) error {
	s.records[strings.TrimSpace(record.Task.ID)] = record
	return nil
}

func (s *conversationRouteTestStore) FindByIdempotency(context.Context, string, string) (GatewayTaskRecord, bool, error) {
	return GatewayTaskRecord{}, false, nil
}

func (s *conversationRouteTestStore) FindByApprovalID(context.Context, string) (GatewayTaskRecord, bool, error) {
	return GatewayTaskRecord{}, false, nil
}

func (s *conversationRouteTestStore) GetTask(context.Context, string) (GatewayTaskRecord, bool, error) {
	return GatewayTaskRecord{}, false, nil
}

func (s *conversationRouteTestStore) UpsertTask(context.Context, GatewayTaskRecord) error {
	return nil
}

func (s *conversationRouteTestStore) GetConversationRoute(
	_ context.Context,
	clientID string,
	userID string,
	conversationID string,
) (ConversationRouteRecord, bool, error) {
	if s.record.ClientID == clientID &&
		s.record.UserID == userID &&
		s.record.ConversationID == conversationID {
		return s.record, true, nil
	}
	return ConversationRouteRecord{}, false, nil
}

func (s *conversationRouteTestStore) BindConversationRoute(
	_ context.Context,
	record ConversationRouteRecord,
) (ConversationRouteRecord, bool, error) {
	if s.record.AssistantID != "" && s.record.AssistantID != record.AssistantID {
		return s.record, true, &ValidationError{Message: "already bound"}
	}
	s.record = record
	return record, false, nil
}

type cacheTestSouthbound struct {
	Southbound
	bindings        map[string][]southbound.UserAssistantBinding
	assistants      map[string]southbound.Assistant
	createdObjects  []southbound.SharedObjectCreateRequest
	createObjectErr error
	failedTasks     []southbound.FailTaskRequest
	listCalls       int
	getCalls        int
}

func (f *cacheTestSouthbound) ListUserAssistants(_ context.Context, userID string) ([]southbound.UserAssistantBinding, error) {
	f.listCalls++
	return f.bindings[userID], nil
}

func (f *cacheTestSouthbound) GetAssistant(_ context.Context, assistantID string) (southbound.Assistant, error) {
	f.getCalls++
	assistant, ok := f.assistants[assistantID]
	if !ok {
		return southbound.Assistant{}, &southbound.HTTPError{StatusCode: 404}
	}
	return assistant, nil
}

func (f *cacheTestSouthbound) CreateSharedObject(_ context.Context, request southbound.SharedObjectCreateRequest) (southbound.SharedObject, error) {
	if f.createObjectErr != nil {
		return southbound.SharedObject{}, f.createObjectErr
	}
	f.createdObjects = append(f.createdObjects, request)
	return southbound.SharedObject{ID: "object-created"}, nil
}

func (f *cacheTestSouthbound) CompleteTask(_ context.Context, taskID string, request southbound.CompleteTaskRequest) (southbound.Task, error) {
	return southbound.Task{
		ID:                taskID,
		TargetAssistantID: request.AssistantID,
		Status:            "completed",
		ResultObjectID:    request.ResultObjectID,
	}, nil
}

func (f *cacheTestSouthbound) FailTask(_ context.Context, taskID string, request southbound.FailTaskRequest) (southbound.Task, error) {
	f.failedTasks = append(f.failedTasks, request)
	return southbound.Task{
		ID:                taskID,
		TargetAssistantID: request.AssistantID,
		Status:            "failed",
		FailedReasonCode:  request.FailedReasonCode,
		FailedReasonText:  request.FailedReasonText,
	}, nil
}

type claimHeartbeatSouthbound struct {
	Southbound
	renewed chan southbound.RenewClaimRequest
}

func (f *claimHeartbeatSouthbound) RenewTaskClaim(
	_ context.Context,
	taskID string,
	request southbound.RenewClaimRequest,
) (southbound.ClaimResponse, error) {
	select {
	case f.renewed <- request:
	default:
	}
	now := time.Now().UTC()
	return southbound.ClaimResponse{
		Task: southbound.Task{ID: taskID, Status: "running"},
		Claim: southbound.Claim{
			TaskID:         taskID,
			AssistantID:    request.AssistantID,
			ClaimToken:     request.ClaimToken,
			LeaseExpiresAt: southbound.Time{Time: now.Add(time.Minute)},
			Status:         "active",
		},
	}, nil
}

type claimRecoverySouthbound struct {
	Southbound
	firstErr   error
	task       southbound.Task
	claimCalls int
	getCalls   int
	startCalls int
}

func (f *claimRecoverySouthbound) ClaimTask(_ context.Context, taskID string, assistantID string) (southbound.ClaimResponse, error) {
	f.claimCalls++
	if f.claimCalls == 1 && f.firstErr != nil {
		return southbound.ClaimResponse{}, f.firstErr
	}
	now := time.Now().UTC()
	return southbound.ClaimResponse{
		Task: f.task,
		Claim: southbound.Claim{
			ID:             "claim-recovered",
			TaskID:         taskID,
			AssistantID:    assistantID,
			ClaimToken:     "token-recovered",
			ClaimedAt:      southbound.Time{Time: now},
			LeaseExpiresAt: southbound.Time{Time: now.Add(5 * time.Minute)},
			Status:         "active",
		},
	}, nil
}

func (f *claimRecoverySouthbound) GetTask(_ context.Context, taskID string) (southbound.Task, error) {
	f.getCalls++
	task := f.task
	task.ID = taskID
	return task, nil
}

func (f *claimRecoverySouthbound) StartTask(_ context.Context, taskID string, _ southbound.StartTaskRequest) (southbound.Task, error) {
	f.startCalls++
	now := time.Now().UTC()
	task := f.task
	task.ID = taskID
	task.Status = "running"
	task.StartedAt = &southbound.Time{Time: now}
	f.task = task
	return task, nil
}

type recordingRuntimeExecutor struct {
	gatewayURL string
	request    RuntimeExecutionRequest
	result     RuntimeExecutionResult
	err        error
}

func (e *recordingRuntimeExecutor) ExecuteChat(_ context.Context, request RuntimeExecutionRequest) (RuntimeExecutionResult, error) {
	e.gatewayURL = request.GatewayURL
	e.request = request
	return e.result, e.err
}

type staticRuntimeRouteResolver struct {
	route RuntimeRoute
	ok    bool
	err   error
}

func (r staticRuntimeRouteResolver) ResolveRuntimeRoute(context.Context, string) (RuntimeRoute, bool, error) {
	return r.route, r.ok, r.err
}
