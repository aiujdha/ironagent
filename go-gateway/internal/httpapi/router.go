package httpapi

import (
	"bufio"
	"bytes"
	"context"
	"crypto/hmac"
	cryptorand "crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"mime/multipart"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"ironagent/go-gateway/internal/auth"
	"ironagent/go-gateway/internal/core"
	"ironagent/go-gateway/internal/metrics"
	"ironagent/go-gateway/internal/presign"
	"ironagent/go-gateway/internal/runtimepool"
	"ironagent/go-gateway/internal/southbound"
	"ironagent/go-gateway/internal/webhook"

	"github.com/gorilla/websocket"
)

const defaultMaxUploadBytes = 50 * 1024 * 1024
const detachedTaskMutationTimeout = 5 * time.Second
const assistantProxySessionCookie = "iron_agent_proxy_session"
const adminProxySessionCookie = "iron_agent_admin_session"

type proxySessionConfig struct {
	secret []byte
	ttl    time.Duration
}

type proxyCircuitBreaker struct {
	mu           sync.Mutex
	threshold    int
	openDuration time.Duration
	failures     map[string]int
	openedUntil  map[string]time.Time
}

func newProxyCircuitBreaker(threshold int, openDuration time.Duration) *proxyCircuitBreaker {
	if threshold <= 0 || openDuration <= 0 {
		return nil
	}
	return &proxyCircuitBreaker{
		threshold:    threshold,
		openDuration: openDuration,
		failures:     make(map[string]int),
		openedUntil:  make(map[string]time.Time),
	}
}

func (c *proxyCircuitBreaker) allow(assistantID string) bool {
	if c == nil || assistantID == "" {
		return true
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	until, ok := c.openedUntil[assistantID]
	if !ok {
		return true
	}
	if time.Now().Before(until) {
		return false
	}
	delete(c.openedUntil, assistantID)
	delete(c.failures, assistantID)
	return true
}

func (c *proxyCircuitBreaker) record(assistantID string, failed bool) bool {
	if c == nil || assistantID == "" {
		return false
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if !failed {
		delete(c.failures, assistantID)
		delete(c.openedUntil, assistantID)
		return false
	}
	c.failures[assistantID]++
	if c.failures[assistantID] >= c.threshold {
		c.openedUntil[assistantID] = time.Now().Add(c.openDuration)
		return true
	}
	return false
}

type Service interface {
	CreateChatTask(ctx context.Context, request core.ChatRequest) (core.TaskView, error)
	UserExists(ctx context.Context, userID string) (bool, error)
	GetRuntime(ctx context.Context, request core.RuntimeRequest) (core.RuntimeView, error)
	StartRuntime(ctx context.Context, request core.RuntimeRequest) (core.RuntimeView, error)
	StopRuntime(ctx context.Context, request core.RuntimeRequest) (core.RuntimeView, error)
	ResolveControlUILaunchTicket(ctx context.Context, ticket string, origin string) (core.ControlUILaunchSession, error)
	GetTask(ctx context.Context, taskID string) (core.TaskView, error)
	StartTask(ctx context.Context, taskID string) (core.TaskView, error)
	CompleteTask(ctx context.Context, taskID string, request core.CompleteTaskRequest) (core.TaskView, error)
	FailTask(ctx context.Context, taskID string, request core.FailTaskRequest) (core.TaskView, error)
	ResolveTaskApproval(ctx context.Context, taskID string, request core.ApprovalResolutionRequest) (core.TaskView, error)
	GetApproval(ctx context.Context, approvalID string) (core.ApprovalResponse, error)
	ListApprovals(ctx context.Context, status string, assistantID string, approvalScope string) ([]core.ApprovalResponse, error)
	ResolveApprovalDecision(ctx context.Context, approvalID string, request core.ApprovalDecisionRequest) (core.ApprovalDecisionResponse, error)
	CancelTask(ctx context.Context, taskID string, request core.CancelTaskRequest) (core.TaskView, error)
	TaskQueueStats(ctx context.Context) (core.TaskQueueStats, error)
	DBPoolStats(ctx context.Context) (core.DBPoolStats, bool)
	Health(ctx context.Context) core.HealthReport
	UploadFile(ctx context.Context, request core.FileUploadRequest) (southbound.RawJSON, error)
	ListFiles(ctx context.Context, request core.FileListRequest) (southbound.RawJSON, error)
	SyncResults(ctx context.Context, request core.SyncResultsRequest) (southbound.WorkspaceScanSummary, error)
	GetFile(ctx context.Context, fileID string) (southbound.RawJSON, error)
	DownloadFile(ctx context.Context, fileID string, disposition string) (core.FileDownloadResponse, error)
	DownloadFileStream(ctx context.Context, fileID string, disposition string) (core.FileDownloadStream, error)
	DeleteFile(ctx context.Context, fileID string, request core.FileDeleteRequest) (southbound.RawJSON, error)
}

type queuedTaskHookSetter interface {
	SetQueuedTaskHook(func(context.Context, core.TaskView))
}

type queuedTaskProgressHookSetter interface {
	SetQueuedTaskProgressHook(func(context.Context, core.QueuedTaskProgressUpdate) error)
}

type chatAdmissionChecker interface {
	CheckChatAdmission(ctx context.Context, request core.ChatRequest) error
}

type queuedTaskActivator interface {
	ActivateQueuedTask(ctx context.Context, taskID string) error
}

type storedTaskRecordProvider interface {
	StoredTaskRecord(ctx context.Context, taskID string) (core.GatewayTaskRecord, bool, error)
}

type handler struct {
	logger                      *slog.Logger
	service                     Service
	webhooks                    *webhook.Dispatcher
	presigns                    *presign.Manager
	runtimes                    *runtimepool.Registry
	publicBaseURL               string
	adminBaseURL                string
	adminAuthDisabled           bool
	internalServiceToken        string
	securityBaseURL             string
	defaultCBURL                string
	callbackRequired            bool
	callbackHosts               []string
	allowPrivateCB              bool
	callbackLookup              func(context.Context, string) ([]net.IP, error)
	maxUploadBytes              int64
	proxySession                proxySessionConfig
	proxySlots                  chan struct{}
	proxyCircuit                *proxyCircuitBreaker
	dbPoolStatsProviders        map[string]core.DBPoolStatsProvider
	taskProgressCallbackEnabled bool
}

type RouterOptions struct {
	Webhooks                    *webhook.Dispatcher
	Presigns                    *presign.Manager
	RuntimeRegistry             *runtimepool.Registry
	PublicBaseURL               string
	AdminBaseURL                string
	AdminAuthDisabled           bool
	InternalServiceToken        string
	SecurityBaseURL             string
	DefaultCallbackURL          string
	CallbackRequired            bool
	CallbackAllowedHosts        []string
	CallbackAllowPrivate        bool
	CallbackIPLookup            func(context.Context, string) ([]net.IP, error)
	MaxUploadBytes              int64
	ProxySessionSecret          string
	ProxySessionTTL             time.Duration
	ProxyMaxInFlight            int
	ProxyFailureThreshold       int
	ProxyCircuitOpenDuration    time.Duration
	DBPoolStatsProviders        map[string]core.DBPoolStatsProvider
	TaskProgressCallbackEnabled bool
}

func normalizeDBPoolStatsProviders(
	providers map[string]core.DBPoolStatsProvider,
) map[string]core.DBPoolStatsProvider {
	if len(providers) == 0 {
		return nil
	}
	normalized := make(map[string]core.DBPoolStatsProvider, len(providers))
	for name, provider := range providers {
		name = strings.TrimSpace(name)
		if name == "" || provider == nil {
			continue
		}
		normalized[name] = provider
	}
	if len(normalized) == 0 {
		return nil
	}
	return normalized
}

func NewRouter(
	logger *slog.Logger,
	service Service,
	authMiddleware func(http.Handler) http.Handler,
	mcpHandler http.Handler,
) http.Handler {
	return NewRouterWithOptions(logger, service, authMiddleware, mcpHandler, RouterOptions{})
}

func NewRouterWithOptions(
	logger *slog.Logger,
	service Service,
	authMiddleware func(http.Handler) http.Handler,
	mcpHandler http.Handler,
	options RouterOptions,
) http.Handler {
	maxUploadBytes := options.MaxUploadBytes
	if maxUploadBytes <= 0 {
		maxUploadBytes = defaultMaxUploadBytes
	}
	proxySessionTTL := options.ProxySessionTTL
	if proxySessionTTL <= 0 {
		proxySessionTTL = 30 * time.Minute
	}
	h := &handler{
		logger:               logger,
		service:              service,
		webhooks:             options.Webhooks,
		presigns:             options.Presigns,
		runtimes:             options.RuntimeRegistry,
		publicBaseURL:        strings.TrimRight(strings.TrimSpace(options.PublicBaseURL), "/"),
		adminBaseURL:         strings.TrimRight(strings.TrimSpace(options.AdminBaseURL), "/"),
		adminAuthDisabled:    options.AdminAuthDisabled,
		internalServiceToken: strings.TrimSpace(options.InternalServiceToken),
		securityBaseURL:      strings.TrimRight(strings.TrimSpace(options.SecurityBaseURL), "/"),
		defaultCBURL:         strings.TrimSpace(options.DefaultCallbackURL),
		callbackRequired:     options.CallbackRequired,
		callbackHosts:        normalizeAllowedHosts(options.CallbackAllowedHosts),
		allowPrivateCB:       options.CallbackAllowPrivate,
		callbackLookup:       options.CallbackIPLookup,
		maxUploadBytes:       maxUploadBytes,
		proxySession: proxySessionConfig{
			secret: []byte(strings.TrimSpace(options.ProxySessionSecret)),
			ttl:    proxySessionTTL,
		},
		proxyCircuit:                newProxyCircuitBreaker(options.ProxyFailureThreshold, options.ProxyCircuitOpenDuration),
		dbPoolStatsProviders:        normalizeDBPoolStatsProviders(options.DBPoolStatsProviders),
		taskProgressCallbackEnabled: options.TaskProgressCallbackEnabled,
	}
	if options.ProxyMaxInFlight > 0 {
		h.proxySlots = make(chan struct{}, options.ProxyMaxInFlight)
	}
	if h.callbackLookup == nil {
		h.callbackLookup = defaultCallbackIPLookup
	}
	if h.webhooks != nil {
		if setter, ok := service.(queuedTaskHookSetter); ok {
			setter.SetQueuedTaskHook(h.dispatchQueuedTaskUpdate)
		}
		if setter, ok := service.(queuedTaskProgressHookSetter); ok {
			setter.SetQueuedTaskProgressHook(h.dispatchQueuedTaskProgressUpdate)
		}
	}

	root := http.NewServeMux()
	root.HandleFunc("GET /health", h.auditPublicBusinessAPIFailure("public_api.health", h.health))
	root.HandleFunc("GET /healthz", h.healthz)
	root.HandleFunc("GET /readyz", h.readyz)
	root.HandleFunc("GET /api/v1/health", h.auditPublicBusinessAPIFailure("public_api.health", h.userHealth))
	root.HandleFunc("GET /api/v1/users/verify", h.verifyUser)
	root.Handle("GET /metrics", authMiddleware(http.HandlerFunc(h.getPrometheusMetrics)))
	if h.adminBaseURL != "" {
		root.HandleFunc("GET /admin", h.redirectAdminRoot)
		root.HandleFunc("GET /admin/login", h.adminLoginPage)
		root.HandleFunc("POST /admin/login", h.adminLogin)
		root.Handle("/admin/", h.adminProxyAuth(http.HandlerFunc(h.proxyAdminUI)))
		root.Handle("/api/v1/admin/", h.adminProxyAuth(http.HandlerFunc(h.proxyAdminAPI)))
		root.Handle("/api/v1/market/", h.adminProxyAuth(http.HandlerFunc(h.proxyAdminAPI)))
		root.Handle("/api/v1/workspace-files", h.adminProxyAuth(http.HandlerFunc(h.proxyAdminAPI)))
		root.Handle("/api/v1/workspace-files/", h.adminProxyAuth(http.HandlerFunc(h.proxyAdminAPI)))
	}
	if h.securityBaseURL != "" {
		root.Handle("/api/v1/hooks/", http.HandlerFunc(h.proxySecurityAPI))
		root.Handle("/api/v1/security/", http.HandlerFunc(h.proxySecurityAPI))
		root.Handle("/api/v1/audit/", http.HandlerFunc(h.proxySecurityAPI))
		root.Handle("/api/v1/approvals", http.HandlerFunc(h.proxySecurityAPI))
		root.Handle("/api/v1/approvals/", http.HandlerFunc(h.proxySecurityAPI))
		root.Handle("/api/v1/assistants/", http.HandlerFunc(h.proxySecurityAPI))
		root.Handle("/api/v1/control-ui/", http.HandlerFunc(h.proxySecurityAPI))
	}
	if h.presigns != nil {
		root.HandleFunc("PUT /api/v1/files/presigned/upload/{token}", h.uploadPresignedFile)
		root.HandleFunc("GET /api/v1/files/presigned/download/{token}", h.downloadPresignedFile)
	}

	api := http.NewServeMux()
	api.HandleFunc("POST /api/v1/chat", h.auditPublicBusinessAPIFailure("public_api.chat", h.createChatTask))
	api.HandleFunc("POST /api/v1/runtime/start", h.startRuntime)
	api.HandleFunc("GET /api/v1/runtime", h.getRuntime)
	api.HandleFunc("POST /api/v1/runtime/stop", h.stopRuntime)
	if h.runtimes != nil {
		api.HandleFunc("GET /api/v1/runtime/registry", h.listRuntimeRegistry)
		api.HandleFunc("GET /api/v1/runtime/scheduler/last-decision", h.getRuntimeSchedulerDecision)
		api.HandleFunc("GET /api/v1/runtime/registry/{assistant_id}", h.getRuntimeRegistry)
		api.HandleFunc("PUT /api/v1/runtime/registry/{assistant_id}", h.upsertRuntimeRegistry)
		api.HandleFunc("POST /api/v1/runtime/registry/{assistant_id}/heartbeat", h.heartbeatRuntimeRegistry)
	}
	api.HandleFunc("GET /api/v1/tasks/{task_id}", h.auditPublicBusinessAPIFailure("public_api.tasks.get", h.getTask))
	api.HandleFunc("POST /api/v1/tasks/{task_id}/start", h.startTask)
	api.HandleFunc("POST /api/v1/tasks/{task_id}/complete", h.completeTask)
	api.HandleFunc("POST /api/v1/tasks/{task_id}/fail", h.failTask)
	api.HandleFunc("POST /api/v1/tasks/{task_id}/approvals/resolve", h.resolveTaskApproval)
	api.HandleFunc("POST /api/v1/tasks/{task_id}/cancel", h.auditPublicBusinessAPIFailure("public_api.tasks.cancel", h.cancelTask))
	api.HandleFunc("GET /api/v1/task-queue", h.getTaskQueueStats)
	api.HandleFunc("GET /api/v1/metrics", h.getMetrics)
	api.HandleFunc("GET /api/v1/metrics/prometheus", h.getPrometheusMetrics)
	api.HandleFunc("GET /api/v1/approvals/{approval_id}", h.auditPublicBusinessAPIFailure("public_api.approvals.get", h.getApproval))
	api.HandleFunc("POST /api/v1/approvals/{approval_id}/decision", h.auditPublicBusinessAPIFailure("public_api.approvals.decision", h.decideApproval))
	api.HandleFunc("POST /api/v1/files/upload", h.auditPublicBusinessAPIFailure("public_api.files.upload", h.uploadFile))
	api.HandleFunc("GET /api/v1/files", h.listFiles)
	api.HandleFunc("POST /api/v1/files/sync-results", h.syncResults)
	api.HandleFunc("POST /api/v1/files/upload-url", h.createUploadURL)
	api.HandleFunc("POST /api/v1/files/download-url", h.createDownloadURL)
	api.HandleFunc("GET /api/v1/files/{file_id}", h.auditPublicBusinessAPIFailure("public_api.files.get", h.getFile))
	api.HandleFunc("GET /api/v1/files/{file_id}/download", h.auditPublicBusinessAPIFailure("public_api.files.download", h.downloadFile))
	api.HandleFunc("POST /api/v1/files/{file_id}/delete", h.deleteFile)
	if h.webhooks != nil {
		api.HandleFunc("POST /api/v1/webhooks/ack", h.ackWebhook)
		api.HandleFunc("GET /api/v1/webhooks/dead-letter", h.listWebhookDeadLetters)
		api.HandleFunc("POST /api/v1/webhooks/dead-letter/{event_id}/replay", h.replayWebhookDeadLetter)
	}
	root.Handle("GET /api/v1/approvals/{approval_id}", authMiddleware(api))
	root.Handle("POST /api/v1/approvals/{approval_id}/decision", authMiddleware(api))
	root.Handle("/api/", authMiddleware(api))
	if mcpHandler != nil {
		root.Handle("/mcp", authMiddleware(mcpHandler))
	}
	if h.runtimes != nil {
		root.Handle("/assistants/", h.assistantProxyAuth(authMiddleware, http.HandlerFunc(h.proxyAssistantRuntime)))
	}
	return withUpstreamRequestID(root)
}

func (h *handler) redirectAdminRoot(w http.ResponseWriter, r *http.Request) {
	target := "/admin/"
	if r.URL.RawQuery != "" {
		target += "?" + r.URL.RawQuery
	}
	http.Redirect(w, r, target, http.StatusMovedPermanently)
}

func (h *handler) proxyAdminUI(w http.ResponseWriter, r *http.Request) {
	path := strings.TrimPrefix(r.URL.Path, "/admin")
	if path == "" {
		path = "/"
	}
	h.proxyAdminRequest(w, r, path, "/admin")
}

func (h *handler) proxyAdminAPI(w http.ResponseWriter, r *http.Request) {
	h.proxyAdminRequest(w, r, r.URL.Path, "")
}

func (h *handler) proxySecurityAPI(w http.ResponseWriter, r *http.Request) {
	h.proxyUpstreamRequest(w, r, h.securityBaseURL, r.URL.Path, "", "security")
}

func (h *handler) proxyAdminRequest(
	w http.ResponseWriter,
	r *http.Request,
	targetPath string,
	forwardedPrefix string,
) {
	h.proxyUpstreamRequest(w, r, h.adminBaseURL, targetPath, forwardedPrefix, "admin")
}

func (h *handler) adminProxyAuth(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if h.internalServiceToken == "" {
			writeProxyError(w, http.StatusServiceUnavailable, "admin_proxy_auth_unavailable", "internal service token is not configured")
			return
		}
		if h.stripLegacyCredentialQuery(w, r) {
			return
		}
		if h.adminAuthDisabled {
			next.ServeHTTP(w, r)
			return
		}
		if len(h.proxySession.secret) == 0 {
			writeProxyError(w, http.StatusServiceUnavailable, "admin_proxy_auth_unavailable", "admin proxy session secret is not configured")
			return
		}
		if !h.proxySession.validCookie(r, adminProxySessionCookie, "admin") {
			writeProxyError(w, http.StatusUnauthorized, "admin_proxy_unauthorized", "admin proxy session is required")
			return
		}
		next.ServeHTTP(w, r)
	})
}

func (h *handler) adminLoginPage(w http.ResponseWriter, r *http.Request) {
	if h.adminAuthDisabled {
		http.Redirect(w, r, safeAdminRedirect(r.URL.Query().Get("next")), http.StatusFound)
		return
	}
	writeProxyError(w, http.StatusUnauthorized, "admin_proxy_unauthorized", "admin proxy session is required")
}

func (h *handler) adminLogin(w http.ResponseWriter, r *http.Request) {
	_ = r.ParseForm()
	next := safeAdminRedirect(r.Form.Get("next"))
	if h.adminAuthDisabled {
		http.Redirect(w, r, next, http.StatusFound)
		return
	}
	if len(h.proxySession.secret) == 0 || !h.validLegacyCredentialValues(r.Form) {
		writeProxyError(w, http.StatusUnauthorized, "admin_proxy_unauthorized", "valid admin credential is required")
		return
	}
	h.issueAdminSession(w, r)
	http.Redirect(w, r, next, http.StatusFound)
}

func (h *handler) stripLegacyCredentialQuery(w http.ResponseWriter, r *http.Request) bool {
	query := r.URL.Query()
	if !hasCredentialQuery(query) {
		return false
	}
	if len(h.proxySession.secret) > 0 && h.validLegacyCredentialValues(query) {
		h.issueAdminSession(w, r)
	}
	http.Redirect(w, r, stripLegacyCredentialQuery(r), http.StatusFound)
	return true
}

func (h *handler) validLegacyCredentialValues(values url.Values) bool {
	for key, candidates := range values {
		if !isCredentialQueryKey(key) {
			continue
		}
		for _, candidate := range candidates {
			if strings.TrimSpace(candidate) == h.internalServiceToken {
				return true
			}
		}
	}
	return false
}

func (h *handler) issueAdminSession(w http.ResponseWriter, r *http.Request) {
	expiresAt := time.Now().UTC().Add(h.proxySession.ttl)
	http.SetCookie(w, &http.Cookie{
		Name:     adminProxySessionCookie,
		Value:    h.proxySession.sign("admin", "admin-ui", expiresAt),
		Path:     "/",
		Expires:  expiresAt,
		MaxAge:   int(time.Until(expiresAt).Seconds()),
		HttpOnly: true,
		Secure:   h.secureProxySessionCookie(r),
		SameSite: http.SameSiteLaxMode,
	})
}

func (h *handler) proxyUpstreamRequest(
	w http.ResponseWriter,
	r *http.Request,
	baseURL string,
	targetPath string,
	forwardedPrefix string,
	component string,
) {
	target, err := url.Parse(baseURL)
	if err != nil || target.Scheme == "" || target.Host == "" {
		writeProxyError(w, http.StatusServiceUnavailable, component+"_proxy_unavailable", component+" proxy target is invalid")
		return
	}
	proxy := httputil.NewSingleHostReverseProxy(target)
	originalDirector := proxy.Director
	proxy.Director = func(req *http.Request) {
		originalDirector(req)
		req.URL.Path = joinURLPath(target.Path, targetPath)
		req.URL.RawPath = ""
		req.URL.RawQuery = r.URL.RawQuery
		req.Host = target.Host
		req.Header.Del("X-Forwarded-Host")
		req.Header.Del("X-Forwarded-Proto")
		req.Header.Del("X-Forwarded-Prefix")
		req.Header.Set("X-Forwarded-Host", r.Host)
		req.Header.Set("X-Forwarded-Proto", forwardedProto(r))
		if component == "admin" && h.internalServiceToken != "" {
			req.Header.Set("X-IronAgent-Internal-Token", h.internalServiceToken)
		}
		if forwardedPrefix != "" {
			req.Header.Set("X-Forwarded-Prefix", forwardedPrefix)
		}
	}
	proxy.ErrorHandler = func(w http.ResponseWriter, _ *http.Request, err error) {
		if h.logger != nil {
			h.logger.Warn(component+" proxy failed", "error", err)
		}
		writeProxyError(w, http.StatusBadGateway, component+"_proxy_error", component+" proxy failed")
	}
	proxy.ServeHTTP(w, r)
}

func (h *handler) healthz(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "alive",
		"components": map[string]string{
			"gateway": "up",
		},
		"timestamp": time.Now().UTC(),
	})
}

func (h *handler) readyz(w http.ResponseWriter, r *http.Request) {
	report := h.healthReport(r.Context())
	if !report.Ready {
		writeJSON(w, http.StatusServiceUnavailable, report)
		return
	}
	writeJSON(w, http.StatusOK, report)
}

func (h *handler) health(w http.ResponseWriter, r *http.Request) {
	report := newPublicHealthResponse(h.healthReport(r.Context()))
	h.writePublicHealth(w, report)
}

func (h *handler) userHealth(w http.ResponseWriter, r *http.Request) {
	userID := strings.TrimSpace(r.URL.Query().Get("user_id"))
	if userID == "" {
		err := &core.ValidationError{Message: "缺少用户标识，请传入 user_id"}
		h.logChatAPIWarn("健康检查请求校验失败", "error", err)
		writeError(w, err)
		return
	}
	runtimeView, err := h.service.GetRuntime(r.Context(), core.RuntimeRequest{UserID: userID})
	if err != nil {
		h.logChatAPIWarn("健康检查用户助手解析失败", "user_id", userID, "error", err)
		writeError(w, err)
		return
	}
	assistantStatus := publicUserAssistantStatus(runtimeView.Status)
	report := h.healthReport(r.Context())
	if !runtimeStatusAvailable(assistantStatus) {
		report.SetComponent("agent_runner", "down", publicUserAssistantHealthMessage(runtimeView.AssistantID, assistantStatus))
	} else {
		report.SetComponent("agent_runner", "up", publicUserAssistantHealthMessage(runtimeView.AssistantID, assistantStatus))
	}
	response := newPublicHealthResponse(report.Finalize())
	response.Assistant = &publicHealthAssistantResponse{
		AssistantID: runtimeView.AssistantID,
		Status:      assistantStatus,
	}
	h.writePublicHealth(w, response, "user_id", userID, "assistant_id", runtimeView.AssistantID, "assistant_status", assistantStatus)
}

func (h *handler) verifyUser(w http.ResponseWriter, r *http.Request) {
	userID := strings.TrimSpace(r.URL.Query().Get("user_id"))
	exists := false
	if userID != "" {
		var err error
		exists, err = h.service.UserExists(r.Context(), userID)
		if err != nil {
			h.logChatAPIWarn("用户存在性校验失败", "user_id", userID, "error", err)
			exists = false
		}
	}
	writeJSON(w, http.StatusOK, exists)
}

func publicUserAssistantStatus(status string) string {
	status = strings.TrimSpace(status)
	if status == "" {
		return "unknown"
	}
	return status
}

func publicUserAssistantHealthMessage(assistantID string, status string) string {
	assistantID = strings.TrimSpace(assistantID)
	status = publicUserAssistantStatus(status)
	if runtimeStatusAvailable(status) {
		return "用户绑定助手状态正常: assistant_id=" + assistantID + " status=" + status
	}
	return "用户绑定助手状态异常: assistant_id=" + assistantID + " status=" + status
}

func (h *handler) writePublicHealth(w http.ResponseWriter, report publicHealthResponse, logArgs ...any) {
	statusCode := http.StatusOK
	if report.Status == "unhealthy" {
		statusCode = http.StatusServiceUnavailable
		args := append([]any{
			"status", report.Status,
			"components", report.Components,
			"message", report.Message,
		}, logArgs...)
		h.logChatAPIWarn("健康检查异常", args...)
	} else {
		args := append([]any{
			"status", report.Status,
			"components", report.Components,
		}, logArgs...)
		h.logChatAPIInfo("健康检查成功", args...)
	}
	writeJSON(w, statusCode, report)
}

type publicHealthResponse struct {
	Status     string                         `json:"status"`
	Components map[string]string              `json:"components"`
	Message    string                         `json:"message,omitempty"`
	Timestamp  time.Time                      `json:"timestamp"`
	Assistant  *publicHealthAssistantResponse `json:"assistant,omitempty"`
}

type publicHealthAssistantResponse struct {
	AssistantID string `json:"assistant_id"`
	Status      string `json:"status"`
}

func newPublicHealthResponse(report core.HealthReport) publicHealthResponse {
	components := map[string]string{
		"gateway":             publicHealthComponentStatus(report.Components["gateway"]),
		"agent_runner":        publicHealthComponentStatus(report.Components["agent_runner"]),
		"container_scheduler": publicHealthComponentStatus(report.Components["container_scheduler"]),
		"file_service":        publicHealthComponentStatus(report.Components["file_service"]),
	}
	status := "healthy"
	for _, componentStatus := range components {
		switch componentStatus {
		case "down":
			status = "unhealthy"
			break
		case "degraded":
			if status == "healthy" {
				status = "degraded"
			}
		}
	}
	response := publicHealthResponse{
		Status:     status,
		Components: components,
		Timestamp:  report.Timestamp,
	}
	if status != "healthy" {
		response.Message = firstPublicHealthErrorMessage(report)
	}
	return response
}

func publicHealthComponentStatus(status string) string {
	switch strings.ToLower(strings.TrimSpace(status)) {
	case "down":
		return "down"
	case "degraded":
		return "degraded"
	default:
		return "up"
	}
}

func firstPublicHealthErrorMessage(report core.HealthReport) string {
	for _, name := range []string{"gateway", "agent_runner", "container_scheduler", "file_service"} {
		if publicHealthComponentStatus(report.Components[name]) == "up" {
			continue
		}
		detail, ok := report.Details[name]
		if ok && strings.TrimSpace(detail.Message) != "" {
			return detail.Message
		}
	}
	return "服务不可用"
}

func (h *handler) healthReport(ctx context.Context) core.HealthReport {
	report := h.service.Health(ctx)
	h.addCallbackHealth(ctx, &report)
	h.addRuntimeHealth(ctx, &report)
	return report.Finalize()
}

func (h *handler) addCallbackHealth(ctx context.Context, report *core.HealthReport) {
	if !h.callbackRequired {
		report.SetComponent("callback", "disabled", h.callbackHealthConfigMessage("callback is not required"))
		return
	}
	if h.webhooks == nil {
		report.SetComponent("callback", "down", h.callbackHealthConfigMessage("callback dispatcher is not configured"))
		return
	}
	if strings.TrimSpace(h.defaultCBURL) == "" {
		report.SetComponent("callback", "down", h.callbackHealthConfigMessage("default callback url is not configured"))
		return
	}
	if err := h.validateCallbackURL(ctx, h.defaultCBURL); err != nil {
		report.SetComponent("callback", "down", h.callbackHealthConfigMessage(err.Error()))
		return
	}
	report.SetComponent("callback", "up", h.callbackHealthConfigMessage("callback is configured"))
}

func (h *handler) callbackHealthConfigMessage(reason string) string {
	dispatcher := "missing"
	if h.webhooks != nil {
		dispatcher = "configured"
	}
	return strings.Join([]string{
		strings.TrimSpace(reason),
		"required=" + strconv.FormatBool(h.callbackRequired),
		"default_url=" + callbackURLHealthLabel(h.defaultCBURL),
		"dispatcher=" + dispatcher,
		"allowed_hosts=" + strconv.Itoa(len(h.callbackHosts)),
		"allow_private=" + strconv.FormatBool(h.allowPrivateCB),
	}, "; ")
}

func callbackURLHealthLabel(raw string) string {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return "empty"
	}
	parsed, err := url.Parse(trimmed)
	if err != nil || strings.TrimSpace(parsed.Host) == "" {
		return "configured"
	}
	return "configured(" + strings.ToLower(parsed.Host) + ")"
}

func (h *handler) addRuntimeHealth(ctx context.Context, report *core.HealthReport) {
	if h.runtimes == nil {
		report.SetComponent("container_scheduler", "unknown", "runtime registry not configured")
		return
	}
	records, err := h.runtimes.SnapshotWithError(ctx)
	if err != nil {
		h.logger.Warn("runtime registry snapshot failed", "error", err)
		report.SetComponent("container_scheduler", "down", "runtime registry snapshot failed")
		return
	}
	ready := 0
	draining := 0
	unhealthy := 0
	for _, record := range records {
		switch {
		case record.DrainMode:
			draining++
		case runtimeStatusAvailable(record.Status):
			ready++
		default:
			unhealthy++
		}
	}
	status := "up"
	if len(records) == 0 {
		status = "down"
	} else if ready == 0 {
		status = "down"
	} else if draining > 0 || unhealthy > 0 {
		status = "degraded"
	}
	report.SetComponent(
		"container_scheduler",
		status,
		"registered="+strconv.Itoa(len(records))+
			" ready="+strconv.Itoa(ready)+
			" draining="+strconv.Itoa(draining)+
			" unhealthy="+strconv.Itoa(unhealthy),
	)
}

func runtimeStatusAvailable(status string) bool {
	switch strings.ToLower(strings.TrimSpace(status)) {
	case "ready", "running", "healthy":
		return true
	default:
		return false
	}
}

type openChatRequest struct {
	RequestID          string   `json:"request_id"`
	UserID             string   `json:"user_id"`
	ConversationID     string   `json:"conversation_id"`
	Message            string   `json:"message"`
	Files              []string `json:"files,omitempty"`
	RequiresResultFile bool     `json:"requires_result_file,omitempty"`
	TimeoutSeconds     int      `json:"timeout_seconds,omitempty"`
}

var openChatRequestFields = map[string]struct{}{
	"request_id":           {},
	"user_id":              {},
	"conversation_id":      {},
	"message":              {},
	"files":                {},
	"requires_result_file": {},
	"timeout_seconds":      {},
}

func (request openChatRequest) toCore(defaultCallbackURL string) core.ChatRequest {
	userID := strings.TrimSpace(request.UserID)
	return core.ChatRequest{
		UserID:             userID,
		RequestID:          strings.TrimSpace(request.RequestID),
		ConversationID:     strings.TrimSpace(request.ConversationID),
		Message:            request.Message,
		Files:              request.Files,
		CallbackURL:        strings.TrimSpace(defaultCallbackURL),
		RequiresResultFile: request.RequiresResultFile,
		CreatedBy:          userID,
		TimeoutSeconds:     request.TimeoutSeconds,
	}
}

func (h *handler) createChatTask(w http.ResponseWriter, r *http.Request) {
	startedAt := time.Now()
	createResult := "failed"
	createStatus := "unknown"
	createStage := "unknown"
	createFailed := true
	publicRequest := isPublicRequest(r)
	defer func() {
		metrics.RecordTaskCreate(createResult, createStatus, createStage, time.Since(startedAt), createFailed)
	}()

	request, err := h.decodeChatRequest(r)
	if err != nil {
		h.logChatAPIWarn("提交任务请求校验失败", "error", err)
		writeError(w, err)
		return
	}
	if checker, ok := h.service.(chatAdmissionChecker); ok {
		if err := checker.CheckChatAdmission(r.Context(), request); err != nil {
			h.logChatAPIWarn(
				"提交任务admission拒绝",
				"request_id", request.RequestID,
				"user_id", request.UserID,
				"conversation_id", request.ConversationID,
				"error", err,
			)
			writeError(w, err)
			return
		}
		r = r.WithContext(core.WithChatAdmissionChecked(r.Context()))
	}
	if err := h.validateChatCallbackURL(r.Context(), publicRequest, request.CallbackURL); err != nil {
		h.logChatAPIWarn(
			"提交任务callback地址校验失败",
			"request_id", request.RequestID,
			"user_id", request.UserID,
			"conversation_id", request.ConversationID,
			"error", err,
		)
		writeError(w, err)
		return
	}
	if publicRequest {
		workspaceFiles, err := h.validatePublicChatFiles(r.Context(), request.UserID, request.Files)
		if err != nil {
			h.logChatAPIWarn(
				"提交任务文件引用校验失败",
				"request_id", request.RequestID,
				"user_id", request.UserID,
				"conversation_id", request.ConversationID,
				"error", err,
			)
			writeError(w, err)
			return
		}
		if len(workspaceFiles) > 0 {
			request.WorkspaceFiles = workspaceFiles
			if request.WorkspaceFile == nil {
				first := workspaceFiles[0]
				request.WorkspaceFile = &first
			}
		}
	}
	h.logChatAPIInfo(
		"提交任务开始",
		"request_id", request.RequestID,
		"upstream_request_id", upstreamRequestID(r.Context()),
		"user_id", request.UserID,
		"conversation_id", request.ConversationID,
		"file_count", len(request.Files),
	)

	task, err := h.service.CreateChatTask(r.Context(), request)
	if err != nil {
		h.logChatAPIWarn(
			"提交任务失败",
			"request_id", request.RequestID,
			"user_id", request.UserID,
			"conversation_id", request.ConversationID,
			"error", err,
		)
		writeError(w, err)
		return
	}

	actualResponse := core.NewChatResponse(task)
	response := actualResponse
	if publicRequest {
		response.Status = "processing"
		response.Stage = "queued"
	}
	createResult = "accepted"
	if task.IdempotencyReplayed {
		createResult = "replayed"
	}
	createStatus = actualResponse.Status
	createStage = actualResponse.Stage
	createFailed = false
	if err := h.ensureChatCreatedEvents(r, request, task, actualResponse); err != nil {
		createResult = "callback_enqueue_failed"
		createFailed = true
		h.logChatAPIWarn(
			"提交任务callback入队失败",
			"request_id", request.RequestID,
			"upstream_request_id", upstreamRequestID(r.Context()),
			"user_id", request.UserID,
			"conversation_id", request.ConversationID,
			"task_id", response.TaskID,
			"error", err,
		)
		if !task.IdempotencyReplayed {
			if cancelErr := h.cancelTaskDetached(
				task.ID,
				"callback accepted enqueue failed",
			); cancelErr != nil && h.logger != nil {
				h.logger.Warn(
					"task cancellation after callback enqueue failure failed",
					"task_id", task.ID,
					"error", cancelErr,
				)
			}
		}
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{
			"error": map[string]any{
				"code":    "callback_enqueue_failed",
				"message": "callback 暂时不可用，请稍后重试",
			},
		})
		return
	}
	if err := h.activateQueuedTaskDetached(task.ID); err != nil {
		createResult = "task_activation_failed"
		createFailed = true
		h.logChatAPIWarn(
			"提交任务队列激活失败",
			"request_id", request.RequestID,
			"user_id", request.UserID,
			"conversation_id", request.ConversationID,
			"task_id", response.TaskID,
			"error", err,
		)
		if !task.IdempotencyReplayed {
			if cancelErr := h.cancelTaskDetached(task.ID, "queued task activation failed"); cancelErr != nil && h.logger != nil {
				h.logger.Warn(
					"task cancellation after queue activation failure failed",
					"task_id", task.ID,
					"error", cancelErr,
				)
			}
			cancelledTask := task
			cancelledTask.Status = "cancelled"
			cancelledTask.FailedReasonCode = "TASK_ACTIVATION_FAILED"
			cancelledTask.FailedReasonText = "queued task activation failed"
			h.dispatchQueuedTaskUpdate(context.WithoutCancel(r.Context()), cancelledTask)
		}
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{
			"error": map[string]any{
				"code":    "task_activation_failed",
				"message": "任务队列暂时不可用，请稍后重试",
			},
		})
		return
	}
	h.logChatAPIInfo(
		"提交任务已接收",
		"request_id", request.RequestID,
		"upstream_request_id", upstreamRequestID(r.Context()),
		"user_id", request.UserID,
		"conversation_id", request.ConversationID,
		"task_id", response.TaskID,
		"status", response.Status,
		"stage", response.Stage,
	)
	writeJSON(w, http.StatusAccepted, response)
}

func (h *handler) activateQueuedTaskDetached(taskID string) error {
	activator, ok := h.service.(queuedTaskActivator)
	if !ok {
		return nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), detachedTaskMutationTimeout)
	defer cancel()
	return activator.ActivateQueuedTask(ctx, taskID)
}

func (h *handler) cancelTaskDetached(taskID string, reason string) error {
	ctx, cancel := context.WithTimeout(context.Background(), detachedTaskMutationTimeout)
	defer cancel()
	_, err := h.service.CancelTask(ctx, taskID, core.CancelTaskRequest{
		ActorID: "go-gateway",
		Reason:  strings.TrimSpace(reason),
	})
	return err
}

func (h *handler) decodeChatRequest(r *http.Request) (core.ChatRequest, error) {
	if !isPublicRequest(r) {
		var request core.ChatRequest
		if err := decodeJSON(r, &request); err != nil {
			return core.ChatRequest{}, err
		}
		if strings.TrimSpace(request.ClientID) == "" {
			request.ClientID = r.Header.Get("X-Client-ID")
		}
		if strings.TrimSpace(request.CallbackURL) == "" {
			request.CallbackURL = h.defaultCBURL
		}
		return request, nil
	}
	publicRequest, err := decodePublicChatRequest(r)
	if err != nil {
		return core.ChatRequest{}, err
	}
	return publicRequest.toCore(h.defaultCBURL), nil
}

func isPublicRequest(r *http.Request) bool {
	info, ok := auth.RequestInfoFromContext(r.Context())
	if !ok {
		return true
	}
	if !info.APIKeyPresent {
		return true
	}
	return !auth.RequestHasScope(r.Context(), "*") &&
		!auth.RequestHasScope(r.Context(), "admin") &&
		!auth.RequestHasScope(r.Context(), "internal")
}

func (h *handler) auditPublicBusinessAPIFailure(action string, next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		startedAt := time.Now()
		capturing := &publicFailureAuditResponseWriter{
			ResponseWriter: w,
			statusCode:     http.StatusOK,
			bodyLimit:      8192,
		}
		next(capturing, r)
		if capturing.statusCode < http.StatusBadRequest {
			return
		}
		h.appendPublicBusinessAPIFailureAudit(
			r,
			action,
			capturing.statusCode,
			capturing.body.Bytes(),
			capturing.truncated,
			time.Since(startedAt),
		)
	}
}

func (h *handler) appendPublicBusinessAPIFailureAudit(
	r *http.Request,
	action string,
	statusCode int,
	responseBody []byte,
	responseBodyTruncated bool,
	duration time.Duration,
) {
	if h.securityBaseURL == "" {
		if h.logger != nil {
			h.logger.Warn("public API failure audit skipped: security service is not configured", "action", action, "status_code", statusCode)
		}
		return
	}
	errorCode, errorMessage := publicFailureErrorFields(statusCode, responseBody)
	requestArgs := publicFailureAuditRequestArgs(r, action, statusCode, errorCode)
	session := map[string]any{
		"channel": "public_api",
	}
	if userID := publicFailureUserID(r); userID != "" {
		session["user_id"] = userID
	}
	if conversationID := strings.TrimSpace(r.URL.Query().Get("conversation_id")); conversationID != "" {
		session["conversation_id"] = conversationID
	}

	metadata := map[string]any{
		"origin":                  "go_gateway_public_api",
		"action":                  action,
		"error_code":              errorCode,
		"error_message":           errorMessage,
		"response_body_truncated": responseBodyTruncated,
	}
	if clientRequestID := publicFailureClientRequestID(r); clientRequestID != "" {
		metadata["client_request_id"] = clientRequestID
	}
	if source := publicFailureRequestSource(r); len(source) > 0 {
		metadata["request_source"] = source
	}

	payload := map[string]any{
		"envelope": map[string]any{
			"protocol_version": "2026-04-03",
			"request_id":       newPublicFailureAuditRequestID(),
			"source":           "go-gateway",
			"hook_name":        "post_execute",
			"occurred_at":      time.Now().UTC().Format(time.RFC3339Nano),
		},
		"assistant": map[string]any{
			"capability_profile": "public-api",
		},
		"session": session,
		"tool": map[string]any{
			"name":   action,
			"args":   requestArgs,
			"source": "client",
		},
		"result": map[string]any{
			"success":     false,
			"duration_ms": int(duration / time.Millisecond),
			"error":       errorMessage,
		},
		"decision": "deny",
		"metadata": metadata,
	}
	body, err := json.Marshal(payload)
	if err != nil {
		if h.logger != nil {
			h.logger.Warn("public API failure audit payload marshal failed", "action", action, "error", err)
		}
		return
	}

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	request, err := http.NewRequestWithContext(
		ctx,
		http.MethodPost,
		strings.TrimRight(h.securityBaseURL, "/")+"/api/v1/audit/log",
		bytes.NewReader(body),
	)
	if err != nil {
		if h.logger != nil {
			h.logger.Warn("public API failure audit request build failed", "action", action, "error", err)
		}
		return
	}
	request.Header.Set("Content-Type", "application/json")
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		if h.logger != nil {
			h.logger.Warn("public API failure audit append failed", "action", action, "error", err)
		}
		return
	}
	defer response.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 1024))
	if response.StatusCode >= http.StatusBadRequest && h.logger != nil {
		h.logger.Warn("public API failure audit append rejected", "action", action, "status_code", response.StatusCode)
	}
}

func publicFailureErrorFields(statusCode int, responseBody []byte) (string, string) {
	code := "http_" + strconv.Itoa(statusCode)
	message := strings.TrimSpace(http.StatusText(statusCode))
	if message == "" {
		message = "HTTP " + strconv.Itoa(statusCode)
	}
	var payload map[string]any
	if err := json.Unmarshal(responseBody, &payload); err != nil {
		return code, message
	}
	if rawError, ok := payload["error"].(map[string]any); ok {
		if value := strings.TrimSpace(stringValue(rawError["code"])); value != "" {
			code = value
		}
		if value := strings.TrimSpace(stringValue(rawError["message"])); value != "" {
			message = value
		}
		return code, message
	}
	if strings.EqualFold(strings.TrimSpace(stringValue(payload["status"])), "unhealthy") {
		code = "health_unhealthy"
		if value := strings.TrimSpace(stringValue(payload["message"])); value != "" {
			message = value
		}
	}
	return code, message
}

func publicFailureAuditRequestArgs(r *http.Request, action string, statusCode int, errorCode string) map[string]any {
	args := map[string]any{
		"action":      action,
		"method":      r.Method,
		"path":        r.URL.Path,
		"status_code": statusCode,
		"error_code":  errorCode,
	}
	for _, key := range []string{"file_id", "task_id", "approval_id"} {
		if value := strings.TrimSpace(r.PathValue(key)); value != "" {
			args[key] = value
		}
	}
	if userID := publicFailureUserID(r); userID != "" {
		args["user_id"] = userID
	}
	if clientRequestID := publicFailureClientRequestID(r); clientRequestID != "" {
		args["client_request_id"] = clientRequestID
	}
	return args
}

func publicFailureUserID(r *http.Request) string {
	if value := strings.TrimSpace(r.URL.Query().Get("user_id")); value != "" {
		return value
	}
	if r.MultipartForm != nil {
		if value := strings.TrimSpace(formValue(r.MultipartForm, "user_id")); value != "" {
			return value
		}
	}
	return ""
}

func publicFailureClientRequestID(r *http.Request) string {
	for _, value := range []string{
		r.Header.Get("X-Request-ID"),
		r.Header.Get("X-Correlation-ID"),
		r.URL.Query().Get("request_id"),
	} {
		if trimmed := strings.TrimSpace(value); trimmed != "" {
			return trimmed
		}
	}
	return ""
}

type upstreamRequestIDContextKey struct{}

// withUpstreamRequestID adopts the caller's X-Request-ID / X-Correlation-ID
// (or ?request_id) into the request context. It is read back only on the
// SYNCHRONOUS request path (createChatTask's task-entry and callback-enqueue-
// failure logs). The async queue worker runs on a fresh context that does NOT
// carry this value, so upstream_request_id does not span async execution /
// runtime / WS logs — use the gateway task_id / southbound_task_id spine for
// end-to-end async correlation. It does NOT mint an id — absent header means no
// upstream correlation id, which is fine (intranet).
func withUpstreamRequestID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if id := publicFailureClientRequestID(r); id != "" {
			r = r.WithContext(context.WithValue(r.Context(), upstreamRequestIDContextKey{}, id))
		}
		next.ServeHTTP(w, r)
	})
}

// upstreamRequestID reads the upstream correlation id adopted at ingress, or ""
// if the caller supplied none.
func upstreamRequestID(ctx context.Context) string {
	if ctx == nil {
		return ""
	}
	if id, ok := ctx.Value(upstreamRequestIDContextKey{}).(string); ok {
		return id
	}
	return ""
}

func publicFailureRequestSource(r *http.Request) map[string]any {
	source := map[string]any{}
	remoteAddr := strings.TrimSpace(r.RemoteAddr)
	if host, _, err := net.SplitHostPort(remoteAddr); err == nil {
		remoteAddr = host
	}
	if remoteAddr != "" {
		source["remote_addr"] = remoteAddr
	}
	if userAgent := strings.TrimSpace(r.UserAgent()); userAgent != "" {
		source["user_agent"] = userAgent
	}
	return source
}

func newPublicFailureAuditRequestID() string {
	randomBytes := make([]byte, 16)
	if _, err := cryptorand.Read(randomBytes); err == nil {
		return "public-api-failure-" + hex.EncodeToString(randomBytes)
	}
	return "public-api-failure-" + strconv.FormatInt(time.Now().UTC().UnixNano(), 10)
}

func decodePublicChatRequest(r *http.Request) (openChatRequest, error) {
	defer r.Body.Close()
	body, err := io.ReadAll(r.Body)
	if err != nil {
		return openChatRequest{}, &core.ValidationError{Message: "读取请求体失败: " + err.Error()}
	}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(body, &raw); err != nil {
		return openChatRequest{}, &core.ValidationError{Message: "请求体不是合法 JSON: " + err.Error()}
	}
	for field := range raw {
		if _, ok := openChatRequestFields[field]; !ok {
			return openChatRequest{}, &core.ValidationError{Message: "不支持的请求字段: " + field}
		}
	}
	var request openChatRequest
	if err := json.Unmarshal(body, &request); err != nil {
		return openChatRequest{}, &core.ValidationError{Message: "请求字段类型不合法: " + err.Error()}
	}
	if strings.TrimSpace(request.RequestID) == "" {
		return openChatRequest{}, &core.ValidationError{Message: "缺少请求唯一标识，请传入 request_id"}
	}
	if strings.TrimSpace(request.UserID) == "" {
		return openChatRequest{}, &core.ValidationError{Message: "缺少用户标识，请传入 user_id"}
	}
	if strings.TrimSpace(request.ConversationID) == "" {
		return openChatRequest{}, &core.ValidationError{Message: "缺少会话标识，请传入 conversation_id"}
	}
	if strings.TrimSpace(request.Message) == "" {
		return openChatRequest{}, &core.ValidationError{Message: "缺少用户消息内容，请传入 message"}
	}
	return request, nil
}

func (h *handler) validatePublicChatFiles(ctx context.Context, userID string, fileIDs []string) ([]core.WorkspaceFileRef, error) {
	workspaceFiles := make([]core.WorkspaceFileRef, 0, len(fileIDs))
	for _, rawFileID := range fileIDs {
		fileID := strings.TrimSpace(rawFileID)
		if fileID == "" {
			return nil, &unprocessableEntityError{message: "引用的文件不存在: file_id 不能为空"}
		}
		metadata, err := h.service.GetFile(ctx, fileID)
		if err != nil {
			if isNotFoundError(err) {
				return nil, &unprocessableEntityError{message: "引用的文件不存在: " + fileID}
			}
			return nil, err
		}
		if fileMetadataMissing(metadata) {
			return nil, &unprocessableEntityError{message: "引用的文件不存在: " + fileID}
		}
		if err := validateFileUser(metadata, userID); err != nil {
			return nil, err
		}
		normalized := normalizeFileMetadata(metadata, core.FileUploadRequest{})
		if strings.TrimSpace(normalized.FileID) != fileID {
			return nil, &unprocessableEntityError{message: "引用的文件不存在: " + fileID}
		}
		workspaceFiles = append(workspaceFiles, workspaceFileRefFromMetadata(normalized))
	}
	return workspaceFiles, nil
}

func workspaceFileRefFromMetadata(metadata fileMetadataResponse) core.WorkspaceFileRef {
	return core.WorkspaceFileRef{
		FileID:       metadata.FileID,
		Filename:     metadata.Filename,
		ObjectType:   "document",
		ContentType:  metadata.MimeType,
		SizeBytes:    metadata.Size,
		Bucket:       metadata.Bucket,
		RelativePath: metadata.RelativePath,
	}
}

func isNotFoundError(err error) bool {
	var httpErr *southbound.HTTPError
	return errors.As(err, &httpErr) && httpErr.StatusCode == http.StatusNotFound
}

func fileMetadataMissing(raw southbound.RawJSON) bool {
	if raw == nil {
		return true
	}
	return strings.TrimSpace(stringValue(firstNonEmpty(raw["file_id"], raw["id"]))) == ""
}

func (h *handler) getRuntime(w http.ResponseWriter, r *http.Request) {
	runtimeView, err := h.service.GetRuntime(r.Context(), core.RuntimeRequest{
		AssistantID: r.URL.Query().Get("assistant_id"),
		UserID:      r.URL.Query().Get("user_id"),
	})
	if err != nil {
		writeError(w, err)
		return
	}
	h.recordRuntime(runtimeView)
	writeJSON(w, http.StatusOK, map[string]any{"runtime": runtimeView})
}

func (h *handler) startRuntime(w http.ResponseWriter, r *http.Request) {
	var request core.RuntimeRequest
	if err := decodeJSON(r, &request); err != nil {
		writeError(w, err)
		return
	}

	runtimeView, err := h.service.StartRuntime(r.Context(), request)
	if err != nil {
		writeError(w, err)
		return
	}
	h.recordRuntime(runtimeView)
	writeJSON(w, http.StatusOK, map[string]any{"runtime": runtimeView})
}

func (h *handler) stopRuntime(w http.ResponseWriter, r *http.Request) {
	var request core.RuntimeRequest
	if err := decodeJSON(r, &request); err != nil {
		writeError(w, err)
		return
	}

	runtimeView, err := h.service.StopRuntime(r.Context(), request)
	if err != nil {
		writeError(w, err)
		return
	}
	h.recordRuntime(runtimeView)
	writeJSON(w, http.StatusOK, map[string]any{"runtime": runtimeView})
}

func (h *handler) listRuntimeRegistry(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"runtimes": h.runtimes.Snapshot()})
}

func (h *handler) getRuntimeSchedulerDecision(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"decision": h.runtimes.LastDecision()})
}

func (h *handler) getRuntimeRegistry(w http.ResponseWriter, r *http.Request) {
	assistantID := r.PathValue("assistant_id")
	if !validAssistantID(assistantID) {
		writeError(w, &core.ValidationError{Message: "invalid assistant_id"})
		return
	}
	record, ok := h.runtimes.Get(assistantID)
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]any{"error": map[string]any{
			"code":    "not_found",
			"message": "runtime not found",
		}})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"runtime": record})
}

func (h *handler) upsertRuntimeRegistry(w http.ResponseWriter, r *http.Request) {
	assistantID := r.PathValue("assistant_id")
	if !validAssistantID(assistantID) {
		writeError(w, &core.ValidationError{Message: "invalid assistant_id"})
		return
	}
	var record runtimepool.RuntimeRecord
	if err := decodeJSON(r, &record); err != nil {
		writeError(w, err)
		return
	}
	record.AssistantID = assistantID
	updated, err := h.runtimes.Upsert(record)
	if err != nil {
		writeError(w, &core.ValidationError{Message: err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"runtime": updated})
}

func (h *handler) heartbeatRuntimeRegistry(w http.ResponseWriter, r *http.Request) {
	assistantID := r.PathValue("assistant_id")
	if !validAssistantID(assistantID) {
		writeError(w, &core.ValidationError{Message: "invalid assistant_id"})
		return
	}
	var record runtimepool.RuntimeRecord
	if err := decodeJSON(r, &record); err != nil {
		writeError(w, err)
		return
	}
	record.AssistantID = assistantID
	updated, err := h.runtimes.Heartbeat(record)
	if err != nil {
		writeError(w, &core.ValidationError{Message: err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"runtime": updated})
}

func (h *handler) assistantProxyAuth(
	authMiddleware func(http.Handler) http.Handler,
	next http.Handler,
) http.Handler {
	authenticated := authMiddleware(next)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		assistantID, proxyKind, _, ok := parseAssistantProxyPath(r.URL.Path)
		if ok && (proxyKind == "api" || proxyKind == "ui" || proxyKind == "control" || proxyKind == "ws") {
			allowed, handled := h.authorizeAssistantBrowserRoute(w, r, assistantID, proxyKind)
			if handled {
				if allowed {
					next.ServeHTTP(w, r)
				}
				return
			}
		}
		authenticated.ServeHTTP(w, r)
	})
}

func (h *handler) authorizeAssistantBrowserRoute(w http.ResponseWriter, r *http.Request, assistantID string, proxyKind string) (bool, bool) {
	if !validAssistantID(assistantID) {
		writeProxyError(w, http.StatusBadRequest, "bad_request", "invalid assistant_id")
		return false, true
	}
	if session, ok := h.proxySession.resolve(r, assistantID); ok {
		if (proxyKind == "api" || proxyKind == "ws") && session.gatewayToken == "" {
			return false, false
		}
		return true, true
	}

	ticket := strings.TrimSpace(r.URL.Query().Get("launchTicket"))
	if ticket == "" {
		ticket = strings.TrimSpace(r.URL.Query().Get("ticket"))
	}
	if ticket == "" {
		return false, false
	}
	if len(h.proxySession.secret) == 0 {
		writeProxyError(w, http.StatusUnauthorized, "proxy_session_unavailable", "proxy session secret is not configured")
		return false, true
	}
	session, err := h.service.ResolveControlUILaunchTicket(r.Context(), ticket, requestOrigin(r))
	if err != nil {
		writeProxyError(w, launchTicketStatus(err), "launch_ticket_invalid", "launch ticket is invalid")
		return false, true
	}
	h.recordLaunchTicketRuntime(assistantID, session)
	if !h.launchTicketMatchesRuntime(assistantID, session) {
		writeProxyError(w, http.StatusForbidden, "launch_ticket_mismatch", "launch ticket does not match assistant runtime")
		return false, true
	}
	now := time.Now().UTC()
	if !session.ExpiresAt.IsZero() && !session.ExpiresAt.After(now) {
		writeProxyError(w, http.StatusUnauthorized, "launch_ticket_expired", "launch ticket is expired")
		return false, true
	}
	expiresAt := now.Add(h.proxySession.ttl)
	http.SetCookie(w, &http.Cookie{
		Name:     assistantProxySessionCookie,
		Value:    h.proxySession.sign(assistantID, session.SessionKey, expiresAt, session.GatewayToken),
		Path:     "/assistants/" + assistantID + "/",
		Expires:  expiresAt,
		MaxAge:   int(time.Until(expiresAt).Seconds()),
		HttpOnly: true,
		Secure:   h.secureProxySessionCookie(r),
		SameSite: http.SameSiteLaxMode,
	})
	http.Redirect(w, r, assistantLaunchRedirectTarget(r, assistantID, proxyKind, session, h.publicBaseURL), http.StatusFound)
	return false, true
}

func (h *handler) recordLaunchTicketRuntime(assistantID string, session core.ControlUILaunchSession) {
	if h.runtimes == nil || strings.TrimSpace(session.AssistantID) != assistantID {
		return
	}
	gatewayURL := strings.TrimSpace(session.GatewayURL)
	if gatewayURL == "" {
		return
	}
	proxyBaseURL := launchTicketRuntimeProxyBaseURL(assistantID, gatewayURL)
	record, ok := h.runtimes.Get(assistantID)
	if !ok {
		if proxyBaseURL == "" {
			return
		}
		record = runtimepool.RuntimeRecord{
			AssistantID:     assistantID,
			InternalBaseURL: proxyBaseURL,
			GatewayURL:      gatewayURL,
		}
	} else {
		if strings.TrimSpace(record.InternalBaseURL) == "" && proxyBaseURL != "" {
			record.InternalBaseURL = proxyBaseURL
		}
		record.GatewayURL = gatewayURL
	}
	if runtimeRecordAvailableForRegistry(record) {
		record.DrainMode = false
	}
	record.UpdatedAt = time.Now().UTC()
	_, _ = h.runtimes.Heartbeat(record)
}

func launchTicketRuntimeProxyBaseURL(assistantID string, gatewayURL string) string {
	target, err := url.Parse(strings.TrimSpace(gatewayURL))
	if err != nil || target.Host == "" || target.User != nil {
		return ""
	}
	switch target.Scheme {
	case "http", "https":
	case "ws":
		target.Scheme = "http"
	case "wss":
		target.Scheme = "https"
	default:
		return ""
	}
	if parsedAssistantID, _, _, ok := parseAssistantProxyPath(target.EscapedPath()); ok && parsedAssistantID == assistantID {
		return ""
	}
	target.Path = ""
	target.RawPath = ""
	target.RawQuery = ""
	target.Fragment = ""
	return target.String()
}

func (h *handler) launchTicketMatchesRuntime(assistantID string, session core.ControlUILaunchSession) bool {
	if strings.TrimSpace(session.AssistantID) == assistantID {
		return true
	}
	record, ok := h.runtimes.Get(assistantID)
	if !ok {
		return false
	}
	sessionGatewayURL := strings.TrimRight(strings.TrimSpace(session.GatewayURL), "/")
	return sessionGatewayURL != "" &&
		(sessionGatewayURL == strings.TrimRight(strings.TrimSpace(record.GatewayURL), "/") ||
			sessionGatewayURL == strings.TrimRight(strings.TrimSpace(record.InternalBaseURL), "/"))
}

func (h *handler) proxyAssistantRuntime(w http.ResponseWriter, r *http.Request) {
	startedAt := time.Now()
	proxyKindMetric := "unknown"
	proxyInFlightKind := ""
	proxyStatusCode := http.StatusInternalServerError
	proxyFailed := true
	proxyAssistantID := ""
	proxyCircuitSample := false
	defer func() {
		if proxyInFlightKind != "" {
			metrics.AddRuntimeProxyInFlight(proxyInFlightKind, -1)
		}
		metrics.RecordRuntimeProxy(proxyKindMetric, proxyStatusCode, time.Since(startedAt), proxyFailed)
		if proxyCircuitSample {
			if h.proxyCircuit.record(proxyAssistantID, proxyFailed) {
				metrics.RecordRuntimeProxyCircuitOpen(proxyKindMetric)
			}
		}
	}()

	assistantID, proxyKind, runtimePath, ok := parseAssistantProxyPath(r.URL.Path)
	if !ok {
		proxyStatusCode = http.StatusNotFound
		if h.logger != nil {
			h.logger.Warn("runtime_proxy_rejected", "assistant_id", assistantID, "proxy_kind", proxyKind, "reason_code", "not_found", "status_code", proxyStatusCode)
		}
		writeProxyError(w, http.StatusNotFound, "not_found", "assistant runtime route not found")
		return
	}
	proxyKindMetric = proxyKind
	if !validAssistantID(assistantID) {
		proxyStatusCode = http.StatusBadRequest
		if h.logger != nil {
			h.logger.Warn("runtime_proxy_rejected", "assistant_id", assistantID, "proxy_kind", proxyKind, "reason_code", "bad_request", "status_code", proxyStatusCode)
		}
		writeProxyError(w, http.StatusBadRequest, "bad_request", "invalid assistant_id")
		return
	}
	proxyAssistantID = assistantID
	if containsPathTraversal(runtimePath) {
		proxyStatusCode = http.StatusBadRequest
		if h.logger != nil {
			h.logger.Warn("runtime_proxy_rejected", "assistant_id", assistantID, "proxy_kind", proxyKind, "reason_code", "bad_request", "status_code", proxyStatusCode)
		}
		writeProxyError(w, http.StatusBadRequest, "bad_request", "invalid runtime API path")
		return
	}
	if proxyKind == "control" {
		proxyStatusCode, proxyFailed = h.proxyAssistantControl(w, r, assistantID, runtimePath)
		return
	}
	if proxyKind == "api" && isAssistantControlAPIPath(runtimePath) {
		proxyStatusCode, proxyFailed = h.proxyAssistantControl(w, r, assistantID, assistantControlAPIPath(runtimePath))
		return
	}
	if !h.proxyCircuit.allow(assistantID) {
		proxyStatusCode = http.StatusServiceUnavailable
		if h.logger != nil {
			h.logger.Warn("runtime_proxy_rejected", "assistant_id", assistantID, "proxy_kind", proxyKind, "reason_code", "runtime_proxy_circuit_open", "status_code", proxyStatusCode, "circuit_open", true)
		}
		writeProxyError(w, http.StatusServiceUnavailable, "runtime_proxy_circuit_open", "runtime proxy circuit is open")
		return
	}
	if !h.acquireProxySlot() {
		proxyStatusCode = http.StatusTooManyRequests
		if h.logger != nil {
			h.logger.Warn("runtime_proxy_rejected", "assistant_id", assistantID, "proxy_kind", proxyKind, "reason_code", "runtime_proxy_at_capacity", "status_code", proxyStatusCode, "proxy_in_flight", len(h.proxySlots), "proxy_capacity", cap(h.proxySlots))
		}
		writeProxyError(w, http.StatusTooManyRequests, "runtime_proxy_at_capacity", "runtime proxy is at capacity")
		return
	}
	defer h.releaseProxySlot()
	proxyInFlightKind = proxyKind
	metrics.AddRuntimeProxyInFlight(proxyInFlightKind, 1)

	record, ok := h.runtimes.Get(assistantID)
	if !ok {
		proxyStatusCode = http.StatusNotFound
		if h.logger != nil {
			h.logger.Warn("runtime_proxy_rejected", "assistant_id", assistantID, "proxy_kind", proxyKind, "reason_code", "runtime_not_found", "status_code", proxyStatusCode)
		}
		writeProxyError(w, http.StatusNotFound, "runtime_not_found", "runtime not registered")
		return
	}
	browserSession, browserSessionProxy := h.proxySession.resolve(r, assistantID)
	browserSessionProxy = (proxyKind == "api" || proxyKind == "ui" || proxyKind == "ws") && browserSessionProxy
	if !browserSessionProxy && (record.DrainMode || !runtimeStatusAvailable(record.Status)) {
		proxyStatusCode = http.StatusServiceUnavailable
		if h.logger != nil {
			h.logger.Warn("runtime_proxy_rejected", "assistant_id", assistantID, "proxy_kind", proxyKind, "reason_code", "runtime_unavailable", "status_code", proxyStatusCode, "runtime_id", record.RuntimeID, "runtime_status", record.Status, "drain_mode", record.DrainMode)
		}
		writeProxyError(w, http.StatusServiceUnavailable, "runtime_unavailable", "runtime is not available")
		return
	}
	if !browserSessionProxy && record.Capacity > 0 && record.InFlight >= record.Capacity {
		proxyStatusCode = http.StatusTooManyRequests
		if h.logger != nil {
			h.logger.Warn("runtime_proxy_rejected", "assistant_id", assistantID, "proxy_kind", proxyKind, "reason_code", "runtime_at_capacity", "status_code", proxyStatusCode, "runtime_id", record.RuntimeID, "runtime_in_flight", record.InFlight, "runtime_capacity", record.Capacity)
		}
		writeProxyError(w, http.StatusTooManyRequests, "runtime_at_capacity", "runtime is at capacity")
		return
	}
	target, err := runtimeProxyTarget(record)
	if err != nil {
		proxyStatusCode = http.StatusServiceUnavailable
		if h.logger != nil {
			h.logger.Warn("runtime_proxy_rejected", "assistant_id", assistantID, "proxy_kind", proxyKind, "reason_code", "runtime_proxy_unavailable", "status_code", proxyStatusCode, "runtime_id", record.RuntimeID, "error", err)
		}
		writeProxyError(w, http.StatusServiceUnavailable, "runtime_proxy_unavailable", err.Error())
		return
	}
	if h.runtimeProxyTargetIsSelf(target, r) {
		proxyStatusCode = http.StatusServiceUnavailable
		if h.logger != nil {
			h.logger.Warn("runtime_proxy_rejected", "assistant_id", assistantID, "proxy_kind", proxyKind, "reason_code", "runtime_proxy_self_target", "status_code", proxyStatusCode, "runtime_id", record.RuntimeID)
		}
		writeProxyError(w, http.StatusServiceUnavailable, "runtime_proxy_self_target", "runtime proxy target points to gateway itself")
		return
	}
	if browserSessionProxy && browserSession.gatewayToken != "" && websocket.IsWebSocketUpgrade(r) {
		proxyStatusCode, proxyFailed = h.proxyAssistantBrowserWebSocket(
			w,
			r,
			assistantID,
			runtimePath,
			target,
			browserSession.gatewayToken,
		)
		return
	}
	if !browserSessionProxy {
		if err := h.runtimes.Reserve(r.Context(), assistantID); err != nil {
			status := http.StatusServiceUnavailable
			code := "runtime_unavailable"
			if strings.Contains(err.Error(), "at capacity") {
				status = http.StatusTooManyRequests
				code = "runtime_at_capacity"
			}
			proxyStatusCode = status
			if h.logger != nil {
				h.logger.Warn("runtime_proxy_rejected", "assistant_id", assistantID, "proxy_kind", proxyKind, "reason_code", code, "status_code", status, "runtime_id", record.RuntimeID, "error", err)
			}
			writeProxyError(w, status, code, err.Error())
			return
		}
		defer h.runtimes.Release(r.Context(), assistantID)
	}
	proxyCircuitSample = true

	proxy := httputil.NewSingleHostReverseProxy(target)
	proxy.FlushInterval = -1
	originalDirector := proxy.Director
	proxy.Director = func(req *http.Request) {
		originalDirector(req)
		req.URL.Path = joinURLPath(target.Path, runtimePath)
		req.URL.RawPath = ""
		req.URL.RawQuery = r.URL.RawQuery
		req.Host = target.Host
		req.Header.Del("X-Forwarded-Host")
		req.Header.Del("X-Forwarded-Proto")
		req.Header.Del("X-Forwarded-Prefix")
		req.Header.Set("X-Forwarded-Host", r.Host)
		req.Header.Set("X-Forwarded-Proto", forwardedProto(r))
		req.Header.Set("X-Forwarded-Prefix", "/assistants/"+assistantID+"/"+proxyKind)
		req.Header.Set("X-IronAgent-Assistant-ID", assistantID)
		if browserSessionProxy && (proxyKind == "api" || proxyKind == "ws") && browserSession.gatewayToken != "" {
			req.Header.Set("Authorization", "Bearer "+browserSession.gatewayToken)
		}
	}
	proxy.ErrorHandler = func(w http.ResponseWriter, _ *http.Request, err error) {
		if h.logger != nil {
			h.logger.Warn("runtime proxy failed", "assistant_id", assistantID, "error", err)
		}
		proxyStatusCode = http.StatusBadGateway
		proxyFailed = true
		writeProxyError(w, http.StatusBadGateway, "runtime_proxy_error", "runtime proxy failed")
	}
	if proxyKind == "ui" {
		proxy.ModifyResponse = func(resp *http.Response) error {
			if resp.StatusCode != http.StatusOK || resp.Body == nil {
				return nil
			}
			if strings.TrimSpace(resp.Header.Get("Content-Encoding")) != "" {
				return nil
			}
			if !strings.Contains(strings.ToLower(resp.Header.Get("Content-Type")), "text/html") {
				return nil
			}
			body, err := io.ReadAll(resp.Body)
			if err != nil {
				return err
			}
			if err := resp.Body.Close(); err != nil {
				return err
			}
			scriptContent := assistantUIConfigScriptContent(assistantLaunchAPIBaseURL(r, h.publicBaseURL, assistantID))
			body = injectAssistantUIConfig(body, scriptContent)
			allowAssistantUIConfigScript(resp.Header, scriptContent)
			resp.Body = io.NopCloser(bytes.NewReader(body))
			resp.ContentLength = int64(len(body))
			resp.Header.Set("Content-Length", strconv.Itoa(len(body)))
			return nil
		}
	}
	capturing := &statusCaptureResponseWriter{ResponseWriter: w, statusCode: http.StatusOK}
	proxy.ServeHTTP(capturing, r)
	if proxyStatusCode != http.StatusBadGateway {
		proxyStatusCode = capturing.statusCode
		proxyFailed = proxyStatusCode >= 500
	}
}

func isAssistantControlAPIPath(path string) bool {
	normalized := assistantControlAPIPath(path)
	switch {
	case normalized == "/api/v1/control-ui/assistant-context":
		return true
	case normalized == "/api/v1/control-ui/launch-tickets/resolve":
		return true
	case normalized == "/api/v1/approvals":
		return true
	case isAssistantApprovalResolveControlPath(normalized):
		return true
	case normalized == "/api/v1/workspace-files":
		return true
	case normalized == "/api/v1/workspace-files/upload":
		return true
	case strings.HasPrefix(normalized, "/api/v1/workspace-files/"):
		return true
	default:
		return false
	}
}

func assistantControlAPIPath(path string) string {
	normalized := path
	if strings.HasPrefix(normalized, "/v1/") {
		normalized = "/api" + normalized
	}
	return normalized
}

func isAssistantApprovalResolveControlPath(path string) bool {
	_, ok := parseAssistantApprovalResolveControlPath(path)
	return ok
}

func parseAssistantApprovalResolveControlPath(path string) (string, bool) {
	const prefix = "/api/v1/approvals/"
	const suffix = "/resolve"
	if !strings.HasPrefix(path, prefix) || !strings.HasSuffix(path, suffix) {
		return "", false
	}
	approvalID := strings.TrimSuffix(strings.TrimPrefix(path, prefix), suffix)
	if approvalID == "" || strings.Contains(approvalID, "/") {
		return "", false
	}
	unescaped, err := url.PathUnescape(approvalID)
	if err != nil || strings.TrimSpace(unescaped) == "" {
		return "", false
	}
	return strings.TrimSpace(unescaped), true
}

func (h *handler) proxyAssistantBrowserWebSocket(
	w http.ResponseWriter,
	r *http.Request,
	assistantID string,
	runtimePath string,
	target *url.URL,
	gatewayToken string,
) (int, bool) {
	upstreamURL := *target
	switch upstreamURL.Scheme {
	case "http":
		upstreamURL.Scheme = "ws"
	case "https":
		upstreamURL.Scheme = "wss"
	}
	upstreamURL.Path = joinURLPath(target.Path, runtimePath)
	upstreamURL.RawPath = ""
	upstreamURL.RawQuery = r.URL.RawQuery

	headers := http.Header{}
	headers.Set("Authorization", "Bearer "+gatewayToken)
	headers.Set("Origin", runtimeProxyOrigin(target))
	headers.Set("X-Forwarded-Host", r.Host)
	headers.Set("X-Forwarded-Proto", forwardedProto(r))
	headers.Set("X-Forwarded-Prefix", "/assistants/"+assistantID+"/ws")
	headers.Set("X-IronAgent-Assistant-ID", assistantID)

	dialer := websocket.Dialer{Subprotocols: websocket.Subprotocols(r)}
	upstream, resp, err := dialer.DialContext(r.Context(), upstreamURL.String(), headers)
	if err != nil {
		if resp != nil && resp.Body != nil {
			_ = resp.Body.Close()
		}
		if h.logger != nil {
			h.logger.Warn("runtime websocket dial failed", "assistant_id", assistantID, "error", err)
		}
		writeProxyError(w, http.StatusBadGateway, "runtime_proxy_error", "runtime websocket proxy failed")
		return http.StatusBadGateway, true
	}
	defer upstream.Close()

	upgrader := websocket.Upgrader{
		CheckOrigin:  func(*http.Request) bool { return true },
		Subprotocols: websocket.Subprotocols(r),
	}
	downstream, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		return http.StatusSwitchingProtocols, true
	}
	defer downstream.Close()

	done := make(chan struct{}, 2)
	go func() {
		relayWebSocket(upstream, downstream, func(messageType int, payload []byte) []byte {
			if messageType != websocket.TextMessage {
				return payload
			}
			return injectConnectAuthToken(payload, gatewayToken)
		})
		done <- struct{}{}
	}()
	go func() {
		relayWebSocket(downstream, upstream, func(messageType int, payload []byte) []byte {
			if messageType != websocket.TextMessage {
				return payload
			}
			return filterWorkspaceSkillsStatusFrame(payload)
		})
		done <- struct{}{}
	}()
	<-done
	return http.StatusSwitchingProtocols, false
}

func runtimeProxyOrigin(target *url.URL) string {
	if target == nil {
		return ""
	}
	scheme := target.Scheme
	if scheme == "ws" {
		scheme = "http"
	} else if scheme == "wss" {
		scheme = "https"
	}
	if scheme == "" || target.Host == "" {
		return ""
	}
	return scheme + "://" + target.Host
}

func relayWebSocket(
	dst *websocket.Conn,
	src *websocket.Conn,
	transform func(int, []byte) []byte,
) {
	for {
		messageType, payload, err := src.ReadMessage()
		if err != nil {
			return
		}
		if transform != nil {
			payload = transform(messageType, payload)
		}
		if err := dst.WriteMessage(messageType, payload); err != nil {
			return
		}
	}
}

func injectConnectAuthToken(payload []byte, gatewayToken string) []byte {
	token := strings.TrimSpace(gatewayToken)
	if token == "" {
		return payload
	}
	var message map[string]any
	if err := json.Unmarshal(payload, &message); err != nil {
		return payload
	}
	if strings.TrimSpace(stringValue(message["type"])) != "req" ||
		strings.TrimSpace(stringValue(message["method"])) != "connect" {
		return payload
	}
	params, _ := message["params"].(map[string]any)
	if params == nil {
		params = map[string]any{}
		message["params"] = params
	}
	auth, _ := params["auth"].(map[string]any)
	if auth == nil {
		auth = map[string]any{}
		params["auth"] = auth
	}
	if strings.TrimSpace(stringValue(auth["token"])) == "" {
		auth["token"] = token
	}
	updated, err := json.Marshal(message)
	if err != nil {
		return payload
	}
	return updated
}

func filterWorkspaceSkillsStatusFrame(payload []byte) []byte {
	var message map[string]any
	if err := json.Unmarshal(payload, &message); err != nil {
		return payload
	}
	if !filterWorkspaceSkillsStatusMessage(message) {
		return payload
	}
	updated, err := json.Marshal(message)
	if err != nil {
		return payload
	}
	return updated
}

func filterWorkspaceSkillsStatusMessage(message map[string]any) bool {
	changed := false
	for _, key := range []string{"payload", "result"} {
		if value, ok := message[key].(map[string]any); ok {
			changed = filterWorkspaceSkillsStatusPayload(value) || changed
		}
	}
	return filterWorkspaceSkillsStatusPayload(message) || changed
}

func filterWorkspaceSkillsStatusPayload(payload map[string]any) bool {
	rawSkills, ok := payload["skills"].([]any)
	if !ok {
		return false
	}
	workspaceDir := cleanRuntimePath(stringValue(payload["workspaceDir"]))
	next := make([]any, 0, len(rawSkills))
	changed := false
	for _, rawSkill := range rawSkills {
		skill, ok := rawSkill.(map[string]any)
		if !ok {
			changed = true
			continue
		}
		source := strings.TrimSpace(stringValue(skill["source"]))
		baseDir := cleanRuntimePath(stringValue(skill["baseDir"]))
		filePath := cleanRuntimePath(stringValue(skill["filePath"]))
		if !isWorkspaceSkillStatus(source, workspaceDir, baseDir, filePath) {
			changed = true
			continue
		}
		displayName := workspaceSkillDirectoryName(workspaceDir, baseDir, filePath)
		if displayName == "" {
			next = append(next, rawSkill)
			continue
		}
		filteredSkill := make(map[string]any, len(skill)+1)
		for key, value := range skill {
			filteredSkill[key] = value
		}
		if stringValue(filteredSkill["name"]) != displayName {
			changed = true
		}
		filteredSkill["name"] = displayName
		filteredSkill["skillKey"] = displayName
		next = append(next, filteredSkill)
	}
	if len(next) != len(rawSkills) {
		changed = true
	}
	if !changed {
		return false
	}
	payload["skills"] = next
	return true
}

func isWorkspaceSkillStatus(source string, workspaceDir string, baseDir string, filePath string) bool {
	if source == "ironagent-workspace" {
		return true
	}
	if workspaceDir == "" {
		return false
	}
	skillsRoot := strings.TrimRight(workspaceDir, "/") + "/skills/"
	return strings.HasPrefix(baseDir, skillsRoot) || strings.HasPrefix(filePath, skillsRoot)
}

func workspaceSkillDirectoryName(workspaceDir string, baseDir string, filePath string) string {
	if workspaceDir != "" {
		skillsRoot := strings.TrimRight(workspaceDir, "/") + "/skills/"
		for _, candidate := range []string{baseDir, filePath} {
			if strings.HasPrefix(candidate, skillsRoot) {
				remainder := strings.TrimPrefix(candidate, skillsRoot)
				if name := firstPathSegment(remainder); name != "" {
					return name
				}
			}
		}
	}
	if name := lastPathSegment(baseDir); name != "" {
		return name
	}
	return lastPathSegment(strings.TrimSuffix(filePath, "/SKILL.md"))
}

func cleanRuntimePath(value string) string {
	value = strings.TrimSpace(strings.ReplaceAll(value, "\\", "/"))
	for strings.Contains(value, "//") {
		value = strings.ReplaceAll(value, "//", "/")
	}
	if value != "/" {
		value = strings.TrimRight(value, "/")
	}
	return value
}

func firstPathSegment(value string) string {
	value = strings.Trim(strings.ReplaceAll(value, "\\", "/"), "/")
	if value == "" {
		return ""
	}
	if before, _, ok := strings.Cut(value, "/"); ok {
		return strings.TrimSpace(before)
	}
	return strings.TrimSpace(value)
}

func lastPathSegment(value string) string {
	value = strings.Trim(strings.ReplaceAll(value, "\\", "/"), "/")
	if value == "" {
		return ""
	}
	if index := strings.LastIndex(value, "/"); index >= 0 {
		return strings.TrimSpace(value[index+1:])
	}
	return strings.TrimSpace(value)
}

func (h *handler) proxyAssistantControl(
	w http.ResponseWriter,
	r *http.Request,
	assistantID string,
	controlPath string,
) (int, bool) {
	switch {
	case r.Method == http.MethodGet && controlPath == "/api/v1/control-ui/assistant-context":
		status := "unknown"
		if record, ok := h.runtimes.Get(assistantID); ok {
			status = record.Status
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"assistants": []map[string]any{{
				"id":     assistantID,
				"status": status,
			}},
		})
		return http.StatusOK, false
	case r.Method == http.MethodPost && controlPath == "/api/v1/control-ui/launch-tickets/resolve":
		writeJSON(w, http.StatusOK, map[string]any{
			"assistant_id":  assistantID,
			"gateway_url":   "",
			"gateway_token": "",
			"session_key":   "agent:" + assistantID + ":main",
			"expires_at":    time.Now().UTC().Add(h.proxySession.ttl),
		})
		return http.StatusOK, false
	case r.Method == http.MethodGet && controlPath == "/api/v1/approvals":
		approvals, err := h.service.ListApprovals(
			r.Context(),
			r.URL.Query().Get("status"),
			assistantID,
			r.URL.Query().Get("approval_scope"),
		)
		if err != nil {
			writeError(w, err)
			return statusFromError(err), true
		}
		writeJSON(w, http.StatusOK, approvals)
		return http.StatusOK, false
	case r.Method == http.MethodPost && isAssistantApprovalResolveControlPath(controlPath):
		return h.resolveAssistantControlApproval(w, r, assistantID, controlPath)
	case r.Method == http.MethodGet && controlPath == "/api/v1/workspace-files":
		bucket := r.URL.Query().Get("bucket")
		response, err := h.service.ListFiles(r.Context(), core.FileListRequest{
			AssistantID: assistantID,
			Bucket:      bucket,
			Direction:   r.URL.Query().Get("direction"),
			FileRole:    r.URL.Query().Get("file_role"),
		})
		if err != nil {
			writeError(w, err)
			return statusFromError(err), true
		}
		payload := normalizeFileListResponse(response)
		if strings.TrimSpace(bucket) == "" {
			payload = filterFileListBuckets(payload, map[string]struct{}{
				"persistent": {},
				"temp":       {},
				"skills":     {},
				"plugins":    {},
			})
		}
		writeJSON(w, http.StatusOK, payload)
		return http.StatusOK, false
	case r.Method == http.MethodPost && controlPath == "/api/v1/workspace-files/upload":
		request, cleanup, err := h.parseAssistantScopedFileUploadRequest(w, r, assistantID)
		if cleanup != nil {
			defer cleanup()
		}
		if err != nil {
			writeError(w, err)
			return statusFromError(err), true
		}
		response, err := h.service.UploadFile(r.Context(), request)
		if err != nil {
			writeError(w, err)
			return statusFromError(err), true
		}
		if request.ContentLength > 0 {
			metrics.RecordFileTransfer("assistant_control_upload", request.ContentLength)
		}
		writeJSON(w, http.StatusOK, normalizeFileResponse(response, request))
		return http.StatusOK, false
	case strings.HasPrefix(controlPath, "/api/v1/workspace-files/"):
		return h.proxyAssistantControlFile(w, r, assistantID, controlPath)
	default:
		writeProxyError(w, http.StatusNotFound, "not_found", "assistant control route not found")
		return http.StatusNotFound, true
	}
}

func (h *handler) resolveAssistantControlApproval(
	w http.ResponseWriter,
	r *http.Request,
	assistantID string,
	controlPath string,
) (int, bool) {
	approvalID, ok := parseAssistantApprovalResolveControlPath(controlPath)
	if !ok {
		writeProxyError(w, http.StatusNotFound, "not_found", "control approval route not found")
		return http.StatusNotFound, true
	}
	var request core.ApprovalResolutionRequest
	if err := decodeJSON(r, &request); err != nil {
		writeError(w, err)
		return statusFromError(err), true
	}
	resolution := strings.ToLower(strings.TrimSpace(request.Resolution))
	if resolution != "allow" && resolution != "deny" {
		writeError(w, &core.ValidationError{Message: "resolution must be allow or deny"})
		return http.StatusBadRequest, true
	}
	approval, err := h.service.GetApproval(r.Context(), approvalID)
	if err != nil {
		writeError(w, err)
		return statusFromError(err), true
	}
	if strings.TrimSpace(approval.AssistantID) != assistantID {
		writeProxyError(w, http.StatusForbidden, "approval_scope_mismatch", "approval does not belong to this assistant control session")
		return http.StatusForbidden, true
	}
	if strings.TrimSpace(approval.Status) != "pending" {
		writeError(w, &core.ConflictError{Message: "approval is already resolved"})
		return http.StatusConflict, true
	}
	decision := "approve"
	if resolution == "deny" {
		decision = "deny"
	}
	response, err := h.service.ResolveApprovalDecision(r.Context(), approvalID, core.ApprovalDecisionRequest{Decision: decision})
	if err != nil {
		writeError(w, err)
		return statusFromError(err), true
	}
	if response.Task != nil {
		task, err := h.service.GetTask(r.Context(), response.Task.TaskID)
		if err == nil {
			h.dispatchStoredTaskEvent(r, "task.approval_resolved", task)
			if task.Status == "running" {
				h.dispatchStoredTaskEvent(r, "task.progress", task)
			}
			if task.Status == "rejected" {
				h.dispatchStoredTaskEvent(r, "task.rejected", task)
			}
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"approval": response.Approval, "task": response.Task})
	return http.StatusOK, false
}

func (h *handler) proxyAssistantControlFile(
	w http.ResponseWriter,
	r *http.Request,
	assistantID string,
	controlPath string,
) (int, bool) {
	filePath := strings.TrimPrefix(controlPath, "/api/v1/workspace-files/")
	if filePath == "" || filePath == "/" {
		writeProxyError(w, http.StatusNotFound, "not_found", "assistant control route not found")
		return http.StatusNotFound, true
	}
	switch {
	case strings.HasSuffix(filePath, "/download"):
		fileID := strings.Trim(strings.TrimSuffix(filePath, "/download"), "/")
		if fileID == "" {
			writeProxyError(w, http.StatusBadRequest, "bad_request", "file_id is required")
			return http.StatusBadRequest, true
		}
		metadata, err := h.service.GetFile(r.Context(), fileID)
		if err != nil {
			writeError(w, err)
			return statusFromError(err), true
		}
		if err := validateAssistantFile(metadata, assistantID); err != nil {
			writeError(w, err)
			return statusFromError(err), true
		}
		response, err := h.service.DownloadFileStream(r.Context(), fileID, r.URL.Query().Get("disposition"))
		if err != nil {
			writeError(w, err)
			return statusFromError(err), true
		}
		defer response.Body.Close()
		if response.ContentType != "" {
			w.Header().Set("Content-Type", response.ContentType)
		}
		if response.ContentDisposition != "" {
			w.Header().Set("Content-Disposition", response.ContentDisposition)
		}
		if response.DownloadSource != "" {
			w.Header().Set("X-IronAgent-Download-Source", response.DownloadSource)
		}
		if response.StorageBackend != "" {
			w.Header().Set("X-IronAgent-Storage-Backend", response.StorageBackend)
		}
		w.WriteHeader(http.StatusOK)
		written, _ := io.Copy(w, response.Body)
		metrics.RecordFileTransfer("assistant_control_download", written)
		return http.StatusOK, false
	case strings.HasSuffix(filePath, "/delete"):
		fileID := strings.Trim(strings.TrimSuffix(filePath, "/delete"), "/")
		if fileID == "" {
			writeProxyError(w, http.StatusBadRequest, "bad_request", "file_id is required")
			return http.StatusBadRequest, true
		}
		var request core.FileDeleteRequest
		if r.ContentLength != 0 {
			if err := decodeJSON(r, &request); err != nil {
				writeError(w, err)
				return statusFromError(err), true
			}
		}
		metadata, err := h.service.GetFile(r.Context(), fileID)
		if err != nil {
			writeError(w, err)
			return statusFromError(err), true
		}
		if err := validateAssistantFile(metadata, assistantID); err != nil {
			writeError(w, err)
			return statusFromError(err), true
		}
		if err := validateWorkspaceFileViewMutation(metadata, "delete"); err != nil {
			writeError(w, err)
			return statusFromError(err), true
		}
		response, err := h.service.DeleteFile(r.Context(), fileID, request)
		if err != nil {
			writeError(w, err)
			return statusFromError(err), true
		}
		writeJSON(w, http.StatusOK, response)
		return http.StatusOK, false
	default:
		fileID := strings.Trim(filePath, "/")
		if fileID == "" {
			writeProxyError(w, http.StatusBadRequest, "bad_request", "file_id is required")
			return http.StatusBadRequest, true
		}
		response, err := h.service.GetFile(r.Context(), fileID)
		if err != nil {
			writeError(w, err)
			return statusFromError(err), true
		}
		if err := validateAssistantFile(response, assistantID); err != nil {
			writeError(w, err)
			return statusFromError(err), true
		}
		writeJSON(w, http.StatusOK, normalizeFileResponse(response, core.FileUploadRequest{}))
		return http.StatusOK, false
	}
}

func (h *handler) getTask(w http.ResponseWriter, r *http.Request) {
	taskID := r.PathValue("task_id")
	h.logChatAPIInfo("查询任务开始", "task_id", taskID)
	task, err := h.service.GetTask(r.Context(), taskID)
	if err != nil {
		h.logChatAPIWarn("查询任务失败", "task_id", taskID, "error", err)
		writeError(w, err)
		return
	}

	response := h.newPublicTaskResponse(r.Context(), task)
	h.logChatAPIInfo(
		"查询任务成功",
		"task_id", taskID,
		"status", response.Status,
		"stage", response.Stage,
	)
	writeJSON(w, http.StatusOK, response)
}

func (h *handler) startTask(w http.ResponseWriter, r *http.Request) {
	task, err := h.service.StartTask(r.Context(), r.PathValue("task_id"))
	if err != nil {
		writeError(w, err)
		return
	}
	h.dispatchStoredTaskEvent(r, "task.progress", task)

	writeJSON(w, http.StatusOK, map[string]any{"task": task})
}

func (h *handler) completeTask(w http.ResponseWriter, r *http.Request) {
	var request core.CompleteTaskRequest
	if err := decodeJSON(r, &request); err != nil {
		writeError(w, err)
		return
	}

	task, err := h.service.CompleteTask(r.Context(), r.PathValue("task_id"), request)
	if err != nil {
		writeError(w, err)
		return
	}
	h.dispatchStoredTaskEvent(r, "task.completed", task)
	writeJSON(w, http.StatusOK, map[string]any{"task": task})
}

func (h *handler) failTask(w http.ResponseWriter, r *http.Request) {
	var request core.FailTaskRequest
	if err := decodeJSON(r, &request); err != nil {
		writeError(w, err)
		return
	}

	task, err := h.service.FailTask(r.Context(), r.PathValue("task_id"), request)
	if err != nil {
		writeError(w, err)
		return
	}
	h.dispatchStoredTaskEvent(r, "task.failed", task)
	writeJSON(w, http.StatusOK, map[string]any{"task": task})
}

func (h *handler) resolveTaskApproval(w http.ResponseWriter, r *http.Request) {
	var request core.ApprovalResolutionRequest
	if err := decodeJSON(r, &request); err != nil {
		writeError(w, err)
		return
	}

	task, err := h.service.ResolveTaskApproval(r.Context(), r.PathValue("task_id"), request)
	if err != nil {
		writeError(w, err)
		return
	}
	h.dispatchStoredTaskEvent(r, "task.approval_resolved", task)
	if task.Status == "running" {
		h.dispatchStoredTaskEvent(r, "task.progress", task)
	}
	if task.Status == "rejected" {
		h.dispatchStoredTaskEvent(r, "task.rejected", task)
	}

	writeJSON(w, http.StatusOK, map[string]any{"task": task})
}

func (h *handler) getApproval(w http.ResponseWriter, r *http.Request) {
	approvalID := r.PathValue("approval_id")
	h.logChatAPIInfo("查询审批详情开始", "approval_id", approvalID)
	approval, err := h.service.GetApproval(r.Context(), approvalID)
	if err != nil {
		h.logChatAPIWarn("查询审批详情失败", "approval_id", approvalID, "error", err)
		writeError(w, err)
		return
	}
	h.logChatAPIInfo(
		"查询审批详情成功",
		"approval_id", approvalID,
		"task_id", approval.TaskID,
		"status", approval.Status,
	)
	writeJSON(w, http.StatusOK, openApprovalResponse(approval))
}

func (h *handler) decideApproval(w http.ResponseWriter, r *http.Request) {
	approvalID := r.PathValue("approval_id")
	var request core.ApprovalDecisionRequest
	if err := decodeJSON(r, &request); err != nil {
		h.logChatAPIWarn("审批决策请求校验失败", "approval_id", approvalID, "error", err)
		writeError(w, err)
		return
	}

	h.logChatAPIInfo(
		"审批决策开始",
		"approval_id", approvalID,
		"decision", request.Decision,
	)
	response, err := h.service.ResolveApprovalDecision(r.Context(), approvalID, request)
	if err != nil {
		h.logChatAPIWarn(
			"审批决策失败",
			"approval_id", approvalID,
			"decision", request.Decision,
			"error", err,
		)
		writeError(w, err)
		return
	}
	if response.Task != nil {
		task, err := h.service.GetTask(r.Context(), response.Task.TaskID)
		if err == nil {
			h.dispatchStoredTaskEvent(r, "task.approval_resolved", task)
			if task.Status == "running" {
				h.dispatchStoredTaskEvent(r, "task.progress", task)
			}
			if task.Status == "rejected" {
				h.dispatchStoredTaskEvent(r, "task.rejected", task)
			}
		}
	}
	h.logChatAPIInfo(
		"审批决策成功",
		"approval_id", approvalID,
		"decision", response.Approval.Decision,
		"status", response.Approval.Status,
	)
	writeJSON(w, http.StatusOK, openApprovalDecisionResponse(response.Approval))
}

type openApprovalView struct {
	ApprovalID string     `json:"approval_id"`
	TaskID     string     `json:"task_id,omitempty"`
	Status     string     `json:"status"`
	RiskLevel  string     `json:"risk_level,omitempty"`
	Action     string     `json:"action,omitempty"`
	Resource   string     `json:"resource,omitempty"`
	CreatedAt  *time.Time `json:"created_at,omitempty"`
}

type openApprovalDecisionView struct {
	ApprovalID string    `json:"approval_id"`
	Decision   string    `json:"decision"`
	ResolvedAt time.Time `json:"resolved_at"`
}

func openApprovalResponse(approval core.ApprovalResponse) openApprovalView {
	return openApprovalView{
		ApprovalID: approval.ApprovalID,
		TaskID:     approval.TaskID,
		Status:     approval.Status,
		RiskLevel:  openApprovalRiskLevel(approval),
		Action:     approval.Action,
		Resource:   approval.Resource,
		CreatedAt:  approval.RequestedAt,
	}
}

func openApprovalRiskLevel(approval core.ApprovalResponse) string {
	switch strings.ToLower(strings.TrimSpace(approval.Reason)) {
	case "low", "medium", "high", "critical":
		return strings.ToLower(strings.TrimSpace(approval.Reason))
	default:
		return "medium"
	}
}

func openApprovalDecisionResponse(approval core.ApprovalResponse) openApprovalDecisionView {
	resolvedAt := time.Now().UTC()
	if approval.ResolvedAt != nil {
		resolvedAt = *approval.ResolvedAt
	}
	return openApprovalDecisionView{
		ApprovalID: approval.ApprovalID,
		Decision:   approval.Decision,
		ResolvedAt: resolvedAt,
	}
}

func (h *handler) cancelTask(w http.ResponseWriter, r *http.Request) {
	taskID := r.PathValue("task_id")
	request := core.CancelTaskRequest{ActorID: "ironagent-api"}
	if strings.TrimSpace(r.Header.Get("X-API-Key")) != "" {
		if err := decodeJSON(r, &request); err != nil {
			h.logChatAPIWarn("取消任务请求校验失败", "task_id", taskID, "error", err)
			writeError(w, err)
			return
		}
	} else {
		if err := decodeOptionalEmptyJSON(r); err != nil {
			h.logChatAPIWarn("取消任务请求校验失败", "task_id", taskID, "error", err)
			writeError(w, err)
			return
		}
	}

	h.logChatAPIInfo("取消任务开始", "task_id", taskID, "actor_id", request.ActorID)
	task, err := h.service.CancelTask(r.Context(), taskID, request)
	if err != nil {
		h.logChatAPIWarn("取消任务失败", "task_id", taskID, "actor_id", request.ActorID, "error", err)
		writeError(w, err)
		return
	}
	h.dispatchStoredTaskEventWithCancelledBy(r, "task.cancelled", task, request.ActorID)

	response := h.newTaskResponseWithFiles(r.Context(), task)
	cancelledAt := task.FailedAt
	if cancelledAt == nil {
		now := time.Now().UTC()
		cancelledAt = &now
	}
	h.logChatAPIInfo(
		"取消任务成功",
		"task_id", response.TaskID,
		"status", response.Status,
		"cancelled_at", cancelledAt.Format(time.RFC3339Nano),
	)
	writeJSON(w, http.StatusOK, map[string]any{
		"task_id":      response.TaskID,
		"status":       response.Status,
		"cancelled_at": cancelledAt,
	})
}

func (h *handler) getTaskQueueStats(w http.ResponseWriter, r *http.Request) {
	stats, err := h.service.TaskQueueStats(r.Context())
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"queue": stats})
}

func (h *handler) getMetrics(w http.ResponseWriter, r *http.Request) {
	stats, err := h.service.TaskQueueStats(r.Context())
	if err != nil {
		writeError(w, err)
		return
	}
	metrics := map[string]any{
		"task_queue": stats,
		"timestamp":  time.Now().UTC(),
	}
	dbPools := h.dbPoolStats(r.Context())
	if len(dbPools) > 0 {
		metrics["db_pools"] = dbPools
	}
	if dbStats, ok := dbPools["primary"]; ok {
		metrics["db_pool"] = dbStats
	}
	if h.runtimes != nil {
		metrics["runtime_registry"] = map[string]any{
			"runtime_count": len(h.runtimes.Snapshot()),
			"last_decision": h.runtimes.LastDecision(),
		}
	}
	writeJSON(w, http.StatusOK, metrics)
}

func (h *handler) getPrometheusMetrics(w http.ResponseWriter, r *http.Request) {
	stats, err := h.service.TaskQueueStats(r.Context())
	if err != nil {
		writeError(w, err)
		return
	}
	report := h.healthReport(r.Context())
	var builder strings.Builder
	writePrometheusGauge(&builder, "go_gateway_health_ready", "Gateway readiness status, 1 means ready.", boolFloat(report.Ready), nil)
	writePrometheusHeader(&builder, "go_gateway_component_up", "Gateway component health, 1 means up.", "gauge")
	for name, status := range report.Components {
		writePrometheusSample(&builder, "go_gateway_component_up", boolFloat(status == "up"), map[string]string{
			"component": name,
			"status":    status,
		})
	}

	writePrometheusGauge(&builder, "go_gateway_task_queue_queued", "Queued gateway tasks.", float64(stats.Queued), nil)
	writePrometheusGauge(&builder, "go_gateway_task_queue_processing", "Processing gateway tasks.", float64(stats.Processing), nil)
	writePrometheusGauge(&builder, "go_gateway_task_queue_retry", "Gateway tasks waiting for retry.", float64(stats.Retry), nil)
	writePrometheusGauge(&builder, "go_gateway_task_queue_failed", "Failed gateway tasks.", float64(stats.Failed), nil)
	writePrometheusGauge(&builder, "go_gateway_task_queue_completed", "Completed gateway tasks.", float64(stats.Completed), nil)
	writePrometheusGauge(&builder, "go_gateway_task_queue_cancelled", "Cancelled gateway tasks.", float64(stats.Cancelled), nil)
	writePrometheusGauge(&builder, "go_gateway_task_queue_active", "Active gateway tasks counted as queued plus processing plus retry.", float64(stats.Active), nil)
	writePrometheusGauge(&builder, "go_gateway_task_queue_max_pending", "Configured pending-task backpressure threshold.", float64(stats.MaxPending), nil)

	dbPools := h.dbPoolStats(r.Context())
	if dbStats, ok := dbPools["primary"]; ok {
		writeDBPoolMetrics(&builder, dbStats)
	}
	writeNamedDBPoolMetrics(&builder, dbPools)

	runtimeMetrics := h.runtimeMetricsSnapshot()
	writePrometheusGauge(&builder, "go_gateway_runtime_registered_total", "Registered runtime count.", float64(runtimeMetrics.Registered), nil)
	writePrometheusGauge(&builder, "go_gateway_runtime_ready_total", "Ready runtime count.", float64(runtimeMetrics.Ready), nil)
	writePrometheusGauge(&builder, "go_gateway_runtime_draining_total", "Draining runtime count.", float64(runtimeMetrics.Draining), nil)
	writePrometheusGauge(&builder, "go_gateway_runtime_unhealthy_total", "Unhealthy runtime count.", float64(runtimeMetrics.Unhealthy), nil)
	writePrometheusGauge(&builder, "go_gateway_runtime_in_flight_total", "Total in-flight tasks across registered runtimes.", float64(runtimeMetrics.InFlight), nil)
	writePrometheusGauge(&builder, "go_gateway_runtime_capacity_total", "Total capacity across registered runtimes.", float64(runtimeMetrics.Capacity), nil)
	if h.webhooks != nil {
		if count, ok := h.webhooks.DeadLetterCount(); ok {
			writePrometheusGauge(&builder, "go_gateway_webhook_dead_letters_total", "Webhook dead-letter count visible to this gateway.", float64(count), nil)
		}
	}
	writeTaskCreateMetrics(&builder)
	writeSouthboundMetrics(&builder)
	writeSchedulerMetrics(&builder)
	writeWebhookMetrics(&builder)
	writeFileTransferMetrics(&builder)
	writeRuntimeProxyMetrics(&builder)

	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(builder.String()))
}

func (h *handler) dbPoolStats(ctx context.Context) map[string]core.DBPoolStats {
	pools := map[string]core.DBPoolStats{}
	if dbStats, ok := h.service.DBPoolStats(ctx); ok {
		pools["primary"] = dbStats
	}
	for name, provider := range h.dbPoolStatsProviders {
		pools[name] = provider.DBPoolStats()
	}
	if len(pools) == 0 {
		return nil
	}
	return pools
}

type runtimeMetrics struct {
	Registered int
	Ready      int
	Draining   int
	Unhealthy  int
	InFlight   int
	Capacity   int
}

func (h *handler) runtimeMetricsSnapshot() runtimeMetrics {
	var metrics runtimeMetrics
	if h.runtimes == nil {
		return metrics
	}
	for _, record := range h.runtimes.Snapshot() {
		metrics.Registered++
		metrics.InFlight += record.InFlight
		metrics.Capacity += record.Capacity
		switch {
		case record.DrainMode:
			metrics.Draining++
		case runtimeStatusAvailable(record.Status):
			metrics.Ready++
		default:
			metrics.Unhealthy++
		}
	}
	return metrics
}

func writeTaskCreateMetrics(builder *strings.Builder) {
	snapshot := metrics.TaskCreateSnapshot()
	if len(snapshot) == 0 {
		return
	}
	writePrometheusHeader(builder, "go_gateway_task_create_total", "Total northbound chat task create requests.", "counter")
	writePrometheusHeader(builder, "go_gateway_task_create_errors_total", "Total failed northbound chat task create requests.", "counter")
	writePrometheusHeader(builder, "go_gateway_task_create_latency_seconds_sum", "Total northbound chat task create latency in seconds.", "counter")
	writePrometheusHeader(builder, "go_gateway_task_create_latency_seconds_count", "Total northbound chat task create latency observations.", "counter")
	writePrometheusHeader(builder, "go_gateway_task_create_latency_seconds_max", "Maximum observed northbound chat task create latency in seconds.", "gauge")
	writePrometheusHeader(builder, "go_gateway_task_create_latency_seconds_bucket", "Northbound chat task create latency buckets.", "counter")
	for _, metric := range snapshot {
		labels := map[string]string{
			"result": metric.Result,
			"status": metric.Status,
			"stage":  metric.Stage,
		}
		writePrometheusSample(builder, "go_gateway_task_create_total", float64(metric.Requests), labels)
		writePrometheusSample(builder, "go_gateway_task_create_errors_total", float64(metric.Errors), labels)
		writePrometheusSample(builder, "go_gateway_task_create_latency_seconds_sum", metric.LatencySumSeconds, labels)
		writePrometheusSample(builder, "go_gateway_task_create_latency_seconds_count", float64(metric.Requests), labels)
		writePrometheusSample(builder, "go_gateway_task_create_latency_seconds_max", metric.LatencyMaxSeconds, labels)
		for _, bucket := range metric.Buckets {
			bucketLabels := copyLabels(labels)
			bucketLabels["le"] = bucket.LE
			writePrometheusSample(builder, "go_gateway_task_create_latency_seconds_bucket", float64(bucket.Count), bucketLabels)
		}
	}
}

func writeDBPoolMetrics(builder *strings.Builder, stats core.DBPoolStats) {
	writePrometheusGauge(builder, "go_gateway_db_pool_max_open_connections", "Configured maximum open database connections.", float64(stats.MaxOpenConnections), nil)
	writePrometheusGauge(builder, "go_gateway_db_pool_open_connections", "Current open database connections.", float64(stats.OpenConnections), nil)
	writePrometheusGauge(builder, "go_gateway_db_pool_in_use", "Current in-use database connections.", float64(stats.InUse), nil)
	writePrometheusGauge(builder, "go_gateway_db_pool_idle", "Current idle database connections.", float64(stats.Idle), nil)

	writePrometheusHeader(builder, "go_gateway_db_pool_wait_count", "Total database connection wait count.", "counter")
	writePrometheusSample(builder, "go_gateway_db_pool_wait_count", float64(stats.WaitCount), nil)
	writePrometheusHeader(builder, "go_gateway_db_pool_wait_duration_seconds", "Total database connection wait duration in seconds.", "counter")
	writePrometheusSample(builder, "go_gateway_db_pool_wait_duration_seconds", stats.WaitDurationSecs, nil)
	writePrometheusHeader(builder, "go_gateway_db_pool_max_idle_closed", "Total database connections closed due to SetMaxIdleConns.", "counter")
	writePrometheusSample(builder, "go_gateway_db_pool_max_idle_closed", float64(stats.MaxIdleClosed), nil)
	writePrometheusHeader(builder, "go_gateway_db_pool_max_idle_time_closed", "Total database connections closed due to SetConnMaxIdleTime.", "counter")
	writePrometheusSample(builder, "go_gateway_db_pool_max_idle_time_closed", float64(stats.MaxIdleTimeClosed), nil)
	writePrometheusHeader(builder, "go_gateway_db_pool_max_lifetime_closed", "Total database connections closed due to SetConnMaxLifetime.", "counter")
	writePrometheusSample(builder, "go_gateway_db_pool_max_lifetime_closed", float64(stats.MaxLifetimeClosed), nil)
}

type namedDBPoolMetric struct {
	name  string
	help  string
	typ   string
	value func(core.DBPoolStats) float64
}

func writeNamedDBPoolMetrics(builder *strings.Builder, pools map[string]core.DBPoolStats) {
	if len(pools) == 0 {
		return
	}
	names := make([]string, 0, len(pools))
	for name := range pools {
		names = append(names, name)
	}
	sort.Strings(names)

	metrics := []namedDBPoolMetric{
		{
			name:  "go_gateway_db_pool_by_pool_max_open_connections",
			help:  "Configured maximum open database connections by pool.",
			typ:   "gauge",
			value: func(stats core.DBPoolStats) float64 { return float64(stats.MaxOpenConnections) },
		},
		{
			name:  "go_gateway_db_pool_by_pool_open_connections",
			help:  "Current open database connections by pool.",
			typ:   "gauge",
			value: func(stats core.DBPoolStats) float64 { return float64(stats.OpenConnections) },
		},
		{
			name:  "go_gateway_db_pool_by_pool_in_use",
			help:  "Current in-use database connections by pool.",
			typ:   "gauge",
			value: func(stats core.DBPoolStats) float64 { return float64(stats.InUse) },
		},
		{
			name:  "go_gateway_db_pool_by_pool_idle",
			help:  "Current idle database connections by pool.",
			typ:   "gauge",
			value: func(stats core.DBPoolStats) float64 { return float64(stats.Idle) },
		},
		{
			name:  "go_gateway_db_pool_by_pool_wait_count",
			help:  "Total database connection wait count by pool.",
			typ:   "counter",
			value: func(stats core.DBPoolStats) float64 { return float64(stats.WaitCount) },
		},
		{
			name:  "go_gateway_db_pool_by_pool_wait_duration_seconds",
			help:  "Total database connection wait duration in seconds by pool.",
			typ:   "counter",
			value: func(stats core.DBPoolStats) float64 { return stats.WaitDurationSecs },
		},
		{
			name:  "go_gateway_db_pool_by_pool_max_idle_closed",
			help:  "Total database connections closed due to SetMaxIdleConns by pool.",
			typ:   "counter",
			value: func(stats core.DBPoolStats) float64 { return float64(stats.MaxIdleClosed) },
		},
		{
			name:  "go_gateway_db_pool_by_pool_max_idle_time_closed",
			help:  "Total database connections closed due to SetConnMaxIdleTime by pool.",
			typ:   "counter",
			value: func(stats core.DBPoolStats) float64 { return float64(stats.MaxIdleTimeClosed) },
		},
		{
			name:  "go_gateway_db_pool_by_pool_max_lifetime_closed",
			help:  "Total database connections closed due to SetConnMaxLifetime by pool.",
			typ:   "counter",
			value: func(stats core.DBPoolStats) float64 { return float64(stats.MaxLifetimeClosed) },
		},
	}
	for _, metric := range metrics {
		writePrometheusHeader(builder, metric.name, metric.help, metric.typ)
		for _, poolName := range names {
			writePrometheusSample(builder, metric.name, metric.value(pools[poolName]), map[string]string{
				"pool": poolName,
			})
		}
	}
}

func writeSouthboundMetrics(builder *strings.Builder) {
	snapshot := metrics.SouthboundSnapshot()
	if len(snapshot) == 0 {
		return
	}
	writePrometheusHeader(builder, "go_gateway_southbound_requests_total", "Total southbound HTTP requests.", "counter")
	writePrometheusHeader(builder, "go_gateway_southbound_errors_total", "Total failed southbound HTTP requests.", "counter")
	writePrometheusHeader(builder, "go_gateway_southbound_latency_seconds_sum", "Total southbound HTTP latency in seconds.", "counter")
	writePrometheusHeader(builder, "go_gateway_southbound_latency_seconds_count", "Total southbound HTTP latency observations.", "counter")
	writePrometheusHeader(builder, "go_gateway_southbound_latency_seconds_max", "Maximum observed southbound HTTP latency in seconds.", "gauge")
	writePrometheusHeader(builder, "go_gateway_southbound_latency_seconds_bucket", "Southbound HTTP latency buckets.", "counter")
	for _, metric := range snapshot {
		labels := map[string]string{
			"component":    metric.Component,
			"method":       metric.Method,
			"path":         metric.Path,
			"status_class": metric.StatusClass,
		}
		writePrometheusSample(builder, "go_gateway_southbound_requests_total", float64(metric.Requests), labels)
		writePrometheusSample(builder, "go_gateway_southbound_errors_total", float64(metric.Errors), labels)
		writePrometheusSample(builder, "go_gateway_southbound_latency_seconds_sum", metric.LatencySumSeconds, labels)
		writePrometheusSample(builder, "go_gateway_southbound_latency_seconds_count", float64(metric.Requests), labels)
		writePrometheusSample(builder, "go_gateway_southbound_latency_seconds_max", metric.LatencyMaxSeconds, labels)
		for _, bucket := range metric.Buckets {
			bucketLabels := copyLabels(labels)
			bucketLabels["le"] = bucket.LE
			writePrometheusSample(builder, "go_gateway_southbound_latency_seconds_bucket", float64(bucket.Count), bucketLabels)
		}
	}
}

func writeSchedulerMetrics(builder *strings.Builder) {
	snapshot := metrics.SchedulerSnapshot()
	if len(snapshot) == 0 {
		return
	}
	writePrometheusHeader(builder, "go_gateway_scheduler_decisions_total", "Total scheduler decisions.", "counter")
	writePrometheusHeader(builder, "go_gateway_scheduler_latency_seconds_sum", "Total scheduler decision latency in seconds.", "counter")
	writePrometheusHeader(builder, "go_gateway_scheduler_latency_seconds_count", "Total scheduler latency observations.", "counter")
	writePrometheusHeader(builder, "go_gateway_scheduler_latency_seconds_max", "Maximum observed scheduler decision latency in seconds.", "gauge")
	writePrometheusHeader(builder, "go_gateway_scheduler_latency_seconds_bucket", "Scheduler decision latency buckets.", "counter")
	writePrometheusHeader(builder, "go_gateway_scheduler_candidates_sum", "Total scheduler candidate count across decisions.", "counter")
	for _, metric := range snapshot {
		labels := map[string]string{
			"outcome": metric.Outcome,
			"reason":  metric.Reason,
		}
		writePrometheusSample(builder, "go_gateway_scheduler_decisions_total", float64(metric.Count), labels)
		writePrometheusSample(builder, "go_gateway_scheduler_latency_seconds_sum", metric.LatencySumSeconds, labels)
		writePrometheusSample(builder, "go_gateway_scheduler_latency_seconds_count", float64(metric.Count), labels)
		writePrometheusSample(builder, "go_gateway_scheduler_latency_seconds_max", metric.LatencyMaxSeconds, labels)
		writePrometheusSample(builder, "go_gateway_scheduler_candidates_sum", float64(metric.CandidateSum), labels)
		for _, bucket := range metric.Buckets {
			bucketLabels := copyLabels(labels)
			bucketLabels["le"] = bucket.LE
			writePrometheusSample(builder, "go_gateway_scheduler_latency_seconds_bucket", float64(bucket.Count), bucketLabels)
		}
	}
}

func writeWebhookMetrics(builder *strings.Builder) {
	snapshot := metrics.WebhookSnapshot()
	if len(snapshot) == 0 {
		return
	}
	writePrometheusHeader(builder, "go_gateway_webhook_events_total", "Total webhook delivery lifecycle events.", "counter")
	for _, metric := range snapshot {
		writePrometheusSample(builder, "go_gateway_webhook_events_total", float64(metric.Count), map[string]string{
			"result":     metric.Result,
			"event_type": metric.EventType,
		})
	}
}

func writeFileTransferMetrics(builder *strings.Builder) {
	snapshot := metrics.FileTransferSnapshot()
	if len(snapshot) == 0 {
		return
	}
	writePrometheusHeader(builder, "go_gateway_file_transfer_total", "Total file transfers through gateway endpoints.", "counter")
	writePrometheusHeader(builder, "go_gateway_file_transfer_bytes_total", "Total file bytes through gateway endpoints.", "counter")
	for _, metric := range snapshot {
		labels := map[string]string{"direction": metric.Direction}
		writePrometheusSample(builder, "go_gateway_file_transfer_total", float64(metric.Count), labels)
		writePrometheusSample(builder, "go_gateway_file_transfer_bytes_total", float64(metric.Bytes), labels)
	}
}

func writeRuntimeProxyMetrics(builder *strings.Builder) {
	snapshot := metrics.RuntimeProxySnapshot()
	inFlightSnapshot := metrics.RuntimeProxyInFlightSnapshot()
	circuitSnapshot := metrics.RuntimeProxyCircuitSnapshot()
	if len(snapshot) == 0 && len(inFlightSnapshot) == 0 && len(circuitSnapshot) == 0 {
		return
	}
	if len(circuitSnapshot) > 0 {
		writePrometheusHeader(builder, "go_gateway_runtime_proxy_circuit_open_total", "Total runtime proxy circuit open events.", "counter")
		for _, metric := range circuitSnapshot {
			writePrometheusSample(builder, "go_gateway_runtime_proxy_circuit_open_total", float64(metric.Count), map[string]string{
				"kind": metric.Kind,
			})
		}
	}
	if len(inFlightSnapshot) > 0 {
		writePrometheusHeader(builder, "go_gateway_runtime_proxy_inflight", "Runtime proxy requests currently in flight.", "gauge")
		for _, metric := range inFlightSnapshot {
			writePrometheusSample(builder, "go_gateway_runtime_proxy_inflight", float64(metric.InFlight), map[string]string{
				"kind": metric.Kind,
			})
		}
	}
	if len(snapshot) == 0 {
		return
	}
	writePrometheusHeader(builder, "go_gateway_runtime_proxy_requests_total", "Total runtime proxy requests.", "counter")
	writePrometheusHeader(builder, "go_gateway_runtime_proxy_errors_total", "Total failed runtime proxy requests.", "counter")
	writePrometheusHeader(builder, "go_gateway_runtime_proxy_latency_seconds_sum", "Total runtime proxy latency in seconds.", "counter")
	writePrometheusHeader(builder, "go_gateway_runtime_proxy_latency_seconds_count", "Total runtime proxy latency observations.", "counter")
	writePrometheusHeader(builder, "go_gateway_runtime_proxy_latency_seconds_max", "Maximum observed runtime proxy latency in seconds.", "gauge")
	writePrometheusHeader(builder, "go_gateway_runtime_proxy_latency_seconds_bucket", "Runtime proxy latency buckets.", "counter")
	for _, metric := range snapshot {
		labels := map[string]string{
			"kind":         metric.Kind,
			"status_class": metric.StatusClass,
		}
		writePrometheusSample(builder, "go_gateway_runtime_proxy_requests_total", float64(metric.Requests), labels)
		writePrometheusSample(builder, "go_gateway_runtime_proxy_errors_total", float64(metric.Errors), labels)
		writePrometheusSample(builder, "go_gateway_runtime_proxy_latency_seconds_sum", metric.LatencySumSeconds, labels)
		writePrometheusSample(builder, "go_gateway_runtime_proxy_latency_seconds_count", float64(metric.Requests), labels)
		writePrometheusSample(builder, "go_gateway_runtime_proxy_latency_seconds_max", metric.LatencyMaxSeconds, labels)
		for _, bucket := range metric.Buckets {
			bucketLabels := copyLabels(labels)
			bucketLabels["le"] = bucket.LE
			writePrometheusSample(builder, "go_gateway_runtime_proxy_latency_seconds_bucket", float64(bucket.Count), bucketLabels)
		}
	}
}

func (h *handler) uploadFile(w http.ResponseWriter, r *http.Request) {
	request, cleanup, err := h.parseFileUploadRequest(w, r)
	if cleanup != nil {
		defer cleanup()
	}
	if err != nil {
		h.logFileAPIWarn("文件上传请求校验失败", "error", err)
		writeError(w, err)
		return
	}
	h.logFileAPIInfo(
		"文件上传开始",
		"user_id", request.UserID,
		"filename", request.Filename,
		"mime_type", request.ContentType,
		"size", request.ContentLength,
	)

	response, err := h.service.UploadFile(r.Context(), request)
	if err != nil {
		h.logFileAPIWarn(
			"文件上传失败",
			"user_id", request.UserID,
			"filename", request.Filename,
			"mime_type", request.ContentType,
			"size", request.ContentLength,
			"error", err,
		)
		writeError(w, err)
		return
	}
	if request.ContentLength > 0 {
		metrics.RecordFileTransfer("direct_upload", request.ContentLength)
	}
	metadata := normalizePublicFileResponse(response, request)
	h.logFileAPIInfo(
		"文件上传成功",
		"user_id", request.UserID,
		"file_id", metadata.FileID,
		"filename", metadata.Filename,
		"mime_type", metadata.MimeType,
		"size", metadata.Size,
	)
	writeJSON(w, http.StatusOK, metadata)
}

func (h *handler) listFiles(w http.ResponseWriter, r *http.Request) {
	response, err := h.service.ListFiles(r.Context(), core.FileListRequest{
		AssistantID: r.URL.Query().Get("assistant_id"),
		UserID:      r.URL.Query().Get("user_id"),
		Bucket:      r.URL.Query().Get("bucket"),
		Direction:   r.URL.Query().Get("direction"),
		FileRole:    r.URL.Query().Get("file_role"),
	})
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, normalizeFileListResponse(response))
}

func (h *handler) getFile(w http.ResponseWriter, r *http.Request) {
	fileID := r.PathValue("file_id")
	userID, err := requiredUserID(r)
	if err != nil {
		h.logFileAPIWarn("文件元信息查询请求校验失败", "file_id", fileID, "error", err)
		writeError(w, err)
		return
	}
	h.logFileAPIInfo("文件元信息查询开始", "user_id", userID, "file_id", fileID)
	response, err := h.service.GetFile(r.Context(), fileID)
	if err != nil {
		h.logFileAPIWarn("文件元信息查询失败", "user_id", userID, "file_id", fileID, "error", err)
		writeError(w, err)
		return
	}
	if err := validateFileUser(response, userID); err != nil {
		h.logFileAPIWarn("文件元信息查询归属校验失败", "user_id", userID, "file_id", fileID, "error", err)
		writeError(w, err)
		return
	}
	metadata := normalizePublicFileResponse(response, core.FileUploadRequest{})
	h.logFileAPIInfo(
		"文件元信息查询成功",
		"user_id", userID,
		"file_id", metadata.FileID,
		"filename", metadata.Filename,
		"mime_type", metadata.MimeType,
		"size", metadata.Size,
	)
	writeJSON(w, http.StatusOK, metadata)
}

func (h *handler) syncResults(w http.ResponseWriter, r *http.Request) {
	var request core.SyncResultsRequest
	if err := decodeJSON(r, &request); err != nil {
		writeError(w, err)
		return
	}

	response, err := h.service.SyncResults(r.Context(), request)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, response)
}

func (h *handler) downloadFile(w http.ResponseWriter, r *http.Request) {
	fileID := r.PathValue("file_id")
	userID, err := requiredUserID(r)
	if err != nil {
		h.logFileAPIWarn("文件下载请求校验失败", "file_id", fileID, "error", err)
		writeError(w, err)
		return
	}
	h.logFileAPIInfo("文件下载开始", "user_id", userID, "file_id", fileID)
	metadata, err := h.service.GetFile(r.Context(), fileID)
	if err != nil {
		h.logFileAPIWarn("文件下载元信息查询失败", "user_id", userID, "file_id", fileID, "error", err)
		writeError(w, err)
		return
	}
	if err := validateFileUser(metadata, userID); err != nil {
		h.logFileAPIWarn("文件下载归属校验失败", "user_id", userID, "file_id", fileID, "error", err)
		writeError(w, err)
		return
	}
	response, err := h.service.DownloadFileStream(
		r.Context(),
		fileID,
		r.URL.Query().Get("disposition"),
	)
	if err != nil {
		h.logFileAPIWarn("文件下载流获取失败", "user_id", userID, "file_id", fileID, "error", err)
		writeError(w, err)
		return
	}
	defer response.Body.Close()

	if response.ContentType != "" {
		w.Header().Set("Content-Type", response.ContentType)
	}
	if response.ContentDisposition != "" {
		w.Header().Set("Content-Disposition", response.ContentDisposition)
	}
	if response.DownloadSource != "" {
		w.Header().Set("X-IronAgent-Download-Source", response.DownloadSource)
	}
	if response.StorageBackend != "" {
		w.Header().Set("X-IronAgent-Storage-Backend", response.StorageBackend)
	}
	w.WriteHeader(http.StatusOK)
	written, _ := io.Copy(w, response.Body)
	metrics.RecordFileTransfer("direct_download", written)
	h.logFileAPIInfo(
		"文件下载成功",
		"user_id", userID,
		"file_id", fileID,
		"mime_type", response.ContentType,
		"size", written,
	)
}

func (h *handler) logFileAPIInfo(message string, args ...any) {
	if h.logger == nil {
		return
	}
	h.logger.Info(message, args...)
}

func (h *handler) logFileAPIWarn(message string, args ...any) {
	if h.logger == nil {
		return
	}
	h.logger.Warn(message, args...)
}

func (h *handler) logChatAPIInfo(message string, args ...any) {
	if h.logger == nil {
		return
	}
	h.logger.Info(message, args...)
}

func (h *handler) logChatAPIWarn(message string, args ...any) {
	if h.logger == nil {
		return
	}
	h.logger.Warn(message, args...)
}

func requiredUserID(r *http.Request) (string, error) {
	userID := strings.TrimSpace(r.URL.Query().Get("user_id"))
	if userID == "" {
		return "", &core.ValidationError{Message: "缺少用户标识，请传入 user_id"}
	}
	return userID, nil
}

func validateFileUser(raw southbound.RawJSON, userID string) error {
	owner := firstNonEmptyString(
		stringValue(raw["owner_user_id"]),
		stringValue(raw["user_id"]),
	)
	if owner == "" {
		return &forbiddenError{message: "文件缺少用户归属"}
	}
	if owner != "" && owner != userID {
		return &forbiddenError{message: "文件不属于该用户"}
	}
	return nil
}

func validateAssistantFile(raw southbound.RawJSON, assistantID string) error {
	owner := strings.TrimSpace(stringValue(raw["assistant_id"]))
	if owner == "" {
		return &forbiddenError{message: "文件缺少助手归属"}
	}
	if owner != assistantID {
		return &forbiddenError{message: "文件不属于该助手"}
	}
	return nil
}

func validateWorkspaceFileUploadBucket(bucket string) error {
	switch strings.TrimSpace(bucket) {
	case "", "temp", "persistent":
		return nil
	case "skills", "plugins":
		return &forbiddenError{message: "skills/plugins are read-only in workspace file views"}
	default:
		return &core.ValidationError{Message: "target_bucket must be temp or persistent"}
	}
}

func validateWorkspaceFileViewMutation(raw southbound.RawJSON, _ string) error {
	switch strings.TrimSpace(stringValue(raw["bucket"])) {
	case "skills", "plugins":
		return &forbiddenError{message: "skills/plugins are read-only in workspace file views"}
	default:
		return nil
	}
}

type countingReader struct {
	reader io.Reader
	n      int64
}

func (r *countingReader) Read(p []byte) (int, error) {
	n, err := r.reader.Read(p)
	r.n += int64(n)
	return n, err
}

func (h *handler) createUploadURL(w http.ResponseWriter, r *http.Request) {
	if h.presigns == nil {
		writeJSON(w, http.StatusNotImplemented, map[string]any{"error": map[string]any{
			"code":    "not_implemented",
			"message": "presigned URLs are not configured",
		}})
		return
	}
	var request presign.UploadRequest
	if err := decodeJSON(r, &request); err != nil {
		writeError(w, err)
		return
	}
	response, err := h.presigns.CreateUploadURL(h.requestBaseURL(r), request)
	if err != nil {
		writeError(w, &core.ValidationError{Message: err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, response)
}

func (h *handler) createDownloadURL(w http.ResponseWriter, r *http.Request) {
	if h.presigns == nil {
		writeJSON(w, http.StatusNotImplemented, map[string]any{"error": map[string]any{
			"code":    "not_implemented",
			"message": "presigned URLs are not configured",
		}})
		return
	}
	var request presign.DownloadRequest
	if err := decodeJSON(r, &request); err != nil {
		writeError(w, err)
		return
	}
	response, err := h.presigns.CreateDownloadURL(h.requestBaseURL(r), request)
	if err != nil {
		writeError(w, &core.ValidationError{Message: err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, response)
}

func (h *handler) uploadPresignedFile(w http.ResponseWriter, r *http.Request) {
	if h.presigns == nil {
		http.NotFound(w, r)
		return
	}
	request, err := h.presigns.ResolveUpload(r.PathValue("token"))
	if err != nil {
		writeError(w, &core.ValidationError{Message: err.Error()})
		return
	}
	defer r.Body.Close()
	if r.ContentLength > h.maxUploadBytes {
		writeError(w, &payloadTooLargeError{message: "上传文件过大"})
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, h.maxUploadBytes)
	contentType := strings.TrimSpace(request.ContentType)
	if contentType == "" {
		contentType = strings.TrimSpace(r.Header.Get("Content-Type"))
	}
	counter := &countingReader{reader: r.Body}
	response, err := h.service.UploadFile(r.Context(), core.FileUploadRequest{
		AssistantID:    request.AssistantID,
		UserID:         request.UserID,
		TargetBucket:   request.TargetBucket,
		Overwrite:      request.Overwrite,
		SourceType:     request.SourceType,
		SourceRef:      request.SourceRef,
		UploaderUserID: request.UploaderUserID,
		Filename:       request.Filename,
		ContentType:    contentType,
		ContentReader:  counter,
		ContentLength:  r.ContentLength,
	})
	if err != nil {
		var maxErr *http.MaxBytesError
		if errors.As(err, &maxErr) {
			writeError(w, &payloadTooLargeError{message: "上传文件过大"})
			return
		}
		writeError(w, err)
		return
	}
	uploadedBytes := r.ContentLength
	if uploadedBytes <= 0 {
		uploadedBytes = counter.n
	}
	if uploadedBytes > 0 {
		metrics.RecordFileTransfer("presigned_upload", uploadedBytes)
	}
	writeJSON(w, http.StatusOK, normalizeFileResponse(response, core.FileUploadRequest{
		Filename:      request.Filename,
		ContentType:   contentType,
		ContentLength: uploadedBytes,
	}))
}

func (h *handler) downloadPresignedFile(w http.ResponseWriter, r *http.Request) {
	if h.presigns == nil {
		http.NotFound(w, r)
		return
	}
	request, err := h.presigns.ResolveDownload(r.PathValue("token"))
	if err != nil {
		writeError(w, &core.ValidationError{Message: err.Error()})
		return
	}
	response, err := h.service.DownloadFileStream(r.Context(), request.FileID, request.Disposition)
	if err != nil {
		writeError(w, err)
		return
	}
	defer response.Body.Close()
	if response.ContentType != "" {
		w.Header().Set("Content-Type", response.ContentType)
	}
	if response.ContentDisposition != "" {
		w.Header().Set("Content-Disposition", response.ContentDisposition)
	}
	w.WriteHeader(http.StatusOK)
	written, _ := io.Copy(w, response.Body)
	metrics.RecordFileTransfer("presigned_download", written)
}

func (h *handler) deleteFile(w http.ResponseWriter, r *http.Request) {
	var request core.FileDeleteRequest
	if err := decodeJSON(r, &request); err != nil {
		writeError(w, err)
		return
	}
	response, err := h.service.DeleteFile(r.Context(), r.PathValue("file_id"), request)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, response)
}

func (h *handler) ackWebhook(w http.ResponseWriter, r *http.Request) {
	var ack webhook.ACK
	if err := decodeJSON(r, &ack); err != nil {
		writeError(w, err)
		return
	}
	stored, ok := h.webhooks.Acknowledge(ack)
	if !ok {
		writeError(w, &core.ValidationError{Message: "unknown event_id"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ack": stored})
}

func (h *handler) listWebhookDeadLetters(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"dead_letters": h.webhooks.DeadLetters()})
}

func (h *handler) replayWebhookDeadLetter(w http.ResponseWriter, r *http.Request) {
	record, ok, err := h.webhooks.ReplayDeadLetter(r.Context(), r.PathValue("event_id"))
	if err != nil {
		writeError(w, err)
		return
	}
	if !ok {
		writeError(w, &core.ValidationError{Message: "unknown dead-letter event_id"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"event": record.Event, "status": record.Status})
}

func (h *handler) ensureChatCreatedEvents(
	r *http.Request,
	request core.ChatRequest,
	task core.TaskView,
	response core.ChatResponse,
) error {
	if h.webhooks == nil || strings.TrimSpace(request.CallbackURL) == "" {
		return nil
	}
	if _, ok := h.webhooks.Subscription(task.ID); ok {
		return nil
	}
	eventCtx := context.Background()
	subscription := webhook.Subscription{
		CallbackURL:    request.CallbackURL,
		RequestID:      request.RequestID,
		ConversationID: request.ConversationID,
		UserID:         request.UserID,
		EmployeeNo:     request.EmployeeNo,
	}
	if err := h.dispatchTaskEvent(
		eventCtx,
		subscription,
		"task.accepted",
		task,
		"processing",
		"queued",
		response.FallbackQueryURL,
		"",
	); err != nil {
		return err
	}
	h.webhooks.Register(task.ID, subscription)
	switch response.Status {
	case "pending_approval":
		h.logWebhookDispatchError(
			task.ID,
			"task.approval_required",
			h.dispatchTaskEvent(
				eventCtx,
				subscription,
				"task.approval_required",
				task,
				response.Status,
				response.Stage,
				response.FallbackQueryURL,
				"",
			),
		)
	case "processing":
		if response.Stage != "queued" {
			eventType := "task.progress"
			taskResponse := core.NewTaskResponse(task)
			if !h.suppressEmptyTaskProgressCallback(eventType, taskResponse.ReplyText) {
				h.logWebhookDispatchError(
					task.ID,
					eventType,
					h.dispatchTaskEvent(
						eventCtx,
						subscription,
						eventType,
						task,
						response.Status,
						response.Stage,
						response.FallbackQueryURL,
						"",
					),
				)
			}
		}
	}
	return nil
}

func (h *handler) recordRuntime(runtimeView core.RuntimeView) {
	if h.runtimes == nil || strings.TrimSpace(runtimeView.AssistantID) == "" {
		return
	}
	record, ok := h.runtimes.Get(runtimeView.AssistantID)
	if !ok {
		record = runtimepool.RuntimeRecord{AssistantID: runtimeView.AssistantID}
	}
	record.Status = runtimeView.Status
	record.InternalBaseURL = runtimeView.GatewayURL
	record.GatewayURL = runtimeView.GatewayURL
	record.LastError = runtimeView.LastError
	if runtimeViewAvailableForRegistry(runtimeView) {
		record.DrainMode = false
	}
	record.UpdatedAt = time.Now().UTC()
	_, _ = h.runtimes.Heartbeat(record)
}

func runtimeViewAvailableForRegistry(runtimeView core.RuntimeView) bool {
	return runtimeStatusAvailable(runtimeView.Status) && strings.TrimSpace(runtimeView.GatewayURL) != ""
}

func runtimeRecordAvailableForRegistry(record runtimepool.RuntimeRecord) bool {
	return runtimeStatusAvailable(record.Status) &&
		(strings.TrimSpace(record.InternalBaseURL) != "" || strings.TrimSpace(record.GatewayURL) != "")
}

func (h *handler) dispatchStoredTaskEvent(r *http.Request, eventType string, task core.TaskView) {
	h.dispatchStoredTaskEventWithCancelledBy(r, eventType, task, "")
}

func (h *handler) dispatchStoredTaskEventWithCancelledBy(
	r *http.Request,
	eventType string,
	task core.TaskView,
	cancelledBy string,
) {
	if h.webhooks == nil {
		return
	}
	subscription, ok := h.taskCallbackSubscription(r.Context(), task.ID)
	if !ok {
		return
	}
	response := core.NewTaskResponse(task)
	response.FallbackQueryURL = h.fallbackQueryURL(r, task.ID)
	if h.suppressEmptyTaskProgressCallback(eventType, response.ReplyText) {
		return
	}
	h.logWebhookDispatchError(
		task.ID,
		eventType,
		h.dispatchTaskEvent(
			r.Context(),
			subscription,
			eventType,
			task,
			response.Status,
			response.Stage,
			response.FallbackQueryURL,
			cancelledBy,
		),
	)
}

func (h *handler) dispatchQueuedTaskUpdate(ctx context.Context, task core.TaskView) {
	if h.webhooks == nil {
		return
	}
	eventType := webhookEventTypeForTask(task)
	if eventType == "" {
		return
	}
	subscription, ok := h.taskCallbackSubscription(ctx, task.ID)
	if !ok {
		return
	}
	response := core.NewTaskResponse(task)
	if h.suppressEmptyTaskProgressCallback(eventType, response.ReplyText) {
		return
	}
	fallbackQueryURL := h.fallbackTaskURL(task.ID)
	eventCtx := context.WithoutCancel(ctx)
	h.logWebhookDispatchError(
		task.ID,
		eventType,
		h.dispatchTaskEvent(
			eventCtx,
			subscription,
			eventType,
			task,
			response.Status,
			response.Stage,
			fallbackQueryURL,
			"",
		),
	)
}

func (h *handler) dispatchQueuedTaskProgressUpdate(ctx context.Context, update core.QueuedTaskProgressUpdate) error {
	if h.webhooks == nil || strings.TrimSpace(update.TaskID) == "" || strings.TrimSpace(update.ReplyText) == "" {
		return nil
	}
	subscription, ok := h.taskCallbackSubscription(ctx, update.TaskID)
	if !ok {
		return nil
	}
	eventType := firstNonEmptyString(strings.TrimSpace(update.EventType), "task.progress")
	status := firstNonEmptyString(strings.TrimSpace(update.Status), "processing")
	stage := firstNonEmptyString(strings.TrimSpace(update.Stage), "tool_running")
	eventCtx := context.WithoutCancel(ctx)
	_, err := h.webhooks.DispatchAsync(eventCtx, subscription.CallbackURL, webhook.Event{
		EventType:        eventType,
		TaskID:           update.TaskID,
		RequestID:        subscription.RequestID,
		ConversationID:   subscription.ConversationID,
		UserID:           subscription.UserID,
		EmployeeNo:       subscription.EmployeeNo,
		Status:           status,
		Stage:            stage,
		FallbackQueryURL: h.fallbackTaskURL(update.TaskID),
		ReplyText:        update.ReplyText,
		ErrorCode:        strings.TrimSpace(update.ErrorCode),
		ErrorMessage:     strings.TrimSpace(update.ErrorMessage),
		ErrorLocation:    strings.TrimSpace(update.ErrorLocation),
		Reason:           firstNonEmptyString(strings.TrimSpace(update.Reason), strings.TrimSpace(update.ErrorMessage)),
	})
	h.logWebhookDispatchError(update.TaskID, eventType, err)
	return err
}

func (h *handler) suppressEmptyTaskProgressCallback(eventType string, replyText string) bool {
	return h.taskProgressCallbackEnabled &&
		eventType == "task.progress" &&
		strings.TrimSpace(replyText) == ""
}

func (h *handler) taskCallbackSubscription(
	ctx context.Context,
	taskID string,
) (webhook.Subscription, bool) {
	if h.webhooks == nil {
		return webhook.Subscription{}, false
	}
	if subscription, ok := h.webhooks.Subscription(taskID); ok {
		return subscription, true
	}
	provider, ok := h.service.(storedTaskRecordProvider)
	if !ok {
		return webhook.Subscription{}, false
	}
	record, ok, err := provider.StoredTaskRecord(ctx, taskID)
	if err != nil {
		if h.logger != nil {
			h.logger.Warn("failed to recover callback subscription", "task_id", taskID, "error", err)
		}
		return webhook.Subscription{}, false
	}
	if !ok || strings.TrimSpace(record.CallbackURL) == "" {
		return webhook.Subscription{}, false
	}
	subscription := webhook.Subscription{
		CallbackURL:    record.CallbackURL,
		RequestID:      record.RequestID,
		ConversationID: record.ConversationID,
		UserID:         record.UserID,
		EmployeeNo:     record.EmployeeNo,
	}
	h.webhooks.Register(taskID, subscription)
	return subscription, true
}

func webhookEventTypeForTask(task core.TaskView) string {
	switch task.Status {
	case "running":
		return "task.progress"
	case "pending_approval":
		return "task.approval_required"
	case "completed":
		return "task.completed"
	case "failed":
		return "task.failed"
	case "cancelled":
		return "task.cancelled"
	case "rejected":
		return "task.rejected"
	default:
		return ""
	}
}

func (h *handler) dispatchTaskEvent(
	ctx context.Context,
	subscription webhook.Subscription,
	eventType string,
	task core.TaskView,
	status string,
	stage string,
	fallbackQueryURL string,
	cancelledBy string,
) error {
	response := h.newTaskResponseWithFiles(ctx, task)
	approvalID := ""
	approvalReason := ""
	approvalDecision := ""
	action := ""
	resource := ""
	if task.Approval != nil {
		approvalID = task.Approval.ApprovalID
		approvalReason = task.Approval.Reason
		approvalDecision = publicWebhookDecision(task.Approval.Resolution)
		action = task.Approval.ApprovalScope
		resource = task.Approval.Target
	}
	_, err := h.webhooks.DispatchAsync(ctx, subscription.CallbackURL, webhook.Event{
		EventType:        eventType,
		TaskID:           task.ID,
		RequestID:        subscription.RequestID,
		ConversationID:   subscription.ConversationID,
		UserID:           subscription.UserID,
		EmployeeNo:       subscription.EmployeeNo,
		Status:           status,
		Stage:            stage,
		FallbackQueryURL: fallbackQueryURL,
		Message:          task.Instruction,
		ApprovalID:       approvalID,
		RiskLevel:        approvalReason,
		Action:           action,
		Resource:         resource,
		Decision:         approvalDecision,
		ReplyText:        response.ReplyText,
		Files:            webhookFiles(response.Files),
		ErrorCode:        response.ErrorCode,
		ErrorMessage:     response.ErrorMessage,
		ErrorLocation:    response.ErrorLocation,
		Reason:           firstNonEmptyString(response.Reason, response.ErrorMessage),
		CancelledBy:      strings.TrimSpace(cancelledBy),
	})
	return err
}

func (h *handler) logWebhookDispatchError(taskID string, eventType string, err error) {
	if err != nil && h.logger != nil {
		h.logger.Warn("webhook enqueue failed", "task_id", taskID, "event_type", eventType, "error", err)
	}
}

func publicWebhookDecision(resolution string) string {
	switch resolution {
	case "allow":
		return "approve"
	case "deny":
		return "deny"
	default:
		return resolution
	}
}

func webhookFiles(files []core.TaskFileResponse) []webhook.FileRef {
	if len(files) == 0 {
		return nil
	}
	out := make([]webhook.FileRef, 0, len(files))
	for _, file := range files {
		if strings.TrimSpace(file.FileID) == "" {
			continue
		}
		out = append(out, webhook.FileRef{
			FileID:      file.FileID,
			Filename:    file.Filename,
			Size:        file.Size,
			MimeType:    file.MimeType,
			DownloadURL: file.DownloadURL,
		})
	}
	return out
}

func (h *handler) fallbackQueryURL(r *http.Request, taskID string) string {
	return h.requestBaseURL(r) + "/api/v1/tasks/" + taskID
}

func (h *handler) fallbackTaskURL(taskID string) string {
	if h.publicBaseURL == "" {
		return "/api/v1/tasks/" + taskID
	}
	return h.publicBaseURL + "/api/v1/tasks/" + taskID
}

func (h *handler) requestBaseURL(r *http.Request) string {
	if h.publicBaseURL != "" {
		return h.publicBaseURL
	}
	return ""
}

func (h *handler) validateCallbackURL(ctx context.Context, raw string) error {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return nil
	}
	parsed, err := url.Parse(trimmed)
	if err != nil || parsed.Hostname() == "" {
		return &core.ValidationError{Message: "callback_url must be an absolute URL"}
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return &core.ValidationError{Message: "callback_url scheme must be http or https"}
	}
	host := strings.ToLower(parsed.Hostname())
	hostPort := strings.ToLower(parsed.Host)
	allowed := false
	for _, candidate := range h.callbackHosts {
		if candidate == host || candidate == hostPort {
			allowed = true
			break
		}
	}
	if !allowed {
		return &core.ValidationError{Message: "callback_url host is not allowed"}
	}
	if h.allowPrivateCB {
		return nil
	}
	if isPrivateCallbackHost(host) {
		return &core.ValidationError{Message: "callback_url host must not be private or loopback"}
	}
	if err := h.rejectPrivateCallbackDNS(ctx, host); err != nil {
		return err
	}
	return nil
}

func (h *handler) validateChatCallbackURL(ctx context.Context, publicRequest bool, raw string) error {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		if h.callbackRequired {
			return &callbackUnavailableError{message: "callback 通道暂不可用，请稍后重试"}
		}
		return nil
	}
	if err := h.validateCallbackURL(ctx, trimmed); err != nil {
		if publicRequest {
			return &callbackUnavailableError{message: "callback 通道暂不可用，请稍后重试"}
		}
		return err
	}
	if h.callbackRequired && h.webhooks == nil {
		return &callbackUnavailableError{message: "callback 通道暂不可用，请稍后重试"}
	}
	return nil
}

func normalizeAllowedHosts(values []string) []string {
	hosts := make([]string, 0, len(values))
	for _, value := range values {
		trimmed := strings.ToLower(strings.TrimSpace(value))
		if trimmed == "" {
			continue
		}
		if parsed, err := url.Parse(trimmed); err == nil && parsed.Host != "" {
			hosts = append(hosts, strings.ToLower(parsed.Host), strings.ToLower(parsed.Hostname()))
			continue
		}
		hosts = append(hosts, trimmed)
	}
	return hosts
}

func isPrivateCallbackHost(host string) bool {
	host = strings.Trim(strings.ToLower(strings.TrimSpace(host)), "[]")
	if host == "localhost" || strings.HasSuffix(host, ".localhost") {
		return true
	}
	ip := net.ParseIP(host)
	if ip == nil {
		return false
	}
	return ip.IsLoopback() ||
		ip.IsPrivate() ||
		ip.IsLinkLocalUnicast() ||
		ip.IsLinkLocalMulticast() ||
		ip.IsUnspecified() ||
		ip.IsMulticast()
}

func (h *handler) rejectPrivateCallbackDNS(ctx context.Context, host string) error {
	if h.callbackLookup == nil || net.ParseIP(strings.Trim(host, "[]")) != nil {
		return nil
	}
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	ips, err := h.callbackLookup(ctx, host)
	if err != nil {
		return &core.ValidationError{Message: "callback_url host DNS lookup failed"}
	}
	for _, ip := range ips {
		if isPrivateCallbackHost(ip.String()) {
			return &core.ValidationError{Message: "callback_url host resolves to private or loopback address"}
		}
	}
	return nil
}

func defaultCallbackIPLookup(ctx context.Context, host string) ([]net.IP, error) {
	addrs, err := net.DefaultResolver.LookupIPAddr(ctx, host)
	if err != nil {
		return nil, err
	}
	ips := make([]net.IP, 0, len(addrs))
	for _, addr := range addrs {
		if addr.IP != nil {
			ips = append(ips, addr.IP)
		}
	}
	return ips, nil
}

func decodeJSON(r *http.Request, target any) error {
	defer r.Body.Close()
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return &core.ValidationError{Message: "invalid JSON body: " + err.Error()}
	}
	return nil
}

func decodeOptionalEmptyJSON(r *http.Request) error {
	defer r.Body.Close()
	if r.Body == nil {
		return nil
	}
	body, err := io.ReadAll(r.Body)
	if err != nil {
		return &core.ValidationError{Message: "invalid JSON body: " + err.Error()}
	}
	if len(bytes.TrimSpace(body)) == 0 {
		return nil
	}
	var payload struct{}
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&payload); err != nil {
		return &core.ValidationError{Message: "invalid JSON body: " + err.Error()}
	}
	return nil
}

type payloadTooLargeError struct {
	message string
}

func (e *payloadTooLargeError) Error() string {
	return e.message
}

type unprocessableEntityError struct {
	message string
}

func (e *unprocessableEntityError) Error() string {
	return e.message
}

type forbiddenError struct {
	message string
}

func (e *forbiddenError) Error() string {
	return e.message
}

type callbackUnavailableError struct {
	message string
}

func (e *callbackUnavailableError) Error() string {
	return e.message
}

func (h *handler) parseFileUploadRequest(w http.ResponseWriter, r *http.Request) (core.FileUploadRequest, func(), error) {
	if strings.TrimSpace(r.Header.Get("X-API-Key")) != "" {
		request, cleanup, err := h.parseFileUploadRequestAllowingEmptyUser(w, r)
		if err != nil {
			return core.FileUploadRequest{}, cleanup, err
		}
		if strings.TrimSpace(request.UserID) == "" && strings.TrimSpace(request.AssistantID) == "" {
			if cleanup != nil {
				cleanup()
			}
			return core.FileUploadRequest{}, nil, &core.ValidationError{Message: "缺少用户标识，请传入 user_id"}
		}
		return request, cleanup, nil
	}

	r.Body = http.MaxBytesReader(w, r.Body, h.maxUploadBytes+(1<<20))
	if err := r.ParseMultipartForm(8 << 20); err != nil {
		var maxErr *http.MaxBytesError
		if errors.As(err, &maxErr) {
			return core.FileUploadRequest{}, nil, &payloadTooLargeError{message: "上传文件过大"}
		}
		return core.FileUploadRequest{}, nil, &core.ValidationError{Message: "multipart 表单无效: " + err.Error()}
	}
	removeForm := func() {
		if r.MultipartForm != nil {
			_ = r.MultipartForm.RemoveAll()
		}
	}
	file, fileHeader, err := r.FormFile("file")
	if err != nil {
		removeForm()
		return core.FileUploadRequest{}, nil, &core.ValidationError{Message: "缺少上传文件，请传入 file"}
	}
	cleanup := func() {
		_ = file.Close()
		removeForm()
	}
	if fileHeader.Size > h.maxUploadBytes {
		cleanup()
		return core.FileUploadRequest{}, nil, &payloadTooLargeError{message: "上传文件过大"}
	}
	if fileHeader.Size == 0 {
		cleanup()
		return core.FileUploadRequest{}, nil, &core.ValidationError{Message: "上传文件不能为空"}
	}
	if err := validatePublicUploadForm(r.MultipartForm); err != nil {
		cleanup()
		return core.FileUploadRequest{}, nil, err
	}

	request := core.FileUploadRequest{
		UserID:         formValue(r.MultipartForm, "user_id"),
		TargetBucket:   "temp",
		SourceType:     "northbound-upload",
		UploaderUserID: formValue(r.MultipartForm, "user_id"),
		Filename:       fileHeader.Filename,
		ContentType:    detectUploadContentType(fileHeader),
		ContentReader:  file,
		ContentLength:  fileHeader.Size,
	}
	if strings.TrimSpace(request.UserID) == "" {
		cleanup()
		return core.FileUploadRequest{}, nil, &core.ValidationError{Message: "缺少用户标识，请传入 user_id"}
	}
	return request, cleanup, nil
}

func validatePublicUploadForm(form *multipart.Form) error {
	if form == nil {
		return &core.ValidationError{Message: "multipart form is required"}
	}
	for key := range form.Value {
		if key != "user_id" {
			return &core.ValidationError{Message: "不支持的表单字段: " + key}
		}
	}
	for key := range form.File {
		if key != "file" {
			return &core.ValidationError{Message: "不支持的文件字段: " + key}
		}
	}
	return nil
}

func (h *handler) parseAssistantScopedFileUploadRequest(
	w http.ResponseWriter,
	r *http.Request,
	assistantID string,
) (core.FileUploadRequest, func(), error) {
	request, cleanup, err := h.parseFileUploadRequestAllowingEmptyUser(w, r)
	if err != nil {
		return core.FileUploadRequest{}, cleanup, err
	}
	request.AssistantID = assistantID
	if err := validateWorkspaceFileUploadBucket(request.TargetBucket); err != nil {
		if cleanup != nil {
			cleanup()
		}
		return core.FileUploadRequest{}, nil, err
	}
	return request, cleanup, nil
}

func (h *handler) parseFileUploadRequestAllowingEmptyUser(
	w http.ResponseWriter,
	r *http.Request,
) (core.FileUploadRequest, func(), error) {
	r.Body = http.MaxBytesReader(w, r.Body, h.maxUploadBytes+(1<<20))
	if err := r.ParseMultipartForm(8 << 20); err != nil {
		var maxErr *http.MaxBytesError
		if errors.As(err, &maxErr) {
			return core.FileUploadRequest{}, nil, &payloadTooLargeError{message: "上传文件过大"}
		}
		return core.FileUploadRequest{}, nil, &core.ValidationError{Message: "multipart 表单无效: " + err.Error()}
	}
	removeForm := func() {
		if r.MultipartForm != nil {
			_ = r.MultipartForm.RemoveAll()
		}
	}
	file, fileHeader, err := r.FormFile("file")
	if err != nil {
		removeForm()
		return core.FileUploadRequest{}, nil, &core.ValidationError{Message: "缺少上传文件，请传入 file"}
	}
	cleanup := func() {
		_ = file.Close()
		removeForm()
	}
	if fileHeader.Size > h.maxUploadBytes {
		cleanup()
		return core.FileUploadRequest{}, nil, &payloadTooLargeError{message: "上传文件过大"}
	}

	overwrite, err := parseBoolValue(r.MultipartForm.Value["overwrite"])
	if err != nil {
		cleanup()
		return core.FileUploadRequest{}, nil, err
	}

	return core.FileUploadRequest{
		AssistantID:    formValue(r.MultipartForm, "assistant_id"),
		UserID:         formValue(r.MultipartForm, "user_id"),
		TargetBucket:   formValue(r.MultipartForm, "target_bucket"),
		Overwrite:      overwrite,
		SourceType:     formValue(r.MultipartForm, "source_type"),
		SourceRef:      formValue(r.MultipartForm, "source_ref"),
		UploaderUserID: formValue(r.MultipartForm, "uploader_user_id"),
		Filename:       fileHeader.Filename,
		ContentType:    detectUploadContentType(fileHeader),
		ContentReader:  file,
		ContentLength:  fileHeader.Size,
	}, cleanup, nil
}

func formValue(form *multipart.Form, key string) string {
	if form == nil || form.Value == nil {
		return ""
	}
	values := form.Value[key]
	if len(values) == 0 {
		return ""
	}
	return values[0]
}

func parseBoolValue(values []string) (bool, error) {
	if len(values) == 0 {
		return false, nil
	}
	value := values[0]
	if value == "" {
		return false, nil
	}
	parsed, err := strconv.ParseBool(value)
	if err != nil {
		return false, &core.ValidationError{Message: "overwrite 参数不是合法布尔值"}
	}
	return parsed, nil
}

func detectUploadContentType(fileHeader *multipart.FileHeader) string {
	if fileHeader == nil {
		return ""
	}
	return fileHeader.Header.Get("Content-Type")
}

type fileMetadataResponse struct {
	FileID       string     `json:"file_id"`
	AssistantID  string     `json:"assistant_id,omitempty"`
	UserID       string     `json:"-"`
	Bucket       string     `json:"bucket,omitempty"`
	Filename     string     `json:"filename,omitempty"`
	RelativePath string     `json:"relative_path,omitempty"`
	Direction    string     `json:"direction,omitempty"`
	FileRole     string     `json:"file_role,omitempty"`
	SourceType   string     `json:"source_type,omitempty"`
	SourceRef    string     `json:"source_ref,omitempty"`
	Size         int64      `json:"size"`
	MimeType     string     `json:"mime_type,omitempty"`
	CreatedAt    *time.Time `json:"created_at,omitempty"`
	Status       string     `json:"status,omitempty"`
	DownloadURL  string     `json:"download_url,omitempty"`
}

type publicFileMetadataResponse struct {
	FileID    string     `json:"file_id"`
	Filename  string     `json:"filename"`
	Size      int64      `json:"size"`
	MimeType  string     `json:"mime_type"`
	CreatedAt *time.Time `json:"created_at,omitempty"`
}

func normalizeFileListResponse(raw southbound.RawJSON) map[string]any {
	files := []fileMetadataResponse{}
	if values, ok := raw["files"].([]southbound.RawJSON); ok {
		for _, value := range values {
			files = append(files, normalizeFileMetadata(value, core.FileUploadRequest{}))
		}
	} else if values, ok := raw["files"].([]any); ok {
		for _, value := range values {
			if item, ok := value.(map[string]any); ok {
				files = append(files, normalizeFileMetadata(southbound.RawJSON(item), core.FileUploadRequest{}))
			}
		}
	}
	total := int64(len(files))
	if rawTotal, ok := numberAsInt64(raw["total"]); ok {
		total = rawTotal
	}
	return map[string]any{
		"files": files,
		"total": total,
	}
}

func filterFileListBuckets(payload map[string]any, allowed map[string]struct{}) map[string]any {
	files, ok := payload["files"].([]fileMetadataResponse)
	if !ok {
		return payload
	}
	filtered := make([]fileMetadataResponse, 0, len(files))
	for _, file := range files {
		if _, ok := allowed[file.Bucket]; ok {
			filtered = append(filtered, file)
		}
	}
	payload["files"] = filtered
	payload["total"] = int64(len(filtered))
	return payload
}

type publicTaskResponse = core.PublicTaskResponse

func (h *handler) newPublicTaskResponse(ctx context.Context, task core.TaskView) publicTaskResponse {
	response := h.newTaskResponseWithFiles(ctx, task)
	return core.NewPublicTaskPayload(core.PublicTaskPayloadInput{
		TaskID:        response.TaskID,
		Status:        response.Status,
		Stage:         response.Stage,
		ReplyText:     response.ReplyText,
		Files:         core.PublicTaskFilesFromTask(response.Files),
		ErrorCode:     response.ErrorCode,
		ErrorMessage:  response.ErrorMessage,
		ErrorLocation: response.ErrorLocation,
		ErrorReason:   firstNonEmptyString(response.Reason, response.ErrorMessage),
	})
}

func (h *handler) newTaskResponseWithFiles(ctx context.Context, task core.TaskView) core.TaskResponse {
	response := core.NewTaskResponse(task)
	response.Files = h.enrichTaskFileResponses(ctx, response.Files, task)
	return response
}

func (h *handler) enrichTaskFileResponses(ctx context.Context, files []core.TaskFileResponse, task core.TaskView) []core.TaskFileResponse {
	if len(files) == 0 {
		return nil
	}
	expectedUserID := strings.TrimSpace(task.CreatedBy)
	out := make([]core.TaskFileResponse, 0, len(files))
	for _, file := range files {
		fileID := strings.TrimSpace(file.FileID)
		if fileID == "" {
			continue
		}
		trustedTaskResultFile := taskHasResultFileID(task, fileID)
		file.FileID = fileID
		metadata, err := h.service.GetFile(ctx, fileID)
		if err != nil {
			if h.logger != nil {
				h.logger.Warn("结果文件元信息补齐失败", "file_id", fileID, "error", err)
			}
			continue
		}
		if fileMetadataMissing(metadata) {
			if h.logger != nil {
				h.logger.Warn("结果文件元信息不存在", "file_id", fileID)
			}
			continue
		}
		normalized := normalizeFileMetadata(metadata, core.FileUploadRequest{})
		trustedSparseTaskResultFile := trustedTaskResultFile &&
			taskResultFileMetadataSparse(normalized) &&
			taskResultFileScoped(normalized, task)
		if strings.TrimSpace(normalized.UserID) == "" && trustedSparseTaskResultFile {
			normalized.UserID = expectedUserID
		}
		if strings.TrimSpace(normalized.UserID) == "" {
			if h.logger != nil {
				h.logger.Warn("结果文件缺少用户归属", "file_id", fileID)
			}
			continue
		}
		if expectedUserID != "" && normalized.UserID != expectedUserID {
			if h.logger != nil {
				h.logger.Warn("结果文件用户归属不匹配", "file_id", fileID, "user_id", normalized.UserID, "expected_user_id", expectedUserID)
			}
			continue
		}
		metadataFileID := strings.TrimSpace(normalized.FileID)
		if metadataFileID != "" && metadataFileID != fileID {
			if h.logger != nil {
				h.logger.Warn("结果文件元信息不匹配", "file_id", fileID, "metadata_file_id", metadataFileID)
			}
			continue
		}
		if !taskResultFileMetadataMatches(normalized, task) && !trustedSparseTaskResultFile {
			if h.logger != nil {
				h.logger.Warn(
					"结果文件不属于当前任务",
					"task_id", task.ID,
					"file_id", fileID,
					"assistant_id", normalized.AssistantID,
					"expected_assistant_id", task.TargetAssistantID,
					"relative_path", normalized.RelativePath,
					"source_ref", normalized.SourceRef,
					"direction", normalized.Direction,
					"file_role", normalized.FileRole,
				)
			}
			continue
		}
		if strings.TrimSpace(file.Filename) == "" {
			file.Filename = normalized.Filename
		}
		if file.Size <= 0 {
			file.Size = normalized.Size
		}
		if strings.TrimSpace(file.MimeType) == "" {
			file.MimeType = normalized.MimeType
		}
		if strings.TrimSpace(file.DownloadURL) == "" {
			file.DownloadURL = normalized.DownloadURL
		}
		file.DownloadURL = appendUserIDToDownloadURL(file.DownloadURL, normalized.UserID)
		out = append(out, file)
	}
	return out
}

func taskHasResultFileID(task core.TaskView, fileID string) bool {
	fileID = strings.TrimSpace(fileID)
	if fileID == "" {
		return false
	}
	response := core.NewTaskResponse(task)
	for _, file := range response.Files {
		if strings.TrimSpace(file.FileID) == fileID {
			return true
		}
	}
	return false
}

func taskResultFileMetadataSparse(file fileMetadataResponse) bool {
	return strings.TrimSpace(file.FileRole) == "" &&
		strings.TrimSpace(file.Direction) == "" &&
		strings.TrimSpace(file.SourceType) == "" &&
		strings.TrimSpace(file.SourceRef) == "" &&
		strings.TrimSpace(file.RelativePath) == ""
}

func taskResultFileMetadataMatches(file fileMetadataResponse, task core.TaskView) bool {
	if strings.TrimSpace(file.FileRole) != "result" {
		return false
	}
	if strings.TrimSpace(file.Direction) != "outbound" {
		return false
	}
	if sourceType := strings.TrimSpace(file.SourceType); sourceType != "" && sourceType != "assistant_result" && sourceType != "assistant_output" {
		return false
	}
	if expectedAssistantID := strings.TrimSpace(task.TargetAssistantID); expectedAssistantID != "" {
		if assistantID := strings.TrimSpace(file.AssistantID); assistantID != "" && assistantID != expectedAssistantID {
			return false
		}
	}
	return taskResultFileScoped(file, task)
}

func taskResultFileScoped(file fileMetadataResponse, task core.TaskView) bool {
	sourceRef := strings.TrimSpace(file.SourceRef)
	sourceRefMatches := false
	if sourceRef != "" {
		for _, value := range taskResultSourceRefs(task) {
			if sourceRef == value {
				sourceRefMatches = true
				break
			}
		}
		if !sourceRefMatches {
			return false
		}
	}

	pathPresent := false
	pathMatches := false
	for _, prefix := range taskResultPathPrefixes(task) {
		for _, value := range []string{file.RelativePath, file.Filename} {
			if !taskResultPathCandidatePresent(value) {
				continue
			}
			pathPresent = true
			if taskResultPathMatchesPrefix(value, prefix) {
				pathMatches = true
				break
			}
		}
		if pathMatches {
			break
		}
	}
	if pathPresent && !pathMatches {
		return false
	}
	return sourceRefMatches || pathMatches
}

func taskResultSourceRefs(task core.TaskView) []string {
	values := []string{strings.TrimSpace(task.ID)}
	if task.Metadata != nil {
		values = append(values, stringMapValue(task.Metadata, "runtime_result_task_id"))
	}
	return compactUniqueStrings(values)
}

func taskResultPathPrefixes(task core.TaskView) []string {
	values := []string{runtimeResultPathPrefixForTaskID(task.ID)}
	if task.Metadata != nil {
		values = append(values, stringMapValue(task.Metadata, "runtime_result_path_prefix"))
	}
	return compactUniqueStrings(values)
}

func runtimeResultPathPrefixForTaskID(taskID string) string {
	normalized := sanitizeTaskPathComponent(taskID, 64)
	if normalized == "" || normalized == "northbound" {
		return ""
	}
	return "go-gateway-results/" + normalized + "/"
}

func taskResultPathMatchesPrefix(value string, prefix string) bool {
	value = normalizeTaskResultRelativePath(value)
	prefix = strings.TrimSuffix(normalizeTaskResultRelativePath(prefix), "/")
	if value == "" || prefix == "" {
		return false
	}
	if taskResultPathHasPrefix(value, prefix) {
		return true
	}
	for _, bucketPrefix := range []string{"temp/", "persistent/"} {
		if strings.HasPrefix(value, bucketPrefix) {
			if taskResultPathHasPrefix(strings.TrimPrefix(value, bucketPrefix), prefix) {
				return true
			}
			return false
		}
	}
	if strings.HasPrefix(value, "workspace/") {
		workspacePath := strings.TrimPrefix(value, "workspace/")
		for _, bucketPrefix := range []string{"temp/", "persistent/"} {
			if strings.HasPrefix(workspacePath, bucketPrefix) {
				return taskResultPathHasPrefix(strings.TrimPrefix(workspacePath, bucketPrefix), prefix)
			}
		}
		if taskResultPathHasPrefix(workspacePath, prefix) {
			return true
		}
	}
	return false
}

func taskResultPathCandidatePresent(value string) bool {
	value = normalizeTaskResultRelativePath(value)
	if value == "" {
		return false
	}
	return strings.Contains(value, "/") || strings.HasPrefix(value, "go-gateway-results")
}

func taskResultPathHasPrefix(value string, prefix string) bool {
	return value == prefix || strings.HasPrefix(value, prefix+"/")
}

func normalizeTaskResultRelativePath(value string) string {
	value = strings.TrimSpace(strings.ReplaceAll(value, "\\", "/"))
	value = strings.Trim(value, "/")
	if strings.HasPrefix(value, "workspace/") {
		value = strings.TrimPrefix(value, "workspace/")
	}
	if value == "." || strings.Contains(value, "../") || strings.HasPrefix(value, "..") {
		return ""
	}
	return value
}

func sanitizeTaskPathComponent(value string, maxLength int) string {
	value = strings.TrimSpace(value)
	if maxLength <= 0 {
		maxLength = 64
	}
	var builder strings.Builder
	lastDash := false
	for _, r := range strings.ToLower(value) {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') {
			builder.WriteRune(r)
			lastDash = false
			continue
		}
		if !lastDash {
			builder.WriteByte('-')
			lastDash = true
		}
	}
	normalized := strings.Trim(builder.String(), "-")
	if normalized == "" {
		return "northbound"
	}
	if len(normalized) > maxLength {
		normalized = strings.Trim(normalized[:maxLength], "-")
	}
	if normalized == "" {
		return "northbound"
	}
	return normalized
}

func compactUniqueStrings(values []string) []string {
	out := make([]string, 0, len(values))
	seen := map[string]struct{}{}
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value == "" {
			continue
		}
		if _, ok := seen[value]; ok {
			continue
		}
		seen[value] = struct{}{}
		out = append(out, value)
	}
	return out
}

func stringMapValue(values map[string]any, key string) string {
	if len(values) == 0 {
		return ""
	}
	return stringValue(values[key])
}

func appendUserIDToDownloadURL(downloadURL string, userID string) string {
	downloadURL = strings.TrimSpace(downloadURL)
	userID = strings.TrimSpace(userID)
	if downloadURL == "" || userID == "" {
		return downloadURL
	}
	parsed, err := url.Parse(downloadURL)
	if err != nil {
		return downloadURL
	}
	query := parsed.Query()
	query.Set("user_id", userID)
	parsed.RawQuery = query.Encode()
	return parsed.String()
}

func normalizeFileResponse(raw southbound.RawJSON, request core.FileUploadRequest) fileMetadataResponse {
	return normalizeFileMetadata(raw, request)
}

func normalizePublicFileResponse(raw southbound.RawJSON, request core.FileUploadRequest) publicFileMetadataResponse {
	metadata := normalizeFileMetadata(raw, request)
	return publicFileMetadataResponse{
		FileID:    metadata.FileID,
		Filename:  metadata.Filename,
		Size:      metadata.Size,
		MimeType:  metadata.MimeType,
		CreatedAt: metadata.CreatedAt,
	}
}

func normalizeFileMetadata(raw southbound.RawJSON, request core.FileUploadRequest) fileMetadataResponse {
	createdAt := parseTimeValue(raw["created_at"])
	if createdAt == nil {
		now := time.Now().UTC()
		createdAt = &now
	}
	size, _ := numberAsInt64(raw["size"])
	if size == 0 {
		if sizeBytes, ok := numberAsInt64(raw["size_bytes"]); ok && sizeBytes > 0 {
			size = sizeBytes
		}
	}
	if size == 0 && request.ContentLength > 0 {
		size = request.ContentLength
	}
	if size == 0 && len(request.Content) > 0 {
		size = int64(len(request.Content))
	}
	fileID := stringValue(firstNonEmpty(raw["file_id"], raw["id"]))
	downloadURL := stringValue(raw["download_url"])
	if fileID != "" && (downloadURL == "" || strings.HasPrefix(downloadURL, "/api/v1/workspace-files/")) {
		downloadURL = "/api/v1/files/" + url.PathEscape(fileID) + "/download"
	}
	return fileMetadataResponse{
		FileID:       fileID,
		AssistantID:  stringValue(raw["assistant_id"]),
		UserID:       firstNonEmptyString(stringValue(raw["owner_user_id"]), stringValue(raw["user_id"]), request.UserID),
		Bucket:       stringValue(raw["bucket"]),
		Filename:     firstNonEmptyString(stringValue(raw["filename"]), request.Filename),
		RelativePath: stringValue(raw["relative_path"]),
		Direction:    stringValue(raw["direction"]),
		FileRole:     stringValue(raw["file_role"]),
		SourceType:   stringValue(raw["source_type"]),
		SourceRef:    stringValue(raw["source_ref"]),
		Size:         size,
		MimeType:     firstNonEmptyString(stringValue(firstNonEmpty(raw["mime_type"], raw["content_type"])), request.ContentType),
		CreatedAt:    createdAt,
		Status:       stringValue(raw["status"]),
		DownloadURL:  downloadURL,
	}
}

func firstNonEmpty(values ...any) any {
	for _, value := range values {
		if value == nil {
			continue
		}
		if text, ok := value.(string); ok && strings.TrimSpace(text) == "" {
			continue
		}
		if bytes, ok := value.([]byte); ok && strings.TrimSpace(string(bytes)) == "" {
			continue
		}
		if strings.TrimSpace(stringValue(value)) != "" || stringValue(value) == "" {
			return value
		}
	}
	return nil
}

func firstNonEmptyString(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}

func stringValue(value any) string {
	switch typed := value.(type) {
	case string:
		return typed
	case []byte:
		return string(typed)
	case nil:
		return ""
	default:
		return ""
	}
}

func numberAsInt64(value any) (int64, bool) {
	switch typed := value.(type) {
	case int:
		return int64(typed), true
	case int64:
		return typed, true
	case int32:
		return int64(typed), true
	case float64:
		return int64(typed), true
	case json.Number:
		parsed, err := typed.Int64()
		return parsed, err == nil
	default:
		return 0, false
	}
}

func parseTimeValue(value any) *time.Time {
	switch typed := value.(type) {
	case time.Time:
		return &typed
	case string:
		for _, layout := range []string{
			time.RFC3339Nano,
			"2006-01-02T15:04:05.999999",
			"2006-01-02T15:04:05",
			"2006-01-02 15:04:05.999999",
			"2006-01-02 15:04:05",
		} {
			parsed, err := time.Parse(layout, typed)
			if err == nil {
				parsed = parsed.UTC()
				return &parsed
			}
		}
		return nil
	default:
		return nil
	}
}

func writePrometheusGauge(builder *strings.Builder, name string, help string, value float64, labels map[string]string) {
	writePrometheusHeader(builder, name, help, "gauge")
	writePrometheusSample(builder, name, value, labels)
}

func writePrometheusHeader(builder *strings.Builder, name string, help string, metricType string) {
	if metricType == "" {
		metricType = "gauge"
	}
	builder.WriteString("# HELP ")
	builder.WriteString(name)
	builder.WriteByte(' ')
	builder.WriteString(help)
	builder.WriteByte('\n')
	builder.WriteString("# TYPE ")
	builder.WriteString(name)
	builder.WriteByte(' ')
	builder.WriteString(metricType)
	builder.WriteByte('\n')
}

func writePrometheusSample(builder *strings.Builder, name string, value float64, labels map[string]string) {
	builder.WriteString(name)
	if len(labels) > 0 {
		builder.WriteByte('{')
		keys := make([]string, 0, len(labels))
		for key := range labels {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		for index, key := range keys {
			if index > 0 {
				builder.WriteByte(',')
			}
			builder.WriteString(key)
			builder.WriteString("=\"")
			builder.WriteString(escapePrometheusLabel(labels[key]))
			builder.WriteByte('"')
		}
		builder.WriteByte('}')
	}
	builder.WriteByte(' ')
	builder.WriteString(strconv.FormatFloat(value, 'f', -1, 64))
	builder.WriteByte('\n')
}

func copyLabels(labels map[string]string) map[string]string {
	copied := make(map[string]string, len(labels)+1)
	for key, value := range labels {
		copied[key] = value
	}
	return copied
}

func escapePrometheusLabel(value string) string {
	value = strings.ReplaceAll(value, "\\", "\\\\")
	value = strings.ReplaceAll(value, "\n", "\\n")
	value = strings.ReplaceAll(value, "\"", "\\\"")
	return value
}

func boolFloat(value bool) float64 {
	if value {
		return 1
	}
	return 0
}

func parseAssistantProxyPath(requestPath string) (string, string, string, bool) {
	const prefix = "/assistants/"
	if !strings.HasPrefix(requestPath, prefix) {
		return "", "", "", false
	}
	rest := strings.TrimPrefix(requestPath, prefix)
	parts := strings.SplitN(rest, "/", 3)
	if len(parts) < 2 || parts[0] == "" {
		return "", "", "", false
	}
	proxyKind := parts[1]
	if proxyKind != "api" && proxyKind != "ui" && proxyKind != "ws" && proxyKind != "control" {
		return "", "", "", false
	}
	runtimePath := "/"
	if len(parts) == 3 && strings.TrimSpace(parts[2]) != "" {
		runtimePath = "/" + parts[2]
	}
	return parts[0], proxyKind, runtimePath, true
}

func validAssistantID(assistantID string) bool {
	if assistantID == "" || len(assistantID) > 191 || assistantID == "." || assistantID == ".." {
		return false
	}
	for _, char := range assistantID {
		switch {
		case char >= 'a' && char <= 'z':
		case char >= 'A' && char <= 'Z':
		case char >= '0' && char <= '9':
		case char == '-' || char == '_' || char == '.':
		default:
			return false
		}
	}
	return true
}

func containsPathTraversal(requestPath string) bool {
	decoded, err := url.PathUnescape(requestPath)
	if err != nil {
		return true
	}
	for _, segment := range strings.Split(decoded, "/") {
		if segment == ".." {
			return true
		}
	}
	return false
}

func runtimeProxyTarget(record runtimepool.RuntimeRecord) (*url.URL, error) {
	rawTarget := strings.TrimSpace(record.InternalBaseURL)
	if rawTarget == "" {
		rawTarget = strings.TrimSpace(record.GatewayURL)
	}
	if rawTarget == "" {
		return nil, errors.New("runtime proxy target is not configured")
	}
	target, err := url.Parse(rawTarget)
	if err != nil {
		return nil, errors.New("runtime proxy target is invalid")
	}
	if target.Scheme != "http" && target.Scheme != "https" {
		return nil, errors.New("runtime proxy target scheme must be http or https")
	}
	if target.Host == "" || target.User != nil {
		return nil, errors.New("runtime proxy target host is invalid")
	}
	target.RawQuery = ""
	target.Fragment = ""
	return target, nil
}

func (h *handler) runtimeProxyTargetIsSelf(target *url.URL, r *http.Request) bool {
	if target == nil {
		return false
	}
	if sameHost(target.Host, r.Host) {
		return true
	}
	if h.publicBaseURL == "" {
		return false
	}
	base, err := url.Parse(h.publicBaseURL)
	if err != nil {
		return false
	}
	return sameHost(target.Host, base.Host)
}

func sameHost(left string, right string) bool {
	left = strings.ToLower(strings.TrimSpace(left))
	right = strings.ToLower(strings.TrimSpace(right))
	return left != "" && right != "" && left == right
}

func joinURLPath(basePath string, requestPath string) string {
	basePath = strings.TrimRight(basePath, "/")
	if basePath == "" {
		basePath = "/"
	}
	if requestPath == "" {
		requestPath = "/"
	}
	if basePath == "/" {
		if strings.HasPrefix(requestPath, "/") {
			return requestPath
		}
		return "/" + requestPath
	}
	if strings.HasPrefix(requestPath, "/") {
		return basePath + requestPath
	}
	return basePath + "/" + requestPath
}

func forwardedProto(r *http.Request) string {
	if r.TLS != nil {
		return "https"
	}
	return "http"
}

func (h *handler) secureProxySessionCookie(r *http.Request) bool {
	if r.TLS != nil {
		return true
	}
	return strings.HasPrefix(strings.ToLower(h.publicBaseURL), "https://")
}

func (h *handler) acquireProxySlot() bool {
	if h.proxySlots == nil {
		return true
	}
	select {
	case h.proxySlots <- struct{}{}:
		return true
	default:
		return false
	}
}

func (h *handler) releaseProxySlot() {
	if h.proxySlots == nil {
		return
	}
	select {
	case <-h.proxySlots:
	default:
	}
}

func requestOrigin(r *http.Request) string {
	return strings.TrimSpace(r.Header.Get("Origin"))
}

func stripLaunchTicketQuery(r *http.Request) string {
	copied := *r.URL
	query := copied.Query()
	query.Del("launchTicket")
	query.Del("ticket")
	copied.RawQuery = query.Encode()
	target := copied.RequestURI()
	if target == "" {
		return r.URL.Path
	}
	return target
}

func assistantLaunchRedirectTarget(
	r *http.Request,
	assistantID string,
	proxyKind string,
	session core.ControlUILaunchSession,
	publicBaseURL string,
) string {
	copied := *r.URL
	query := copied.Query()
	query.Del("launchTicket")
	query.Del("ticket")
	if proxyKind == "ui" {
		sessionKey := strings.TrimSpace(session.SessionKey)
		if sessionKey == "" {
			sessionKey = "main"
		}
		query.Set("session", sessionKey)
		if apiBaseURL := assistantLaunchAPIBaseURL(r, publicBaseURL, assistantID); apiBaseURL != "" {
			query.Set("ironAgentApiUrl", apiBaseURL)
		}
	}
	copied.RawQuery = query.Encode()
	if proxyKind == "ui" {
		fragment := url.Values{}
		if token := strings.TrimSpace(session.GatewayToken); token != "" {
			fragment.Set("token", token)
		}
		if gatewayURL := assistantLaunchGatewayURL(r, publicBaseURL, assistantID); gatewayURL != "" {
			fragment.Set("gatewayUrl", gatewayURL)
		}
		fragment.Set("ironAgentAssistantId", assistantID)
		if apiBaseURL := assistantLaunchAPIBaseURL(r, publicBaseURL, assistantID); apiBaseURL != "" {
			fragment.Set("ironAgentApiUrl", apiBaseURL)
		}
		encodedFragment := fragment.Encode()
		if decodedFragment, err := url.QueryUnescape(encodedFragment); err == nil {
			copied.Fragment = decodedFragment
			copied.RawFragment = encodedFragment
		} else {
			copied.Fragment = encodedFragment
		}
	}
	target := copied.String()
	if target == "" {
		return r.URL.Path
	}
	return target
}

func assistantLaunchAPIBaseURL(r *http.Request, publicBaseURL string, assistantID string) string {
	if base := strings.TrimRight(strings.TrimSpace(publicBaseURL), "/"); base != "" {
		return base + "/assistants/" + url.PathEscape(assistantID) + "/control"
	}
	host := strings.TrimSpace(r.Host)
	if host == "" {
		return ""
	}
	return forwardedProto(r) + "://" + host + "/assistants/" + url.PathEscape(assistantID) + "/control"
}

func assistantLaunchGatewayURL(r *http.Request, publicBaseURL string, assistantID string) string {
	base := strings.TrimRight(strings.TrimSpace(publicBaseURL), "/")
	if base != "" {
		parsed, err := url.Parse(base)
		if err != nil || parsed.Host == "" {
			return ""
		}
		if parsed.Scheme == "https" {
			parsed.Scheme = "wss"
		} else {
			parsed.Scheme = "ws"
		}
		parsed.Path = strings.TrimRight(parsed.Path, "/") + "/assistants/" + url.PathEscape(assistantID) + "/ui"
		parsed.RawQuery = ""
		parsed.Fragment = ""
		return parsed.String()
	}
	host := strings.TrimSpace(r.Host)
	if host == "" {
		return ""
	}
	scheme := "ws"
	if forwardedProto(r) == "https" {
		scheme = "wss"
	}
	return scheme + "://" + host + "/assistants/" + url.PathEscape(assistantID) + "/ui"
}

func assistantUIConfigScriptContent(apiBaseURL string) string {
	apiBaseURL = strings.TrimSpace(apiBaseURL)
	if apiBaseURL == "" {
		return ""
	}
	quotedAPIBaseURL := strconv.Quote(apiBaseURL)
	return "(function(){const apiBase=" + quotedAPIBaseURL + ";" +
		"globalThis.__IRON_AGENT_API_URL__=apiBase;" +
		"const originalFetch=globalThis.fetch;" +
		"if(typeof originalFetch==='function'){" +
		"globalThis.fetch=function(input,init){" +
		"try{" +
		"const rawUrl=typeof input==='string'?input:(input&&input.url)||'';" +
		"const requestUrl=new URL(rawUrl,globalThis.location&&globalThis.location.href);" +
		"const apiUrl=new URL(apiBase,globalThis.location&&globalThis.location.href);" +
		"if(requestUrl.href.startsWith(apiUrl.href)){" +
		"init=Object.assign({},init||{},{credentials:'same-origin'});" +
		"}" +
		"}catch(_err){}" +
		"return originalFetch.call(this,input,init);" +
		"};" +
		"}" +
		"})();"
}

func injectAssistantUIConfig(body []byte, scriptContent string) []byte {
	scriptContent = strings.TrimSpace(scriptContent)
	if scriptContent == "" || !bytes.Contains(body, []byte("<html")) {
		return body
	}
	script := []byte("<script>" + scriptContent + "</script>\n")
	if bytes.Contains(body, script) {
		return body
	}
	if bytes.Contains(body, []byte("<head>")) {
		return bytes.Replace(body, []byte("<head>"), append([]byte("<head>\n"), script...), 1)
	}
	if bytes.Contains(body, []byte("</head>")) {
		return bytes.Replace(body, []byte("</head>"), append(script, []byte("</head>")...), 1)
	}
	return append(script, body...)
}

func allowAssistantUIConfigScript(header http.Header, scriptContent string) {
	scriptContent = strings.TrimSpace(scriptContent)
	csp := strings.TrimSpace(header.Get("Content-Security-Policy"))
	if scriptContent == "" || csp == "" {
		return
	}
	sum := sha256.Sum256([]byte(scriptContent))
	hashSource := "'sha256-" + base64.StdEncoding.EncodeToString(sum[:]) + "'"
	if strings.Contains(csp, hashSource) {
		return
	}
	directives := strings.Split(csp, ";")
	for i, directive := range directives {
		trimmed := strings.TrimSpace(directive)
		if strings.HasPrefix(trimmed, "script-src ") || trimmed == "script-src" {
			directives[i] = trimmed + " " + hashSource
			header.Set("Content-Security-Policy", strings.Join(directives, "; "))
			return
		}
	}
	header.Set("Content-Security-Policy", csp+"; script-src 'self' "+hashSource)
}

func stripLegacyCredentialQuery(r *http.Request) string {
	copied := *r.URL
	query := copied.Query()
	for key := range query {
		if isCredentialQueryKey(key) {
			query.Del(key)
		}
	}
	copied.RawQuery = query.Encode()
	target := copied.RequestURI()
	if target == "" {
		return r.URL.Path
	}
	return target
}

func hasCredentialQuery(query url.Values) bool {
	for key := range query {
		if isCredentialQueryKey(key) {
			return true
		}
	}
	return false
}

func isCredentialQueryKey(key string) bool {
	normalized := strings.ToLower(strings.TrimSpace(key))
	return normalized == "token" || strings.HasSuffix(normalized, "token")
}

func safeAdminRedirect(raw string) string {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return "/admin/"
	}
	parsed, err := url.Parse(trimmed)
	if err != nil || parsed.IsAbs() || strings.HasPrefix(trimmed, "//") {
		return "/admin/"
	}
	if parsed.Path != "/admin" && !strings.HasPrefix(parsed.Path, "/admin/") {
		return "/admin/"
	}
	return parsed.RequestURI()
}

func launchTicketStatus(err error) int {
	var httpErr *southbound.HTTPError
	if errors.As(err, &httpErr) {
		switch httpErr.StatusCode {
		case http.StatusForbidden:
			return http.StatusForbidden
		case http.StatusGone:
			return http.StatusUnauthorized
		case http.StatusNotFound:
			return http.StatusUnauthorized
		}
	}
	return http.StatusUnauthorized
}

type proxySession struct {
	gatewayToken string
}

func (c proxySessionConfig) sign(assistantID string, sessionKey string, expiresAt time.Time, gatewayToken ...string) string {
	token := ""
	if len(gatewayToken) > 0 {
		token = strings.TrimSpace(gatewayToken[0])
	}
	payload := strings.Join([]string{
		assistantID,
		strings.TrimSpace(sessionKey),
		strconv.FormatInt(expiresAt.UTC().Unix(), 10),
		token,
	}, "|")
	mac := hmac.New(sha256.New, c.secret)
	_, _ = mac.Write([]byte(payload))
	signature := mac.Sum(nil)
	return base64.RawURLEncoding.EncodeToString([]byte(payload)) + "." + base64.RawURLEncoding.EncodeToString(signature)
}

func (c proxySessionConfig) valid(r *http.Request, assistantID string) bool {
	_, ok := c.resolve(r, assistantID)
	return ok
}

func (c proxySessionConfig) validCookie(r *http.Request, cookieName string, subjectID string) bool {
	_, ok := c.resolveCookie(r, cookieName, subjectID)
	return ok
}

func (c proxySessionConfig) resolve(r *http.Request, assistantID string) (proxySession, bool) {
	return c.resolveCookie(r, assistantProxySessionCookie, assistantID)
}

func (c proxySessionConfig) resolveCookie(r *http.Request, cookieName string, subjectID string) (proxySession, bool) {
	if len(c.secret) == 0 {
		return proxySession{}, false
	}
	cookie, err := r.Cookie(cookieName)
	if err != nil {
		return proxySession{}, false
	}
	rawPayload, rawSignature, ok := strings.Cut(cookie.Value, ".")
	if !ok || rawPayload == "" || rawSignature == "" {
		return proxySession{}, false
	}
	payload, err := base64.RawURLEncoding.DecodeString(rawPayload)
	if err != nil {
		return proxySession{}, false
	}
	signature, err := base64.RawURLEncoding.DecodeString(rawSignature)
	if err != nil {
		return proxySession{}, false
	}
	mac := hmac.New(sha256.New, c.secret)
	_, _ = mac.Write(payload)
	if !hmac.Equal(signature, mac.Sum(nil)) {
		return proxySession{}, false
	}
	parts := strings.Split(string(payload), "|")
	if (len(parts) != 3 && len(parts) != 4) || parts[0] != subjectID {
		return proxySession{}, false
	}
	expiresAt, err := strconv.ParseInt(parts[2], 10, 64)
	if err != nil {
		return proxySession{}, false
	}
	if !time.Now().UTC().Before(time.Unix(expiresAt, 0).UTC()) {
		return proxySession{}, false
	}
	session := proxySession{}
	if len(parts) == 4 {
		session.gatewayToken = strings.TrimSpace(parts[3])
	}
	return session, true
}

func writeProxyError(w http.ResponseWriter, statusCode int, code string, message string) {
	writeJSON(w, statusCode, map[string]any{
		"error": map[string]any{
			"code":    code,
			"message": message,
		},
	})
}

func statusFromError(err error) int {
	var tooLargeErr *payloadTooLargeError
	if errors.As(err, &tooLargeErr) {
		return http.StatusRequestEntityTooLarge
	}
	var validationErr *core.ValidationError
	if errors.As(err, &validationErr) {
		return http.StatusBadRequest
	}
	var unprocessableErr *unprocessableEntityError
	if errors.As(err, &unprocessableErr) {
		return http.StatusUnprocessableEntity
	}
	var forbiddenErr *forbiddenError
	if errors.As(err, &forbiddenErr) {
		return http.StatusForbidden
	}
	var callbackErr *callbackUnavailableError
	if errors.As(err, &callbackErr) {
		return http.StatusServiceUnavailable
	}
	var queueFullErr *core.QueueFullError
	if errors.As(err, &queueFullErr) {
		return http.StatusTooManyRequests
	}
	var conflictErr *core.ConflictError
	if errors.As(err, &conflictErr) {
		return http.StatusConflict
	}
	var httpErr *southbound.HTTPError
	if errors.As(err, &httpErr) {
		if httpErr.StatusCode >= 500 {
			return http.StatusBadGateway
		}
		return httpErr.StatusCode
	}
	return http.StatusInternalServerError
}

func writeError(w http.ResponseWriter, err error) {
	var tooLargeErr *payloadTooLargeError
	if errors.As(err, &tooLargeErr) {
		writeJSON(w, http.StatusRequestEntityTooLarge, map[string]any{
			"error": map[string]any{
				"code":    "payload_too_large",
				"message": tooLargeErr.message,
			},
		})
		return
	}

	var validationErr *core.ValidationError
	if errors.As(err, &validationErr) {
		writeJSON(w, http.StatusBadRequest, map[string]any{
			"error": map[string]any{
				"code":    "bad_request",
				"message": publicValidationErrorMessage(validationErr.Message),
			},
		})
		return
	}

	var unprocessableErr *unprocessableEntityError
	if errors.As(err, &unprocessableErr) {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{
			"error": map[string]any{
				"code":    "bad_request",
				"message": unprocessableErr.message,
			},
		})
		return
	}

	var forbiddenErr *forbiddenError
	if errors.As(err, &forbiddenErr) {
		writeJSON(w, http.StatusForbidden, map[string]any{
			"error": map[string]any{
				"code":    "forbidden",
				"message": forbiddenErr.message,
			},
		})
		return
	}

	var callbackErr *callbackUnavailableError
	if errors.As(err, &callbackErr) {
		message := strings.TrimSpace(callbackErr.message)
		if message == "" {
			message = "callback 通道暂不可用，请稍后重试"
		}
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{
			"error": map[string]any{
				"code":      "callback_unavailable",
				"message":   message,
				"retryable": true,
			},
		})
		return
	}

	var queueFullErr *core.QueueFullError
	if errors.As(err, &queueFullErr) {
		writeJSON(w, http.StatusTooManyRequests, map[string]any{
			"error": map[string]any{
				"code":    "queue_full",
				"message": publicValidationErrorMessage(queueFullErr.Message),
			},
		})
		return
	}

	var conflictErr *core.ConflictError
	if errors.As(err, &conflictErr) {
		writeJSON(w, http.StatusConflict, map[string]any{
			"error": map[string]any{
				"code":    "conflict",
				"message": conflictErr.Message,
			},
		})
		return
	}

	var httpErr *southbound.HTTPError
	if errors.As(err, &httpErr) {
		statusCode := httpErr.StatusCode
		if statusCode >= 500 {
			statusCode = http.StatusBadGateway
		}
		writeJSON(w, statusCode, map[string]any{
			"error": map[string]any{
				"code":    "southbound_error",
				"message": publicSouthboundErrorMessage(httpErr),
			},
		})
		return
	}

	writeJSON(w, http.StatusInternalServerError, map[string]any{
		"error": map[string]any{
			"code":    "internal_error",
			"message": "服务端错误",
		},
	})
}

func publicValidationErrorMessage(message string) string {
	message = strings.TrimSpace(message)
	switch message {
	case "":
		return "请求参数错误"
	case "idempotency_key was already used with a different request":
		return "request_id 已被不同请求使用"
	case "instruction or message is required":
		return "缺少用户消息内容，请传入 message"
	case "task_id is required":
		return "缺少任务标识"
	case "approval_id is required":
		return "缺少审批标识"
	case "assistant_id is required":
		return "缺少助手标识"
	case "assistant_id or user_id is required":
		return "缺少用户标识，请传入 user_id"
	case "filename is required":
		return "缺少文件名"
	case "file content is required":
		return "缺少上传文件，请传入 file"
	case "task queue is full":
		return "任务队列已满，请稍后重试"
	case "task is queued and has not started":
		return "任务仍在排队中，暂不能执行该操作"
	case "task is still pending approval":
		return "任务仍在等待审批"
	case "task has no approval to resolve":
		return "任务没有待处理审批"
	case "decision must be approve or deny":
		return "审批决策只能是 approve 或 deny"
	case "resolution must be allow or deny":
		return "审批结果只能是 allow 或 deny"
	default:
		if strings.HasPrefix(message, "invalid JSON body:") {
			return "请求体不是合法 JSON"
		}
		if strings.HasPrefix(message, "invalid boolean value") {
			return "布尔字段值不合法"
		}
		return message
	}
}

func publicSouthboundErrorMessage(httpErr *southbound.HTTPError) string {
	body := strings.TrimSpace(httpErr.Body)
	if body == "" {
		return "下游服务返回错误"
	}
	return "下游服务返回错误: " + body
}

func writeJSON(w http.ResponseWriter, statusCode int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(statusCode)
	_ = json.NewEncoder(w).Encode(payload)
}

type statusCaptureResponseWriter struct {
	http.ResponseWriter
	statusCode int
}

func (w *statusCaptureResponseWriter) WriteHeader(statusCode int) {
	w.statusCode = statusCode
	w.ResponseWriter.WriteHeader(statusCode)
}

func (w *statusCaptureResponseWriter) Flush() {
	if flusher, ok := w.ResponseWriter.(http.Flusher); ok {
		flusher.Flush()
	}
}

func (w *statusCaptureResponseWriter) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	hijacker, ok := w.ResponseWriter.(http.Hijacker)
	if !ok {
		return nil, nil, errors.New("response writer does not support hijacking")
	}
	return hijacker.Hijack()
}

type publicFailureAuditResponseWriter struct {
	http.ResponseWriter
	statusCode  int
	wroteHeader bool
	body        bytes.Buffer
	bodyLimit   int
	truncated   bool
}

func (w *publicFailureAuditResponseWriter) WriteHeader(statusCode int) {
	if w.wroteHeader {
		return
	}
	w.statusCode = statusCode
	w.wroteHeader = true
	w.ResponseWriter.WriteHeader(statusCode)
}

func (w *publicFailureAuditResponseWriter) Write(payload []byte) (int, error) {
	if !w.wroteHeader {
		w.statusCode = http.StatusOK
		w.wroteHeader = true
	}
	if w.bodyLimit > 0 && w.body.Len() < w.bodyLimit {
		remaining := w.bodyLimit - w.body.Len()
		if len(payload) > remaining {
			_, _ = w.body.Write(payload[:remaining])
			w.truncated = true
		} else {
			_, _ = w.body.Write(payload)
		}
	} else if len(payload) > 0 {
		w.truncated = true
	}
	return w.ResponseWriter.Write(payload)
}

func (w *publicFailureAuditResponseWriter) Flush() {
	if flusher, ok := w.ResponseWriter.(http.Flusher); ok {
		flusher.Flush()
	}
}

func (w *publicFailureAuditResponseWriter) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	hijacker, ok := w.ResponseWriter.(http.Hijacker)
	if !ok {
		return nil, nil, errors.New("response writer does not support hijacking")
	}
	return hijacker.Hijack()
}
