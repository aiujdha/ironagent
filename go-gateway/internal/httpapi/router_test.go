package httpapi

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"mime/multipart"
	"net"
	"net/http"
	"net/http/httptest"
	"net/textproto"
	"net/url"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"ironagent/go-gateway/internal/auth"
	"ironagent/go-gateway/internal/core"
	gatewaymetrics "ironagent/go-gateway/internal/metrics"
	"ironagent/go-gateway/internal/presign"
	"ironagent/go-gateway/internal/runtimepool"
	"ironagent/go-gateway/internal/southbound"
	"ironagent/go-gateway/internal/taskstore"
	"ironagent/go-gateway/internal/webhook"

	"github.com/gorilla/websocket"
)

func TestWithUpstreamRequestID(t *testing.T) {
	t.Parallel()
	var captured string
	h := withUpstreamRequestID(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		captured = upstreamRequestID(r.Context())
	}))
	cases := []struct {
		name   string
		header string
		value  string
		want   string
	}{
		{"X-Request-ID", "X-Request-ID", "req-abc", "req-abc"},
		{"X-Correlation-ID fallback", "X-Correlation-ID", "corr-1", "corr-1"},
		{"no header -> empty", "", "", ""},
	}
	for _, tc := range cases {
		captured = "sentinel"
		req := httptest.NewRequest(http.MethodGet, "/x", nil)
		if tc.header != "" {
			req.Header.Set(tc.header, tc.value)
		}
		h.ServeHTTP(httptest.NewRecorder(), req)
		if captured != tc.want {
			t.Errorf("%s: upstreamRequestID = %q, want %q", tc.name, captured, tc.want)
		}
	}
}

func TestPhaseOneApprovalClosure(t *testing.T) {
	t.Parallel()

	service := core.NewService(newFakeSouthbound())
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-1",
		"instruction":         "Summarize the document in one sentence.",
		"approval_mode":       "user_required",
		"created_by":          "e2e-main-0416",
	}, http.StatusAccepted)

	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)
	if created.Status != "pending_approval" {
		t.Fatalf("expected pending_approval, got %q", created.Status)
	}
	if created.Stage != "waiting_approval" {
		t.Fatalf("expected waiting_approval stage, got %q", created.Stage)
	}
	if created.TaskID == "" {
		t.Fatalf("expected task_id in create response")
	}

	resolveResponse := performJSONRequest(
		t,
		router,
		http.MethodPost,
		"/api/v1/tasks/"+created.TaskID+"/approvals/resolve",
		map[string]any{"resolution": "allow"},
		http.StatusOK,
	)

	var resolved struct {
		Task core.TaskView `json:"task"`
	}
	decodeResponse(t, resolveResponse, &resolved)
	if resolved.Task.Status != "running" {
		t.Fatalf("expected running after approval resolve, got %q", resolved.Task.Status)
	}
	if resolved.Task.Approval == nil || resolved.Task.Approval.Status != "approved" {
		t.Fatalf("expected approved approval status, got %#v", resolved.Task.Approval)
	}

	getResponse := performJSONRequest(
		t,
		router,
		http.MethodGet,
		"/api/v1/tasks/"+created.TaskID,
		nil,
		http.StatusOK,
	)

	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if fetched.Status != "processing" {
		t.Fatalf("expected processing on follow-up get, got %q", fetched.Status)
	}
	if fetched.Stage != "tool_running" {
		t.Fatalf("expected tool_running on follow-up get, got %q", fetched.Stage)
	}
	var rawTask map[string]any
	decodeResponse(t, getResponse, &rawTask)
	if _, ok := rawTask["approval"]; ok {
		t.Fatalf("task query response must not expose approval internals, got %#v", rawTask["approval"])
	}
}

func TestApprovalDecisionFacadeResolvesAndStartsTask(t *testing.T) {
	t.Parallel()

	proxiedApprovalFacade := false
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/api/v1/approvals/") {
			proxiedApprovalFacade = true
		}
		w.WriteHeader(http.StatusTeapot)
	}))
	defer upstream.Close()

	southboundFake := newFakeSouthbound()
	service := core.NewServiceWithTaskStore(southboundFake, taskstore.NewMemoryStore())
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{SecurityBaseURL: upstream.URL},
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-approval-facade",
		"instruction":         "Run after approval.",
		"approval_mode":       "user_required",
		"request_id":          "req-approval-facade",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	approvalID := southboundFake.tasks[created.TaskID].ApprovalID
	getApprovalResponse := performJSONRequest(
		t,
		router,
		http.MethodGet,
		"/api/v1/approvals/"+approvalID,
		nil,
		http.StatusOK,
	)
	var fetched openApprovalView
	decodeResponse(t, getApprovalResponse, &fetched)
	if fetched.TaskID != created.TaskID {
		t.Fatalf("expected approval facade to expose task_id %q, got %q", created.TaskID, fetched.TaskID)
	}

	decisionResponse := performJSONRequest(
		t,
		router,
		http.MethodPost,
		"/api/v1/approvals/"+approvalID+"/decision",
		map[string]any{"decision": "approve", "comment": "已确认"},
		http.StatusOK,
	)
	var decided openApprovalDecisionView
	decodeResponse(t, decisionResponse, &decided)
	if decided.Decision != "approve" {
		t.Fatalf("expected external approve decision, got %q", decided.Decision)
	}
	if decided.ResolvedAt.IsZero() {
		t.Fatalf("expected resolved_at in approval decision response")
	}
	taskResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var task publicTaskResponse
	decodeResponse(t, taskResponse, &task)
	if task.Status != "processing" || task.Stage != "tool_running" {
		t.Fatalf("expected approval decision to start task, got %#v", task)
	}
	if proxiedApprovalFacade {
		t.Fatalf("approval facade route was incorrectly proxied to security-service")
	}
}

func TestPublicChatRejectsFieldsOutsideIronAgentContract(t *testing.T) {
	t.Parallel()

	service := core.NewService(newFakeSouthbound())
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	requestBody := map[string]any{
		"request_id":          "req-contract-extra",
		"user_id":             "contract-user",
		"conversation_id":     "conv-contract",
		"message":             "hello",
		"target_assistant_id": "demo-main",
	}
	raw, err := json.Marshal(requestBody)
	if err != nil {
		t.Fatalf("marshal request: %v", err)
	}
	request := httptest.NewRequest(http.MethodPost, "/api/v1/chat", bytes.NewReader(raw))
	request.Header.Set("Content-Type", "application/json")
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 for unsupported field, got %d body=%s", recorder.Code, recorder.Body.String())
	}
}

func TestPublicChatWithPublicScopedAPIKeyRejectsInternalFields(t *testing.T) {
	t.Parallel()

	service := core.NewService(newFakeSouthbound())
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{
			ScopedAPIKeys: []auth.ScopedAPIKey{{Key: "public-key", Scopes: []string{"public"}}},
		}),
		nil,
	)

	requestBody := map[string]any{
		"request_id":          "req-public-key-extra",
		"user_id":             "contract-user",
		"conversation_id":     "conv-contract",
		"message":             "hello",
		"target_assistant_id": "demo-main",
	}
	raw, err := json.Marshal(requestBody)
	if err != nil {
		t.Fatalf("marshal request: %v", err)
	}
	request := httptest.NewRequest(http.MethodPost, "/api/v1/chat", bytes.NewReader(raw))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("X-API-Key", "public-key")
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 for unsupported field, got %d body=%s", recorder.Code, recorder.Body.String())
	}
}

func TestUserVerifyReturnsOnlyBoolean(t *testing.T) {
	t.Parallel()

	southboundFake := newFakeSouthbound()
	southboundFake.users["known-user"] = southbound.User{UserID: "known-user"}
	southboundFake.userErrors["error-user"] = errors.New("security service unavailable")
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		core.NewService(southboundFake),
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	tests := []struct {
		name string
		path string
		want bool
	}{
		{
			name: "known user",
			path: "/api/v1/users/verify?user_id=known-user",
			want: true,
		},
		{
			name: "unknown user",
			path: "/api/v1/users/verify?user_id=missing-user",
			want: false,
		},
		{
			name: "empty user",
			path: "/api/v1/users/verify?user_id=",
			want: false,
		},
		{
			name: "missing user parameter",
			path: "/api/v1/users/verify",
			want: false,
		},
		{
			name: "southbound error",
			path: "/api/v1/users/verify?user_id=error-user",
			want: false,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			response := performPublicJSONRequest(t, router, http.MethodGet, tc.path, nil, http.StatusOK)
			var got bool
			decodeResponse(t, response, &got)
			if got != tc.want {
				t.Fatalf("verify response = %v, want %v body=%s", got, tc.want, response.Body.String())
			}
		})
	}
}

func TestPublicChatRejectsCallbackURLField(t *testing.T) {
	t.Parallel()

	service := core.NewService(newFakeSouthbound())
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	response := performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":      "req-public-callback-url",
		"user_id":         "e2e-main-0416",
		"conversation_id": "conv-public-callback-url",
		"message":         "hello",
		"callback_url":    "http://127.0.0.1:18081/callback",
	}, http.StatusBadRequest)
	var payload map[string]map[string]string
	decodeResponse(t, response, &payload)
	if payload["error"]["code"] != "bad_request" {
		t.Fatalf("unexpected error payload %#v", payload)
	}
	if !strings.Contains(payload["error"]["message"], "callback_url") {
		t.Fatalf("expected callback_url in error message, got %#v", payload)
	}
}

func TestPublicChatAcceptsTimeoutSeconds(t *testing.T) {
	t.Parallel()

	southboundFake := newFakeSouthbound()
	service := core.NewService(southboundFake)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	response := performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":      "req-public-timeout",
		"user_id":         "e2e-main-0416",
		"conversation_id": "conv-public-timeout",
		"message":         "hello",
		"timeout_seconds": 123,
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, response, &created)

	var stored southbound.Task
	for _, task := range southboundFake.tasks {
		if task.ID == created.TaskID {
			stored = task
			break
		}
	}
	if stored.ID == "" {
		t.Fatalf("created task %q not found in southbound tasks", created.TaskID)
	}
	if stored.TimeoutSeconds != 123 {
		t.Fatalf("TimeoutSeconds = %d, want 123", stored.TimeoutSeconds)
	}
}

func TestPublicChatRequiresRequestID(t *testing.T) {
	t.Parallel()

	southboundFake := newFakeSouthbound()
	service := core.NewService(southboundFake)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	response := performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"user_id":         "e2e-main-0416",
		"conversation_id": "conv-missing-request-id",
		"message":         "hello",
	}, http.StatusBadRequest)

	var payload map[string]map[string]string
	decodeResponse(t, response, &payload)
	if payload["error"]["message"] != "缺少请求唯一标识，请传入 request_id" {
		t.Fatalf("unexpected error response: %#v", payload)
	}
	if southboundFake.nextTaskID != 0 {
		t.Fatalf("expected missing request_id to be rejected before task creation, got %d tasks", southboundFake.nextTaskID)
	}
}

func TestPublicChatRejectsUnknownFileIDBeforeTaskCreation(t *testing.T) {
	t.Parallel()

	southboundFake := newFakeSouthbound()
	service := core.NewService(southboundFake)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	response := performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":      "req-missing-file",
		"user_id":         "e2e-main-0416",
		"conversation_id": "conv-missing-file",
		"message":         "请处理附件",
		"files":           []string{"missing-file"},
	}, http.StatusUnprocessableEntity)

	var payload map[string]map[string]string
	decodeResponse(t, response, &payload)
	if payload["error"]["message"] != "引用的文件不存在: missing-file" {
		t.Fatalf("unexpected error response: %#v", payload)
	}
	if southboundFake.nextTaskID != 0 {
		t.Fatalf("expected unknown file_id to be rejected before task creation, got %d tasks", southboundFake.nextTaskID)
	}
}

func TestPublicChatRejectsMismatchedFileMetadataBeforeTaskCreation(t *testing.T) {
	t.Parallel()

	southboundFake := newFakeSouthbound()
	southboundFake.files["file-1"] = southbound.RawJSON{
		"file_id":  "file-2",
		"user_id":  "e2e-main-0416",
		"filename": "wrong.txt",
	}
	service := core.NewService(southboundFake)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	response := performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":      "req-mismatched-file-metadata",
		"user_id":         "e2e-main-0416",
		"conversation_id": "conv-mismatched-file-metadata",
		"message":         "请处理附件",
		"files":           []string{"file-1"},
	}, http.StatusUnprocessableEntity)

	var payload map[string]map[string]string
	decodeResponse(t, response, &payload)
	if payload["error"]["message"] != "引用的文件不存在: file-1" {
		t.Fatalf("unexpected error response: %#v", payload)
	}
	if southboundFake.nextTaskID != 0 {
		t.Fatalf("expected mismatched file metadata to be rejected before task creation, got %d tasks", southboundFake.nextTaskID)
	}
}

func TestPublicChatRejectsOwnerlessFileBeforeTaskCreation(t *testing.T) {
	t.Parallel()

	southboundFake := newFakeSouthbound()
	southboundFake.files["ownerless-file"] = southbound.RawJSON{
		"file_id":  "ownerless-file",
		"filename": "ownerless.txt",
	}
	service := core.NewService(southboundFake)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	response := performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":      "req-ownerless-file",
		"user_id":         "e2e-main-0416",
		"conversation_id": "conv-ownerless-file",
		"message":         "请处理附件",
		"files":           []string{"ownerless-file"},
	}, http.StatusForbidden)

	var payload map[string]map[string]string
	decodeResponse(t, response, &payload)
	if payload["error"]["message"] != "文件缺少用户归属" {
		t.Fatalf("unexpected error response: %#v", payload)
	}
	if southboundFake.nextTaskID != 0 {
		t.Fatalf("expected ownerless file to be rejected before task creation, got %d tasks", southboundFake.nextTaskID)
	}
}

func TestPublicChatReturnsQueuedReceiptEvenWhenSouthboundStartsImmediately(t *testing.T) {
	t.Parallel()

	service := core.NewService(newFakeSouthbound())
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	response := performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":      "req-public-queued-receipt",
		"user_id":         "e2e-main-0416",
		"conversation_id": "conv-public-queued-receipt",
		"message":         "请回复 HEARTBEAT_OK",
	}, http.StatusAccepted)

	var created core.ChatResponse
	decodeResponse(t, response, &created)
	if created.TaskID == "" {
		t.Fatalf("expected task_id in public chat response")
	}
	if created.Status != "processing" || created.Stage != "queued" {
		t.Fatalf("public chat receipt must match API doc processing/queued, got status=%q stage=%q", created.Status, created.Stage)
	}
}

func TestTaskQueryUsesCallbackPayloadShape(t *testing.T) {
	t.Parallel()

	southboundFake := newFakeSouthbound()
	service := core.NewService(southboundFake)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-task-query-shape",
		"instruction":         "Run once.",
		"approval_mode":       "none",
		"request_id":          "req-task-query-shape",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	getResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var payload map[string]any
	decodeResponse(t, getResponse, &payload)
	if payload["taskId"] != created.TaskID {
		t.Fatalf("expected taskId %q, got %#v", created.TaskID, payload)
	}
	for _, forbidden := range []string{"task_id", "reply_text", "error_code", "error_message", "error_reason", "created_at", "updated_at"} {
		if _, ok := payload[forbidden]; ok {
			t.Fatalf("task query must not expose snake_case field %q: %#v", forbidden, payload)
		}
	}
	if _, ok := payload["replyText"]; !ok {
		t.Fatalf("task query must expose replyText in callback payload shape: %#v", payload)
	}
}

func TestCompletedCallbackEnrichesResultFileMetadata(t *testing.T) {
	t.Parallel()

	delivered := &eventCollector{}
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		event, err := decodeCallbackEventFromRequest(r)
		if err != nil {
			t.Errorf("decode callback event: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		delivered.add(event)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer callback.Close()

	southboundFake := newFakeSouthbound()
	southboundFake.files["file-1"] = southbound.RawJSON{
		"file_id":      "file-1",
		"user_id":      "user-callback-file",
		"filename":     "result.txt",
		"size":         12,
		"content_type": "text/plain",
		"download_url": "/api/v1/workspace-files/file-1/download",
	}
	dispatcher := webhook.NewDispatcher(webhook.Config{
		Secret:      "callback-secret",
		MaxAttempts: 1,
		RetryDelay:  time.Millisecond,
	})
	service := core.NewService(southboundFake)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			Webhooks:             dispatcher,
			CallbackAllowedHosts: []string{"127.0.0.1"},
			CallbackAllowPrivate: true,
		},
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-callback-file",
		"instruction":         "Run once.",
		"approval_mode":       "none",
		"callback_url":        callback.URL,
		"request_id":          "req-callback-file",
		"user_id":             "user-callback-file",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)
	markFakeResultFileForTask(t, southboundFake, "file-1", created.TaskID, "demo-approval")

	performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+created.TaskID+"/complete", map[string]any{
		"assistant_id":             "demo-approval",
		"claim_token":              "token-1",
		"result_workspace_file_id": "file-1",
	}, http.StatusOK)
	if !dispatcher.WaitIdle(time.Second) {
		t.Fatalf("timed out waiting for callbacks")
	}
	events := delivered.snapshot()
	if len(events) == 0 {
		t.Fatalf("expected callback events")
	}
	last := events[len(events)-1]
	if last.Status != "completed" || last.Stage != "done" {
		t.Fatalf("expected completed callback, got %#v", last)
	}
	if len(last.Files) != 1 {
		t.Fatalf("expected completed callback file metadata, got %#v", last)
	}
	file := last.Files[0]
	if file.FileID != "file-1" || file.Filename != "result.txt" || file.Size != 12 || file.MimeType != "text/plain" {
		t.Fatalf("unexpected callback file metadata: %#v", file)
	}
	if file.DownloadURL != "/api/v1/files/file-1/download?user_id=user-callback-file" {
		t.Fatalf("expected callback file download_url, got %#v", file)
	}

	taskResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var taskPayload core.PublicTaskResponse
	decodeResponse(t, taskResponse, &taskPayload)
	if len(taskPayload.Files) != 1 || taskPayload.Files[0].DownloadURL != "/api/v1/files/file-1/download?user_id=user-callback-file" {
		t.Fatalf("expected task query file download_url, got %#v", taskPayload.Files)
	}
}

func TestCompletedCallbackAndTaskQueryReturnMultipleResultFiles(t *testing.T) {
	t.Parallel()

	southboundFake := newFakeSouthbound()
	for _, file := range []southbound.RawJSON{
		{
			"file_id":      "file-2",
			"user_id":      "user-callback-files",
			"filename":     "summary.json",
			"size_bytes":   21,
			"content_type": "application/json",
			"download_url": "/api/v1/workspace-files/file-2/download",
		},
		{
			"file_id":      "file-1",
			"user_id":      "user-callback-files",
			"filename":     "report.txt",
			"size_bytes":   12,
			"content_type": "text/plain",
			"download_url": "/api/v1/workspace-files/file-1/download",
		},
	} {
		southboundFake.files[stringValue(file["file_id"])] = file
	}
	router, dispatcher, delivered, callbackURL := newCallbackTestFixture(t, southboundFake)
	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-callback-files",
		"instruction":         "Run once.",
		"approval_mode":       "none",
		"callback_url":        callbackURL,
		"request_id":          "req-callback-files",
		"user_id":             "user-callback-files",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)
	markFakeResultFileForTask(t, southboundFake, "file-2", created.TaskID, "demo-approval")
	markFakeResultFileForTask(t, southboundFake, "file-1", created.TaskID, "demo-approval")

	performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+created.TaskID+"/complete", map[string]any{
		"assistant_id":              "demo-approval",
		"claim_token":               "token-1",
		"result_workspace_file_ids": []string{"file-2", "file-1"},
	}, http.StatusOK)
	if !dispatcher.WaitIdle(time.Second) {
		t.Fatalf("timed out waiting for callbacks")
	}
	events := delivered.snapshot()
	if len(events) == 0 {
		t.Fatalf("expected callback events")
	}
	last := events[len(events)-1]
	wantCallbackFiles := []webhook.FileRef{
		{
			FileID:      "file-2",
			Filename:    "summary.json",
			Size:        21,
			MimeType:    "application/json",
			DownloadURL: "/api/v1/files/file-2/download?user_id=user-callback-files",
		},
		{
			FileID:      "file-1",
			Filename:    "report.txt",
			Size:        12,
			MimeType:    "text/plain",
			DownloadURL: "/api/v1/files/file-1/download?user_id=user-callback-files",
		},
	}
	if !reflect.DeepEqual(last.Files, wantCallbackFiles) {
		t.Fatalf("callback files = %#v, want %#v", last.Files, wantCallbackFiles)
	}

	taskResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var taskPayload core.PublicTaskResponse
	decodeResponse(t, taskResponse, &taskPayload)
	wantPublicFiles := []core.PublicTaskFileResponse{
		{
			FileID:      "file-2",
			FileName:    "summary.json",
			Size:        "21",
			MimeType:    "application/json",
			DownloadURL: "/api/v1/files/file-2/download?user_id=user-callback-files",
		},
		{
			FileID:      "file-1",
			FileName:    "report.txt",
			Size:        "12",
			MimeType:    "text/plain",
			DownloadURL: "/api/v1/files/file-1/download?user_id=user-callback-files",
		},
	}
	if !reflect.DeepEqual(taskPayload.Files, wantPublicFiles) {
		t.Fatalf("task query files = %#v, want %#v", taskPayload.Files, wantPublicFiles)
	}
}

func TestCompletedCallbackAndTaskQueryOmitFilesWithoutResultFile(t *testing.T) {
	t.Parallel()

	delivered := &eventCollector{}
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		event, err := decodeCallbackEventFromRequest(r)
		if err != nil {
			t.Errorf("decode callback event: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		delivered.add(event)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer callback.Close()

	dispatcher := webhook.NewDispatcher(webhook.Config{
		Secret:      "callback-secret",
		MaxAttempts: 1,
		RetryDelay:  time.Millisecond,
	})
	service := core.NewService(newFakeSouthbound())
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			Webhooks:             dispatcher,
			CallbackAllowedHosts: []string{"127.0.0.1"},
			CallbackAllowPrivate: true,
		},
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-without-result-file",
		"instruction":         "Run once.",
		"approval_mode":       "none",
		"callback_url":        callback.URL,
		"request_id":          "req-without-result-file",
		"user_id":             "user-without-result-file",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+created.TaskID+"/complete", map[string]any{
		"assistant_id":     "demo-approval",
		"claim_token":      "token-1",
		"result_object_id": "object-result-only",
	}, http.StatusOK)
	if !dispatcher.WaitIdle(time.Second) {
		t.Fatalf("timed out waiting for callbacks")
	}
	events := delivered.snapshot()
	if len(events) == 0 {
		t.Fatalf("expected callback events")
	}
	last := events[len(events)-1]
	if last.Status != "completed" || last.Stage != "done" {
		t.Fatalf("expected completed callback, got %#v", last)
	}
	if len(last.Files) != 0 {
		t.Fatalf("callback files must be empty without a real result file, got %#v", last.Files)
	}

	taskResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var taskPayload core.PublicTaskResponse
	decodeResponse(t, taskResponse, &taskPayload)
	if len(taskPayload.Files) != 0 {
		t.Fatalf("task query files must be empty without a real result file, got %#v", taskPayload.Files)
	}
}

func TestCompletedCallbackAndTaskQueryOmitFilesWhenResultMetadataMissing(t *testing.T) {
	t.Parallel()

	router, dispatcher, delivered, callbackURL := newCallbackTestFixture(t, newFakeSouthbound())
	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-missing-result-metadata",
		"instruction":         "Run once.",
		"approval_mode":       "none",
		"callback_url":        callbackURL,
		"request_id":          "req-missing-result-metadata",
		"user_id":             "user-missing-result-metadata",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+created.TaskID+"/complete", map[string]any{
		"assistant_id":             "demo-approval",
		"claim_token":              "token-1",
		"result_workspace_file_id": "missing-result-file",
	}, http.StatusOK)
	if !dispatcher.WaitIdle(time.Second) {
		t.Fatalf("timed out waiting for callbacks")
	}
	events := delivered.snapshot()
	if len(events) == 0 {
		t.Fatalf("expected callback events")
	}
	last := events[len(events)-1]
	if last.Status != "completed" || last.Stage != "done" {
		t.Fatalf("expected completed callback, got %#v", last)
	}
	if len(last.Files) != 0 {
		t.Fatalf("callback files must be empty when result file metadata is missing, got %#v", last.Files)
	}

	taskResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var taskPayload core.PublicTaskResponse
	decodeResponse(t, taskResponse, &taskPayload)
	if len(taskPayload.Files) != 0 {
		t.Fatalf("task query files must be empty when result file metadata is missing, got %#v", taskPayload.Files)
	}
	var rawTask map[string]any
	decodeResponse(t, taskResponse, &rawTask)
	if _, ok := rawTask["files"]; ok {
		t.Fatalf("task query body must omit files when result file metadata is missing: %#v", rawTask)
	}
	if strings.Contains(taskResponse.Body.String(), "missing-result-file") {
		t.Fatalf("task query body must not expose unverifiable result file id: %s", taskResponse.Body.String())
	}
}

func TestCompletedCallbackAndTaskQueryOmitFilesWhenResultMetadataLookupFails(t *testing.T) {
	t.Parallel()

	southboundFake := newFakeSouthbound()
	southboundFake.fileErrors["error-result-file"] = errors.New("workspace file metadata unavailable")
	router, dispatcher, delivered, callbackURL := newCallbackTestFixture(t, southboundFake)
	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-error-result-metadata",
		"instruction":         "Run once.",
		"approval_mode":       "none",
		"callback_url":        callbackURL,
		"request_id":          "req-error-result-metadata",
		"user_id":             "user-error-result-metadata",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+created.TaskID+"/complete", map[string]any{
		"assistant_id":             "demo-approval",
		"claim_token":              "token-1",
		"result_workspace_file_id": "error-result-file",
	}, http.StatusOK)
	if !dispatcher.WaitIdle(time.Second) {
		t.Fatalf("timed out waiting for callbacks")
	}
	events := delivered.snapshot()
	if len(events) == 0 {
		t.Fatalf("expected callback events")
	}
	last := events[len(events)-1]
	if last.Status != "completed" || last.Stage != "done" {
		t.Fatalf("expected completed callback, got %#v", last)
	}
	if len(last.Files) != 0 {
		t.Fatalf("callback files must be empty when result metadata lookup fails, got %#v", last.Files)
	}

	taskResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var taskPayload core.PublicTaskResponse
	decodeResponse(t, taskResponse, &taskPayload)
	if len(taskPayload.Files) != 0 {
		t.Fatalf("task query files must be empty when result metadata lookup fails, got %#v", taskPayload.Files)
	}
	if strings.Contains(taskResponse.Body.String(), "error-result-file") {
		t.Fatalf("task query body must not expose failed result file id: %s", taskResponse.Body.String())
	}
}

func TestCompletedCallbackAndTaskQueryRejectMismatchedResultFileMetadata(t *testing.T) {
	t.Parallel()

	southboundFake := newFakeSouthbound()
	southboundFake.files["file-1"] = southbound.RawJSON{
		"file_id":      "file-2",
		"user_id":      "user-mismatched-result-metadata",
		"filename":     "wrong.txt",
		"size":         12,
		"content_type": "text/plain",
		"download_url": "/api/v1/files/file-2/download",
	}
	router, dispatcher, delivered, callbackURL := newCallbackTestFixture(t, southboundFake)
	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-mismatched-result-metadata",
		"instruction":         "Run once.",
		"approval_mode":       "none",
		"callback_url":        callbackURL,
		"request_id":          "req-mismatched-result-metadata",
		"user_id":             "user-mismatched-result-metadata",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+created.TaskID+"/complete", map[string]any{
		"assistant_id":             "demo-approval",
		"claim_token":              "token-1",
		"result_workspace_file_id": "file-1",
	}, http.StatusOK)
	if !dispatcher.WaitIdle(time.Second) {
		t.Fatalf("timed out waiting for callbacks")
	}
	events := delivered.snapshot()
	if len(events) == 0 {
		t.Fatalf("expected callback events")
	}
	last := events[len(events)-1]
	if last.Status != "completed" || last.Stage != "done" {
		t.Fatalf("expected completed callback, got %#v", last)
	}
	if len(last.Files) != 0 {
		t.Fatalf("callback files must be empty when result metadata file_id mismatches, got %#v", last.Files)
	}

	taskResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var taskPayload core.PublicTaskResponse
	decodeResponse(t, taskResponse, &taskPayload)
	if len(taskPayload.Files) != 0 {
		t.Fatalf("task query files must be empty when result metadata file_id mismatches, got %#v", taskPayload.Files)
	}
	if strings.Contains(taskResponse.Body.String(), "file-1") || strings.Contains(taskResponse.Body.String(), "file-2") {
		t.Fatalf("task query body must not expose mismatched result file metadata: %s", taskResponse.Body.String())
	}
}

func TestCompletedCallbackEnrichesResultFileMetadataFromVariants(t *testing.T) {
	t.Parallel()

	southboundFake := newFakeSouthbound()
	southboundFake.files["file variant/1"] = southbound.RawJSON{
		"id":         "file variant/1",
		"user_id":    "user-result-metadata-variant",
		"filename":   "variant.csv",
		"size_bytes": 34,
		"mime_type":  "text/csv",
	}
	router, dispatcher, delivered, callbackURL := newCallbackTestFixture(t, southboundFake)
	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-result-metadata-variant",
		"instruction":         "Run once.",
		"approval_mode":       "none",
		"callback_url":        callbackURL,
		"request_id":          "req-result-metadata-variant",
		"user_id":             "user-result-metadata-variant",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)
	markFakeResultFileForTask(t, southboundFake, "file variant/1", created.TaskID, "demo-approval")

	performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+created.TaskID+"/complete", map[string]any{
		"assistant_id":             "demo-approval",
		"claim_token":              "token-1",
		"result_workspace_file_id": "file variant/1",
	}, http.StatusOK)
	if !dispatcher.WaitIdle(time.Second) {
		t.Fatalf("timed out waiting for callbacks")
	}
	events := delivered.snapshot()
	if len(events) == 0 {
		t.Fatalf("expected callback events")
	}
	last := events[len(events)-1]
	if len(last.Files) != 1 {
		t.Fatalf("expected completed callback file metadata, got %#v", last.Files)
	}
	file := last.Files[0]
	if file.FileID != "file variant/1" || file.Filename != "variant.csv" || file.Size != 34 || file.MimeType != "text/csv" {
		t.Fatalf("unexpected callback file metadata: %#v", file)
	}
	if file.DownloadURL != "/api/v1/files/file%20variant%2F1/download?user_id=user-result-metadata-variant" {
		t.Fatalf("expected generated callback download_url, got %#v", file)
	}

	taskResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var taskPayload core.PublicTaskResponse
	decodeResponse(t, taskResponse, &taskPayload)
	if !reflect.DeepEqual(taskPayload.Files, []core.PublicTaskFileResponse{{
		FileID:      "file variant/1",
		FileName:    "variant.csv",
		Size:        "34",
		MimeType:    "text/csv",
		DownloadURL: "/api/v1/files/file%20variant%2F1/download?user_id=user-result-metadata-variant",
	}}) {
		t.Fatalf("unexpected task query files: %#v", taskPayload.Files)
	}
}

func TestFailedCallbackAndTaskQueryOmitFiles(t *testing.T) {
	t.Parallel()

	router, dispatcher, delivered, callbackURL := newCallbackTestFixture(t, newFakeSouthbound())
	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-failed-without-files",
		"instruction":         "Run and fail.",
		"approval_mode":       "none",
		"callback_url":        callbackURL,
		"request_id":          "req-failed-without-files",
		"user_id":             "user-failed-without-files",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+created.TaskID+"/fail", map[string]any{
		"assistant_id":       "demo-approval",
		"claim_token":        "token-1",
		"failed_reason_code": "runtime_error",
		"failed_reason_text": "tool exited 1",
	}, http.StatusOK)
	if !dispatcher.WaitIdle(time.Second) {
		t.Fatalf("timed out waiting for callbacks")
	}
	events := delivered.snapshot()
	if len(events) == 0 {
		t.Fatalf("expected callback events")
	}
	last := events[len(events)-1]
	if last.Status != "failed" || last.Stage != "error" {
		t.Fatalf("expected failed callback, got %#v", last)
	}
	if len(last.Files) != 0 {
		t.Fatalf("failed callback files must be empty without a result file, got %#v", last.Files)
	}

	taskResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var taskPayload core.PublicTaskResponse
	decodeResponse(t, taskResponse, &taskPayload)
	if len(taskPayload.Files) != 0 {
		t.Fatalf("failed task query files must be empty without a result file, got %#v", taskPayload.Files)
	}
}

func TestUploadChatCallbackFilesFlowMatrix(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name           string
		finishTask     func(t *testing.T, router http.Handler, taskID string, upload publicFileMetadataResponse, fake *fakeSouthbound)
		wantStatus     string
		wantStage      string
		wantFiles      []webhook.FileRef
		wantPublicFile []core.PublicTaskFileResponse
		wantDownload   string
	}{
		{
			name: "uploaded input file is not returned when only result object is completed",
			finishTask: func(t *testing.T, router http.Handler, taskID string, _ publicFileMetadataResponse, _ *fakeSouthbound) {
				t.Helper()
				performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+taskID+"/complete", map[string]any{
					"assistant_id":     "demo-approval",
					"claim_token":      "token-1",
					"result_object_id": "object-result-only",
				}, http.StatusOK)
			},
			wantStatus: "completed",
			wantStage:  "done",
		},
		{
			name: "uploaded input file is omitted when used as result file",
			finishTask: func(t *testing.T, router http.Handler, taskID string, upload publicFileMetadataResponse, _ *fakeSouthbound) {
				t.Helper()
				performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+taskID+"/complete", map[string]any{
					"assistant_id":             "demo-approval",
					"claim_token":              "token-1",
					"result_workspace_file_id": upload.FileID,
				}, http.StatusOK)
			},
			wantStatus: "completed",
			wantStage:  "done",
		},
		{
			name: "separate result file is returned instead of uploaded input file",
			finishTask: func(t *testing.T, router http.Handler, taskID string, _ publicFileMetadataResponse, fake *fakeSouthbound) {
				t.Helper()
				fake.files["result-1"] = southbound.RawJSON{
					"file_id":      "result-1",
					"user_id":      "e2e-main-0416",
					"filename":     "result.json",
					"size_bytes":   21,
					"content_type": "application/json",
					"download_url": "/api/v1/workspace-files/result-1/download",
				}
				markFakeResultFileForTask(t, fake, "result-1", taskID, "demo-main")
				fake.fileBytes["result-1"] = []byte(`{"result":"ok"}`)
				performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+taskID+"/complete", map[string]any{
					"assistant_id":             "demo-approval",
					"claim_token":              "token-1",
					"result_workspace_file_id": "result-1",
				}, http.StatusOK)
			},
			wantStatus: "completed",
			wantStage:  "done",
			wantFiles: []webhook.FileRef{{
				FileID:      "result-1",
				Filename:    "result.json",
				Size:        21,
				MimeType:    "application/json",
				DownloadURL: "/api/v1/files/result-1/download?user_id=e2e-main-0416",
			}},
			wantPublicFile: []core.PublicTaskFileResponse{{
				FileID:      "result-1",
				FileName:    "result.json",
				Size:        "21",
				MimeType:    "application/json",
				DownloadURL: "/api/v1/files/result-1/download?user_id=e2e-main-0416",
			}},
			wantDownload: `{"result":"ok"}`,
		},
		{
			name: "missing result file metadata omits files",
			finishTask: func(t *testing.T, router http.Handler, taskID string, _ publicFileMetadataResponse, _ *fakeSouthbound) {
				t.Helper()
				performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+taskID+"/complete", map[string]any{
					"assistant_id":             "demo-approval",
					"claim_token":              "token-1",
					"result_workspace_file_id": "missing-result-file",
				}, http.StatusOK)
			},
			wantStatus: "completed",
			wantStage:  "done",
		},
		{
			name: "wrong owner result file metadata omits files",
			finishTask: func(t *testing.T, router http.Handler, taskID string, _ publicFileMetadataResponse, fake *fakeSouthbound) {
				t.Helper()
				fake.files["other-user-result"] = southbound.RawJSON{
					"file_id":      "other-user-result",
					"user_id":      "other-user",
					"filename":     "other-user.txt",
					"size_bytes":   12,
					"content_type": "text/plain",
				}
				performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+taskID+"/complete", map[string]any{
					"assistant_id":             "demo-approval",
					"claim_token":              "token-1",
					"result_workspace_file_id": "other-user-result",
				}, http.StatusOK)
			},
			wantStatus: "completed",
			wantStage:  "done",
		},
		{
			name: "other task scoped result file metadata omits files",
			finishTask: func(t *testing.T, router http.Handler, taskID string, _ publicFileMetadataResponse, fake *fakeSouthbound) {
				t.Helper()
				fake.files["other-task-result"] = southbound.RawJSON{
					"file_id":      "other-task-result",
					"user_id":      "e2e-main-0416",
					"filename":     "other-task.txt",
					"size_bytes":   18,
					"content_type": "text/plain",
					"download_url": "/api/v1/workspace-files/other-task-result/download",
				}
				markFakeResultFileForTask(t, fake, "other-task-result", "gw-other-task", "demo-main")
				performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+taskID+"/complete", map[string]any{
					"assistant_id":             "demo-approval",
					"claim_token":              "token-1",
					"result_workspace_file_id": "other-task-result",
				}, http.StatusOK)
			},
			wantStatus: "completed",
			wantStage:  "done",
		},
		{
			name: "current source ref with other task path omits files",
			finishTask: func(t *testing.T, router http.Handler, taskID string, _ publicFileMetadataResponse, fake *fakeSouthbound) {
				t.Helper()
				fake.files["current-source-other-path-result"] = southbound.RawJSON{
					"file_id":      "current-source-other-path-result",
					"user_id":      "e2e-main-0416",
					"filename":     "other-path.txt",
					"size_bytes":   15,
					"content_type": "text/plain",
					"download_url": "/api/v1/workspace-files/current-source-other-path-result/download",
				}
				markFakeResultFileForTask(t, fake, "current-source-other-path-result", taskID, "demo-main")
				fake.files["current-source-other-path-result"]["relative_path"] = "temp/go-gateway-results/gw-other-task/other-path.txt"
				performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+taskID+"/complete", map[string]any{
					"assistant_id":             "demo-approval",
					"claim_token":              "token-1",
					"result_workspace_file_id": "current-source-other-path-result",
				}, http.StatusOK)
			},
			wantStatus: "completed",
			wantStage:  "done",
		},
		{
			name: "other source ref with current task path omits files",
			finishTask: func(t *testing.T, router http.Handler, taskID string, _ publicFileMetadataResponse, fake *fakeSouthbound) {
				t.Helper()
				fake.files["other-source-current-path-result"] = southbound.RawJSON{
					"file_id":      "other-source-current-path-result",
					"user_id":      "e2e-main-0416",
					"filename":     "current-path.txt",
					"size_bytes":   17,
					"content_type": "text/plain",
					"download_url": "/api/v1/workspace-files/other-source-current-path-result/download",
				}
				markFakeResultFileForTask(t, fake, "other-source-current-path-result", taskID, "demo-main")
				fake.files["other-source-current-path-result"]["source_ref"] = "gw-other-task"
				performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+taskID+"/complete", map[string]any{
					"assistant_id":             "demo-approval",
					"claim_token":              "token-1",
					"result_workspace_file_id": "other-source-current-path-result",
				}, http.StatusOK)
			},
			wantStatus: "completed",
			wantStage:  "done",
		},
		{
			name: "wrong source type result metadata omits files",
			finishTask: func(t *testing.T, router http.Handler, taskID string, _ publicFileMetadataResponse, fake *fakeSouthbound) {
				t.Helper()
				fake.files["wrong-source-type-result"] = southbound.RawJSON{
					"file_id":      "wrong-source-type-result",
					"user_id":      "e2e-main-0416",
					"filename":     "wrong-source-type.txt",
					"size_bytes":   21,
					"content_type": "text/plain",
					"download_url": "/api/v1/workspace-files/wrong-source-type-result/download",
				}
				markFakeResultFileForTask(t, fake, "wrong-source-type-result", taskID, "demo-main")
				fake.files["wrong-source-type-result"]["source_type"] = "manual_import"
				performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+taskID+"/complete", map[string]any{
					"assistant_id":             "demo-approval",
					"claim_token":              "token-1",
					"result_workspace_file_id": "wrong-source-type-result",
				}, http.StatusOK)
			},
			wantStatus: "completed",
			wantStage:  "done",
		},
		{
			name: "nested bucket segment path omits files",
			finishTask: func(t *testing.T, router http.Handler, taskID string, _ publicFileMetadataResponse, fake *fakeSouthbound) {
				t.Helper()
				fake.files["nested-bucket-segment-result"] = southbound.RawJSON{
					"file_id":      "nested-bucket-segment-result",
					"user_id":      "e2e-main-0416",
					"filename":     "nested.txt",
					"size_bytes":   11,
					"content_type": "text/plain",
					"download_url": "/api/v1/workspace-files/nested-bucket-segment-result/download",
				}
				markFakeResultFileForTask(t, fake, "nested-bucket-segment-result", taskID, "demo-main")
				fake.files["nested-bucket-segment-result"]["source_ref"] = ""
				fake.files["nested-bucket-segment-result"]["relative_path"] = "archive/temp/go-gateway-results/" + sanitizeTaskPathComponent(taskID, 64) + "/nested.txt"
				performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+taskID+"/complete", map[string]any{
					"assistant_id":             "demo-approval",
					"claim_token":              "token-1",
					"result_workspace_file_id": "nested-bucket-segment-result",
				}, http.StatusOK)
			},
			wantStatus: "completed",
			wantStage:  "done",
		},
		{
			name: "failed task after upload chat omits files",
			finishTask: func(t *testing.T, router http.Handler, taskID string, _ publicFileMetadataResponse, _ *fakeSouthbound) {
				t.Helper()
				performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+taskID+"/fail", map[string]any{
					"assistant_id":       "demo-approval",
					"claim_token":        "token-1",
					"failed_reason_code": "runtime_error",
					"failed_reason_text": "tool exited 1",
				}, http.StatusOK)
			},
			wantStatus: "failed",
			wantStage:  "error",
		},
	}

	for _, tc := range tests {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()

			fake := newFakeSouthbound()
			router, dispatcher, delivered := newPublicCallbackFlowFixture(t, fake)
			uploaded := performPublicFileUpload(t, router, "e2e-main-0416", "input.txt", "text/plain", []byte("input file content"))
			if uploaded.FileID != "file-1" {
				t.Fatalf("upload file_id = %q, want file-1", uploaded.FileID)
			}

			chatResponse := performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
				"request_id":      "req-" + strings.ReplaceAll(tc.name, " ", "-"),
				"user_id":         "e2e-main-0416",
				"conversation_id": "conv-callback-files-flow",
				"message":         "请处理上传文件",
				"files":           []string{uploaded.FileID},
			}, http.StatusAccepted)
			var created core.ChatResponse
			decodeResponse(t, chatResponse, &created)
			if created.TaskID == "" {
				t.Fatalf("expected task_id from /api/v1/chat")
			}

			tc.finishTask(t, router, created.TaskID, uploaded, fake)
			if !dispatcher.WaitIdle(time.Second) {
				t.Fatalf("timed out waiting for callbacks")
			}
			event, ok := delivered.lastWithStatus(tc.wantStatus)
			if !ok {
				t.Fatalf("expected %s callback, got %#v", tc.wantStatus, delivered.snapshot())
			}
			if event.Stage != tc.wantStage {
				t.Fatalf("callback stage = %q, want %q", event.Stage, tc.wantStage)
			}
			if len(event.Files) == 0 {
				event.Files = nil
			}
			if !reflect.DeepEqual(event.Files, tc.wantFiles) {
				t.Fatalf("callback files = %#v, want %#v", event.Files, tc.wantFiles)
			}

			taskResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
			var taskPayload core.PublicTaskResponse
			decodeResponse(t, taskResponse, &taskPayload)
			if taskPayload.Status != tc.wantStatus || taskPayload.Stage != tc.wantStage {
				t.Fatalf("task query status/stage = %s/%s, want %s/%s", taskPayload.Status, taskPayload.Stage, tc.wantStatus, tc.wantStage)
			}
			if len(taskPayload.Files) == 0 {
				taskPayload.Files = nil
			}
			if !reflect.DeepEqual(taskPayload.Files, tc.wantPublicFile) {
				t.Fatalf("task query files = %#v, want %#v", taskPayload.Files, tc.wantPublicFile)
			}
			if tc.wantDownload != "" {
				downloadRequest := httptest.NewRequest(http.MethodGet, event.Files[0].DownloadURL, nil)
				downloadRecorder := httptest.NewRecorder()
				router.ServeHTTP(downloadRecorder, downloadRequest)
				if downloadRecorder.Code != http.StatusOK {
					t.Fatalf("callback download_url returned %d body=%s", downloadRecorder.Code, downloadRecorder.Body.String())
				}
				if downloadRecorder.Body.String() != tc.wantDownload {
					t.Fatalf("callback download body = %q, want %q", downloadRecorder.Body.String(), tc.wantDownload)
				}
			}
		})
	}
}

func TestAppendUserIDToDownloadURLOverwritesExistingScope(t *testing.T) {
	t.Parallel()

	got := appendUserIDToDownloadURL("/api/v1/files/result-1/download?disposition=attachment&user_id=other-user", "owner-user")
	want := "/api/v1/files/result-1/download?disposition=attachment&user_id=owner-user"
	if got != want {
		t.Fatalf("download URL = %q, want %q", got, want)
	}
}

func TestPublicUploadRejectsFieldsOutsideIronAgentContract(t *testing.T) {
	t.Parallel()

	service := core.NewService(newFakeSouthbound())
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	body := &bytes.Buffer{}
	writer := multipart.NewWriter(body)
	if err := writer.WriteField("user_id", "contract-user"); err != nil {
		t.Fatalf("write user_id: %v", err)
	}
	if err := writer.WriteField("assistant_id", "demo-main"); err != nil {
		t.Fatalf("write assistant_id: %v", err)
	}
	part, err := writer.CreateFormFile("file", "contract.txt")
	if err != nil {
		t.Fatalf("create form file: %v", err)
	}
	if _, err := part.Write([]byte("contract")); err != nil {
		t.Fatalf("write form file: %v", err)
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("close multipart writer: %v", err)
	}

	request := httptest.NewRequest(http.MethodPost, "/api/v1/files/upload", body)
	request.Header.Set("Content-Type", writer.FormDataContentType())
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 for unsupported form field, got %d body=%s", recorder.Code, recorder.Body.String())
	}

	var payload map[string]map[string]string
	decodeResponse(t, recorder, &payload)
	if payload["error"]["code"] != "bad_request" || payload["error"]["message"] != "不支持的表单字段: assistant_id" {
		t.Fatalf("unexpected error response: %#v", payload)
	}
}

func TestPublicUploadRejectsEmptyFile(t *testing.T) {
	t.Parallel()

	service := core.NewService(newFakeSouthbound())
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	body := &bytes.Buffer{}
	writer := multipart.NewWriter(body)
	if err := writer.WriteField("user_id", "contract-user"); err != nil {
		t.Fatalf("write user_id: %v", err)
	}
	if _, err := writer.CreateFormFile("file", "empty.txt"); err != nil {
		t.Fatalf("create form file: %v", err)
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("close multipart writer: %v", err)
	}

	request := httptest.NewRequest(http.MethodPost, "/api/v1/files/upload", body)
	request.Header.Set("Content-Type", writer.FormDataContentType())
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 for empty file, got %d body=%s", recorder.Code, recorder.Body.String())
	}

	var payload map[string]map[string]string
	decodeResponse(t, recorder, &payload)
	if payload["error"]["code"] != "bad_request" || payload["error"]["message"] != "上传文件不能为空" {
		t.Fatalf("unexpected error response: %#v", payload)
	}
}

func TestAdminProxyServesControlPlaneThroughGateway(t *testing.T) {
	t.Parallel()

	var gotPath string
	var gotQuery string
	var gotForwardedHost string
	var gotForwardedPrefix string
	var gotInternalServiceToken string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotQuery = r.URL.RawQuery
		gotForwardedHost = r.Header.Get("X-Forwarded-Host")
		gotForwardedPrefix = r.Header.Get("X-Forwarded-Prefix")
		gotInternalServiceToken = r.Header.Get("X-IronAgent-Internal-Token")
		w.Header().Set("Content-Type", "text/html")
		_, _ = w.Write([]byte("<html>admin</html>"))
	}))
	defer upstream.Close()

	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		core.NewService(newFakeSouthbound()),
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			AdminBaseURL:         upstream.URL,
			InternalServiceToken: "internal-service-token",
			ProxySessionSecret:   "proxy-session-secret",
		},
	)
	request := httptest.NewRequest(http.MethodGet, "/admin/?section=assistants", nil)
	request.Host = "gateway.example"
	request.AddCookie(&http.Cookie{
		Name:  adminProxySessionCookie,
		Value: proxySessionConfig{secret: []byte("proxy-session-secret")}.sign("admin", "admin-ui", time.Now().Add(time.Minute)),
	})
	recorder := httptest.NewRecorder()

	router.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", recorder.Code, recorder.Body.String())
	}
	if gotPath != "/" {
		t.Fatalf("expected admin proxy to strip /admin prefix, got path %q", gotPath)
	}
	if gotQuery != "section=assistants" {
		t.Fatalf("expected query to be preserved, got %q", gotQuery)
	}
	if gotForwardedHost != "gateway.example" {
		t.Fatalf("expected forwarded host to be gateway.example, got %q", gotForwardedHost)
	}
	if gotForwardedPrefix != "/admin" {
		t.Fatalf("expected forwarded prefix /admin, got %q", gotForwardedPrefix)
	}
	if gotInternalServiceToken != "internal-service-token" {
		t.Fatalf("expected admin proxy to inject internal service token, got %q", gotInternalServiceToken)
	}
	cookies := recorder.Result().Cookies()
	if len(cookies) != 0 {
		t.Fatalf("expected no new admin session cookie, got %#v", cookies)
	}
}

func TestAdminProxyRejectsAdminMarketAndWorkspaceRoutesWithoutBrowserAuth(t *testing.T) {
	t.Parallel()

	upstreamCalled := false
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamCalled = true
		writeJSON(w, http.StatusOK, map[string]string{"path": r.URL.Path})
	}))
	defer upstream.Close()

	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		core.NewService(newFakeSouthbound()),
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			AdminBaseURL:         upstream.URL,
			InternalServiceToken: "internal-service-token",
			ProxySessionSecret:   "proxy-session-secret",
		},
	)
	for _, test := range []struct {
		method string
		path   string
	}{
		{method: http.MethodGet, path: "/api/v1/admin/bootstrap/fast"},
		{method: http.MethodGet, path: "/api/v1/market/skills"},
		{method: http.MethodPost, path: "/api/v1/workspace-files/upload"},
	} {
		request := httptest.NewRequest(test.method, test.path, strings.NewReader("{}"))
		recorder := httptest.NewRecorder()
		router.ServeHTTP(recorder, request)
		if recorder.Code != http.StatusUnauthorized {
			t.Fatalf("%s %s: expected 401, got %d body=%s", test.method, test.path, recorder.Code, recorder.Body.String())
		}
	}
	if upstreamCalled {
		t.Fatalf("expected unauthenticated admin requests not to reach upstream")
	}
}

func TestAdminProxyRejectsAPIRequestsWithoutBrowserAuth(t *testing.T) {
	t.Parallel()

	upstreamCalled := false
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamCalled = true
		writeJSON(w, http.StatusOK, map[string]string{"path": r.URL.Path})
	}))
	defer upstream.Close()

	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		core.NewService(newFakeSouthbound()),
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			AdminBaseURL:         upstream.URL,
			InternalServiceToken: "internal-service-token",
			ProxySessionSecret:   "proxy-session-secret",
		},
	)

	request := httptest.NewRequest(http.MethodGet, "/api/v1/admin/bootstrap/fast", nil)
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d body=%s", recorder.Code, recorder.Body.String())
	}
	if upstreamCalled {
		t.Fatalf("expected unauthenticated admin request not to reach upstream")
	}
}

func TestAdminProxyRejectsBrowserRequestsWithoutLogin(t *testing.T) {
	t.Parallel()
	upstreamCalled := false
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamCalled = true
		_, _ = w.Write([]byte("<html>admin</html>"))
	}))
	defer upstream.Close()

	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		core.NewService(newFakeSouthbound()),
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			AdminBaseURL:         upstream.URL,
			InternalServiceToken: "internal-service-token",
			ProxySessionSecret:   "proxy-session-secret",
		},
	)

	request := httptest.NewRequest(http.MethodGet, "/admin/?section=assistants", nil)
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d body=%s", recorder.Code, recorder.Body.String())
	}
	if recorder.Header().Get("Location") != "" {
		t.Fatalf("expected no login redirect, got %q", recorder.Header().Get("Location"))
	}
	if upstreamCalled {
		t.Fatalf("expected unauthenticated browser request not to reach upstream")
	}
}

func TestAdminProxyCanDisableBrowserLogin(t *testing.T) {
	t.Parallel()

	var gotPath string
	var gotToken string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotToken = r.Header.Get("X-IronAgent-Internal-Token")
		_, _ = w.Write([]byte("<html>admin</html>"))
	}))
	defer upstream.Close()

	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		core.NewService(newFakeSouthbound()),
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			AdminBaseURL:         upstream.URL,
			AdminAuthDisabled:    true,
			InternalServiceToken: "internal-service-token",
			ProxySessionSecret:   "proxy-session-secret",
		},
	)

	request := httptest.NewRequest(http.MethodGet, "/admin/?section=assistants", nil)
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", recorder.Code, recorder.Body.String())
	}
	if gotPath != "/" {
		t.Fatalf("expected admin proxy to strip /admin prefix, got path %q", gotPath)
	}
	if gotToken != "internal-service-token" {
		t.Fatalf("expected admin proxy to inject internal service token, got %q", gotToken)
	}
}

func TestAdminLoginRedirectsWhenAdminAuthDisabled(t *testing.T) {
	t.Parallel()

	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		core.NewService(newFakeSouthbound()),
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			AdminBaseURL:         "http://admin.local",
			AdminAuthDisabled:    true,
			InternalServiceToken: "internal-service-token",
			ProxySessionSecret:   "proxy-session-secret",
		},
	)

	request := httptest.NewRequest(http.MethodGet, "/admin/login?next=%2Fadmin%2F", nil)
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusFound {
		t.Fatalf("expected 302, got %d body=%s", recorder.Code, recorder.Body.String())
	}
	if recorder.Header().Get("Location") != "/admin/" {
		t.Fatalf("expected /admin/ redirect, got %q", recorder.Header().Get("Location"))
	}
}

func TestInvalidLegacyCredentialQueryIsStrippedWithoutMintingAdminSession(t *testing.T) {
	t.Parallel()

	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		core.NewService(newFakeSouthbound()),
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			AdminBaseURL:         "http://admin.local",
			InternalServiceToken: "internal-service-token",
			ProxySessionSecret:   "proxy-session-secret",
		},
	)

	request := httptest.NewRequest(http.MethodGet, "/admin/?internalServiceToken=stale-token&section=assistants", nil)
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusFound {
		t.Fatalf("expected token-stripping redirect, got %d body=%s", recorder.Code, recorder.Body.String())
	}
	if recorder.Header().Get("Location") != "/admin/?section=assistants" {
		t.Fatalf("expected legacy token to be stripped, got %q", recorder.Header().Get("Location"))
	}
	cookies := recorder.Result().Cookies()
	if len(cookies) != 0 {
		t.Fatalf("expected no admin session cookie for invalid legacy token, got %#v", cookies)
	}
}

func TestSecurityProxyForwardsHookAndApprovalRoutesWithoutGatewayAPIAuth(t *testing.T) {
	t.Parallel()

	var paths []string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.URL.Path)
		writeJSON(w, http.StatusOK, map[string]string{"path": r.URL.Path})
	}))
	defer upstream.Close()

	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		core.NewService(newFakeSouthbound()),
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{SecurityBaseURL: upstream.URL},
	)
	for _, test := range []struct {
		method string
		path   string
	}{
		{method: http.MethodPost, path: "/api/v1/hooks/pre-execute"},
		{method: http.MethodPost, path: "/api/v1/audit/log"},
		{method: http.MethodGet, path: "/api/v1/approvals?status=pending"},
		{method: http.MethodGet, path: "/api/v1/assistants/demo-main"},
		{method: http.MethodGet, path: "/api/v1/control-ui/assistant-context"},
	} {
		request := httptest.NewRequest(test.method, test.path, strings.NewReader("{}"))
		recorder := httptest.NewRecorder()
		router.ServeHTTP(recorder, request)
		if recorder.Code != http.StatusOK {
			t.Fatalf("%s %s: expected 200, got %d body=%s", test.method, test.path, recorder.Code, recorder.Body.String())
		}
	}

	wantPaths := []string{
		"/api/v1/hooks/pre-execute",
		"/api/v1/audit/log",
		"/api/v1/approvals",
		"/api/v1/assistants/demo-main",
		"/api/v1/control-ui/assistant-context",
	}
	if strings.Join(paths, ",") != strings.Join(wantPaths, ",") {
		t.Fatalf("expected proxied paths %v, got %v", wantPaths, paths)
	}
}

func TestHealthEndpointDoesNotRequireAPIKey(t *testing.T) {
	t.Parallel()

	registry := runtimepool.NewRegistry(5)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID: "demo-approval",
		Status:      "ready",
		Capacity:    1,
	}); err != nil {
		t.Fatalf("registry heartbeat: %v", err)
	}
	southboundFake := newFakeSouthbound()
	setFakeAssistantRuntimeStatus(t, southboundFake, "demo-main", "running")
	service := core.NewService(southboundFake)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	for _, tc := range []struct {
		path          string
		wantAssistant bool
	}{
		{path: "/api/v1/health?user_id=e2e-main-0416", wantAssistant: true},
		{path: "/health", wantAssistant: false},
	} {
		request := httptest.NewRequest(http.MethodGet, tc.path, nil)
		recorder := httptest.NewRecorder()
		router.ServeHTTP(recorder, request)

		if recorder.Code != http.StatusOK {
			t.Fatalf("%s: expected 200, got %d, body=%s", tc.path, recorder.Code, recorder.Body.String())
		}
		var health struct {
			Status     string            `json:"status"`
			Components map[string]string `json:"components"`
			Assistant  *struct {
				AssistantID string `json:"assistant_id"`
				Status      string `json:"status"`
			} `json:"assistant,omitempty"`
		}
		decodeResponse(t, recorder, &health)
		if health.Status != "healthy" {
			t.Fatalf("%s: expected healthy status, got %q", tc.path, health.Status)
		}
		if len(health.Components) != 4 {
			t.Fatalf("%s: expected public health to expose only 4 components, got %#v", tc.path, health.Components)
		}
		for _, component := range []string{"gateway", "agent_runner", "container_scheduler", "file_service"} {
			if health.Components[component] != "up" {
				t.Fatalf("%s: unexpected health component %s in %#v", tc.path, component, health.Components)
			}
		}
		if tc.wantAssistant {
			if health.Assistant == nil {
				t.Fatalf("%s: expected assistant health details", tc.path)
			}
			if health.Assistant.AssistantID != "demo-main" || health.Assistant.Status != "running" {
				t.Fatalf("%s: unexpected assistant health %#v", tc.path, health.Assistant)
			}
		} else if health.Assistant != nil {
			t.Fatalf("%s: did not expect assistant health details, got %#v", tc.path, health.Assistant)
		}
		var raw map[string]any
		decodeResponse(t, recorder, &raw)
		if _, ok := raw["ready"]; ok {
			t.Fatalf("%s: public health must not expose ready: %#v", tc.path, raw)
		}
		if _, ok := raw["details"]; ok {
			t.Fatalf("%s: public health must not expose details: %#v", tc.path, raw)
		}
		for _, forbidden := range []string{"southbound_admin", "southbound_security", "task_queue"} {
			if _, ok := health.Components[forbidden]; ok {
				t.Fatalf("%s: public health must not expose %s: %#v", tc.path, forbidden, health.Components)
			}
		}
		if health.Components["gateway"] != "up" || health.Components["file_service"] != "up" {
			t.Fatalf("%s: unexpected health components %#v", tc.path, health.Components)
		}
	}
}

func TestUserHealthEndpointRequiresUserID(t *testing.T) {
	t.Parallel()

	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		core.NewService(newFakeSouthbound()),
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{},
	)

	request := httptest.NewRequest(http.MethodGet, "/api/v1/health", nil)
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d, body=%s", recorder.Code, recorder.Body.String())
	}
	var payload struct {
		Error struct {
			Code    string `json:"code"`
			Message string `json:"message"`
		} `json:"error"`
	}
	decodeResponse(t, recorder, &payload)
	if payload.Error.Code != "bad_request" || payload.Error.Message != "缺少用户标识，请传入 user_id" {
		t.Fatalf("unexpected error payload %#v", payload.Error)
	}
}

func TestUserHealthEndpointFailsWhenBoundAssistantIsNotHealthy(t *testing.T) {
	t.Parallel()

	registry := runtimepool.NewRegistry(5)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID: "demo-main",
		Status:      "ready",
		Capacity:    1,
	}); err != nil {
		t.Fatalf("registry heartbeat: %v", err)
	}
	southboundFake := newFakeSouthbound()
	setFakeAssistantRuntimeStatus(t, southboundFake, "demo-main", "stopped")
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		core.NewService(southboundFake),
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	request := httptest.NewRequest(http.MethodGet, "/api/v1/health?user_id=e2e-main-0416", nil)
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected 503, got %d, body=%s", recorder.Code, recorder.Body.String())
	}
	var health struct {
		Status     string            `json:"status"`
		Components map[string]string `json:"components"`
		Message    string            `json:"message"`
		Assistant  struct {
			AssistantID string `json:"assistant_id"`
			Status      string `json:"status"`
		} `json:"assistant"`
	}
	decodeResponse(t, recorder, &health)
	if health.Status != "unhealthy" || health.Components["agent_runner"] != "down" {
		t.Fatalf("expected unhealthy agent_runner, got status=%q components=%#v", health.Status, health.Components)
	}
	if health.Assistant.AssistantID != "demo-main" || health.Assistant.Status != "stopped" {
		t.Fatalf("unexpected assistant health %#v", health.Assistant)
	}
	if !strings.Contains(health.Message, "用户绑定助手状态异常") {
		t.Fatalf("expected user assistant health message, got %q", health.Message)
	}
}

func TestHealthEndpointAllowsDegradedRuntimeRegistry(t *testing.T) {
	t.Parallel()

	registry := runtimepool.NewRegistry(5)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID: "ready-assistant",
		Status:      "ready",
		Capacity:    1,
	}); err != nil {
		t.Fatalf("ready heartbeat: %v", err)
	}
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID: "unhealthy-assistant",
		Status:      "unhealthy",
		Capacity:    1,
		LastError:   "gateway healthz is not reachable",
	}); err != nil {
		t.Fatalf("unhealthy heartbeat: %v", err)
	}
	southboundFake := newFakeSouthbound()
	setFakeAssistantRuntimeStatus(t, southboundFake, "demo-main", "running")
	service := core.NewService(southboundFake)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	request := httptest.NewRequest(http.MethodGet, "/api/v1/health?user_id=e2e-main-0416", nil)
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d, body=%s", recorder.Code, recorder.Body.String())
	}
	var health struct {
		Status     string            `json:"status"`
		Components map[string]string `json:"components"`
		Message    string            `json:"message"`
	}
	decodeResponse(t, recorder, &health)
	if health.Status != "degraded" {
		t.Fatalf("expected degraded status, got %q", health.Status)
	}
	if health.Components["container_scheduler"] != "degraded" {
		t.Fatalf("expected degraded container_scheduler, got %#v", health.Components)
	}
	if !strings.Contains(health.Message, "registered=2 ready=1") {
		t.Fatalf("expected runtime summary message, got %q", health.Message)
	}

	request = httptest.NewRequest(http.MethodGet, "/readyz", nil)
	recorder = httptest.NewRecorder()
	router.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("expected readyz 200, got %d, body=%s", recorder.Code, recorder.Body.String())
	}
	var ready core.HealthReport
	decodeResponse(t, recorder, &ready)
	if !ready.Ready || ready.Status != "degraded" {
		t.Fatalf("expected ready degraded report, got ready=%v status=%q", ready.Ready, ready.Status)
	}
}

func TestPublicBusinessAPIFailuresWriteFormalAuditEvents(t *testing.T) {
	var mu sync.Mutex
	auditEvents := []map[string]any{}
	auditServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/audit/log" {
			t.Fatalf("expected audit append path, got %q", r.URL.Path)
		}
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatalf("decode audit payload: %v", err)
		}
		mu.Lock()
		auditEvents = append(auditEvents, payload)
		mu.Unlock()
		writeJSON(w, http.StatusOK, map[string]any{"status": "ok"})
	}))
	defer auditServer.Close()

	southboundFake := newFakeSouthbound()
	southboundFake.health = southbound.HealthSummary{
		Components: []southbound.HealthComponent{
			{Name: "southbound_security", Status: "down", Error: "connection refused"},
			{Name: "southbound_admin", Status: "up", StatusCode: http.StatusOK},
		},
		CheckedAt: time.Now().UTC(),
	}
	baseService := core.NewService(southboundFake)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		publicFailureApprovalService{Service: baseService},
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
		RouterOptions{SecurityBaseURL: auditServer.URL},
	)

	performPublicJSONRequest(t, router, http.MethodGet, "/api/v1/health?user_id=e2e-main-0416", nil, http.StatusServiceUnavailable)
	performPublicUploadFailure(t, router)
	performPublicJSONRequest(t, router, http.MethodGet, "/api/v1/files/file-1", nil, http.StatusBadRequest)
	performPublicJSONRequest(t, router, http.MethodGet, "/api/v1/files/file-1/download", nil, http.StatusBadRequest)
	performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"user_id":         "e2e-main-0416",
		"conversation_id": "conv-audit-failure",
		"message":         "hello",
	}, http.StatusBadRequest)
	performPublicJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/missing-task", nil, http.StatusBadRequest)
	performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/missing-task/cancel", map[string]any{
		"unexpected": true,
	}, http.StatusBadRequest)
	performPublicJSONRequest(t, router, http.MethodGet, "/api/v1/approvals/missing-approval", nil, http.StatusBadRequest)
	performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/approvals/missing-approval/decision", map[string]any{
		"decision": "maybe",
	}, http.StatusBadRequest)

	wantActions := map[string]bool{
		"public_api.health":             false,
		"public_api.files.upload":       false,
		"public_api.files.get":          false,
		"public_api.files.download":     false,
		"public_api.chat":               false,
		"public_api.tasks.get":          false,
		"public_api.tasks.cancel":       false,
		"public_api.approvals.get":      false,
		"public_api.approvals.decision": false,
	}
	mu.Lock()
	defer mu.Unlock()
	if len(auditEvents) != len(wantActions) {
		t.Fatalf("expected %d audit events, got %d: %#v", len(wantActions), len(auditEvents), auditEvents)
	}
	for _, event := range auditEvents {
		tool := event["tool"].(map[string]any)
		action := tool["name"].(string)
		if _, ok := wantActions[action]; !ok {
			t.Fatalf("unexpected audit action %q in %#v", action, event)
		}
		wantActions[action] = true
		envelope := event["envelope"].(map[string]any)
		if !strings.HasPrefix(envelope["request_id"].(string), "public-api-failure-") {
			t.Fatalf("expected generated public API audit request id, got %#v", envelope)
		}
		result := event["result"].(map[string]any)
		if result["success"] != false {
			t.Fatalf("expected failed audit result, got %#v", result)
		}
		metadata := event["metadata"].(map[string]any)
		if metadata["origin"] != "go_gateway_public_api" || metadata["error_code"] == "" {
			t.Fatalf("expected public API audit metadata, got %#v", metadata)
		}
		args := tool["args"].(map[string]any)
		if args["status_code"].(float64) < 400 {
			t.Fatalf("expected failed status code in audit args, got %#v", args)
		}
	}
	for action, seen := range wantActions {
		if !seen {
			t.Fatalf("missing audit action %q in %#v", action, auditEvents)
		}
	}
}

func TestReadinessFailsWhenSouthboundIsDown(t *testing.T) {
	t.Parallel()

	southboundFake := newFakeSouthbound()
	southboundFake.health = southbound.HealthSummary{
		Components: []southbound.HealthComponent{
			{Name: "southbound_security", Status: "down", Error: "connection refused"},
			{Name: "southbound_admin", Status: "up", StatusCode: http.StatusOK},
		},
		CheckedAt: time.Now().UTC(),
	}
	service := core.NewService(southboundFake)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	request := httptest.NewRequest(http.MethodGet, "/readyz", nil)
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected 503, got %d, body=%s", recorder.Code, recorder.Body.String())
	}
	var health core.HealthReport
	decodeResponse(t, recorder, &health)
	if health.Ready {
		t.Fatalf("expected not ready health report")
	}
	if health.Components["southbound_security"] != "down" || health.Components["file_service"] != "down" {
		t.Fatalf("unexpected health components %#v", health.Components)
	}
}

func TestReadinessFailsWhenNoRuntimeIsReady(t *testing.T) {
	t.Parallel()

	registry := runtimepool.NewRegistryWithStore(1, time.Millisecond, nil, nil)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID: "demo-approval",
		Status:      "running",
		Capacity:    1,
	}); err != nil {
		t.Fatalf("heartbeat runtime: %v", err)
	}
	time.Sleep(2 * time.Millisecond)

	service := core.NewServiceWithScheduler(newFakeSouthbound(), registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	request := httptest.NewRequest(http.MethodGet, "/readyz", nil)
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected 503, got %d, body=%s", recorder.Code, recorder.Body.String())
	}
	var health core.HealthReport
	decodeResponse(t, recorder, &health)
	if health.Ready {
		t.Fatalf("expected not ready health report")
	}
	if health.Components["container_scheduler"] != "down" {
		t.Fatalf("expected down container_scheduler, got %#v", health.Components)
	}
}

func TestReadinessReportsRuntimeSnapshotFailure(t *testing.T) {
	t.Parallel()

	registry := runtimepool.NewRegistryWithStore(1, time.Minute, failingRuntimeStore{}, nil)
	service := core.NewServiceWithScheduler(newFakeSouthbound(), registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	request := httptest.NewRequest(http.MethodGet, "/readyz", nil)
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected 503, got %d, body=%s", recorder.Code, recorder.Body.String())
	}
	var health core.HealthReport
	decodeResponse(t, recorder, &health)
	if health.Ready {
		t.Fatalf("expected not ready health report")
	}
	if health.Components["container_scheduler"] != "down" {
		t.Fatalf("expected down container_scheduler, got %#v", health.Components)
	}
	if health.Details["container_scheduler"].Message != "runtime registry snapshot failed" {
		t.Fatalf("expected snapshot failure detail, got %#v", health.Details["container_scheduler"])
	}
}

func TestPhaseOneStartAndCancelClosure(t *testing.T) {
	t.Parallel()

	service := core.NewService(newFakeSouthbound())
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-2",
		"instruction":         "Run the task.",
		"approval_mode":       "none",
		"created_by":          "runtime-test",
	}, http.StatusAccepted)

	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)
	if created.Status != "processing" {
		t.Fatalf("expected processing, got %q", created.Status)
	}
	if created.Stage != "tool_running" {
		t.Fatalf("expected tool_running, got %q", created.Stage)
	}

	getResponse := performJSONRequest(
		t,
		router,
		http.MethodGet,
		"/api/v1/tasks/"+created.TaskID,
		nil,
		http.StatusOK,
	)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if fetched.Status != "processing" {
		t.Fatalf("expected processing after chat, got %q", fetched.Status)
	}
	if fetched.Stage != "tool_running" {
		t.Fatalf("expected tool_running after chat, got %q", fetched.Stage)
	}

	cancelResponse := performJSONRequest(
		t,
		router,
		http.MethodPost,
		"/api/v1/tasks/"+created.TaskID+"/cancel",
		map[string]any{"actor_id": "runtime-test", "reason": "cleanup"},
		http.StatusOK,
	)
	var cancelled struct {
		TaskID      string    `json:"task_id"`
		Status      string    `json:"status"`
		CancelledAt time.Time `json:"cancelled_at"`
	}
	decodeResponse(t, cancelResponse, &cancelled)
	if cancelled.Status != "cancelled" {
		t.Fatalf("expected cancelled after cancel, got %q", cancelled.Status)
	}
	if cancelled.TaskID != created.TaskID || cancelled.CancelledAt.IsZero() {
		t.Fatalf("unexpected cancel response: %#v", cancelled)
	}
}

func TestChatResolvesBindingCreatesObjectAndStartsTask(t *testing.T) {
	t.Parallel()

	southboundFake := newFakeSouthbound()
	service := core.NewService(southboundFake)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"user_id":             "e2e-main-0416",
		"target_assistant_id": "demo-approval",
		"instruction":         "Run the task with a workspace file.",
		"approval_mode":       "none",
		"workspace_file": map[string]any{
			"file_id":      "file-1",
			"filename":     "input.txt",
			"object_type":  "document",
			"content_type": "text/plain",
			"size_bytes":   12,
			"sha256":       "demo-sha256",
		},
	}, http.StatusAccepted)

	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)
	if created.Status != "processing" {
		t.Fatalf("expected processing, got %q", created.Status)
	}
	if created.Stage != "tool_running" {
		t.Fatalf("expected tool_running, got %q", created.Stage)
	}

	stored := southboundFake.tasks[created.TaskID]
	if stored.SourceAssistantID != "external-default" {
		t.Fatalf("expected external source assistant, got %q", stored.SourceAssistantID)
	}
	if stored.TargetAssistantID != "demo-approval" {
		t.Fatalf("expected target assistant demo-approval, got %q", stored.TargetAssistantID)
	}
	if stored.InputObjectID != "object-from-file" {
		t.Fatalf("expected object-from-file, got %q", stored.InputObjectID)
	}
	if stored.Status != "running" {
		t.Fatalf("expected running task, got %q", stored.Status)
	}
}

func TestExternalChatContractUsesRequestIDAndFiles(t *testing.T) {
	t.Parallel()

	registry := runtimepool.NewRegistry(2)
	if _, err := registry.Upsert(runtimepool.RuntimeRecord{
		AssistantID:     "demo-main",
		InternalBaseURL: "http://demo-main:18789",
		Status:          "running",
		Capacity:        2,
	}); err != nil {
		t.Fatalf("upsert source runtime: %v", err)
	}
	if _, err := registry.Upsert(runtimepool.RuntimeRecord{
		AssistantID:     "demo-approval",
		InternalBaseURL: "http://demo-approval:18789",
		Status:          "running",
		Capacity:        2,
	}); err != nil {
		t.Fatalf("upsert target runtime: %v", err)
	}

	southboundFake := newFakeSouthbound()
	service := core.NewServiceWithOptions(southboundFake, registry, taskstore.NewMemoryStore())
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	payload := map[string]any{
		"request_id":      "req-ironagent-001",
		"conversation_id": "conv-ironagent-001",
		"user_id":         "e2e-main-0416",
		"message":         "请总结这个文件",
		"files":           []string{"file-ironagent-001"},
	}
	firstResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", payload, http.StatusAccepted)
	secondResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", payload, http.StatusAccepted)

	var first core.ChatResponse
	var second core.ChatResponse
	decodeResponse(t, firstResponse, &first)
	decodeResponse(t, secondResponse, &second)
	if first.TaskID == "" || first.TaskID != second.TaskID {
		t.Fatalf("expected request_id idempotency to return same task_id, got %q and %q", first.TaskID, second.TaskID)
	}
	if first.CreatedAt.IsZero() {
		t.Fatalf("expected created_at in chat response")
	}
	if southboundFake.nextTaskID != 1 {
		t.Fatalf("expected one southbound task, got %d", southboundFake.nextTaskID)
	}
	stored := southboundFake.tasks[first.TaskID]
	if stored.SourceAssistantID != "external-default" || stored.TargetAssistantID != "demo-main" {
		t.Fatalf("unexpected routing source=%q target=%q", stored.SourceAssistantID, stored.TargetAssistantID)
	}
	if stored.InputObjectID != "object-from-file" {
		t.Fatalf("expected files[] to create object-from-file, got %q", stored.InputObjectID)
	}
}

func TestExternalChatRequiresExistingUserAssistantBinding(t *testing.T) {
	t.Parallel()

	southboundFake := newFakeSouthbound()
	service := core.NewService(southboundFake)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":      "req-auto-user-assistant",
		"conversation_id": "conv-auto-user-assistant",
		"user_id":         "fresh-user-001",
		"message":         "HELLO",
		"approval_mode":   "none",
	}, http.StatusBadRequest)

	var payload map[string]map[string]string
	decodeResponse(t, createResponse, &payload)
	if payload["error"]["code"] != "bad_request" {
		t.Fatalf("unexpected error response: %#v", payload)
	}
	if payload["error"]["message"] != "用户无可用助手，请在后台重新创建助手后再继续调用" {
		t.Fatalf("unexpected error message: %#v", payload)
	}
}

func TestExternalChatRejectsSoftDeletedDefaultAssistant(t *testing.T) {
	t.Parallel()

	southboundFake := newFakeSouthbound()
	assistant := southboundFake.assistants["demo-main"]
	assistant.DeletedAt = sbTimePtr(time.Now().UTC())
	assistant.Deleted = true
	southboundFake.assistants["demo-main"] = assistant
	service := core.NewService(southboundFake)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	createResponse := performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":      "req-soft-deleted-assistant",
		"conversation_id": "conv-soft-deleted-assistant",
		"user_id":         "e2e-main-0416",
		"message":         "HELLO",
	}, http.StatusBadRequest)

	var payload map[string]map[string]string
	decodeResponse(t, createResponse, &payload)
	if payload["error"]["code"] != "bad_request" {
		t.Fatalf("unexpected error response: %#v", payload)
	}
	if payload["error"]["message"] != "用户无可用助手，请在后台重新创建助手后再继续调用" {
		t.Fatalf("unexpected error message: %#v", payload)
	}
}

func TestExternalChatContractAcceptsPureMessageWithDefaultCallback(t *testing.T) {
	t.Parallel()

	delivered := &eventCollector{}
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		event, err := decodeCallbackEventFromRequest(r)
		if err != nil {
			t.Errorf("decode callback event: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		delivered.add(event)
		writeJSON(w, http.StatusOK, map[string]any{
			"received": true,
			"taskId":   event.TaskID,
			"handled":  true,
		})
	}))
	defer callback.Close()

	registry := runtimepool.NewRegistry(2)
	if _, err := registry.Upsert(runtimepool.RuntimeRecord{
		AssistantID:     "demo-main",
		InternalBaseURL: "http://demo-main:18789",
		Status:          "running",
		Capacity:        2,
	}); err != nil {
		t.Fatalf("upsert source runtime: %v", err)
	}
	if _, err := registry.Upsert(runtimepool.RuntimeRecord{
		AssistantID:     "demo-approval",
		InternalBaseURL: "http://demo-approval:18789",
		Status:          "running",
		Capacity:        2,
	}); err != nil {
		t.Fatalf("upsert target runtime: %v", err)
	}

	southboundFake := newFakeSouthbound()
	dispatcher := webhook.NewDispatcher(webhook.Config{
		Secret:      "callback-secret",
		MaxAttempts: 1,
		RetryDelay:  time.Millisecond,
	})
	service := core.NewServiceWithOptions(southboundFake, registry, taskstore.NewMemoryStore())
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			Webhooks:             dispatcher,
			RuntimeRegistry:      registry,
			PublicBaseURL:        "http://gateway.local",
			DefaultCallbackURL:   callback.URL,
			CallbackAllowedHosts: []string{"127.0.0.1"},
			CallbackAllowPrivate: true,
		},
	)

	response := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":      "req-message-only",
		"conversation_id": "conv-message-only",
		"user_id":         "e2e-main-0416",
		"message":         "只根据这条消息执行",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, response, &created)
	if created.TaskID == "" || created.Status != "processing" {
		t.Fatalf("unexpected chat response: %#v", created)
	}
	stored := southboundFake.tasks[created.TaskID]
	if stored.InputObjectID != "object-from-file" || stored.Instruction != "只根据这条消息执行" {
		t.Fatalf("expected pure message to create shared object and instruction, got %#v", stored)
	}
	if !dispatcher.WaitIdle(time.Second) {
		t.Fatalf("timed out waiting for default callback delivery")
	}
	events := delivered.snapshot()
	if len(events) != 2 || events[0].Status != "processing" || events[0].Stage != "queued" || events[1].Status != "processing" || events[1].Stage != "tool_running" {
		t.Fatalf("unexpected default callback events: %#v", events)
	}
}

func TestPublicChatFailsClosedWhenCallbackRequiredWithoutDefaultURL(t *testing.T) {
	t.Parallel()

	southboundFake := newFakeSouthbound()
	service := core.NewService(southboundFake)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
		RouterOptions{
			Webhooks:         webhook.NewDispatcher(webhook.Config{Secret: "callback-secret"}),
			CallbackRequired: true,
		},
	)

	response := performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":      "req-callback-required",
		"conversation_id": "conv-callback-required",
		"user_id":         "e2e-main-0416",
		"message":         "需要 callback",
	}, http.StatusServiceUnavailable)
	var payload struct {
		Error struct {
			Code      string `json:"code"`
			Message   string `json:"message"`
			Retryable bool   `json:"retryable"`
		} `json:"error"`
	}
	decodeResponse(t, response, &payload)
	if payload.Error.Code != "callback_unavailable" || !payload.Error.Retryable {
		t.Fatalf("unexpected error payload %#v", payload.Error)
	}
	if southboundFake.nextTaskID != 0 {
		t.Fatalf("expected no task to be created, got nextTaskID=%d", southboundFake.nextTaskID)
	}

	request := httptest.NewRequest(http.MethodGet, "/readyz", nil)
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected readyz 503, got %d body=%s", recorder.Code, recorder.Body.String())
	}
	var health core.HealthReport
	decodeResponse(t, recorder, &health)
	if health.Components["callback"] != "down" {
		t.Fatalf("expected callback down, got %#v", health.Components)
	}
	callbackDetail := health.Details["callback"].Message
	for _, want := range []string{"required=true", "default_url=empty", "dispatcher=configured"} {
		if !strings.Contains(callbackDetail, want) {
			t.Fatalf("callback detail %q missing %q", callbackDetail, want)
		}
	}
}

func TestChatIdempotencyReplaysStoredTask(t *testing.T) {
	t.Parallel()

	southboundFake := newFakeSouthbound()
	service := core.NewServiceWithTaskStore(southboundFake, taskstore.NewMemoryStore())
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	payload := map[string]any{
		"client_id":           "xiaoxiongmao",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-idempotent",
		"instruction":         "Run the task once.",
		"approval_mode":       "none",
		"idempotency_key":     "idem-1",
	}
	firstResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", payload, http.StatusAccepted)
	secondResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", payload, http.StatusAccepted)

	var first core.ChatResponse
	var second core.ChatResponse
	decodeResponse(t, firstResponse, &first)
	decodeResponse(t, secondResponse, &second)
	if first.TaskID != second.TaskID {
		t.Fatalf("expected same task_id for idempotent replay, got %q and %q", first.TaskID, second.TaskID)
	}
	if southboundFake.nextTaskID != 1 {
		t.Fatalf("expected one southbound task, got %d", southboundFake.nextTaskID)
	}
}

func TestFallbackURLIgnoresForwardedHeadersWithoutPublicBaseURL(t *testing.T) {
	t.Parallel()

	service := core.NewService(newFakeSouthbound())
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	payload := map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-forwarded-host",
		"instruction":         "Run the task.",
		"approval_mode":       "none",
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal payload: %v", err)
	}
	request := httptest.NewRequest(http.MethodPost, "http://gateway.internal/api/v1/chat", bytes.NewReader(raw))
	request.Host = "gateway.internal"
	request.Header.Set("X-API-Key", "dev-key")
	request.Header.Set("Accept", "application/json")
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("X-Forwarded-Proto", "https")
	request.Header.Set("X-Forwarded-Host", "evil.example")

	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusAccepted {
		t.Fatalf("expected 202, got %d, body=%s", recorder.Code, recorder.Body.String())
	}
	var created core.ChatResponse
	decodeResponse(t, recorder, &created)
	if created.FallbackQueryURL != "" {
		t.Fatalf("chat response must not expose fallback_query_url, got %q", created.FallbackQueryURL)
	}
}

func TestChatIdempotencyRejectsDifferentPayload(t *testing.T) {
	t.Parallel()

	southboundFake := newFakeSouthbound()
	service := core.NewServiceWithTaskStore(southboundFake, taskstore.NewMemoryStore())
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	payload := map[string]any{
		"client_id":           "xiaoxiongmao",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-idempotent",
		"instruction":         "Run the task once.",
		"approval_mode":       "none",
		"idempotency_key":     "idem-2",
	}
	performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", payload, http.StatusAccepted)

	payload["instruction"] = "Run a different task."
	performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", payload, http.StatusBadRequest)
	if southboundFake.nextTaskID != 1 {
		t.Fatalf("expected no duplicate southbound task, got %d", southboundFake.nextTaskID)
	}
}

func TestTaskQueryFallsBackToGatewayStore(t *testing.T) {
	t.Parallel()

	southboundFake := newFakeSouthbound()
	service := core.NewServiceWithTaskStore(southboundFake, taskstore.NewMemoryStore())
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"client_id":           "xiaoxiongmao",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-cached",
		"instruction":         "Run the cached task.",
		"approval_mode":       "none",
		"idempotency_key":     "idem-cache",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	delete(southboundFake.tasks, created.TaskID)
	getResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if fetched.TaskID != created.TaskID {
		t.Fatalf("expected cached task_id %q, got %q", created.TaskID, fetched.TaskID)
	}
	if fetched.Status != "processing" {
		t.Fatalf("expected cached processing status, got %q", fetched.Status)
	}
}

func TestPhaseOneFileBridge(t *testing.T) {
	t.Parallel()

	service := core.NewService(newFakeSouthbound())
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	uploadBody := &bytes.Buffer{}
	writer := multipart.NewWriter(uploadBody)
	_ = writer.WriteField("assistant_id", "demo-main")
	_ = writer.WriteField("user_id", "bridge-user")
	_ = writer.WriteField("target_bucket", "temp")
	_ = writer.WriteField("source_type", "bridge-test")
	part, err := writer.CreateFormFile("file", "hello.txt")
	if err != nil {
		t.Fatalf("create form file: %v", err)
	}
	if _, err := part.Write([]byte("hello bridge")); err != nil {
		t.Fatalf("write form file: %v", err)
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("close multipart writer: %v", err)
	}

	request := httptest.NewRequest(http.MethodPost, "/api/v1/files/upload", uploadBody)
	request.Header.Set("X-API-Key", "dev-key")
	request.Header.Set("Content-Type", writer.FormDataContentType())
	request.Header.Set("Accept", "application/json")

	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d, body=%s", recorder.Code, recorder.Body.String())
	}

	listResponse := performJSONRequest(
		t,
		router,
		http.MethodGet,
		"/api/v1/files?assistant_id=demo-main",
		nil,
		http.StatusOK,
	)
	var listed map[string]any
	decodeResponse(t, listResponse, &listed)
	if listed["total"].(float64) != 1 {
		t.Fatalf("expected total=1, got %#v", listed["total"])
	}
	files := listed["files"].([]any)
	listedFile := files[0].(map[string]any)
	if listedFile["assistant_id"] != "demo-main" {
		t.Fatalf("expected assistant_id demo-main, got %#v", listedFile["assistant_id"])
	}
	if listedFile["bucket"] != "temp" {
		t.Fatalf("expected bucket temp, got %#v", listedFile["bucket"])
	}
	if listedFile["relative_path"] != "temp/hello.txt" {
		t.Fatalf("expected relative_path temp/hello.txt, got %#v", listedFile["relative_path"])
	}
	if listedFile["download_url"] != "/api/v1/files/file-1/download" {
		t.Fatalf("expected gateway download URL, got %#v", listedFile["download_url"])
	}

	getResponse := performJSONRequest(
		t,
		router,
		http.MethodGet,
		"/api/v1/files/file-1?user_id=bridge-user",
		nil,
		http.StatusOK,
	)
	var file map[string]any
	decodeResponse(t, getResponse, &file)
	if file["file_id"] != "file-1" {
		t.Fatalf("expected file-1, got %#v", file["file_id"])
	}

	downloadRequest := httptest.NewRequest(http.MethodGet, "/api/v1/files/file-1/download?user_id=bridge-user", nil)
	downloadRequest.Header.Set("X-API-Key", "dev-key")
	downloadRecorder := httptest.NewRecorder()
	router.ServeHTTP(downloadRecorder, downloadRequest)
	if downloadRecorder.Code != http.StatusOK {
		t.Fatalf("expected 200 download, got %d", downloadRecorder.Code)
	}
	if downloadRecorder.Body.String() != "hello bridge" {
		t.Fatalf("unexpected download body %q", downloadRecorder.Body.String())
	}
	if downloadRecorder.Header().Get("X-IronAgent-Download-Source") != "object_store" {
		t.Fatalf(
			"expected download source header, got %q",
			downloadRecorder.Header().Get("X-IronAgent-Download-Source"),
		)
	}
	if downloadRecorder.Header().Get("X-IronAgent-Storage-Backend") != "cos" {
		t.Fatalf(
			"expected storage backend header, got %q",
			downloadRecorder.Header().Get("X-IronAgent-Storage-Backend"),
		)
	}

	deleteResponse := performJSONRequest(
		t,
		router,
		http.MethodPost,
		"/api/v1/files/file-1/delete",
		map[string]any{"user_id": "bridge-user"},
		http.StatusOK,
	)
	var deleted map[string]any
	decodeResponse(t, deleteResponse, &deleted)
	if deleted["status"] != "deleted" {
		t.Fatalf("expected deleted status, got %#v", deleted["status"])
	}
}

func TestFileSyncResultsForwardsTaskScope(t *testing.T) {
	t.Parallel()

	southboundFake := newFakeSouthbound()
	service := core.NewService(southboundFake)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)
	changedAfter := time.Date(2026, 6, 1, 1, 2, 3, 456789000, time.UTC)

	response := performJSONRequest(t, router, http.MethodPost, "/api/v1/files/sync-results", map[string]any{
		"assistant_id":       "demo-main",
		"bucket":             "temp",
		"user_id":            "user-001",
		"changed_after":      changedAfter.Format(time.RFC3339Nano),
		"result_path_prefix": "go-gateway-results/gw-task-001/",
		"task_id":            "gw-task-001",
		"source_ref":         "gw-task-001",
	}, http.StatusOK)
	var summary southbound.WorkspaceScanSummary
	decodeResponse(t, response, &summary)
	if summary.FilesSynced != 1 {
		t.Fatalf("files_synced = %d, want 1", summary.FilesSynced)
	}
	if len(southboundFake.syncRequests) != 1 {
		t.Fatalf("sync request count = %d, want 1", len(southboundFake.syncRequests))
	}
	got := southboundFake.syncRequests[0]
	if got.AssistantID != "demo-main" ||
		got.Bucket != "temp" ||
		got.UserID != "user-001" ||
		!got.ChangedAfter.Equal(changedAfter) ||
		got.ResultPathPrefix != "go-gateway-results/gw-task-001/" ||
		got.TaskID != "gw-task-001" ||
		got.SourceRef != "gw-task-001" {
		t.Fatalf("sync request = %#v", got)
	}
}

func TestDirectFileUploadRequiresExistingUserAssistantBinding(t *testing.T) {
	t.Parallel()

	southboundFake := newFakeSouthbound()
	service := core.NewService(southboundFake)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	uploadBody := &bytes.Buffer{}
	writer := multipart.NewWriter(uploadBody)
	_ = writer.WriteField("user_id", "fresh-upload-user")
	part, err := writer.CreateFormFile("file", "contract.txt")
	if err != nil {
		t.Fatalf("create form file: %v", err)
	}
	if _, err := part.Write([]byte("upload through user_id")); err != nil {
		t.Fatalf("write form file: %v", err)
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("close multipart writer: %v", err)
	}

	request := httptest.NewRequest(http.MethodPost, "/api/v1/files/upload", uploadBody)
	request.Header.Set("Content-Type", writer.FormDataContentType())
	request.Header.Set("Accept", "application/json")

	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d, body=%s", recorder.Code, recorder.Body.String())
	}

	var payload map[string]map[string]string
	decodeResponse(t, recorder, &payload)
	if payload["error"]["code"] != "bad_request" {
		t.Fatalf("unexpected error response: %#v", payload)
	}
	if payload["error"]["message"] != "用户无可用助手，请在后台重新创建助手后再继续调用" {
		t.Fatalf("unexpected error message: %#v", payload)
	}
}

func TestDirectFileUploadRejectsSoftDeletedDefaultAssistant(t *testing.T) {
	t.Parallel()

	southboundFake := newFakeSouthbound()
	assistant := southboundFake.assistants["demo-main"]
	assistant.DeletedAt = sbTimePtr(time.Now().UTC())
	assistant.Deleted = true
	southboundFake.assistants["demo-main"] = assistant
	service := core.NewService(southboundFake)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	uploadBody := &bytes.Buffer{}
	writer := multipart.NewWriter(uploadBody)
	_ = writer.WriteField("user_id", "e2e-main-0416")
	part, err := writer.CreateFormFile("file", "contract.txt")
	if err != nil {
		t.Fatalf("create form file: %v", err)
	}
	if _, err := part.Write([]byte("upload through user_id")); err != nil {
		t.Fatalf("write form file: %v", err)
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("close multipart writer: %v", err)
	}

	request := httptest.NewRequest(http.MethodPost, "/api/v1/files/upload", uploadBody)
	request.Header.Set("Content-Type", writer.FormDataContentType())
	request.Header.Set("Accept", "application/json")

	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d, body=%s", recorder.Code, recorder.Body.String())
	}
	var payload map[string]map[string]string
	decodeResponse(t, recorder, &payload)
	if payload["error"]["code"] != "bad_request" {
		t.Fatalf("unexpected error response: %#v", payload)
	}
	if payload["error"]["message"] != "用户无可用助手，请在后台重新创建助手后再继续调用" {
		t.Fatalf("unexpected error message: %#v", payload)
	}
}

func TestDirectFileUploadMetadataPreservesMimeType(t *testing.T) {
	t.Parallel()

	southboundFake := newFakeSouthbound()
	service := core.NewService(southboundFake)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	uploadBody := &bytes.Buffer{}
	writer := multipart.NewWriter(uploadBody)
	_ = writer.WriteField("user_id", "e2e-main-0416")
	part, err := createTestUploadPart(writer, "contract.txt", "text/plain")
	if err != nil {
		t.Fatalf("create form file: %v", err)
	}
	if _, err := part.Write([]byte("upload through user_id")); err != nil {
		t.Fatalf("write form file: %v", err)
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("close multipart writer: %v", err)
	}

	request := httptest.NewRequest(http.MethodPost, "/api/v1/files/upload", uploadBody)
	request.Header.Set("Content-Type", writer.FormDataContentType())
	request.Header.Set("Accept", "application/json")

	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d, body=%s", recorder.Code, recorder.Body.String())
	}
	var uploaded publicFileMetadataResponse
	decodeResponse(t, recorder, &uploaded)
	if uploaded.MimeType != "text/plain" {
		t.Fatalf("expected upload mime_type text/plain, got %#v", uploaded)
	}

	getRequest := httptest.NewRequest(http.MethodGet, "/api/v1/files/file-1?user_id=e2e-main-0416", nil)
	getRequest.Header.Set("Accept", "application/json")
	getRecorder := httptest.NewRecorder()
	router.ServeHTTP(getRecorder, getRequest)
	if getRecorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d, body=%s", getRecorder.Code, getRecorder.Body.String())
	}
	var metadata publicFileMetadataResponse
	decodeResponse(t, getRecorder, &metadata)
	if metadata.MimeType != uploaded.MimeType {
		t.Fatalf("expected metadata mime_type %q, got %#v", uploaded.MimeType, metadata)
	}
}

func TestFileGetAndDownloadRequireMatchingUserID(t *testing.T) {
	t.Parallel()

	southboundFake := newFakeSouthbound()
	southboundFake.files["owned-file"] = southbound.RawJSON{
		"file_id":      "owned-file",
		"user_id":      "owner-user",
		"filename":     "owned.txt",
		"content_type": "text/plain",
	}
	southboundFake.files["audit-only-file"] = southbound.RawJSON{
		"file_id":          "audit-only-file",
		"uploader_user_id": "owner-user",
		"filename":         "audit-only.txt",
		"content_type":     "text/plain",
	}
	southboundFake.fileBytes["owned-file"] = []byte("owned content")
	service := core.NewService(southboundFake)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	performJSONRequest(t, router, http.MethodGet, "/api/v1/files/owned-file", nil, http.StatusBadRequest)
	performJSONRequest(t, router, http.MethodGet, "/api/v1/files/owned-file?user_id=other-user", nil, http.StatusForbidden)
	performJSONRequest(t, router, http.MethodGet, "/api/v1/files/audit-only-file?user_id=owner-user", nil, http.StatusForbidden)

	response := performJSONRequest(t, router, http.MethodGet, "/api/v1/files/owned-file?user_id=owner-user", nil, http.StatusOK)
	var file fileMetadataResponse
	decodeResponse(t, response, &file)
	if file.FileID != "owned-file" {
		t.Fatalf("expected owned-file, got %#v", file)
	}

	downloadRequest := httptest.NewRequest(http.MethodGet, "/api/v1/files/owned-file/download?user_id=owner-user", nil)
	downloadRequest.Header.Set("X-API-Key", "dev-key")
	downloadRecorder := httptest.NewRecorder()
	router.ServeHTTP(downloadRecorder, downloadRequest)
	if downloadRecorder.Code != http.StatusOK || downloadRecorder.Body.String() != "owned content" {
		t.Fatalf("unexpected download response code=%d body=%q", downloadRecorder.Code, downloadRecorder.Body.String())
	}
}

func TestDirectFileUploadRejectsOversizedBody(t *testing.T) {
	t.Parallel()

	service := core.NewService(newFakeSouthbound())
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{MaxUploadBytes: 5},
	)

	uploadBody := &bytes.Buffer{}
	writer := multipart.NewWriter(uploadBody)
	_ = writer.WriteField("assistant_id", "demo-main")
	_ = writer.WriteField("user_id", "bridge-user")
	part, err := writer.CreateFormFile("file", "too-large.txt")
	if err != nil {
		t.Fatalf("create form file: %v", err)
	}
	if _, err := part.Write([]byte("too large")); err != nil {
		t.Fatalf("write form file: %v", err)
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("close multipart writer: %v", err)
	}

	request := httptest.NewRequest(http.MethodPost, "/api/v1/files/upload", uploadBody)
	request.Header.Set("X-API-Key", "dev-key")
	request.Header.Set("Content-Type", writer.FormDataContentType())
	request.Header.Set("Accept", "application/json")

	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("expected 413, got %d, body=%s", recorder.Code, recorder.Body.String())
	}
}

func TestPhaseOneRuntimeByBinding(t *testing.T) {
	t.Parallel()

	service := core.NewService(newFakeSouthbound())
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	startResponse := performJSONRequest(
		t,
		router,
		http.MethodPost,
		"/api/v1/runtime/start",
		map[string]any{"user_id": "e2e-main-0416"},
		http.StatusOK,
	)
	var started struct {
		Runtime core.RuntimeView `json:"runtime"`
	}
	decodeResponse(t, startResponse, &started)
	if started.Runtime.AssistantID != "demo-main" {
		t.Fatalf("expected demo-main runtime, got %q", started.Runtime.AssistantID)
	}
	if started.Runtime.Status != "running" {
		t.Fatalf("expected running runtime, got %q", started.Runtime.Status)
	}
	if started.Runtime.ContainerName != "ironagent-assistant-demo-main" {
		t.Fatalf("unexpected container %q", started.Runtime.ContainerName)
	}

	getResponse := performJSONRequest(
		t,
		router,
		http.MethodGet,
		"/api/v1/runtime?user_id=e2e-main-0416",
		nil,
		http.StatusOK,
	)
	var fetched struct {
		Runtime core.RuntimeView `json:"runtime"`
	}
	decodeResponse(t, getResponse, &fetched)
	if fetched.Runtime.ContainerName != "ironagent-assistant-demo-main" {
		t.Fatalf("unexpected fetched container %q", fetched.Runtime.ContainerName)
	}

	stopResponse := performJSONRequest(
		t,
		router,
		http.MethodPost,
		"/api/v1/runtime/stop",
		map[string]any{"user_id": "e2e-main-0416"},
		http.StatusOK,
	)
	var stopped struct {
		Runtime core.RuntimeView `json:"runtime"`
	}
	decodeResponse(t, stopResponse, &stopped)
	if stopped.Runtime.Status != "stopped" {
		t.Fatalf("expected stopped runtime, got %q", stopped.Runtime.Status)
	}
}

func TestRuntimeRegistrySchedulesTargetAndTracksInFlight(t *testing.T) {
	t.Parallel()

	registry := runtimepool.NewRegistry(2)
	if _, err := registry.Upsert(runtimepool.RuntimeRecord{
		AssistantID:     "demo-main",
		InternalBaseURL: "http://demo-main:18789",
		Status:          "running",
		Capacity:        2,
	}); err != nil {
		t.Fatalf("upsert source runtime: %v", err)
	}
	if _, err := registry.Upsert(runtimepool.RuntimeRecord{
		AssistantID:     "demo-approval",
		InternalBaseURL: "http://demo-approval:18789",
		Status:          "running",
		Capacity:        2,
	}); err != nil {
		t.Fatalf("upsert target runtime: %v", err)
	}

	southboundFake := newFakeSouthbound()
	service := core.NewServiceWithScheduler(southboundFake, registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"input_object_id":     "object-scheduled",
		"instruction":         "Run the scheduled task.",
		"approval_mode":       "none",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	stored := southboundFake.tasks[created.TaskID]
	if stored.TargetAssistantID != "demo-approval" {
		t.Fatalf("expected scheduler to choose demo-approval, got %q", stored.TargetAssistantID)
	}
	record, ok := registry.Get("demo-approval")
	if !ok {
		t.Fatalf("expected demo-approval runtime record")
	}
	if record.InFlight != 0 {
		t.Fatalf("expected runtime executor disabled path to release in_flight after start, got %d", record.InFlight)
	}

	performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+created.TaskID+"/complete", map[string]any{
		"assistant_id":             "demo-approval",
		"claim_token":              "token-1",
		"result_workspace_file_id": "file-1",
	}, http.StatusOK)
	record, _ = registry.Get("demo-approval")
	if record.InFlight != 0 {
		t.Fatalf("expected in_flight=0 after complete, got %d", record.InFlight)
	}
}

func TestRuntimeSchedulerUsesCapabilitiesAndLoadRatio(t *testing.T) {
	t.Parallel()

	registry := runtimepool.NewRegistry(10)
	runtimes := []runtimepool.RuntimeRecord{
		{AssistantID: "demo-main", InternalBaseURL: "http://demo-main:18789", Status: "running", Capacity: 10},
		{AssistantID: "demo-analysis-a", InternalBaseURL: "http://demo-analysis-a:18789", Status: "running", Capacity: 10, InFlight: 5, Capabilities: []string{"analysis"}},
		{AssistantID: "demo-analysis-b", InternalBaseURL: "http://demo-analysis-b:18789", Status: "running", Capacity: 10, InFlight: 1, Capabilities: []string{"analysis"}},
		{AssistantID: "demo-image", InternalBaseURL: "http://demo-image:18789", Status: "running", Capacity: 10, InFlight: 0, Capabilities: []string{"image"}},
	}
	for _, record := range runtimes {
		if _, err := registry.Upsert(record); err != nil {
			t.Fatalf("upsert runtime %s: %v", record.AssistantID, err)
		}
	}

	southboundFake := newFakeSouthbound()
	service := core.NewServiceWithScheduler(southboundFake, registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id":   "demo-main",
		"input_object_id":       "object-capability-scheduled",
		"instruction":           "Run the analysis task.",
		"approval_mode":         "none",
		"required_capabilities": []string{"analysis"},
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	stored := southboundFake.tasks[created.TaskID]
	if stored.TargetAssistantID != "demo-analysis-b" {
		t.Fatalf("expected lowest-load matching target demo-analysis-b, got %q", stored.TargetAssistantID)
	}

	decisionResponse := performJSONRequest(
		t,
		router,
		http.MethodGet,
		"/api/v1/runtime/scheduler/last-decision",
		nil,
		http.StatusOK,
	)
	var decisionBody struct {
		Decision runtimepool.SchedulerDecision `json:"decision"`
	}
	decodeResponse(t, decisionResponse, &decisionBody)
	if decisionBody.Decision.SelectedAssistantID != "demo-analysis-b" {
		t.Fatalf("expected decision selected demo-analysis-b, got %#v", decisionBody.Decision)
	}
	if decisionBody.Decision.Reason != "selected_lowest_load" {
		t.Fatalf("expected selected_lowest_load, got %q", decisionBody.Decision.Reason)
	}
	foundMissingCapability := false
	for _, candidate := range decisionBody.Decision.Candidates {
		if candidate.AssistantID == "demo-image" && strings.HasPrefix(candidate.Reason, "missing_capabilities") {
			foundMissingCapability = true
		}
	}
	if !foundMissingCapability {
		t.Fatalf("expected demo-image to be rejected for missing capability, got %#v", decisionBody.Decision.Candidates)
	}
}

func TestRuntimeSchedulerRejectsPreferredTargetWithoutRequiredCapability(t *testing.T) {
	t.Parallel()

	registry := runtimepool.NewRegistry(1)
	if _, err := registry.Upsert(runtimepool.RuntimeRecord{
		AssistantID:     "demo-approval",
		InternalBaseURL: "http://demo-approval:18789",
		Status:          "running",
		Capacity:        1,
		Capabilities:    []string{"image"},
	}); err != nil {
		t.Fatalf("upsert target runtime: %v", err)
	}
	service := core.NewServiceWithScheduler(newFakeSouthbound(), registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id":   "demo-main",
		"target_assistant_id":   "demo-approval",
		"input_object_id":       "object-missing-capability",
		"instruction":           "Run the analysis task.",
		"approval_mode":         "none",
		"required_capabilities": []string{"analysis"},
	}, http.StatusBadRequest)
}

func TestRuntimeSchedulerRejectsUnregisteredPreferredTarget(t *testing.T) {
	t.Parallel()

	registry := runtimepool.NewRegistry(1)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID: "demo-main",
		Status:      "running",
		Capacity:    1,
	}); err != nil {
		t.Fatalf("heartbeat source runtime: %v", err)
	}
	service := core.NewServiceWithScheduler(newFakeSouthbound(), registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-missing",
		"input_object_id":     "object-unregistered-target",
		"instruction":         "Do not bypass runtime registry.",
		"approval_mode":       "none",
	}, http.StatusBadRequest)
}

func TestTaskQueueEnqueuesThenWorkerExecutesSouthbound(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-queued-1",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-queued",
		"instruction":         "Run from queue.",
		"approval_mode":       "none",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)
	if created.TaskID == "" || !strings.HasPrefix(created.TaskID, "gw-") {
		t.Fatalf("expected gateway task id, got %q", created.TaskID)
	}
	if created.Status != "processing" || created.Stage != "queued" {
		t.Fatalf("expected queued response, got status=%q stage=%q", created.Status, created.Stage)
	}
	if southboundFake.nextTaskID != 0 {
		t.Fatalf("expected no synchronous southbound task, got %d", southboundFake.nextTaskID)
	}

	queueResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/task-queue", nil, http.StatusOK)
	var queued struct {
		Queue core.TaskQueueStats `json:"queue"`
	}
	decodeResponse(t, queueResponse, &queued)
	if queued.Queue.Queued != 1 {
		t.Fatalf("expected one queued task, got %#v", queued.Queue)
	}

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "test-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}
	if southboundFake.nextTaskID != 1 {
		t.Fatalf("expected one southbound task after worker, got %d", southboundFake.nextTaskID)
	}

	getResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if fetched.TaskID != created.TaskID {
		t.Fatalf("expected external task id %q, got %q", created.TaskID, fetched.TaskID)
	}
	if fetched.Status != "processing" || fetched.Stage != "tool_running" {
		t.Fatalf("expected worker-started task, got %#v", fetched)
	}

	metricsResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/metrics", nil, http.StatusOK)
	var metrics struct {
		TaskQueue core.TaskQueueStats `json:"task_queue"`
	}
	decodeResponse(t, metricsResponse, &metrics)
	if metrics.TaskQueue.Completed != 1 {
		t.Fatalf("expected completed queue metric, got %#v", metrics.TaskQueue)
	}
}

func TestTaskQueueRuntimeExecutorCompletesSouthboundTask(t *testing.T) {
	t.Parallel()

	store := &dbStatsMemoryStore{
		MemoryStore: taskstore.NewMemoryStore(),
		stats: core.DBPoolStats{
			MaxOpenConnections: 40,
			OpenConnections:    12,
			InUse:              7,
			Idle:               5,
			WaitCount:          9,
			WaitDurationSecs:   0.125,
			MaxIdleClosed:      2,
			MaxIdleTimeClosed:  3,
			MaxLifetimeClosed:  4,
		},
	}
	southboundFake := newFakeSouthbound()
	southboundFake.assistants["demo-approval"] = southbound.Assistant{
		AssistantID: "demo-approval",
		Name:        "Demo Approval",
		Runtime: southbound.AssistantRuntime{
			Status:       "running",
			GatewayURL:   "http://127.0.0.1:18789",
			GatewayToken: "runtime-token",
		},
	}
	runtimeExecutor := &fakeRuntimeExecutor{reply: "runtime says ok"}
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetDefaultTaskTimeout(42 * time.Minute)
	service.SetRuntimeExecutor(runtimeExecutor)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-runtime-executor-1",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-runtime-executor",
		"instruction":         "Run through the assistant runtime.",
		"approval_mode":       "none",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "test-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}
	if len(runtimeExecutor.calls) != 1 {
		t.Fatalf("runtime calls = %d, want 1", len(runtimeExecutor.calls))
	}
	call := runtimeExecutor.calls[0]
	if call.AssistantID != "demo-approval" {
		t.Fatalf("runtime assistant id = %q, want demo-approval", call.AssistantID)
	}
	if call.GatewayToken != "runtime-token" {
		t.Fatalf("runtime gateway token was not propagated")
	}
	if call.SessionKey == "agent:demo-approval:main" {
		t.Fatalf("runtime session key = %q", call.SessionKey)
	}
	if !strings.HasPrefix(call.SessionKey, "agent:demo-approval:run-task-") {
		t.Fatalf("runtime session key = %q", call.SessionKey)
	}
	if call.TimeoutMs != int((42*time.Minute)/time.Millisecond) {
		t.Fatalf("runtime timeout ms = %d", call.TimeoutMs)
	}

	getResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if fetched.Status != "completed" || fetched.Stage != "done" {
		t.Fatalf("expected completed task, got %#v", fetched)
	}
	if fetched.ReplyText != "runtime says ok" {
		t.Fatalf("reply_text = %q", fetched.ReplyText)
	}

	if southboundFake.nextTaskID != 1 {
		t.Fatalf("expected one southbound task, got %d", southboundFake.nextTaskID)
	}
	for _, task := range southboundFake.tasks {
		if task.Status != "completed" || task.ResultObjectID == "" {
			t.Fatalf("expected completed southbound task with result object, got %#v", task)
		}
	}
}

func TestTaskQueueRuntimeExecutorDefersProviderQuotaWithoutPublicFailure(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	southboundFake.assistants["demo-approval"] = southbound.Assistant{
		AssistantID: "demo-approval",
		Name:        "Demo Approval",
		Runtime: southbound.AssistantRuntime{
			Status:       "running",
			GatewayURL:   "http://127.0.0.1:18789",
			GatewayToken: "runtime-token",
		},
	}
	runtimeExecutor := &fakeRuntimeExecutor{
		reply: "runtime says ok after retry",
		errors: []error{
			errors.New("runtime chat error: ⚠️ concurrency allocated quota exceeded. please try again later"),
		},
	}
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetRuntimeExecutor(runtimeExecutor)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-runtime-quota-retry",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-runtime-quota-retry",
		"instruction":         "Defer provider quota failures.",
		"approval_mode":       "none",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "quota-retry-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   2,
		RetryDelay:    time.Nanosecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks first attempt error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("first attempt processed = %d, want 1", processed)
	}
	stats, err := store.TaskQueueStats(context.Background())
	if err != nil {
		t.Fatalf("TaskQueueStats error = %v", err)
	}
	if stats.Retry != 1 || stats.Failed != 0 {
		t.Fatalf("expected capacity defer without queue failure after provider quota, got %#v", stats)
	}
	time.Sleep(time.Millisecond)
	deferred, err := store.ClaimQueuedTasks(context.Background(), "quota-retry-inspector", 1, time.Second)
	if err != nil {
		t.Fatalf("ClaimQueuedTasks after provider quota defer error = %v", err)
	}
	if len(deferred) != 1 {
		t.Fatalf("deferred claim count = %d, want 1", len(deferred))
	}
	if deferred[0].Attempts != 1 {
		t.Fatalf("provider quota defer burned attempt: attempts = %d, want 1", deferred[0].Attempts)
	}
	if ok, err := store.DeferQueuedTask(context.Background(), created.TaskID, deferred[0].LockedBy, deferred[0].AttemptToken, "inspection complete", nil); err != nil {
		t.Fatalf("restore inspected deferred task: %v", err)
	} else if !ok {
		t.Fatal("restore inspected deferred task returned false")
	}

	getResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if fetched.Status == "failed" {
		t.Fatalf("provider quota defer should not expose public task failure: %#v", fetched)
	}

	time.Sleep(time.Millisecond)
	processed, err = service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "quota-retry-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   2,
		RetryDelay:    time.Nanosecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks after provider quota defer error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("post-defer processed = %d, want 1", processed)
	}
	if len(runtimeExecutor.calls) < 1 {
		t.Fatalf("runtime calls = %d, want at least 1", len(runtimeExecutor.calls))
	}
	getResponse = performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	decodeResponse(t, getResponse, &fetched)
	if fetched.Status != "completed" || fetched.Stage != "done" {
		t.Fatalf("expected completed task after capacity defer, got %#v", fetched)
	}
}

func TestTaskQueueWaitsForCreatedRunningSouthboundTask(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	southboundFake.createTaskStatus = "running"
	southboundFake.getTaskAutoCompleteAfter = 2
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-created-running-southbound",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-created-running-southbound",
		"instruction":         "Wait for a southbound task that is already running.",
		"approval_mode":       "none",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "running-southbound-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}
	if southboundFake.nextTaskID != 1 {
		t.Fatalf("southbound create count = %d, want 1", southboundFake.nextTaskID)
	}

	getResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if fetched.Status != "completed" || fetched.Stage != "done" {
		t.Fatalf("expected completed task after southbound polling, got %#v", fetched)
	}
	stats, err := store.TaskQueueStats(context.Background())
	if err != nil {
		t.Fatalf("TaskQueueStats error = %v", err)
	}
	if stats.Completed != 1 || stats.Retry != 0 || stats.Failed != 0 {
		t.Fatalf("expected completed queue without retry/failure, got %#v", stats)
	}
}

func TestTaskQueueResumesExistingSouthboundTaskWithoutCreatingAnother(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	southboundFake.getTaskAutoCompleteAfter = 1
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-resume-existing-southbound",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-resume-existing-southbound",
		"instruction":         "Resume an existing southbound task.",
		"approval_mode":       "none",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	existingSouthboundID := "task-existing-running"
	now := time.Now().UTC()
	southboundFake.tasks[existingSouthboundID] = southbound.Task{
		ID:                existingSouthboundID,
		RootTaskID:        existingSouthboundID,
		Status:            "running",
		SourceAssistantID: "demo-main",
		TargetAssistantID: "demo-approval",
		InputObjectID:     "object-resume-existing-southbound",
		Instruction:       "Resume an existing southbound task.",
		ApprovalMode:      "none",
		TimeoutSeconds:    60,
		CreatedAt:         sbTime(now),
		StartedAt:         sbTimePtr(now),
	}
	record, ok, err := store.GetTask(context.Background(), created.TaskID)
	if err != nil || !ok {
		t.Fatalf("stored gateway task missing: ok=%v err=%v", ok, err)
	}
	record.SouthboundTaskID = existingSouthboundID
	record.Task.Status = "running"
	if record.Task.Metadata == nil {
		record.Task.Metadata = map[string]any{}
	}
	record.Task.Metadata["queue_status"] = "processing"
	if err := store.UpsertTask(context.Background(), record); err != nil {
		t.Fatalf("upsert stored gateway task: %v", err)
	}

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "resume-southbound-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}
	if southboundFake.nextTaskID != 0 {
		t.Fatalf("southbound create count = %d, want 0", southboundFake.nextTaskID)
	}

	getResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if fetched.Status != "completed" || fetched.Stage != "done" {
		t.Fatalf("expected resumed task to complete, got %#v", fetched)
	}
	stored, ok, err := store.GetTask(context.Background(), created.TaskID)
	if err != nil || !ok {
		t.Fatalf("stored completed gateway task missing: ok=%v err=%v", ok, err)
	}
	if stored.SouthboundTaskID != existingSouthboundID {
		t.Fatalf("stored southbound id = %q, want %q", stored.SouthboundTaskID, existingSouthboundID)
	}
}

func TestTaskQueueDefersActiveSouthboundTaskWithoutPublicFailure(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-active-southbound-defer",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-active-southbound-defer",
		"instruction":         "Keep the existing southbound task running.",
		"approval_mode":       "none",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	existingSouthboundID := "task-existing-long-running"
	now := time.Now().UTC()
	southboundFake.tasks[existingSouthboundID] = southbound.Task{
		ID:                existingSouthboundID,
		RootTaskID:        existingSouthboundID,
		Status:            "running",
		SourceAssistantID: "demo-main",
		TargetAssistantID: "demo-approval",
		InputObjectID:     "object-active-southbound-defer",
		Instruction:       "Keep the existing southbound task running.",
		ApprovalMode:      "none",
		TimeoutSeconds:    1,
		CreatedAt:         sbTime(now),
		StartedAt:         sbTimePtr(now),
	}
	record, ok, err := store.GetTask(context.Background(), created.TaskID)
	if err != nil || !ok {
		t.Fatalf("stored gateway task missing: ok=%v err=%v", ok, err)
	}
	record.SouthboundTaskID = existingSouthboundID
	record.Task.Status = "running"
	if record.Task.Metadata == nil {
		record.Task.Metadata = map[string]any{}
	}
	record.Task.Metadata["queue_status"] = "processing"
	if err := store.UpsertTask(context.Background(), record); err != nil {
		t.Fatalf("upsert stored gateway task: %v", err)
	}

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:                     "active-southbound-worker",
		ClaimLimit:                   1,
		LeaseDuration:                2 * time.Second,
		MaxAttempts:                  1,
		RetryDelay:                   time.Millisecond,
		TaskProgressCallbackInterval: 3 * time.Minute,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}
	if southboundFake.nextTaskID != 0 {
		t.Fatalf("southbound create count = %d, want 0", southboundFake.nextTaskID)
	}

	stats, err := store.TaskQueueStats(context.Background())
	if err != nil {
		t.Fatalf("TaskQueueStats error = %v", err)
	}
	if stats.Retry != 1 || stats.Failed != 0 {
		t.Fatalf("expected deferred retry without queue failure, got %#v", stats)
	}
	if task := southboundFake.tasks[existingSouthboundID]; task.Status != "cancelled" {
		t.Fatalf("expected stale southbound task to be cancelled for retry, got %q", task.Status)
	}

	getResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if fetched.Status != "processing" || fetched.Stage != "tool_running" {
		t.Fatalf("expected public task to stay processing/tool_running, got %#v", fetched)
	}
	stored, ok, err := store.GetTask(context.Background(), created.TaskID)
	if err != nil || !ok {
		t.Fatalf("stored gateway task missing after defer: ok=%v err=%v", ok, err)
	}
	if stored.SouthboundTaskID != existingSouthboundID {
		t.Fatalf("stored southbound id = %q, want %q", stored.SouthboundTaskID, existingSouthboundID)
	}

	err = service.ProcessQueuedChatTask(context.Background(), core.GatewayTaskQueueItem{
		TaskID: created.TaskID,
		Request: core.ChatRequest{
			RequestID:         "req-active-southbound-defer",
			SourceAssistantID: "demo-main",
			TargetAssistantID: "demo-approval",
			InputObjectID:     "object-active-southbound-defer",
			Instruction:       "Keep the existing southbound task running.",
			ApprovalMode:      "none",
		},
		LastError: fmt.Sprintf("collaboration task %s is still running after 1s", existingSouthboundID),
	}, core.TaskWorkerOptions{
		RetryDelay: time.Nanosecond,
	}, nil, nil)
	if err != nil {
		t.Fatalf("ProcessQueuedChatTask replacement error = %v", err)
	}
	if southboundFake.nextTaskID != 1 {
		t.Fatalf("southbound create count after stale replacement = %d, want 1", southboundFake.nextTaskID)
	}
	stored, ok, err = store.GetTask(context.Background(), created.TaskID)
	if err != nil || !ok {
		t.Fatalf("stored gateway task missing after replacement: ok=%v err=%v", ok, err)
	}
	if stored.SouthboundTaskID == existingSouthboundID {
		t.Fatalf("stored southbound id should be replaced after stale cancellation, still %q", stored.SouthboundTaskID)
	}
	if task := southboundFake.tasks[existingSouthboundID]; task.Status != "cancelled" {
		t.Fatalf("old southbound task status = %q, want cancelled", task.Status)
	}
}

// ②A: an active-southbound task whose downstream stays "active" past the
// task-level wall-clock cap must terminalize to failed (H4) instead of being
// deferred forever. Mirrors the defer test above but with a tiny
// ActiveSouthboundDeferTimeout so the CreatedAt-anchored deadline is exceeded.
func TestTaskQueueFailsActiveSouthboundAfterDeferDeadline(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-active-southbound-deadline",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-active-southbound-deadline",
		"instruction":         "Downstream stays active past the wall-clock cap.",
		"approval_mode":       "none",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	existingSouthboundID := "task-active-deadline"
	now := time.Now().UTC()
	southboundFake.tasks[existingSouthboundID] = southbound.Task{
		ID:                existingSouthboundID,
		RootTaskID:        existingSouthboundID,
		Status:            "running",
		SourceAssistantID: "demo-main",
		TargetAssistantID: "demo-approval",
		InputObjectID:     "object-active-southbound-deadline",
		Instruction:       "Downstream stays active past the wall-clock cap.",
		ApprovalMode:      "none",
		TimeoutSeconds:    1,
		CreatedAt:         sbTime(now),
		StartedAt:         sbTimePtr(now),
	}
	record, ok, err := store.GetTask(context.Background(), created.TaskID)
	if err != nil || !ok {
		t.Fatalf("stored gateway task missing: ok=%v err=%v", ok, err)
	}
	record.SouthboundTaskID = existingSouthboundID
	record.Task.Status = "running"
	if record.Task.Metadata == nil {
		record.Task.Metadata = map[string]any{}
	}
	record.Task.Metadata["queue_status"] = "processing"
	if err := store.UpsertTask(context.Background(), record); err != nil {
		t.Fatalf("upsert stored gateway task: %v", err)
	}

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:                     "active-southbound-deadline-worker",
		ClaimLimit:                   1,
		LeaseDuration:                2 * time.Second,
		MaxAttempts:                  1,
		RetryDelay:                   time.Millisecond,
		ActiveSouthboundDeferTimeout: time.Nanosecond, // CreatedAt-anchored deadline already exceeded
		TaskProgressCallbackInterval: 3 * time.Minute,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}

	stats, err := store.TaskQueueStats(context.Background())
	if err != nil {
		t.Fatalf("TaskQueueStats error = %v", err)
	}
	if stats.Failed != 1 || stats.Retry != 0 {
		t.Fatalf("expected active-southbound wall-clock deadline to terminalize as failed, got %#v", stats)
	}

	stored, ok2, err := store.GetTask(context.Background(), created.TaskID)
	if err != nil || !ok2 {
		t.Fatalf("stored gateway task missing: ok=%v err=%v", ok2, err)
	}
	if stored.Task.Status != "failed" {
		t.Fatalf("gateway task should be failed after wall-clock deadline, got %q (reason=%q)", stored.Task.Status, stored.Task.FailedReasonText)
	}
	if stored.Task.FailedReasonCode != "runtime_active_southbound_timeout" {
		t.Fatalf("expected runtime_active_southbound_timeout reason, got %q", stored.Task.FailedReasonCode)
	}
}

func TestTaskQueueRuntimeExecutorExposesRunningCheckpoint(t *testing.T) {
	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	southboundFake.assistants["demo-approval"] = southbound.Assistant{
		AssistantID: "demo-approval",
		Name:        "Demo Approval",
		Runtime: southbound.AssistantRuntime{
			Status:       "running",
			GatewayURL:   "http://127.0.0.1:18789",
			GatewayToken: "runtime-token",
		},
	}
	runtimeExecutor := newBlockingRuntimeExecutor("runtime says ok")
	defer runtimeExecutor.releaseNow()
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetRuntimeExecutor(runtimeExecutor)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-runtime-checkpoint",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-runtime-checkpoint",
		"instruction":         "Expose running while runtime is still executing.",
		"approval_mode":       "none",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	done := make(chan error, 1)
	go func() {
		processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
			WorkerID:      "checkpoint-worker",
			ClaimLimit:    1,
			LeaseDuration: 10 * time.Second,
			MaxAttempts:   1,
			RetryDelay:    time.Millisecond,
		})
		if err != nil {
			done <- err
			return
		}
		if processed != 1 {
			done <- errors.New("processed count mismatch")
			return
		}
		done <- nil
	}()

	select {
	case <-runtimeExecutor.entered:
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for runtime execution")
	}

	record, ok, err := store.GetTask(context.Background(), created.TaskID)
	if err != nil {
		t.Fatalf("GetTask record error = %v", err)
	}
	if !ok {
		t.Fatalf("missing stored task record")
	}
	if record.SouthboundTaskID == "" {
		t.Fatalf("southbound task id was not checkpointed: %#v", record)
	}
	if record.Task.Status != "running" {
		t.Fatalf("stored task status = %q, want running", record.Task.Status)
	}

	getResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var running publicTaskResponse
	decodeResponse(t, getResponse, &running)
	if running.Status != "processing" || running.Stage != "tool_running" {
		t.Fatalf("expected running public task, got %#v", running)
	}

	runtimeExecutor.releaseNow()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("ProcessQueuedTasks error = %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for queued task worker")
	}

	finalResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var completed publicTaskResponse
	decodeResponse(t, finalResponse, &completed)
	if completed.Status != "completed" || completed.Stage != "done" {
		t.Fatalf("expected completed task, got %#v", completed)
	}
}

func TestTaskQueueStaleRuntimeRetriesThenFailsOnFinalAttempt(t *testing.T) {
	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	southboundFake.assistants["demo-approval"] = southbound.Assistant{
		AssistantID: "demo-approval",
		Name:        "Demo Approval",
		Runtime: southbound.AssistantRuntime{
			Status:       "running",
			GatewayURL:   "http://127.0.0.1:18789",
			GatewayToken: "runtime-token",
		},
	}
	runtimeExecutor := newBlockingRuntimeExecutor("runtime says ok")
	defer runtimeExecutor.releaseNow()
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetRuntimeExecutor(runtimeExecutor)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-runtime-stale",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-runtime-stale",
		"instruction":         "Block long enough to trigger stale progress handling.",
		"approval_mode":       "none",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	options := core.TaskWorkerOptions{
		WorkerID:             "stale-runtime-worker",
		ClaimLimit:           1,
		LeaseDuration:        time.Second,
		MaxAttempts:          3,
		RetryDelay:           time.Millisecond,
		StaleProgressTimeout: 20 * time.Millisecond,
	}
	processed, err := service.ProcessQueuedTasks(context.Background(), options)
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}

	stats, err := store.TaskQueueStats(context.Background())
	if err != nil {
		t.Fatalf("TaskQueueStats error = %v", err)
	}
	if stats.Failed != 0 || stats.Retry != 1 {
		t.Fatalf("expected stale runtime attempt to be queued for retry, got %#v", stats)
	}

	for attempt := 2; attempt <= options.MaxAttempts; attempt++ {
		time.Sleep(5 * time.Millisecond)
		processed, err = service.ProcessQueuedTasks(context.Background(), options)
		if err != nil {
			t.Fatalf("ProcessQueuedTasks attempt %d error = %v", attempt, err)
		}
		if processed != 1 {
			t.Fatalf("attempt %d processed = %d, want 1", attempt, processed)
		}
	}
	stats, err = store.TaskQueueStats(context.Background())
	if err != nil {
		t.Fatalf("TaskQueueStats error = %v", err)
	}
	if stats.Failed != 1 || stats.Retry != 0 {
		t.Fatalf("expected final failed queue item after retries, got %#v", stats)
	}

	finalResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var final publicTaskResponse
	decodeResponse(t, finalResponse, &final)
	if final.Status != "failed" || final.Stage != "error" {
		t.Fatalf("expected failed task, got %#v", final)
	}
	if final.ErrorCode != "gateway_task_stale_progress" {
		t.Fatalf("errorCode = %q, want gateway_task_stale_progress", final.ErrorCode)
	}
	if !strings.Contains(final.ErrorMessage, "runtime made no progress") {
		t.Fatalf("errorMessage = %q, want runtime stale detail", final.ErrorMessage)
	}
}

func TestTaskQueueRuntimeExecutorCancelDuringRunningCheckpointStaysCancelled(t *testing.T) {
	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	southboundFake.assistants["demo-approval"] = southbound.Assistant{
		AssistantID: "demo-approval",
		Name:        "Demo Approval",
		Runtime: southbound.AssistantRuntime{
			Status:       "running",
			GatewayURL:   "http://127.0.0.1:18789",
			GatewayToken: "runtime-token",
		},
	}
	runtimeExecutor := newBlockingRuntimeExecutor("runtime says ok")
	defer runtimeExecutor.releaseNow()
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetRuntimeExecutor(runtimeExecutor)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-runtime-cancel-checkpoint",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-runtime-cancel-checkpoint",
		"instruction":         "Cancel while runtime is still executing.",
		"approval_mode":       "none",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	done := make(chan error, 1)
	go func() {
		_, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
			WorkerID:      "cancel-checkpoint-worker",
			ClaimLimit:    1,
			LeaseDuration: 10 * time.Second,
			MaxAttempts:   1,
			RetryDelay:    time.Millisecond,
		})
		done <- err
	}()

	select {
	case <-runtimeExecutor.entered:
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for runtime execution")
	}

	cancelResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+created.TaskID+"/cancel", map[string]any{
		"actor_id": "runtime-test",
		"reason":   "cancel during runtime",
	}, http.StatusOK)
	var cancelled struct {
		Status string `json:"status"`
	}
	decodeResponse(t, cancelResponse, &cancelled)
	if cancelled.Status != "cancelled" {
		t.Fatalf("expected cancelled response, got %#v", cancelled)
	}

	runtimeExecutor.releaseNow()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("ProcessQueuedTasks error = %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for queued task worker")
	}

	finalResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var final publicTaskResponse
	decodeResponse(t, finalResponse, &final)
	if final.Status != "cancelled" || final.Stage != "blocked" {
		t.Fatalf("cancelled task was overwritten by terminal worker result: %#v", final)
	}
	for _, task := range southboundFake.tasks {
		if task.Status != "cancelled" {
			t.Fatalf("southbound task status = %q, want cancelled", task.Status)
		}
	}
}

func TestTaskQueueRuntimeExecutorCompletesWithSyncedResultFile(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	setFakeAssistantRuntimeStatus(t, southboundFake, "demo-main", "running")
	runtimeExecutor := &fakeRuntimeExecutor{reply: "runtime created a result file"}
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetRuntimeExecutor(runtimeExecutor)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	createResponse := performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":      "req-runtime-result-file",
		"user_id":         "e2e-main-0416",
		"conversation_id": "conv-runtime-result-file",
		"message":         "生成一个结果文件。",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "test-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}
	if len(runtimeExecutor.calls) != 1 {
		t.Fatalf("runtime calls = %d, want 1", len(runtimeExecutor.calls))
	}
	runtimeMessage := runtimeExecutor.calls[0].Message
	for _, want := range []string{"/workspace/temp/go-gateway-results/", "不要只在回复文本", "If no file output is requested"} {
		if !strings.Contains(runtimeMessage, want) {
			t.Fatalf("runtime message missing %q: %s", want, runtimeMessage)
		}
	}

	getResponse := performPublicJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if fetched.Status != "completed" || fetched.Stage != "done" {
		t.Fatalf("expected completed task, got %#v", fetched)
	}
	if len(fetched.Files) != 1 {
		t.Fatalf("expected one synced result file, got %#v", fetched.Files)
	}
	file := fetched.Files[0]
	if file.FileID != "result-1" || file.DownloadURL != "/api/v1/files/result-1/download?user_id=e2e-main-0416" {
		t.Fatalf("unexpected result file payload: %#v", file)
	}
	if len(southboundFake.syncRequests) == 0 || southboundFake.syncRequests[0].UserID != "e2e-main-0416" {
		t.Fatalf("sync requests did not include user_id: %#v", southboundFake.syncRequests)
	}
	if southboundFake.syncRequests[0].ResultPathPrefix == "" {
		t.Fatalf("sync request did not include result path prefix: %#v", southboundFake.syncRequests[0])
	}
	for _, task := range southboundFake.tasks {
		if task.Status == "completed" && task.ResultWorkspaceFileID != "result-1" {
			t.Fatalf("expected southbound result workspace file, got %#v", task)
		}
	}
}

func TestTaskResponseIncludesStoredResultFileWithSparseMetadata(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	service := core.NewServiceWithTaskStore(southboundFake, store)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)
	fileID := "sparse-result-file"
	southboundFake.files[fileID] = southbound.RawJSON{
		"file_id":    fileID,
		"filename":   "go-gateway-results/root-task/result.md",
		"size":       207,
		"mime_type":  "application/octet-stream",
		"created_at": time.Now().UTC(),
	}
	now := time.Now().UTC()
	if err := store.UpsertTask(context.Background(), core.GatewayTaskRecord{
		Task: core.TaskView{
			ID:                    "gw-sparse-result-task",
			Status:                "completed",
			TargetAssistantID:     "assistant-a",
			CreatedBy:             "user-a",
			CreatedAt:             now,
			ResultWorkspaceFileID: fileID,
			Metadata: map[string]any{
				"runtime_reply_text":                "TASK_RESULT_READY",
				"runtime_result_task_id":            "root-task",
				"runtime_result_path_prefix":        "go-gateway-results/root-task/",
				"runtime_result_workspace_file_ids": []string{fileID},
				"runtime_result_file_count":         1,
				"requires_result_file":              true,
			},
		},
		CreatedAt: now,
		UpdatedAt: now,
	}); err != nil {
		t.Fatalf("UpsertTask() error = %v", err)
	}

	getResponse := performPublicJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/gw-sparse-result-task", nil, http.StatusOK)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if fetched.Status != "completed" || fetched.Stage != "done" {
		t.Fatalf("expected completed task, got %#v", fetched)
	}
	if len(fetched.Files) != 1 {
		t.Fatalf("expected sparse metadata result file to be preserved, got %#v", fetched.Files)
	}
	file := fetched.Files[0]
	if file.FileID != fileID {
		t.Fatalf("file id = %q, want %q", file.FileID, fileID)
	}
	if file.FileName != "go-gateway-results/root-task/result.md" || file.Size != "207" || file.MimeType != "application/octet-stream" {
		t.Fatalf("unexpected sparse metadata enrichment: %#v", file)
	}
	if file.DownloadURL != "/api/v1/files/sparse-result-file/download?user_id=user-a" {
		t.Fatalf("download url = %q", file.DownloadURL)
	}
}

func TestTaskQueueRuntimeExecutorIgnoresUnscopedResultFiles(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	southboundFake.syncResultFiles = []southbound.RawJSON{
		{
			"file_id":       "result-stale-long-task",
			"filename":      "long-task.log",
			"relative_path": "temp/long-task.log",
			"size_bytes":    32,
			"content_type":  "text/plain",
			"bucket":        "temp",
			"updated_at":    time.Now().UTC().Add(time.Hour).Format(time.RFC3339Nano),
		},
		{
			"file_id":      "result-current",
			"filename":     "current.md",
			"size_bytes":   9,
			"content_type": "text/markdown",
			"bucket":       "temp",
		},
	}
	setFakeAssistantRuntimeStatus(t, southboundFake, "demo-main", "running")
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetRuntimeExecutor(&fakeRuntimeExecutor{reply: "runtime created the current scoped result file"})
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	createResponse := performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":      "req-runtime-result-file-scope",
		"user_id":         "e2e-main-0416",
		"conversation_id": "conv-runtime-result-file-scope",
		"message":         "生成一个结果文件。",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "test-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}

	getResponse := performPublicJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if len(fetched.Files) != 1 {
		t.Fatalf("task query files = %#v, want one current result file", fetched.Files)
	}
	if fetched.Files[0].FileID != "result-current" {
		t.Fatalf("task query files = %#v, want result-current only", fetched.Files)
	}
}

func TestTaskQueueRuntimeExecutorCompletesWithAllSyncedResultFiles(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	southboundFake.syncResultFiles = []southbound.RawJSON{
		{
			"file_id":      "result-old",
			"filename":     "old.txt",
			"size_bytes":   3,
			"content_type": "text/plain",
			"bucket":       "temp",
		},
		{
			"file_id":      "result-new",
			"filename":     "new.txt",
			"size_bytes":   4,
			"content_type": "text/plain",
			"bucket":       "persistent",
		},
	}
	setFakeAssistantRuntimeStatus(t, southboundFake, "demo-main", "running")
	runtimeExecutor := &fakeRuntimeExecutor{reply: "runtime created two result files"}
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetRuntimeExecutor(runtimeExecutor)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	createResponse := performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":      "req-runtime-result-files",
		"user_id":         "e2e-main-0416",
		"conversation_id": "conv-runtime-result-files",
		"message":         "生成两个结果文件。",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "test-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}

	getResponse := performPublicJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	wantFiles := []core.PublicTaskFileResponse{
		{
			FileID:      "result-new",
			FileName:    "new.txt",
			Size:        "4",
			MimeType:    "text/plain",
			DownloadURL: "/api/v1/files/result-new/download?user_id=e2e-main-0416",
		},
		{
			FileID:      "result-old",
			FileName:    "old.txt",
			Size:        "3",
			MimeType:    "text/plain",
			DownloadURL: "/api/v1/files/result-old/download?user_id=e2e-main-0416",
		},
	}
	if !reflect.DeepEqual(fetched.Files, wantFiles) {
		t.Fatalf("task query files = %#v, want %#v", fetched.Files, wantFiles)
	}
	record, ok, err := store.GetTask(context.Background(), created.TaskID)
	if err != nil || !ok {
		t.Fatalf("stored task lookup ok=%v err=%v", ok, err)
	}
	if record.Task.ResultWorkspaceFileID != "result-new" {
		t.Fatalf("primary result workspace file = %q, want result-new", record.Task.ResultWorkspaceFileID)
	}
	rawIDs, ok := record.Task.Metadata["runtime_result_workspace_file_ids"].([]string)
	if !ok || !reflect.DeepEqual(rawIDs, []string{"result-new", "result-old"}) {
		t.Fatalf("stored runtime result file ids = %#v", record.Task.Metadata["runtime_result_workspace_file_ids"])
	}
	if len(southboundFake.completeRequests) != 1 {
		t.Fatalf("complete request count = %d, want 1", len(southboundFake.completeRequests))
	}
	if got := southboundFake.completeRequests[0].ResultWorkspaceFileIDs; !reflect.DeepEqual(got, []string{"result-new", "result-old"}) {
		t.Fatalf("southbound complete result file ids = %#v", got)
	}
}

func TestTaskQueueRuntimeExecutorIncludesTaskScopedResultFileWithOlderMTime(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	oldTime := time.Now().UTC().Add(-time.Hour).Format(time.RFC3339Nano)
	southboundFake.syncResultFiles = []southbound.RawJSON{
		{
			"file_id":                    "old-mtime-result",
			"filename":                   "old-mtime.txt",
			"size_bytes":                 10,
			"content_type":               "text/plain",
			"bucket":                     "temp",
			"created_at":                 oldTime,
			"updated_at":                 oldTime,
			"workspace_last_modified_at": oldTime,
		},
	}
	setFakeAssistantRuntimeStatus(t, southboundFake, "demo-main", "running")
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetRuntimeExecutor(&fakeRuntimeExecutor{reply: "runtime created one result file"})
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	createResponse := performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":      "req-runtime-result-file-old-mtime",
		"user_id":         "e2e-main-0416",
		"conversation_id": "conv-runtime-result-file-old-mtime",
		"message":         "生成一个结果文件。",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "test-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}

	record, ok, err := store.GetTask(context.Background(), created.TaskID)
	if err != nil || !ok {
		t.Fatalf("stored task lookup ok=%v err=%v", ok, err)
	}
	if record.Task.ResultWorkspaceFileID != "old-mtime-result" {
		t.Fatalf("primary result workspace file = %q, want old-mtime-result", record.Task.ResultWorkspaceFileID)
	}
	if got, ok := record.Task.Metadata["runtime_result_workspace_file_ids"].([]string); !ok || !reflect.DeepEqual(got, []string{"old-mtime-result"}) {
		t.Fatalf("stored runtime result file ids = %#v", record.Task.Metadata["runtime_result_workspace_file_ids"])
	}
	for index, request := range southboundFake.syncRequests {
		if !request.ChangedAfter.IsZero() {
			t.Fatalf("task-scoped sync request %d should not depend on mtime: %#v", index, request)
		}
	}
}

func TestTaskQueueRuntimeExecutorRetriesTaskScopedResultFileSync(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	southboundFake.syncResultFilesAvailableAfter = 3
	southboundFake.syncResultFiles = []southbound.RawJSON{
		{
			"file_id":      "late-result",
			"filename":     "late.txt",
			"size_bytes":   4,
			"content_type": "text/plain",
			"bucket":       "temp",
		},
	}
	setFakeAssistantRuntimeStatus(t, southboundFake, "demo-main", "running")
	runtimeExecutor := &fakeRuntimeExecutor{
		replyFunc: func(request core.RuntimeExecutionRequest) string {
			return "created /workspace/temp/go-gateway-results/" + request.RequestID + "/late.txt"
		},
	}
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetRuntimeExecutor(runtimeExecutor)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	createResponse := performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":      "req-runtime-result-file-retry",
		"user_id":         "e2e-main-0416",
		"conversation_id": "conv-runtime-result-file-retry",
		"message":         "生成一个结果文件。",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "test-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}
	if len(southboundFake.syncRequests) < 3 {
		t.Fatalf("sync requests = %d, want retry after missing result", len(southboundFake.syncRequests))
	}
	record, ok, err := store.GetTask(context.Background(), created.TaskID)
	if err != nil || !ok {
		t.Fatalf("stored task lookup ok=%v err=%v", ok, err)
	}
	if record.Task.ResultWorkspaceFileID != "late-result" {
		t.Fatalf("primary result workspace file = %q, want late-result", record.Task.ResultWorkspaceFileID)
	}
}

func TestTaskQueueRuntimeExecutorDoesNotRetryMissingFilesForTextOnlyReply(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	southboundFake.disableDefaultSyncResult = true
	setFakeAssistantRuntimeStatus(t, southboundFake, "demo-main", "running")
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetRuntimeExecutor(&fakeRuntimeExecutor{reply: "runtime returned a text-only answer"})
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	createResponse := performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":      "req-runtime-text-only-no-result-retry",
		"user_id":         "e2e-main-0416",
		"conversation_id": "conv-runtime-text-only-no-result-retry",
		"message":         "直接回答，不要生成文件。",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "test-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}
	if len(southboundFake.syncRequests) != 2 {
		t.Fatalf("sync requests = %d, want one temp and one persistent scan without missing-file retries: %#v", len(southboundFake.syncRequests), southboundFake.syncRequests)
	}
	record, ok, err := store.GetTask(context.Background(), created.TaskID)
	if err != nil || !ok {
		t.Fatalf("stored task lookup ok=%v err=%v", ok, err)
	}
	if record.Task.ResultWorkspaceFileID != "" {
		t.Fatalf("result workspace file = %q, want empty", record.Task.ResultWorkspaceFileID)
	}
	if got := record.Task.Metadata["runtime_result_file_count"]; got != 0 {
		t.Fatalf("result file count metadata = %#v, want 0", got)
	}
}

func TestTaskQueueRuntimeExecutorDoesNotMatchResultFilePrefixSiblingTask(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	setFakeAssistantRuntimeStatus(t, southboundFake, "demo-main", "running")
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetRuntimeExecutor(&fakeRuntimeExecutor{reply: "created a result file for a sibling task"})
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	createResponse := performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":      "req-runtime-result-prefix-boundary",
		"user_id":         "e2e-main-0416",
		"conversation_id": "conv-runtime-result-prefix-boundary",
		"message":         "生成一个结果文件。",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)
	southboundFake.syncResultFiles = []southbound.RawJSON{
		{
			"file_id":       "sibling-task-result",
			"filename":      "sibling.txt",
			"content_type":  "text/plain",
			"size_bytes":    7,
			"bucket":        "temp",
			"relative_path": "go-gateway-results/" + created.TaskID + "-extra/sibling.txt",
		},
	}

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "test-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}
	record, ok, err := store.GetTask(context.Background(), created.TaskID)
	if err != nil || !ok {
		t.Fatalf("stored task lookup ok=%v err=%v", ok, err)
	}
	if record.Task.ResultWorkspaceFileID != "" {
		t.Fatalf("result workspace file = %q, want empty for sibling task prefix", record.Task.ResultWorkspaceFileID)
	}
	if got := record.Task.Metadata["runtime_result_file_count"]; got != 0 {
		t.Fatalf("result file count metadata = %#v, want 0", got)
	}
	if got := record.Task.Metadata["runtime_result_workspace_file_ids"]; got != nil {
		t.Fatalf("result file ids metadata = %#v, want nil", got)
	}
}

func TestTaskQueueRuntimeExecutorUsesStableResultFileTieBreaker(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	sameTime := time.Now().UTC().Format(time.RFC3339Nano)
	southboundFake.syncResultFiles = []southbound.RawJSON{
		{
			"file_id":      "result-b",
			"filename":     "b.txt",
			"size_bytes":   1,
			"content_type": "text/plain",
			"bucket":       "temp",
			"created_at":   sameTime,
			"updated_at":   sameTime,
		},
		{
			"file_id":      "result-a",
			"filename":     "a.txt",
			"size_bytes":   1,
			"content_type": "text/plain",
			"bucket":       "temp",
			"created_at":   sameTime,
			"updated_at":   sameTime,
		},
	}
	setFakeAssistantRuntimeStatus(t, southboundFake, "demo-main", "running")
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetRuntimeExecutor(&fakeRuntimeExecutor{reply: "runtime created tied result files"})
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	createResponse := performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":      "req-runtime-result-file-tie-breaker",
		"user_id":         "e2e-main-0416",
		"conversation_id": "conv-runtime-result-file-tie-breaker",
		"message":         "生成两个同时间戳结果文件。",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "test-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}

	record, ok, err := store.GetTask(context.Background(), created.TaskID)
	if err != nil || !ok {
		t.Fatalf("stored task lookup ok=%v err=%v", ok, err)
	}
	wantIDs := []string{"result-a", "result-b"}
	if record.Task.ResultWorkspaceFileID != wantIDs[0] {
		t.Fatalf("primary result workspace file = %q, want %q", record.Task.ResultWorkspaceFileID, wantIDs[0])
	}
	if got, ok := record.Task.Metadata["runtime_result_workspace_file_ids"].([]string); !ok || !reflect.DeepEqual(got, wantIDs) {
		t.Fatalf("stored runtime result file ids = %#v, want %#v", record.Task.Metadata["runtime_result_workspace_file_ids"], wantIDs)
	}
}

func TestTraceQueuedRuntimeResultFilesReachCompletedCallback(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	southboundFake.syncResultFiles = []southbound.RawJSON{
		{
			"file_id":      "trace-temp-result",
			"filename":     "trace-temp.txt",
			"size_bytes":   11,
			"content_type": "text/plain",
			"bucket":       "temp",
		},
		{
			"file_id":      "trace-persistent-result",
			"filename":     "trace-persistent.json",
			"size_bytes":   17,
			"content_type": "application/json",
			"bucket":       "persistent",
		},
	}
	southboundFake.fileBytes["trace-temp-result"] = []byte("temp-result")
	southboundFake.fileBytes["trace-persistent-result"] = []byte(`{"result":"ok"}`)
	setFakeAssistantRuntimeStatus(t, southboundFake, "demo-main", "running")
	runtimeExecutor := &fakeRuntimeExecutor{
		reply: "created /workspace/temp/trace-temp.txt and /workspace/persistent/trace-persistent.json",
	}
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetRuntimeExecutor(runtimeExecutor)

	delivered := &eventCollector{}
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		event, err := decodeCallbackEventFromRequest(r)
		if err != nil {
			t.Errorf("decode callback event: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		delivered.add(event)
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(callback.Close)
	dispatcher := webhook.NewDispatcher(webhook.Config{
		Secret:      "callback-secret",
		MaxAttempts: 1,
		RetryDelay:  time.Millisecond,
	})
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}, AllowPublicUnauthenticated: true}),
		nil,
		RouterOptions{
			Webhooks:             dispatcher,
			DefaultCallbackURL:   callback.URL,
			CallbackAllowedHosts: []string{"127.0.0.1"},
			CallbackAllowPrivate: true,
		},
	)

	createResponse := performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":           "req-trace-runtime-result-files",
		"user_id":              "e2e-main-0416",
		"conversation_id":      "conv-trace-runtime-result-files",
		"message":              "生成两个结果文件。",
		"requires_result_file": true,
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "trace-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}
	if !dispatcher.WaitIdle(time.Second) {
		t.Fatalf("timed out waiting for callbacks")
	}

	if len(runtimeExecutor.calls) != 1 {
		t.Fatalf("runtime calls = %d, want 1", len(runtimeExecutor.calls))
	}
	runtimeMessage := runtimeExecutor.calls[0].Message
	for _, want := range []string{"[Runtime result file handling]", "/workspace/temp", "/workspace/persistent"} {
		if !strings.Contains(runtimeMessage, want) {
			t.Fatalf("runtime message missing %q: %s", want, runtimeMessage)
		}
	}

	if len(southboundFake.syncRequests) != 2 {
		t.Fatalf("sync requests = %#v, want temp and persistent", southboundFake.syncRequests)
	}
	for index, request := range southboundFake.syncRequests {
		if request.AssistantID != "demo-main" || request.UserID != "e2e-main-0416" || !request.ChangedAfter.IsZero() {
			t.Fatalf("sync request %d not scoped to runtime task: %#v", index, request)
		}
		if request.ResultPathPrefix == "" ||
			request.TaskID == "" ||
			request.SourceRef != request.TaskID ||
			!strings.Contains(request.ResultPathPrefix, request.TaskID) {
			t.Fatalf("sync request %d missing task scope: %#v", index, request)
		}
	}
	if southboundFake.syncRequests[0].Bucket != "temp" || southboundFake.syncRequests[1].Bucket != "persistent" {
		t.Fatalf("sync buckets = %q/%q, want temp/persistent", southboundFake.syncRequests[0].Bucket, southboundFake.syncRequests[1].Bucket)
	}

	if len(southboundFake.completeRequests) != 1 {
		t.Fatalf("complete requests = %#v, want one", southboundFake.completeRequests)
	}
	completeRequest := southboundFake.completeRequests[0]
	if completeRequest.ResultObjectID == "" {
		t.Fatalf("complete request missing result object id: %#v", completeRequest)
	}
	if completeRequest.ResultWorkspaceFileID != "trace-persistent-result" {
		t.Fatalf("primary result file = %q, want trace-persistent-result", completeRequest.ResultWorkspaceFileID)
	}

	record, ok, err := store.GetTask(context.Background(), created.TaskID)
	if err != nil || !ok {
		t.Fatalf("stored task lookup ok=%v err=%v", ok, err)
	}
	if record.Task.Status != "completed" || record.Task.ResultWorkspaceFileID != "trace-persistent-result" {
		t.Fatalf("stored task result = status %q file %q", record.Task.Status, record.Task.ResultWorkspaceFileID)
	}
	wantFileIDs := []string{"trace-persistent-result", "trace-temp-result"}
	if got, ok := record.Task.Metadata["runtime_result_workspace_file_ids"].([]string); !ok || !reflect.DeepEqual(got, wantFileIDs) {
		t.Fatalf("stored result file ids = %#v, want %#v", record.Task.Metadata["runtime_result_workspace_file_ids"], wantFileIDs)
	}
	if got := record.Task.Metadata["runtime_result_file_count"]; got != 2 {
		t.Fatalf("stored result file count = %#v, want 2", got)
	}
	if got := record.Task.Metadata["runtime_result_file_sync_error"]; got != nil {
		t.Fatalf("unexpected sync error metadata: %#v", got)
	}
	if got := record.Task.Metadata["runtime_reply_text"]; got != "TASK_RESULT_READY" {
		t.Fatalf("stored public runtime reply = %#v, want TASK_RESULT_READY", got)
	}
	if got := record.Task.Metadata["runtime_raw_reply_text"]; got != runtimeExecutor.reply {
		t.Fatalf("stored raw runtime reply = %#v, want %q", got, runtimeExecutor.reply)
	}

	wantCallbackFiles := []webhook.FileRef{
		{
			FileID:      "trace-persistent-result",
			Filename:    "trace-persistent.json",
			Size:        17,
			MimeType:    "application/json",
			DownloadURL: "/api/v1/files/trace-persistent-result/download?user_id=e2e-main-0416",
		},
		{
			FileID:      "trace-temp-result",
			Filename:    "trace-temp.txt",
			Size:        11,
			MimeType:    "text/plain",
			DownloadURL: "/api/v1/files/trace-temp-result/download?user_id=e2e-main-0416",
		},
	}
	completedEvent, ok := delivered.lastWithStatus("completed")
	if !ok {
		t.Fatalf("expected completed callback, got %#v", delivered.snapshot())
	}
	if completedEvent.TaskID != created.TaskID || completedEvent.Stage != "done" || completedEvent.ReplyText != "### 执行结果\n\nTASK_RESULT_READY" {
		t.Fatalf("completed callback trace mismatch: %#v", completedEvent)
	}
	if !reflect.DeepEqual(completedEvent.Files, wantCallbackFiles) {
		t.Fatalf("callback files = %#v, want %#v", completedEvent.Files, wantCallbackFiles)
	}

	taskResponse := performPublicJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var taskPayload core.PublicTaskResponse
	decodeResponse(t, taskResponse, &taskPayload)
	wantPublicFiles := []core.PublicTaskFileResponse{
		{
			FileID:      "trace-persistent-result",
			FileName:    "trace-persistent.json",
			Size:        "17",
			MimeType:    "application/json",
			DownloadURL: "/api/v1/files/trace-persistent-result/download?user_id=e2e-main-0416",
		},
		{
			FileID:      "trace-temp-result",
			FileName:    "trace-temp.txt",
			Size:        "11",
			MimeType:    "text/plain",
			DownloadURL: "/api/v1/files/trace-temp-result/download?user_id=e2e-main-0416",
		},
	}
	if taskPayload.ReplyText != "TASK_RESULT_READY" {
		t.Fatalf("task query replyText = %q, want TASK_RESULT_READY", taskPayload.ReplyText)
	}
	if !reflect.DeepEqual(taskPayload.Files, wantPublicFiles) {
		t.Fatalf("task query files = %#v, want %#v", taskPayload.Files, wantPublicFiles)
	}

	for _, file := range completedEvent.Files {
		downloadRequest := httptest.NewRequest(http.MethodGet, file.DownloadURL, nil)
		downloadRecorder := httptest.NewRecorder()
		router.ServeHTTP(downloadRecorder, downloadRequest)
		if downloadRecorder.Code != http.StatusOK {
			t.Fatalf("download %s returned %d body=%s", file.FileID, downloadRecorder.Code, downloadRecorder.Body.String())
		}
		if got, want := downloadRecorder.Body.String(), string(southboundFake.fileBytes[file.FileID]); got != want {
			t.Fatalf("download %s body = %q, want %q", file.FileID, got, want)
		}
	}
	stats, err := store.TaskQueueStats(context.Background())
	if err != nil {
		t.Fatalf("TaskQueueStats error = %v", err)
	}
	if stats.Completed != 1 || stats.Active != 0 {
		t.Fatalf("queue stats = %#v, want completed=1 active=0", stats)
	}
}

func TestQueuedRequiredResultFileCompletesWhenRuntimeFinalStalls(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	southboundFake.syncResultFiles = []southbound.RawJSON{
		{
			"file_id":      "stall-recovered-result",
			"filename":     "stall-result.md",
			"size_bytes":   23,
			"content_type": "text/markdown",
			"bucket":       "temp",
		},
	}
	southboundFake.fileBytes["stall-recovered-result"] = []byte("RUN_ID=stall-recovered")
	setFakeAssistantRuntimeStatus(t, southboundFake, "demo-main", "running")
	runtimeExecutor := newBlockingRuntimeExecutor("runtime final arrived too late")
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetRuntimeExecutor(runtimeExecutor)

	delivered := &eventCollector{}
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		event, err := decodeCallbackEventFromRequest(r)
		if err != nil {
			t.Errorf("decode callback event: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		delivered.add(event)
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(callback.Close)
	dispatcher := webhook.NewDispatcher(webhook.Config{
		Secret:      "callback-secret",
		MaxAttempts: 1,
		RetryDelay:  time.Millisecond,
	})
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}, AllowPublicUnauthenticated: true}),
		nil,
		RouterOptions{
			Webhooks:             dispatcher,
			DefaultCallbackURL:   callback.URL,
			CallbackAllowedHosts: []string{"127.0.0.1"},
			CallbackAllowPrivate: true,
		},
	)

	createResponse := performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":           "req-stalled-runtime-result-file",
		"user_id":              "e2e-main-0416",
		"conversation_id":      "conv-stalled-runtime-result-file",
		"message":              "生成结果文件后不要依赖 replyText 路径。",
		"requires_result_file": true,
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "stall-recovery-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}
	if !dispatcher.WaitIdle(time.Second) {
		t.Fatalf("timed out waiting for callbacks")
	}
	select {
	case <-runtimeExecutor.canceled:
	case <-time.After(time.Second):
		t.Fatalf("runtime executor was not cancelled after result-file recovery")
	}
	if len(southboundFake.completeRequests) != 1 {
		t.Fatalf("complete requests = %#v, want one", southboundFake.completeRequests)
	}
	if got := southboundFake.completeRequests[0].ResultWorkspaceFileIDs; !reflect.DeepEqual(got, []string{"stall-recovered-result"}) {
		t.Fatalf("complete result files = %#v, want stall-recovered-result", got)
	}

	record, ok, err := store.GetTask(context.Background(), created.TaskID)
	if err != nil || !ok {
		t.Fatalf("stored task lookup ok=%v err=%v", ok, err)
	}
	if record.Task.Status != "completed" || record.Task.ResultWorkspaceFileID != "stall-recovered-result" {
		t.Fatalf("stored task result = status %q file %q", record.Task.Status, record.Task.ResultWorkspaceFileID)
	}
	if got := record.Task.Metadata["runtime_reply_text"]; got != "TASK_RESULT_READY" {
		t.Fatalf("stored public runtime reply = %#v, want TASK_RESULT_READY", got)
	}

	completedEvent, ok := delivered.lastWithStatus("completed")
	if !ok {
		t.Fatalf("expected completed callback, got %#v", delivered.snapshot())
	}
	if completedEvent.TaskID != created.TaskID || completedEvent.ReplyText != "### 执行结果\n\nTASK_RESULT_READY" || len(completedEvent.Files) != 1 {
		t.Fatalf("completed callback mismatch: %#v", completedEvent)
	}
	if completedEvent.Files[0].FileID != "stall-recovered-result" {
		t.Fatalf("callback file = %#v, want stall-recovered-result", completedEvent.Files[0])
	}

	taskResponse := performPublicJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var taskPayload core.PublicTaskResponse
	decodeResponse(t, taskResponse, &taskPayload)
	if taskPayload.Status != "completed" || taskPayload.ReplyText != "TASK_RESULT_READY" || len(taskPayload.Files) != 1 {
		t.Fatalf("task payload mismatch: %#v", taskPayload)
	}
	if taskPayload.Files[0].FileID != "stall-recovered-result" {
		t.Fatalf("task file = %#v, want stall-recovered-result", taskPayload.Files[0])
	}

	stats, err := store.TaskQueueStats(context.Background())
	if err != nil {
		t.Fatalf("TaskQueueStats error = %v", err)
	}
	if stats.Completed != 1 || stats.Active != 0 || stats.Retry != 0 {
		t.Fatalf("queue stats = %#v, want completed=1 active=0 retry=0", stats)
	}
}

func TestTaskQueueRuntimeExecutorRecordsResultFileSyncError(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	southboundFake.syncError = errors.New("workspace sync unavailable")
	setFakeAssistantRuntimeStatus(t, southboundFake, "demo-main", "running")
	runtimeExecutor := &fakeRuntimeExecutor{reply: "runtime replied without a registered result file"}
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetRuntimeExecutor(runtimeExecutor)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	createResponse := performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":      "req-runtime-result-sync-error",
		"user_id":         "e2e-main-0416",
		"conversation_id": "conv-runtime-result-sync-error",
		"message":         "生成一个结果文件。",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "test-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}

	getResponse := performPublicJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if fetched.Status != "completed" || fetched.Stage != "done" {
		t.Fatalf("task status/stage = %s/%s, want completed/done", fetched.Status, fetched.Stage)
	}
	if len(fetched.Files) != 0 {
		t.Fatalf("task query files = %#v, want empty", fetched.Files)
	}
	record, ok, err := store.GetTask(context.Background(), created.TaskID)
	if err != nil || !ok {
		t.Fatalf("stored task lookup ok=%v err=%v", ok, err)
	}
	if got := record.Task.Metadata["runtime_result_file_sync_error"]; got != "workspace sync unavailable" {
		t.Fatalf("sync error metadata = %#v", got)
	}
	if got := record.Task.Metadata["runtime_result_file_count"]; got != 0 {
		t.Fatalf("result file count metadata = %#v, want 0", got)
	}
}

func TestTaskQueueRuntimeExecutorFailsRequiredResultFileWhenMissing(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	southboundFake.disableDefaultSyncResult = true
	setFakeAssistantRuntimeStatus(t, southboundFake, "demo-main", "running")
	runtimeExecutor := &fakeRuntimeExecutor{reply: "runtime says the result file is ready"}
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetRuntimeExecutor(runtimeExecutor)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	createResponse := performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":           "req-required-result-file-missing",
		"user_id":              "e2e-main-0416",
		"conversation_id":      "conv-required-result-file-missing",
		"message":              "生成一个结果文件。",
		"requires_result_file": true,
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "test-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}

	getResponse := performPublicJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if fetched.Status != "failed" || fetched.Stage != "error" {
		t.Fatalf("task status/stage = %s/%s, want failed/error", fetched.Status, fetched.Stage)
	}
	if fetched.ErrorCode != "result_file_missing" {
		t.Fatalf("errorCode = %q, want result_file_missing", fetched.ErrorCode)
	}
	if !strings.Contains(fetched.ErrorMessage, "未发现可下载的结果文件") {
		t.Fatalf("errorMessage = %q, want missing result file message", fetched.ErrorMessage)
	}
	if len(fetched.Files) != 0 {
		t.Fatalf("task query files = %#v, want empty", fetched.Files)
	}

	record, ok, err := store.GetTask(context.Background(), created.TaskID)
	if err != nil || !ok {
		t.Fatalf("stored task lookup ok=%v err=%v", ok, err)
	}
	if record.Task.Status != "failed" || record.Task.FailedReasonCode != "result_file_missing" {
		t.Fatalf("stored task status/code = %s/%s, want failed/result_file_missing", record.Task.Status, record.Task.FailedReasonCode)
	}
	if got := record.Task.Metadata["requires_result_file"]; got != true {
		t.Fatalf("requires result file metadata = %#v, want true", got)
	}
	if len(southboundFake.completeRequests) != 0 {
		t.Fatalf("complete request count = %d, want 0", len(southboundFake.completeRequests))
	}
	if len(southboundFake.syncRequests) < 2 {
		t.Fatalf("sync attempts = %d, want retry attempts for required result file", len(southboundFake.syncRequests))
	}
}

func TestTaskQueueRuntimeExecutorRecordsResultFileSyncSummaryErrors(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	southboundFake.disableDefaultSyncResult = true
	southboundFake.syncSummaryErrors = []string{"temp scan failed"}
	setFakeAssistantRuntimeStatus(t, southboundFake, "demo-main", "running")
	runtimeExecutor := &fakeRuntimeExecutor{reply: "runtime replied without a registered result file"}
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetRuntimeExecutor(runtimeExecutor)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	createResponse := performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":      "req-runtime-result-sync-summary-error",
		"user_id":         "e2e-main-0416",
		"conversation_id": "conv-runtime-result-sync-summary-error",
		"message":         "生成一个结果文件。",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "test-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}

	record, ok, err := store.GetTask(context.Background(), created.TaskID)
	if err != nil || !ok {
		t.Fatalf("stored task lookup ok=%v err=%v", ok, err)
	}
	got, _ := record.Task.Metadata["runtime_result_file_sync_error"].(string)
	if !strings.Contains(got, "workspace result sync reported") || !strings.Contains(got, "temp scan failed") {
		t.Fatalf("sync summary error metadata = %#v", record.Task.Metadata["runtime_result_file_sync_error"])
	}
	if got := record.Task.Metadata["runtime_result_file_count"]; got != 0 {
		t.Fatalf("result file count metadata = %#v, want 0", got)
	}
}

func TestPublicChatFilesInjectWorkspaceLookupGuidance(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	southboundFake.files["file-temp-ctx"] = southbound.RawJSON{
		"file_id":       "file-temp-ctx",
		"assistant_id":  "demo-main",
		"user_id":       "e2e-main-0416",
		"filename":      "report.xlsx",
		"content_type":  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
		"size_bytes":    int64(42),
		"bucket":        "temp",
		"relative_path": "temp/uploads/report.xlsx",
		"direction":     "inbound",
	}
	runtimeExecutor := &fakeRuntimeExecutor{reply: "runtime says ok"}
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetRuntimeExecutor(runtimeExecutor)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{AllowPublicUnauthenticated: true}),
		nil,
	)

	createResponse := performPublicJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":      "req-file-context-1",
		"user_id":         "e2e-main-0416",
		"conversation_id": "conv-file-context-1",
		"message":         "请分析附件 report.xlsx",
		"files":           []string{"file-temp-ctx"},
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "test-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}
	if len(runtimeExecutor.calls) != 1 {
		t.Fatalf("runtime calls = %d, want 1", len(runtimeExecutor.calls))
	}
	message := runtimeExecutor.calls[0].Message
	for _, expected := range []string{
		"请分析附件 report.xlsx",
		"Workspace file context",
		"scanning, searching, or reading workspace files",
		"check /workspace/temp first",
		"then /workspace/persistent",
		"file_id=file-temp-ctx",
		"filename=report.xlsx",
		"preferred_path=/workspace/temp/uploads/report.xlsx",
		"fallback_path=/workspace/persistent/uploads/report.xlsx",
	} {
		if !strings.Contains(message, expected) {
			t.Fatalf("runtime message missing %q:\n%s", expected, message)
		}
	}
}

func TestCancelCompletedTaskReturnsConflict(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	southboundFake.assistants["demo-approval"] = southbound.Assistant{
		AssistantID: "demo-approval",
		Name:        "Demo Approval",
		Runtime: southbound.AssistantRuntime{
			Status:       "running",
			GatewayURL:   "http://127.0.0.1:18789",
			GatewayToken: "runtime-token",
		},
	}
	runtimeExecutor := &fakeRuntimeExecutor{reply: "runtime says ok"}
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetRuntimeExecutor(runtimeExecutor)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-cancel-completed",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-cancel-completed",
		"instruction":         "Complete before cancel.",
		"approval_mode":       "none",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "test-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}

	response := performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+created.TaskID+"/cancel", map[string]any{
		"actor_id": "runtime-test",
		"reason":   "late cancel",
	}, http.StatusConflict)
	var payload map[string]map[string]string
	decodeResponse(t, response, &payload)
	if payload["error"]["code"] != "conflict" {
		t.Fatalf("expected conflict error, got %#v", payload)
	}
	if payload["error"]["message"] != "任务已结束，不能取消" {
		t.Fatalf("unexpected conflict message: %#v", payload)
	}
	if strings.Contains(response.Body.String(), "security-service") ||
		strings.Contains(response.Body.String(), "/api/v1/collaboration") {
		t.Fatalf("cancel conflict leaked internal URL: %s", response.Body.String())
	}
}

func TestTaskQueueRuntimeExecutorPreservesStoredReply(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	southboundFake.dropTaskMetaOnGet = true
	southboundFake.assistants["demo-approval"] = southbound.Assistant{
		AssistantID: "demo-approval",
		Name:        "Demo Approval",
		Runtime: southbound.AssistantRuntime{
			Status:       "running",
			GatewayURL:   "http://127.0.0.1:18789",
			GatewayToken: "runtime-token",
		},
	}
	runtimeExecutor := &fakeRuntimeExecutor{reply: "runtime says ok"}
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetRuntimeExecutor(runtimeExecutor)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-runtime-executor-preserve-reply",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-runtime-executor-preserve-reply",
		"instruction":         "Run through the assistant runtime.",
		"approval_mode":       "none",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "test-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}

	getResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if fetched.Status != "completed" || fetched.Stage != "done" {
		t.Fatalf("expected completed task, got %#v", fetched)
	}
	if fetched.ReplyText != "runtime says ok" {
		t.Fatalf("reply_text = %q", fetched.ReplyText)
	}
}

func TestTaskQueueRuntimeExecutorFailsEmptyReply(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	southboundFake.assistants["demo-approval"] = southbound.Assistant{
		AssistantID: "demo-approval",
		Name:        "Demo Approval",
		Runtime: southbound.AssistantRuntime{
			Status:       "running",
			GatewayURL:   "http://127.0.0.1:18789",
			GatewayToken: "runtime-token",
		},
	}
	runtimeExecutor := &fakeRuntimeExecutor{reply: "   "}
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetRuntimeExecutor(runtimeExecutor)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-runtime-executor-empty",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-runtime-executor-empty",
		"instruction":         "Run through the assistant runtime.",
		"approval_mode":       "none",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "test-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}

	getResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if fetched.Status != "failed" || fetched.Stage != "error" {
		t.Fatalf("expected failed task, got %#v", fetched)
	}
	if fetched.ErrorCode != "runtime_empty_reply" {
		t.Fatalf("error_code = %q, want runtime_empty_reply", fetched.ErrorCode)
	}
	if fetched.ReplyText != "" {
		t.Fatalf("reply_text = %q, want empty", fetched.ReplyText)
	}
}

func TestTaskQueueRuntimeExecutorFailsAgentErrorReply(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	southboundFake.assistants["demo-approval"] = southbound.Assistant{
		AssistantID: "demo-approval",
		Name:        "Demo Approval",
		Runtime: southbound.AssistantRuntime{
			Status:       "running",
			GatewayURL:   "http://127.0.0.1:18789",
			GatewayToken: "runtime-token",
		},
	}
	runtimeExecutor := &fakeRuntimeExecutor{reply: "⚠️ Agent failed before reply: No API key found for provider \"openai-codex\""}
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetRuntimeExecutor(runtimeExecutor)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-runtime-executor-agent-error",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-runtime-executor-agent-error",
		"instruction":         "Run through the assistant runtime.",
		"approval_mode":       "none",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "test-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}

	getResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if fetched.Status != "failed" || fetched.Stage != "error" {
		t.Fatalf("expected failed task, got %#v", fetched)
	}
	if fetched.ErrorCode != "runtime_execution_failed" {
		t.Fatalf("error_code = %q, want runtime_execution_failed", fetched.ErrorCode)
	}
	if !strings.Contains(fetched.ErrorMessage, "No API key found") {
		t.Fatalf("error_message = %q, want runtime failure details", fetched.ErrorMessage)
	}
	if fetched.ReplyText != "" {
		t.Fatalf("reply_text = %q, want empty", fetched.ReplyText)
	}
}

func TestPrometheusMetricsEndpoint(t *testing.T) {
	gatewaymetrics.ResetForTest()
	defer gatewaymetrics.ResetForTest()
	gatewaymetrics.RecordTaskCreate("accepted", "processing", "queued", 20*time.Millisecond, false)
	gatewaymetrics.RecordSouthbound("security", http.MethodGet, "/api/v1/test", http.StatusOK, 10*time.Millisecond, false)
	gatewaymetrics.RecordScheduler("selected", "selected_lowest_load", 25, 5*time.Millisecond)
	gatewaymetrics.RecordWebhook("delivered", "task.completed")
	gatewaymetrics.RecordFileTransfer("presigned_upload", 128)
	gatewaymetrics.RecordRuntimeProxy("ui", http.StatusOK, 15*time.Millisecond, false)
	gatewaymetrics.AddRuntimeProxyInFlight("ui", 2)
	gatewaymetrics.RecordRuntimeProxyCircuitOpen("ui")

	store := &dbStatsMemoryStore{
		MemoryStore: taskstore.NewMemoryStore(),
		stats: core.DBPoolStats{
			MaxOpenConnections: 40,
			OpenConnections:    12,
			InUse:              7,
			Idle:               5,
			WaitCount:          9,
			WaitDurationSecs:   0.125,
			MaxIdleClosed:      2,
			MaxIdleTimeClosed:  3,
			MaxLifetimeClosed:  4,
		},
	}
	registry := runtimepool.NewRegistry(5)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID: "demo-approval",
		Status:      "ready",
		Capacity:    5,
		InFlight:    2,
	}); err != nil {
		t.Fatalf("registry heartbeat: %v", err)
	}
	service := core.NewServiceWithQueue(newFakeSouthbound(), registry, store, store)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{
			ScopedAPIKeys: []auth.ScopedAPIKey{{Key: "debug-key", Scopes: []string{"debug"}}},
		}),
		nil,
		RouterOptions{
			RuntimeRegistry: registry,
			DBPoolStatsProviders: map[string]core.DBPoolStatsProvider{
				"callback_outbox": staticDBPoolStatsProvider{stats: core.DBPoolStats{
					MaxOpenConnections: 8,
					OpenConnections:    4,
					InUse:              3,
					Idle:               1,
					WaitCount:          2,
					WaitDurationSecs:   0.05,
				}},
			},
		},
	)

	request := httptest.NewRequest(http.MethodGet, "/api/v1/metrics/prometheus", nil)
	request.Header.Set("X-API-Key", "debug-key")
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d, body=%s", recorder.Code, recorder.Body.String())
	}
	if contentType := recorder.Header().Get("Content-Type"); !strings.Contains(contentType, "text/plain") {
		t.Fatalf("unexpected content type %q", contentType)
	}
	body := recorder.Body.String()
	for _, expected := range []string{
		"go_gateway_health_ready 1",
		"go_gateway_task_queue_active 0",
		"go_gateway_db_pool_max_open_connections 40",
		"go_gateway_db_pool_open_connections 12",
		"go_gateway_db_pool_in_use 7",
		"go_gateway_db_pool_idle 5",
		"go_gateway_db_pool_wait_count 9",
		"go_gateway_db_pool_wait_duration_seconds 0.125",
		"go_gateway_db_pool_max_idle_closed 2",
		"go_gateway_db_pool_max_idle_time_closed 3",
		"go_gateway_db_pool_max_lifetime_closed 4",
		"go_gateway_db_pool_by_pool_max_open_connections{pool=\"primary\"} 40",
		"go_gateway_db_pool_by_pool_max_open_connections{pool=\"callback_outbox\"} 8",
		"go_gateway_db_pool_by_pool_in_use{pool=\"callback_outbox\"} 3",
		"go_gateway_db_pool_by_pool_wait_duration_seconds{pool=\"callback_outbox\"} 0.05",
		"go_gateway_runtime_registered_total 1",
		"go_gateway_runtime_in_flight_total 2",
		"go_gateway_runtime_capacity_total 5",
		"go_gateway_task_create_total{result=\"accepted\",stage=\"queued\",status=\"processing\"} 1",
		"go_gateway_southbound_requests_total{component=\"security\",method=\"GET\",path=\"/api/v1/test\",status_class=\"2xx\"} 1",
		"go_gateway_southbound_latency_seconds_bucket{component=\"security\",le=\"+Inf\",method=\"GET\",path=\"/api/v1/test\",status_class=\"2xx\"} 1",
		"go_gateway_scheduler_decisions_total{outcome=\"selected\",reason=\"selected_lowest_load\"} 1",
		"go_gateway_scheduler_latency_seconds_bucket{le=\"+Inf\",outcome=\"selected\",reason=\"selected_lowest_load\"} 1",
		"go_gateway_webhook_events_total{event_type=\"task.completed\",result=\"delivered\"} 1",
		"go_gateway_file_transfer_bytes_total{direction=\"presigned_upload\"} 128",
		"go_gateway_runtime_proxy_inflight{kind=\"ui\"} 2",
		"go_gateway_runtime_proxy_circuit_open_total{kind=\"ui\"} 1",
		"go_gateway_runtime_proxy_requests_total{kind=\"ui\",status_class=\"2xx\"} 1",
		"go_gateway_runtime_proxy_latency_seconds_bucket{kind=\"ui\",le=\"+Inf\",status_class=\"2xx\"} 1",
	} {
		if !strings.Contains(body, expected) {
			t.Fatalf("prometheus metrics missing %q in:\n%s", expected, body)
		}
	}
}

func TestJSONMetricsEndpointIncludesDBPoolStats(t *testing.T) {
	t.Parallel()

	store := &dbStatsMemoryStore{
		MemoryStore: taskstore.NewMemoryStore(),
		stats: core.DBPoolStats{
			MaxOpenConnections: 40,
			OpenConnections:    8,
			InUse:              6,
			Idle:               2,
			WaitCount:          5,
			WaitDurationSecs:   0.25,
			MaxIdleClosed:      1,
			MaxIdleTimeClosed:  2,
			MaxLifetimeClosed:  3,
		},
	}
	service := core.NewServiceWithQueue(newFakeSouthbound(), nil, store, store)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{
			ScopedAPIKeys: []auth.ScopedAPIKey{{Key: "debug-key", Scopes: []string{"debug"}}},
		}),
		nil,
		RouterOptions{
			DBPoolStatsProviders: map[string]core.DBPoolStatsProvider{
				"callback_outbox": staticDBPoolStatsProvider{stats: core.DBPoolStats{
					MaxOpenConnections: 8,
					OpenConnections:    3,
					InUse:              2,
					Idle:               1,
					WaitCount:          4,
					WaitDurationSecs:   0.125,
				}},
			},
		},
	)

	request := httptest.NewRequest(http.MethodGet, "/api/v1/metrics", nil)
	request.Header.Set("X-API-Key", "debug-key")
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d, body=%s", recorder.Code, recorder.Body.String())
	}
	var body struct {
		DBPool  core.DBPoolStats            `json:"db_pool"`
		DBPools map[string]core.DBPoolStats `json:"db_pools"`
	}
	decodeResponse(t, recorder, &body)
	if body.DBPool.MaxOpenConnections != 40 ||
		body.DBPool.OpenConnections != 8 ||
		body.DBPool.InUse != 6 ||
		body.DBPool.Idle != 2 ||
		body.DBPool.WaitCount != 5 ||
		body.DBPool.WaitDurationSecs != 0.25 ||
		body.DBPool.MaxIdleClosed != 1 ||
		body.DBPool.MaxIdleTimeClosed != 2 ||
		body.DBPool.MaxLifetimeClosed != 3 {
		t.Fatalf("unexpected db_pool stats: %#v", body.DBPool)
	}
	if body.DBPools["primary"].MaxOpenConnections != 40 ||
		body.DBPools["callback_outbox"].MaxOpenConnections != 8 ||
		body.DBPools["callback_outbox"].InUse != 2 ||
		body.DBPools["callback_outbox"].WaitDurationSecs != 0.125 {
		t.Fatalf("unexpected db_pools stats: %#v", body.DBPools)
	}
}

func TestTaskQueueAcceptsWhenNoRuntimeCandidateExists(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	registry := runtimepool.NewRegistry(1)
	service := core.NewServiceWithQueue(newFakeSouthbound(), registry, store, store)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":      "req-no-runtime-yet",
		"user_id":         "e2e-main-0416",
		"input_object_id": "object-no-runtime-yet",
		"instruction":     "Queue until capacity exists.",
		"approval_mode":   "none",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)
	if created.Stage != "queued" {
		t.Fatalf("expected queued stage without runtime capacity, got %q", created.Stage)
	}
}

func TestTaskQueueKeepsNoRuntimeCandidateRetryable(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	registry := runtimepool.NewRegistry(1)
	service := core.NewServiceWithQueue(newFakeSouthbound(), registry, store, store)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":      "req-no-runtime-retry",
		"user_id":         "e2e-main-0416",
		"input_object_id": "object-no-runtime-retry",
		"instruction":     "Retry until a runtime registers.",
		"approval_mode":   "none",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "no-runtime-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   2,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}
	stats, err := store.TaskQueueStats(context.Background())
	if err != nil {
		t.Fatalf("TaskQueueStats error = %v", err)
	}
	if stats.Retry != 1 || stats.Failed != 0 {
		t.Fatalf("expected retry without failure for missing runtime, got %#v", stats)
	}

	getResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if fetched.Status == "failed" {
		t.Fatalf("missing runtime should not fail the gateway task: %#v", fetched)
	}
}

func TestTaskQueueKeepsUnregisteredPreferredTargetRetryable(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	registry := runtimepool.NewRegistry(1)
	service := core.NewServiceWithQueue(newFakeSouthbound(), registry, store, store)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-unregistered-preferred-target",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-missing",
		"input_object_id":     "object-unregistered-preferred-target",
		"instruction":         "Retry until target runtime registers.",
		"approval_mode":       "none",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "unregistered-target-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   2,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}
	stats, err := store.TaskQueueStats(context.Background())
	if err != nil {
		t.Fatalf("TaskQueueStats error = %v", err)
	}
	if stats.Retry != 1 || stats.Failed != 0 {
		t.Fatalf("expected retry without failure for unregistered target, got %#v", stats)
	}

	getResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if fetched.Status == "failed" {
		t.Fatalf("unregistered preferred runtime should not fail the gateway task: %#v", fetched)
	}
}

func TestTaskQueueRefreshesUnregisteredPreferredTarget(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	registry := runtimepool.NewRegistry(1)
	southboundFake := newFakeSouthbound()
	southboundFake.assistants["demo-new"] = southbound.Assistant{
		AssistantID: "demo-new",
		Name:        "Demo New",
		Runtime: southbound.AssistantRuntime{
			ContainerID:   "runtime-demo-new",
			ContainerName: "ironagent-assistant-demo-new",
			Status:        "running",
			GatewayURL:    "http://127.0.0.1:18789",
			GatewayToken:  "runtime-token",
		},
	}
	service := core.NewServiceWithQueue(southboundFake, registry, store, store)
	service.SetRuntimeExecutor(&fakeRuntimeExecutor{reply: "runtime says ok"})
	service.SetRuntimeRefresher(func(ctx context.Context, assistantID string) error {
		assistant, err := southboundFake.GetAssistant(ctx, assistantID)
		if err != nil {
			return err
		}
		now := time.Now().UTC()
		_, err = registry.Heartbeat(runtimepool.RuntimeRecord{
			AssistantID:     assistant.AssistantID,
			RuntimeID:       assistant.Runtime.ContainerID,
			PodName:         assistant.Runtime.ContainerName,
			Status:          assistant.Runtime.Status,
			GatewayURL:      assistant.Runtime.GatewayURL,
			InternalBaseURL: assistant.Runtime.GatewayURL,
			Capacity:        1,
			Capabilities:    []string{"chat"},
			LastHeartbeatAt: &now,
		})
		return err
	})
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-refresh-unregistered-target",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-new",
		"input_object_id":     "object-refresh-unregistered-target",
		"instruction":         "Refresh the target runtime before retrying scheduler selection.",
		"approval_mode":       "none",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "refresh-unregistered-target-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}

	getResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if fetched.Status != "completed" || fetched.Stage != "done" {
		t.Fatalf("expected refreshed runtime task to complete, got %#v", fetched)
	}
	if _, ok := registry.Get("demo-new"); !ok {
		t.Fatalf("expected refreshed runtime to be registered")
	}
}

func TestTaskQueueKeepsCapacityContentionRetryable(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	registry := runtimepool.NewRegistry(1)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:  "demo-approval",
		RuntimeID:    "runtime-demo-approval",
		Status:       "ready",
		Capacity:     1,
		InFlight:     1,
		Capabilities: []string{"chat"},
	}); err != nil {
		t.Fatalf("Heartbeat error = %v", err)
	}
	service := core.NewServiceWithQueue(newFakeSouthbound(), registry, store, store)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-runtime-at-capacity",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-runtime-at-capacity",
		"instruction":         "Queue until runtime capacity frees.",
		"approval_mode":       "none",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	for cycle := 0; cycle < 3; cycle++ {
		if cycle > 0 {
			time.Sleep(5 * time.Millisecond)
		}
		processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
			WorkerID:      "capacity-worker",
			ClaimLimit:    1,
			LeaseDuration: time.Second,
			MaxAttempts:   1,
			RetryDelay:    time.Millisecond,
		})
		if err != nil {
			t.Fatalf("ProcessQueuedTasks cycle %d error = %v", cycle, err)
		}
		if processed != 1 {
			t.Fatalf("cycle %d processed = %d, want 1", cycle, processed)
		}
	}

	stats, err := store.TaskQueueStats(context.Background())
	if err != nil {
		t.Fatalf("TaskQueueStats error = %v", err)
	}
	if stats.Retry != 1 || stats.Failed != 0 {
		t.Fatalf("expected retry without failure for capacity contention, got %#v", stats)
	}

	getResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if fetched.Status == "failed" {
		t.Fatalf("capacity contention should not fail the gateway task: %#v", fetched)
	}
}

func TestTaskQueueSuppressesRepeatedCapacityWaitCallbacks(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	registry := runtimepool.NewRegistry(1)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:  "demo-approval",
		RuntimeID:    "runtime-demo-approval",
		Status:       "ready",
		Capacity:     1,
		InFlight:     1,
		Capabilities: []string{"chat"},
	}); err != nil {
		t.Fatalf("Heartbeat error = %v", err)
	}
	service := core.NewServiceWithQueue(newFakeSouthbound(), registry, store, store)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)
	var mu sync.Mutex
	var updates []core.QueuedTaskProgressUpdate
	service.SetQueuedTaskProgressHook(func(_ context.Context, update core.QueuedTaskProgressUpdate) error {
		mu.Lock()
		defer mu.Unlock()
		updates = append(updates, update)
		return nil
	})

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-runtime-capacity-callback-throttle",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-runtime-capacity-callback-throttle",
		"instruction":         "Only notify capacity wait once while it remains blocked.",
		"approval_mode":       "none",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	options := core.TaskWorkerOptions{
		WorkerID:                     "capacity-callback-worker",
		ClaimLimit:                   1,
		LeaseDuration:                time.Second,
		MaxAttempts:                  1,
		RetryDelay:                   time.Millisecond,
		CapacityWaitMaxAttempts:      10,
		CapacityWaitCallbackInterval: time.Hour,
	}
	for cycle := 0; cycle < 2; cycle++ {
		if cycle > 0 {
			time.Sleep(5 * time.Millisecond)
		}
		processed, err := service.ProcessQueuedTasks(context.Background(), options)
		if err != nil {
			t.Fatalf("ProcessQueuedTasks cycle %d error = %v", cycle, err)
		}
		if processed != 1 {
			t.Fatalf("cycle %d processed = %d, want 1", cycle, processed)
		}
	}

	mu.Lock()
	gotUpdates := append([]core.QueuedTaskProgressUpdate(nil), updates...)
	mu.Unlock()
	if len(gotUpdates) != 1 {
		t.Fatalf("capacity wait callbacks = %d, want 1: %#v", len(gotUpdates), gotUpdates)
	}
	if gotUpdates[0].EventType != "task.retrying" ||
		gotUpdates[0].ErrorCode != "runtime_capacity_unavailable" ||
		!strings.Contains(gotUpdates[0].ReplyText, "capacity_wait_count=1") {
		t.Fatalf("unexpected capacity wait callback: %#v", gotUpdates[0])
	}
	record, ok, err := store.GetTask(context.Background(), created.TaskID)
	if err != nil || !ok {
		t.Fatalf("GetTask ok=%v err=%v", ok, err)
	}
	if got := record.Task.Metadata["queue_capacity_wait_count"]; got != 2 {
		t.Fatalf("queue_capacity_wait_count = %#v, want 2", got)
	}
	if _, ok := record.Task.Metadata["queue_capacity_wait_last_callback_at"]; !ok {
		t.Fatalf("missing queue_capacity_wait_last_callback_at metadata: %#v", record.Task.Metadata)
	}
}

func TestTaskQueueFailsCapacityContentionAfterConfiguredLimit(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	registry := runtimepool.NewRegistry(1)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:  "demo-approval",
		RuntimeID:    "runtime-demo-approval",
		Status:       "ready",
		Capacity:     1,
		InFlight:     1,
		Capabilities: []string{"chat"},
	}); err != nil {
		t.Fatalf("Heartbeat error = %v", err)
	}
	service := core.NewServiceWithQueue(newFakeSouthbound(), registry, store, store)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-runtime-capacity-timeout",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-runtime-capacity-timeout",
		"instruction":         "Fail after capacity stays unavailable.",
		"approval_mode":       "none",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	options := core.TaskWorkerOptions{
		WorkerID:                "capacity-worker",
		ClaimLimit:              1,
		LeaseDuration:           time.Second,
		MaxAttempts:             1,
		RetryDelay:              time.Millisecond,
		CapacityWaitMaxAttempts: 1,
	}
	for cycle := 0; cycle < 2; cycle++ {
		if cycle > 0 {
			time.Sleep(5 * time.Millisecond)
		}
		processed, err := service.ProcessQueuedTasks(context.Background(), options)
		if err != nil {
			t.Fatalf("ProcessQueuedTasks cycle %d error = %v", cycle, err)
		}
		if processed != 1 {
			t.Fatalf("cycle %d processed = %d, want 1", cycle, processed)
		}
	}

	stats, err := store.TaskQueueStats(context.Background())
	if err != nil {
		t.Fatalf("TaskQueueStats error = %v", err)
	}
	if stats.Failed != 1 || stats.Retry != 0 {
		t.Fatalf("expected failed capacity timeout, got %#v", stats)
	}

	getResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if fetched.Status != "failed" {
		t.Fatalf("status = %q, want failed: %#v", fetched.Status, fetched)
	}
	if fetched.ErrorCode != "runtime_capacity_timeout" {
		t.Fatalf("errorCode = %q, want runtime_capacity_timeout", fetched.ErrorCode)
	}
	record, ok, err := store.GetTask(context.Background(), created.TaskID)
	if err != nil || !ok {
		t.Fatalf("GetTask ok=%v err=%v", ok, err)
	}
	if got := record.Task.Metadata["queue_capacity_wait_count"]; got != 2 {
		t.Fatalf("queue_capacity_wait_count = %#v, want 2", got)
	}
}

func TestTaskQueueFortyConcurrentWorkersWithClaimLimitOneCompleteDistinctTasks(t *testing.T) {
	t.Parallel()

	const taskCount = 40
	store := taskstore.NewMemoryStore()
	registry := runtimepool.NewRegistry(taskCount)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:     "demo-main",
		RuntimeID:       "runtime-demo-main",
		Status:          "ready",
		GatewayURL:      "http://127.0.0.1:18789",
		InternalBaseURL: "http://127.0.0.1:18789",
		Capacity:        taskCount,
		Capabilities:    []string{"chat"},
	}); err != nil {
		t.Fatalf("Heartbeat error = %v", err)
	}
	southboundFake := newFakeSouthbound()
	southboundFake.disableDefaultSyncResult = true
	service := core.NewServiceWithQueue(southboundFake, registry, store, store)
	service.SetRuntimeRouteResolver(registry)
	executor := newMultiBlockingRuntimeExecutor("runtime says ok", taskCount)
	service.SetRuntimeExecutor(executor)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	for i := 0; i < taskCount; i++ {
		createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
			"request_id":          fmt.Sprintf("req-concurrent-%d", i),
			"user_id":             "e2e-main-0416",
			"target_assistant_id": "demo-main",
			"input_object_id":     fmt.Sprintf("object-concurrent-%d", i),
			"instruction":         "Run concurrently with claim limit one.",
			"approval_mode":       "none",
		}, http.StatusAccepted)
		var created core.ChatResponse
		decodeResponse(t, createResponse, &created)
	}

	var wg sync.WaitGroup
	errs := make(chan error, taskCount)
	for i := 0; i < taskCount; i++ {
		wg.Add(1)
		go func(worker int) {
			defer wg.Done()
			processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
				WorkerID:      fmt.Sprintf("concurrent-worker-%d", worker),
				ClaimLimit:    1,
				LeaseDuration: 2 * time.Second,
				MaxAttempts:   2,
				RetryDelay:    time.Millisecond,
			})
			if err != nil {
				errs <- err
				return
			}
			if processed != 1 {
				errs <- fmt.Errorf("worker %d processed %d tasks, want 1", worker, processed)
			}
		}(i)
	}

	executor.waitForEntered(t, taskCount, time.Second)
	executor.releaseNow()
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	if got := executor.callCount(); got != taskCount {
		t.Fatalf("runtime calls = %d, want %d", got, taskCount)
	}
	stats, err := store.TaskQueueStats(context.Background())
	if err != nil {
		t.Fatalf("TaskQueueStats error = %v", err)
	}
	if stats.Completed != taskCount || stats.Failed != 0 || stats.Active != 0 {
		t.Fatalf("expected all concurrent tasks completed, got %#v", stats)
	}
}

func TestTaskQueueKeepsUnhealthyRuntimeRetryable(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	registry := runtimepool.NewRegistry(1)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:  "demo-approval",
		RuntimeID:    "runtime-demo-approval",
		Status:       "unhealthy",
		Capacity:     1,
		InFlight:     0,
		Capabilities: []string{"chat"},
	}); err != nil {
		t.Fatalf("Heartbeat error = %v", err)
	}
	service := core.NewServiceWithQueue(newFakeSouthbound(), registry, store, store)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-runtime-unhealthy",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-runtime-unhealthy",
		"instruction":         "Queue until runtime becomes healthy.",
		"approval_mode":       "none",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "unhealthy-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   2,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}

	stats, err := store.TaskQueueStats(context.Background())
	if err != nil {
		t.Fatalf("TaskQueueStats error = %v", err)
	}
	if stats.Retry != 1 || stats.Failed != 0 {
		t.Fatalf("expected retry without failure for unhealthy runtime, got %#v", stats)
	}

	getResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if fetched.Status == "failed" {
		t.Fatalf("unhealthy runtime should not fail the gateway task: %#v", fetched)
	}
}

func TestTaskQueueCancelPreventsWorkerExecution(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-cancel-queued",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-cancel-queued",
		"instruction":         "Cancel before worker.",
		"approval_mode":       "none",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+created.TaskID+"/cancel", map[string]any{
		"actor_id": "queue-test",
		"reason":   "user cancelled before worker",
	}, http.StatusOK)

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "test-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 0 {
		t.Fatalf("processed = %d, want 0", processed)
	}
	if southboundFake.nextTaskID != 0 {
		t.Fatalf("expected no southbound task after queued cancel, got %d", southboundFake.nextTaskID)
	}

	getResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+created.TaskID, nil, http.StatusOK)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if fetched.Status != "cancelled" {
		t.Fatalf("expected cancelled task, got %#v", fetched)
	}
}

func TestTaskQueueCancelActiveSouthboundTaskCancelsQueueRow(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	southboundFake := newFakeSouthbound()
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	now := time.Now().UTC()
	taskID := "queued-active-southbound-cancel"
	southboundTaskID := "task-active-southbound-cancel"
	southboundFake.tasks[southboundTaskID] = southbound.Task{
		ID:                southboundTaskID,
		RootTaskID:        southboundTaskID,
		Status:            "running",
		SourceAssistantID: "demo-main",
		TargetAssistantID: "demo-approval",
		InputObjectID:     "object-active-southbound-cancel",
		Instruction:       "Cancel active southbound task.",
		ApprovalMode:      "none",
		CreatedAt:         sbTime(now),
		StartedAt:         sbTimePtr(now),
	}
	if err := store.UpsertTask(context.Background(), core.GatewayTaskRecord{
		Task: core.TaskView{
			ID:                taskID,
			TargetAssistantID: "demo-approval",
			Status:            "running",
			CreatedAt:         now,
			Metadata: map[string]any{
				"queue_status": "processing",
			},
		},
		SouthboundTaskID: southboundTaskID,
		CreatedAt:        now,
		UpdatedAt:        now,
	}); err != nil {
		t.Fatalf("upsert task: %v", err)
	}
	if err := store.EnqueueTask(context.Background(), core.GatewayTaskQueueItem{
		TaskID: taskID,
		Request: core.ChatRequest{
			RequestID:         "req-active-southbound-cancel",
			SourceAssistantID: "demo-main",
			TargetAssistantID: "demo-approval",
			Message:           "cancel active southbound",
		},
		Status:    "processing",
		CreatedAt: now,
		UpdatedAt: now,
	}); err != nil {
		t.Fatalf("enqueue task: %v", err)
	}
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+taskID+"/cancel", map[string]any{
		"actor_id": "queue-test",
		"reason":   "user cancelled active southbound",
	}, http.StatusOK)

	stats, err := store.TaskQueueStats(context.Background())
	if err != nil {
		t.Fatalf("TaskQueueStats error = %v", err)
	}
	if stats.Cancelled != 1 || stats.Processing != 0 || stats.Active != 0 {
		t.Fatalf("expected active southbound cancel to cancel queue row, got %#v", stats)
	}
	if task := southboundFake.tasks[southboundTaskID]; task.Status != "cancelled" {
		t.Fatalf("expected southbound task cancelled, got %q", task.Status)
	}

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "test-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks after active cancel error = %v", err)
	}
	if processed != 0 {
		t.Fatalf("processed after active cancel = %d, want 0", processed)
	}
	getResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/tasks/"+taskID, nil, http.StatusOK)
	var fetched publicTaskResponse
	decodeResponse(t, getResponse, &fetched)
	if fetched.Status != "cancelled" {
		t.Fatalf("expected cancelled task, got %#v", fetched)
	}
}

func TestTaskQueueCancelReleasesReservedRuntime(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	registry := runtimepool.NewRegistry(1)
	if _, err := registry.Upsert(runtimepool.RuntimeRecord{
		AssistantID:     "demo-main",
		InternalBaseURL: "http://127.0.0.1:18789",
		Status:          "running",
		Capacity:        1,
		InFlight:        1,
	}); err != nil {
		t.Fatalf("upsert runtime: %v", err)
	}
	service := core.NewServiceWithQueue(newFakeSouthbound(), registry, store, store)
	now := time.Now().UTC()
	if err := store.UpsertTask(context.Background(), core.GatewayTaskRecord{
		Task: core.TaskView{
			ID:                "queued-runtime-task",
			TargetAssistantID: "demo-main",
			Status:            "processing",
			CreatedAt:         now,
		},
		CreatedAt: now,
		UpdatedAt: now,
	}); err != nil {
		t.Fatalf("upsert task: %v", err)
	}
	if err := store.EnqueueTask(context.Background(), core.GatewayTaskQueueItem{
		TaskID: "queued-runtime-task",
		Request: core.ChatRequest{
			RequestID:         "req-release-runtime",
			SourceAssistantID: "ironagent-upstream",
			TargetAssistantID: "demo-main",
			Message:           "cancel",
		},
		Status:    "processing",
		CreatedAt: now,
		UpdatedAt: now,
	}); err != nil {
		t.Fatalf("enqueue task: %v", err)
	}
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/queued-runtime-task/cancel", map[string]any{
		"actor_id": "queue-test",
		"reason":   "cleanup stale reservation",
	}, http.StatusOK)

	record, ok := registry.Get("demo-main")
	if !ok || record.InFlight != 0 {
		t.Fatalf("expected queued cancel to release reserved runtime, got %#v", record)
	}
}

func TestTaskQueueBackpressureRejectsNewTasks(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	service := core.NewServiceWithQueue(newFakeSouthbound(), nil, store, store)
	service.SetTaskQueueMaxPending(1)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	payload := map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-backpressure",
		"instruction":         "Queue one.",
		"approval_mode":       "none",
	}
	performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", payload, http.StatusAccepted)
	payload["request_id"] = "req-backpressure-2"
	performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", payload, http.StatusTooManyRequests)
}

func TestTaskQueueAssistantBackpressureRejectsBeforeCallbackValidation(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	now := time.Now().UTC()
	if _, _, err := store.CreateQueuedTask(context.Background(), core.GatewayTaskRecord{
		Task: core.TaskView{
			ID:                "queued-assistant-full",
			TargetAssistantID: "demo-approval",
			Status:            "pending_claim",
			CreatedAt:         now,
		},
		CreatedAt: now,
		UpdatedAt: now,
	}, core.GatewayTaskQueueItem{
		TaskID:    "queued-assistant-full",
		Status:    "queued",
		CreatedAt: now,
	}); err != nil {
		t.Fatalf("CreateQueuedTask() error = %v", err)
	}
	service := core.NewServiceWithQueue(newFakeSouthbound(), nil, store, store)
	service.SetTaskQueueMaxPendingPerAssistant(1)
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	payload := map[string]any{
		"request_id":          "req-assistant-backpressure",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"callback_url":        "not-a-url",
		"input_object_id":     "object-assistant-backpressure",
		"instruction":         "Reject before callback URL validation.",
		"approval_mode":       "none",
	}
	performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", payload, http.StatusTooManyRequests)
}

func TestChatCreateFailsClosedWhenAcceptedCallbackCannotEnqueue(t *testing.T) {
	t.Parallel()

	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"received": true})
	}))
	defer callback.Close()

	store := taskstore.NewMemoryStore()
	dispatcher := webhook.NewDispatcher(webhook.Config{
		Secret:       "callback-secret",
		Store:        &failingEnqueueOutboxStore{MemoryOutboxStore: webhook.NewMemoryOutboxStore()},
		StoreTimeout: time.Second,
	})
	service := core.NewServiceWithQueue(newFakeSouthbound(), nil, store, store)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			Webhooks:             dispatcher,
			PublicBaseURL:        "http://gateway.local",
			CallbackAllowedHosts: []string{"127.0.0.1"},
			CallbackAllowPrivate: true,
		},
	)

	response := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-callback-outbox-down",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-callback-outbox-down",
		"instruction":         "Do not accept without accepted callback.",
		"approval_mode":       "none",
		"callback_url":        callback.URL,
	}, http.StatusServiceUnavailable)
	var payload map[string]map[string]string
	decodeResponse(t, response, &payload)
	if payload["error"]["code"] != "callback_enqueue_failed" {
		t.Fatalf("unexpected error payload %#v", payload)
	}
	stats, err := store.TaskQueueStats(context.Background())
	if err != nil {
		t.Fatalf("TaskQueueStats error = %v", err)
	}
	if stats.Queued != 0 || stats.Cancelled != 1 {
		t.Fatalf("expected queued task to be cancelled after callback failure, got %#v", stats)
	}
}

func TestTaskQueueAdmittingTaskWaitsForActivation(t *testing.T) {
	t.Parallel()

	store := taskstore.NewMemoryStore()
	service := core.NewServiceWithQueue(newFakeSouthbound(), nil, store, store)
	task, err := service.CreateChatTask(context.Background(), core.ChatRequest{
		RequestID:         "req-admitting-waits",
		SourceAssistantID: "demo-main",
		TargetAssistantID: "demo-approval",
		InputObjectID:     "object-admitting-waits",
		Instruction:       "Do not run until accepted callback is durable.",
		ApprovalMode:      "none",
	})
	if err != nil {
		t.Fatalf("CreateChatTask error = %v", err)
	}

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "admitting-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 0 {
		t.Fatalf("processed admitting task = %d, want 0", processed)
	}
	stats, err := store.TaskQueueStats(context.Background())
	if err != nil {
		t.Fatalf("TaskQueueStats error = %v", err)
	}
	if stats.Admitting != 1 || stats.Queued != 0 {
		t.Fatalf("expected admitting task before activation, got %#v", stats)
	}
	if err := service.ActivateQueuedTask(context.Background(), task.ID); err != nil {
		t.Fatalf("ActivateQueuedTask error = %v", err)
	}
	stats, err = store.TaskQueueStats(context.Background())
	if err != nil {
		t.Fatalf("TaskQueueStats error = %v", err)
	}
	if stats.Admitting != 0 || stats.Queued != 1 {
		t.Fatalf("expected queued task after activation, got %#v", stats)
	}
}

func TestTaskQueueWorkerDispatchesProgressCallback(t *testing.T) {
	t.Parallel()

	delivered := &eventCollector{}
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		event, err := decodeCallbackEventFromRequest(r)
		if err != nil {
			t.Errorf("decode callback event: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		delivered.add(event)
		writeJSON(w, http.StatusOK, map[string]any{
			"received": true,
			"taskId":   event.TaskID,
			"handled":  true,
		})
	}))
	defer callback.Close()

	store := taskstore.NewMemoryStore()
	dispatcher := webhook.NewDispatcher(webhook.Config{
		Secret:      "callback-secret",
		MaxAttempts: 1,
		RetryDelay:  time.Millisecond,
	})
	service := core.NewServiceWithQueue(newFakeSouthbound(), nil, store, store)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			Webhooks:             dispatcher,
			PublicBaseURL:        "http://gateway.local",
			CallbackAllowedHosts: []string{"127.0.0.1"},
			CallbackAllowPrivate: true,
		},
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-queued-callback",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-queued-callback",
		"instruction":         "Run queued callback.",
		"approval_mode":       "none",
		"callback_url":        callback.URL,
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)
	if !dispatcher.WaitIdle(time.Second) {
		t.Fatalf("timed out waiting for accepted callback")
	}

	if _, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "callback-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	}); err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if !dispatcher.WaitIdle(time.Second) {
		t.Fatalf("timed out waiting for worker callback")
	}
	deadline := time.Now().Add(time.Second)
	for len(delivered.snapshot()) < 2 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
		_ = dispatcher.WaitIdle(50 * time.Millisecond)
	}
	events := delivered.snapshot()
	if len(events) != 2 {
		t.Fatalf("expected accepted and progress callbacks, got %#v", events)
	}
	if events[0].Status != "processing" || events[0].Stage != "queued" {
		t.Fatalf("unexpected accepted callback %#v", events[0])
	}
	if events[1].Status != "processing" || events[1].Stage != "tool_running" {
		t.Fatalf("unexpected progress callback %#v", events[1])
	}
	if events[1].TaskID != created.TaskID || events[1].Status != "processing" {
		t.Fatalf("unexpected progress callback %#v", events[1])
	}
}

func TestTaskQueueWorkerDispatchesRetryCallback(t *testing.T) {
	t.Parallel()

	delivered := &eventCollector{}
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		event, err := decodeCallbackEventFromRequest(r)
		if err != nil {
			t.Errorf("decode callback event: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		delivered.add(event)
		writeJSON(w, http.StatusOK, map[string]any{
			"received": true,
			"taskId":   event.TaskID,
			"handled":  true,
		})
	}))
	defer callback.Close()

	store := taskstore.NewMemoryStore()
	dispatcher := webhook.NewDispatcher(webhook.Config{
		Secret:      "callback-secret",
		MaxAttempts: 1,
		RetryDelay:  time.Millisecond,
	})
	southboundFake := newFakeSouthbound()
	southboundFake.assistants["demo-approval"] = southbound.Assistant{
		AssistantID: "demo-approval",
		Name:        "Demo Approval",
		Runtime: southbound.AssistantRuntime{
			Status:       "running",
			GatewayURL:   "http://127.0.0.1:18789",
			GatewayToken: "runtime-token",
		},
	}
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetRuntimeExecutor(&fakeRuntimeExecutor{
		errors: []error{context.DeadlineExceeded, context.DeadlineExceeded},
	})
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			Webhooks:             dispatcher,
			PublicBaseURL:        "http://gateway.local",
			CallbackAllowedHosts: []string{"127.0.0.1"},
			CallbackAllowPrivate: true,
		},
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-queued-retry-callback",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-queued-retry-callback",
		"instruction":         "Run queued callback and retry on timeout.",
		"approval_mode":       "none",
		"callback_url":        callback.URL,
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)
	if !dispatcher.WaitIdle(time.Second) {
		t.Fatalf("timed out waiting for accepted callback")
	}

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "retry-callback-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   2,
		RetryDelay:    10 * time.Millisecond,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}

	var retryEvent webhook.Event
	retryOK := false
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		_ = dispatcher.WaitIdle(50 * time.Millisecond)
		for _, event := range delivered.snapshot() {
			if event.EventType == "task.retrying" {
				retryEvent = event
				retryOK = true
				break
			}
		}
		if retryOK {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if !retryOK {
		t.Fatalf("missing retry callback; events=%#v", delivered.snapshot())
	}
	if retryEvent.TaskID != created.TaskID || retryEvent.Status != "retrying" || retryEvent.Stage != "retry_wait" {
		t.Fatalf("unexpected retry callback %#v", retryEvent)
	}
	if retryEvent.ErrorCode != "runtime_execution_failed" ||
		!strings.Contains(retryEvent.ErrorMessage, "context deadline exceeded") ||
		retryEvent.ErrorLocation != "runtime_ws.deadline" ||
		!strings.Contains(retryEvent.ReplyText, "error_location=runtime_ws.deadline") {
		t.Fatalf("retry callback lacks failure detail: %#v", retryEvent)
	}
	if len(retryEvent.Files) != 0 {
		t.Fatalf("retry callback files = %#v, want empty", retryEvent.Files)
	}

	deadline = time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		processed, err = service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
			WorkerID:      "retry-callback-worker-2",
			ClaimLimit:    1,
			LeaseDuration: time.Second,
			MaxAttempts:   2,
			RetryDelay:    10 * time.Millisecond,
		})
		if err != nil {
			t.Fatalf("ProcessQueuedTasks retry error = %v", err)
		}
		if processed == 1 {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if processed != 1 {
		t.Fatalf("processed retry = %d, want 1", processed)
	}

	var failedEvent webhook.Event
	failedOK := false
	deadline = time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		_ = dispatcher.WaitIdle(50 * time.Millisecond)
		for _, event := range delivered.snapshot() {
			if event.EventType == "task.failed" {
				failedEvent = event
				failedOK = true
				break
			}
		}
		if failedOK {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if !failedOK {
		t.Fatalf("missing failed callback; events=%#v", delivered.snapshot())
	}
	if failedEvent.TaskID != created.TaskID || failedEvent.Status != "failed" || failedEvent.Stage != "error" {
		t.Fatalf("unexpected failed callback %#v", failedEvent)
	}
	if failedEvent.ErrorCode != "runtime_execution_failed" ||
		!strings.Contains(failedEvent.ErrorMessage, "context deadline exceeded") ||
		failedEvent.ErrorLocation != "runtime_ws.deadline" {
		t.Fatalf("failed callback lacks structured failure detail: %#v", failedEvent)
	}
	if len(failedEvent.Files) != 0 {
		t.Fatalf("failed callback files = %#v, want empty", failedEvent.Files)
	}
}

func TestTaskQueueWorkerDispatchesRuntimeProgressReplyTextBatches(t *testing.T) {
	t.Parallel()

	delivered := &eventCollector{}
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		event, err := decodeCallbackEventFromRequest(r)
		if err != nil {
			t.Errorf("decode callback event: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		delivered.add(event)
		writeJSON(w, http.StatusOK, map[string]any{
			"received": true,
			"taskId":   event.TaskID,
			"handled":  true,
		})
	}))
	defer callback.Close()

	store := taskstore.NewMemoryStore()
	dispatcher := webhook.NewDispatcher(webhook.Config{
		Secret:      "callback-secret",
		MaxAttempts: 1,
		RetryDelay:  time.Millisecond,
	})
	southboundFake := newFakeSouthbound()
	southboundFake.assistants["demo-approval"] = southbound.Assistant{
		AssistantID: "demo-approval",
		Name:        "Demo Approval",
		Runtime: southbound.AssistantRuntime{
			Status:       "running",
			GatewayURL:   "http://127.0.0.1:18789",
			GatewayToken: "runtime-token",
		},
	}
	runtimeExecutor := &fakeRuntimeExecutor{
		reply: "runtime says ok",
		progressEvents: []core.RuntimeProgressEvent{
			{State: "chat_sent", Event: "chat.send"},
			{State: "ack", Event: "chat.ack"},
			{
				State:    "progress",
				Event:    "tool.progress",
				ToolName: "actual-tool",
				Detail:   "正在读取用户上传文件 token=secret /workspace/temp/input.txt",
			},
			{
				State: "tool_output_read",
				Event: "tool-events",
				Text:  strings.Repeat("读", 160),
			},
			{
				State:  "generated",
				Event:  "file.generated",
				Detail: "写入结果文件",
			},
		},
	}
	service := core.NewServiceWithQueue(southboundFake, nil, store, store)
	service.SetRuntimeExecutor(runtimeExecutor)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			Webhooks:                    dispatcher,
			PublicBaseURL:               "http://gateway.local",
			CallbackAllowedHosts:        []string{"127.0.0.1"},
			CallbackAllowPrivate:        true,
			TaskProgressCallbackEnabled: true,
		},
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-runtime-progress-replytext",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-runtime-progress-replytext",
		"instruction":         "Run queued callback with runtime progress.",
		"approval_mode":       "none",
		"callback_url":        callback.URL,
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)
	if !dispatcher.WaitIdle(time.Second) {
		t.Fatalf("timed out waiting for accepted callback")
	}

	processed, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:                         "runtime-progress-worker",
		ClaimLimit:                       1,
		LeaseDuration:                    time.Second,
		MaxAttempts:                      1,
		RetryDelay:                       time.Millisecond,
		TaskProgressCallbackEnabled:      true,
		TaskProgressCallbackBatchSize:    3,
		TaskProgressCallbackInterval:     time.Hour,
		TaskProgressCallbackStepMaxRunes: 150,
		TaskProgressCallbackStateTTL:     time.Hour,
	})
	if err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if processed != 1 {
		t.Fatalf("processed = %d, want 1", processed)
	}
	if !dispatcher.WaitIdle(time.Second) {
		t.Fatalf("timed out waiting for runtime progress callbacks")
	}

	var (
		progressCallbacks []webhook.Event
		completed         webhook.Event
		completedOK       bool
	)
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		events := delivered.snapshot()
		progressCallbacks = progressCallbacks[:0]
		completedOK = false
		for _, event := range events {
			if event.Status == "processing" && event.Stage == "tool_running" {
				progressCallbacks = append(progressCallbacks, event)
			}
			if event.Status == "completed" {
				completed = event
				completedOK = true
			}
		}
		if len(progressCallbacks) >= 4 && completedOK {
			break
		}
		_ = dispatcher.WaitIdle(50 * time.Millisecond)
		time.Sleep(10 * time.Millisecond)
	}
	if len(progressCallbacks) != 4 {
		t.Fatalf("progress callbacks = %#v, want startup batch plus three whitelisted runtime progress callbacks; all events=%#v", progressCallbacks, delivered.snapshot())
	}
	for _, event := range progressCallbacks {
		if strings.TrimSpace(event.ReplyText) == "" {
			t.Fatalf("progress callback has empty replyText: %#v; all events=%#v", event, delivered.snapshot())
		}
		if !strings.HasSuffix(event.ReplyText, "\n\n") {
			t.Fatalf("progress callback missing markdown block boundary: %q", event.ReplyText)
		}
	}
	wantEventTypes := []string{"task.progress", "tool.progress", "tool.output", "file.generated"}
	for i, want := range wantEventTypes {
		if progressCallbacks[i].EventType != want {
			t.Fatalf("progressCallbacks[%d].EventType = %q, want %q; callbacks=%#v", i, progressCallbacks[i].EventType, want, progressCallbacks)
		}
	}
	firstProgress := progressCallbacks[0].ReplyText
	for _, want := range []string{
		"### 执行进度：第 1-3 步",
		"1. **任务已启动**：运行环境已启动，开始处理请求",
		"2. **请求已发送**：已将用户请求发送给运行时",
		"3. **请求已接收**：运行时已确认接收，正在处理",
	} {
		if !strings.Contains(firstProgress, want) {
			t.Fatalf("first progress callback missing %q: %q", want, firstProgress)
		}
	}
	allProgressText := strings.Join(func() []string {
		out := make([]string, 0, len(progressCallbacks))
		for _, event := range progressCallbacks {
			out = append(out, event.ReplyText)
		}
		return out
	}(), "\n")
	if !strings.Contains(allProgressText, "actual-tool") ||
		!strings.Contains(allProgressText, "tool.output") ||
		!strings.Contains(allProgressText, "file generated") {
		t.Fatalf("unexpected progress batches: %q", allProgressText)
	}
	for _, forbidden := range []string{"southbound_task_id"} {
		if strings.Contains(allProgressText, forbidden) {
			t.Fatalf("progress callbacks leaked filtered internal progress %q: %q", forbidden, allProgressText)
		}
	}
	if strings.Contains(allProgressText, "secret") || strings.Contains(allProgressText, "/workspace/") {
		t.Fatalf("progress batch leaked sensitive content: %q", allProgressText)
	}
	if got := strings.Count(progressCallbacks[2].ReplyText, "读"); got != 150 {
		t.Fatalf("tool output retained %d output runes, want 150: %q", got, progressCallbacks[2].ReplyText)
	}
	if !strings.Contains(allProgressText, "...") {
		t.Fatalf("progress missing truncation ellipsis: %q", allProgressText)
	}
	for _, event := range progressCallbacks {
		if len(event.Files) != 0 {
			t.Fatalf("progress callback files = %#v, want empty", event.Files)
		}
	}
	if !completedOK {
		t.Fatalf("missing completed callback; all events=%#v", delivered.snapshot())
	}
	if completed.ReplyText != "### 执行结果\n\nruntime says ok" {
		t.Fatalf("completed replyText = %q, want markdown final runtime reply", completed.ReplyText)
	}
}

func TestTaskQueueWorkerRecoversCallbackSubscriptionFromStore(t *testing.T) {
	t.Parallel()

	delivered := &eventCollector{}
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		event, err := decodeCallbackEventFromRequest(r)
		if err != nil {
			t.Errorf("decode callback event: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		delivered.add(event)
		writeJSON(w, http.StatusOK, map[string]any{
			"received": true,
			"taskId":   event.TaskID,
			"handled":  true,
		})
	}))
	defer callback.Close()

	store := taskstore.NewMemoryStore()
	service := core.NewServiceWithQueue(newFakeSouthbound(), nil, store, store)
	firstDispatcher := webhook.NewDispatcher(webhook.Config{
		Secret:      "callback-secret",
		MaxAttempts: 1,
		RetryDelay:  time.Millisecond,
	})
	firstRouter := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			Webhooks:             firstDispatcher,
			PublicBaseURL:        "http://gateway.local",
			CallbackAllowedHosts: []string{"127.0.0.1"},
			CallbackAllowPrivate: true,
		},
	)

	createResponse := performJSONRequest(t, firstRouter, http.MethodPost, "/api/v1/chat", map[string]any{
		"request_id":          "req-queued-callback-recovered",
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-queued-callback-recovered",
		"instruction":         "Recover callback subscription after restart.",
		"approval_mode":       "none",
		"callback_url":        callback.URL,
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)
	if !firstDispatcher.WaitIdle(time.Second) {
		t.Fatalf("timed out waiting for accepted callback")
	}

	secondDispatcher := webhook.NewDispatcher(webhook.Config{
		Secret:      "callback-secret",
		MaxAttempts: 1,
		RetryDelay:  time.Millisecond,
	})
	_ = NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			Webhooks:             secondDispatcher,
			PublicBaseURL:        "http://gateway.local",
			CallbackAllowedHosts: []string{"127.0.0.1"},
			CallbackAllowPrivate: true,
		},
	)

	if _, err := service.ProcessQueuedTasks(context.Background(), core.TaskWorkerOptions{
		WorkerID:      "callback-recovery-worker",
		ClaimLimit:    1,
		LeaseDuration: time.Second,
		MaxAttempts:   1,
		RetryDelay:    time.Millisecond,
	}); err != nil {
		t.Fatalf("ProcessQueuedTasks error = %v", err)
	}
	if !secondDispatcher.WaitIdle(time.Second) {
		t.Fatalf("timed out waiting for recovered worker callback")
	}
	deadline := time.Now().Add(time.Second)
	for len(delivered.snapshot()) < 2 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
		_ = secondDispatcher.WaitIdle(50 * time.Millisecond)
	}
	events := delivered.snapshot()
	if len(events) != 2 {
		t.Fatalf("expected accepted and recovered progress callbacks, got %#v", events)
	}
	if events[0].Stage != "queued" || events[1].Stage != "tool_running" {
		t.Fatalf("unexpected recovered callback order %#v", events)
	}
	if events[1].TaskID != created.TaskID {
		t.Fatalf("unexpected recovered callback task id %#v", events[1])
	}
}

func TestRuntimeRegistryRejectsUnreadyPreferredTarget(t *testing.T) {
	t.Parallel()

	registry := runtimepool.NewRegistry(1)
	if _, err := registry.Upsert(runtimepool.RuntimeRecord{
		AssistantID: "demo-approval",
		Status:      "stopped",
		Capacity:    1,
	}); err != nil {
		t.Fatalf("upsert target runtime: %v", err)
	}
	service := core.NewServiceWithScheduler(newFakeSouthbound(), registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-unready",
		"instruction":         "Run the task.",
		"approval_mode":       "none",
	}, http.StatusBadRequest)
}

func TestRuntimeRegistryEndpoints(t *testing.T) {
	t.Parallel()

	registry := runtimepool.NewRegistry(3)
	service := core.NewServiceWithScheduler(newFakeSouthbound(), registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	putResponse := performJSONRequest(t, router, http.MethodPut, "/api/v1/runtime/registry/demo-approval", map[string]any{
		"status":   "running",
		"capacity": 5,
	}, http.StatusOK)
	var updated struct {
		Runtime runtimepool.RuntimeRecord `json:"runtime"`
	}
	decodeResponse(t, putResponse, &updated)
	if updated.Runtime.AssistantID != "demo-approval" || updated.Runtime.Capacity != 5 {
		t.Fatalf("unexpected runtime record %#v", updated.Runtime)
	}

	getResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/runtime/registry/demo-approval", nil, http.StatusOK)
	var fetched struct {
		Runtime runtimepool.RuntimeRecord `json:"runtime"`
	}
	decodeResponse(t, getResponse, &fetched)
	if fetched.Runtime.Status != "running" {
		t.Fatalf("expected running runtime, got %q", fetched.Runtime.Status)
	}

	listResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/runtime/registry", nil, http.StatusOK)
	var listed struct {
		Runtimes []runtimepool.RuntimeRecord `json:"runtimes"`
	}
	decodeResponse(t, listResponse, &listed)
	if len(listed.Runtimes) != 1 {
		t.Fatalf("expected 1 runtime, got %d", len(listed.Runtimes))
	}
}

func TestRuntimeRegistryHeartbeatTTLAndDrain(t *testing.T) {
	t.Parallel()

	registry := runtimepool.NewRegistryWithStore(1, time.Millisecond, nil, nil)
	service := core.NewServiceWithScheduler(newFakeSouthbound(), registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	heartbeatResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/runtime/registry/demo-approval/heartbeat", map[string]any{
		"status":   "running",
		"capacity": 1,
	}, http.StatusOK)
	var heartbeat struct {
		Runtime runtimepool.RuntimeRecord `json:"runtime"`
	}
	decodeResponse(t, heartbeatResponse, &heartbeat)
	if heartbeat.Runtime.LastHeartbeatAt == nil {
		t.Fatalf("expected heartbeat timestamp")
	}

	time.Sleep(2 * time.Millisecond)
	performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-expired-runtime",
		"instruction":         "Run the task.",
		"approval_mode":       "none",
	}, http.StatusBadRequest)

	performJSONRequest(t, router, http.MethodPost, "/api/v1/runtime/registry/demo-approval/heartbeat", map[string]any{
		"status":     "running",
		"capacity":   1,
		"drain_mode": true,
	}, http.StatusOK)
	performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-draining-runtime",
		"instruction":         "Run the task.",
		"approval_mode":       "none",
	}, http.StatusBadRequest)
}

func TestRecordRuntimeClearsRecoveredDrainForRoutableRuntime(t *testing.T) {
	t.Parallel()

	registry := runtimepool.NewRegistry(1)
	if _, err := registry.Upsert(runtimepool.RuntimeRecord{
		AssistantID:     "demo-approval",
		Status:          "running",
		InternalBaseURL: "http://old-runtime:18789",
		GatewayURL:      "http://old-runtime:18789",
		DrainMode:       true,
	}); err != nil {
		t.Fatalf("Upsert() error = %v", err)
	}
	h := &handler{runtimes: registry}

	h.recordRuntime(core.RuntimeView{
		AssistantID: "demo-approval",
		Status:      "running",
		GatewayURL:  "http://new-runtime:18789",
	})

	record, ok := registry.Get("demo-approval")
	if !ok {
		t.Fatalf("runtime missing after recordRuntime")
	}
	if record.DrainMode {
		t.Fatalf("recordRuntime kept recovered drain_mode")
	}
	if record.InternalBaseURL != "http://new-runtime:18789" || record.GatewayURL != "http://new-runtime:18789" {
		t.Fatalf("recordRuntime route mismatch internal=%q gateway=%q", record.InternalBaseURL, record.GatewayURL)
	}
}

func TestRuntimeRegistryRejectsInvalidAssistantID(t *testing.T) {
	t.Parallel()

	registry := runtimepool.NewRegistry(1)
	service := core.NewServiceWithScheduler(newFakeSouthbound(), registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	performJSONRequest(t, router, http.MethodPost, "/api/v1/runtime/registry/bad%2Fid/heartbeat", map[string]any{
		"status":   "running",
		"capacity": 1,
	}, http.StatusBadRequest)
	performJSONRequest(t, router, http.MethodPut, "/api/v1/runtime/registry/bad%2Fid", map[string]any{
		"status":   "running",
		"capacity": 1,
	}, http.StatusBadRequest)
	performJSONRequest(t, router, http.MethodGet, "/api/v1/runtime/registry/bad%2Fid", nil, http.StatusBadRequest)
}

func TestAssistantRuntimeAPIProxyRoutesToRegisteredRuntime(t *testing.T) {
	t.Parallel()

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/models" {
			t.Fatalf("expected rewritten path /v1/models, got %q", r.URL.Path)
		}
		if r.URL.RawQuery != "limit=1" {
			t.Fatalf("expected query to be preserved, got %q", r.URL.RawQuery)
		}
		if r.Header.Get("X-IronAgent-Assistant-ID") != "demo-main" {
			t.Fatalf("expected assistant header, got %q", r.Header.Get("X-IronAgent-Assistant-ID"))
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"assistant": r.Header.Get("X-IronAgent-Assistant-ID"),
			"path":      r.URL.Path,
		})
	}))
	defer upstream.Close()

	registry := runtimepool.NewRegistry(2)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:     "demo-main",
		InternalBaseURL: upstream.URL,
		Status:          "running",
		Capacity:        2,
	}); err != nil {
		t.Fatalf("heartbeat runtime: %v", err)
	}
	service := core.NewServiceWithScheduler(newFakeSouthbound(), registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	response := performJSONRequest(t, router, http.MethodGet, "/assistants/demo-main/api/v1/models?limit=1", nil, http.StatusOK)
	var payload map[string]string
	decodeResponse(t, response, &payload)
	if payload["assistant"] != "demo-main" || payload["path"] != "/v1/models" {
		t.Fatalf("unexpected proxy response %#v", payload)
	}
	record, ok := registry.Get("demo-main")
	if !ok || record.InFlight != 0 {
		t.Fatalf("expected proxy reservation to release, got %#v", record)
	}
}

func TestAssistantRuntimeAPIProxyRejectsWhenProxyLimitReached(t *testing.T) {
	t.Parallel()

	entered := make(chan struct{})
	release := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		close(entered)
		<-release
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
	}))
	defer upstream.Close()

	registry := runtimepool.NewRegistry(10)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:     "demo-main",
		InternalBaseURL: upstream.URL,
		Status:          "running",
		Capacity:        10,
	}); err != nil {
		t.Fatalf("heartbeat runtime: %v", err)
	}
	service := core.NewServiceWithScheduler(newFakeSouthbound(), registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			RuntimeRegistry:  registry,
			ProxyMaxInFlight: 1,
		},
	)

	done := make(chan struct{})
	go func() {
		defer close(done)
		performJSONRequest(t, router, http.MethodGet, "/assistants/demo-main/api/v1/models", nil, http.StatusOK)
	}()
	<-entered

	performJSONRequest(t, router, http.MethodGet, "/assistants/demo-main/api/v1/models", nil, http.StatusTooManyRequests)
	close(release)
	<-done
}

func TestAssistantRuntimeAPIProxyRejectsSelfTarget(t *testing.T) {
	t.Parallel()

	registry := runtimepool.NewRegistry(2)
	service := core.NewServiceWithScheduler(newFakeSouthbound(), registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)
	gateway := httptest.NewServer(router)
	defer gateway.Close()
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID: "demo-main",
		GatewayURL:  gateway.URL,
		Status:      "running",
		Capacity:    2,
	}); err != nil {
		t.Fatalf("heartbeat runtime: %v", err)
	}

	request, err := http.NewRequest(http.MethodGet, gateway.URL+"/assistants/demo-main/api/v1/models", nil)
	if err != nil {
		t.Fatalf("build request: %v", err)
	}
	request.Header.Set("X-API-Key", "dev-key")
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatalf("request gateway: %v", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503", response.StatusCode)
	}
}

func TestAssistantRuntimeAPIProxyCircuitOpensAfterFailures(t *testing.T) {
	t.Parallel()

	hits := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		hits++
		http.Error(w, "runtime failed", http.StatusInternalServerError)
	}))
	defer upstream.Close()

	registry := runtimepool.NewRegistry(10)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:     "demo-main",
		InternalBaseURL: upstream.URL,
		Status:          "running",
		Capacity:        10,
	}); err != nil {
		t.Fatalf("heartbeat runtime: %v", err)
	}
	service := core.NewServiceWithScheduler(newFakeSouthbound(), registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			RuntimeRegistry:          registry,
			ProxyFailureThreshold:    1,
			ProxyCircuitOpenDuration: time.Minute,
		},
	)

	performJSONRequest(t, router, http.MethodGet, "/assistants/demo-main/api/v1/models", nil, http.StatusInternalServerError)
	performJSONRequest(t, router, http.MethodGet, "/assistants/demo-main/api/v1/models", nil, http.StatusServiceUnavailable)
	if hits != 1 {
		t.Fatalf("upstream hits = %d, want 1", hits)
	}
}

func TestAssistantRuntimeWSProxyRoutesToRegisteredRuntime(t *testing.T) {
	t.Parallel()

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/socket/events" {
			t.Fatalf("expected rewritten path /socket/events, got %q", r.URL.Path)
		}
		if r.Header.Get("X-Forwarded-Prefix") != "/assistants/demo-main/ws" {
			t.Fatalf("expected ws forwarded prefix, got %q", r.Header.Get("X-Forwarded-Prefix"))
		}
		writeJSON(w, http.StatusOK, map[string]any{"path": r.URL.Path})
	}))
	defer upstream.Close()

	registry := runtimepool.NewRegistry(2)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:     "demo-main",
		InternalBaseURL: upstream.URL,
		Status:          "running",
		Capacity:        2,
	}); err != nil {
		t.Fatalf("heartbeat runtime: %v", err)
	}
	service := core.NewServiceWithScheduler(newFakeSouthbound(), registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	response := performJSONRequest(t, router, http.MethodGet, "/assistants/demo-main/ws/socket/events", nil, http.StatusOK)
	var payload map[string]string
	decodeResponse(t, response, &payload)
	if payload["path"] != "/socket/events" {
		t.Fatalf("unexpected proxy response %#v", payload)
	}
}

func TestAssistantRuntimeWSProxySupportsUpgrade(t *testing.T) {
	t.Parallel()

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/socket" {
			t.Fatalf("expected rewritten path /socket, got %q", r.URL.Path)
		}
		if !strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
			t.Fatalf("expected websocket upgrade, got %q", r.Header.Get("Upgrade"))
		}
		hijacker, ok := w.(http.Hijacker)
		if !ok {
			t.Fatal("upstream response writer does not support hijack")
		}
		conn, rw, err := hijacker.Hijack()
		if err != nil {
			t.Fatalf("hijack upstream: %v", err)
		}
		defer conn.Close()
		_, _ = rw.WriteString("HTTP/1.1 101 Switching Protocols\r\n")
		_, _ = rw.WriteString("Connection: Upgrade\r\n")
		_, _ = rw.WriteString("Upgrade: websocket\r\n")
		_, _ = rw.WriteString("\r\n")
		if err := rw.Flush(); err != nil {
			t.Fatalf("flush upstream upgrade: %v", err)
		}
	}))
	defer upstream.Close()

	registry := runtimepool.NewRegistry(2)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:     "demo-main",
		InternalBaseURL: upstream.URL,
		Status:          "running",
		Capacity:        2,
	}); err != nil {
		t.Fatalf("heartbeat runtime: %v", err)
	}
	service := core.NewServiceWithScheduler(newFakeSouthbound(), registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)
	gateway := httptest.NewServer(router)
	defer gateway.Close()
	gatewayURL, err := url.Parse(gateway.URL)
	if err != nil {
		t.Fatalf("parse gateway URL: %v", err)
	}
	conn, err := net.Dial("tcp", gatewayURL.Host)
	if err != nil {
		t.Fatalf("dial gateway: %v", err)
	}
	defer conn.Close()
	_, _ = io.WriteString(conn, "GET /assistants/demo-main/ws/socket HTTP/1.1\r\n")
	_, _ = io.WriteString(conn, "Host: "+gatewayURL.Host+"\r\n")
	_, _ = io.WriteString(conn, "X-API-Key: dev-key\r\n")
	_, _ = io.WriteString(conn, "Connection: Upgrade\r\n")
	_, _ = io.WriteString(conn, "Upgrade: websocket\r\n")
	_, _ = io.WriteString(conn, "\r\n")
	response, err := http.ReadResponse(bufio.NewReader(conn), nil)
	if err != nil {
		t.Fatalf("read gateway upgrade response: %v", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusSwitchingProtocols {
		t.Fatalf("status = %d, want 101", response.StatusCode)
	}
	if !strings.EqualFold(response.Header.Get("Upgrade"), "websocket") {
		t.Fatalf("upgrade header = %q, want websocket", response.Header.Get("Upgrade"))
	}
}

func TestAssistantRuntimeWSProxyAcceptsLaunchTicketSession(t *testing.T) {
	t.Parallel()

	upstreamMessages := make(chan []byte, 1)
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/socket" {
			t.Fatalf("expected rewritten path /socket, got %q", r.URL.Path)
		}
		if !strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
			t.Fatalf("expected websocket upgrade, got %q", r.Header.Get("Upgrade"))
		}
		if got := r.Header.Get("Authorization"); got != "Bearer runtime-token" {
			t.Fatalf("expected launch-ticket runtime token injection, got %q", got)
		}
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			t.Fatalf("upgrade upstream websocket: %v", err)
		}
		defer conn.Close()
		_, message, err := conn.ReadMessage()
		if err != nil {
			t.Fatalf("read upstream websocket message: %v", err)
		}
		upstreamMessages <- message
	}))
	defer upstream.Close()

	registry := runtimepool.NewRegistry(2)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:     "demo-main",
		InternalBaseURL: upstream.URL,
		GatewayURL:      "http://127.0.0.1:18789",
		Status:          "running",
		Capacity:        1,
		InFlight:        1,
	}); err != nil {
		t.Fatalf("heartbeat runtime: %v", err)
	}
	southboundFake := newFakeSouthbound()
	ticket := southboundFake.launchTickets["ticket-demo-main"]
	ticket.GatewayURL = upstream.URL
	southboundFake.launchTickets["ticket-demo-main"] = ticket
	service := core.NewServiceWithScheduler(southboundFake, registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{
			ScopedAPIKeys: []auth.ScopedAPIKey{{Key: "proxy-key", Scopes: []string{"proxy"}}},
		}),
		nil,
		RouterOptions{
			RuntimeRegistry:    registry,
			ProxySessionSecret: "proxy-session-secret",
			ProxySessionTTL:    time.Minute,
		},
	)
	cookie := redeemAssistantProxyCookie(t, router, "/assistants/demo-main/ui/?launchTicket=ticket-demo-main")

	gateway := httptest.NewServer(router)
	defer gateway.Close()
	gatewayURL := "ws" + strings.TrimPrefix(gateway.URL, "http") + "/assistants/demo-main/ws/socket"
	headers := http.Header{}
	headers.Set("Cookie", cookie.Name+"="+cookie.Value)
	conn, _, err := websocket.DefaultDialer.Dial(gatewayURL, headers)
	if err != nil {
		t.Fatalf("dial gateway websocket: %v", err)
	}
	defer conn.Close()
	if err := conn.WriteMessage(websocket.TextMessage, []byte(`{"type":"req","method":"connect","params":{"client":"control"}}`)); err != nil {
		t.Fatalf("write gateway websocket message: %v", err)
	}
	select {
	case message := <-upstreamMessages:
		var decoded map[string]any
		if err := json.Unmarshal(message, &decoded); err != nil {
			t.Fatalf("decode upstream message: %v", err)
		}
		params := decoded["params"].(map[string]any)
		auth := params["auth"].(map[string]any)
		if auth["token"] != "runtime-token" {
			t.Fatalf("expected connect auth token injection, got %#v", decoded)
		}
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for upstream websocket message")
	}
	record, ok := registry.Get("demo-main")
	if !ok || record.InFlight != 1 {
		t.Fatalf("expected browser websocket proxy not to consume task capacity, got %#v", record)
	}
}

func TestAssistantRuntimeUIWebSocketProxyAcceptsLaunchTicketSession(t *testing.T) {
	t.Parallel()

	upstreamMessages := make(chan []byte, 1)
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			t.Fatalf("expected rewritten path /, got %q", r.URL.Path)
		}
		if !strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
			t.Fatalf("expected websocket upgrade, got %q", r.Header.Get("Upgrade"))
		}
		if got := r.Header.Get("Authorization"); got != "Bearer runtime-token" {
			t.Fatalf("expected launch-ticket runtime token injection, got %q", got)
		}
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			t.Fatalf("upgrade upstream websocket: %v", err)
		}
		defer conn.Close()
		_, message, err := conn.ReadMessage()
		if err != nil {
			t.Fatalf("read upstream websocket message: %v", err)
		}
		upstreamMessages <- message
	}))
	defer upstream.Close()

	registry := runtimepool.NewRegistry(2)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:     "demo-main",
		InternalBaseURL: upstream.URL,
		GatewayURL:      "http://127.0.0.1:18789",
		Status:          "running",
		Capacity:        1,
		InFlight:        1,
	}); err != nil {
		t.Fatalf("heartbeat runtime: %v", err)
	}
	southboundFake := newFakeSouthbound()
	ticket := southboundFake.launchTickets["ticket-demo-main"]
	ticket.GatewayURL = upstream.URL
	southboundFake.launchTickets["ticket-demo-main"] = ticket
	service := core.NewServiceWithScheduler(southboundFake, registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{
			ScopedAPIKeys: []auth.ScopedAPIKey{{Key: "proxy-key", Scopes: []string{"proxy"}}},
		}),
		nil,
		RouterOptions{
			RuntimeRegistry:    registry,
			ProxySessionSecret: "proxy-session-secret",
			ProxySessionTTL:    time.Minute,
		},
	)
	cookie := redeemAssistantProxyCookie(t, router, "/assistants/demo-main/ui/?launchTicket=ticket-demo-main")

	gateway := httptest.NewServer(router)
	defer gateway.Close()
	gatewayURL := "ws" + strings.TrimPrefix(gateway.URL, "http") + "/assistants/demo-main/ui"
	headers := http.Header{}
	headers.Set("Cookie", cookie.Name+"="+cookie.Value)
	conn, _, err := websocket.DefaultDialer.Dial(gatewayURL, headers)
	if err != nil {
		t.Fatalf("dial gateway websocket: %v", err)
	}
	defer conn.Close()
	if err := conn.WriteMessage(websocket.TextMessage, []byte(`{"type":"req","method":"connect","params":{"client":"control"}}`)); err != nil {
		t.Fatalf("write gateway websocket message: %v", err)
	}
	select {
	case message := <-upstreamMessages:
		var decoded map[string]any
		if err := json.Unmarshal(message, &decoded); err != nil {
			t.Fatalf("decode upstream message: %v", err)
		}
		params := decoded["params"].(map[string]any)
		auth := params["auth"].(map[string]any)
		if auth["token"] != "runtime-token" {
			t.Fatalf("expected connect auth token injection, got %#v", decoded)
		}
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for upstream websocket message")
	}
	record, ok := registry.Get("demo-main")
	if !ok || record.InFlight != 1 {
		t.Fatalf("expected browser websocket proxy not to consume task capacity, got %#v", record)
	}
}

func TestAssistantRuntimeWSProxyStreamsSSE(t *testing.T) {
	t.Parallel()

	release := make(chan struct{})
	var releaseOnce sync.Once
	defer releaseOnce.Do(func() { close(release) })
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/events" {
			t.Fatalf("expected rewritten path /events, got %q", r.URL.Path)
		}
		flusher, ok := w.(http.Flusher)
		if !ok {
			t.Fatal("upstream response writer does not support flush")
		}
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: first\n\n")
		flusher.Flush()
		<-release
		_, _ = io.WriteString(w, "data: second\n\n")
		flusher.Flush()
	}))
	defer upstream.Close()

	registry := runtimepool.NewRegistry(2)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:     "demo-main",
		InternalBaseURL: upstream.URL,
		Status:          "running",
		Capacity:        2,
	}); err != nil {
		t.Fatalf("heartbeat runtime: %v", err)
	}
	service := core.NewServiceWithScheduler(newFakeSouthbound(), registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)
	gateway := httptest.NewServer(router)
	defer gateway.Close()

	request, err := http.NewRequest(http.MethodGet, gateway.URL+"/assistants/demo-main/ws/events", nil)
	if err != nil {
		t.Fatalf("build request: %v", err)
	}
	request.Header.Set("X-API-Key", "dev-key")
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatalf("request gateway: %v", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", response.StatusCode)
	}
	readDone := make(chan string, 1)
	go func() {
		line, _ := bufio.NewReader(response.Body).ReadString('\n')
		readDone <- line
	}()
	select {
	case line := <-readDone:
		if line != "data: first\n" {
			t.Fatalf("first SSE line = %q", line)
		}
		releaseOnce.Do(func() { close(release) })
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for first SSE event")
	}
}

func TestAssistantRuntimeUIProxyRoutesToRegisteredRuntime(t *testing.T) {
	t.Parallel()

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/assets/app.js" {
			t.Fatalf("expected rewritten path /assets/app.js, got %q", r.URL.Path)
		}
		if r.Header.Get("X-Forwarded-Prefix") != "/assistants/demo-main/ui" {
			t.Fatalf("expected ui forwarded prefix, got %q", r.Header.Get("X-Forwarded-Prefix"))
		}
		writeJSON(w, http.StatusOK, map[string]any{"path": r.URL.Path})
	}))
	defer upstream.Close()

	registry := runtimepool.NewRegistry(2)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:     "demo-main",
		InternalBaseURL: upstream.URL,
		Status:          "running",
		Capacity:        2,
	}); err != nil {
		t.Fatalf("heartbeat runtime: %v", err)
	}
	service := core.NewServiceWithScheduler(newFakeSouthbound(), registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	response := performJSONRequest(t, router, http.MethodGet, "/assistants/demo-main/ui/assets/app.js", nil, http.StatusOK)
	var payload map[string]string
	decodeResponse(t, response, &payload)
	if payload["path"] != "/assets/app.js" {
		t.Fatalf("unexpected proxy response %#v", payload)
	}
}

func TestAssistantRuntimeUIProxyInjectsControlAPIBase(t *testing.T) {
	t.Parallel()

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/chat" {
			t.Fatalf("expected rewritten path /chat, got %q", r.URL.Path)
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Content-Security-Policy", "default-src 'self'; script-src 'self'")
		_, _ = w.Write([]byte("<!doctype html><html><head><title>App</title></head><body></body></html>"))
	}))
	defer upstream.Close()

	registry := runtimepool.NewRegistry(2)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:     "demo-main",
		InternalBaseURL: upstream.URL,
		Status:          "running",
		Capacity:        2,
	}); err != nil {
		t.Fatalf("heartbeat runtime: %v", err)
	}
	service := core.NewServiceWithScheduler(newFakeSouthbound(), registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	response := performJSONRequest(t, router, http.MethodGet, "/assistants/demo-main/ui/chat", nil, http.StatusOK)
	body := response.Body.String()
	if !strings.Contains(body, `const apiBase="http://example.com/assistants/demo-main/control"`) ||
		!strings.Contains(body, "globalThis.__IRON_AGENT_API_URL__=apiBase") {
		t.Fatalf("expected UI HTML to include control API base, got %q", body)
	}
	if !strings.Contains(body, "credentials:'same-origin'") {
		t.Fatalf("expected UI HTML to force same-origin credentials for control API calls, got %q", body)
	}
	if !strings.Contains(response.Header().Get("Content-Security-Policy"), "sha256-") {
		t.Fatalf("expected UI CSP to allow injected config script, got %q", response.Header().Get("Content-Security-Policy"))
	}
}

func TestAssistantRuntimeUIProxyAcceptsLaunchTicketSession(t *testing.T) {
	t.Parallel()

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-IronAgent-Assistant-ID") != "demo-main" {
			t.Fatalf("expected assistant header, got %q", r.Header.Get("X-IronAgent-Assistant-ID"))
		}
		writeJSON(w, http.StatusOK, map[string]any{"path": r.URL.Path})
	}))
	defer upstream.Close()

	registry := runtimepool.NewRegistry(2)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:     "demo-main",
		InternalBaseURL: upstream.URL,
		GatewayURL:      "http://127.0.0.1:18789",
		Status:          "running",
		Capacity:        2,
	}); err != nil {
		t.Fatalf("heartbeat runtime: %v", err)
	}
	southboundFake := newFakeSouthbound()
	ticket := southboundFake.launchTickets["ticket-demo-main"]
	ticket.GatewayURL = upstream.URL
	ticket.SessionKey = "agent:demo-main:main"
	southboundFake.launchTickets["ticket-demo-main"] = ticket
	service := core.NewServiceWithScheduler(southboundFake, registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{
			ScopedAPIKeys: []auth.ScopedAPIKey{{Key: "proxy-key", Scopes: []string{"proxy"}}},
		}),
		nil,
		RouterOptions{
			RuntimeRegistry:    registry,
			ProxySessionSecret: "proxy-session-secret",
			ProxySessionTTL:    time.Minute,
		},
	)

	ticketRequest := httptest.NewRequest(http.MethodGet, "/assistants/demo-main/ui/?launchTicket=ticket-demo-main&session=agent%3Astale%3Amain", nil)
	ticketRecorder := httptest.NewRecorder()
	router.ServeHTTP(ticketRecorder, ticketRequest)
	if ticketRecorder.Code != http.StatusFound {
		t.Fatalf("expected launch ticket redirect, got %d body=%s", ticketRecorder.Code, ticketRecorder.Body.String())
	}
	location, err := url.Parse(ticketRecorder.Header().Get("Location"))
	if err != nil {
		t.Fatalf("parse redirect location: %v", err)
	}
	if location.Path != "/assistants/demo-main/ui/" || location.Query().Get("launchTicket") != "" {
		t.Fatalf("expected sanitized UI redirect, got %q", ticketRecorder.Header().Get("Location"))
	}
	if location.Query().Get("session") != "agent:demo-main:main" {
		t.Fatalf("expected UI redirect to include session, got %q", ticketRecorder.Header().Get("Location"))
	}
	if location.Query().Get("ironAgentApiUrl") != "http://example.com/assistants/demo-main/control" {
		t.Fatalf("expected UI redirect query to include gateway API base URL, got %q", ticketRecorder.Header().Get("Location"))
	}
	fragment, err := url.ParseQuery(location.Fragment)
	if err != nil {
		t.Fatalf("parse redirect fragment: %v", err)
	}
	if fragment.Get("token") != "runtime-token" || fragment.Get("ironAgentAssistantId") != "demo-main" {
		t.Fatalf("expected UI redirect to include token and assistant id fragment, got %q", ticketRecorder.Header().Get("Location"))
	}
	if fragment.Get("gatewayUrl") != "ws://example.com/assistants/demo-main/ui" {
		t.Fatalf("expected UI redirect to include gateway URL fragment, got %q", ticketRecorder.Header().Get("Location"))
	}
	if fragment.Get("ironAgentApiUrl") != "http://example.com/assistants/demo-main/control" {
		t.Fatalf("expected UI redirect to include gateway API base URL, got %q", ticketRecorder.Header().Get("Location"))
	}
	cookies := ticketRecorder.Result().Cookies()
	if len(cookies) != 1 || cookies[0].Name != assistantProxySessionCookie {
		t.Fatalf("expected proxy session cookie, got %#v", cookies)
	}
	if cookies[0].MaxAge < 55 {
		t.Fatalf("expected proxy cookie to use session TTL, got max-age %d", cookies[0].MaxAge)
	}

	uiRequest := httptest.NewRequest(http.MethodGet, "/assistants/demo-main/ui/assets/app.js", nil)
	uiRequest.AddCookie(cookies[0])
	uiRecorder := httptest.NewRecorder()
	router.ServeHTTP(uiRecorder, uiRequest)
	if uiRecorder.Code != http.StatusOK {
		t.Fatalf("expected UI proxy with session cookie, got %d body=%s", uiRecorder.Code, uiRecorder.Body.String())
	}
}

func TestAssistantRuntimeUIProxyCookieTTLDoesNotUseRemainingTicketTTL(t *testing.T) {
	t.Parallel()

	registry := runtimepool.NewRegistry(2)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:     "demo-main",
		InternalBaseURL: "http://127.0.0.1:18789",
		GatewayURL:      "http://127.0.0.1:18789",
		Status:          "running",
		Capacity:        2,
	}); err != nil {
		t.Fatalf("heartbeat runtime: %v", err)
	}
	southboundFake := newFakeSouthbound()
	ticket := southboundFake.launchTickets["ticket-demo-main"]
	ticket.ExpiresAt = sbTime(time.Now().UTC().Add(2 * time.Second))
	southboundFake.launchTickets["ticket-demo-main"] = ticket
	service := core.NewServiceWithScheduler(southboundFake, registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{
			ScopedAPIKeys: []auth.ScopedAPIKey{{Key: "proxy-key", Scopes: []string{"proxy"}}},
		}),
		nil,
		RouterOptions{
			RuntimeRegistry:    registry,
			ProxySessionSecret: "proxy-session-secret",
			ProxySessionTTL:    time.Minute,
		},
	)

	ticketRequest := httptest.NewRequest(http.MethodGet, "/assistants/demo-main/ui/?launchTicket=ticket-demo-main", nil)
	ticketRecorder := httptest.NewRecorder()
	router.ServeHTTP(ticketRecorder, ticketRequest)
	if ticketRecorder.Code != http.StatusFound {
		t.Fatalf("expected launch ticket redirect, got %d body=%s", ticketRecorder.Code, ticketRecorder.Body.String())
	}
	cookies := ticketRecorder.Result().Cookies()
	if len(cookies) != 1 || cookies[0].Name != assistantProxySessionCookie {
		t.Fatalf("expected proxy session cookie, got %#v", cookies)
	}
	if cookies[0].MaxAge < 55 {
		t.Fatalf("expected proxy cookie to use configured session TTL, got max-age %d", cookies[0].MaxAge)
	}
}

func TestAssistantRuntimeUIProxyBypassesRuntimeCapacity(t *testing.T) {
	t.Parallel()

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/assets/app.js" {
			t.Fatalf("expected rewritten path /assets/app.js, got %q", r.URL.Path)
		}
		writeJSON(w, http.StatusOK, map[string]any{"path": r.URL.Path})
	}))
	defer upstream.Close()

	registry := runtimepool.NewRegistry(2)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:     "demo-main",
		InternalBaseURL: upstream.URL,
		GatewayURL:      "http://127.0.0.1:18789",
		Status:          "running",
		Capacity:        1,
		InFlight:        1,
	}); err != nil {
		t.Fatalf("heartbeat runtime: %v", err)
	}
	southboundFake := newFakeSouthbound()
	ticket := southboundFake.launchTickets["ticket-demo-main"]
	ticket.GatewayURL = upstream.URL
	southboundFake.launchTickets["ticket-demo-main"] = ticket
	service := core.NewServiceWithScheduler(southboundFake, registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{
			ScopedAPIKeys: []auth.ScopedAPIKey{{Key: "proxy-key", Scopes: []string{"proxy"}}},
		}),
		nil,
		RouterOptions{
			RuntimeRegistry:    registry,
			ProxySessionSecret: "proxy-session-secret",
			ProxySessionTTL:    time.Minute,
		},
	)

	ticketRequest := httptest.NewRequest(http.MethodGet, "/assistants/demo-main/ui/?launchTicket=ticket-demo-main", nil)
	ticketRecorder := httptest.NewRecorder()
	router.ServeHTTP(ticketRecorder, ticketRequest)
	if ticketRecorder.Code != http.StatusFound {
		t.Fatalf("expected launch ticket redirect, got %d body=%s", ticketRecorder.Code, ticketRecorder.Body.String())
	}
	cookies := ticketRecorder.Result().Cookies()
	if len(cookies) != 1 || cookies[0].Name != assistantProxySessionCookie {
		t.Fatalf("expected proxy session cookie, got %#v", cookies)
	}

	uiRequest := httptest.NewRequest(http.MethodGet, "/assistants/demo-main/ui/assets/app.js", nil)
	uiRequest.AddCookie(cookies[0])
	uiRecorder := httptest.NewRecorder()
	router.ServeHTTP(uiRecorder, uiRequest)
	if uiRecorder.Code != http.StatusOK {
		t.Fatalf("expected UI proxy to ignore runtime task capacity, got %d body=%s", uiRecorder.Code, uiRecorder.Body.String())
	}
	record, ok := registry.Get("demo-main")
	if !ok || record.InFlight != 1 {
		t.Fatalf("expected UI proxy not to consume task capacity, got %#v", record)
	}
}

func TestAssistantRuntimeControlBridgeAcceptsLaunchTicketSession(t *testing.T) {
	t.Parallel()

	registry := runtimepool.NewRegistry(2)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:     "demo-main",
		InternalBaseURL: "http://127.0.0.1:18789",
		GatewayURL:      "http://127.0.0.1:18789",
		Status:          "running",
		Capacity:        2,
	}); err != nil {
		t.Fatalf("heartbeat runtime: %v", err)
	}
	service := core.NewServiceWithScheduler(newFakeSouthbound(), registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{
			ScopedAPIKeys: []auth.ScopedAPIKey{{Key: "proxy-key", Scopes: []string{"proxy"}}},
		}),
		nil,
		RouterOptions{
			RuntimeRegistry:    registry,
			ProxySessionSecret: "proxy-session-secret",
			ProxySessionTTL:    time.Minute,
		},
	)

	ticketRequest := httptest.NewRequest(
		http.MethodGet,
		"/assistants/demo-main/control/api/v1/control-ui/assistant-context?launchTicket=ticket-demo-main",
		nil,
	)
	ticketRecorder := httptest.NewRecorder()
	router.ServeHTTP(ticketRecorder, ticketRequest)
	if ticketRecorder.Code != http.StatusFound {
		t.Fatalf("expected launch ticket redirect, got %d body=%s", ticketRecorder.Code, ticketRecorder.Body.String())
	}
	if ticketRecorder.Header().Get("Location") != "/assistants/demo-main/control/api/v1/control-ui/assistant-context" {
		t.Fatalf("expected sanitized control redirect, got %q", ticketRecorder.Header().Get("Location"))
	}
	cookies := ticketRecorder.Result().Cookies()
	if len(cookies) != 1 || cookies[0].Name != assistantProxySessionCookie {
		t.Fatalf("expected proxy session cookie, got %#v", cookies)
	}
	if cookies[0].Path != "/assistants/demo-main/" {
		t.Fatalf("expected assistant cookie path, got %q", cookies[0].Path)
	}

	contextRequest := httptest.NewRequest(
		http.MethodGet,
		"/assistants/demo-main/control/api/v1/control-ui/assistant-context",
		nil,
	)
	contextRequest.AddCookie(cookies[0])
	contextRecorder := httptest.NewRecorder()
	router.ServeHTTP(contextRecorder, contextRequest)
	if contextRecorder.Code != http.StatusOK {
		t.Fatalf("expected control context success, got %d body=%s", contextRecorder.Code, contextRecorder.Body.String())
	}
	var payload map[string][]map[string]string
	decodeResponse(t, contextRecorder, &payload)
	if got := payload["assistants"][0]["id"]; got != "demo-main" {
		t.Fatalf("unexpected assistant context payload %#v", payload)
	}

	staleResolveRequest := httptest.NewRequest(
		http.MethodPost,
		"/assistants/demo-main/control/api/v1/control-ui/launch-tickets/resolve",
		strings.NewReader(`{"ticket":"stale-ticket"}`),
	)
	staleResolveRequest.AddCookie(cookies[0])
	staleResolveRecorder := httptest.NewRecorder()
	router.ServeHTTP(staleResolveRecorder, staleResolveRequest)
	if staleResolveRecorder.Code != http.StatusOK {
		t.Fatalf("expected stale launch ticket bridge success, got %d body=%s", staleResolveRecorder.Code, staleResolveRecorder.Body.String())
	}
	var staleResolvePayload map[string]any
	decodeResponse(t, staleResolveRecorder, &staleResolvePayload)
	if staleResolvePayload["assistant_id"] != "demo-main" || staleResolvePayload["session_key"] != "agent:demo-main:main" {
		t.Fatalf("unexpected stale launch ticket bridge payload %#v", staleResolvePayload)
	}

	apiResolveRequest := httptest.NewRequest(
		http.MethodPost,
		"/assistants/demo-main/api/v1/control-ui/launch-tickets/resolve",
		strings.NewReader(`{"ticket":"stale-ticket"}`),
	)
	apiResolveRequest.AddCookie(cookies[0])
	apiResolveRecorder := httptest.NewRecorder()
	router.ServeHTTP(apiResolveRecorder, apiResolveRequest)
	if apiResolveRecorder.Code != http.StatusOK {
		t.Fatalf("expected assistant api launch ticket bridge success, got %d body=%s", apiResolveRecorder.Code, apiResolveRecorder.Body.String())
	}

	apiWorkspaceRequest := httptest.NewRequest(
		http.MethodGet,
		"/assistants/demo-main/api/v1/workspace-files",
		nil,
	)
	apiWorkspaceRequest.AddCookie(cookies[0])
	apiWorkspaceRecorder := httptest.NewRecorder()
	router.ServeHTTP(apiWorkspaceRecorder, apiWorkspaceRequest)
	if apiWorkspaceRecorder.Code != http.StatusOK {
		t.Fatalf("expected assistant api workspace bridge success, got %d body=%s", apiWorkspaceRecorder.Code, apiWorkspaceRecorder.Body.String())
	}
}

func TestAssistantControlApprovalResolveUsesSessionAssistantScope(t *testing.T) {
	t.Parallel()

	registry := runtimepool.NewRegistry(2)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:     "demo-main",
		InternalBaseURL: "http://127.0.0.1:18789",
		GatewayURL:      "http://127.0.0.1:18789",
		Status:          "running",
		Capacity:        2,
	}); err != nil {
		t.Fatalf("heartbeat runtime: %v", err)
	}
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:     "demo-other",
		InternalBaseURL: "http://127.0.0.1:18790",
		GatewayURL:      "http://127.0.0.1:18790",
		Status:          "running",
		Capacity:        2,
	}); err != nil {
		t.Fatalf("heartbeat other runtime: %v", err)
	}
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:     "demo-approval",
		InternalBaseURL: "http://127.0.0.1:18791",
		GatewayURL:      "http://127.0.0.1:18791",
		Status:          "running",
		Capacity:        2,
	}); err != nil {
		t.Fatalf("heartbeat approval runtime: %v", err)
	}
	southboundFake := newFakeSouthbound()
	southboundFake.assistants["demo-approval"] = southbound.Assistant{
		AssistantID: "demo-approval",
		Name:        "Demo Approval",
		Runtime: southbound.AssistantRuntime{
			ContainerName: "ironagent-assistant-demo-approval",
			Status:        "running",
			GatewayURL:    "http://127.0.0.1:18791",
		},
	}
	southboundFake.launchTickets["ticket-demo-other"] = southbound.ControlUILaunchTicketResolveResponse{
		AssistantID:  "demo-other",
		GatewayURL:   "http://127.0.0.1:18790",
		GatewayToken: "runtime-token-other",
		SessionKey:   "main",
		ExpiresAt:    sbTime(time.Now().UTC().Add(5 * time.Minute)),
	}
	service := core.NewServiceWithOptions(southboundFake, registry, taskstore.NewMemoryStore())
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			RuntimeRegistry:    registry,
			ProxySessionSecret: "proxy-session-secret",
			ProxySessionTTL:    time.Minute,
		},
	)

	mainCookie := resolveLaunchTicketCookie(t, router, "demo-main", "ticket-demo-main")
	otherCookie := resolveLaunchTicketCookie(t, router, "demo-other", "ticket-demo-other")

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-control-approval",
		"instruction":         "Run after approval.",
		"approval_mode":       "user_required",
		"request_id":          "req-control-approval",
	}, http.StatusAccepted)
	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)
	approvalID := southboundFake.tasks[created.TaskID].ApprovalID

	listRequest := httptest.NewRequest(
		http.MethodGet,
		"/assistants/demo-main/control/api/v1/approvals?status=pending&assistant_id=demo-other",
		nil,
	)
	listRequest.AddCookie(mainCookie)
	listRecorder := httptest.NewRecorder()
	router.ServeHTTP(listRecorder, listRequest)
	if listRecorder.Code != http.StatusOK {
		t.Fatalf("expected scoped approval list success, got %d body=%s", listRecorder.Code, listRecorder.Body.String())
	}
	var approvalList []core.ApprovalResponse
	decodeResponse(t, listRecorder, &approvalList)
	if len(approvalList) != 1 || approvalList[0].ApprovalID != approvalID || approvalList[0].AssistantID != "demo-main" {
		t.Fatalf("unexpected scoped approval list payload %#v", approvalList)
	}

	crossScopeRequest := httptest.NewRequest(
		http.MethodPost,
		"/assistants/demo-other/control/api/v1/approvals/"+approvalID+"/resolve",
		strings.NewReader(`{"resolution":"allow"}`),
	)
	crossScopeRequest.AddCookie(otherCookie)
	crossScopeRecorder := httptest.NewRecorder()
	router.ServeHTTP(crossScopeRecorder, crossScopeRequest)
	if crossScopeRecorder.Code != http.StatusForbidden {
		t.Fatalf("expected cross-assistant approval resolve to fail, got %d body=%s", crossScopeRecorder.Code, crossScopeRecorder.Body.String())
	}

	resolveRequest := httptest.NewRequest(
		http.MethodPost,
		"/assistants/demo-main/control/api/v1/approvals/"+approvalID+"/resolve",
		strings.NewReader(`{"resolution":"allow"}`),
	)
	resolveRequest.AddCookie(mainCookie)
	resolveRecorder := httptest.NewRecorder()
	router.ServeHTTP(resolveRecorder, resolveRequest)
	if resolveRecorder.Code != http.StatusOK {
		t.Fatalf("expected scoped approval resolve success, got %d body=%s", resolveRecorder.Code, resolveRecorder.Body.String())
	}
	var payload struct {
		Approval core.ApprovalResponse `json:"approval"`
		Task     *core.TaskResponse    `json:"task"`
	}
	decodeResponse(t, resolveRecorder, &payload)
	if payload.Approval.AssistantID != "demo-main" || payload.Approval.Decision != "approve" {
		t.Fatalf("unexpected approval resolve payload %#v", payload.Approval)
	}
	if payload.Task == nil || payload.Task.Status != "processing" {
		t.Fatalf("expected approval resolve to start task, got %#v", payload.Task)
	}

	replayRequest := httptest.NewRequest(
		http.MethodPost,
		"/assistants/demo-main/control/api/v1/approvals/"+approvalID+"/resolve",
		strings.NewReader(`{"resolution":"allow"}`),
	)
	replayRequest.AddCookie(mainCookie)
	replayRecorder := httptest.NewRecorder()
	router.ServeHTTP(replayRecorder, replayRequest)
	if replayRecorder.Code != http.StatusConflict {
		t.Fatalf("expected already-resolved approval to fail, got %d body=%s", replayRecorder.Code, replayRecorder.Body.String())
	}
}

func resolveLaunchTicketCookie(t *testing.T, router http.Handler, assistantID string, ticket string) *http.Cookie {
	t.Helper()

	request := httptest.NewRequest(
		http.MethodGet,
		"/assistants/"+assistantID+"/control/api/v1/control-ui/assistant-context?launchTicket="+ticket,
		nil,
	)
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusFound {
		t.Fatalf("expected launch ticket redirect for %s, got %d body=%s", assistantID, recorder.Code, recorder.Body.String())
	}
	cookies := recorder.Result().Cookies()
	if len(cookies) != 1 || cookies[0].Name != assistantProxySessionCookie {
		t.Fatalf("expected proxy session cookie for %s, got %#v", assistantID, cookies)
	}
	return cookies[0]
}

func TestInjectConnectAuthToken(t *testing.T) {
	t.Parallel()

	payload := []byte(`{"type":"req","method":"connect","params":{"client":"control"}}`)
	updated := injectConnectAuthToken(payload, "runtime-token")

	var message map[string]any
	if err := json.Unmarshal(updated, &message); err != nil {
		t.Fatalf("decode injected payload: %v", err)
	}
	params, ok := message["params"].(map[string]any)
	if !ok {
		t.Fatalf("expected params object, got %#v", message)
	}
	auth, ok := params["auth"].(map[string]any)
	if !ok {
		t.Fatalf("expected auth object, got %#v", params)
	}
	if auth["token"] != "runtime-token" {
		t.Fatalf("expected injected token, got %#v", auth)
	}
	if params["client"] != "control" {
		t.Fatalf("expected existing params to be preserved, got %#v", params)
	}
}

func TestFilterWorkspaceSkillsStatusFrameUsesWorkspaceDirectoryNames(t *testing.T) {
	t.Parallel()

	payload := []byte(`{
		"type":"res",
		"id":"skills-1",
		"ok":true,
		"payload":{
			"workspaceDir":"/workspace",
			"managedSkillsDir":"/home/node/.ironagent/skills",
			"skills":[
				{
					"name":"1password",
					"source":"ironagent-bundled",
					"baseDir":"/app/skills/1password",
					"filePath":"/app/skills/1password/SKILL.md"
				},
				{
						"name":"excel-agent",
					"source":"ironagent-workspace",
					"baseDir":"/workspace/skills/111",
					"filePath":"/workspace/skills/111/SKILL.md",
					"description":"table skill"
				}
			]
		}
	}`)

	updated := filterWorkspaceSkillsStatusFrame(payload)

	var message map[string]any
	if err := json.Unmarshal(updated, &message); err != nil {
		t.Fatalf("decode filtered payload: %v", err)
	}
	payloadObject, ok := message["payload"].(map[string]any)
	if !ok {
		t.Fatalf("expected payload object, got %#v", message)
	}
	skills, ok := payloadObject["skills"].([]any)
	if !ok || len(skills) != 1 {
		t.Fatalf("expected one workspace skill, got %#v", payloadObject["skills"])
	}
	skill, ok := skills[0].(map[string]any)
	if !ok {
		t.Fatalf("expected skill object, got %#v", skills[0])
	}
	if skill["name"] != "111" {
		t.Fatalf("expected workspace directory name, got %#v", skill)
	}
	if skill["skillKey"] != "111" {
		t.Fatalf("expected skill key to match directory name, got %#v", skill)
	}
	if skill["description"] != "table skill" {
		t.Fatalf("expected skill metadata to be preserved, got %#v", skill)
	}
}

func TestFilterWorkspaceSkillsStatusFrameLeavesOtherFramesUntouched(t *testing.T) {
	t.Parallel()

	payload := []byte(`{"type":"event","event":"chat.delta","payload":{"text":"hi"}}`)

	if updated := filterWorkspaceSkillsStatusFrame(payload); !bytes.Equal(updated, payload) {
		t.Fatalf("expected unrelated frame to be unchanged, got %s", string(updated))
	}
}

func TestAssistantRuntimeControlBridgeHandlesWorkspaceFiles(t *testing.T) {
	t.Parallel()

	registry := runtimepool.NewRegistry(2)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:     "demo-main",
		InternalBaseURL: "http://127.0.0.1:18789",
		GatewayURL:      "http://127.0.0.1:18789",
		Status:          "running",
		Capacity:        2,
	}); err != nil {
		t.Fatalf("heartbeat runtime: %v", err)
	}
	fake := newFakeSouthbound()
	fake.files["skill-1"] = southbound.RawJSON{
		"file_id":       "skill-1",
		"assistant_id":  "demo-main",
		"filename":      "skill.md",
		"bucket":        "skills",
		"relative_path": "skills/skill.md",
	}
	fake.files["plugin-1"] = southbound.RawJSON{
		"file_id":       "plugin-1",
		"assistant_id":  "demo-main",
		"filename":      "plugin.md",
		"bucket":        "plugins",
		"relative_path": "plugins/plugin.md",
	}
	fake.files["temp-1"] = southbound.RawJSON{
		"file_id":       "temp-1",
		"assistant_id":  "demo-main",
		"filename":      "scratch.txt",
		"bucket":        "temp",
		"relative_path": "temp/scratch.txt",
	}
	service := core.NewServiceWithScheduler(fake, registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{
			ScopedAPIKeys: []auth.ScopedAPIKey{{Key: "proxy-key", Scopes: []string{"proxy"}}},
		}),
		nil,
		RouterOptions{
			RuntimeRegistry:    registry,
			ProxySessionSecret: "proxy-session-secret",
			ProxySessionTTL:    time.Minute,
		},
	)

	cookie := redeemAssistantProxyCookie(t, router, "/assistants/demo-main/control/api/v1/control-ui/assistant-context?launchTicket=ticket-demo-main")

	var uploadBody bytes.Buffer
	writer := multipart.NewWriter(&uploadBody)
	if err := writer.WriteField("assistant_id", "ignored-assistant"); err != nil {
		t.Fatalf("write assistant_id field: %v", err)
	}
	if err := writer.WriteField("target_bucket", "persistent"); err != nil {
		t.Fatalf("write bucket field: %v", err)
	}
	if err := writer.WriteField("source_type", "webui-chat"); err != nil {
		t.Fatalf("write source_type field: %v", err)
	}
	part, err := writer.CreateFormFile("file", "evidence.txt")
	if err != nil {
		t.Fatalf("create file part: %v", err)
	}
	if _, err := part.Write([]byte("hello")); err != nil {
		t.Fatalf("write file body: %v", err)
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("close multipart writer: %v", err)
	}

	uploadRequest := httptest.NewRequest(
		http.MethodPost,
		"/assistants/demo-main/control/api/v1/workspace-files/upload",
		bytes.NewReader(uploadBody.Bytes()),
	)
	uploadRequest.Header.Set("Content-Type", writer.FormDataContentType())
	uploadRequest.AddCookie(cookie)
	uploadRecorder := httptest.NewRecorder()
	router.ServeHTTP(uploadRecorder, uploadRequest)
	if uploadRecorder.Code != http.StatusOK {
		t.Fatalf("expected upload success, got %d body=%s", uploadRecorder.Code, uploadRecorder.Body.String())
	}
	var uploaded map[string]any
	decodeResponse(t, uploadRecorder, &uploaded)
	if uploaded["assistant_id"] != "demo-main" {
		t.Fatalf("expected assistant-scoped upload, got %#v", uploaded)
	}

	var readonlyUploadBody bytes.Buffer
	readonlyWriter := multipart.NewWriter(&readonlyUploadBody)
	if err := readonlyWriter.WriteField("target_bucket", "skills"); err != nil {
		t.Fatalf("write readonly bucket field: %v", err)
	}
	readonlyPart, err := readonlyWriter.CreateFormFile("file", "SKILL.md")
	if err != nil {
		t.Fatalf("create readonly upload file part: %v", err)
	}
	if _, err := readonlyPart.Write([]byte("blocked")); err != nil {
		t.Fatalf("write readonly upload body: %v", err)
	}
	if err := readonlyWriter.Close(); err != nil {
		t.Fatalf("close readonly multipart writer: %v", err)
	}
	readonlyUploadRequest := httptest.NewRequest(
		http.MethodPost,
		"/assistants/demo-main/control/api/v1/workspace-files/upload",
		bytes.NewReader(readonlyUploadBody.Bytes()),
	)
	readonlyUploadRequest.Header.Set("Content-Type", readonlyWriter.FormDataContentType())
	readonlyUploadRequest.AddCookie(cookie)
	readonlyUploadRecorder := httptest.NewRecorder()
	router.ServeHTTP(readonlyUploadRecorder, readonlyUploadRequest)
	if readonlyUploadRecorder.Code != http.StatusForbidden {
		t.Fatalf("expected readonly bucket upload forbidden, got %d body=%s", readonlyUploadRecorder.Code, readonlyUploadRecorder.Body.String())
	}

	listRequest := httptest.NewRequest(
		http.MethodGet,
		"/assistants/demo-main/control/api/v1/workspace-files?assistant_id=wrong",
		nil,
	)
	listRequest.AddCookie(cookie)
	listRecorder := httptest.NewRecorder()
	router.ServeHTTP(listRecorder, listRequest)
	if listRecorder.Code != http.StatusOK {
		t.Fatalf("expected list success, got %d body=%s", listRecorder.Code, listRecorder.Body.String())
	}
	var listed map[string]any
	decodeResponse(t, listRecorder, &listed)
	files, ok := listed["files"].([]any)
	if !ok || len(files) != 4 {
		t.Fatalf("expected default list to include workspace and capability buckets, got %#v", listed)
	}
	buckets := map[any]bool{}
	for _, value := range files {
		file, ok := value.(map[string]any)
		if !ok {
			t.Fatalf("unexpected file payload %#v", value)
		}
		bucket := file["bucket"]
		buckets[bucket] = true
	}
	for _, bucket := range []string{"persistent", "temp", "skills", "plugins"} {
		if !buckets[bucket] {
			t.Fatalf("expected default list to include %q bucket, got %#v", bucket, listed)
		}
	}

	skillsListRequest := httptest.NewRequest(
		http.MethodGet,
		"/assistants/demo-main/control/api/v1/workspace-files?bucket=skills",
		nil,
	)
	skillsListRequest.AddCookie(cookie)
	skillsListRecorder := httptest.NewRecorder()
	router.ServeHTTP(skillsListRecorder, skillsListRequest)
	if skillsListRecorder.Code != http.StatusOK {
		t.Fatalf("expected explicit skills bucket list success, got %d body=%s", skillsListRecorder.Code, skillsListRecorder.Body.String())
	}
	var skillsListed map[string]any
	decodeResponse(t, skillsListRecorder, &skillsListed)
	skillsFiles, ok := skillsListed["files"].([]any)
	if !ok || len(skillsFiles) != 1 {
		t.Fatalf("expected explicit skills bucket list to remain available, got %#v", skillsListed)
	}
	if file, ok := skillsFiles[0].(map[string]any); !ok || file["bucket"] != "skills" {
		t.Fatalf("expected skills bucket payload, got %#v", skillsListed)
	}

	deleteSkillRequest := httptest.NewRequest(
		http.MethodPost,
		"/assistants/demo-main/control/api/v1/workspace-files/skill-1/delete",
		strings.NewReader("{}"),
	)
	deleteSkillRequest.Header.Set("Content-Type", "application/json")
	deleteSkillRequest.AddCookie(cookie)
	deleteSkillRecorder := httptest.NewRecorder()
	router.ServeHTTP(deleteSkillRecorder, deleteSkillRequest)
	if deleteSkillRecorder.Code != http.StatusForbidden {
		t.Fatalf("expected skills delete forbidden, got %d body=%s", deleteSkillRecorder.Code, deleteSkillRecorder.Body.String())
	}

	downloadRequest := httptest.NewRequest(
		http.MethodGet,
		"/assistants/demo-main/control/api/v1/workspace-files/file-1/download?disposition=attachment",
		nil,
	)
	downloadRequest.AddCookie(cookie)
	downloadRecorder := httptest.NewRecorder()
	router.ServeHTTP(downloadRecorder, downloadRequest)
	if downloadRecorder.Code != http.StatusOK {
		t.Fatalf("expected download success, got %d body=%s", downloadRecorder.Code, downloadRecorder.Body.String())
	}
	if body := downloadRecorder.Body.String(); body != "hello" {
		t.Fatalf("expected downloaded file bytes, got %q", body)
	}

	deleteRequest := httptest.NewRequest(
		http.MethodPost,
		"/assistants/demo-main/control/api/v1/workspace-files/file-1/delete",
		strings.NewReader("{}"),
	)
	deleteRequest.Header.Set("Content-Type", "application/json")
	deleteRequest.AddCookie(cookie)
	deleteRecorder := httptest.NewRecorder()
	router.ServeHTTP(deleteRecorder, deleteRequest)
	if deleteRecorder.Code != http.StatusOK {
		t.Fatalf("expected delete success, got %d body=%s", deleteRecorder.Code, deleteRecorder.Body.String())
	}

	fake.files["other-file"] = southbound.RawJSON{
		"file_id":      "other-file",
		"assistant_id": "other-assistant",
		"filename":     "other.txt",
		"bucket":       "temp",
	}
	otherRequest := httptest.NewRequest(
		http.MethodGet,
		"/assistants/demo-main/control/api/v1/workspace-files/other-file/download",
		nil,
	)
	otherRequest.AddCookie(cookie)
	otherRecorder := httptest.NewRecorder()
	router.ServeHTTP(otherRecorder, otherRequest)
	if otherRecorder.Code != http.StatusForbidden {
		t.Fatalf("expected cross-assistant file to be forbidden, got %d body=%s", otherRecorder.Code, otherRecorder.Body.String())
	}

	fake.files["orphan-file"] = southbound.RawJSON{
		"file_id":  "orphan-file",
		"filename": "orphan.txt",
		"bucket":   "persistent",
	}
	orphanRequest := httptest.NewRequest(
		http.MethodGet,
		"/assistants/demo-main/control/api/v1/workspace-files/orphan-file/download",
		nil,
	)
	orphanRequest.AddCookie(cookie)
	orphanRecorder := httptest.NewRecorder()
	router.ServeHTTP(orphanRecorder, orphanRequest)
	if orphanRecorder.Code != http.StatusForbidden {
		t.Fatalf("expected orphan file metadata to be forbidden, got %d body=%s", orphanRecorder.Code, orphanRecorder.Body.String())
	}
}

func TestAssistantRuntimeUIProxyRegistersRuntimeFromLaunchTicket(t *testing.T) {
	t.Parallel()

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/assets/app.js" {
			t.Fatalf("expected rewritten path /assets/app.js, got %q", r.URL.Path)
		}
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
	}))
	defer upstream.Close()

	southboundFake := newFakeSouthbound()
	ticket := southboundFake.launchTickets["ticket-demo-main"]
	ticket.GatewayURL = upstream.URL
	southboundFake.launchTickets["ticket-demo-main"] = ticket

	registry := runtimepool.NewRegistry(2)
	service := core.NewServiceWithScheduler(southboundFake, registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{
			ScopedAPIKeys: []auth.ScopedAPIKey{{Key: "proxy-key", Scopes: []string{"proxy"}}},
		}),
		nil,
		RouterOptions{
			RuntimeRegistry:    registry,
			ProxySessionSecret: "proxy-session-secret",
			ProxySessionTTL:    time.Minute,
		},
	)

	ticketRequest := httptest.NewRequest(http.MethodGet, "/assistants/demo-main/ui/?launchTicket=ticket-demo-main", nil)
	ticketRecorder := httptest.NewRecorder()
	router.ServeHTTP(ticketRecorder, ticketRequest)
	if ticketRecorder.Code != http.StatusFound {
		t.Fatalf("expected launch ticket redirect, got %d body=%s", ticketRecorder.Code, ticketRecorder.Body.String())
	}
	record, ok := registry.Get("demo-main")
	if !ok {
		t.Fatalf("expected launch ticket to register runtime")
	}
	if record.InternalBaseURL != upstream.URL || record.GatewayURL != upstream.URL {
		t.Fatalf("expected launch ticket gateway URL to seed runtime, got %#v", record)
	}
	cookies := ticketRecorder.Result().Cookies()
	if len(cookies) != 1 || cookies[0].Name != assistantProxySessionCookie {
		t.Fatalf("expected proxy session cookie, got %#v", cookies)
	}

	uiRequest := httptest.NewRequest(http.MethodGet, "/assistants/demo-main/ui/assets/app.js", nil)
	uiRequest.AddCookie(cookies[0])
	uiRecorder := httptest.NewRecorder()
	router.ServeHTTP(uiRecorder, uiRequest)
	if uiRecorder.Code != http.StatusOK {
		t.Fatalf("expected UI proxy after launch-ticket runtime registration, got %d body=%s", uiRecorder.Code, uiRecorder.Body.String())
	}
}

func TestAssistantRuntimeUIProxyKeepsInternalRuntimeWhenLaunchTicketUsesPublicProxyURL(t *testing.T) {
	t.Parallel()

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/assets/app.js" {
			t.Fatalf("expected rewritten path /assets/app.js, got %q", r.URL.Path)
		}
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
	}))
	defer upstream.Close()

	registry := runtimepool.NewRegistry(2)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:     "demo-main",
		InternalBaseURL: upstream.URL,
		GatewayURL:      upstream.URL,
		Status:          "running",
		Capacity:        2,
	}); err != nil {
		t.Fatalf("heartbeat runtime: %v", err)
	}
	southboundFake := newFakeSouthbound()
	ticket := southboundFake.launchTickets["ticket-demo-main"]
	ticket.GatewayURL = "ws://gateway.example/assistants/demo-main/ws"
	southboundFake.launchTickets["ticket-demo-main"] = ticket
	service := core.NewServiceWithScheduler(southboundFake, registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{
			ScopedAPIKeys: []auth.ScopedAPIKey{{Key: "proxy-key", Scopes: []string{"proxy"}}},
		}),
		nil,
		RouterOptions{
			RuntimeRegistry:    registry,
			ProxySessionSecret: "proxy-session-secret",
			ProxySessionTTL:    time.Minute,
		},
	)

	ticketRequest := httptest.NewRequest(http.MethodGet, "/assistants/demo-main/ui/?launchTicket=ticket-demo-main", nil)
	ticketRecorder := httptest.NewRecorder()
	router.ServeHTTP(ticketRecorder, ticketRequest)
	if ticketRecorder.Code != http.StatusFound {
		t.Fatalf("expected launch ticket redirect, got %d body=%s", ticketRecorder.Code, ticketRecorder.Body.String())
	}
	record, ok := registry.Get("demo-main")
	if !ok {
		t.Fatalf("expected existing runtime to stay registered")
	}
	if record.InternalBaseURL != upstream.URL {
		t.Fatalf("expected public launch URL not to overwrite internal runtime target, got %#v", record)
	}
	if record.GatewayURL != "ws://gateway.example/assistants/demo-main/ws" {
		t.Fatalf("expected launch gateway URL to be recorded for metadata, got %#v", record)
	}

	cookies := ticketRecorder.Result().Cookies()
	if len(cookies) != 1 || cookies[0].Name != assistantProxySessionCookie {
		t.Fatalf("expected proxy session cookie, got %#v", cookies)
	}
	uiRequest := httptest.NewRequest(http.MethodGet, "/assistants/demo-main/ui/assets/app.js", nil)
	uiRequest.AddCookie(cookies[0])
	uiRecorder := httptest.NewRecorder()
	router.ServeHTTP(uiRecorder, uiRequest)
	if uiRecorder.Code != http.StatusOK {
		t.Fatalf("expected UI proxy to keep using internal runtime target, got %d body=%s", uiRecorder.Code, uiRecorder.Body.String())
	}
}

func TestAssistantRuntimeUIProxyNormalizesDirectWebSocketLaunchTicketRuntime(t *testing.T) {
	t.Parallel()

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/assets/app.js" {
			t.Fatalf("expected rewritten path /assets/app.js, got %q", r.URL.Path)
		}
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
	}))
	defer upstream.Close()

	directWebSocketURL := "ws" + strings.TrimPrefix(upstream.URL, "http") + "/ws"
	southboundFake := newFakeSouthbound()
	ticket := southboundFake.launchTickets["ticket-demo-main"]
	ticket.GatewayURL = directWebSocketURL
	southboundFake.launchTickets["ticket-demo-main"] = ticket

	registry := runtimepool.NewRegistry(2)
	service := core.NewServiceWithScheduler(southboundFake, registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{
			ScopedAPIKeys: []auth.ScopedAPIKey{{Key: "proxy-key", Scopes: []string{"proxy"}}},
		}),
		nil,
		RouterOptions{
			RuntimeRegistry:    registry,
			ProxySessionSecret: "proxy-session-secret",
			ProxySessionTTL:    time.Minute,
		},
	)

	ticketRequest := httptest.NewRequest(http.MethodGet, "/assistants/demo-main/ui/?launchTicket=ticket-demo-main", nil)
	ticketRecorder := httptest.NewRecorder()
	router.ServeHTTP(ticketRecorder, ticketRequest)
	if ticketRecorder.Code != http.StatusFound {
		t.Fatalf("expected launch ticket redirect, got %d body=%s", ticketRecorder.Code, ticketRecorder.Body.String())
	}
	record, ok := registry.Get("demo-main")
	if !ok {
		t.Fatalf("expected launch ticket to register runtime")
	}
	if record.InternalBaseURL != upstream.URL || record.GatewayURL != directWebSocketURL {
		t.Fatalf("expected websocket launch URL to normalize internal target, got %#v", record)
	}

	cookies := ticketRecorder.Result().Cookies()
	if len(cookies) != 1 || cookies[0].Name != assistantProxySessionCookie {
		t.Fatalf("expected proxy session cookie, got %#v", cookies)
	}
	uiRequest := httptest.NewRequest(http.MethodGet, "/assistants/demo-main/ui/assets/app.js", nil)
	uiRequest.AddCookie(cookies[0])
	uiRecorder := httptest.NewRecorder()
	router.ServeHTTP(uiRecorder, uiRequest)
	if uiRecorder.Code != http.StatusOK {
		t.Fatalf("expected UI proxy after websocket launch-ticket runtime registration, got %d body=%s", uiRecorder.Code, uiRecorder.Body.String())
	}
}

func TestAssistantRuntimeUIProxyRequiresTicketSessionOrAPIKey(t *testing.T) {
	t.Parallel()

	registry := runtimepool.NewRegistry(2)
	service := core.NewServiceWithScheduler(newFakeSouthbound(), registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{
			ScopedAPIKeys: []auth.ScopedAPIKey{{Key: "proxy-key", Scopes: []string{"proxy"}}},
		}),
		nil,
		RouterOptions{
			RuntimeRegistry:    registry,
			ProxySessionSecret: "proxy-session-secret",
			ProxySessionTTL:    time.Minute,
		},
	)

	request := httptest.NewRequest(http.MethodGet, "/assistants/demo-main/ui/assets/app.js", nil)
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusUnauthorized {
		t.Fatalf("expected missing UI credentials to return 401, got %d body=%s", recorder.Code, recorder.Body.String())
	}
}

func TestAssistantRuntimeAPIProxyRejectsUISessionCookieWithoutProxyKey(t *testing.T) {
	t.Parallel()

	registry := runtimepool.NewRegistry(2)
	service := core.NewServiceWithScheduler(newFakeSouthbound(), registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{
			ScopedAPIKeys: []auth.ScopedAPIKey{{Key: "proxy-key", Scopes: []string{"proxy"}}},
		}),
		nil,
		RouterOptions{
			RuntimeRegistry:    registry,
			ProxySessionSecret: "proxy-session-secret",
			ProxySessionTTL:    time.Minute,
		},
	)

	request := httptest.NewRequest(http.MethodGet, "/assistants/demo-main/api/v1/models", nil)
	request.AddCookie(&http.Cookie{
		Name:  assistantProxySessionCookie,
		Value: proxySessionConfig{secret: []byte("proxy-session-secret")}.sign("demo-main", "session-key", time.Now().Add(time.Minute)),
	})
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusUnauthorized {
		t.Fatalf("expected UI session cookie to be rejected for API proxy, got %d body=%s", recorder.Code, recorder.Body.String())
	}
}

func TestAssistantRuntimeAPIProxyRejectsUnavailableRuntime(t *testing.T) {
	t.Parallel()

	registry := runtimepool.NewRegistry(1)
	if _, err := registry.Heartbeat(runtimepool.RuntimeRecord{
		AssistantID:     "demo-main",
		InternalBaseURL: "http://127.0.0.1:1",
		Status:          "unhealthy",
		Capacity:        1,
	}); err != nil {
		t.Fatalf("heartbeat runtime: %v", err)
	}
	service := core.NewServiceWithScheduler(newFakeSouthbound(), registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	performJSONRequest(t, router, http.MethodGet, "/assistants/demo-main/api/v1/models", nil, http.StatusServiceUnavailable)
	performJSONRequest(t, router, http.MethodGet, "/assistants/missing/api/v1/models", nil, http.StatusNotFound)
}

func TestAssistantRuntimeAPIProxyRejectsInvalidPath(t *testing.T) {
	t.Parallel()

	registry := runtimepool.NewRegistry(1)
	service := core.NewServiceWithScheduler(newFakeSouthbound(), registry)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{RuntimeRegistry: registry},
	)

	performJSONRequest(t, router, http.MethodGet, "/assistants/demo%20main/api/v1/models", nil, http.StatusBadRequest)
	if validAssistantID("demo main") {
		t.Fatalf("expected assistant id with spaces to be invalid")
	}
	if !containsPathTraversal("/../secret") {
		t.Fatalf("expected traversal helper to reject parent path segment")
	}
}

func TestWebhookDispatchAckAndDeadLetter(t *testing.T) {
	t.Parallel()

	delivered := &eventCollector{}
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		event, err := decodeCallbackEventFromRequest(r)
		if err != nil {
			t.Errorf("decode callback event: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		delivered.add(event)
		writeJSON(w, http.StatusOK, map[string]any{
			"received": true,
			"taskId":   event.TaskID,
			"handled":  true,
		})
	}))
	defer callback.Close()

	dispatcher := webhook.NewDispatcher(webhook.Config{
		Secret:      "callback-secret",
		MaxAttempts: 1,
		RetryDelay:  time.Millisecond,
	})
	service := core.NewService(newFakeSouthbound())
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			Webhooks:             dispatcher,
			CallbackAllowedHosts: []string{"127.0.0.1"},
			CallbackAllowPrivate: true,
		},
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-webhook",
		"message":             "Run the task.",
		"approval_mode":       "none",
		"callback_url":        callback.URL,
		"request_id":          "req-1",
		"conversation_id":     "conv-1",
		"user_id":             "user-1",
		"employee_no":         "emp-1",
	}, http.StatusAccepted)

	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)
	if !dispatcher.WaitIdle(time.Second) {
		t.Fatalf("timed out waiting for callbacks")
	}
	events := delivered.snapshot()
	if len(events) != 2 {
		t.Fatalf("expected accepted and progress callbacks, got %d", len(events))
	}
	if events[0].Status != "processing" || events[0].Stage != "queued" {
		t.Fatalf("unexpected accepted callback %#v", events[0])
	}
	if events[1].Status != "processing" || events[1].Stage != "tool_running" {
		t.Fatalf("unexpected progress callback %#v", events[1])
	}
	if events[0].TaskID != created.TaskID || events[0].Status != "processing" {
		t.Fatalf("unexpected callback contract payload %#v", events[0])
	}

	performJSONRequest(t, router, http.MethodPost, "/api/v1/tasks/"+created.TaskID+"/cancel", map[string]any{
		"actor_id": "webhook-test",
		"reason":   "done",
	}, http.StatusOK)
	if !dispatcher.WaitIdle(time.Second) {
		t.Fatalf("timed out waiting for cancellation callback")
	}
	events = delivered.snapshot()
	if len(events) != 3 {
		t.Fatalf("expected cancellation callback, got %d events", len(events))
	}
	if events[2].Status != "cancelled" || events[2].Stage != "blocked" {
		t.Fatalf("expected cancelled callback to expose cancelled/blocked status, got %#v", events[2])
	}

	records := dispatcher.Records()
	if len(records) == 0 {
		t.Fatalf("expected internal webhook records")
	}
	ackEventID := records[0].Event.EventID
	ackResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/webhooks/ack", webhook.ACK{
		Received: true,
		EventID:  ackEventID,
		TaskID:   created.TaskID,
		Handled:  true,
	}, http.StatusOK)
	var acked struct {
		ACK webhook.ACK `json:"ack"`
	}
	decodeResponse(t, ackResponse, &acked)
	if acked.ACK.EventID != ackEventID {
		t.Fatalf("expected ack for %s, got %s", ackEventID, acked.ACK.EventID)
	}
	performJSONRequest(t, router, http.MethodPost, "/api/v1/webhooks/ack", webhook.ACK{
		Received: true,
		EventID:  ackEventID,
		TaskID:   "wrong-task",
		Handled:  true,
	}, http.StatusBadRequest)

	failingCallback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		dropHTTPConnection(w)
	}))
	defer failingCallback.Close()

	performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-dead-letter",
		"message":             "Run the failing callback task.",
		"approval_mode":       "none",
		"callback_url":        failingCallback.URL,
	}, http.StatusAccepted)
	if !dispatcher.WaitIdle(time.Second) {
		t.Fatalf("timed out waiting for failing callback")
	}

	deadLetterResponse := performJSONRequest(t, router, http.MethodGet, "/api/v1/webhooks/dead-letter", nil, http.StatusOK)
	var deadLetters struct {
		DeadLetters []webhook.DeadLetter `json:"dead_letters"`
	}
	decodeResponse(t, deadLetterResponse, &deadLetters)
	if len(deadLetters.DeadLetters) == 0 {
		t.Fatalf("expected dead-letter entries")
	}
}

func TestApprovalResolveDispatchesResolvedAndProgressCallbacks(t *testing.T) {
	t.Parallel()

	delivered := &eventCollector{}
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		event, err := decodeCallbackEventFromRequest(r)
		if err != nil {
			t.Errorf("decode callback event: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		delivered.add(event)
		writeJSON(w, http.StatusOK, map[string]any{
			"received": true,
			"taskId":   event.TaskID,
			"handled":  true,
		})
	}))
	defer callback.Close()

	dispatcher := webhook.NewDispatcher(webhook.Config{
		Secret:      "callback-secret",
		MaxAttempts: 1,
		RetryDelay:  time.Millisecond,
	})
	service := core.NewService(newFakeSouthbound())
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			Webhooks:             dispatcher,
			CallbackAllowedHosts: []string{"127.0.0.1"},
			CallbackAllowPrivate: true,
		},
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-approval-webhook",
		"message":             "Run after approval.",
		"approval_mode":       "user_required",
		"callback_url":        callback.URL,
	}, http.StatusAccepted)

	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)
	if !dispatcher.WaitIdle(time.Second) {
		t.Fatalf("timed out waiting for initial approval callbacks")
	}
	events := delivered.snapshot()
	if len(events) != 2 {
		t.Fatalf("expected accepted and approval_required callbacks, got %d", len(events))
	}

	performJSONRequest(
		t,
		router,
		http.MethodPost,
		"/api/v1/tasks/"+created.TaskID+"/approvals/resolve",
		map[string]any{"resolution": "allow"},
		http.StatusOK,
	)
	if !dispatcher.WaitIdle(time.Second) {
		t.Fatalf("timed out waiting for approval resolved callbacks")
	}
	events = delivered.snapshot()
	if len(events) != 4 {
		t.Fatalf("expected approval resolved and progress callbacks, got %d events", len(events))
	}
	if events[2].TaskID != created.TaskID {
		t.Fatalf("expected resolved callback for task %s, got %#v", created.TaskID, events[2])
	}
	if events[3].Status != "processing" || events[3].Stage != "tool_running" {
		t.Fatalf("expected progress callback, got %#v", events[3])
	}
}

func TestCallbackURLRejectsUnallowedHost(t *testing.T) {
	t.Parallel()

	service := core.NewService(newFakeSouthbound())
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			Webhooks:             webhook.NewDispatcher(webhook.Config{Secret: "callback-secret"}),
			CallbackAllowedHosts: []string{"xiaoxiongmao.example.com"},
		},
	)

	performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-bad-callback",
		"message":             "Run the task.",
		"approval_mode":       "none",
		"callback_url":        "http://127.0.0.1:12345/callback",
	}, http.StatusBadRequest)
}

func TestCallbackURLRejectsPrivateHostByDefault(t *testing.T) {
	t.Parallel()

	service := core.NewService(newFakeSouthbound())
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			Webhooks:             webhook.NewDispatcher(webhook.Config{Secret: "callback-secret"}),
			CallbackAllowedHosts: []string{"127.0.0.1"},
		},
	)

	performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-private-callback",
		"message":             "Run the task.",
		"approval_mode":       "none",
		"callback_url":        "http://127.0.0.1:12345/callback",
	}, http.StatusBadRequest)
}

func TestCallbackURLRejectsHostResolvingToPrivateAddress(t *testing.T) {
	t.Parallel()

	service := core.NewService(newFakeSouthbound())
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			CallbackAllowedHosts: []string{"xiaoxiongmao.example.com"},
			CallbackIPLookup: func(context.Context, string) ([]net.IP, error) {
				return []net.IP{net.ParseIP("10.0.0.8")}, nil
			},
		},
	)

	performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-private-dns-callback",
		"message":             "Run the task.",
		"approval_mode":       "none",
		"callback_url":        "https://xiaoxiongmao.example.com/callback",
	}, http.StatusBadRequest)
}

func TestCallbackURLAllowsHostResolvingToPublicAddress(t *testing.T) {
	t.Parallel()

	service := core.NewService(newFakeSouthbound())
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			CallbackAllowedHosts: []string{"xiaoxiongmao.example.com"},
			CallbackIPLookup: func(context.Context, string) ([]net.IP, error) {
				return []net.IP{net.ParseIP("203.0.113.8")}, nil
			},
		},
	)

	performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-public-dns-callback",
		"message":             "Run the task.",
		"approval_mode":       "none",
		"callback_url":        "https://xiaoxiongmao.example.com/callback",
	}, http.StatusAccepted)
}

func TestPresignedFileUploadAndDownload(t *testing.T) {
	t.Parallel()

	service := core.NewService(newFakeSouthbound())
	presigner := presign.NewManager(presign.Config{Secret: "presign-secret"})
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{Presigns: presigner},
	)

	uploadURLResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/files/upload-url", map[string]any{
		"assistant_id":  "demo-main",
		"target_bucket": "temp",
		"filename":      "presigned.txt",
		"content_type":  "text/plain",
	}, http.StatusOK)
	var uploadURL presign.URLResponse
	decodeResponse(t, uploadURLResponse, &uploadURL)
	if uploadURL.Method != "PUT" {
		t.Fatalf("expected PUT upload URL, got %q", uploadURL.Method)
	}

	uploadPath := mustURLPath(t, uploadURL.URL)
	uploadRequest := httptest.NewRequest(http.MethodPut, uploadPath, bytes.NewReader([]byte("hello presign")))
	uploadRequest.Header.Set("Content-Type", "text/plain")
	uploadRecorder := httptest.NewRecorder()
	router.ServeHTTP(uploadRecorder, uploadRequest)
	if uploadRecorder.Code != http.StatusOK {
		t.Fatalf("expected presigned upload 200, got %d, body=%s", uploadRecorder.Code, uploadRecorder.Body.String())
	}
	var uploaded fileMetadataResponse
	decodeResponse(t, uploadRecorder, &uploaded)
	if uploaded.Size != int64(len("hello presign")) {
		t.Fatalf("uploaded size = %d, want %d", uploaded.Size, len("hello presign"))
	}

	downloadURLResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/files/download-url", map[string]any{
		"file_id": "file-1",
	}, http.StatusOK)
	var downloadURL presign.URLResponse
	decodeResponse(t, downloadURLResponse, &downloadURL)
	if downloadURL.Method != "GET" {
		t.Fatalf("expected GET download URL, got %q", downloadURL.Method)
	}

	downloadPath := mustURLPath(t, downloadURL.URL)
	downloadRequest := httptest.NewRequest(http.MethodGet, downloadPath, nil)
	downloadRecorder := httptest.NewRecorder()
	router.ServeHTTP(downloadRecorder, downloadRequest)
	if downloadRecorder.Code != http.StatusOK {
		t.Fatalf("expected presigned download 200, got %d", downloadRecorder.Code)
	}
	if downloadRecorder.Body.String() != "hello presign" {
		t.Fatalf("unexpected presigned download body %q", downloadRecorder.Body.String())
	}
}

func TestNormalizeFileMetadataPrefersPositiveSizeBytes(t *testing.T) {
	t.Parallel()

	metadata := normalizeFileMetadata(southbound.RawJSON{
		"file_id":    "file-1",
		"size":       0,
		"size_bytes": 12,
	}, core.FileUploadRequest{})
	if metadata.Size != 12 {
		t.Fatalf("size = %d, want 12", metadata.Size)
	}
}

func TestPresignedUploadRejectsOversizedBody(t *testing.T) {
	t.Parallel()

	service := core.NewService(newFakeSouthbound())
	presigner := presign.NewManager(presign.Config{Secret: "presign-secret"})
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			Presigns:       presigner,
			MaxUploadBytes: 5,
		},
	)

	uploadURLResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/files/upload-url", map[string]any{
		"assistant_id": "demo-main",
		"filename":     "too-large.txt",
	}, http.StatusOK)
	var uploadURL presign.URLResponse
	decodeResponse(t, uploadURLResponse, &uploadURL)

	uploadPath := mustURLPath(t, uploadURL.URL)
	uploadRequest := httptest.NewRequest(http.MethodPut, uploadPath, bytes.NewReader([]byte("too large")))
	uploadRecorder := httptest.NewRecorder()
	router.ServeHTTP(uploadRecorder, uploadRequest)
	if uploadRecorder.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("expected 413, got %d, body=%s", uploadRecorder.Code, uploadRecorder.Body.String())
	}
}

func TestPhaseOneCompleteAndFail(t *testing.T) {
	t.Parallel()

	service := core.NewService(newFakeSouthbound())
	router := NewRouter(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
	)

	createResponse := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-3",
		"instruction":         "Run the task.",
		"approval_mode":       "none",
		"created_by":          "runtime-test",
	}, http.StatusAccepted)

	var created core.ChatResponse
	decodeResponse(t, createResponse, &created)

	completeResponse := performJSONRequest(
		t,
		router,
		http.MethodPost,
		"/api/v1/tasks/"+created.TaskID+"/complete",
		map[string]any{
			"assistant_id":             "demo-approval",
			"claim_token":              "token-1",
			"result_workspace_file_id": "file-1",
		},
		http.StatusOK,
	)
	var completed struct {
		Task core.TaskView `json:"task"`
	}
	decodeResponse(t, completeResponse, &completed)
	if completed.Task.Status != "completed" {
		t.Fatalf("expected completed, got %q", completed.Task.Status)
	}
	if completed.Task.ResultWorkspaceFileID != "file-1" {
		t.Fatalf("expected result file_id file-1, got %q", completed.Task.ResultWorkspaceFileID)
	}

	failCreate := performJSONRequest(t, router, http.MethodPost, "/api/v1/chat", map[string]any{
		"source_assistant_id": "demo-main",
		"target_assistant_id": "demo-approval",
		"input_object_id":     "object-4",
		"instruction":         "Run and fail the task.",
		"approval_mode":       "none",
		"created_by":          "runtime-test",
	}, http.StatusAccepted)

	var failing core.ChatResponse
	decodeResponse(t, failCreate, &failing)

	failResponse := performJSONRequest(
		t,
		router,
		http.MethodPost,
		"/api/v1/tasks/"+failing.TaskID+"/fail",
		map[string]any{
			"assistant_id":       "demo-approval",
			"claim_token":        "token-1",
			"failed_reason_code": "runtime_error",
			"failed_reason_text": "tool exited 1",
		},
		http.StatusOK,
	)
	var failed struct {
		Task core.TaskView `json:"task"`
	}
	decodeResponse(t, failResponse, &failed)
	if failed.Task.Status != "failed" {
		t.Fatalf("expected failed, got %q", failed.Task.Status)
	}
	if failed.Task.FailedReasonCode != "runtime_error" {
		t.Fatalf("expected runtime_error, got %q", failed.Task.FailedReasonCode)
	}
}

func performJSONRequest(
	t *testing.T,
	handler http.Handler,
	method string,
	path string,
	payload any,
	wantStatus int,
) *httptest.ResponseRecorder {
	t.Helper()

	var body io.Reader
	if payload != nil {
		raw, err := json.Marshal(payload)
		if err != nil {
			t.Fatalf("marshal payload: %v", err)
		}
		body = bytes.NewReader(raw)
	}

	request := httptest.NewRequest(method, path, body)
	request.Header.Set("X-API-Key", "dev-key")
	request.Header.Set("Accept", "application/json")
	if payload != nil {
		request.Header.Set("Content-Type", "application/json")
	}

	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	if recorder.Code != wantStatus {
		t.Fatalf("%s %s: expected %d, got %d, body=%s", method, path, wantStatus, recorder.Code, recorder.Body.String())
	}
	return recorder
}

func performPublicJSONRequest(
	t *testing.T,
	handler http.Handler,
	method string,
	path string,
	payload any,
	wantStatus int,
) *httptest.ResponseRecorder {
	t.Helper()

	var body io.Reader
	if payload != nil {
		raw, err := json.Marshal(payload)
		if err != nil {
			t.Fatalf("marshal payload: %v", err)
		}
		body = bytes.NewReader(raw)
	}

	request := httptest.NewRequest(method, path, body)
	request.Header.Set("Accept", "application/json")
	if payload != nil {
		request.Header.Set("Content-Type", "application/json")
	}

	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	if recorder.Code != wantStatus {
		t.Fatalf("%s %s: expected %d, got %d, body=%s", method, path, wantStatus, recorder.Code, recorder.Body.String())
	}
	return recorder
}

func performPublicUploadFailure(t *testing.T, handler http.Handler) {
	t.Helper()

	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	if err := writer.WriteField("user_id", "e2e-main-0416"); err != nil {
		t.Fatalf("write user_id field: %v", err)
	}
	if err := writer.WriteField("assistant_id", "demo-main"); err != nil {
		t.Fatalf("write unsupported assistant_id field: %v", err)
	}
	part, err := createTestUploadPart(writer, "audit.txt", "text/plain")
	if err != nil {
		t.Fatalf("create upload part: %v", err)
	}
	if _, err := part.Write([]byte("audit failure")); err != nil {
		t.Fatalf("write upload part: %v", err)
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("close multipart writer: %v", err)
	}

	request := httptest.NewRequest(http.MethodPost, "/api/v1/files/upload", &body)
	request.Header.Set("Content-Type", writer.FormDataContentType())
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("expected public upload failure 400, got %d body=%s", recorder.Code, recorder.Body.String())
	}
}

func decodeResponse(t *testing.T, recorder *httptest.ResponseRecorder, target any) {
	t.Helper()
	if err := json.Unmarshal(recorder.Body.Bytes(), target); err != nil {
		t.Fatalf("decode response: %v", err)
	}
}

func createTestUploadPart(writer *multipart.Writer, filename string, contentType string) (io.Writer, error) {
	header := make(textproto.MIMEHeader)
	header.Set("Content-Disposition", `form-data; name="file"; filename="`+strings.ReplaceAll(filename, `"`, `\"`)+`"`)
	header.Set("Content-Type", contentType)
	return writer.CreatePart(header)
}

func mustURLPath(t *testing.T, rawURL string) string {
	t.Helper()
	parsed, err := url.Parse(rawURL)
	if err != nil {
		t.Fatalf("parse URL %q: %v", rawURL, err)
	}
	if parsed.RawQuery != "" {
		return parsed.Path + "?" + parsed.RawQuery
	}
	return parsed.Path
}

func redeemAssistantProxyCookie(t *testing.T, handler http.Handler, path string) *http.Cookie {
	t.Helper()
	request := httptest.NewRequest(http.MethodGet, path, nil)
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusFound {
		t.Fatalf("expected launch ticket redirect, got %d body=%s", recorder.Code, recorder.Body.String())
	}
	cookies := recorder.Result().Cookies()
	if len(cookies) != 1 {
		t.Fatalf("expected one proxy session cookie, got %#v", cookies)
	}
	return cookies[0]
}

func redeemAdminProxyCookie(t *testing.T, handler http.Handler, path string) *http.Cookie {
	t.Helper()
	request := httptest.NewRequest(http.MethodGet, path, nil)
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusFound {
		t.Fatalf("expected admin session redirect, got %d body=%s", recorder.Code, recorder.Body.String())
	}
	cookies := recorder.Result().Cookies()
	if len(cookies) != 1 {
		t.Fatalf("expected one admin session cookie, got %#v", cookies)
	}
	if cookies[0].Name != adminProxySessionCookie {
		t.Fatalf("expected %s cookie, got %s", adminProxySessionCookie, cookies[0].Name)
	}
	if recorder.Header().Get("Location") != "/admin/" {
		t.Fatalf("expected token-stripped admin redirect, got %q", recorder.Header().Get("Location"))
	}
	return cookies[0]
}

type eventCollector struct {
	mu     sync.Mutex
	events []webhook.Event
}

type dbStatsMemoryStore struct {
	*taskstore.MemoryStore
	stats core.DBPoolStats
}

func (s *dbStatsMemoryStore) DBPoolStats() core.DBPoolStats {
	return s.stats
}

type staticDBPoolStatsProvider struct {
	stats core.DBPoolStats
}

func (p staticDBPoolStatsProvider) DBPoolStats() core.DBPoolStats {
	return p.stats
}

type publicFailureApprovalService struct {
	Service
}

func (s publicFailureApprovalService) GetApproval(context.Context, string) (core.ApprovalResponse, error) {
	return core.ApprovalResponse{}, &core.ValidationError{Message: "approval not found"}
}

func decodeCallbackEventFromRequest(r *http.Request) (webhook.Event, error) {
	var payload webhook.CallbackPayload
	if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
		return webhook.Event{}, err
	}
	files := make([]webhook.FileRef, 0, len(payload.Files))
	for _, file := range payload.Files {
		files = append(files, webhook.FileRef{
			FileID:      file.FileID,
			Filename:    file.FileName,
			Size:        parseInt64ForTest(file.Size),
			MimeType:    file.MimeType,
			DownloadURL: file.DownloadURL,
		})
	}
	return webhook.Event{
		TaskID:        payload.TaskID,
		EventType:     payload.EventType,
		Status:        payload.Status,
		Stage:         payload.Stage,
		ReplyText:     payload.ReplyText,
		Files:         files,
		ErrorCode:     payload.ErrorCode,
		ErrorMessage:  payload.ErrorMessage,
		ErrorLocation: payload.ErrorLocation,
		Reason:        payload.ErrorReason,
	}, nil
}

func newCallbackTestFixture(t *testing.T, southboundFake *fakeSouthbound) (http.Handler, *webhook.Dispatcher, *eventCollector, string) {
	t.Helper()

	delivered := &eventCollector{}
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		event, err := decodeCallbackEventFromRequest(r)
		if err != nil {
			t.Errorf("decode callback event: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		delivered.add(event)
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(callback.Close)

	dispatcher := webhook.NewDispatcher(webhook.Config{
		Secret:      "callback-secret",
		MaxAttempts: 1,
		RetryDelay:  time.Millisecond,
	})
	service := core.NewService(southboundFake)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}}),
		nil,
		RouterOptions{
			Webhooks:             dispatcher,
			CallbackAllowedHosts: []string{"127.0.0.1"},
			CallbackAllowPrivate: true,
		},
	)
	return router, dispatcher, delivered, callback.URL
}

func newPublicCallbackFlowFixture(t *testing.T, southboundFake *fakeSouthbound) (http.Handler, *webhook.Dispatcher, *eventCollector) {
	t.Helper()

	delivered := &eventCollector{}
	callback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		event, err := decodeCallbackEventFromRequest(r)
		if err != nil {
			t.Errorf("decode callback event: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		delivered.add(event)
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(callback.Close)

	dispatcher := webhook.NewDispatcher(webhook.Config{
		Secret:      "callback-secret",
		MaxAttempts: 1,
		RetryDelay:  time.Millisecond,
	})
	service := core.NewService(southboundFake)
	router := NewRouterWithOptions(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		auth.NewMiddleware(auth.Config{APIKeys: []string{"dev-key"}, AllowPublicUnauthenticated: true}),
		nil,
		RouterOptions{
			Webhooks:             dispatcher,
			DefaultCallbackURL:   callback.URL,
			CallbackAllowedHosts: []string{"127.0.0.1"},
			CallbackAllowPrivate: true,
		},
	)
	return router, dispatcher, delivered
}

func performPublicFileUpload(
	t *testing.T,
	handler http.Handler,
	userID string,
	filename string,
	contentType string,
	content []byte,
) publicFileMetadataResponse {
	t.Helper()

	body := &bytes.Buffer{}
	writer := multipart.NewWriter(body)
	if err := writer.WriteField("user_id", userID); err != nil {
		t.Fatalf("write user_id: %v", err)
	}
	part, err := createTestUploadPart(writer, filename, contentType)
	if err != nil {
		t.Fatalf("create upload part: %v", err)
	}
	if _, err := part.Write(content); err != nil {
		t.Fatalf("write upload content: %v", err)
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("close multipart writer: %v", err)
	}

	request := httptest.NewRequest(http.MethodPost, "/api/v1/files/upload", body)
	request.Header.Set("Content-Type", writer.FormDataContentType())
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("expected upload 200, got %d body=%s", recorder.Code, recorder.Body.String())
	}
	var uploaded publicFileMetadataResponse
	decodeResponse(t, recorder, &uploaded)
	return uploaded
}

func (c *eventCollector) add(event webhook.Event) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.events = append(c.events, event)
}

func (c *eventCollector) snapshot() []webhook.Event {
	c.mu.Lock()
	defer c.mu.Unlock()
	events := make([]webhook.Event, len(c.events))
	copy(events, c.events)
	return events
}

func (c *eventCollector) lastWithStatus(status string) (webhook.Event, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for i := len(c.events) - 1; i >= 0; i-- {
		if c.events[i].Status == status {
			return c.events[i], true
		}
	}
	return webhook.Event{}, false
}

func parseInt64ForTest(value string) int64 {
	parsed, _ := strconv.ParseInt(value, 10, 64)
	return parsed
}

type multiBlockingRuntimeExecutor struct {
	reply   string
	entered chan core.RuntimeExecutionRequest
	release chan struct{}
	once    sync.Once
	mu      sync.Mutex
	calls   []core.RuntimeExecutionRequest
}

func newMultiBlockingRuntimeExecutor(reply string, capacity int) *multiBlockingRuntimeExecutor {
	return &multiBlockingRuntimeExecutor{
		reply:   reply,
		entered: make(chan core.RuntimeExecutionRequest, capacity),
		release: make(chan struct{}),
	}
}

func (b *multiBlockingRuntimeExecutor) ExecuteChat(
	ctx context.Context,
	request core.RuntimeExecutionRequest,
) (core.RuntimeExecutionResult, error) {
	b.mu.Lock()
	b.calls = append(b.calls, request)
	b.mu.Unlock()
	select {
	case b.entered <- request:
	case <-ctx.Done():
		return core.RuntimeExecutionResult{}, ctx.Err()
	}
	select {
	case <-ctx.Done():
		return core.RuntimeExecutionResult{}, ctx.Err()
	case <-b.release:
		return core.RuntimeExecutionResult{
			ReplyText: b.reply,
			Status:    "ok",
		}, nil
	}
}

func (b *multiBlockingRuntimeExecutor) waitForEntered(t *testing.T, count int, timeout time.Duration) {
	t.Helper()
	deadline := time.After(timeout)
	for i := 0; i < count; i++ {
		select {
		case <-b.entered:
		case <-deadline:
			b.releaseNow()
			t.Fatalf("timed out waiting for runtime call %d of %d", i+1, count)
		}
	}
}

func (b *multiBlockingRuntimeExecutor) releaseNow() {
	b.once.Do(func() {
		close(b.release)
	})
}

func (b *multiBlockingRuntimeExecutor) callCount() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return len(b.calls)
}

type fakeRuntimeExecutor struct {
	reply          string
	replyFunc      func(core.RuntimeExecutionRequest) string
	errors         []error
	progressEvents []core.RuntimeProgressEvent
	calls          []core.RuntimeExecutionRequest
}

func (f *fakeRuntimeExecutor) ExecuteChat(
	ctx context.Context,
	request core.RuntimeExecutionRequest,
) (core.RuntimeExecutionResult, error) {
	callIndex := len(f.calls)
	f.calls = append(f.calls, request)
	for _, event := range f.progressEvents {
		if request.OnProgress == nil {
			continue
		}
		if event.At.IsZero() {
			event.At = time.Now().UTC()
		}
		if err := request.OnProgress(ctx, event); err != nil {
			return core.RuntimeExecutionResult{}, err
		}
	}
	if callIndex < len(f.errors) && f.errors[callIndex] != nil {
		return core.RuntimeExecutionResult{}, f.errors[callIndex]
	}
	reply := f.reply
	if f.replyFunc != nil {
		reply = f.replyFunc(request)
	}
	return core.RuntimeExecutionResult{
		ReplyText: reply,
		Status:    "ok",
	}, nil
}

type blockingRuntimeExecutor struct {
	reply      string
	entered    chan core.RuntimeExecutionRequest
	release    chan struct{}
	canceled   chan struct{}
	once       sync.Once
	cancelOnce sync.Once
}

func newBlockingRuntimeExecutor(reply string) *blockingRuntimeExecutor {
	return &blockingRuntimeExecutor{
		reply:    reply,
		entered:  make(chan core.RuntimeExecutionRequest, 1),
		release:  make(chan struct{}),
		canceled: make(chan struct{}),
	}
}

func (b *blockingRuntimeExecutor) ExecuteChat(
	ctx context.Context,
	request core.RuntimeExecutionRequest,
) (core.RuntimeExecutionResult, error) {
	select {
	case b.entered <- request:
	default:
	}
	select {
	case <-ctx.Done():
		b.cancelOnce.Do(func() {
			close(b.canceled)
		})
		return core.RuntimeExecutionResult{}, ctx.Err()
	case <-b.release:
		return core.RuntimeExecutionResult{
			ReplyText: b.reply,
			Status:    "ok",
		}, nil
	}
}

func (b *blockingRuntimeExecutor) releaseNow() {
	b.once.Do(func() {
		close(b.release)
	})
}

type failingEnqueueOutboxStore struct {
	*webhook.MemoryOutboxStore
}

func (s *failingEnqueueOutboxStore) Enqueue(context.Context, webhook.DeliveryRecord) error {
	return errors.New("outbox enqueue failed")
}

type fakeSouthbound struct {
	mu                            sync.Mutex
	nextTaskID                    int
	nextApprovalID                int
	nextFileID                    int
	dropTaskMetaOnGet             bool
	createTaskStatus              string
	getTaskAutoCompleteAfter      int
	getTaskCalls                  map[string]int
	tasks                         map[string]southbound.Task
	approvals                     map[string]southbound.ApprovalStatus
	files                         map[string]southbound.RawJSON
	fileErrors                    map[string]error
	fileBytes                     map[string][]byte
	syncError                     error
	syncSummaryErrors             []string
	listError                     error
	syncResultFiles               []southbound.RawJSON
	syncResultFilesAvailableAfter int
	disableDefaultSyncResult      bool
	syncRequests                  []southbound.SyncResultsRequest
	completeRequests              []southbound.CompleteTaskRequest
	assistants                    map[string]southbound.Assistant
	users                         map[string]southbound.User
	userErrors                    map[string]error
	userBindings                  map[string][]southbound.UserAssistantBinding
	launchTickets                 map[string]southbound.ControlUILaunchTicketResolveResponse
	health                        southbound.HealthSummary
}

func newFakeSouthbound() *fakeSouthbound {
	return &fakeSouthbound{
		tasks:        make(map[string]southbound.Task),
		getTaskCalls: make(map[string]int),
		approvals:    make(map[string]southbound.ApprovalStatus),
		files:        make(map[string]southbound.RawJSON),
		fileErrors:   make(map[string]error),
		fileBytes:    make(map[string][]byte),
		users: map[string]southbound.User{
			"e2e-main-0416": {UserID: "e2e-main-0416"},
		},
		userErrors: make(map[string]error),
		userBindings: map[string][]southbound.UserAssistantBinding{
			"e2e-main-0416": {
				{
					UserID:      "e2e-main-0416",
					AssistantID: "demo-main",
					BoundAt:     sbTime(time.Now().UTC().Add(-time.Hour)),
				},
			},
		},
		launchTickets: map[string]southbound.ControlUILaunchTicketResolveResponse{
			"ticket-demo-main": {
				AssistantID:  "demo-main",
				GatewayURL:   "http://127.0.0.1:18789",
				GatewayToken: "runtime-token",
				SessionKey:   "main",
				ExpiresAt:    sbTime(time.Now().UTC().Add(5 * time.Minute)),
			},
		},
		assistants: map[string]southbound.Assistant{
			"demo-main": {
				AssistantID: "demo-main",
				Name:        "Demo Main",
				Runtime: southbound.AssistantRuntime{
					ContainerName: "ironagent-assistant-demo-main",
					Status:        "stopped",
					GatewayURL:    "http://127.0.0.1:18789",
				},
			},
		},
	}
}

func (f *fakeSouthbound) GetUser(_ context.Context, userID string) (southbound.User, error) {
	if err := f.userErrors[userID]; err != nil {
		return southbound.User{}, err
	}
	user, ok := f.users[userID]
	if !ok {
		return southbound.User{}, &southbound.HTTPError{
			StatusCode: http.StatusNotFound,
			Method:     http.MethodGet,
			URL:        "/api/v1/users/" + userID,
			Body:       "user not found",
		}
	}
	return user, nil
}

func markFakeResultFileForTask(t *testing.T, fake *fakeSouthbound, fileID string, taskID string, assistantID string) {
	t.Helper()
	record, ok := fake.files[fileID]
	if !ok {
		t.Fatalf("fake result file %q not found", fileID)
	}
	record["assistant_id"] = assistantID
	record["direction"] = "outbound"
	record["file_role"] = "result"
	record["source_type"] = "assistant_result"
	record["source_ref"] = taskID
	if strings.TrimSpace(stringValue(record["relative_path"])) == "" {
		filename := strings.TrimSpace(stringValue(record["filename"]))
		if filename == "" {
			filename = "result.txt"
		}
		record["bucket"] = firstNonEmptyString(stringValue(record["bucket"]), "temp")
		record["relative_path"] = "temp/go-gateway-results/" + sanitizeTaskPathComponent(taskID, 64) + "/" + filename
	}
	fake.files[fileID] = record
}

func setFakeAssistantRuntimeStatus(t *testing.T, southboundFake *fakeSouthbound, assistantID string, status string) {
	t.Helper()
	assistant, ok := southboundFake.assistants[assistantID]
	if !ok {
		t.Fatalf("missing fake assistant %q", assistantID)
	}
	assistant.Runtime.Status = status
	southboundFake.assistants[assistantID] = assistant
}

func (f *fakeSouthbound) ListUserAssistants(_ context.Context, userID string) ([]southbound.UserAssistantBinding, error) {
	return f.userBindings[userID], nil
}

func sbTime(value time.Time) southbound.Time {
	return southbound.Time{Time: value}
}

func sbTimePtr(value time.Time) *southbound.Time {
	converted := sbTime(value)
	return &converted
}

func (f *fakeSouthbound) GetAssistant(_ context.Context, assistantID string) (southbound.Assistant, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	assistant, ok := f.assistants[assistantID]
	if !ok {
		return southbound.Assistant{}, &southbound.HTTPError{
			StatusCode: http.StatusNotFound,
			Method:     http.MethodGet,
			URL:        "/api/v1/assistants/" + assistantID,
			Body:       "assistant not found",
		}
	}
	return assistant, nil
}

func (f *fakeSouthbound) ListAssistantContext(context.Context) ([]southbound.AssistantContextEntry, error) {
	entries := make([]southbound.AssistantContextEntry, 0, len(f.assistants))
	for _, assistant := range f.assistants {
		entries = append(entries, southbound.AssistantContextEntry{
			ID:     assistant.AssistantID,
			Status: assistant.Runtime.Status,
		})
	}
	sort.Slice(entries, func(i, j int) bool {
		return entries[i].ID < entries[j].ID
	})
	return entries, nil
}

func (f *fakeSouthbound) StartAssistant(_ context.Context, assistantID string) (southbound.AssistantRuntimeControlResponse, error) {
	assistant := f.assistants[assistantID]
	assistant.Runtime.Status = "running"
	f.assistants[assistantID] = assistant
	return southbound.AssistantRuntimeControlResponse{
		AssistantID: assistantID,
		Status:      "running",
	}, nil
}

func (f *fakeSouthbound) StopAssistant(_ context.Context, assistantID string) (southbound.AssistantRuntimeControlResponse, error) {
	assistant := f.assistants[assistantID]
	assistant.Runtime.Status = "stopped"
	f.assistants[assistantID] = assistant
	return southbound.AssistantRuntimeControlResponse{
		AssistantID: assistantID,
		Status:      "stopped",
	}, nil
}

func (f *fakeSouthbound) ResolveControlUILaunchTicket(
	_ context.Context,
	ticket string,
	_ string,
) (southbound.ControlUILaunchTicketResolveResponse, error) {
	normalized := strings.TrimSpace(ticket)
	resolved, ok := f.launchTickets[normalized]
	if !ok {
		return southbound.ControlUILaunchTicketResolveResponse{}, &southbound.HTTPError{
			StatusCode: http.StatusNotFound,
			Method:     http.MethodPost,
			URL:        "/api/v1/control-ui/launch-tickets/resolve",
			Body:       "launch ticket not found",
		}
	}
	delete(f.launchTickets, normalized)
	return resolved, nil
}

func (f *fakeSouthbound) CreateSharedObject(_ context.Context, request southbound.SharedObjectCreateRequest) (southbound.SharedObject, error) {
	return southbound.SharedObject{
		ID:                    "object-from-file",
		Filename:              request.Filename,
		ContentType:           request.ContentType,
		SizeBytes:             request.SizeBytes,
		SHA256:                request.SHA256,
		SourceAssistantID:     request.SourceAssistantID,
		SourceWorkspaceFileID: request.SourceWorkspaceFileID,
		Status:                "active",
	}, nil
}

func (f *fakeSouthbound) CreateTask(_ context.Context, request southbound.CreateTaskRequest) (southbound.Task, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.nextTaskID++
	taskID := "task-" + time.Now().UTC().Format("150405") + "-" + string(rune('0'+f.nextTaskID))

	task := southbound.Task{
		ID:                taskID,
		RootTaskID:        taskID,
		Depth:             0,
		MaxDepth:          request.MaxDepth,
		SourceAssistantID: request.SourceAssistantID,
		TargetAssistantID: request.TargetAssistantID,
		InputObjectID:     request.InputObjectID,
		Instruction:       request.Instruction,
		ApprovalMode:      request.ApprovalMode,
		IdempotencyKey:    request.IdempotencyKey,
		TimeoutSeconds:    request.TimeoutSeconds,
		CreatedBy:         request.CreatedBy,
		CreatedAt:         sbTime(time.Now().UTC()),
		MetaJSON:          cloneRawMap(request.MetaJSON),
	}

	if request.ApprovalMode == "none" {
		task.Status = "pending_claim"
	} else {
		f.nextApprovalID++
		approvalID := "approval-" + string(rune('0'+f.nextApprovalID))
		task.Status = "pending_approval"
		task.ApprovalID = approvalID
		requestedAt := time.Now().UTC()
		expiresAt := requestedAt.Add(15 * time.Minute)
		f.approvals[approvalID] = southbound.ApprovalStatus{
			ApprovalID:    approvalID,
			AssistantID:   request.SourceAssistantID,
			ApprovalScope: "user",
			Reason:        "cross-assistant collaboration requires approval",
			Target:        request.InputObjectID + ":" + request.TargetAssistantID,
			Status:        "pending",
			RequestedAt:   sbTimePtr(requestedAt),
			ExpiresAt:     sbTimePtr(expiresAt),
		}
	}
	if strings.TrimSpace(f.createTaskStatus) != "" {
		task.Status = strings.TrimSpace(f.createTaskStatus)
	}

	f.tasks[task.ID] = task
	return task, nil
}

func cloneRawMap(source map[string]any) map[string]any {
	if len(source) == 0 {
		return map[string]any{}
	}
	out := make(map[string]any, len(source))
	for key, value := range source {
		out[key] = value
	}
	return out
}

func (f *fakeSouthbound) GetTask(_ context.Context, taskID string) (southbound.Task, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	task, ok := f.tasks[taskID]
	if !ok {
		return southbound.Task{}, &core.ValidationError{Message: "task not found"}
	}
	f.getTaskCalls[taskID]++
	if f.getTaskAutoCompleteAfter > 0 &&
		task.Status == "running" &&
		f.getTaskCalls[taskID] >= f.getTaskAutoCompleteAfter {
		task.Status = "completed"
		now := time.Now().UTC()
		task.CompletedAt = sbTimePtr(now)
		f.tasks[task.ID] = task
	}
	if f.dropTaskMetaOnGet {
		task.MetaJSON = map[string]any{}
	}
	if task.ApprovalID != "" {
		approval := f.approvals[task.ApprovalID]
		if task.Status == "pending_approval" && approval.Status == "approved" {
			task.Status = "pending_claim"
			f.tasks[task.ID] = task
		}
		if task.Status == "pending_approval" && approval.Status == "denied" {
			task.Status = "rejected"
			f.tasks[task.ID] = task
		}
	}
	return task, nil
}

func (f *fakeSouthbound) GetApproval(_ context.Context, approvalID string) (southbound.ApprovalStatus, error) {
	return f.approvals[approvalID], nil
}

func (f *fakeSouthbound) ListApprovals(
	_ context.Context,
	status string,
	assistantID string,
	approvalScope string,
) ([]southbound.ApprovalStatus, error) {
	var approvals []southbound.ApprovalStatus
	for _, approval := range f.approvals {
		if status != "" && approval.Status != status {
			continue
		}
		if assistantID != "" && approval.AssistantID != assistantID {
			continue
		}
		if approvalScope != "" && approval.ApprovalScope != approvalScope {
			continue
		}
		approvals = append(approvals, approval)
	}
	sort.Slice(approvals, func(i, j int) bool {
		return approvals[i].ApprovalID < approvals[j].ApprovalID
	})
	return approvals, nil
}

func (f *fakeSouthbound) ResolveApproval(
	_ context.Context,
	approvalID string,
	resolution string,
) (southbound.ApprovalResolution, error) {
	approval := f.approvals[approvalID]
	approval.Resolution = resolution
	approval.Status = "approved"
	now := time.Now().UTC()
	approval.ResolvedAt = sbTimePtr(now)
	if resolution == "deny" {
		approval.Status = "denied"
	}
	f.approvals[approvalID] = approval
	return southbound.ApprovalResolution{ID: approvalID, Status: approval.Status}, nil
}

func (f *fakeSouthbound) ClaimTask(_ context.Context, taskID string, assistantID string) (southbound.ClaimResponse, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	task := f.tasks[taskID]
	task.Status = "claimed"
	now := time.Now().UTC()
	task.AcceptedAt = sbTimePtr(now)
	f.tasks[task.ID] = task
	return southbound.ClaimResponse{
		Task: task,
		Claim: southbound.Claim{
			ID:             "claim-1",
			TaskID:         taskID,
			AssistantID:    assistantID,
			ClaimToken:     "token-1",
			ClaimedAt:      sbTime(now),
			LeaseExpiresAt: sbTime(now.Add(5 * time.Minute)),
			Status:         "active",
		},
	}, nil
}

func (f *fakeSouthbound) RenewTaskClaim(_ context.Context, taskID string, request southbound.RenewClaimRequest) (southbound.ClaimResponse, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	task := f.tasks[taskID]
	now := time.Now().UTC()
	return southbound.ClaimResponse{
		Task: task,
		Claim: southbound.Claim{
			ID:             "claim-1",
			TaskID:         taskID,
			AssistantID:    request.AssistantID,
			ClaimToken:     request.ClaimToken,
			ClaimedAt:      sbTime(now),
			LeaseExpiresAt: sbTime(now.Add(5 * time.Minute)),
			Status:         "active",
		},
	}, nil
}

func (f *fakeSouthbound) StartTask(_ context.Context, taskID string, _ southbound.StartTaskRequest) (southbound.Task, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	task := f.tasks[taskID]
	task.Status = "running"
	now := time.Now().UTC()
	task.StartedAt = sbTimePtr(now)
	f.tasks[task.ID] = task
	return task, nil
}

func (f *fakeSouthbound) CompleteTask(_ context.Context, taskID string, request southbound.CompleteTaskRequest) (southbound.Task, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.completeRequests = append(f.completeRequests, request)
	task := f.tasks[taskID]
	task.Status = "completed"
	task.ResultObjectID = request.ResultObjectID
	task.ResultWorkspaceFileID = request.ResultWorkspaceFileID
	if len(request.ResultWorkspaceFileIDs) > 0 {
		if task.MetaJSON == nil {
			task.MetaJSON = map[string]any{}
		}
		task.MetaJSON["runtime_result_workspace_file_ids"] = append([]string(nil), request.ResultWorkspaceFileIDs...)
		task.MetaJSON["runtime_result_file_count"] = len(request.ResultWorkspaceFileIDs)
	}
	now := time.Now().UTC()
	task.CompletedAt = sbTimePtr(now)
	f.tasks[task.ID] = task
	return task, nil
}

func (f *fakeSouthbound) FailTask(_ context.Context, taskID string, request southbound.FailTaskRequest) (southbound.Task, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	task := f.tasks[taskID]
	task.Status = "failed"
	task.FailedReasonCode = request.FailedReasonCode
	task.FailedReasonText = request.FailedReasonText
	now := time.Now().UTC()
	task.FailedAt = sbTimePtr(now)
	f.tasks[task.ID] = task
	return task, nil
}

func (f *fakeSouthbound) CancelTask(_ context.Context, taskID string, request southbound.CancelTaskRequest) (southbound.Task, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	task := f.tasks[taskID]
	task.Status = "cancelled"
	task.FailedReasonCode = "cancelled"
	task.FailedReasonText = request.Reason
	f.tasks[task.ID] = task
	return task, nil
}

func (f *fakeSouthbound) UploadFile(_ context.Context, request southbound.FileUploadRequest) (southbound.RawJSON, error) {
	content := request.Content
	if request.ContentReader != nil {
		var err error
		content, err = io.ReadAll(request.ContentReader)
		if err != nil {
			return nil, err
		}
	}
	f.nextFileID++
	fileID := fmt.Sprintf("file-%d", f.nextFileID)
	sizeBytes := int64(len(content))
	targetBucket := strings.TrimSpace(request.TargetBucket)
	if targetBucket == "" {
		targetBucket = "temp"
	}
	filename := strings.TrimSpace(request.Filename)
	relativePath := strings.Trim(targetBucket+"/"+filename, "/")
	direction := "inbound"
	fileRole := "input"
	if strings.TrimSpace(request.SourceType) == "assistant_result" {
		direction = "outbound"
		fileRole = "result"
	}
	record := southbound.RawJSON{
		"status":        "accepted",
		"file_id":       fileID,
		"assistant_id":  request.AssistantID,
		"user_id":       request.UserID,
		"owner_user_id": request.UserID,
		"filename":      filename,
		"content_type":  request.ContentType,
		"size_bytes":    sizeBytes,
		"bucket":        targetBucket,
		"relative_path": relativePath,
		"direction":     direction,
		"file_role":     fileRole,
		"source_type":   request.SourceType,
		"source_ref":    request.SourceRef,
	}
	f.files[fileID] = southbound.RawJSON{
		"file_id":       fileID,
		"assistant_id":  request.AssistantID,
		"user_id":       request.UserID,
		"owner_user_id": request.UserID,
		"filename":      filename,
		"content_type":  request.ContentType,
		"size_bytes":    sizeBytes,
		"bucket":        targetBucket,
		"relative_path": relativePath,
		"direction":     direction,
		"file_role":     fileRole,
		"source_type":   request.SourceType,
		"source_ref":    request.SourceRef,
		"download_url":  "/api/v1/files/" + fileID + "/download",
	}
	f.fileBytes[fileID] = append([]byte(nil), content...)
	return record, nil
}

func (f *fakeSouthbound) ListFiles(_ context.Context, request southbound.FileListRequest) (southbound.RawJSON, error) {
	if f.listError != nil {
		return nil, f.listError
	}
	files := make([]southbound.RawJSON, 0, len(f.files))
	for _, file := range f.files {
		if bucket := strings.TrimSpace(request.Bucket); bucket != "" && stringValue(file["bucket"]) != bucket {
			continue
		}
		if direction := strings.TrimSpace(request.Direction); direction != "" && stringValue(file["direction"]) != direction {
			continue
		}
		if fileRole := strings.TrimSpace(request.FileRole); fileRole != "" && stringValue(file["file_role"]) != fileRole {
			continue
		}
		files = append(files, file)
	}
	return southbound.RawJSON{
		"files": files,
		"total": len(files),
	}, nil
}

func (f *fakeSouthbound) SyncResults(_ context.Context, request southbound.SyncResultsRequest) (southbound.WorkspaceScanSummary, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.syncRequests = append(f.syncRequests, request)
	if f.syncError != nil {
		return southbound.WorkspaceScanSummary{}, f.syncError
	}
	withSummaryErrors := func(summary southbound.WorkspaceScanSummary) southbound.WorkspaceScanSummary {
		if len(f.syncSummaryErrors) > 0 {
			summary.Errors = append(summary.Errors, f.syncSummaryErrors...)
		}
		return summary
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	resultPathPrefix := strings.Trim(strings.ReplaceAll(request.ResultPathPrefix, "\\", "/"), "/")
	resultRelativePath := func(filename string) string {
		relativeName := strings.Trim(strings.ReplaceAll(filename, "\\", "/"), "/")
		if relativeName == "" {
			relativeName = "result.txt"
		}
		if resultPathPrefix != "" && !strings.HasPrefix(relativeName, resultPathPrefix+"/") {
			relativeName = resultPathPrefix + "/" + relativeName
		}
		return strings.Trim(strings.TrimSpace(request.Bucket)+"/"+relativeName, "/")
	}
	if len(f.syncResultFiles) > 0 {
		if f.syncResultFilesAvailableAfter > 0 && len(f.syncRequests) < f.syncResultFilesAvailableAfter {
			return withSummaryErrors(southbound.WorkspaceScanSummary{
				AssistantsScanned: 1,
				FilesScanned:      0,
				FilesSynced:       0,
				FilesDeleted:      0,
			}), nil
		}
		for index, file := range f.syncResultFiles {
			if bucket := strings.TrimSpace(stringValue(file["bucket"])); bucket != "" && bucket != request.Bucket {
				continue
			}
			record := southbound.RawJSON{}
			for key, value := range file {
				record[key] = value
			}
			fileID := strings.TrimSpace(stringValue(record["file_id"]))
			if fileID == "" {
				continue
			}
			if strings.TrimSpace(stringValue(record["assistant_id"])) == "" {
				record["assistant_id"] = request.AssistantID
			}
			if strings.TrimSpace(stringValue(record["user_id"])) == "" {
				record["user_id"] = request.UserID
			}
			if strings.TrimSpace(stringValue(record["bucket"])) == "" {
				record["bucket"] = request.Bucket
			}
			if strings.TrimSpace(stringValue(record["relative_path"])) == "" {
				record["relative_path"] = resultRelativePath(stringValue(record["filename"]))
			}
			if strings.TrimSpace(stringValue(record["source_ref"])) == "" && strings.TrimSpace(request.SourceRef) != "" {
				record["source_ref"] = strings.TrimSpace(request.SourceRef)
			}
			record["direction"] = "outbound"
			record["file_role"] = "result"
			if strings.TrimSpace(stringValue(record["created_at"])) == "" {
				record["created_at"] = time.Now().UTC().Add(time.Duration(index) * time.Millisecond).Format(time.RFC3339Nano)
			}
			if strings.TrimSpace(stringValue(record["updated_at"])) == "" {
				record["updated_at"] = record["created_at"]
			}
			f.files[fileID] = record
			if _, ok := f.fileBytes[fileID]; !ok {
				f.fileBytes[fileID] = []byte("assistant result " + fileID)
			}
		}
		return withSummaryErrors(southbound.WorkspaceScanSummary{
			AssistantsScanned: 1,
			FilesScanned:      len(f.syncResultFiles),
			FilesSynced:       len(f.syncResultFiles),
			FilesDeleted:      0,
		}), nil
	}
	if f.disableDefaultSyncResult {
		return withSummaryErrors(southbound.WorkspaceScanSummary{
			AssistantsScanned: 1,
			FilesScanned:      0,
			FilesSynced:       0,
			FilesDeleted:      0,
		}), nil
	}
	f.files["result-1"] = southbound.RawJSON{
		"file_id":       "result-1",
		"assistant_id":  request.AssistantID,
		"user_id":       request.UserID,
		"filename":      "result.txt",
		"content_type":  "text/plain",
		"bucket":        request.Bucket,
		"relative_path": resultRelativePath("result.txt"),
		"source_ref":    strings.TrimSpace(request.SourceRef),
		"direction":     "outbound",
		"file_role":     "result",
		"download_url":  "/api/v1/files/result-1/download",
		"created_at":    now,
		"updated_at":    now,
	}
	f.fileBytes["result-1"] = []byte("assistant result")
	return withSummaryErrors(southbound.WorkspaceScanSummary{
		AssistantsScanned: 1,
		FilesScanned:      1,
		FilesSynced:       1,
		FilesDeleted:      0,
	}), nil
}

func (f *fakeSouthbound) GetFile(_ context.Context, fileID string) (southbound.RawJSON, error) {
	if err, ok := f.fileErrors[fileID]; ok {
		return nil, err
	}
	return f.files[fileID], nil
}

func (f *fakeSouthbound) DownloadFile(_ context.Context, fileID string, _ string) (southbound.FileDownloadResponse, error) {
	return southbound.FileDownloadResponse{
		Content:            f.fileBytes[fileID],
		ContentType:        "text/plain; charset=utf-8",
		ContentDisposition: `attachment; filename="hello.txt"`,
		DownloadSource:     "object_store",
		StorageBackend:     "cos",
	}, nil
}

func (f *fakeSouthbound) DownloadFileStream(_ context.Context, fileID string, _ string) (southbound.DownloadReader, error) {
	return southbound.DownloadReader{
		ResponseBody:       io.NopCloser(bytes.NewReader(f.fileBytes[fileID])),
		ContentType:        "text/plain; charset=utf-8",
		ContentDisposition: `attachment; filename="hello.txt"`,
		DownloadSource:     "object_store",
		StorageBackend:     "cos",
	}, nil
}

func (f *fakeSouthbound) DeleteFile(_ context.Context, fileID string, _ string) (southbound.RawJSON, error) {
	delete(f.files, fileID)
	delete(f.fileBytes, fileID)
	return southbound.RawJSON{
		"file_id": fileID,
		"status":  "deleted",
	}, nil
}

func (f *fakeSouthbound) CheckHealth(context.Context) (southbound.HealthSummary, error) {
	if len(f.health.Components) > 0 {
		return f.health, nil
	}
	return southbound.HealthSummary{
		Components: []southbound.HealthComponent{
			{Name: "southbound_security", Status: "up", StatusCode: http.StatusOK},
			{Name: "southbound_admin", Status: "up", StatusCode: http.StatusOK},
		},
		CheckedAt: time.Now().UTC(),
	}, nil
}

func dropHTTPConnection(w http.ResponseWriter) {
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

type failingRuntimeStore struct{}

func (failingRuntimeStore) UpsertRuntime(
	context.Context,
	runtimepool.RuntimeRecord,
) (runtimepool.RuntimeRecord, error) {
	return runtimepool.RuntimeRecord{}, errors.New("not implemented")
}

func (failingRuntimeStore) HeartbeatRuntime(
	context.Context,
	runtimepool.RuntimeRecord,
) (runtimepool.RuntimeRecord, error) {
	return runtimepool.RuntimeRecord{}, errors.New("not implemented")
}

func (failingRuntimeStore) GetRuntime(context.Context, string) (runtimepool.RuntimeRecord, bool, error) {
	return runtimepool.RuntimeRecord{}, false, errors.New("not implemented")
}

func (failingRuntimeStore) ListRuntimes(context.Context) ([]runtimepool.RuntimeRecord, error) {
	return nil, errors.New("database unavailable")
}

func (failingRuntimeStore) DeleteRuntime(context.Context, string) error {
	return errors.New("not implemented")
}

func (failingRuntimeStore) ReserveRuntime(context.Context, string, time.Duration) error {
	return errors.New("not implemented")
}

func (failingRuntimeStore) ReleaseRuntime(context.Context, string) error {
	return errors.New("not implemented")
}

func (failingRuntimeStore) MarkExpired(context.Context, time.Duration) (int, int, error) {
	return 0, 0, nil
}
