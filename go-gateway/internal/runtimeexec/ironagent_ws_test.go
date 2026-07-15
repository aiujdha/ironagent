package runtimeexec

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gorilla/websocket"

	"ironagent/go-gateway/internal/core"
)

func TestTaskLogID(t *testing.T) {
	t.Parallel()
	if got := taskLogID(core.RuntimeExecutionRequest{GatewayTaskID: "gw-1", RequestID: "sb-1"}); got != "gw-1" {
		t.Errorf("with gateway id: got %q, want gw-1", got)
	}
	if got := taskLogID(core.RuntimeExecutionRequest{RequestID: "sb-1"}); got != "sb-1" {
		t.Errorf("fallback to southbound id: got %q, want sb-1", got)
	}
}

func TestExtractMessageTextReadsContentBlocks(t *testing.T) {
	t.Parallel()

	raw := json.RawMessage(`{
		"role":"assistant",
		"content":[
			{"type":"text","text":"第一段"},
			{"type":"text","text":"第二段"}
		]
	}`)

	if got := extractMessageText(raw); got != "第一段\n第二段" {
		t.Fatalf("extractMessageText() = %q", got)
	}
}

func TestLatestAssistantTextFromHistoryUsesLastAssistantMessage(t *testing.T) {
	t.Parallel()

	raw := json.RawMessage(`{
		"messages":[
			{"role":"assistant","content":[{"type":"text","text":"旧回复"}]},
			{"role":"user","content":[{"type":"text","text":"用户消息"}]},
			{"role":"assistant","content":[{"type":"text","text":"最新回复"}]}
		]
	}`)

	if got := latestAssistantTextFromHistory(raw); got != "最新回复" {
		t.Fatalf("latestAssistantTextFromHistory() = %q", got)
	}
}

func TestChatPayloadAcceptsSnakeRunID(t *testing.T) {
	t.Parallel()

	payload, err := parseChatPayload(json.RawMessage(`{"run_id":"run-1","state":"heartbeat"}`))
	if err != nil {
		t.Fatalf("parseChatPayload() error = %v", err)
	}

	if got := payload.runID(); got != "run-1" {
		t.Fatalf("runID() = %q", got)
	}
}

func TestRuntimeProgressEventClassification(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name  string
		event string
		state string
		want  bool
	}{
		{name: "chat progress state", event: "chat", state: "progress", want: true},
		{name: "chat thinking state", event: "chat", state: "thinking", want: true},
		{name: "assistant plan event", event: "assistant.plan", want: true},
		{name: "chat heartbeat event", event: "chat.heartbeat", want: true},
		{name: "tool progress event", event: "tool.progress", want: true},
		{name: "tool output read event", event: "tool output read", want: true},
		{name: "tool exec state", event: "tool-events", state: "tool_exec", want: true},
		{name: "chat tool write state", event: "chat", state: "tool_write", want: true},
		{name: "chat final", event: "chat", state: "final", want: false},
		{name: "unrelated event", event: "connect.challenge", want: false},
	}

	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if got := isRuntimeProgressEvent(tc.event, tc.state); got != tc.want {
				t.Fatalf("isRuntimeProgressEvent(%q, %q) = %v", tc.event, tc.state, got)
			}
		})
	}
}

func TestRuntimeProgressStateFallsBackFromEvent(t *testing.T) {
	t.Parallel()

	if got := runtimeProgressState("tool.progress", ""); got != "progress" {
		t.Fatalf("runtimeProgressState() = %q", got)
	}
	if got := runtimeProgressState("chat.heartbeat", ""); got != "heartbeat" {
		t.Fatalf("runtimeProgressState() = %q", got)
	}
}

func TestRuntimeProgressTextPrefersToolOutputBeforeToolName(t *testing.T) {
	t.Parallel()

	payload, err := parseChatPayload(json.RawMessage(`{
		"run_id": "run-1",
		"state": "tool_output_read",
		"event": "tool-events",
		"toolName": "read",
		"message": {"content": "这是读取到的文件正文"}
	}`))
	if err != nil {
		t.Fatalf("parseChatPayload() error = %v", err)
	}

	if got := payload.safeProgressText(); got != "这是读取到的文件正文" {
		t.Fatalf("safeProgressText() = %q", got)
	}
}

func TestMergeRuntimeDeltaTextHandlesIncrementalAndCumulativeDelta(t *testing.T) {
	t.Parallel()

	if got := mergeRuntimeDeltaText("第一段", "第二段"); got != "第一段第二段" {
		t.Fatalf("incremental merge = %q", got)
	}
	if got := mergeRuntimeDeltaText("第一段", "第一段第二段"); got != "第一段第二段" {
		t.Fatalf("cumulative merge = %q", got)
	}
	if got := mergeRuntimeDeltaText("第一段第二段", "第二段"); got != "第一段第二段" {
		t.Fatalf("duplicate suffix merge = %q", got)
	}
	if got := mergeRuntimeDeltaText("Hello", " world"); got != "Hello world" {
		t.Fatalf("whitespace-preserving merge = %q", got)
	}
}

func TestSelectRuntimeReplyTextUsesAccumulatedDeltaWhenFinalIsLastChunk(t *testing.T) {
	t.Parallel()

	if got := selectRuntimeReplyText("第二段", "第一段第二段", ""); got != "第一段第二段" {
		t.Fatalf("selectRuntimeReplyText() = %q", got)
	}
	if got := selectRuntimeReplyText("", "第一段第二段", ""); got != "第一段第二段" {
		t.Fatalf("selectRuntimeReplyText() with empty final = %q", got)
	}
	if got := selectRuntimeReplyText("完整回复", "片段", ""); got != "完整回复" {
		t.Fatalf("selectRuntimeReplyText() should keep final text, got %q", got)
	}
}

func TestExecuteChatReturnsAccumulatedDeltaWhenFinalMessageEmpty(t *testing.T) {
	t.Parallel()

	server, errCh := newChatRuntimeServer(t, []wsFrame{
		{
			Type:    "event",
			Event:   "chat",
			Payload: json.RawMessage(`{"runId":"run-delta-empty-final","state":"delta","message":{"content":[{"type":"text","text":"第一段"}]}}`),
		},
		{
			Type:    "event",
			Event:   "chat",
			Payload: json.RawMessage(`{"runId":"run-delta-empty-final","state":"delta","message":{"content":[{"type":"text","text":"第二段"}]}}`),
		},
		{
			Type:    "event",
			Event:   "chat",
			Payload: json.RawMessage(`{"runId":"run-delta-empty-final","state":"final"}`),
		},
	})
	defer server.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	result, err := NewIronAgentWebSocketExecutor().ExecuteChat(ctx, core.RuntimeExecutionRequest{
		AssistantID:  "assistant-1",
		GatewayURL:   server.URL,
		GatewayToken: "token",
		SessionKey:   "agent:assistant-1:main",
		Message:      "hello",
		RequestID:    "run-delta-empty-final",
		TimeoutMs:    2000,
	})
	if err != nil {
		t.Fatalf("ExecuteChat() error = %v", err)
	}
	if result.ReplyText != "第一段第二段" {
		t.Fatalf("ReplyText = %q", result.ReplyText)
	}
	if err := <-errCh; err != nil {
		t.Fatalf("server error = %v", err)
	}
}

func TestExecuteChatReturnsAccumulatedDeltaWhenFinalIsLastChunk(t *testing.T) {
	t.Parallel()

	server, errCh := newChatRuntimeServer(t, []wsFrame{
		{
			Type:    "event",
			Event:   "chat",
			Payload: json.RawMessage(`{"runId":"run-final-last-chunk","state":"delta","message":{"content":[{"type":"text","text":"第一段"}]}}`),
		},
		{
			Type:    "event",
			Event:   "chat",
			Payload: json.RawMessage(`{"runId":"run-final-last-chunk","state":"delta","message":{"content":[{"type":"text","text":"第二段"}]}}`),
		},
		{
			Type:    "event",
			Event:   "chat",
			Payload: json.RawMessage(`{"runId":"run-final-last-chunk","state":"final","message":{"content":[{"type":"text","text":"第二段"}]}}`),
		},
	})
	defer server.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	result, err := NewIronAgentWebSocketExecutor().ExecuteChat(ctx, core.RuntimeExecutionRequest{
		AssistantID:  "assistant-1",
		GatewayURL:   server.URL,
		GatewayToken: "token",
		SessionKey:   "agent:assistant-1:main",
		Message:      "hello",
		RequestID:    "run-final-last-chunk",
		TimeoutMs:    2000,
	})
	if err != nil {
		t.Fatalf("ExecuteChat() error = %v", err)
	}
	if result.ReplyText != "第一段第二段" {
		t.Fatalf("ReplyText = %q", result.ReplyText)
	}
	if err := <-errCh; err != nil {
		t.Fatalf("server error = %v", err)
	}
}

func TestExecuteChatDoesNotResetDeadlineOnHeartbeat(t *testing.T) {
	t.Parallel()

	errCh := make(chan error, 1)
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			errCh <- err
			return
		}
		defer conn.Close()

		if err := conn.WriteJSON(wsFrame{
			Type:  "event",
			Event: "connect.challenge",
		}); err != nil {
			errCh <- err
			return
		}
		var connectReq testWSRequest
		if err := conn.ReadJSON(&connectReq); err != nil {
			errCh <- err
			return
		}
		if err := conn.WriteJSON(wsFrame{Type: "res", ID: connectReq.ID, OK: true}); err != nil {
			errCh <- err
			return
		}
		var chatReq testWSRequest
		if err := conn.ReadJSON(&chatReq); err != nil {
			errCh <- err
			return
		}
		if _, ok := chatReq.Params["staleTimeoutMs"]; ok {
			errCh <- fmt.Errorf("unexpected staleTimeoutMs param")
			return
		}
		if err := conn.WriteJSON(wsFrame{Type: "res", ID: chatReq.ID, OK: true}); err != nil {
			errCh <- err
			return
		}

		time.Sleep(70 * time.Millisecond)
		if err := conn.WriteJSON(wsFrame{
			Type:    "event",
			Event:   "chat.heartbeat",
			Payload: json.RawMessage(`{"run_id":"run-1","state":"heartbeat"}`),
		}); err != nil {
			errCh <- err
			return
		}
		time.Sleep(70 * time.Millisecond)
		if err := conn.WriteJSON(wsFrame{
			Type:  "event",
			Event: "chat",
			Payload: json.RawMessage(`{
				"runId":"run-1",
				"state":"final",
				"message":{"content":[{"type":"text","text":"ok"}]}
			}`),
		}); err != nil {
			errCh <- err
			return
		}
		errCh <- nil
	}))
	defer server.Close()

	var states []string
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	result, err := NewIronAgentWebSocketExecutor().ExecuteChat(ctx, core.RuntimeExecutionRequest{
		AssistantID:    "assistant-1",
		GatewayURL:     server.URL,
		GatewayToken:   "token",
		SessionKey:     "agent:assistant-1:main",
		Message:        "hello",
		RequestID:      "run-1",
		TimeoutMs:      2000,
		StaleTimeoutMs: 100,
		OnProgress: func(_ context.Context, event core.RuntimeProgressEvent) error {
			states = append(states, event.State)
			return nil
		},
	})
	var stale core.RuntimeStaleTimeoutError
	if !errors.As(err, &stale) {
		t.Fatalf("ExecuteChat() error = %v, want RuntimeStaleTimeoutError", err)
	}
	if result.ReplyText != "" {
		t.Fatalf("ReplyText = %q, want empty on stale timeout", result.ReplyText)
	}
	if !containsString(states, "heartbeat") {
		t.Fatalf("progress states = %v, want heartbeat", states)
	}
	select {
	case <-errCh:
	case <-time.After(time.Second):
		t.Fatalf("timed out waiting for server")
	}
}

func TestExecuteChatCancelsRuntimeOnOverallDeadline(t *testing.T) {
	t.Parallel()

	cancelReqCh := make(chan testWSRequest, 1)
	errCh := make(chan error, 1)
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			errCh <- err
			return
		}
		defer conn.Close()

		if err := conn.WriteJSON(wsFrame{
			Type:  "event",
			Event: "connect.challenge",
		}); err != nil {
			errCh <- err
			return
		}
		var connectReq testWSRequest
		if err := conn.ReadJSON(&connectReq); err != nil {
			errCh <- err
			return
		}
		if err := conn.WriteJSON(wsFrame{Type: "res", ID: connectReq.ID, OK: true}); err != nil {
			errCh <- err
			return
		}
		var chatReq testWSRequest
		if err := conn.ReadJSON(&chatReq); err != nil {
			errCh <- err
			return
		}
		if err := conn.WriteJSON(wsFrame{Type: "res", ID: chatReq.ID, OK: true}); err != nil {
			errCh <- err
			return
		}
		_ = conn.SetReadDeadline(time.Now().Add(time.Second))
		var cancelReq testWSRequest
		if err := conn.ReadJSON(&cancelReq); err != nil {
			errCh <- err
			return
		}
		cancelReqCh <- cancelReq
		errCh <- nil
	}))
	defer server.Close()

	result, err := NewIronAgentWebSocketExecutor().ExecuteChat(context.Background(), core.RuntimeExecutionRequest{
		AssistantID:    "assistant-1",
		GatewayURL:     server.URL,
		GatewayToken:   "token",
		SessionKey:     "agent:assistant-1:main",
		Message:        "hello",
		RequestID:      "run-deadline",
		TimeoutMs:      50,
		DeadlineSource: core.DeadlineSourceClientSupplied,
		StaleTimeoutMs: 0,
	})
	var attr *core.AttributedRuntimeError
	if !errors.As(err, &attr) {
		t.Fatalf("ExecuteChat() error = %v, want *core.AttributedRuntimeError", err)
	}
	// client-supplied deadline → UPSTREAM (caller under-budgeted), non-retryable.
	if attr.Domain != core.FaultDomainUpstream || attr.Reason != core.ReasonUpstreamTimeoutBudget {
		t.Fatalf("deadline attribution = %s/%s, want UPSTREAM/%s", attr.Domain, attr.Reason, core.ReasonUpstreamTimeoutBudget)
	}
	if attr.Retryable {
		t.Fatalf("client-supplied deadline must not be retryable")
	}
	if result.ReplyText != "" {
		t.Fatalf("ReplyText = %q, want empty on deadline", result.ReplyText)
	}
	var cancelReq testWSRequest
	select {
	case cancelReq = <-cancelReqCh:
	case <-time.After(time.Second):
		t.Fatalf("timed out waiting for chat.abort")
	}
	if cancelReq.Method != "chat.abort" {
		t.Fatalf("cancel method = %q, want chat.abort", cancelReq.Method)
	}
	if cancelReq.Params["runId"] != "run-deadline" || cancelReq.Params["sessionKey"] != "agent:assistant-1:main" {
		t.Fatalf("cancel params = %#v", cancelReq.Params)
	}
	if _, hasReason := cancelReq.Params["reason"]; hasReason {
		t.Fatalf("chat.abort params must not include reason (runtime ChatAbortParamsSchema is additionalProperties:false): %#v", cancelReq.Params)
	}
	if err := <-errCh; err != nil {
		t.Fatalf("server error = %v", err)
	}
}

func TestExecuteChatResetsDeadlineOnBusinessProgressEvent(t *testing.T) {
	t.Parallel()

	errCh := make(chan error, 1)
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			errCh <- err
			return
		}
		defer conn.Close()

		if err := conn.WriteJSON(wsFrame{
			Type:  "event",
			Event: "connect.challenge",
		}); err != nil {
			errCh <- err
			return
		}
		var connectReq testWSRequest
		if err := conn.ReadJSON(&connectReq); err != nil {
			errCh <- err
			return
		}
		if err := conn.WriteJSON(wsFrame{Type: "res", ID: connectReq.ID, OK: true}); err != nil {
			errCh <- err
			return
		}
		var chatReq testWSRequest
		if err := conn.ReadJSON(&chatReq); err != nil {
			errCh <- err
			return
		}
		if err := conn.WriteJSON(wsFrame{Type: "res", ID: chatReq.ID, OK: true}); err != nil {
			errCh <- err
			return
		}

		time.Sleep(70 * time.Millisecond)
		if err := conn.WriteJSON(wsFrame{
			Type:    "event",
			Event:   "chat.progress",
			Payload: json.RawMessage(`{"run_id":"run-1","state":"progress","detail":"created result file"}`),
		}); err != nil {
			errCh <- err
			return
		}
		time.Sleep(70 * time.Millisecond)
		if err := conn.WriteJSON(wsFrame{
			Type:  "event",
			Event: "chat",
			Payload: json.RawMessage(`{
				"runId":"run-1",
				"state":"final",
				"message":{"content":[{"type":"text","text":"ok"}]}
			}`),
		}); err != nil {
			errCh <- err
			return
		}
		errCh <- nil
	}))
	defer server.Close()

	var states []string
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	result, err := NewIronAgentWebSocketExecutor().ExecuteChat(ctx, core.RuntimeExecutionRequest{
		AssistantID:    "assistant-1",
		GatewayURL:     server.URL,
		GatewayToken:   "token",
		SessionKey:     "agent:assistant-1:main",
		Message:        "hello",
		RequestID:      "run-1",
		TimeoutMs:      2000,
		StaleTimeoutMs: 100,
		OnProgress: func(_ context.Context, event core.RuntimeProgressEvent) error {
			states = append(states, event.State)
			return nil
		},
	})
	if err != nil {
		t.Fatalf("ExecuteChat() error = %v", err)
	}
	if result.ReplyText != "ok" {
		t.Fatalf("ReplyText = %q", result.ReplyText)
	}
	if !containsString(states, "progress") {
		t.Fatalf("progress states = %v, want progress", states)
	}
	if err := <-errCh; err != nil {
		t.Fatalf("server error = %v", err)
	}
}

// TestExecuteChatGatewayDefaultStaleDisabledEnforcesDeadline is the negative test
// for the A1 widening guard: DeadlineSource=GatewayDefault with StaleTimeoutMs=0
// must NOT widen to the 1h backstop (stale detection is disabled, so the backstop
// would remove all liveness governance). The original short TimeoutMs must still
// fire. If the "&& StaleTimeoutMs>0" guard regresses, execCtx widens to 1h with no
// stale fallback and ExecuteChat blocks ~1h — caught by the 2s watchdog.
func TestExecuteChatGatewayDefaultStaleDisabledEnforcesDeadline(t *testing.T) {
	t.Parallel()

	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		_ = conn.WriteJSON(wsFrame{Type: "event", Event: "connect.challenge"})
		var connectReq testWSRequest
		if conn.ReadJSON(&connectReq) != nil {
			return
		}
		_ = conn.WriteJSON(wsFrame{Type: "res", ID: connectReq.ID, OK: true})
		var chatReq testWSRequest
		if conn.ReadJSON(&chatReq) != nil {
			return
		}
		_ = conn.WriteJSON(wsFrame{Type: "res", ID: chatReq.ID, OK: true})
		// Go silent: no progress, no final. The original 50ms deadline must fire.
		_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
		var drain testWSRequest
		_ = conn.ReadJSON(&drain)
	}))
	defer server.Close()

	done := make(chan error, 1)
	go func() {
		_, err := NewIronAgentWebSocketExecutor().ExecuteChat(context.Background(), core.RuntimeExecutionRequest{
			AssistantID:    "assistant-1",
			GatewayURL:     server.URL,
			GatewayToken:   "token",
			SessionKey:     "agent:assistant-1:main",
			Message:        "hello",
			RequestID:      "run-gwdefault-stale0",
			TimeoutMs:      50,
			DeadlineSource: core.DeadlineSourceGatewayDefault,
			StaleTimeoutMs: 0,
		})
		done <- err
	}()
	select {
	case err := <-done:
		var attr *core.AttributedRuntimeError
		if !errors.As(err, &attr) {
			t.Fatalf("ExecuteChat() error = %v, want *core.AttributedRuntimeError", err)
		}
		if attr.Reason != core.ReasonGatewayDeadlineSelf {
			t.Fatalf("deadline reason = %s, want %s (gateway-default deadline must not widen when stale disabled)", attr.Reason, core.ReasonGatewayDeadlineSelf)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("ExecuteChat did not return within 2s — the A1 StaleTimeoutMs>0 guard likely regressed (execCtx widened to 1h with stale disabled)")
	}
}

func TestNewExecutorDefaultDeadlineBackstop(t *testing.T) {
	t.Parallel()
	if got := NewIronAgentWebSocketExecutor().DeadlineBackstop; got != 20*time.Minute {
		t.Fatalf("default DeadlineBackstop = %v, want 20m (conservative default; must not regress to a hard-coded 1h)", got)
	}
}

func TestExecuteChatGatewayDefaultUsesConfiguredBackstop(t *testing.T) {
	t.Parallel()
	// A1 must widen a gateway-default deadline to the executor's CONFIGURED
	// DeadlineBackstop, not a hard-coded constant. With a 300ms backstop and a
	// 50ms original timeout, the deadline must fire at ~300ms; if A1 ignored the
	// config and used the old 1h (or the 20m default), the 1s watchdog trips.
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		_ = conn.WriteJSON(wsFrame{Type: "event", Event: "connect.challenge"})
		var connectReq testWSRequest
		if conn.ReadJSON(&connectReq) != nil {
			return
		}
		_ = conn.WriteJSON(wsFrame{Type: "res", ID: connectReq.ID, OK: true})
		var chatReq testWSRequest
		if conn.ReadJSON(&chatReq) != nil {
			return
		}
		_ = conn.WriteJSON(wsFrame{Type: "res", ID: chatReq.ID, OK: true})
		// Go silent: the widened 300ms backstop deadline must fire.
		_ = conn.SetReadDeadline(time.Now().Add(3 * time.Second))
		var drain testWSRequest
		_ = conn.ReadJSON(&drain)
	}))
	defer server.Close()

	exec := NewIronAgentWebSocketExecutor()
	exec.DeadlineBackstop = 300 * time.Millisecond
	done := make(chan error, 1)
	go func() {
		_, err := exec.ExecuteChat(context.Background(), core.RuntimeExecutionRequest{
			AssistantID:    "assistant-1",
			GatewayURL:     server.URL,
			GatewayToken:   "token",
			SessionKey:     "agent:assistant-1:main",
			Message:        "hello",
			RequestID:      "run-gwdefault-backstop",
			TimeoutMs:      50,
			DeadlineSource: core.DeadlineSourceGatewayDefault,
			StaleTimeoutMs: 5000,
		})
		done <- err
	}()
	select {
	case err := <-done:
		var attr *core.AttributedRuntimeError
		if !errors.As(err, &attr) {
			t.Fatalf("ExecuteChat() error = %v, want *core.AttributedRuntimeError", err)
		}
		// A gateway-default backstop firing while still inside the stale window is
		// attributed INDETERMINATE (can't tell if the runtime was slow or the
		// backstop too short). The mutation-proof signal here is the ~300ms timing
		// (caught by the watchdog), proving the CONFIGURED backstop was used.
		if attr.Reason != core.ReasonUndetermined {
			t.Fatalf("deadline reason = %s, want %s", attr.Reason, core.ReasonUndetermined)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("deadline did not fire within 1s — A1 ignored the configured 300ms DeadlineBackstop (used a hard-coded constant)")
	}
}

func TestExecuteChatGatewayDefaultDeadlineDoesNotKillProgressingRuntime(t *testing.T) {
	t.Parallel()

	errCh := make(chan error, 1)
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			errCh <- err
			return
		}
		defer conn.Close()

		if err := conn.WriteJSON(wsFrame{Type: "event", Event: "connect.challenge"}); err != nil {
			errCh <- err
			return
		}
		var connectReq testWSRequest
		if err := conn.ReadJSON(&connectReq); err != nil {
			errCh <- err
			return
		}
		if err := conn.WriteJSON(wsFrame{Type: "res", ID: connectReq.ID, OK: true}); err != nil {
			errCh <- err
			return
		}
		var chatReq testWSRequest
		if err := conn.ReadJSON(&chatReq); err != nil {
			errCh <- err
			return
		}
		if err := conn.WriteJSON(wsFrame{Type: "res", ID: chatReq.ID, OK: true}); err != nil {
			errCh <- err
			return
		}
		// Keep working well past the tiny TimeoutMs: a gateway-default deadline
		// must not hard-kill a runtime that is still making progress (A1).
		time.Sleep(70 * time.Millisecond)
		if err := conn.WriteJSON(wsFrame{
			Type:    "event",
			Event:   "chat.progress",
			Payload: json.RawMessage(`{"run_id":"run-a1","state":"progress","detail":"still working"}`),
		}); err != nil {
			errCh <- err
			return
		}
		time.Sleep(70 * time.Millisecond)
		if err := conn.WriteJSON(wsFrame{
			Type:    "event",
			Event:   "chat",
			Payload: json.RawMessage(`{"runId":"run-a1","state":"final","message":{"content":[{"type":"text","text":"ok"}]}}`),
		}); err != nil {
			errCh <- err
			return
		}
		errCh <- nil
	}))
	defer server.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	result, err := NewIronAgentWebSocketExecutor().ExecuteChat(ctx, core.RuntimeExecutionRequest{
		AssistantID:    "assistant-1",
		GatewayURL:     server.URL,
		GatewayToken:   "token",
		SessionKey:     "agent:assistant-1:main",
		Message:        "hello",
		RequestID:      "run-a1",
		TimeoutMs:      50, // would hard-kill at 50ms if treated as a client cap
		DeadlineSource: core.DeadlineSourceGatewayDefault,
		StaleTimeoutMs: 5000, // large: stale-progress does not fire during the run
	})
	if err != nil {
		t.Fatalf("ExecuteChat() error = %v, want success (gateway-default deadline must not kill a progressing runtime)", err)
	}
	if result.ReplyText != "ok" {
		t.Fatalf("ReplyText = %q, want \"ok\"", result.ReplyText)
	}
	if err := <-errCh; err != nil {
		t.Fatalf("server error = %v", err)
	}
}

func TestExecuteChatReturnsInFlightErrorWhenRuntimeReportsInFlight(t *testing.T) {
	t.Parallel()

	errCh := make(chan error, 1)
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			errCh <- err
			return
		}
		defer conn.Close()

		if err := conn.WriteJSON(wsFrame{Type: "event", Event: "connect.challenge"}); err != nil {
			errCh <- err
			return
		}
		var connectReq testWSRequest
		if err := conn.ReadJSON(&connectReq); err != nil {
			errCh <- err
			return
		}
		if err := conn.WriteJSON(wsFrame{Type: "res", ID: connectReq.ID, OK: true}); err != nil {
			errCh <- err
			return
		}
		var chatReq testWSRequest
		if err := conn.ReadJSON(&chatReq); err != nil {
			errCh <- err
			return
		}
		// Ack chat.send with an in_flight status (ok=true) and then send NO
		// events, mimicking a run already executing elsewhere.
		if err := conn.WriteJSON(wsFrame{
			Type:    "res",
			ID:      chatReq.ID,
			OK:      true,
			Payload: json.RawMessage(`{"runId":"run-inflight","status":"in_flight"}`),
		}); err != nil {
			errCh <- err
			return
		}
		errCh <- nil
	}))
	defer server.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_, err := NewIronAgentWebSocketExecutor().ExecuteChat(ctx, core.RuntimeExecutionRequest{
		AssistantID:    "assistant-1",
		GatewayURL:     server.URL,
		GatewayToken:   "token",
		SessionKey:     "agent:assistant-1:main",
		Message:        "hello",
		RequestID:      "run-inflight",
		TimeoutMs:      2000, // large: returning fast on in_flight, not via timeout
		StaleTimeoutMs: 2000,
	})
	var attr *core.AttributedRuntimeError
	if !errors.As(err, &attr) {
		t.Fatalf("ExecuteChat() error = %v, want *core.AttributedRuntimeError", err)
	}
	if attr.Reason != core.ReasonGatewayInFlightUnreachable {
		t.Fatalf("in_flight attribution reason = %q, want %q", attr.Reason, core.ReasonGatewayInFlightUnreachable)
	}
	if !attr.Retryable {
		t.Fatalf("in_flight must be retryable")
	}
	if err := <-errCh; err != nil {
		t.Fatalf("server error = %v", err)
	}
}

func TestExecuteChatConnectRejectedIsRetryableRuntimeFault(t *testing.T) {
	t.Parallel()

	errCh := make(chan error, 1)
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			errCh <- err
			return
		}
		defer conn.Close()

		if err := conn.WriteJSON(wsFrame{Type: "event", Event: "connect.challenge"}); err != nil {
			errCh <- err
			return
		}
		var connectReq testWSRequest
		if err := conn.ReadJSON(&connectReq); err != nil {
			errCh <- err
			return
		}
		// Reject the connect with a runtime-declared retryable error code.
		retryable := true
		if err := conn.WriteJSON(wsFrame{
			Type:  "res",
			ID:    connectReq.ID,
			OK:    false,
			Error: &wsError{Code: "E_AT_CAPACITY", Message: "at capacity", Retryable: &retryable},
		}); err != nil {
			errCh <- err
			return
		}
		errCh <- nil
	}))
	defer server.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_, err := NewIronAgentWebSocketExecutor().ExecuteChat(ctx, core.RuntimeExecutionRequest{
		AssistantID:    "assistant-1",
		GatewayURL:     server.URL,
		GatewayToken:   "token",
		SessionKey:     "agent:assistant-1:main",
		Message:        "hello",
		RequestID:      "run-reject",
		TimeoutMs:      2000,
		StaleTimeoutMs: 2000,
	})
	var attr *core.AttributedRuntimeError
	if !errors.As(err, &attr) {
		t.Fatalf("ExecuteChat() error = %v, want *core.AttributedRuntimeError", err)
	}
	if attr.Domain != core.FaultDomainRuntime || attr.Reason != core.ReasonRuntimeConnectRejected {
		t.Fatalf("attribution = %s/%s, want RUNTIME/%s", attr.Domain, attr.Reason, core.ReasonRuntimeConnectRejected)
	}
	if !attr.Retryable {
		t.Fatalf("connect reject with runtime retryable=true must adopt retryable=true")
	}
	if attr.RuntimeCode != "E_AT_CAPACITY" {
		t.Fatalf("RuntimeCode = %q, want E_AT_CAPACITY", attr.RuntimeCode)
	}
	if err := <-errCh; err != nil {
		t.Fatalf("server error = %v", err)
	}
}

func TestWSErrorRetryable(t *testing.T) {
	t.Parallel()
	tr, fa := true, false
	cases := []struct {
		name  string
		frame wsFrame
		want  bool
	}{
		{"explicit retryable=true adopted", wsFrame{Error: &wsError{Retryable: &tr}}, true},
		{"explicit retryable=false adopted", wsFrame{Error: &wsError{Retryable: &fa}}, false},
		{"absent hint fail-closed to false", wsFrame{Error: &wsError{Code: "E_X"}}, false},
		{"nil error fail-closed to false", wsFrame{}, false},
	}
	for _, tc := range cases {
		if got := tc.frame.errorRetryable(); got != tc.want {
			t.Errorf("%s: errorRetryable = %v, want %v", tc.name, got, tc.want)
		}
	}
}

type testWSRequest struct {
	Type   string         `json:"type"`
	ID     string         `json:"id"`
	Method string         `json:"method"`
	Params map[string]any `json:"params"`
}

func newChatRuntimeServer(t *testing.T, events []wsFrame) (*httptest.Server, <-chan error) {
	t.Helper()

	errCh := make(chan error, 1)
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			errCh <- err
			return
		}
		defer conn.Close()

		if err := conn.WriteJSON(wsFrame{
			Type:  "event",
			Event: "connect.challenge",
		}); err != nil {
			errCh <- err
			return
		}
		var connectReq testWSRequest
		if err := conn.ReadJSON(&connectReq); err != nil {
			errCh <- err
			return
		}
		if err := conn.WriteJSON(wsFrame{Type: "res", ID: connectReq.ID, OK: true}); err != nil {
			errCh <- err
			return
		}
		var chatReq testWSRequest
		if err := conn.ReadJSON(&chatReq); err != nil {
			errCh <- err
			return
		}
		if err := conn.WriteJSON(wsFrame{Type: "res", ID: chatReq.ID, OK: true}); err != nil {
			errCh <- err
			return
		}
		for _, event := range events {
			if err := conn.WriteJSON(event); err != nil {
				errCh <- err
				return
			}
		}
		errCh <- nil
	}))
	return server, errCh
}

func containsString(values []string, needle string) bool {
	for _, value := range values {
		if value == needle {
			return true
		}
	}
	return false
}
