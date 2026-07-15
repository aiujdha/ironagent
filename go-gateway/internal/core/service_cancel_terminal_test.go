package core

import (
	"context"
	"errors"
	"testing"
)

// ①A-1: a worker hitting the retry path must not resurrect a task that has
// already reached a (real, non-capacity-defer) terminal state. Before the fix
// markQueuedTaskRetrying unconditionally wrote status="running", clobbering a
// concurrent cancel.
func TestMarkQueuedTaskRetryingPreservesCancelledTerminalState(t *testing.T) {
	store := newDetachTestStore()
	svc := NewServiceWithTaskStore(&cacheTestSouthbound{}, store)
	ctx := context.Background()

	if err := store.UpsertTask(ctx, GatewayTaskRecord{
		Task: TaskView{ID: "task-cancel-retry", Status: "cancelled"},
	}); err != nil {
		t.Fatalf("seed cancelled task: %v", err)
	}

	svc.markQueuedTaskRetrying(ctx, GatewayTaskQueueItem{TaskID: "task-cancel-retry"}, errors.New("attempt failed"), nil, nil)

	rec, ok, _ := store.GetTask(ctx, "task-cancel-retry")
	if !ok {
		t.Fatal("task disappeared from store")
	}
	if rec.Task.Status != "cancelled" {
		t.Fatalf("cancelled terminal state overwritten by retrying: got status=%q, want cancelled", rec.Task.Status)
	}
}

// ①A-1b: capacity-defer cancellation is a *reversible* pseudo-terminal state;
// the retry guard must let it resume to running (mirrors detach logic at 3554).
func TestMarkQueuedTaskRetryingResumesCapacityDeferCancellation(t *testing.T) {
	store := newDetachTestStore()
	svc := NewServiceWithTaskStore(&cacheTestSouthbound{}, store)
	ctx := context.Background()
	if err := store.UpsertTask(ctx, GatewayTaskRecord{
		Task: TaskView{
			ID:               "task-capacity-defer-retry",
			Status:           "cancelled",
			FailedReasonText: "deferred for runtime capacity recovery",
		},
	}); err != nil {
		t.Fatalf("seed capacity-defer task: %v", err)
	}

	svc.markQueuedTaskRetrying(ctx, GatewayTaskQueueItem{TaskID: "task-capacity-defer-retry"}, errors.New("runtime capacity unavailable"), nil, nil)

	rec, _, _ := store.GetTask(ctx, "task-capacity-defer-retry")
	if rec.Task.Status != "running" {
		t.Fatalf("capacity-defer cancellation must resume to running, got status=%q", rec.Task.Status)
	}
}

// ①A-2: markQueuedTaskFailed must not clobber a real cancelled terminal state
// (terminal-state-by-outcome: cancel wins over a late worker failure verdict).
func TestMarkQueuedTaskFailedPreservesCancelledTerminalState(t *testing.T) {
	store := newDetachTestStore()
	svc := NewServiceWithTaskStore(&cacheTestSouthbound{}, store)
	ctx := context.Background()
	if err := store.UpsertTask(ctx, GatewayTaskRecord{
		Task: TaskView{ID: "task-cancel-fail", Status: "cancelled"},
	}); err != nil {
		t.Fatalf("seed cancelled task: %v", err)
	}

	view, err := svc.markQueuedTaskFailed(ctx, GatewayTaskQueueItem{TaskID: "task-cancel-fail"}, errors.New("worker error"), nil, nil)
	if err != nil {
		t.Fatalf("markQueuedTaskFailed error: %v", err)
	}
	if view.Status != "cancelled" {
		t.Fatalf("returned view should report true terminal cancelled, got %q", view.Status)
	}
	rec, _, _ := store.GetTask(ctx, "task-cancel-fail")
	if rec.Task.Status != "cancelled" {
		t.Fatalf("cancelled terminal state overwritten by failed: got status=%q, want cancelled", rec.Task.Status)
	}
}

// ①A-2b: capacity-defer cancellation must still be allowed to terminalize to
// failed (e.g. capacity exhausted) — the fail guard must not over-block it.
func TestMarkQueuedTaskFailedAllowsCapacityDeferCancellation(t *testing.T) {
	store := newDetachTestStore()
	svc := NewServiceWithTaskStore(&cacheTestSouthbound{}, store)
	ctx := context.Background()
	if err := store.UpsertTask(ctx, GatewayTaskRecord{
		Task: TaskView{
			ID:               "task-capacity-defer-fail",
			Status:           "cancelled",
			FailedReasonText: "deferred for runtime capacity recovery",
		},
	}); err != nil {
		t.Fatalf("seed capacity-defer task: %v", err)
	}

	if _, err := svc.markQueuedTaskFailed(ctx, GatewayTaskQueueItem{TaskID: "task-capacity-defer-fail"}, errors.New("capacity exhausted"), nil, nil); err != nil {
		t.Fatalf("markQueuedTaskFailed error: %v", err)
	}
	rec, _, _ := store.GetTask(ctx, "task-capacity-defer-fail")
	if rec.Task.Status != "failed" {
		t.Fatalf("capacity-defer cancellation should be allowed to fail, got status=%q", rec.Task.Status)
	}
}

// ①A-3: markQueuedTaskRetrying must report whether it actually marked retrying,
// so callers can gate the task.retrying callback. Without this the caller still
// fires task.retrying after a cancel, producing a cancelled→retrying contradiction.
func TestMarkQueuedTaskRetryingReportsAppliedForCallbackGating(t *testing.T) {
	store := newDetachTestStore()
	svc := NewServiceWithTaskStore(&cacheTestSouthbound{}, store)
	ctx := context.Background()

	if err := store.UpsertTask(ctx, GatewayTaskRecord{Task: TaskView{ID: "t-terminal", Status: "cancelled"}}); err != nil {
		t.Fatalf("seed: %v", err)
	}
	if svc.markQueuedTaskRetrying(ctx, GatewayTaskQueueItem{TaskID: "t-terminal"}, errors.New("x"), nil, nil) {
		t.Fatal("expected applied=false on already-terminal task so caller suppresses task.retrying")
	}

	if err := store.UpsertTask(ctx, GatewayTaskRecord{Task: TaskView{ID: "t-running", Status: "running"}}); err != nil {
		t.Fatalf("seed: %v", err)
	}
	if !svc.markQueuedTaskRetrying(ctx, GatewayTaskQueueItem{TaskID: "t-running"}, errors.New("x"), nil, nil) {
		t.Fatal("expected applied=true on non-terminal task")
	}
}
