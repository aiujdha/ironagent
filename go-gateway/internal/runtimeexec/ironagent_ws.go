package runtimeexec

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
	"unicode"

	"github.com/gorilla/websocket"

	"ironagent/go-gateway/internal/core"
)

type IronAgentWebSocketExecutor struct {
	HandshakeTimeout time.Duration
	// DeadlineBackstop caps how long a gateway-default (no client timeout) task
	// that keeps streaming progress may run before its overall deadline fires.
	// Stale-progress detection governs liveness independently; this only stops a
	// forever-streaming task from holding a worker. Configurable so it can be
	// tuned against the worker-drain timeout and runtime capacity in a given
	// deployment. Zero falls back to defaultGatewayDefaultDeadlineBackstop.
	DeadlineBackstop time.Duration
	Logger           *slog.Logger
}

func NewIronAgentWebSocketExecutor(logger ...*slog.Logger) *IronAgentWebSocketExecutor {
	var selected *slog.Logger
	if len(logger) > 0 {
		selected = logger[0]
	}
	return &IronAgentWebSocketExecutor{
		HandshakeTimeout: 10 * time.Second,
		DeadlineBackstop: defaultGatewayDefaultDeadlineBackstop,
		Logger:           selected,
	}
}

// defaultGatewayDefaultDeadlineBackstop is the conservative DEFAULT cap applied
// to a gateway-default overall deadline (A1) when no explicit backstop is
// configured. A runtime that keeps emitting progress is governed by
// stale-progress detection, not by this cap; the cap only stops a task that
// streams progress forever from occupying a worker. It is intentionally well
// under a typical worker-drain / runtime-capacity window; operators who run
// genuinely long gateway-default tasks raise it via the executor's
// DeadlineBackstop (GO_GATEWAY_TASK_WORKER_GATEWAY_DEFAULT_DEADLINE_BACKSTOP).
// A client-supplied timeout is always honored as a hard cap instead.
const defaultGatewayDefaultDeadlineBackstop = 20 * time.Minute

// taskLogID returns the gateway-facing task id (gw-xxx) for cross-layer log
// correlation, falling back to the southbound/runtime id when the gateway id was
// not threaded through (e.g. the sync path). The southbound id is always logged
// separately under "southbound_task_id" so both remain queryable.
func taskLogID(request core.RuntimeExecutionRequest) string {
	if request.GatewayTaskID != "" {
		return request.GatewayTaskID
	}
	return request.RequestID
}

func (e *IronAgentWebSocketExecutor) ExecuteChat(
	ctx context.Context,
	request core.RuntimeExecutionRequest,
) (core.RuntimeExecutionResult, error) {
	if strings.TrimSpace(request.GatewayURL) == "" {
		return core.RuntimeExecutionResult{}, errors.New("runtime gateway_url is required")
	}
	if strings.TrimSpace(request.GatewayToken) == "" {
		return core.RuntimeExecutionResult{}, errors.New("runtime gateway_token is required")
	}
	if strings.TrimSpace(request.Message) == "" {
		return core.RuntimeExecutionResult{}, errors.New("runtime message is required")
	}

	timeout := time.Duration(request.TimeoutMs) * time.Millisecond
	if timeout <= 0 {
		timeout = 10 * time.Minute
	}
	// A1: a gateway-default overall deadline must not hard-kill a runtime that is
	// still emitting progress. Widen it to a backstop so stale-progress detection
	// governs liveness; a client-supplied timeout stays a hard cap.
	// Only widen when stale-progress detection is actually enabled — if stale is
	// disabled (StaleTimeoutMs==0) the backstop would remove ALL liveness
	// governance, so keep the original cap in that config.
	if request.DeadlineSource == core.DeadlineSourceGatewayDefault && request.StaleTimeoutMs > 0 {
		backstop := e.DeadlineBackstop
		if backstop <= 0 {
			backstop = defaultGatewayDefaultDeadlineBackstop
		}
		if timeout < backstop {
			// Observability: a widened deadline lets this task hold a worker up to
			// `backstop`. Emit it so ops can see worker-occupancy risk under a
			// limited-monitoring deployment (grep gateway_runtime_*_deadline_widened).
			e.logInfo(
				"gateway_runtime_gateway_default_deadline_widened",
				"task_id", taskLogID(request),
				"southbound_task_id", request.RequestID,
				"original_timeout_ms", request.TimeoutMs,
				"backstop_ms", backstop.Milliseconds(),
				"stale_timeout_ms", request.StaleTimeoutMs,
			)
			timeout = backstop
		}
	}
	execCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	e.logInfo(
		"运行时WebSocket执行开始",
		"assistant_id", request.AssistantID,
		"task_id", taskLogID(request),
		"southbound_task_id", request.RequestID,
		"session_key", request.SessionKey,
		"gateway_url", request.GatewayURL,
		"timeout_ms", int(timeout/time.Millisecond),
	)
	conn, resp, err := websocket.DefaultDialer.DialContext(execCtx, runtimeWebSocketURL(request.GatewayURL), http.Header{})
	if err != nil {
		if resp != nil {
			defer resp.Body.Close()
		}
		e.logWarn(
			"运行时WebSocket连接失败",
			"assistant_id", request.AssistantID,
			"task_id", taskLogID(request),
			"southbound_task_id", request.RequestID,
			"gateway_url", request.GatewayURL,
			"error", err,
		)
		// RULE 3: the upgrade *http.Response distinguishes a runtime that answered
		// (4xx/5xx on upgrade = runtime fault) from a pure dial error (the gateway
		// could not reach the runtime = gateway fault).
		cause := fmt.Errorf("connect runtime websocket: %w", err)
		switch {
		case resp != nil && resp.StatusCode >= 400 && resp.StatusCode < 500:
			return core.RuntimeExecutionResult{}, &core.AttributedRuntimeError{Domain: core.FaultDomainRuntime, Reason: core.ReasonRuntimeUpgrade4xx, Owner: core.FaultOwnerRuntime, Confidence: core.ConfidenceHigh, EvidenceNote: fmt.Sprintf("upgrade_status=%d", resp.StatusCode), Cause: cause}
		case resp != nil && resp.StatusCode >= 500:
			return core.RuntimeExecutionResult{}, &core.AttributedRuntimeError{Domain: core.FaultDomainRuntime, Reason: core.ReasonRuntimeUpgrade5xx, Owner: core.FaultOwnerRuntime, Confidence: core.ConfidenceHigh, EvidenceNote: fmt.Sprintf("upgrade_status=%d", resp.StatusCode), Cause: cause}
		default:
			// A pure dial failure (connection-refused / DNS / i/o-timeout / TLS) is
			// the canonical transient case — Retryable: true, matching the
			// pre-attribution behavior where this flowed through
			// isRetryableTransportError and retried up to MaxAttempts.
			return core.RuntimeExecutionResult{}, &core.AttributedRuntimeError{Domain: core.FaultDomainGateway, Reason: core.ReasonGatewayRuntimeDialFailed, Owner: core.FaultOwnerGateway, Confidence: core.ConfidenceHigh, Retryable: true, Cause: cause}
		}
	}
	done := make(chan struct{})
	go func() {
		select {
		case <-execCtx.Done():
			_ = conn.SetReadDeadline(time.Now())
		case <-done:
		}
	}()
	defer close(done)
	defer conn.Close()

	if err := e.connect(execCtx, conn, request); err != nil {
		e.logWarn(
			"运行时WebSocket握手失败",
			"assistant_id", request.AssistantID,
			"task_id", taskLogID(request),
			"southbound_task_id", request.RequestID,
			"gateway_url", request.GatewayURL,
			"error", err,
		)
		return core.RuntimeExecutionResult{}, err
	}
	result, err := e.sendAndWait(execCtx, conn, request)
	if err != nil {
		e.logWarn(
			"运行时WebSocket执行失败",
			"assistant_id", request.AssistantID,
			"task_id", taskLogID(request),
			"southbound_task_id", request.RequestID,
			"session_key", request.SessionKey,
			"error", err,
		)
		return core.RuntimeExecutionResult{}, err
	}
	e.logInfo(
		"运行时WebSocket执行完成",
		"assistant_id", request.AssistantID,
		"task_id", taskLogID(request),
		"southbound_task_id", request.RequestID,
		"session_key", request.SessionKey,
		"status", result.Status,
		"reply_chars", len([]rune(result.ReplyText)),
	)
	return result, nil
}

func (e *IronAgentWebSocketExecutor) connect(
	ctx context.Context,
	conn *websocket.Conn,
	request core.RuntimeExecutionRequest,
) error {
	handshakeTimeout := e.HandshakeTimeout
	if handshakeTimeout <= 0 {
		handshakeTimeout = 10 * time.Second
	}
	challengeCtx, cancel := context.WithTimeout(ctx, handshakeTimeout)
	defer cancel()
	for {
		frame, err := readFrame(challengeCtx, conn)
		if err != nil {
			return fmt.Errorf("read connect challenge: %w", err)
		}
		if frame.Type == "event" && frame.Event == "connect.challenge" {
			break
		}
	}

	params := map[string]any{
		"minProtocol": 3,
		"maxProtocol": 3,
		"client": map[string]any{
			"id":          "ironagent-tui",
			"displayName": "IronAgent Go Gateway",
			"version":     "ironagent-go-gateway",
			"platform":    "server",
			"mode":        "ui",
		},
		"caps":   []string{"tool-events"},
		"role":   "operator",
		"scopes": []string{"operator.admin", "operator.read", "operator.write", "operator.approvals"},
		"auth": map[string]any{
			"token": strings.TrimSpace(request.GatewayToken),
		},
		"userAgent": "IronAgent Go Gateway",
		"locale":    "zh-CN",
	}
	if err := writeRequest(conn, "connect-1", "connect", params); err != nil {
		return fmt.Errorf("send connect: %w", err)
	}
	frame, err := waitResponse(ctx, conn, "connect-1")
	if err != nil {
		return err
	}
	if !frame.OK {
		return &core.AttributedRuntimeError{
			Domain:      core.FaultDomainRuntime,
			Reason:      core.ReasonRuntimeConnectRejected,
			Owner:       core.FaultOwnerRuntime,
			Confidence:  core.ConfidenceHigh,
			Retryable:   frame.errorRetryable(),
			RuntimeCode: frame.errorCode(),
			Cause:       fmt.Errorf("runtime connect rejected: %s", frame.errorMessage()),
		}
	}
	e.logInfo(
		"运行时WebSocket握手完成",
		"assistant_id", request.AssistantID,
		"task_id", taskLogID(request),
		"southbound_task_id", request.RequestID,
		"gateway_url", request.GatewayURL,
	)
	return nil
}

func (e *IronAgentWebSocketExecutor) sendAndWait(
	ctx context.Context,
	conn *websocket.Conn,
	request core.RuntimeExecutionRequest,
) (core.RuntimeExecutionResult, error) {
	runID := strings.TrimSpace(request.RequestID)
	if runID == "" {
		runID = fmt.Sprintf("go-gateway-%d", time.Now().UnixNano())
	}
	sessionKey := strings.TrimSpace(request.SessionKey)
	if sessionKey == "" {
		sessionKey = "agent:" + strings.TrimSpace(request.AssistantID) + ":main"
	}
	params := map[string]any{
		"sessionKey":     sessionKey,
		"message":        request.Message,
		"idempotencyKey": runID,
		"timeoutMs":      request.TimeoutMs,
	}
	staleTimeout := time.Duration(request.StaleTimeoutMs) * time.Millisecond
	lastProgressAt := time.Now().UTC()
	lastProgressState := "chat_sent"
	notifyRuntimeProgress(ctx, request, core.RuntimeProgressEvent{
		State:          lastProgressState,
		Event:          "chat.send",
		RunID:          runID,
		At:             lastProgressAt,
		LastProgressAt: lastProgressAt,
	})
	if err := writeRequest(conn, "chat-1", "chat.send", params); err != nil {
		return core.RuntimeExecutionResult{}, fmt.Errorf("send runtime chat: %w", err)
	}
	e.logInfo(
		"运行时聊天发送完成",
		"assistant_id", request.AssistantID,
		"task_id", taskLogID(request),
		"southbound_task_id", request.RequestID,
		"run_id", runID,
		"session_key", sessionKey,
		"message_chars", len([]rune(request.Message)),
	)
	ack, err := waitResponseWithProgress(ctx, conn, "chat-1", staleTimeout, lastProgressAt)
	if err != nil {
		return core.RuntimeExecutionResult{}, err
	}
	if !ack.OK {
		return core.RuntimeExecutionResult{}, &core.AttributedRuntimeError{
			Domain:      core.FaultDomainRuntime,
			Reason:      core.ReasonRuntimeChatRejected,
			Owner:       core.FaultOwnerRuntime,
			Confidence:  core.ConfidenceHigh,
			Retryable:   ack.errorRetryable(),
			RuntimeCode: ack.errorCode(),
			Cause:       fmt.Errorf("runtime chat rejected: %s", ack.errorMessage()),
		}
	}
	if ackPayload, perr := parseChatPayload(ack.Payload); perr == nil && strings.EqualFold(strings.TrimSpace(ackPayload.Status), "in_flight") {
		// The runtime reports the task is already executing under this
		// idempotencyKey, but this (reconnected) connection is never registered
		// to receive that run's events and cannot retrieve its result (no
		// subscription; dedupe stores no result payload). Fail fast as retryable
		// instead of blocking until the stale/overall deadline. NOTE: this path
		// does NOT abort the prior run, so a retry with the same idempotencyKey
		// may report in_flight again until the prior run terminalizes or is
		// cancelled elsewhere — convergence is not guaranteed here. The clean fix
		// belongs in the runtime (register the reconnecting connection, or expose
		// chat.await); see docs/runtime-in-flight-result-retrieval-requirement-*.
		// run_id == idempotencyKey here (see params above), so a non-converging
		// in_flight loop shows up as the SAME run_id retried repeatedly. The ack
		// may also echo the prior run's id under runId/run_id; when present and
		// distinct, log it as runtime_reported_run_id so a stuck prior run is
		// greppable without threading the queue attempt count into this layer.
		inFlightAttrs := []any{
			"assistant_id", request.AssistantID,
			"task_id", taskLogID(request),
			"southbound_task_id", request.RequestID,
			"run_id", runID,
			"session_key", sessionKey,
		}
		if reportedRunID := ackPayload.runID(); reportedRunID != "" && reportedRunID != runID {
			inFlightAttrs = append(inFlightAttrs, "runtime_reported_run_id", reportedRunID)
		}
		e.logWarn(
			"运行时报告任务已在执行中(in_flight)，本连接无法获取其结果",
			inFlightAttrs...,
		)
		return core.RuntimeExecutionResult{}, &core.AttributedRuntimeError{
			Domain:       core.FaultDomainGateway,
			Reason:       core.ReasonGatewayInFlightUnreachable,
			Owner:        core.FaultOwnerGateway,
			Confidence:   core.ConfidenceMedium,
			Retryable:    true,
			EvidenceNote: "run=" + runID + " in_flight; reconnected connection cannot retrieve result",
		}
	}
	e.logInfo(
		"运行时聊天已接收",
		"assistant_id", request.AssistantID,
		"task_id", taskLogID(request),
		"southbound_task_id", request.RequestID,
		"run_id", runID,
		"session_key", sessionKey,
	)
	lastProgressAt = time.Now().UTC()
	lastProgressState = "ack"
	notifyRuntimeProgress(ctx, request, core.RuntimeProgressEvent{
		State:          lastProgressState,
		Event:          "chat.ack",
		RunID:          runID,
		At:             lastProgressAt,
		LastProgressAt: lastProgressAt,
	})

	accumulatedDeltaText := ""
	for {
		frame, err := readFrameWithProgressDeadline(ctx, conn, staleTimeout, lastProgressAt)
		if err != nil {
			if isReadTimeout(err) && staleTimeout > 0 && time.Since(lastProgressAt) >= staleTimeout {
				e.cancelRuntimeChat(ctx, conn, request, sessionKey, runID, "stale_progress_timeout", err)
				return core.RuntimeExecutionResult{}, core.RuntimeStaleTimeoutError{
					Timeout:        staleTimeout,
					LastProgressAt: lastProgressAt,
					LastState:      lastProgressState,
					LastText:       strings.TrimSpace(accumulatedDeltaText),
				}
			}
			if contextDeadlineReached(ctx) {
				e.cancelRuntimeChat(ctx, conn, request, sessionKey, runID, "execution_context_done", context.DeadlineExceeded)
				source := request.DeadlineSource
				if source == "" {
					// Fail-closed: an unset source is a gateway-default deadline,
					// never blamed on the caller (matches hooksDeadlineSource).
					source = core.DeadlineSourceGatewayDefault
				}
				attr := &core.AttributedRuntimeError{
					Domain:       core.FaultDomainGateway,
					Reason:       core.ReasonGatewayDeadlineSelf,
					Owner:        core.FaultOwnerGateway,
					Confidence:   core.ConfidenceHigh,
					Retryable:    false,
					EvidenceNote: fmt.Sprintf("source=%s timeout=%s last_progress=%s last_state=%s", source, time.Duration(request.TimeoutMs)*time.Millisecond, lastProgressAt.UTC().Format(time.RFC3339Nano), lastProgressState),
				}
				switch {
				case source == core.DeadlineSourceClientSupplied:
					attr.Domain = core.FaultDomainUpstream
					attr.Reason = core.ReasonUpstreamTimeoutBudget
					attr.Owner = core.FaultOwnerUpstream
				case staleTimeout > 0 && time.Since(lastProgressAt) < staleTimeout:
					// #8: the gateway-default backstop fired while the runtime was
					// still actively progressing (last progress within the stale
					// window) — a healthy long-runner hitting the worker-occupancy
					// cap, not a confident gateway fault. Mark indeterminate at low
					// confidence so dashboards don't over-count gateway deadlines.
					attr.Domain = core.FaultDomainIndeterminate
					attr.Reason = core.ReasonUndetermined
					attr.Confidence = core.ConfidenceLow
					attr.EvidenceNote += " (backstop fired while still progressing)"
				}
				return core.RuntimeExecutionResult{}, attr
			}
			return core.RuntimeExecutionResult{}, fmt.Errorf("wait runtime chat final: %w", err)
		}
		if frame.Type != "event" {
			continue
		}
		payload, err := parseChatPayload(frame.Payload)
		if err != nil || payload.runID() != runID {
			continue
		}
		if isRuntimeProgressEvent(frame.Event, payload.State) {
			now := time.Now().UTC()
			progressState := runtimeProgressState(frame.Event, payload.State)
			text := payload.safeProgressText()
			if runtimeProgressExtendsStale(frame.Event, progressState, text) {
				lastProgressAt = now
				lastProgressState = progressState
			}
			notifyRuntimeProgress(ctx, request, core.RuntimeProgressEvent{
				State:          progressState,
				Event:          strings.TrimSpace(frame.Event),
				RunID:          runID,
				Text:           text,
				Detail:         strings.TrimSpace(payload.Detail),
				Status:         strings.TrimSpace(payload.Status),
				ToolName:       payload.toolName(),
				At:             now,
				LastProgressAt: lastProgressAt,
			})
			continue
		}
		if !strings.EqualFold(strings.TrimSpace(frame.Event), "chat") {
			continue
		}
		switch strings.TrimSpace(payload.State) {
		case "delta":
			lastProgressAt = time.Now().UTC()
			lastProgressState = "delta"
			text := extractMessageTextRaw(payload.Message)
			if strings.TrimSpace(text) != "" {
				accumulatedDeltaText = mergeRuntimeDeltaText(accumulatedDeltaText, text)
			}
			notifyRuntimeProgress(ctx, request, core.RuntimeProgressEvent{
				State:          lastProgressState,
				Event:          "chat",
				RunID:          runID,
				Text:           text,
				At:             lastProgressAt,
				LastProgressAt: lastProgressAt,
			})
		case "final":
			lastProgressAt = time.Now().UTC()
			lastProgressState = "final"
			finalText := extractMessageText(payload.Message)
			replyText := selectRuntimeReplyText(finalText, accumulatedDeltaText, "")
			if replyText == "" {
				replyText = selectRuntimeReplyText(finalText, accumulatedDeltaText, loadLatestAssistantText(ctx, conn, sessionKey))
			}
			e.logInfo(
				"运行时聊天最终响应",
				"assistant_id", request.AssistantID,
				"task_id", taskLogID(request),
				"southbound_task_id", request.RequestID,
				"run_id", runID,
				"session_key", sessionKey,
				"reply_chars", len([]rune(replyText)),
			)
			notifyRuntimeProgress(ctx, request, core.RuntimeProgressEvent{
				State:          lastProgressState,
				Event:          "chat",
				RunID:          runID,
				Text:           replyText,
				At:             lastProgressAt,
				LastProgressAt: lastProgressAt,
			})
			return core.RuntimeExecutionResult{
				ReplyText: replyText,
				Status:    "ok",
			}, nil
		case "error":
			lastProgressAt = time.Now().UTC()
			lastProgressState = "error"
			errorMessage := strings.TrimSpace(payload.ErrorMessage)
			e.logWarn(
				"运行时聊天错误事件",
				"assistant_id", request.AssistantID,
				"task_id", taskLogID(request),
				"southbound_task_id", request.RequestID,
				"run_id", runID,
				"session_key", sessionKey,
				"error", errorMessage,
			)
			notifyRuntimeProgress(ctx, request, core.RuntimeProgressEvent{
				State:          lastProgressState,
				Event:          "chat",
				RunID:          runID,
				Text:           errorMessage,
				At:             lastProgressAt,
				LastProgressAt: lastProgressAt,
			})
			return core.RuntimeExecutionResult{}, &core.AttributedRuntimeError{
				Domain:     core.FaultDomainRuntime,
				Reason:     core.ReasonRuntimeExecError,
				Owner:      core.FaultOwnerRuntime,
				Confidence: core.ConfidenceHigh,
				Cause:      fmt.Errorf("runtime chat error: %s", errorMessage),
			}
		}
	}
}

func (e *IronAgentWebSocketExecutor) cancelRuntimeChat(
	_ context.Context,
	conn *websocket.Conn,
	request core.RuntimeExecutionRequest,
	sessionKey string,
	runID string,
	reason string,
	cause error,
) {
	if conn == nil {
		return
	}
	// Runtime exposes the cancel operation as the "chat.abort" method; its
	// ChatAbortParamsSchema is additionalProperties:false and accepts only
	// sessionKey + optional runId, so reason must NOT be sent on the wire
	// (it is kept for local logging below). Sending "chat.cancel" or an extra
	// reason field makes the runtime reject the request and the abort never runs.
	err := writeRequest(conn, "chat-abort-1", "chat.abort", map[string]any{
		"sessionKey": sessionKey,
		"runId":      runID,
	})
	attrs := []any{
		"assistant_id", request.AssistantID,
		"task_id", taskLogID(request),
		"southbound_task_id", request.RequestID,
		"run_id", runID,
		"session_key", sessionKey,
		"cancel_reason", strings.TrimSpace(reason),
	}
	if cause != nil {
		attrs = append(attrs, "cause", cause)
	}
	if err != nil {
		e.logWarn("运行时聊天主动取消失败", append(attrs, "error", err)...)
		return
	}
	e.logWarn("运行时聊天已主动取消", attrs...)
}

func (e *IronAgentWebSocketExecutor) logInfo(message string, args ...any) {
	if e.Logger == nil {
		return
	}
	e.Logger.Info(message, args...)
}

func (e *IronAgentWebSocketExecutor) logWarn(message string, args ...any) {
	if e.Logger == nil {
		return
	}
	e.Logger.Warn(message, args...)
}

func loadLatestAssistantText(ctx context.Context, conn *websocket.Conn, sessionKey string) string {
	if err := writeRequest(conn, "chat-history-1", "chat.history", map[string]any{
		"sessionKey": sessionKey,
		"limit":      20,
	}); err != nil {
		return ""
	}
	frame, err := waitResponse(ctx, conn, "chat-history-1")
	if err != nil || !frame.OK {
		return ""
	}
	return latestAssistantTextFromHistory(frame.Payload)
}

type wsFrame struct {
	Type    string          `json:"type"`
	ID      string          `json:"id"`
	OK      bool            `json:"ok"`
	Event   string          `json:"event"`
	Payload json.RawMessage `json:"payload"`
	Error   *wsError        `json:"error"`
}

type wsError struct {
	Code      string `json:"code"`
	Message   string `json:"message"`
	Retryable *bool  `json:"retryable"`
	// RetryAfterMs is parsed but NOT yet wired into the retry-delay scheduler
	// (retries currently use jitteredRetryDelay). Honoring a runtime-requested
	// cool-down is a follow-up; kept here so the wire field is not dropped.
	RetryAfterMs int `json:"retryAfterMs"`
}

func (f wsFrame) errorMessage() string {
	if f.Error == nil {
		return "unknown error"
	}
	if strings.TrimSpace(f.Error.Message) != "" {
		return f.Error.Message
	}
	return f.Error.Code
}

func (f wsFrame) errorCode() string {
	if f.Error == nil {
		return ""
	}
	return strings.TrimSpace(f.Error.Code)
}

// errorRetryable returns the runtime-declared retryable hint. The iron-agent
// ErrorShape carries a `retryable` bool; gateway adopts it directly instead of
// guessing from message substrings. Defaults to false (fail-closed) when absent.
func (f wsFrame) errorRetryable() bool {
	if f.Error == nil || f.Error.Retryable == nil {
		return false
	}
	return *f.Error.Retryable
}

type chatPayload struct {
	RunID         string          `json:"runId"`
	RunIDSnake    string          `json:"run_id"`
	State         string          `json:"state"`
	Message       json.RawMessage `json:"message"`
	Text          string          `json:"text"`
	Output        string          `json:"output"`
	Content       string          `json:"content"`
	Detail        string          `json:"detail"`
	Status        string          `json:"status"`
	ToolName      string          `json:"toolName"`
	ToolNameSnake string          `json:"tool_name"`
	ErrorMessage  string          `json:"errorMessage"`
}

func (p chatPayload) runID() string {
	if strings.TrimSpace(p.RunID) != "" {
		return strings.TrimSpace(p.RunID)
	}
	return strings.TrimSpace(p.RunIDSnake)
}

func (p chatPayload) progressText() string {
	candidates := []string{
		extractMessageText(p.Message),
		p.Text,
		p.Detail,
		p.Status,
		p.ToolName,
		p.ToolNameSnake,
		p.State,
	}
	for _, candidate := range candidates {
		if text := strings.TrimSpace(candidate); text != "" {
			return text
		}
	}
	return ""
}

func (p chatPayload) safeProgressText() string {
	candidates := []string{
		p.Status,
		p.Detail,
		extractMessageText(p.Message),
		p.Text,
		p.Output,
		p.Content,
		p.ToolName,
		p.ToolNameSnake,
		p.State,
	}
	for _, candidate := range candidates {
		if text := strings.TrimSpace(candidate); text != "" {
			return text
		}
	}
	return ""
}

func (p chatPayload) toolName() string {
	if name := strings.TrimSpace(p.ToolName); name != "" {
		return name
	}
	return strings.TrimSpace(p.ToolNameSnake)
}

func isRuntimeProgressEvent(event string, state string) bool {
	normalizedEvent := strings.ToLower(strings.TrimSpace(event))
	normalizedState := strings.ToLower(strings.TrimSpace(state))
	if isToolProgressToken(normalizedEvent) || isToolProgressToken(normalizedState) {
		return true
	}
	if normalizedEvent == "chat" {
		switch normalizedState {
		case "progress", "heartbeat", "tool", "tool_running", "running", "thinking", "reasoning", "planning", "plan", "step":
			return true
		default:
			return false
		}
	}
	switch normalizedEvent {
	case "chat.progress", "chat.heartbeat", "chat.tool", "chat.thinking", "chat.reasoning", "chat.planning", "chat.plan", "chat.step", "assistant.progress", "assistant.thinking", "assistant.reasoning", "assistant.planning", "assistant.plan", "assistant.step", "tool", "tool.progress", "tool.heartbeat", "tool-event", "tool-events":
		return true
	default:
		return false
	}
}

func runtimeProgressState(event string, state string) string {
	if trimmed := strings.TrimSpace(state); trimmed != "" {
		return trimmed
	}
	normalizedEvent := strings.ToLower(strings.TrimSpace(event))
	switch normalizedEvent {
	case "chat.progress", "tool.progress":
		return "progress"
	case "chat.heartbeat", "tool.heartbeat":
		return "heartbeat"
	case "chat.tool", "tool", "tool-event", "tool-events":
		return "tool_running"
	default:
		return normalizedEvent
	}
}

func runtimeProgressExtendsStale(event string, state string, text string) bool {
	normalizedEvent := strings.ToLower(strings.TrimSpace(event))
	normalizedState := strings.ToLower(strings.TrimSpace(state))
	normalizedText := strings.ToLower(strings.TrimSpace(text))
	if strings.Contains(normalizedEvent, "heartbeat") ||
		strings.Contains(normalizedState, "heartbeat") ||
		strings.Contains(normalizedText, "heartbeat") {
		return false
	}
	switch normalizedState {
	case "running", "tool_running":
		return normalizedText != "" &&
			normalizedText != "running" &&
			normalizedText != "tool_running"
	default:
		return true
	}
}

func isToolProgressToken(value string) bool {
	normalized := strings.ToLower(strings.TrimSpace(value))
	if normalized == "" {
		return false
	}
	if strings.Contains(normalized, "heartbeat") {
		return false
	}
	tokens := strings.FieldsFunc(normalized, func(r rune) bool {
		return r == '.' || r == '_' || r == '-' || unicode.IsSpace(r)
	})
	hasTool := false
	for _, token := range tokens {
		switch token {
		case "tool", "tools":
			hasTool = true
		}
	}
	if !hasTool {
		return false
	}
	for _, token := range tokens {
		switch token {
		case "progress", "running", "exec", "execute", "execution", "output", "read", "write", "call", "skill", "result":
			return true
		}
	}
	return normalized == "tool" || normalized == "tools"
}

func mergeRuntimeDeltaText(current string, next string) string {
	if strings.TrimSpace(next) == "" {
		return current
	}
	if strings.TrimSpace(current) == "" {
		return next
	}
	currentTrimmed := strings.TrimSpace(current)
	nextTrimmed := strings.TrimSpace(next)
	if strings.HasPrefix(next, current) || strings.HasPrefix(nextTrimmed, currentTrimmed) {
		return next
	}
	if strings.HasSuffix(current, next) || strings.HasSuffix(currentTrimmed, nextTrimmed) {
		return current
	}
	return current + next
}

func selectRuntimeReplyText(finalText string, deltaText string, historyText string) string {
	finalText = strings.TrimSpace(finalText)
	deltaText = strings.TrimSpace(deltaText)
	historyText = strings.TrimSpace(historyText)

	if historyText != "" {
		if finalText == "" && deltaText == "" {
			return historyText
		}
		if finalText != "" && len([]rune(historyText)) > len([]rune(finalText)) && strings.Contains(historyText, finalText) {
			return historyText
		}
		if deltaText != "" && len([]rune(historyText)) >= len([]rune(deltaText)) && strings.Contains(historyText, deltaText) {
			return historyText
		}
	}
	if finalText == "" {
		if deltaText != "" {
			return deltaText
		}
		return historyText
	}
	if deltaText != "" && len([]rune(deltaText)) > len([]rune(finalText)) && strings.HasSuffix(deltaText, finalText) {
		return deltaText
	}
	return finalText
}

func runtimeWebSocketURL(raw string) string {
	parsed, err := url.Parse(raw)
	if err != nil {
		return raw
	}
	switch parsed.Scheme {
	case "https":
		parsed.Scheme = "wss"
	default:
		parsed.Scheme = "ws"
	}
	if parsed.Path == "" {
		parsed.Path = "/"
	}
	return parsed.String()
}

func writeRequest(conn *websocket.Conn, id string, method string, params map[string]any) error {
	return conn.WriteJSON(map[string]any{
		"type":   "req",
		"id":     id,
		"method": method,
		"params": params,
	})
}

func waitResponse(ctx context.Context, conn *websocket.Conn, id string) (wsFrame, error) {
	for {
		frame, err := readFrame(ctx, conn)
		if err != nil {
			return wsFrame{}, err
		}
		if frame.Type == "res" && frame.ID == id {
			return frame, nil
		}
	}
}

func waitResponseWithProgress(
	ctx context.Context,
	conn *websocket.Conn,
	id string,
	staleTimeout time.Duration,
	lastProgressAt time.Time,
) (wsFrame, error) {
	for {
		frame, err := readFrameWithProgressDeadline(ctx, conn, staleTimeout, lastProgressAt)
		if err != nil {
			if isReadTimeout(err) && staleTimeout > 0 && time.Since(lastProgressAt) >= staleTimeout {
				return wsFrame{}, core.RuntimeStaleTimeoutError{
					Timeout:        staleTimeout,
					LastProgressAt: lastProgressAt,
					LastState:      "chat_sent",
				}
			}
			return wsFrame{}, err
		}
		if frame.Type == "res" && frame.ID == id {
			return frame, nil
		}
	}
}

func readFrame(ctx context.Context, conn *websocket.Conn) (wsFrame, error) {
	if deadline, ok := ctx.Deadline(); ok {
		_ = conn.SetReadDeadline(deadline)
	}
	var frame wsFrame
	if err := conn.ReadJSON(&frame); err != nil {
		if ctx.Err() != nil {
			return wsFrame{}, ctx.Err()
		}
		return wsFrame{}, err
	}
	return frame, nil
}

func readFrameWithProgressDeadline(
	ctx context.Context,
	conn *websocket.Conn,
	staleTimeout time.Duration,
	lastProgressAt time.Time,
) (wsFrame, error) {
	deadline, hasDeadline := ctx.Deadline()
	if staleTimeout > 0 && !lastProgressAt.IsZero() {
		staleDeadline := lastProgressAt.Add(staleTimeout)
		if !hasDeadline || staleDeadline.Before(deadline) {
			deadline = staleDeadline
			hasDeadline = true
		}
	}
	if hasDeadline {
		_ = conn.SetReadDeadline(deadline)
	}
	var frame wsFrame
	if err := conn.ReadJSON(&frame); err != nil {
		if ctx.Err() != nil {
			return wsFrame{}, ctx.Err()
		}
		return wsFrame{}, err
	}
	return frame, nil
}

func isReadTimeout(err error) bool {
	if err == nil {
		return false
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return true
	}
	return strings.Contains(strings.ToLower(err.Error()), "timeout")
}

func contextDeadlineReached(ctx context.Context) bool {
	if ctx == nil {
		return false
	}
	if ctx.Err() != nil {
		return true
	}
	deadline, ok := ctx.Deadline()
	return ok && !time.Now().Before(deadline)
}

func notifyRuntimeProgress(ctx context.Context, request core.RuntimeExecutionRequest, event core.RuntimeProgressEvent) {
	if request.OnProgress == nil {
		return
	}
	_ = request.OnProgress(ctx, event)
}

func parseChatPayload(raw json.RawMessage) (chatPayload, error) {
	if len(raw) == 0 {
		return chatPayload{}, errors.New("empty chat payload")
	}
	var payload chatPayload
	if err := json.Unmarshal(raw, &payload); err != nil {
		return chatPayload{}, err
	}
	return payload, nil
}

func extractMessageText(raw json.RawMessage) string {
	return strings.TrimSpace(extractMessageTextRaw(raw))
}

func extractMessageTextRaw(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var value any
	if err := json.Unmarshal(raw, &value); err != nil {
		return ""
	}
	parts := make([]string, 0)
	collectText(value, &parts)
	return strings.Join(parts, "\n")
}

func latestAssistantTextFromHistory(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var payload struct {
		Messages []json.RawMessage `json:"messages"`
	}
	if err := json.Unmarshal(raw, &payload); err != nil {
		return ""
	}
	for i := len(payload.Messages) - 1; i >= 0; i-- {
		message := payload.Messages[i]
		var envelope struct {
			Role string `json:"role"`
		}
		if err := json.Unmarshal(message, &envelope); err != nil {
			continue
		}
		if strings.ToLower(strings.TrimSpace(envelope.Role)) != "assistant" {
			continue
		}
		if text := extractMessageText(message); text != "" {
			return text
		}
	}
	return ""
}

func collectText(value any, parts *[]string) {
	switch typed := value.(type) {
	case string:
		if strings.TrimSpace(typed) != "" {
			*parts = append(*parts, typed)
		}
	case map[string]any:
		if text, ok := typed["text"].(string); ok && strings.TrimSpace(text) != "" {
			*parts = append(*parts, text)
			return
		}
		if content, ok := typed["content"]; ok {
			collectText(content, parts)
			return
		}
		if message, ok := typed["message"]; ok {
			collectText(message, parts)
		}
	case []any:
		for _, item := range typed {
			collectText(item, parts)
		}
	}
}
