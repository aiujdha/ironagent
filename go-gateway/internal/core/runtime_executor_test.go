package core

import (
	"errors"
	"fmt"
	"testing"
	"time"
)

func TestAttributedDeadlineNotRetryable(t *testing.T) {
	t.Parallel()
	err := &AttributedRuntimeError{Domain: FaultDomainGateway, Reason: ReasonGatewayDeadlineSelf, Owner: FaultOwnerGateway, Confidence: ConfidenceHigh, Retryable: false}
	if retryQueueWorkerError(err) {
		t.Fatalf("retryQueueWorkerError(deadline) = true, want false")
	}
	if retryQueuedTaskError(err) {
		t.Fatalf("retryQueuedTaskError(deadline) = true, want false")
	}
	wrapped := fmt.Errorf("execute runtime: %w", err)
	if retryQueueWorkerError(wrapped) {
		t.Fatalf("retryQueueWorkerError(wrapped) = true, want false")
	}
	var attr *AttributedRuntimeError
	if !errors.As(wrapped, &attr) || attr.Reason != ReasonGatewayDeadlineSelf {
		t.Fatalf("errors.As failed to recover AttributedRuntimeError: %v", attr)
	}
}

func TestRuntimeStaleStaysRetryable(t *testing.T) {
	t.Parallel()
	// Regression guard: a runtime that truly went silent must remain retryable
	// even though gateway-origin deadlines are not.
	err := RuntimeStaleTimeoutError{Timeout: 10 * time.Minute}
	if !retryQueuedTaskError(err) {
		t.Fatalf("retryQueuedTaskError(stale) = false, want true")
	}
	fa := queueWorkerFaultDomain(err)
	if fa.Domain != FaultDomainRuntime || fa.Reason != ReasonRuntimeStaleNoProgress || fa.Owner != FaultOwnerGateway {
		t.Fatalf("stale fault domain = %+v", fa)
	}
}

func TestInFlightRetryable(t *testing.T) {
	t.Parallel()
	err := &AttributedRuntimeError{Domain: FaultDomainGateway, Reason: ReasonGatewayInFlightUnreachable, Owner: FaultOwnerGateway, Confidence: ConfidenceMedium, Retryable: true}
	if !retryQueuedTaskError(err) {
		t.Fatalf("retryQueuedTaskError(in_flight) = false, want true")
	}
}

func TestQueueWorkerFaultDomainFailClosed(t *testing.T) {
	t.Parallel()
	if fa := queueWorkerFaultDomain(nil); fa.Domain != FaultDomainNone {
		t.Fatalf("nil fault domain = %v, want NONE", fa.Domain)
	}
	in := &AttributedRuntimeError{Domain: FaultDomainRuntime, Reason: ReasonRuntimeExecError, Owner: FaultOwnerRuntime, Confidence: ConfidenceHigh}
	if fa := queueWorkerFaultDomain(in); fa.Domain != FaultDomainRuntime || fa.Reason != ReasonRuntimeExecError {
		t.Fatalf("typed fault domain = %+v", fa)
	}
	// Fail-closed: an unattributed error is INDETERMINATE owned by the gateway,
	// NEVER default-blamed on the runtime.
	fa := queueWorkerFaultDomain(errors.New("some unrecognized failure"))
	if fa.Domain != FaultDomainIndeterminate || fa.Owner != FaultOwnerGateway {
		t.Fatalf("unknown fault domain = %+v, want INDETERMINATE/gateway", fa)
	}
}

func TestDeadlineReasonCodeMapping(t *testing.T) {
	t.Parallel()
	err := &AttributedRuntimeError{Reason: ReasonGatewayDeadlineSelf}
	// failed_reason_code stays the legacy runtime_execution_failed for backward
	// compatibility; the deadline attribution lives in the fault_domain fields.
	if got := queueWorkerReasonCode(err); got != "runtime_execution_failed" {
		t.Fatalf("queueWorkerReasonCode = %q, want runtime_execution_failed", got)
	}
	if got := queueWorkerErrorLocation(err); got != "runtime_ws.deadline" {
		t.Fatalf("queueWorkerErrorLocation = %q, want runtime_ws.deadline", got)
	}
	inflight := &AttributedRuntimeError{Reason: ReasonGatewayInFlightUnreachable}
	if got := queueWorkerReasonCode(inflight); got != "runtime_in_flight" {
		t.Fatalf("queueWorkerReasonCode(in_flight) = %q, want runtime_in_flight", got)
	}
}

// TestRetryDecisionRegressions locks the retry semantics that the attribution
// short-circuit broke: a non-retryable stamp must NOT suppress the legacy
// transport/capacity net, and only a gateway-origin deadline is a hard no-retry.
func TestRetryDecisionRegressions(t *testing.T) {
	t.Parallel()
	dialFail := &AttributedRuntimeError{Domain: FaultDomainGateway, Reason: ReasonGatewayRuntimeDialFailed, Owner: FaultOwnerGateway, Confidence: ConfidenceHigh, Retryable: true, Cause: errors.New("connect runtime websocket: dial tcp: connection refused")}
	atCapacity := &AttributedRuntimeError{Domain: FaultDomainRuntime, Reason: ReasonRuntimeExecError, Owner: FaultOwnerRuntime, Confidence: ConfidenceHigh, Retryable: false, Cause: errors.New("runtime chat error: provider at capacity, allocated quota exceeded")}
	rateLimited := &AttributedRuntimeError{Domain: FaultDomainRuntime, Reason: ReasonRuntimeChatRejected, Owner: FaultOwnerRuntime, Confidence: ConfidenceHigh, Retryable: false, Cause: errors.New("runtime chat rejected: rate limit exceeded, too many requests")}
	deadlineGw := &AttributedRuntimeError{Domain: FaultDomainGateway, Reason: ReasonGatewayDeadlineSelf, Owner: FaultOwnerGateway, Confidence: ConfidenceHigh, Retryable: false}
	deadlineUp := &AttributedRuntimeError{Domain: FaultDomainUpstream, Reason: ReasonUpstreamTimeoutBudget, Owner: FaultOwnerUpstream, Confidence: ConfidenceHigh, Retryable: false}
	inflight := &AttributedRuntimeError{Domain: FaultDomainGateway, Reason: ReasonGatewayInFlightUnreachable, Owner: FaultOwnerGateway, Confidence: ConfidenceMedium, Retryable: true}
	stale := RuntimeStaleTimeoutError{Timeout: time.Minute}
	plainConnRefused := errors.New("connect runtime websocket: dial tcp 1.2.3.4:443: connect: connection refused")

	cases := []struct {
		name string
		err  error
		want bool
	}{
		{"dial failure (transient) retryable", dialFail, true},
		{"exec-error carrying at_capacity falls through to legacy net", atCapacity, true},
		{"chat-reject carrying rate-limit falls through to legacy net", rateLimited, true},
		{"gateway-default deadline NOT retryable (B1)", deadlineGw, false},
		{"client-supplied deadline NOT retryable", deadlineUp, false},
		{"in_flight retryable", inflight, true},
		{"stale-progress retryable", stale, true},
		{"plain connection-refused retryable (legacy net)", plainConnRefused, true},
	}
	for _, tc := range cases {
		if got := retryQueuedTaskError(tc.err); got != tc.want {
			t.Errorf("%s: retryQueuedTaskError = %v, want %v", tc.name, got, tc.want)
		}
		if got := retryQueueWorkerError(tc.err); got != tc.want {
			t.Errorf("%s: retryQueueWorkerError = %v, want %v", tc.name, got, tc.want)
		}
	}
}

func TestDeadlineSourceForTimeoutSeconds(t *testing.T) {
	t.Parallel()
	if got := deadlineSourceForTimeoutSeconds(30); got != DeadlineSourceClientSupplied {
		t.Fatalf("deadlineSourceForTimeoutSeconds(30) = %q, want %q", got, DeadlineSourceClientSupplied)
	}
	if got := deadlineSourceForTimeoutSeconds(0); got != DeadlineSourceGatewayDefault {
		t.Fatalf("deadlineSourceForTimeoutSeconds(0) = %q, want %q", got, DeadlineSourceGatewayDefault)
	}
}

func TestHooksDeadlineSource(t *testing.T) {
	t.Parallel()
	// Unknown source (nil / empty hooks) must fail-closed to gateway_default,
	// NOT client_supplied — a deadline we cannot prove the caller set must not be
	// blamed on UPSTREAM (review #4).
	if got := hooksDeadlineSource(nil); got != DeadlineSourceGatewayDefault {
		t.Fatalf("hooksDeadlineSource(nil) = %q, want %q", got, DeadlineSourceGatewayDefault)
	}
	if got := hooksDeadlineSource(&southboundExecutionHooks{}); got != DeadlineSourceGatewayDefault {
		t.Fatalf("hooksDeadlineSource(empty) = %q, want %q", got, DeadlineSourceGatewayDefault)
	}
	if got := hooksDeadlineSource(&southboundExecutionHooks{deadlineSource: DeadlineSourceClientSupplied}); got != DeadlineSourceClientSupplied {
		t.Fatalf("hooksDeadlineSource(client) = %q, want %q", got, DeadlineSourceClientSupplied)
	}
}

func TestQueueWorkerFaultDomainStringArms(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name       string
		err        error
		wantDomain FaultDomain
		wantReason string
	}{
		{"runtime gateway_url unconfigured", errors.New("assistant x has no runtime gateway_url"), FaultDomainGateway, "gateway.runtime_unconfigured"},
		{"result file sync failed", errors.New("result file fetch failed"), FaultDomainGateway, "gateway.result_sync_failed"},
		{"dial string arm", errors.New("connect runtime websocket: dial tcp"), FaultDomainGateway, ReasonGatewayRuntimeDialFailed},
		{"unknown fail-closed to indeterminate", errors.New("totally unexpected failure"), FaultDomainIndeterminate, ReasonUndetermined},
	}
	for _, tc := range cases {
		fa := queueWorkerFaultDomain(tc.err)
		if fa.Domain != tc.wantDomain || fa.Reason != tc.wantReason {
			t.Errorf("%s: got %s/%s, want %s/%s", tc.name, fa.Domain, fa.Reason, tc.wantDomain, tc.wantReason)
		}
		// Fail-closed invariant: a string-matched or unknown error must never be
		// silently attributed to the runtime — owner stays gateway.
		if fa.Owner != FaultOwnerGateway {
			t.Errorf("%s: owner = %s, want gateway (fail-closed)", tc.name, fa.Owner)
		}
	}
}

// (TestIsRuntimeInFlight removed with the in_flight-await mechanism it covered.)
