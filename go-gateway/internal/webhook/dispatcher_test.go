package webhook

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"ironagent/go-gateway/internal/core"
)

func TestOutboxDispatchAsyncProcessesAndAcknowledges(t *testing.T) {
	t.Parallel()

	store := NewMemoryOutboxStore()
	payloads := make(chan CallbackPayload, 1)
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload CallbackPayload
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Errorf("decode callback payload: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		payloads <- payload
		w.WriteHeader(http.StatusNoContent)
	}))
	defer callback.Close()

	dispatcher := NewDispatcher(Config{
		Secret:         "callback-secret",
		MaxAttempts:    1,
		RetryDelay:     time.Millisecond,
		Store:          store,
		WorkerInterval: time.Hour,
		PendingLimit:   10,
	})
	record, err := dispatcher.DispatchAsync(context.Background(), callback.URL, Event{
		EventType: "task.progress",
		TaskID:    "task-outbox",
		Status:    "processing",
		Stage:     "tool_running",
	})
	if err != nil {
		t.Fatalf("DispatchAsync() error = %v", err)
	}
	if record.Event.EventSeq != 1 {
		t.Fatalf("EventSeq = %d, want 1", record.Event.EventSeq)
	}

	select {
	case payload := <-payloads:
		if payload.TaskID != record.Event.TaskID {
			t.Fatalf("delivered taskId = %q, want %q", payload.TaskID, record.Event.TaskID)
		}
		if payload.Status != "processing" || payload.Stage != "tool_running" {
			t.Fatalf("unexpected callback payload: %#v", payload)
		}
	case <-time.After(time.Second):
		t.Fatalf("timed out waiting for outbox delivery")
	}

	ack, ok := dispatcher.Acknowledge(ACK{
		Received: true,
		EventID:  record.Event.EventID,
		TaskID:   record.Event.TaskID,
		Handled:  true,
	})
	if !ok || ack.EventID != record.Event.EventID {
		t.Fatalf("expected stored ACK for %q, got %#v ok=%v", record.Event.EventID, ack, ok)
	}
	if _, ok := dispatcher.Acknowledge(ACK{
		Received: true,
		EventID:  record.Event.EventID,
		TaskID:   "wrong-task",
		Handled:  true,
	}); ok {
		t.Fatalf("expected ACK with wrong task_id to be rejected")
	}
}

func TestOutboxDispatchAsyncCanDisableBackgroundWorker(t *testing.T) {
	t.Parallel()

	store := NewMemoryOutboxStore()
	payloads := make(chan CallbackPayload, 1)
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload CallbackPayload
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Errorf("decode callback payload: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		payloads <- payload
		w.WriteHeader(http.StatusNoContent)
	}))
	defer callback.Close()

	dispatcher := NewDispatcher(Config{
		Secret:              "callback-secret",
		MaxAttempts:         1,
		Store:               store,
		WorkerInterval:      time.Millisecond,
		PendingLimit:        10,
		DisableOutboxWorker: true,
	})
	record, err := dispatcher.DispatchAsync(context.Background(), callback.URL, Event{
		EventType: "task.progress",
		TaskID:    "task-outbox-disabled",
		Status:    "processing",
		Stage:     "tool_running",
	})
	if err != nil {
		t.Fatalf("DispatchAsync() error = %v", err)
	}

	select {
	case payload := <-payloads:
		t.Fatalf("callback delivered by disabled background worker: %#v", payload)
	case <-time.After(50 * time.Millisecond):
	}

	processed, err := dispatcher.ProcessPending(context.Background())
	if err != nil {
		t.Fatalf("ProcessPending() error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("ProcessPending() = %d, want 1", processed)
	}
	select {
	case payload := <-payloads:
		if payload.TaskID != record.Event.TaskID {
			t.Fatalf("delivered taskId = %q, want %q", payload.TaskID, record.Event.TaskID)
		}
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for manual ProcessPending delivery")
	}
}

func TestOutboxDispatchAsyncWaitsWhenEnqueueBackpressureFull(t *testing.T) {
	t.Parallel()

	store := &blockingSeqOutboxStore{
		MemoryOutboxStore: NewMemoryOutboxStore(),
		entered:           make(chan struct{}, 1),
		release:           make(chan struct{}),
	}
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	defer callback.Close()

	dispatcher := NewDispatcher(Config{
		Secret:             "callback-secret",
		MaxAttempts:        1,
		Store:              store,
		StoreTimeout:       time.Second,
		EnqueueMaxInFlight: 1,
		WorkerInterval:     time.Hour,
		PendingLimit:       1,
	})

	firstDone := make(chan error, 1)
	go func() {
		_, err := dispatcher.DispatchAsync(context.Background(), callback.URL, Event{
			EventType: "task.accepted",
			TaskID:    "task-first",
			Status:    "processing",
			Stage:     "queued",
		})
		firstDone <- err
	}()

	select {
	case <-store.entered:
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for first enqueue to enter store")
	}

	secondDone := make(chan error, 1)
	go func() {
		_, err := dispatcher.DispatchAsync(context.Background(), callback.URL, Event{
			EventType: "task.accepted",
			TaskID:    "task-second",
			Status:    "processing",
			Stage:     "queued",
		})
		secondDone <- err
	}()

	select {
	case err := <-secondDone:
		t.Fatalf("second DispatchAsync() returned before slot release: %v", err)
	case <-time.After(25 * time.Millisecond):
	}

	close(store.release)
	select {
	case err := <-firstDone:
		if err != nil {
			t.Fatalf("first DispatchAsync() error = %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for first enqueue to finish")
	}
	select {
	case err := <-secondDone:
		if err != nil {
			t.Fatalf("second DispatchAsync() error = %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for second enqueue to finish")
	}
}

func TestOutboxDispatchAsyncStoreTimeout(t *testing.T) {
	t.Parallel()

	store := &blockingSeqOutboxStore{
		MemoryOutboxStore: NewMemoryOutboxStore(),
		entered:           make(chan struct{}, 1),
	}
	dispatcher := NewDispatcher(Config{
		Secret:             "callback-secret",
		Store:              store,
		StoreTimeout:       10 * time.Millisecond,
		EnqueueMaxInFlight: 1,
		WorkerInterval:     time.Hour,
	})

	started := time.Now()
	_, err := dispatcher.DispatchAsync(context.Background(), "https://callback.example.com", Event{
		EventType: "task.accepted",
		TaskID:    "task-timeout",
		Status:    "processing",
		Stage:     "queued",
	})
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("DispatchAsync() error = %v, want context deadline exceeded", err)
	}
	if elapsed := time.Since(started); elapsed > time.Second {
		t.Fatalf("DispatchAsync() took %s, want short store timeout", elapsed)
	}
}

func TestCallbackPayloadUsesPublicTaskPayloadShape(t *testing.T) {
	t.Parallel()

	event := Event{
		TaskID:        "task-public-shape",
		Status:        "failed",
		Stage:         "done",
		ReplyText:     "",
		ErrorCode:     "TOOL_EXECUTION_ERROR",
		ErrorMessage:  "工具执行失败",
		ErrorLocation: "runtime_ws.deadline",
		Reason:        "数据源连接超时",
		Files: []FileRef{{
			FileID:      "file-001",
			Filename:    "result.txt",
			Size:        12,
			MimeType:    "text/plain",
			DownloadURL: "/api/v1/files/file-001/download",
		}},
	}

	payload := NewCallbackPayload(event)
	expected := core.NewPublicTaskPayload(core.PublicTaskPayloadInput{
		TaskID:        event.TaskID,
		Status:        event.Status,
		Stage:         event.Stage,
		ReplyText:     event.ReplyText,
		ErrorCode:     event.ErrorCode,
		ErrorMessage:  event.ErrorMessage,
		ErrorLocation: event.ErrorLocation,
		ErrorReason:   event.Reason,
		Files: []core.PublicTaskFileResponse{{
			FileID:      "file-001",
			FileName:    "result.txt",
			Size:        "12",
			MimeType:    "text/plain",
			DownloadURL: "/api/v1/files/file-001/download",
		}},
	})
	if !reflect.DeepEqual(payload, expected) {
		t.Fatalf("callback payload must use public task payload shape\npayload:  %#v\nexpected: %#v", payload, expected)
	}

	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal payload: %v", err)
	}
	var body map[string]any
	if err := json.Unmarshal(raw, &body); err != nil {
		t.Fatalf("unmarshal payload: %v", err)
	}
	for _, forbidden := range []string{"task_id", "reply_text", "reply_format", "reply_mime_type", "error_code", "error_message", "error_location", "error_reason", "event_id", "event_type"} {
		if _, ok := body[forbidden]; ok {
			t.Fatalf("callback payload must not expose %q: %#v", forbidden, body)
		}
	}
	for _, required := range []string{"taskId", "status", "stage", "replyText", "errorCode", "errorMessage", "errorLocation", "errorReason"} {
		if _, ok := body[required]; !ok {
			t.Fatalf("callback payload missing %q: %#v", required, body)
		}
	}
}

func TestCallbackReplyTextUsesMarkdownContent(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name      string
		event     Event
		wantReply string
	}{
		{
			name: "completed wraps plain final text",
			event: Event{
				EventType: "task.completed",
				TaskID:    "task-md-completed",
				Status:    "completed",
				Stage:     "done",
				ReplyText: "runtime says ok",
			},
			wantReply: "### 执行结果\n\nruntime says ok",
		},
		{
			name: "progress markdown remains unchanged",
			event: Event{
				EventType: "task.progress",
				TaskID:    "task-md-progress",
				Status:    "processing",
				Stage:     "tool_running",
				ReplyText: "### 执行进度：第 1-2 步\n\n1. **runtime started**：runtime started",
			},
			wantReply: "### 执行进度：第 1-2 步\n\n1. **runtime started**：runtime started\n\n",
		},
		{
			name: "progress markdown keeps block boundary",
			event: Event{
				EventType: "task.progress",
				TaskID:    "task-md-progress-boundary",
				Status:    "processing",
				Stage:     "tool_running",
				ReplyText: "### 执行进度：第 5-5 步\n\n5. **任务仍在执行**：等待运行时返回后续进度或最终结果",
			},
			wantReply: "### 执行进度：第 5-5 步\n\n5. **任务仍在执行**：等待运行时返回后续进度或最终结果\n\n",
		},
		{
			name: "fine grained progress markdown keeps block boundary",
			event: Event{
				EventType: "tool.progress",
				TaskID:    "task-md-tool-progress-boundary",
				Status:    "processing",
				Stage:     "tool_running",
				ReplyText: "### 执行进度：第 1-1 步\n\n1. **file-reader**：开始读取文件",
			},
			wantReply: "### 执行进度：第 1-1 步\n\n1. **file-reader**：开始读取文件\n\n",
		},
		{
			name: "retry markdown keeps block boundary",
			event: Event{
				EventType: "task.retrying",
				TaskID:    "task-md-retrying",
				Status:    "processing",
				Stage:     "retrying",
				ReplyText: "任务本次 attempt 未完成，Gateway 已安排重试。\nattempt=1",
			},
			wantReply: "### 重试安排\n\n任务本次 attempt 未完成，Gateway 已安排重试。\nattempt=1\n\n",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()

			payload := NewCallbackPayload(tc.event)
			if payload.ReplyText != tc.wantReply {
				t.Fatalf("ReplyText = %q, want %q", payload.ReplyText, tc.wantReply)
			}
		})
	}
}

func TestCallbackPayloadRetryingThenProgressKeepsMarkdownBoundary(t *testing.T) {
	t.Parallel()

	retrying := NewCallbackPayload(Event{
		EventType: "task.retrying",
		TaskID:    "task-md-retry-progress",
		Status:    "processing",
		Stage:     "retrying",
		ReplyText: "任务本次 attempt 未完成，Gateway 已安排重试。\nerror=runtime_ws.stale_progress",
	})
	progress := NewCallbackPayload(Event{
		EventType: "task.progress",
		TaskID:    "task-md-retry-progress",
		Status:    "processing",
		Stage:     "tool_running",
		ReplyText: "### 执行进度：第 1-3 步\n\n1. **任务已启动**：运行环境已启动，开始处理请求",
	})

	combined := retrying.ReplyText + progress.ReplyText
	if strings.Contains(combined, "runtime_ws.stale_progress### 执行进度") {
		t.Fatalf("retrying and progress markdown blocks were concatenated without a boundary: %q", combined)
	}
	if !strings.Contains(combined, "runtime_ws.stale_progress\n\n### 执行进度") {
		t.Fatalf("combined markdown missing retrying/progress block boundary: %q", combined)
	}
}

func TestCallbackPayloadExposesEventEnvelope(t *testing.T) {
	t.Parallel()

	timestamp := time.Date(2026, 6, 5, 8, 9, 10, 11, time.UTC)
	event := Event{
		EventID:   "event-envelope",
		EventSeq:  7,
		EventType: "task.completed",
		TaskID:    "task-envelope",
		Status:    "completed",
		Stage:     "done",
		Timestamp: timestamp,
		Nonce:     "nonce-envelope",
		Signature: "signature-envelope",
	}

	payload := NewCallbackPayload(event)
	if payload.EventID != event.EventID {
		t.Fatalf("EventID = %q, want %q", payload.EventID, event.EventID)
	}
	if payload.EventSeq != event.EventSeq {
		t.Fatalf("EventSeq = %d, want %d", payload.EventSeq, event.EventSeq)
	}
	if payload.EventType != event.EventType {
		t.Fatalf("EventType = %q, want %q", payload.EventType, event.EventType)
	}
	if payload.Timestamp != timestamp.Format(time.RFC3339Nano) {
		t.Fatalf("Timestamp = %q, want %q", payload.Timestamp, timestamp.Format(time.RFC3339Nano))
	}
	if payload.Nonce != event.Nonce || payload.Signature != event.Signature {
		t.Fatalf("callback envelope nonce/signature not exposed: %#v", payload)
	}
}

func TestCompletedCallbackPayloadOmitsFilesWhenNoResultFile(t *testing.T) {
	t.Parallel()

	payload := NewCallbackPayload(Event{
		EventID:   "event-without-result-file",
		EventType: "task.completed",
		TaskID:    "task-without-result-file",
		Status:    "completed",
		Stage:     "done",
		ReplyText: "完成",
	})

	if len(payload.Files) != 0 {
		t.Fatalf("expected no callback files without a real result file, got %#v", payload.Files)
	}

	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal payload: %v", err)
	}
	var body map[string]any
	if err := json.Unmarshal(raw, &body); err != nil {
		t.Fatalf("unmarshal payload: %v", err)
	}
	if _, ok := body["files"]; ok {
		t.Fatalf("callback body must not include files without a real result file: %#v", body)
	}
}

func TestCompletedCallbackPayloadKeepsRealResultFile(t *testing.T) {
	t.Parallel()

	payload := NewCallbackPayload(Event{
		EventID:   "event-with-result-file",
		EventType: "task.completed",
		TaskID:    "task-with-result-file",
		Status:    "completed",
		Stage:     "done",
		Files: []FileRef{{
			FileID:      "real-result-file",
			Filename:    "result.txt",
			Size:        12,
			MimeType:    "text/plain",
			DownloadURL: "/api/v1/files/real-result-file/download",
		}},
	})

	if len(payload.Files) != 1 {
		t.Fatalf("expected real callback file, got %#v", payload.Files)
	}
	file := payload.Files[0]
	if file.FileID != "real-result-file" || file.FileName != "result.txt" || file.Size != "12" || file.MimeType != "text/plain" || file.DownloadURL != "/api/v1/files/real-result-file/download" {
		t.Fatalf("real result file should not be replaced by fallback: %#v", file)
	}
}

func TestCallbackPayloadFilesMatrix(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name       string
		event      Event
		wantFiles  []core.PublicTaskFileResponse
		omitFiles  bool
		wantStatus string
	}{
		{
			name: "processing without files omits files",
			event: Event{
				EventType: "task.accepted",
				TaskID:    "task-processing-empty",
				Status:    "processing",
				Stage:     "queued",
			},
			omitFiles:  true,
			wantStatus: "processing",
		},
		{
			name: "completed without files omits files",
			event: Event{
				EventType: "task.completed",
				TaskID:    "task-completed-empty",
				Status:    "completed",
				Stage:     "done",
			},
			omitFiles:  true,
			wantStatus: "completed",
		},
		{
			name: "failed can carry diagnostic files",
			event: Event{
				EventType: "task.failed",
				TaskID:    "task-failed-file",
				Status:    "failed",
				Stage:     "done",
				Files: []FileRef{{
					FileID:      "diag-file",
					Filename:    "error.log",
					Size:        48,
					MimeType:    "text/plain",
					DownloadURL: "/api/v1/files/diag-file/download",
				}},
			},
			wantFiles: []core.PublicTaskFileResponse{{
				FileID:      "diag-file",
				FileName:    "error.log",
				Size:        "48",
				MimeType:    "text/plain",
				DownloadURL: "/api/v1/files/diag-file/download",
			}},
			wantStatus: "failed",
		},
		{
			name: "multiple files keep order and skip blank file ids",
			event: Event{
				EventType: "task.completed",
				TaskID:    "task-multi-file",
				Status:    "completed",
				Stage:     "done",
				Files: []FileRef{
					{FileID: "file-a", Filename: "a.txt", Size: 1, MimeType: "text/plain", DownloadURL: "/api/v1/files/file-a/download"},
					{FileID: "", Filename: "ignored.txt", Size: 2},
					{FileID: "file-b", Filename: "b.csv", Size: 0, MimeType: "text/csv", DownloadURL: "/api/v1/files/file-b/download"},
				},
			},
			wantFiles: []core.PublicTaskFileResponse{
				{FileID: "file-a", FileName: "a.txt", Size: "1", MimeType: "text/plain", DownloadURL: "/api/v1/files/file-a/download"},
				{FileID: "file-b", FileName: "b.csv", MimeType: "text/csv", DownloadURL: "/api/v1/files/file-b/download"},
			},
			wantStatus: "completed",
		},
	}

	for _, tc := range tests {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()

			payload := NewCallbackPayload(tc.event)
			if payload.Status != tc.wantStatus {
				t.Fatalf("status = %q, want %q", payload.Status, tc.wantStatus)
			}
			if !reflect.DeepEqual(payload.Files, tc.wantFiles) {
				t.Fatalf("files = %#v, want %#v", payload.Files, tc.wantFiles)
			}

			raw, err := json.Marshal(payload)
			if err != nil {
				t.Fatalf("marshal payload: %v", err)
			}
			var body map[string]any
			if err := json.Unmarshal(raw, &body); err != nil {
				t.Fatalf("unmarshal payload: %v", err)
			}
			_, hasFiles := body["files"]
			if tc.omitFiles && hasFiles {
				t.Fatalf("files must be omitted for %s: %#v", tc.name, body)
			}
			if !tc.omitFiles && !hasFiles {
				t.Fatalf("files must be present for %s: %#v", tc.name, body)
			}
		})
	}
}

func TestOutboxDispatchMovesNetworkFailuresToDeadLetter(t *testing.T) {
	t.Parallel()

	store := NewMemoryOutboxStore()
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		closeCallbackConnection(w)
	}))
	defer callback.Close()

	dispatcher := NewDispatcher(Config{
		Secret:         "callback-secret",
		MaxAttempts:    1,
		RetryDelay:     time.Millisecond,
		Store:          store,
		WorkerInterval: time.Hour,
		PendingLimit:   10,
	})
	record, err := dispatcher.Dispatch(context.Background(), callback.URL, Event{
		EventType: "task.failed",
		TaskID:    "task-dead",
		Status:    "failed",
		Stage:     "error",
	})
	if err == nil {
		t.Fatalf("Dispatch() error = nil, want callback failure")
	}

	letters := dispatcher.DeadLetters()
	if len(letters) != 1 {
		t.Fatalf("DeadLetters() len = %d, want 1", len(letters))
	}
	if letters[0].Event.EventID != record.Event.EventID {
		t.Fatalf("dead-letter event_id = %q, want %q", letters[0].Event.EventID, record.Event.EventID)
	}
	if letters[0].Attempts != 1 {
		t.Fatalf("dead-letter attempts = %d, want 1", letters[0].Attempts)
	}
}

func TestDispatchTreatsHTTP500AsFailureByDefault(t *testing.T) {
	t.Parallel()

	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"code":500,"msg":"mock upstream failed"}`))
	}))
	defer callback.Close()

	var logs bytes.Buffer
	dispatcher := NewDispatcher(Config{
		Secret:      "callback-secret",
		MaxAttempts: 1,
		Logger:      slog.New(slog.NewTextHandler(&logs, nil)),
	})
	record, err := dispatcher.Dispatch(context.Background(), callback.URL, Event{
		EventType: "task.completed",
		TaskID:    "task-http-500",
		Status:    "completed",
		Stage:     "done",
		ReplyText: "ok",
	})
	if err == nil {
		t.Fatalf("Dispatch() error = nil, want HTTP status failure")
	}
	if record.Delivered {
		t.Fatalf("expected record to not be delivered")
	}
	if len(dispatcher.DeadLetters()) != 1 {
		t.Fatalf("DeadLetters() len = %d, want 1", len(dispatcher.DeadLetters()))
	}

	output := logs.String()
	for _, want := range []string{
		"callback事件推送失败，进入dead-letter",
		"callback_delivery_mode=http_response",
		"callback_response_observed=true",
		"callback_response_status_code=500",
		"callback_request_packet",
		"callback_response_packet",
		"HTTP/1.1 500 Internal Server Error",
		"mock upstream failed",
		"failure_reason=upstream_5xx",
	} {
		if !strings.Contains(output, want) {
			t.Fatalf("callback response log missing %q in:\n%s", want, output)
		}
	}
}

func TestDispatchAcceptsHTTP500WhenAcceptAnyResponseEnabled(t *testing.T) {
	t.Parallel()

	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"code":500,"msg":"mock upstream failed"}`))
	}))
	defer callback.Close()

	var logs bytes.Buffer
	dispatcher := NewDispatcher(Config{
		Secret:            "callback-secret",
		MaxAttempts:       1,
		AcceptAnyResponse: true,
		Logger:            slog.New(slog.NewTextHandler(&logs, nil)),
	})
	record, err := dispatcher.Dispatch(context.Background(), callback.URL, Event{
		EventType: "task.completed",
		TaskID:    "task-accept-any-response",
		Status:    "completed",
		Stage:     "done",
		ReplyText: "ok",
	})
	if err != nil {
		t.Fatalf("Dispatch() error = %v", err)
	}
	if !record.Delivered {
		t.Fatalf("expected record to be delivered")
	}
	if len(dispatcher.DeadLetters()) != 0 {
		t.Fatalf("DeadLetters() len = %d, want 0", len(dispatcher.DeadLetters()))
	}

	output := logs.String()
	for _, want := range []string{
		"callback事件推送成功",
		"callback_response_status_code=500",
		"callback_accept_any_response=true",
		"mock upstream failed",
	} {
		if !strings.Contains(output, want) {
			t.Fatalf("callback response log missing %q in:\n%s", want, output)
		}
	}
}

func TestDispatchTreatsAny2xxCallbackAsDelivered(t *testing.T) {
	t.Parallel()

	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"taskId":"wrong-task"}`))
	}))
	defer callback.Close()

	dispatcher := NewDispatcher(Config{
		Secret:      "callback-secret",
		MaxAttempts: 1,
	})
	record, err := dispatcher.Dispatch(context.Background(), callback.URL, Event{
		EventType: "task.completed",
		TaskID:    "task-no-ack-required",
		Status:    "completed",
		Stage:     "done",
		ReplyText: "ok",
	})
	if err != nil {
		t.Fatalf("Dispatch() error = %v", err)
	}
	if !record.Delivered {
		t.Fatalf("expected record to be delivered")
	}
}

func TestDispatchLogsCallbackResponseResult(t *testing.T) {
	t.Parallel()

	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusAccepted)
		_, _ = w.Write([]byte(`{"received":true,"trace":"upstream-1"}`))
	}))
	defer callback.Close()

	var logs bytes.Buffer
	dispatcher := NewDispatcher(Config{
		Secret:      "callback-secret",
		MaxAttempts: 1,
		Logger:      slog.New(slog.NewTextHandler(&logs, nil)),
	})
	record, err := dispatcher.Dispatch(context.Background(), callback.URL, Event{
		EventType: "task.completed",
		TaskID:    "task-log-response",
		Status:    "completed",
		Stage:     "done",
		ReplyText: "ok",
	})
	if err != nil {
		t.Fatalf("Dispatch() error = %v", err)
	}
	if !record.Delivered {
		t.Fatalf("expected record to be delivered")
	}

	output := logs.String()
	for _, want := range []string{
		"callback事件推送成功",
		"callback_delivery_mode=http_response",
		"callback_response_observed=true",
		"callback_response_status_code=202",
		"callback_response_status=\"202 Accepted\"",
		"callback_response_content_type=application/json",
		"callback_request_packet",
		"POST / HTTP/1.1",
		"callback_response_packet",
		"HTTP/1.1 202 Accepted",
		"received",
		"upstream-1",
	} {
		if !strings.Contains(output, want) {
			t.Fatalf("callback response log missing %q in:\n%s", want, output)
		}
	}
}

func TestDispatchLogsCallbackFilesAndResponseAckAsStructuredFields(t *testing.T) {
	t.Parallel()

	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true,"trace":"upstream-structured"}`))
	}))
	defer callback.Close()

	var logs bytes.Buffer
	dispatcher := NewDispatcher(Config{
		Secret:      "callback-secret",
		MaxAttempts: 1,
		Logger:      slog.New(slog.NewJSONHandler(&logs, nil)),
	})
	record, err := dispatcher.Dispatch(context.Background(), callback.URL, Event{
		EventType: "task.completed",
		TaskID:    "task-log-files",
		Status:    "completed",
		Stage:     "done",
		ReplyText: "ok",
		Files: []FileRef{{
			FileID:      "file-log-1",
			Filename:    "result.md",
			Size:        240,
			MimeType:    "text/markdown",
			DownloadURL: "/api/v1/files/file-log-1/download?user_id=user-1",
		}},
	})
	if err != nil {
		t.Fatalf("Dispatch() error = %v", err)
	}
	if !record.Delivered {
		t.Fatalf("expected record to be delivered")
	}

	entry := findJSONLogRecord(t, logs.String(), "callback事件推送成功")
	for key, want := range map[string]any{
		"callback_response_observed":     true,
		"callback_response_ok":           true,
		"callback_response_http_ok":      true,
		"callback_response_ack_observed": true,
		"callback_response_ack_ok":       true,
		"callback_response_ack_field":    "ok",
		"callback_has_files":             true,
	} {
		if got := entry[key]; got != want {
			t.Fatalf("%s = %#v, want %#v; entry=%#v", key, got, want, entry)
		}
	}
	if got := entry["callback_files_count"]; got != float64(1) {
		t.Fatalf("callback_files_count = %#v, want 1; entry=%#v", got, entry)
	}
	fileIDs, ok := entry["callback_file_ids"].([]any)
	if !ok || len(fileIDs) != 1 || fileIDs[0] != "file-log-1" {
		t.Fatalf("callback_file_ids = %#v, want [file-log-1]", entry["callback_file_ids"])
	}
	files, ok := entry["callback_files"].([]any)
	if !ok || len(files) != 1 {
		t.Fatalf("callback_files = %#v, want one file", entry["callback_files"])
	}
	file, ok := files[0].(map[string]any)
	if !ok || file["fileId"] != "file-log-1" || file["fileName"] != "result.md" {
		t.Fatalf("callback_files[0] = %#v, want file metadata", files[0])
	}
	filesJSON, ok := entry["callback_files_json"].(string)
	if !ok || !strings.Contains(filesJSON, `"fileId":"file-log-1"`) {
		t.Fatalf("callback_files_json = %#v, want file-log-1 JSON", entry["callback_files_json"])
	}
	requestPacket, ok := entry["callback_request_packet"].(string)
	if !ok || !strings.Contains(requestPacket, `"files":[`) || !strings.Contains(requestPacket, `"fileId":"file-log-1"`) {
		t.Fatalf("callback_request_packet does not expose files[] payload: %#v", entry["callback_request_packet"])
	}
}

func TestDispatchLogsEmptyCallbackResponseBodyAndPackets(t *testing.T) {
	t.Parallel()

	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	defer callback.Close()

	var logs bytes.Buffer
	dispatcher := NewDispatcher(Config{
		Secret:      "callback-secret",
		MaxAttempts: 1,
		Logger:      slog.New(slog.NewTextHandler(&logs, nil)),
	})
	record, err := dispatcher.Dispatch(context.Background(), callback.URL, Event{
		EventType: "task.progress",
		TaskID:    "task-log-empty-response-body",
		Status:    "processing",
		Stage:     "queued",
	})
	if err != nil {
		t.Fatalf("Dispatch() error = %v", err)
	}
	if !record.Delivered {
		t.Fatalf("expected record to be delivered")
	}

	output := logs.String()
	for _, want := range []string{
		"callback事件推送成功",
		"callback_request_packet",
		"POST / HTTP/1.1",
		"callback_response_packet",
		"HTTP/1.1 204 No Content",
		"callback_response_status_code=204",
		"callback_response_body=\"\"",
		"callback_response_body_truncated=false",
	} {
		if !strings.Contains(output, want) {
			t.Fatalf("callback response log missing %q in:\n%s", want, output)
		}
	}
}

func findJSONLogRecord(t *testing.T, output string, message string) map[string]any {
	t.Helper()
	for _, line := range strings.Split(output, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		var entry map[string]any
		if err := json.Unmarshal([]byte(line), &entry); err != nil {
			t.Fatalf("unmarshal log line %q: %v", line, err)
		}
		if entry["msg"] == message {
			return entry
		}
	}
	t.Fatalf("log message %q not found in:\n%s", message, output)
	return nil
}

func TestReadCallbackResponseBodyTruncatesLargeBody(t *testing.T) {
	t.Parallel()

	body, truncated, err := readCallbackResponseBody(strings.NewReader(strings.Repeat("x", maxCallbackResponseBodyBytes+10)))
	if err != nil {
		t.Fatalf("readCallbackResponseBody() error = %v", err)
	}
	if !truncated {
		t.Fatalf("truncated = false, want true")
	}
	if len(body) != maxCallbackResponseBodyBytes {
		t.Fatalf("body length = %d, want %d", len(body), maxCallbackResponseBodyBytes)
	}
}

func TestFireAndForgetDoesNotWaitForCallbackResponse(t *testing.T) {
	t.Parallel()

	received := make(chan struct{}, 1)
	release := make(chan struct{})
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		select {
		case received <- struct{}{}:
		default:
		}
		<-release
		w.WriteHeader(http.StatusNoContent)
	}))
	defer callback.Close()
	defer close(release)

	dispatcher := NewDispatcher(Config{
		Secret:         "callback-secret",
		MaxAttempts:    1,
		HTTPTimeout:    time.Second,
		FireAndForget:  true,
		Store:          NewMemoryOutboxStore(),
		WorkerInterval: time.Hour,
		PendingLimit:   10,
	})
	started := time.Now()
	record, err := dispatcher.Dispatch(context.Background(), callback.URL, Event{
		EventType: "task.completed",
		TaskID:    "task-fire-and-forget",
		Status:    "completed",
		Stage:     "done",
		ReplyText: "ok",
	})
	if err != nil {
		t.Fatalf("Dispatch() error = %v", err)
	}
	if elapsed := time.Since(started); elapsed > 100*time.Millisecond {
		t.Fatalf("Dispatch() waited %v, want fire-and-forget return", elapsed)
	}
	if !record.Delivered || record.Status != "delivered" {
		t.Fatalf("record = %#v, want delivered status", record)
	}
	if len(dispatcher.DeadLetters()) != 0 {
		t.Fatalf("DeadLetters() len = %d, want 0", len(dispatcher.DeadLetters()))
	}

	select {
	case <-received:
	case <-time.After(time.Second):
		t.Fatalf("timed out waiting for callback POST")
	}
}

func TestCallbackFailureAttrsClassifiesHTTPStatus(t *testing.T) {
	t.Parallel()

	attrs := callbackFailureAttrs(&callbackPostError{
		Kind:       "http_status",
		StatusCode: http.StatusInternalServerError,
		Body:       "upstream down",
	})
	values := map[string]any{}
	for i := 0; i+1 < len(attrs); i += 2 {
		key, _ := attrs[i].(string)
		values[key] = attrs[i+1]
	}

	if values["failure_reason"] != "upstream_5xx" {
		t.Fatalf("failure_reason = %#v, want upstream_5xx", values["failure_reason"])
	}
	if values["http_status"] != http.StatusInternalServerError {
		t.Fatalf("http_status = %#v, want %d", values["http_status"], http.StatusInternalServerError)
	}
	if values["response_body"] != "upstream down" {
		t.Fatalf("response_body = %#v, want upstream down", values["response_body"])
	}
}

func TestMemoryOutboxClaimLeaseAndRecoverStuck(t *testing.T) {
	t.Parallel()

	store := NewMemoryOutboxStore()
	now := time.Now().UTC()
	record := DeliveryRecord{
		Event: Event{
			EventID:   "event-lease",
			EventSeq:  1,
			EventType: "task.progress",
			TaskID:    "task-lease",
			Status:    "processing",
			Stage:     "tool_running",
		},
		CallbackURL: "https://callback.example.com",
		Status:      "pending",
		CreatedAt:   now,
		UpdatedAt:   now,
	}
	if err := store.Enqueue(context.Background(), record); err != nil {
		t.Fatalf("Enqueue() error = %v", err)
	}

	claimed, err := store.ClaimPending(context.Background(), now, 10, "worker-a", time.Millisecond)
	if err != nil {
		t.Fatalf("ClaimPending() error = %v", err)
	}
	if len(claimed) != 1 || claimed[0].Status != "sending" || claimed[0].LockedBy != "worker-a" {
		t.Fatalf("unexpected claimed records %#v", claimed)
	}

	claimedAgain, err := store.ClaimPending(context.Background(), now, 10, "worker-b", time.Millisecond)
	if err != nil {
		t.Fatalf("ClaimPending() second error = %v", err)
	}
	if len(claimedAgain) != 0 {
		t.Fatalf("expected leased event to be hidden, got %#v", claimedAgain)
	}

	recovered, err := store.RecoverStuck(context.Background(), now.Add(2*time.Millisecond))
	if err != nil {
		t.Fatalf("RecoverStuck() error = %v", err)
	}
	if recovered != 1 {
		t.Fatalf("RecoverStuck() = %d, want 1", recovered)
	}

	reclaimed, err := store.ClaimPending(context.Background(), now.Add(3*time.Millisecond), 10, "worker-b", time.Second)
	if err != nil {
		t.Fatalf("ClaimPending() after recovery error = %v", err)
	}
	if len(reclaimed) != 1 || reclaimed[0].LockedBy != "worker-b" {
		t.Fatalf("expected worker-b to reclaim event, got %#v", reclaimed)
	}
}

func TestMemoryOutboxSupersedesPendingRetryingEvents(t *testing.T) {
	t.Parallel()

	store := NewMemoryOutboxStore()
	now := time.Now().UTC()
	callbackURL := "https://callback.example.com/taskcallback"
	if err := store.Enqueue(context.Background(), DeliveryRecord{
		Event: Event{
			EventID:   "event-retrying-old",
			EventSeq:  1,
			EventType: "task.retrying",
			TaskID:    "task-retrying-superseded",
			Status:    "retrying",
			Stage:     "retry_wait",
		},
		CallbackURL: callbackURL,
		Status:      "pending",
		CreatedAt:   now,
		UpdatedAt:   now,
	}); err != nil {
		t.Fatalf("Enqueue old retrying error = %v", err)
	}
	if err := store.Enqueue(context.Background(), DeliveryRecord{
		Event: Event{
			EventID:   "event-retrying-new",
			EventSeq:  2,
			EventType: "task.retrying",
			TaskID:    "task-retrying-superseded",
			Status:    "retrying",
			Stage:     "retry_wait",
		},
		CallbackURL: callbackURL,
		Status:      "pending",
		CreatedAt:   now.Add(time.Millisecond),
		UpdatedAt:   now,
	}); err != nil {
		t.Fatalf("Enqueue new retrying error = %v", err)
	}

	claimed, err := store.ClaimPending(context.Background(), now.Add(time.Second), 10, "worker-a", time.Second)
	if err != nil {
		t.Fatalf("ClaimPending() error = %v", err)
	}
	if len(claimed) != 1 || claimed[0].Event.EventID != "event-retrying-new" {
		t.Fatalf("claimed = %#v, want only newest retrying", claimed)
	}
}

func TestMemoryOutboxTerminalEventSupersedesPendingRetrying(t *testing.T) {
	t.Parallel()

	store := NewMemoryOutboxStore()
	now := time.Now().UTC()
	callbackURL := "https://callback.example.com/taskcallback"
	if err := store.Enqueue(context.Background(), DeliveryRecord{
		Event: Event{
			EventID:   "event-retrying-before-terminal",
			EventSeq:  1,
			EventType: "task.retrying",
			TaskID:    "task-terminal-supersedes-retrying",
			Status:    "retrying",
			Stage:     "retry_wait",
		},
		CallbackURL: callbackURL,
		Status:      "pending",
		CreatedAt:   now,
		UpdatedAt:   now,
	}); err != nil {
		t.Fatalf("Enqueue retrying error = %v", err)
	}
	if err := store.Enqueue(context.Background(), DeliveryRecord{
		Event: Event{
			EventID:   "event-terminal-after-retrying",
			EventSeq:  2,
			EventType: "task.failed",
			TaskID:    "task-terminal-supersedes-retrying",
			Status:    "failed",
			Stage:     "error",
		},
		CallbackURL: callbackURL,
		Status:      "pending",
		CreatedAt:   now.Add(time.Millisecond),
		UpdatedAt:   now,
	}); err != nil {
		t.Fatalf("Enqueue terminal error = %v", err)
	}

	claimed, err := store.ClaimPending(context.Background(), now.Add(time.Second), 10, "worker-a", time.Second)
	if err != nil {
		t.Fatalf("ClaimPending() error = %v", err)
	}
	if len(claimed) != 1 || claimed[0].Event.EventID != "event-terminal-after-retrying" {
		t.Fatalf("claimed = %#v, want terminal event", claimed)
	}
}

func TestOutboxProcessPendingSupersedesLegacyRetryingBacklog(t *testing.T) {
	t.Parallel()

	received := make(chan int64, 3)
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload CallbackPayload
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Errorf("decode callback payload: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		received <- payload.EventSeq
		w.WriteHeader(http.StatusNoContent)
	}))
	defer callback.Close()

	store := NewMemoryOutboxStore()
	now := time.Now().UTC()
	store.mu.Lock()
	for seq := int64(1); seq <= 3; seq++ {
		eventID := fmt.Sprintf("legacy-retrying-%d", seq)
		store.records[eventID] = DeliveryRecord{
			Event: Event{
				EventID:   eventID,
				EventSeq:  seq,
				EventType: "task.retrying",
				TaskID:    "task-legacy-retrying-backlog",
				Status:    "retrying",
				Stage:     "retry_wait",
			},
			CallbackURL: callback.URL,
			Status:      "pending",
			CreatedAt:   now.Add(time.Duration(seq) * time.Millisecond),
			UpdatedAt:   now,
		}
	}
	store.mu.Unlock()

	var logs bytes.Buffer
	dispatcher := NewDispatcher(Config{
		Secret:              "callback-secret",
		MaxAttempts:         1,
		Store:               store,
		WorkerInterval:      time.Hour,
		PendingLimit:        10,
		DeliveryConcurrency: 1,
		Logger:              slog.New(slog.NewTextHandler(&logs, nil)),
	})
	processed, err := dispatcher.ProcessPending(context.Background())
	if err != nil {
		t.Fatalf("ProcessPending() error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}
	select {
	case seq := <-received:
		if seq != 3 {
			t.Fatalf("delivered event_seq = %d, want 3", seq)
		}
	default:
		t.Fatal("latest retrying callback was not delivered")
	}
	select {
	case seq := <-received:
		t.Fatalf("unexpected extra retrying callback event_seq=%d", seq)
	default:
	}
	if !strings.Contains(logs.String(), "superseded_count=2") {
		t.Fatalf("missing superseded_count log in:\n%s", logs.String())
	}
}

func TestOutboxProcessPendingCapsClaimLimitToDeliveryConcurrency(t *testing.T) {
	t.Parallel()

	store := &claimLimitOutboxStore{MemoryOutboxStore: NewMemoryOutboxStore()}
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	defer callback.Close()
	now := time.Now().UTC()
	for i := 0; i < 5; i++ {
		record := DeliveryRecord{
			Event: Event{
				EventID:   "event-claim-limit-" + string(rune('a'+i)),
				EventSeq:  int64(i + 1),
				EventType: "task.progress",
				TaskID:    "task-claim-limit-" + string(rune('a'+i)),
				Status:    "processing",
				Stage:     "tool_running",
			},
			CallbackURL: callback.URL,
			Status:      "pending",
			CreatedAt:   now.Add(time.Duration(i) * time.Millisecond),
			UpdatedAt:   now,
		}
		if err := store.Enqueue(context.Background(), record); err != nil {
			t.Fatalf("Enqueue() error = %v", err)
		}
	}

	dispatcher := NewDispatcher(Config{
		Secret:              "callback-secret",
		MaxAttempts:         1,
		Store:               store,
		WorkerInterval:      time.Hour,
		PendingLimit:        10,
		DeliveryConcurrency: 2,
	})
	processed, err := dispatcher.ProcessPending(context.Background())
	if err != nil {
		t.Fatalf("ProcessPending() error = %v", err)
	}
	if processed != 2 {
		t.Fatalf("processed = %d, want 2", processed)
	}
	if len(store.claimLimits) != 1 || store.claimLimits[0] != 2 {
		t.Fatalf("claim limits = %#v, want [2]", store.claimLimits)
	}
}

func TestOutboxProcessPendingBlocksLaterSameTaskEvents(t *testing.T) {
	t.Parallel()

	received := make(chan string, 2)
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload CallbackPayload
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Errorf("decode callback payload: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		received <- payload.Stage
		w.WriteHeader(http.StatusNoContent)
	}))
	defer callback.Close()

	store := NewMemoryOutboxStore()
	now := time.Now().UTC()
	for index, stage := range []string{"first", "second"} {
		record := DeliveryRecord{
			Event: Event{
				EventID:   "event-same-task-" + stage,
				EventSeq:  int64(index + 1),
				EventType: "task.progress",
				TaskID:    "task-same-lane",
				Status:    "processing",
				Stage:     stage,
			},
			CallbackURL: callback.URL,
			Status:      "pending",
			CreatedAt:   now.Add(time.Duration(index) * time.Millisecond),
			UpdatedAt:   now,
		}
		if err := store.Enqueue(context.Background(), record); err != nil {
			t.Fatalf("Enqueue() error = %v", err)
		}
	}
	dispatcher := NewDispatcher(Config{
		Secret:              "callback-secret",
		MaxAttempts:         1,
		Store:               store,
		WorkerInterval:      time.Hour,
		PendingLimit:        10,
		DeliveryConcurrency: 4,
	})

	processed, err := dispatcher.ProcessPending(context.Background())
	if err != nil {
		t.Fatalf("ProcessPending() first error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("first processed = %d, want 1", processed)
	}
	select {
	case stage := <-received:
		if stage != "first" {
			t.Fatalf("first delivered stage = %q, want first", stage)
		}
	default:
		t.Fatal("first callback was not delivered")
	}

	processed, err = dispatcher.ProcessPending(context.Background())
	if err != nil {
		t.Fatalf("ProcessPending() second error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("second processed = %d, want 1", processed)
	}
	select {
	case stage := <-received:
		if stage != "second" {
			t.Fatalf("second delivered stage = %q, want second", stage)
		}
	default:
		t.Fatal("second callback was not delivered")
	}
}

func TestOutboxDoesNotRetryReceiverLimitRejection(t *testing.T) {
	t.Parallel()

	var calls atomic.Int64
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls.Add(1)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(`{"code":-1,"msg":"exceed max call times"}`))
	}))
	defer callback.Close()

	store := NewMemoryOutboxStore()
	var logs bytes.Buffer
	dispatcher := NewDispatcher(Config{
		Secret:              "callback-secret",
		MaxAttempts:         3,
		RetryDelay:          time.Millisecond,
		Store:               store,
		WorkerInterval:      time.Hour,
		PendingLimit:        10,
		DeliveryConcurrency: 1,
		DisableOutboxWorker: true,
		Logger:              slog.New(slog.NewTextHandler(&logs, nil)),
	})
	record, err := dispatcher.DispatchAsync(context.Background(), callback.URL, Event{
		EventType: "task.retrying",
		TaskID:    "task-receiver-limit",
		Status:    "retrying",
		Stage:     "retry_wait",
		ReplyText: "capacity wait",
	})
	if err != nil {
		t.Fatalf("DispatchAsync() error = %v", err)
	}
	if _, err := dispatcher.ProcessPending(context.Background()); err != nil {
		t.Fatalf("ProcessPending() error = %v", err)
	}
	if calls.Load() != 1 {
		t.Fatalf("callback calls = %d, want 1", calls.Load())
	}
	letters := dispatcher.DeadLetters()
	if len(letters) != 1 {
		t.Fatalf("DeadLetters() len = %d, want 1", len(letters))
	}
	if letters[0].Event.EventID != record.Event.EventID || letters[0].Attempts != 1 {
		t.Fatalf("unexpected dead letter: %#v", letters[0])
	}
	output := logs.String()
	for _, want := range []string{
		"callback事件被接收端明确拒收，进入dead-letter",
		"failure_reason=receiver_limit",
		"exceed max call times",
	} {
		if !strings.Contains(output, want) {
			t.Fatalf("callback receiver-limit log missing %q in:\n%s", want, output)
		}
	}
}

func TestOutboxRetriesHTTP500ReceiverLimitPhraseUntilMaxAttempts(t *testing.T) {
	t.Parallel()

	var calls atomic.Int64
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls.Add(1)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"code":-1,"msg":"exceed max call times"}`))
	}))
	defer callback.Close()

	store := NewMemoryOutboxStore()
	var logs bytes.Buffer
	dispatcher := NewDispatcher(Config{
		Secret:              "callback-secret",
		MaxAttempts:         3,
		RetryDelay:          time.Millisecond,
		MaxRetryDelay:       time.Millisecond,
		Store:               store,
		WorkerInterval:      time.Hour,
		PendingLimit:        10,
		DeliveryConcurrency: 1,
		DisableOutboxWorker: true,
		Logger:              slog.New(slog.NewTextHandler(&logs, nil)),
	})
	record, err := dispatcher.DispatchAsync(context.Background(), callback.URL, Event{
		EventType: "task.completed",
		TaskID:    "task-terminal-500",
		Status:    "completed",
		Stage:     "done",
		ReplyText: "done",
	})
	if err != nil {
		t.Fatalf("DispatchAsync() error = %v", err)
	}
	for attempt := 1; attempt <= 3; attempt++ {
		if attempt > 1 {
			time.Sleep(2 * time.Millisecond)
		}
		if _, err := dispatcher.ProcessPending(context.Background()); err != nil {
			t.Fatalf("ProcessPending() attempt %d error = %v", attempt, err)
		}
	}
	if calls.Load() != 3 {
		t.Fatalf("callback calls = %d, want 3", calls.Load())
	}
	letters := dispatcher.DeadLetters()
	if len(letters) != 1 {
		t.Fatalf("DeadLetters() len = %d, want 1", len(letters))
	}
	if letters[0].Event.EventID != record.Event.EventID || letters[0].Attempts != 3 {
		t.Fatalf("unexpected dead letter: %#v", letters[0])
	}
	output := logs.String()
	if strings.Contains(output, "failure_reason=receiver_limit") {
		t.Fatalf("HTTP 500 receiver-limit phrase must not be classified receiver_limit:\n%s", output)
	}
	if !strings.Contains(output, "failure_reason=upstream_5xx") {
		t.Fatalf("HTTP 500 receiver-limit phrase log missing upstream_5xx in:\n%s", output)
	}
}

func TestOutboxDeadLetterReplay(t *testing.T) {
	t.Parallel()

	store := NewMemoryOutboxStore()
	var dropResponse atomic.Bool
	dropResponse.Store(true)
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if dropResponse.Load() {
			closeCallbackConnection(w)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer callback.Close()

	dispatcher := NewDispatcher(Config{
		Secret:         "callback-secret",
		MaxAttempts:    1,
		RetryDelay:     time.Millisecond,
		Store:          store,
		WorkerInterval: time.Hour,
		PendingLimit:   10,
	})
	record, err := dispatcher.Dispatch(context.Background(), callback.URL, Event{
		EventType: "task.failed",
		TaskID:    "task-replay",
		Status:    "failed",
		Stage:     "error",
	})
	if err == nil {
		t.Fatalf("Dispatch() error = nil, want callback failure")
	}

	dropResponse.Store(false)
	replayed, ok, err := dispatcher.ReplayDeadLetter(context.Background(), record.Event.EventID)
	if err != nil {
		t.Fatalf("ReplayDeadLetter() error = %v", err)
	}
	if !ok || replayed.Status != "pending" || replayed.Attempts != 0 {
		t.Fatalf("unexpected replayed record %#v ok=%v", replayed, ok)
	}
	if _, err := dispatcher.ProcessPending(context.Background()); err != nil {
		t.Fatalf("ProcessPending() error = %v", err)
	}
	if len(dispatcher.DeadLetters()) != 0 {
		t.Fatalf("expected dead-letter to be cleared after replay")
	}
}

func closeCallbackConnection(w http.ResponseWriter) {
	hijacker, ok := w.(http.Hijacker)
	if !ok {
		return
	}
	conn, _, err := hijacker.Hijack()
	if err != nil {
		return
	}
	_ = conn.Close()
}

type blockingSeqOutboxStore struct {
	*MemoryOutboxStore
	entered chan struct{}
	release chan struct{}
}

func (s *blockingSeqOutboxStore) NextEventSeq(ctx context.Context, taskID string) (int64, error) {
	select {
	case s.entered <- struct{}{}:
	default:
	}
	select {
	case <-s.release:
		return s.MemoryOutboxStore.NextEventSeq(ctx, taskID)
	case <-ctx.Done():
		return 0, ctx.Err()
	}
}

type claimLimitOutboxStore struct {
	*MemoryOutboxStore
	claimLimits []int
}

func (s *claimLimitOutboxStore) ClaimPending(
	ctx context.Context,
	now time.Time,
	limit int,
	workerID string,
	leaseDuration time.Duration,
) ([]DeliveryRecord, error) {
	s.claimLimits = append(s.claimLimits, limit)
	return s.MemoryOutboxStore.ClaimPending(ctx, now, limit, workerID, leaseDuration)
}
