package mcpapi

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"

	"ironagent/go-gateway/internal/core"
	"ironagent/go-gateway/internal/presign"
	"ironagent/go-gateway/internal/southbound"
)

const (
	protocolVersion = "2025-06-18"
	sessionHeader   = "Mcp-Session-Id"
	versionHeader   = "MCP-Protocol-Version"
	serverName      = "ironagent-go-gateway"
	serverVersion   = "0.1.0"
	maxRequestBytes = 1 << 20
)

const (
	fileToolPrefix = "ironagent.files."
)

type Service interface {
	UploadFile(ctx context.Context, request core.FileUploadRequest) (southbound.RawJSON, error)
	ListFiles(ctx context.Context, request core.FileListRequest) (southbound.RawJSON, error)
	SyncResults(ctx context.Context, request core.SyncResultsRequest) (southbound.WorkspaceScanSummary, error)
	GetFile(ctx context.Context, fileID string) (southbound.RawJSON, error)
	DeleteFile(ctx context.Context, fileID string, request core.FileDeleteRequest) (southbound.RawJSON, error)
}

type Presigner interface {
	CreateUploadURL(baseURL string, request presign.UploadRequest) (presign.URLResponse, error)
	CreateDownloadURL(baseURL string, request presign.DownloadRequest) (presign.URLResponse, error)
}

type Handler struct {
	logger        *slog.Logger
	service       Service
	presigner     Presigner
	publicBaseURL string
	sessions      *sessionStore
}

type sessionStore struct {
	mu       sync.RWMutex
	versions map[string]string
}

func NewHandler(logger *slog.Logger, service Service) http.Handler {
	return NewHandlerWithPresigner(logger, service, nil, "")
}

func NewHandlerWithPresigner(
	logger *slog.Logger,
	service Service,
	presigner Presigner,
	publicBaseURL string,
) http.Handler {
	return &Handler{
		logger:        logger,
		service:       service,
		presigner:     presigner,
		publicBaseURL: strings.TrimRight(strings.TrimSpace(publicBaseURL), "/"),
		sessions:      &sessionStore{versions: make(map[string]string)},
	}
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if err := validateOrigin(r.Header.Get("Origin")); err != nil {
		http.Error(w, err.Error(), http.StatusForbidden)
		return
	}

	switch r.Method {
	case http.MethodGet:
		h.handleStream(w, r)
	case http.MethodDelete:
		h.handleDeleteSession(w, r)
	case http.MethodPost:
		h.handleMessage(w, r)
	default:
		w.Header().Set("Allow", "GET, POST, DELETE")
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

func (h *Handler) handleStream(w http.ResponseWriter, r *http.Request) {
	if _, ok := h.requireSession(w, r); !ok {
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Connection", "keep-alive")
	w.WriteHeader(http.StatusOK)
	if flusher, ok := w.(http.Flusher); ok {
		flusher.Flush()
	}
}

func (h *Handler) handleDeleteSession(w http.ResponseWriter, r *http.Request) {
	sessionID := strings.TrimSpace(r.Header.Get(sessionHeader))
	if sessionID == "" {
		http.Error(w, sessionHeader+" is required", http.StatusBadRequest)
		return
	}
	if !h.sessions.delete(sessionID) {
		http.NotFound(w, r)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (h *Handler) handleMessage(w http.ResponseWriter, r *http.Request) {
	if !acceptsJSON(r.Header.Get("Accept")) {
		http.Error(w, "Accept must include application/json", http.StatusNotAcceptable)
		return
	}

	defer r.Body.Close()
	payload, err := io.ReadAll(io.LimitReader(r.Body, maxRequestBytes+1))
	if err != nil {
		http.Error(w, "failed to read request body", http.StatusBadRequest)
		return
	}
	if len(payload) > maxRequestBytes {
		http.Error(w, "request body too large", http.StatusRequestEntityTooLarge)
		return
	}

	var envelope requestEnvelope
	if err := json.Unmarshal(payload, &envelope); err != nil {
		h.writeJSONRPCError(w, nil, -32700, "parse error", err.Error(), http.StatusBadRequest)
		return
	}
	if envelope.JSONRPC != "2.0" {
		h.writeJSONRPCError(w, envelope.ID, -32600, "invalid request", "jsonrpc must be 2.0", http.StatusBadRequest)
		return
	}
	if envelope.Method == "" {
		w.WriteHeader(http.StatusAccepted)
		return
	}

	if envelope.Method == "initialize" {
		h.handleInitialize(w, envelope)
		return
	}

	sessionID, ok := h.requireSession(w, r)
	if !ok {
		return
	}
	if !h.validateProtocolVersion(w, r, sessionID, envelope.ID) {
		return
	}

	if envelope.ID == nil {
		w.WriteHeader(http.StatusAccepted)
		return
	}

	switch envelope.Method {
	case "ping":
		h.writeJSONRPCResult(w, envelope.ID, map[string]any{})
	case "tools/list":
		h.writeJSONRPCResult(w, envelope.ID, map[string]any{
			"tools": toolDefinitions(),
		})
	case "tools/call":
		h.handleToolCall(w, r.Context(), envelope)
	case "resources/list":
		h.writeJSONRPCResult(w, envelope.ID, map[string]any{
			"resources": []any{},
		})
	case "resources/templates/list":
		h.writeJSONRPCResult(w, envelope.ID, map[string]any{
			"resourceTemplates": []map[string]any{},
		})
	case "resources/read":
		h.handleResourceRead(w, r.Context(), envelope)
	case "notifications/initialized":
		w.WriteHeader(http.StatusAccepted)
	default:
		h.writeJSONRPCError(w, envelope.ID, -32601, "method not found", envelope.Method, http.StatusOK)
	}
}

func (h *Handler) handleInitialize(w http.ResponseWriter, envelope requestEnvelope) {
	requestedVersion := protocolVersion
	if envelope.Params != nil {
		var params initializeParams
		if err := json.Unmarshal(envelope.Params, &params); err == nil && strings.TrimSpace(params.ProtocolVersion) != "" {
			requestedVersion = strings.TrimSpace(params.ProtocolVersion)
		}
	}
	negotiated := requestedVersion
	if requestedVersion != protocolVersion {
		negotiated = protocolVersion
	}

	sessionID, err := generateSessionID()
	if err != nil {
		h.writeJSONRPCError(w, envelope.ID, -32000, "server error", err.Error(), http.StatusInternalServerError)
		return
	}
	h.sessions.put(sessionID, negotiated)

	w.Header().Set("Content-Type", "application/json")
	w.Header().Set(sessionHeader, sessionID)
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(responseEnvelope{
		JSONRPC: "2.0",
		ID:      envelope.ID,
		Result: map[string]any{
			"protocolVersion": negotiated,
			"capabilities": map[string]any{
				"resources": map[string]any{},
				"tools":     map[string]any{},
			},
			"serverInfo": map[string]any{
				"name":    serverName,
				"version": serverVersion,
			},
			"instructions": "Use ironagent.files.* tools for file management. File bytes must move through presigned HTTP URLs, not MCP payloads.",
		},
	})
}

func (h *Handler) handleToolCall(w http.ResponseWriter, ctx context.Context, envelope requestEnvelope) {
	var params toolCallParams
	if err := unmarshalParams(envelope.Params, &params); err != nil {
		h.writeJSONRPCError(w, envelope.ID, -32602, "invalid params", err.Error(), http.StatusOK)
		return
	}

	result, err := h.callTool(ctx, params)
	if err != nil {
		h.writeJSONRPCResult(w, envelope.ID, toolErrorResult(err))
		return
	}
	h.writeJSONRPCResult(w, envelope.ID, result)
}

func (h *Handler) callTool(ctx context.Context, params toolCallParams) (map[string]any, error) {
	switch params.Name {
	case fileToolPrefix + "upload_url":
		if h.presigner == nil {
			return nil, fmt.Errorf("presigned URL support is not configured")
		}
		var args uploadURLArgs
		if err := decodeArguments(params.Arguments, &args); err != nil {
			return nil, err
		}
		response, err := h.presigner.CreateUploadURL(h.publicBaseURL, presign.UploadRequest{
			AssistantID:    strings.TrimSpace(args.AssistantID),
			UserID:         strings.TrimSpace(args.UserID),
			TargetBucket:   strings.TrimSpace(args.TargetBucket),
			Overwrite:      args.Overwrite,
			SourceType:     strings.TrimSpace(args.SourceType),
			SourceRef:      strings.TrimSpace(args.SourceRef),
			UploaderUserID: strings.TrimSpace(args.UploaderUserID),
			Filename:       strings.TrimSpace(args.Filename),
			ContentType:    strings.TrimSpace(args.ContentType),
		})
		if err != nil {
			return nil, err
		}
		return structuredToolResult(response, nil), nil
	case fileToolPrefix + "download_url":
		if h.presigner == nil {
			return nil, fmt.Errorf("presigned URL support is not configured")
		}
		var args downloadURLArgs
		if err := decodeArguments(params.Arguments, &args); err != nil {
			return nil, err
		}
		response, err := h.presigner.CreateDownloadURL(h.publicBaseURL, presign.DownloadRequest{
			FileID:      strings.TrimSpace(args.FileID),
			Disposition: strings.TrimSpace(args.Disposition),
		})
		if err != nil {
			return nil, err
		}
		return structuredToolResult(response, nil), nil
	case fileToolPrefix + "list":
		var args listFilesArgs
		if err := decodeArguments(params.Arguments, &args); err != nil {
			return nil, err
		}
		response, err := h.service.ListFiles(ctx, core.FileListRequest{
			AssistantID: strings.TrimSpace(args.AssistantID),
			UserID:      strings.TrimSpace(args.UserID),
			Bucket:      strings.TrimSpace(args.Bucket),
			Direction:   strings.TrimSpace(args.Direction),
			FileRole:    strings.TrimSpace(args.FileRole),
		})
		if err != nil {
			return nil, err
		}
		return structuredToolResult(response, nil), nil
	case fileToolPrefix + "get":
		var args getFileArgs
		if err := decodeArguments(params.Arguments, &args); err != nil {
			return nil, err
		}
		response, err := h.service.GetFile(ctx, strings.TrimSpace(args.FileID))
		if err != nil {
			return nil, err
		}
		return structuredToolResult(response, nil), nil
	case fileToolPrefix + "delete":
		var args deleteFileArgs
		if err := decodeArguments(params.Arguments, &args); err != nil {
			return nil, err
		}
		response, err := h.service.DeleteFile(ctx, strings.TrimSpace(args.FileID), core.FileDeleteRequest{
			UserID: strings.TrimSpace(args.UserID),
		})
		if err != nil {
			return nil, err
		}
		return structuredToolResult(response, nil), nil
	default:
		return nil, fmt.Errorf("unknown tool %q", params.Name)
	}
}

func (h *Handler) handleResourceRead(w http.ResponseWriter, ctx context.Context, envelope requestEnvelope) {
	var params resourceReadParams
	if err := unmarshalParams(envelope.Params, &params); err != nil {
		h.writeJSONRPCError(w, envelope.ID, -32602, "invalid params", err.Error(), http.StatusOK)
		return
	}

	resourceURI, err := url.Parse(strings.TrimSpace(params.URI))
	if err != nil {
		h.writeJSONRPCError(w, envelope.ID, -32602, "invalid params", "uri must be a valid URI", http.StatusOK)
		return
	}
	if resourceURI.Scheme != "ironagent" || resourceURI.Host != "files" {
		h.writeJSONRPCError(w, envelope.ID, -32602, "invalid params", "unsupported resource URI", http.StatusOK)
		return
	}
	_ = ctx
	h.writeJSONRPCError(w, envelope.ID, -32601, "method not supported", "file bytes must be downloaded with ironagent.files.download_url", http.StatusOK)
}

func (h *Handler) requireSession(w http.ResponseWriter, r *http.Request) (string, bool) {
	sessionID := strings.TrimSpace(r.Header.Get(sessionHeader))
	if sessionID == "" {
		http.Error(w, sessionHeader+" is required", http.StatusBadRequest)
		return "", false
	}
	if _, ok := h.sessions.get(sessionID); !ok {
		http.NotFound(w, r)
		return "", false
	}
	return sessionID, true
}

func (h *Handler) validateProtocolVersion(
	w http.ResponseWriter,
	r *http.Request,
	sessionID string,
	id json.RawMessage,
) bool {
	expected, _ := h.sessions.get(sessionID)
	requested := strings.TrimSpace(r.Header.Get(versionHeader))
	if requested == "" {
		return true
	}
	if requested != expected {
		h.writeJSONRPCError(w, id, -32600, "invalid request", "unsupported MCP protocol version", http.StatusBadRequest)
		return false
	}
	return true
}

func (h *Handler) writeJSONRPCResult(w http.ResponseWriter, id json.RawMessage, result any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(responseEnvelope{
		JSONRPC: "2.0",
		ID:      id,
		Result:  result,
	})
}

func (h *Handler) writeJSONRPCError(
	w http.ResponseWriter,
	id json.RawMessage,
	code int,
	message string,
	data any,
	statusCode int,
) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(statusCode)
	_ = json.NewEncoder(w).Encode(responseEnvelope{
		JSONRPC: "2.0",
		ID:      id,
		Error: &responseError{
			Code:    code,
			Message: message,
			Data:    data,
		},
	})
}

func (s *sessionStore) put(sessionID string, version string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.versions[sessionID] = version
}

func (s *sessionStore) get(sessionID string) (string, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	version, ok := s.versions[sessionID]
	return version, ok
}

func (s *sessionStore) delete(sessionID string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.versions[sessionID]; !ok {
		return false
	}
	delete(s.versions, sessionID)
	return true
}

type requestEnvelope struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method,omitempty"`
	Params  json.RawMessage `json:"params,omitempty"`
}

type responseEnvelope struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Result  any             `json:"result,omitempty"`
	Error   *responseError  `json:"error,omitempty"`
}

type responseError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
	Data    any    `json:"data,omitempty"`
}

type initializeParams struct {
	ProtocolVersion string `json:"protocolVersion"`
}

type toolCallParams struct {
	Name      string         `json:"name"`
	Arguments map[string]any `json:"arguments"`
}

type resourceReadParams struct {
	URI string `json:"uri"`
}

type uploadURLArgs struct {
	AssistantID    string `json:"assistant_id"`
	UserID         string `json:"user_id"`
	TargetBucket   string `json:"target_bucket"`
	Overwrite      bool   `json:"overwrite"`
	SourceType     string `json:"source_type"`
	SourceRef      string `json:"source_ref"`
	UploaderUserID string `json:"uploader_user_id"`
	Filename       string `json:"filename"`
	ContentType    string `json:"content_type"`
}

type listFilesArgs struct {
	AssistantID string `json:"assistant_id"`
	UserID      string `json:"user_id"`
	Bucket      string `json:"bucket"`
	Direction   string `json:"direction"`
	FileRole    string `json:"file_role"`
}

type getFileArgs struct {
	FileID string `json:"file_id"`
}

type downloadURLArgs struct {
	FileID      string `json:"file_id"`
	Disposition string `json:"disposition"`
}

type deleteFileArgs struct {
	FileID string `json:"file_id"`
	UserID string `json:"user_id"`
}

func toolDefinitions() []map[string]any {
	return fileToolDefinitions(fileToolPrefix)
}

func fileToolDefinitions(prefix string) []map[string]any {
	return []map[string]any{
		{
			"name":        prefix + "upload_url",
			"title":       "Create File Upload URL",
			"description": "Create a presigned HTTP PUT URL for file bytes. MCP never carries file content.",
			"inputSchema": map[string]any{
				"type": "object",
				"properties": map[string]any{
					"assistant_id":     map[string]any{"type": "string", "description": "Target assistant ID."},
					"user_id":          map[string]any{"type": "string", "description": "User ID used for assistant binding resolve."},
					"target_bucket":    map[string]any{"type": "string", "description": "Workspace bucket: temp or persistent."},
					"overwrite":        map[string]any{"type": "boolean"},
					"source_type":      map[string]any{"type": "string"},
					"source_ref":       map[string]any{"type": "string"},
					"uploader_user_id": map[string]any{"type": "string"},
					"filename":         map[string]any{"type": "string"},
					"content_type":     map[string]any{"type": "string"},
				},
				"required": []string{"filename"},
			},
		},
		{
			"name":        prefix + "download_url",
			"title":       "Create File Download URL",
			"description": "Create a presigned HTTP GET URL for file bytes.",
			"inputSchema": map[string]any{
				"type": "object",
				"properties": map[string]any{
					"file_id":     map[string]any{"type": "string"},
					"disposition": map[string]any{"type": "string"},
				},
				"required": []string{"file_id"},
			},
		},
		{
			"name":        prefix + "list",
			"title":       "List Workspace Files",
			"description": "List workspace files for an assistant or user binding.",
			"inputSchema": map[string]any{
				"type": "object",
				"properties": map[string]any{
					"assistant_id": map[string]any{"type": "string"},
					"user_id":      map[string]any{"type": "string"},
					"bucket":       map[string]any{"type": "string"},
					"direction":    map[string]any{"type": "string"},
					"file_role":    map[string]any{"type": "string"},
				},
			},
		},
		{
			"name":        prefix + "get",
			"title":       "Get Workspace File Metadata",
			"description": "Fetch metadata for a workspace file.",
			"inputSchema": map[string]any{
				"type": "object",
				"properties": map[string]any{
					"file_id": map[string]any{"type": "string"},
				},
				"required": []string{"file_id"},
			},
		},
		{
			"name":        prefix + "delete",
			"title":       "Delete Workspace File",
			"description": "Delete a workspace file through the control plane.",
			"inputSchema": map[string]any{
				"type": "object",
				"properties": map[string]any{
					"file_id": map[string]any{"type": "string"},
					"user_id": map[string]any{"type": "string"},
				},
				"required": []string{"file_id"},
			},
		},
	}
}

func structuredToolResult(structured any, content []map[string]any) map[string]any {
	text := mustJSON(structured)
	items := []map[string]any{
		{
			"type": "text",
			"text": text,
		},
	}
	items = append(items, content...)
	return map[string]any{
		"content":           items,
		"structuredContent": structured,
		"isError":           false,
	}
}

func toolErrorResult(err error) map[string]any {
	return map[string]any{
		"content": []map[string]any{
			{
				"type": "text",
				"text": err.Error(),
			},
		},
		"isError": true,
	}
}

func decodeArguments(arguments map[string]any, target any) error {
	if arguments == nil {
		arguments = map[string]any{}
	}
	payload, err := json.Marshal(arguments)
	if err != nil {
		return err
	}
	return json.Unmarshal(payload, target)
}

func unmarshalParams(raw json.RawMessage, target any) error {
	if len(raw) == 0 {
		return errors.New("params are required")
	}
	if err := json.Unmarshal(raw, target); err != nil {
		return err
	}
	return nil
}

func acceptsJSON(accept string) bool {
	accept = strings.ToLower(accept)
	return strings.Contains(accept, "application/json")
}

func validateOrigin(origin string) error {
	trimmed := strings.TrimSpace(origin)
	if trimmed == "" {
		return nil
	}
	parsed, err := url.Parse(trimmed)
	if err != nil {
		return fmt.Errorf("invalid Origin header")
	}
	host := parsed.Hostname()
	if host == "localhost" {
		return nil
	}
	ip := net.ParseIP(host)
	if ip != nil && ip.IsLoopback() {
		return nil
	}
	return fmt.Errorf("Origin header is not allowed")
}

func generateSessionID() (string, error) {
	var raw [16]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return "", err
	}
	return hex.EncodeToString(raw[:]), nil
}

func mustJSON(value any) string {
	payload, err := json.Marshal(value)
	if err != nil {
		return "{}"
	}
	return string(payload)
}
