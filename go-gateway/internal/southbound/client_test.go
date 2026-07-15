package southbound

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestWriteFileUploadMultipartPreservesContentType(t *testing.T) {
	t.Parallel()

	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	if err := writeFileUploadMultipart(writer, FileUploadRequest{
		UserID:       "user-001",
		TargetBucket: "temp",
		Filename:     `hello "quoted".txt`,
		ContentType:  "text/plain",
		Content:      []byte("hello"),
	}, bytes.NewReader([]byte("hello"))); err != nil {
		t.Fatalf("write multipart: %v", err)
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("close multipart: %v", err)
	}

	reader := multipart.NewReader(bytes.NewReader(body.Bytes()), writer.Boundary())
	for {
		part, err := reader.NextPart()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatalf("read multipart part: %v", err)
		}
		if part.FormName() != "file" {
			continue
		}
		if got := part.Header.Get("Content-Type"); got != "text/plain" {
			t.Fatalf("expected file content type text/plain, got %q", got)
		}
		if !strings.Contains(part.FileName(), `hello "quoted".txt`) {
			t.Fatalf("expected quoted filename to roundtrip, got %q", part.FileName())
		}
		return
	}
	t.Fatal("file part not found")
}

func TestResolveControlUILaunchTicketSendsInternalServiceToken(t *testing.T) {
	t.Parallel()

	var gotInternalToken string
	var gotOrigin string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/control-ui/launch-tickets/resolve" {
			t.Fatalf("unexpected path %q", r.URL.Path)
		}
		gotInternalToken = r.Header.Get("X-IronAgent-Internal-Token")
		gotOrigin = r.Header.Get("Origin")
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"assistant_id":  "demo-main",
			"gateway_url":   "http://127.0.0.1:18789",
			"gateway_token": "runtime-token",
			"session_key":   "agent:demo-main:main",
			"expires_at":    time.Now().UTC().Format(time.RFC3339Nano),
		})
	}))
	defer server.Close()

	client := NewClient(Config{
		SecurityBaseURL:      server.URL,
		InternalServiceToken: "internal-service-token",
	})
	if _, err := client.ResolveControlUILaunchTicket(context.Background(), "ticket-demo-main", "http://gateway.local"); err != nil {
		t.Fatalf("resolve launch ticket: %v", err)
	}
	if gotInternalToken != "internal-service-token" {
		t.Fatalf("expected internal service token, got %q", gotInternalToken)
	}
	if gotOrigin != "http://gateway.local" {
		t.Fatalf("expected origin to be forwarded, got %q", gotOrigin)
	}
}

func TestGetUserCallsSecurityUserEndpoint(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/api/v1/users/user-001" {
			t.Fatalf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"user_id":      "user-001",
			"display_name": "User 001",
			"employee_id":  "E001",
			"department":   "Ops",
			"role":         "user",
			"created_at":   time.Now().UTC().Format(time.RFC3339Nano),
			"updated_at":   time.Now().UTC().Format(time.RFC3339Nano),
		})
	}))
	defer server.Close()

	client := NewClient(Config{SecurityBaseURL: server.URL})
	user, err := client.GetUser(context.Background(), "user-001")
	if err != nil {
		t.Fatalf("GetUser() error = %v", err)
	}
	if user.UserID != "user-001" {
		t.Fatalf("user.UserID = %q, want user-001", user.UserID)
	}
}

func TestCreateSharedObjectRetriesTransientServiceUnavailable(t *testing.T) {
	t.Parallel()

	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/collaboration/objects" {
			t.Fatalf("unexpected path %q", r.URL.Path)
		}
		if calls.Add(1) < 3 {
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte(`{"detail":"collaboration repository unavailable"}`))
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"id":                  "object-001",
			"object_type":         "document",
			"filename":            "input.txt",
			"content_type":        "text/plain",
			"source_assistant_id": "external-default",
			"status":              "active",
			"created_by":          "go-gateway",
			"created_at":          time.Now().UTC().Format(time.RFC3339Nano),
			"meta_json":           map[string]any{},
		})
	}))
	defer server.Close()

	client := NewClient(Config{SecurityBaseURL: server.URL})
	created, err := client.CreateSharedObject(context.Background(), SharedObjectCreateRequest{
		SourceAssistantID: "external-default",
		Filename:          "input.txt",
		ObjectType:        "document",
		IdempotencyKey:    "gateway-task-001-input",
	})
	if err != nil {
		t.Fatalf("CreateSharedObject() error = %v", err)
	}
	if created.ID != "object-001" {
		t.Fatalf("created object id = %q", created.ID)
	}
	if calls.Load() != 3 {
		t.Fatalf("calls = %d, want 3", calls.Load())
	}
}

func TestCreateSharedObjectDoesNotRetryWithoutIdempotencyKey(t *testing.T) {
	t.Parallel()

	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/collaboration/objects" {
			t.Fatalf("unexpected path %q", r.URL.Path)
		}
		calls.Add(1)
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte(`{"detail":"collaboration repository unavailable"}`))
	}))
	defer server.Close()

	client := NewClient(Config{SecurityBaseURL: server.URL})
	_, err := client.CreateSharedObject(context.Background(), SharedObjectCreateRequest{
		SourceAssistantID: "external-default",
		Filename:          "input.txt",
		ObjectType:        "document",
	})
	if err == nil {
		t.Fatal("CreateSharedObject() error is nil")
	}
	if calls.Load() != 1 {
		t.Fatalf("calls = %d, want 1", calls.Load())
	}
}

func TestCheckHealthProbesCollaborationReadyz(t *testing.T) {
	t.Parallel()

	var probedCollaboration atomic.Bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/healthz":
			w.WriteHeader(http.StatusOK)
		case "/api/v1/collaboration/readyz":
			probedCollaboration.Store(true)
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok"})
		default:
			t.Fatalf("unexpected path %q", r.URL.Path)
		}
	}))
	defer server.Close()

	client := NewClient(Config{
		SecurityBaseURL: server.URL,
		AdminBaseURL:    server.URL,
		HTTPTimeout:     time.Second,
	})
	summary, err := client.CheckHealth(context.Background())
	if err != nil {
		t.Fatalf("CheckHealth() error = %v", err)
	}

	statuses := map[string]string{}
	for _, component := range summary.Components {
		statuses[component.Name] = component.Status
	}
	if !probedCollaboration.Load() {
		t.Fatal("collaboration health path was not probed")
	}
	if statuses["southbound_collaboration"] != "up" {
		t.Fatalf("southbound_collaboration status = %q, summary=%#v", statuses["southbound_collaboration"], summary.Components)
	}
}

func TestCompleteTaskSendsMultipleResultWorkspaceFileIDs(t *testing.T) {
	t.Parallel()

	var body map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/collaboration/tasks/task-001/complete" {
			t.Fatalf("unexpected path %q", r.URL.Path)
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatalf("decode request body: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"id":                       "task-001",
			"source_assistant_id":      "source",
			"target_assistant_id":      "demo-main",
			"input_object_id":          "object-001",
			"instruction":              "done",
			"status":                   "completed",
			"approval_mode":            "none",
			"result_workspace_file_id": "file-1",
			"created_by":               "user-001",
			"created_at":               time.Now().UTC().Format(time.RFC3339Nano),
			"timeout_seconds":          600,
			"meta_json": map[string]any{
				"runtime_result_workspace_file_ids": []string{"file-1", "file-2"},
			},
		})
	}))
	defer server.Close()

	client := NewClient(Config{SecurityBaseURL: server.URL})
	if _, err := client.CompleteTask(context.Background(), "task-001", CompleteTaskRequest{
		AssistantID:            "demo-main",
		ClaimToken:             "claim-001",
		ResultWorkspaceFileID:  "file-1",
		ResultWorkspaceFileIDs: []string{"file-1", "file-2"},
	}); err != nil {
		t.Fatalf("CompleteTask() error = %v", err)
	}
	if got := body["result_workspace_file_ids"]; got == nil {
		t.Fatalf("result_workspace_file_ids missing in body %#v", body)
	}
	values := body["result_workspace_file_ids"].([]any)
	if len(values) != 2 || values[0] != "file-1" || values[1] != "file-2" {
		t.Fatalf("result_workspace_file_ids = %#v", values)
	}
}

func TestSyncResultsSendsTaskScopeQuery(t *testing.T) {
	t.Parallel()

	changedAfter := time.Date(2026, 6, 1, 1, 2, 3, 456789000, time.FixedZone("CST", 8*60*60))
	var gotInternalToken string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Fatalf("unexpected method %q", r.Method)
		}
		if r.URL.Path != "/api/v1/workspace-files/sync-results" {
			t.Fatalf("unexpected path %q", r.URL.Path)
		}
		gotInternalToken = r.Header.Get("X-IronAgent-Internal-Token")
		query := r.URL.Query()
		assertQuery := func(key string, want string) {
			t.Helper()
			if got := query.Get(key); got != want {
				t.Fatalf("query[%s] = %q, want %q; raw=%s", key, got, want, r.URL.RawQuery)
			}
		}
		assertQuery("assistant_id", "demo-main")
		assertQuery("bucket", "temp")
		assertQuery("user_id", "user-001")
		assertQuery("changed_after", changedAfter.UTC().Format(time.RFC3339Nano))
		assertQuery("result_path_prefix", "go-gateway-results/gw-task-001/")
		assertQuery("task_id", "gw-task-001")
		assertQuery("source_ref", "gw-task-001")
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(WorkspaceScanSummary{
			AssistantsScanned: 1,
			FilesScanned:      1,
			FilesSynced:       1,
		})
	}))
	defer server.Close()

	client := NewClient(Config{
		SecurityBaseURL:      server.URL,
		InternalServiceToken: "internal-service-token",
	})
	summary, err := client.SyncResults(context.Background(), SyncResultsRequest{
		AssistantID:      " demo-main ",
		Bucket:           " temp ",
		UserID:           " user-001 ",
		ChangedAfter:     changedAfter,
		ResultPathPrefix: " go-gateway-results/gw-task-001/ ",
		TaskID:           " gw-task-001 ",
		SourceRef:        " gw-task-001 ",
	})
	if err != nil {
		t.Fatalf("SyncResults() error = %v", err)
	}
	if gotInternalToken != "internal-service-token" {
		t.Fatalf("expected internal service token, got %q", gotInternalToken)
	}
	if summary.FilesSynced != 1 || summary.FilesScanned != 1 {
		t.Fatalf("unexpected sync summary %#v", summary)
	}
}
