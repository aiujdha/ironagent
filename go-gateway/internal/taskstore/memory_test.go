package taskstore

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"time"

	"ironagent/go-gateway/internal/core"
)

func TestMemoryQueueConcurrentClaimsDoNotDuplicateTasks(t *testing.T) {
	t.Parallel()

	store := NewMemoryStore()
	ctx := context.Background()
	const taskCount = 2000
	for i := 0; i < taskCount; i++ {
		taskID := fmt.Sprintf("task-%03d", i)
		if err := store.EnqueueTask(ctx, core.GatewayTaskQueueItem{
			TaskID:    taskID,
			Status:    "queued",
			CreatedAt: time.Now().UTC(),
		}); err != nil {
			t.Fatalf("enqueue %s: %v", taskID, err)
		}
	}

	claimed := make(chan string, taskCount)
	var wg sync.WaitGroup
	for worker := 0; worker < 20; worker++ {
		wg.Add(1)
		go func(worker int) {
			defer wg.Done()
			for {
				items, err := store.ClaimQueuedTasks(
					ctx,
					fmt.Sprintf("worker-%02d", worker),
					25,
					time.Minute,
				)
				if err != nil {
					t.Errorf("claim: %v", err)
					return
				}
				if len(items) == 0 {
					return
				}
				for _, item := range items {
					claimed <- item.TaskID
				}
			}
		}(worker)
	}
	wg.Wait()
	close(claimed)

	seen := map[string]bool{}
	for taskID := range claimed {
		if seen[taskID] {
			t.Fatalf("duplicate claim for %s", taskID)
		}
		seen[taskID] = true
	}
	if len(seen) != taskCount {
		t.Fatalf("claimed %d tasks, want %d", len(seen), taskCount)
	}
}

func TestMemoryCreateQueuedTaskConcurrentIdempotencyReturnsSingleTask(t *testing.T) {
	t.Parallel()

	store := NewMemoryStore()
	ctx := context.Background()
	const workers = 50
	results := make(chan core.GatewayTaskRecord, workers)
	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		i := i
		wg.Add(1)
		go func() {
			defer wg.Done()
			record := core.GatewayTaskRecord{
				Task: core.TaskView{
					ID:        fmt.Sprintf("gw-idem-%02d", i),
					Status:    "pending_claim",
					CreatedAt: time.Now().UTC(),
				},
				ClientID:       "default",
				RequestID:      "req-idem",
				IdempotencyKey: "req-idem",
				RequestHash:    "same-hash",
			}
			created, _, err := store.CreateQueuedTask(ctx, record, core.GatewayTaskQueueItem{
				TaskID:      record.Task.ID,
				RequestHash: record.RequestHash,
				Status:      "admitting",
				CreatedAt:   record.Task.CreatedAt,
			})
			if err != nil {
				t.Errorf("CreateQueuedTask() error = %v", err)
				return
			}
			results <- created
		}()
	}
	wg.Wait()
	close(results)

	taskIDs := map[string]struct{}{}
	for record := range results {
		taskIDs[record.Task.ID] = struct{}{}
	}
	if len(taskIDs) != 1 {
		t.Fatalf("created task IDs = %#v, want one idempotent task", taskIDs)
	}
	stats, err := store.TaskQueueStats(ctx)
	if err != nil {
		t.Fatalf("TaskQueueStats() error = %v", err)
	}
	if stats.Active != 1 || stats.Admitting != 1 {
		t.Fatalf("queue stats = %#v, want one admitting task", stats)
	}
}

func TestMemoryTaskQueueStatsByTargetAssistantCountsOnlyActiveStatuses(t *testing.T) {
	t.Parallel()

	store := NewMemoryStore()
	ctx := context.Background()
	now := time.Now().UTC()
	create := func(taskID string, assistantID string, status string) {
		t.Helper()
		_, _, err := store.CreateQueuedTask(ctx, core.GatewayTaskRecord{
			Task: core.TaskView{
				ID:                taskID,
				TargetAssistantID: assistantID,
				Status:            "pending_claim",
				CreatedAt:         now,
			},
		}, core.GatewayTaskQueueItem{
			TaskID:    taskID,
			Status:    status,
			CreatedAt: now,
		})
		if err != nil {
			t.Fatalf("CreateQueuedTask(%s): %v", taskID, err)
		}
	}
	create("task-a-admitting", "assistant-a", "admitting")
	create("task-a-queued", "assistant-a", "queued")
	create("task-a-processing", "assistant-a", "processing")
	create("task-a-retry", "assistant-a", "retry")
	create("task-a-failed", "assistant-a", "failed")
	create("task-b-queued", "assistant-b", "queued")

	stats, err := store.TaskQueueStatsByTargetAssistant(ctx, "assistant-a")
	if err != nil {
		t.Fatalf("TaskQueueStatsByTargetAssistant() error = %v", err)
	}
	if stats.Admitting != 1 || stats.Queued != 1 || stats.Processing != 1 || stats.Retry != 1 || stats.Active != 4 {
		t.Fatalf("assistant-a stats = %#v, want one of each active status", stats)
	}
	if stats.Failed != 0 || stats.Completed != 0 || stats.Cancelled != 0 {
		t.Fatalf("assistant-a terminal stats = %#v, want zero terminal counts", stats)
	}
}

func TestMemoryQueueCapacityWaitStatePersistsAndResets(t *testing.T) {
	t.Parallel()

	store := NewMemoryStore()
	ctx := context.Background()
	now := time.Now().UTC()
	if err := store.EnqueueTask(ctx, core.GatewayTaskQueueItem{
		TaskID:    "task-capacity-state",
		Status:    "queued",
		CreatedAt: now,
	}); err != nil {
		t.Fatalf("enqueue: %v", err)
	}

	claimed, err := store.ClaimQueuedTasks(ctx, "worker-a", 1, time.Minute)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if len(claimed) != 1 {
		t.Fatalf("claimed %d tasks, want 1", len(claimed))
	}
	firstAt := now.Add(time.Second)
	lastAt := now.Add(2 * time.Second)
	if ok, err := store.DeferQueuedTaskForCapacity(ctx, claimed[0].TaskID, claimed[0].LockedBy, claimed[0].AttemptToken, "runtime at capacity", nil, core.QueueCapacityWaitState{
		Count:   1,
		FirstAt: &firstAt,
		LastAt:  lastAt,
	}); err != nil || !ok {
		t.Fatalf("DeferQueuedTaskForCapacity ok=%v err=%v", ok, err)
	}
	callbackAt := now.Add(3 * time.Second)
	if ok, err := store.RecordQueuedTaskCapacityWaitCallback(ctx, "task-capacity-state", callbackAt); err != nil || !ok {
		t.Fatalf("RecordQueuedTaskCapacityWaitCallback ok=%v err=%v", ok, err)
	}

	claimed, err = store.ClaimQueuedTasks(ctx, "worker-b", 1, time.Minute)
	if err != nil {
		t.Fatalf("claim after capacity defer: %v", err)
	}
	if len(claimed) != 1 {
		t.Fatalf("claimed after capacity defer = %d, want 1", len(claimed))
	}
	if claimed[0].CapacityWaitCount != 1 ||
		claimed[0].CapacityWaitFirstAt == nil ||
		!claimed[0].CapacityWaitFirstAt.Equal(firstAt) ||
		claimed[0].CapacityWaitLastAt == nil ||
		!claimed[0].CapacityWaitLastAt.Equal(lastAt) ||
		claimed[0].CapacityWaitLastCallbackAt == nil ||
		!claimed[0].CapacityWaitLastCallbackAt.Equal(callbackAt) {
		t.Fatalf("capacity wait state was not restored on claim: %#v", claimed[0])
	}

	if ok, err := store.DeferQueuedTask(ctx, claimed[0].TaskID, claimed[0].LockedBy, claimed[0].AttemptToken, "non-capacity retry", nil); err != nil || !ok {
		t.Fatalf("DeferQueuedTask ok=%v err=%v", ok, err)
	}
	claimed, err = store.ClaimQueuedTasks(ctx, "worker-c", 1, time.Minute)
	if err != nil {
		t.Fatalf("claim after non-capacity defer: %v", err)
	}
	if len(claimed) != 1 {
		t.Fatalf("claimed after non-capacity defer = %d, want 1", len(claimed))
	}
	if claimed[0].CapacityWaitCount != 0 ||
		claimed[0].CapacityWaitFirstAt != nil ||
		claimed[0].CapacityWaitLastAt != nil ||
		claimed[0].CapacityWaitLastCallbackAt != nil {
		t.Fatalf("capacity wait state was not reset after non-capacity defer: %#v", claimed[0])
	}
}

func TestMemoryQueueExtendProcessingLeasePreventsExpiredReclaim(t *testing.T) {
	t.Parallel()

	store := NewMemoryStore()
	ctx := context.Background()
	if err := store.EnqueueTask(ctx, core.GatewayTaskQueueItem{
		TaskID:    "task-lease",
		Status:    "queued",
		CreatedAt: time.Now().UTC(),
	}); err != nil {
		t.Fatalf("enqueue: %v", err)
	}

	claimed, err := store.ClaimQueuedTasks(ctx, "worker-a", 1, 20*time.Millisecond)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if len(claimed) != 1 {
		t.Fatalf("claimed %d tasks, want 1", len(claimed))
	}

	ok, err := store.ExtendQueuedTaskLease(ctx, "task-lease", claimed[0].LockedBy, 150*time.Millisecond)
	if err != nil {
		t.Fatalf("extend lease: %v", err)
	}
	if !ok {
		t.Fatalf("extend lease returned false")
	}

	time.Sleep(50 * time.Millisecond)
	reclaimed, err := store.ClaimQueuedTasks(ctx, "worker-b", 1, time.Minute)
	if err != nil {
		t.Fatalf("reclaim before extended expiry: %v", err)
	}
	if len(reclaimed) != 0 {
		t.Fatalf("expected extended lease to hide task, got %#v", reclaimed)
	}

	time.Sleep(130 * time.Millisecond)
	reclaimed, err = store.ClaimQueuedTasks(ctx, "worker-b", 1, time.Minute)
	if err != nil {
		t.Fatalf("reclaim after extended expiry: %v", err)
	}
	if len(reclaimed) != 1 || reclaimed[0].TaskID != "task-lease" {
		t.Fatalf("expected task to be reclaimable after extended lease, got %#v", reclaimed)
	}
}

func TestMemoryQueueCompleteAndFailRequireCurrentLease(t *testing.T) {
	t.Parallel()

	store := NewMemoryStore()
	ctx := context.Background()
	if err := store.EnqueueTask(ctx, core.GatewayTaskQueueItem{
		TaskID:    "task-stale-complete",
		Status:    "queued",
		CreatedAt: time.Now().UTC(),
	}); err != nil {
		t.Fatalf("enqueue complete task: %v", err)
	}
	claimed, err := store.ClaimQueuedTasks(ctx, "worker-a", 1, 20*time.Millisecond)
	if err != nil {
		t.Fatalf("claim complete task: %v", err)
	}
	if len(claimed) != 1 {
		t.Fatalf("claimed %d complete tasks, want 1", len(claimed))
	}

	time.Sleep(40 * time.Millisecond)
	if _, err := store.CompleteQueuedTask(ctx, "task-stale-complete", claimed[0].LockedBy, claimed[0].AttemptToken); err != nil {
		t.Fatalf("stale complete: %v", err)
	}
	stats, err := store.TaskQueueStats(ctx)
	if err != nil {
		t.Fatalf("stats after stale complete: %v", err)
	}
	if stats.Completed != 0 {
		t.Fatalf("stale worker completed expired task: %#v", stats)
	}

	reclaimed, err := store.ClaimQueuedTasks(ctx, "worker-b", 1, time.Minute)
	if err != nil {
		t.Fatalf("reclaim complete task: %v", err)
	}
	if len(reclaimed) != 1 {
		t.Fatalf("reclaimed %d complete tasks, want 1", len(reclaimed))
	}
	if _, err := store.CompleteQueuedTask(ctx, "task-stale-complete", claimed[0].LockedBy, claimed[0].AttemptToken); err != nil {
		t.Fatalf("wrong-worker complete: %v", err)
	}
	stats, err = store.TaskQueueStats(ctx)
	if err != nil {
		t.Fatalf("stats after wrong-worker complete: %v", err)
	}
	if stats.Completed != 0 {
		t.Fatalf("old worker completed reclaimed task: %#v", stats)
	}
	if _, err := store.CompleteQueuedTask(ctx, "task-stale-complete", reclaimed[0].LockedBy, reclaimed[0].AttemptToken); err != nil {
		t.Fatalf("current complete: %v", err)
	}

	if err := store.EnqueueTask(ctx, core.GatewayTaskQueueItem{
		TaskID:    "task-current-fail",
		Status:    "queued",
		CreatedAt: time.Now().UTC(),
	}); err != nil {
		t.Fatalf("enqueue fail task: %v", err)
	}
	failClaim, err := store.ClaimQueuedTasks(ctx, "worker-c", 1, time.Minute)
	if err != nil {
		t.Fatalf("claim fail task: %v", err)
	}
	if len(failClaim) != 1 {
		t.Fatalf("claimed %d fail tasks, want 1", len(failClaim))
	}
	if _, err := store.FailQueuedTask(ctx, "task-current-fail", "wrong-worker", failClaim[0].AttemptToken, "boom", false, nil, nil); err != nil {
		t.Fatalf("wrong-worker fail: %v", err)
	}
	stats, err = store.TaskQueueStats(ctx)
	if err != nil {
		t.Fatalf("stats after wrong-worker fail: %v", err)
	}
	if stats.Failed != 0 {
		t.Fatalf("wrong worker failed task: %#v", stats)
	}
	if _, err := store.FailQueuedTask(ctx, "task-current-fail", failClaim[0].LockedBy, failClaim[0].AttemptToken, "boom", false, nil, nil); err != nil {
		t.Fatalf("current fail: %v", err)
	}
	stats, err = store.TaskQueueStats(ctx)
	if err != nil {
		t.Fatalf("stats after current fail: %v", err)
	}
	if stats.Completed != 1 || stats.Failed != 1 {
		t.Fatalf("expected one completed and one failed task, got %#v", stats)
	}
}

func TestMemoryConversationRouteRejectsConflictingAssistant(t *testing.T) {
	t.Parallel()

	store := NewMemoryStore()
	ctx := context.Background()
	first, replayed, err := store.BindConversationRoute(ctx, core.ConversationRouteRecord{
		ClientID:       "client-a",
		UserID:         "user-a",
		ConversationID: "conv-a",
		AssistantID:    "assistant-a",
	})
	if err != nil {
		t.Fatalf("BindConversationRoute first error = %v", err)
	}
	if replayed {
		t.Fatalf("first bind replayed")
	}
	if first.AssistantID != "assistant-a" {
		t.Fatalf("first assistant = %q", first.AssistantID)
	}

	existing, replayed, err := store.BindConversationRoute(ctx, core.ConversationRouteRecord{
		ClientID:       "client-a",
		UserID:         "user-a",
		ConversationID: "conv-a",
		AssistantID:    "assistant-b",
	})
	if err == nil {
		t.Fatalf("conflicting bind error = nil")
	}
	if !replayed {
		t.Fatalf("conflicting bind replayed = false")
	}
	if existing.AssistantID != "assistant-a" {
		t.Fatalf("existing assistant = %q, want assistant-a", existing.AssistantID)
	}
}

func TestMemoryQueueDeferReleasesLeaseWithoutBurningAttempt(t *testing.T) {
	t.Parallel()

	store := NewMemoryStore()
	ctx := context.Background()
	if err := store.EnqueueTask(ctx, core.GatewayTaskQueueItem{
		TaskID:    "task-defer",
		Status:    "queued",
		CreatedAt: time.Now().UTC(),
	}); err != nil {
		t.Fatalf("enqueue: %v", err)
	}

	claimed, err := store.ClaimQueuedTasks(ctx, "worker-a", 1, time.Minute)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if len(claimed) != 1 || claimed[0].Attempts != 1 {
		t.Fatalf("unexpected first claim: %#v", claimed)
	}

	if ok, err := store.DeferQueuedTask(ctx, "task-defer", "wrong-worker", claimed[0].AttemptToken, "runtime at capacity", nil); err != nil {
		t.Fatalf("wrong-worker defer: %v", err)
	} else if ok {
		t.Fatal("wrong-worker defer returned true")
	}

	retryAt := time.Now().UTC().Add(-time.Millisecond)
	if ok, err := store.DeferQueuedTask(ctx, "task-defer", claimed[0].LockedBy, claimed[0].AttemptToken, "runtime at capacity", &retryAt); err != nil {
		t.Fatalf("defer: %v", err)
	} else if !ok {
		t.Fatal("defer returned false")
	}

	stats, err := store.TaskQueueStats(ctx)
	if err != nil {
		t.Fatalf("stats: %v", err)
	}
	if stats.Retry != 1 || stats.Processing != 0 || stats.Failed != 0 {
		t.Fatalf("unexpected stats after defer: %#v", stats)
	}

	reclaimed, err := store.ClaimQueuedTasks(ctx, "worker-b", 1, time.Minute)
	if err != nil {
		t.Fatalf("reclaim: %v", err)
	}
	if len(reclaimed) != 1 {
		t.Fatalf("reclaimed %d tasks, want 1", len(reclaimed))
	}
	if reclaimed[0].Attempts != 1 {
		t.Fatalf("reclaimed attempts = %d, want 1", reclaimed[0].Attempts)
	}
}
