package core

import (
	"context"
	"fmt"
	"strings"
	"time"
)

type RuntimeExecutionRequest struct {
	AssistantID    string
	GatewayURL     string
	GatewayToken   string
	SessionKey     string
	Message        string
	RequestID      string // southbound/runtime task id (used as the runtime idempotencyKey)
	GatewayTaskID  string // gateway-facing task id (gw-xxx) for cross-layer log correlation
	TimeoutMs      int
	DeadlineSource string
	StaleTimeoutMs int
	OnProgress     func(context.Context, RuntimeProgressEvent) error
}

type RuntimeExecutionResult struct {
	ReplyText string
	Status    string
}

type RuntimeExecutor interface {
	ExecuteChat(ctx context.Context, request RuntimeExecutionRequest) (RuntimeExecutionResult, error)
}

type RuntimeProgressEvent struct {
	State          string
	Event          string
	RunID          string
	Text           string
	Detail         string
	Status         string
	ToolName       string
	LastProgressAt time.Time
	At             time.Time
}

// FaultDomain identifies which party in the upstream<->gateway<->runtime chain
// is responsible for an outcome. NONE marks success; INDETERMINATE marks a fault
// that could not be attributed (fail-closed: never default-blame the runtime).
type FaultDomain string

const (
	FaultDomainUpstream      FaultDomain = "UPSTREAM"
	FaultDomainGateway       FaultDomain = "GATEWAY"
	FaultDomainRuntime       FaultDomain = "RUNTIME"
	FaultDomainIndeterminate FaultDomain = "INDETERMINATE"
	FaultDomainNone          FaultDomain = "NONE"
)

// FaultOwner is the orthogonal "who decided" axis: it distinguishes a runtime
// that self-reported failure (owner=runtime) from a gateway that adjudicated the
// runtime failed (owner=gateway) even when FaultDomain is RUNTIME.
type FaultOwner string

const (
	FaultOwnerUpstream FaultOwner = "upstream"
	FaultOwnerGateway  FaultOwner = "gateway"
	FaultOwnerRuntime  FaultOwner = "runtime"
)

type AttributionConfidence string

const (
	ConfidenceHigh   AttributionConfidence = "high"
	ConfidenceMedium AttributionConfidence = "medium"
	ConfidenceLow    AttributionConfidence = "low"
)

// Reason sub-codes under each fault domain (domain.sub form). These are the
// attribution taxonomy codes carried by AttributedRuntimeError; they are
// independent of the legacy failed_reason_code task-status strings.
const (
	ReasonRuntimeExecError       = "runtime.exec_error"
	ReasonRuntimeConnectRejected = "runtime.connect_rejected"
	ReasonRuntimeChatRejected    = "runtime.chat_rejected"
	ReasonRuntimeUpgrade4xx      = "runtime.upgrade_4xx"
	ReasonRuntimeUpgrade5xx      = "runtime.upgrade_5xx"
	ReasonRuntimeStaleNoProgress = "runtime.stale_no_progress"
	ReasonRuntimeEmptyReply      = "runtime.empty_reply"
	ReasonRuntimeDeclaredFailed  = "runtime.declared_failed"

	ReasonGatewayDeadlineSelf        = "gateway.deadline_self"
	ReasonGatewayRuntimeDialFailed   = "gateway.runtime_dial_failed"
	ReasonGatewayInFlightUnreachable = "gateway.in_flight_unreachable"

	ReasonUpstreamTimeoutBudget = "upstream.timeout_budget_too_short"

	ReasonUndetermined = "undetermined"
	ReasonOK           = "ok"
)

// FaultAttributed is implemented by any error that carries a fault attribution
// stamped at its birth site. The service-layer classifier reads attribution via
// errors.As against this interface, so a typed error need only implement
// FaultAttribution() to be understood — no central registry of error types.
type FaultAttributed interface {
	FaultAttribution() AttributedRuntimeError
}

// AttributedRuntimeError is the unified fault stamp. It is produced at the error
// birth site (e.g. the WS read loop) carrying the four attribution axes plus a
// retryability hint and free-form evidence, and is read up the stack via
// errors.As. Diagnostic specifics (timeout, source, runId, status codes) go into
// EvidenceNote so the type stays uniform.
type AttributedRuntimeError struct {
	Domain       FaultDomain
	Reason       string
	Owner        FaultOwner
	Confidence   AttributionConfidence
	Retryable    bool
	RuntimeCode  string
	EvidenceNote string
	Cause        error
}

func (e *AttributedRuntimeError) Error() string {
	msg := fmt.Sprintf("[%s/%s owner=%s conf=%s]", e.Domain, e.Reason, e.Owner, e.Confidence)
	if code := strings.TrimSpace(e.RuntimeCode); code != "" {
		msg += " code=" + code
	}
	if note := strings.TrimSpace(e.EvidenceNote); note != "" {
		msg += " " + note
	}
	if e.Cause != nil {
		msg += ": " + e.Cause.Error()
	}
	return msg
}

func (e *AttributedRuntimeError) Unwrap() error { return e.Cause }

func (e *AttributedRuntimeError) FaultAttribution() AttributedRuntimeError { return *e }

type RuntimeStaleTimeoutError struct {
	Timeout        time.Duration
	LastProgressAt time.Time
	LastState      string
	LastText       string
}

func (e RuntimeStaleTimeoutError) Error() string {
	state := strings.TrimSpace(e.LastState)
	if state == "" {
		state = "unknown"
	}
	return fmt.Sprintf("runtime made no progress for %s after %s", e.Timeout, state)
}

// FaultAttribution maps a stale-progress timeout to the RUNTIME domain (the
// runtime went silent) but owner=gateway (the gateway made the timing call), at
// medium confidence — a too-short stale window can misfire. It stays retryable.
func (e RuntimeStaleTimeoutError) FaultAttribution() AttributedRuntimeError {
	return AttributedRuntimeError{
		Domain:       FaultDomainRuntime,
		Reason:       ReasonRuntimeStaleNoProgress,
		Owner:        FaultOwnerGateway,
		Confidence:   ConfidenceMedium,
		Retryable:    true,
		EvidenceNote: fmt.Sprintf("stale=%s last_state=%s", e.Timeout, strings.TrimSpace(e.LastState)),
	}
}

// DeadlineSource records where a runtime execution deadline came from so the
// gateway can attribute an overall-deadline timeout correctly and decide
// retryability.
const (
	// DeadlineSourceClientSupplied means the caller passed timeout_seconds; the
	// gateway honors it as a hard cap even while the runtime is still emitting
	// progress (the caller's own budget).
	DeadlineSourceClientSupplied = "client_supplied"
	// DeadlineSourceGatewayDefault means no caller timeout was given and the
	// gateway fell back to its own default; this deadline must NOT hard-kill a
	// runtime that is still making progress (stale-progress detection governs
	// liveness instead).
	DeadlineSourceGatewayDefault = "gateway_default"
)
