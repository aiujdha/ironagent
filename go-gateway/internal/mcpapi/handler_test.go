package mcpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"ironagent/go-gateway/internal/core"
	"ironagent/go-gateway/internal/presign"
	"ironagent/go-gateway/internal/southbound"
)

func TestMCPFileFlow(t *testing.T) {
	t.Parallel()

	service := newFakeService()
	presigner := presign.NewManager(presign.Config{Secret: "test-secret"})
	handler := NewHandlerWithPresigner(
		slog.New(slog.NewTextHandler(io.Discard, nil)),
		service,
		presigner,
		"http://gateway.test",
	)

	initializeRecorder := performMCPRequest(t, handler, "", "", map[string]any{
		"jsonrpc": "2.0",
		"id":      1,
		"method":  "initialize",
		"params": map[string]any{
			"protocolVersion": protocolVersion,
		},
	}, http.StatusOK)
	sessionID := initializeRecorder.Header().Get(sessionHeader)
	if sessionID == "" {
		t.Fatal("expected session header from initialize")
	}

	toolsRecorder := performMCPRequest(t, handler, sessionID, protocolVersion, map[string]any{
		"jsonrpc": "2.0",
		"id":      2,
		"method":  "tools/list",
	}, http.StatusOK)
	var toolsResponse responseEnvelope
	decodeMCPResponse(t, toolsRecorder, &toolsResponse)
	tools := toolsResponse.Result.(map[string]any)["tools"].([]any)
	if len(tools) != 5 {
		t.Fatalf("expected 5 tools, got %d", len(tools))
	}
	toolNames := map[string]bool{}
	for _, tool := range tools {
		toolNames[tool.(map[string]any)["name"].(string)] = true
	}
	for _, name := range []string{
		"ironagent.files.upload_url",
		"ironagent.files.download_url",
		"ironagent.files.list",
		"ironagent.files.get",
		"ironagent.files.delete",
	} {
		if !toolNames[name] {
			t.Fatalf("expected tool %q in list", name)
		}
	}

	uploadRecorder := performMCPRequest(t, handler, sessionID, protocolVersion, map[string]any{
		"jsonrpc": "2.0",
		"id":      3,
		"method":  "tools/call",
		"params": map[string]any{
			"name": "ironagent.files.upload_url",
			"arguments": map[string]any{
				"assistant_id":  "demo-main",
				"target_bucket": "temp",
				"filename":      "hello.txt",
				"content_type":  "text/plain",
			},
		},
	}, http.StatusOK)
	var uploadResponse responseEnvelope
	decodeMCPResponse(t, uploadRecorder, &uploadResponse)
	uploadResult := uploadResponse.Result.(map[string]any)
	if uploadResult["isError"] != false {
		t.Fatalf("expected upload success, got %#v", uploadResult)
	}
	uploadStructured := uploadResult["structuredContent"].(map[string]any)
	if uploadStructured["method"] != "PUT" {
		t.Fatalf("expected PUT upload URL, got %#v", uploadStructured["method"])
	}
	if got := uploadStructured["url"].(string); got[:len("http://gateway.test")] != "http://gateway.test" {
		t.Fatalf("expected configured base URL, got %q", got)
	}

	service.files["result-1"] = southbound.RawJSON{
		"file_id":      "result-1",
		"assistant_id": "demo-main",
		"filename":     "result.txt",
		"content_type": "text/plain",
	}
	service.bytes["result-1"] = []byte("assistant result")

	getRecorder := performMCPRequest(t, handler, sessionID, protocolVersion, map[string]any{
		"jsonrpc": "2.0",
		"id":      4,
		"method":  "tools/call",
		"params": map[string]any{
			"name": "ironagent.files.get",
			"arguments": map[string]any{
				"file_id": "result-1",
			},
		},
	}, http.StatusOK)
	var getResponse responseEnvelope
	decodeMCPResponse(t, getRecorder, &getResponse)
	getResult := getResponse.Result.(map[string]any)
	getStructured := getResult["structuredContent"].(map[string]any)
	if getStructured["file_id"] != "result-1" {
		t.Fatalf("expected result-1 metadata, got %#v", getStructured)
	}

	downloadRecorder := performMCPRequest(t, handler, sessionID, protocolVersion, map[string]any{
		"jsonrpc": "2.0",
		"id":      5,
		"method":  "tools/call",
		"params": map[string]any{
			"name": "ironagent.files.download_url",
			"arguments": map[string]any{
				"file_id": "result-1",
			},
		},
	}, http.StatusOK)
	var downloadResponse responseEnvelope
	decodeMCPResponse(t, downloadRecorder, &downloadResponse)
	downloadResult := downloadResponse.Result.(map[string]any)
	downloadStructured := downloadResult["structuredContent"].(map[string]any)
	if downloadStructured["method"] != "GET" {
		t.Fatalf("expected GET download URL, got %#v", downloadStructured["method"])
	}
}

func TestMCPStreamAndDeleteSession(t *testing.T) {
	t.Parallel()

	service := newFakeService()
	handler := NewHandler(slog.New(slog.NewTextHandler(io.Discard, nil)), service)

	initializeRecorder := performMCPRequest(t, handler, "", "", map[string]any{
		"jsonrpc": "2.0",
		"id":      1,
		"method":  "initialize",
		"params": map[string]any{
			"protocolVersion": protocolVersion,
		},
	}, http.StatusOK)
	sessionID := initializeRecorder.Header().Get(sessionHeader)

	streamRequest := httptest.NewRequest(http.MethodGet, "/mcp", nil)
	streamRequest.Header.Set(sessionHeader, sessionID)
	streamRecorder := httptest.NewRecorder()
	handler.ServeHTTP(streamRecorder, streamRequest)
	if streamRecorder.Code != http.StatusOK {
		t.Fatalf("expected 200 stream, got %d", streamRecorder.Code)
	}
	if streamRecorder.Header().Get("Content-Type") != "text/event-stream" {
		t.Fatalf("unexpected stream content type %q", streamRecorder.Header().Get("Content-Type"))
	}

	deleteRequest := httptest.NewRequest(http.MethodDelete, "/mcp", nil)
	deleteRequest.Header.Set(sessionHeader, sessionID)
	deleteRecorder := httptest.NewRecorder()
	handler.ServeHTTP(deleteRecorder, deleteRequest)
	if deleteRecorder.Code != http.StatusNoContent {
		t.Fatalf("expected 204 delete, got %d", deleteRecorder.Code)
	}
}

func TestMCPRejectsOversizedRequest(t *testing.T) {
	t.Parallel()

	handler := NewHandler(slog.New(slog.NewTextHandler(io.Discard, nil)), newFakeService())
	request := httptest.NewRequest(http.MethodPost, "/mcp", strings.NewReader(strings.Repeat("a", maxRequestBytes+1)))
	request.Header.Set("Accept", "application/json, text/event-stream")
	request.Header.Set("Content-Type", "application/json")

	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("expected 413, got %d, body=%s", recorder.Code, recorder.Body.String())
	}
}

func performMCPRequest(
	t *testing.T,
	handler http.Handler,
	sessionID string,
	version string,
	payload any,
	wantStatus int,
) *httptest.ResponseRecorder {
	t.Helper()
	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal payload: %v", err)
	}
	request := httptest.NewRequest(http.MethodPost, "/mcp", bytes.NewReader(raw))
	request.Header.Set("Accept", "application/json, text/event-stream")
	request.Header.Set("Content-Type", "application/json")
	if sessionID != "" {
		request.Header.Set(sessionHeader, sessionID)
	}
	if version != "" {
		request.Header.Set(versionHeader, version)
	}
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	if recorder.Code != wantStatus {
		t.Fatalf("expected %d, got %d, body=%s", wantStatus, recorder.Code, recorder.Body.String())
	}
	return recorder
}

func decodeMCPResponse(t *testing.T, recorder *httptest.ResponseRecorder, target *responseEnvelope) {
	t.Helper()
	if err := json.Unmarshal(recorder.Body.Bytes(), target); err != nil {
		t.Fatalf("decode response: %v", err)
	}
}

type fakeService struct {
	files map[string]southbound.RawJSON
	bytes map[string][]byte
}

func newFakeService() *fakeService {
	return &fakeService{
		files: make(map[string]southbound.RawJSON),
		bytes: make(map[string][]byte),
	}
}

func (f *fakeService) UploadFile(_ context.Context, request core.FileUploadRequest) (southbound.RawJSON, error) {
	f.files["file-1"] = southbound.RawJSON{
		"file_id":      "file-1",
		"assistant_id": request.AssistantID,
		"filename":     request.Filename,
		"content_type": request.ContentType,
	}
	f.bytes["file-1"] = append([]byte(nil), request.Content...)
	return f.files["file-1"], nil
}

func (f *fakeService) ListFiles(_ context.Context, _ core.FileListRequest) (southbound.RawJSON, error) {
	files := make([]any, 0, len(f.files))
	for _, file := range f.files {
		files = append(files, file)
	}
	return southbound.RawJSON{
		"files": files,
		"total": len(files),
	}, nil
}

func (f *fakeService) SyncResults(_ context.Context, request core.SyncResultsRequest) (southbound.WorkspaceScanSummary, error) {
	f.files["result-1"] = southbound.RawJSON{
		"file_id":      "result-1",
		"assistant_id": request.AssistantID,
		"filename":     "result.txt",
		"content_type": "text/plain",
		"file_role":    "result",
	}
	f.bytes["result-1"] = []byte("assistant result")
	return southbound.WorkspaceScanSummary{
		AssistantsScanned: 1,
		FilesScanned:      1,
		FilesSynced:       1,
	}, nil
}

func (f *fakeService) GetFile(_ context.Context, fileID string) (southbound.RawJSON, error) {
	return f.files[fileID], nil
}

func (f *fakeService) DeleteFile(_ context.Context, fileID string, _ core.FileDeleteRequest) (southbound.RawJSON, error) {
	delete(f.files, fileID)
	delete(f.bytes, fileID)
	return southbound.RawJSON{
		"file_id": fileID,
		"status":  "deleted",
	}, nil
}
