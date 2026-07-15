package core

import (
	"errors"
	"testing"
	"time"
)

// ②A: the active-southbound defer loop must have a task-level wall-clock upper
// bound. Anchored at CreatedAt so it is monotonic across attempts (a defer must
// not reset it), mirroring K8s activeDeadlineSeconds — this closes the H4 hole
// where a perpetually-"active" downstream task is deferred forever.
func TestActiveSouthboundTerminalReasonWallClock(t *testing.T) {
	base := time.Date(2026, 6, 14, 12, 0, 0, 0, time.UTC)
	opts := TaskWorkerOptions{ActiveSouthboundDeferTimeout: 30 * time.Minute}

	expired := GatewayTaskQueueItem{TaskID: "t1", CreatedAt: base.Add(-31 * time.Minute)}
	reason := activeSouthboundTerminalReason(expired, opts, errors.New("still active"), base)
	if reason == nil {
		t.Fatal("expected terminal reason once wall-clock deadline exceeded")
	}
	if reason.Code != "runtime_active_southbound_timeout" {
		t.Fatalf("unexpected terminal code %q", reason.Code)
	}

	fresh := GatewayTaskQueueItem{TaskID: "t2", CreatedAt: base.Add(-5 * time.Minute)}
	if activeSouthboundTerminalReason(fresh, opts, errors.New("still active"), base) != nil {
		t.Fatal("expected no terminal reason before deadline")
	}

	// timeout<=0 disables the cap (config layer enforces a non-zero default).
	if activeSouthboundTerminalReason(expired, TaskWorkerOptions{ActiveSouthboundDeferTimeout: 0}, errors.New("x"), base) != nil {
		t.Fatal("zero timeout must disable the cap")
	}

	// No reliable anchor (zero CreatedAt) must not terminalize, to avoid mis-kills.
	noAnchor := GatewayTaskQueueItem{TaskID: "t3"}
	if activeSouthboundTerminalReason(noAnchor, opts, errors.New("x"), base) != nil {
		t.Fatal("zero CreatedAt must not terminalize")
	}
}
