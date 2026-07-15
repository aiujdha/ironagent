package auth

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

const hmacBodyMemoryThreshold = 1 << 20

type Config struct {
	APIKeys                    []string
	ScopedAPIKeys              []ScopedAPIKey
	HMACSecret                 string
	AllowedSkew                time.Duration
	MaxBodyBytes               int64
	NonceStore                 NonceStore
	AllowPublicUnauthenticated bool
	Logger                     *slog.Logger
}

type ScopedAPIKey struct {
	Key    string
	Scopes []string
}

type RequestInfo struct {
	APIKeyPresent bool
	RequiredScope string
	Scopes        []string
}

type requestInfoContextKey struct{}

func RequestInfoFromContext(ctx context.Context) (RequestInfo, bool) {
	info, ok := ctx.Value(requestInfoContextKey{}).(RequestInfo)
	return info, ok
}

func RequestHasScope(ctx context.Context, scope string) bool {
	info, ok := RequestInfoFromContext(ctx)
	if !ok {
		return false
	}
	scope = strings.ToLower(strings.TrimSpace(scope))
	for _, candidate := range info.Scopes {
		if candidate == "*" || candidate == scope {
			return true
		}
	}
	return false
}

type NonceStore interface {
	UseNonce(ctx context.Context, clientID string, nonce string, expiresAt time.Time) (bool, error)
}

func NewMiddleware(cfg Config) func(http.Handler) http.Handler {
	keys := make(map[string]map[string]struct{}, len(cfg.APIKeys)+len(cfg.ScopedAPIKeys))
	maxBodyBytes := cfg.MaxBodyBytes
	if maxBodyBytes <= 0 {
		maxBodyBytes = 64 * 1024 * 1024
	}
	nonces := cfg.NonceStore
	if nonces == nil {
		nonces = newNonceCache()
	}
	for _, key := range cfg.APIKeys {
		addScopes(keys, key, []string{"*"})
	}
	for _, scoped := range cfg.ScopedAPIKeys {
		addScopes(keys, scoped.Key, scoped.Scopes)
	}

	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Body != nil {
				r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes)
			}
			cleanup, info, err := authorizeRequest(r, keys, cfg.HMACSecret, cfg.AllowedSkew, nonces, cfg.AllowPublicUnauthenticated)
			if err != nil {
				if cfg.Logger != nil {
					status := authErrorStatusCode(err)
					domain, reason, owner := authFaultAttribution(status)
					cfg.Logger.Warn("gateway_auth_rejected",
						"fault_domain", domain,
						"reason_code", reason,
						"auth_reason", authErrorReason(err),
						"fault_owner", owner,
						"attribution_confidence", "high",
						"boundary", "upstream_ingress",
						"lifecycle_phase", "admitted",
						"status_code", status,
						"method", r.Method,
						"path", r.URL.Path,
						"upstream_request_id", firstNonEmptyHeader(r, "X-Request-ID", "X-Correlation-ID"),
						"error", err.Error(),
					)
				}
				writeAuthError(w, err)
				return
			}
			if cleanup != nil {
				defer cleanup()
			}
			r = r.WithContext(context.WithValue(r.Context(), requestInfoContextKey{}, info))
			next.ServeHTTP(w, r)
		})
	}
}

func addScopes(keys map[string]map[string]struct{}, key string, scopes []string) {
	key = strings.TrimSpace(key)
	if key == "" {
		return
	}
	if keys[key] == nil {
		keys[key] = make(map[string]struct{})
	}
	if len(scopes) == 0 {
		scopes = []string{"public"}
	}
	for _, scope := range scopes {
		scope = strings.ToLower(strings.TrimSpace(scope))
		if scope == "" {
			continue
		}
		keys[key][scope] = struct{}{}
	}
}

func scopeNames(scopes map[string]struct{}) []string {
	out := make([]string, 0, len(scopes))
	for scope := range scopes {
		out = append(out, scope)
	}
	return out
}

func authorizeRequest(
	r *http.Request,
	apiKeys map[string]map[string]struct{},
	hmacSecret string,
	allowedSkew time.Duration,
	nonces NonceStore,
	allowPublicUnauthenticated bool,
) (func(), RequestInfo, error) {
	required := requiredScope(r.Method, r.URL.Path)
	apiKey := strings.TrimSpace(r.Header.Get("X-API-Key"))
	info := RequestInfo{APIKeyPresent: apiKey != "", RequiredScope: required}
	if apiKey == "" && allowPublicUnauthenticated && required == "public" {
		info.Scopes = []string{"public"}
		return nil, info, nil
	}
	scopes, ok := apiKeys[apiKey]
	if !ok {
		return nil, info, authError{statusCode: http.StatusUnauthorized, message: "invalid api key", reason: "auth.invalid_api_key"}
	}
	info.Scopes = scopeNames(scopes)
	if !hasScope(scopes, required) {
		return nil, info, authError{
			statusCode: http.StatusForbidden,
			message:    fmt.Sprintf("api key lacks %q scope", required),
			reason:     "auth.scope_denied",
		}
	}

	if hmacSecret == "" {
		return nil, info, nil
	}

	clientID := strings.TrimSpace(r.Header.Get("X-Client-ID"))
	if clientID == "" {
		return nil, info, authError{statusCode: http.StatusUnauthorized, message: "missing x-client-id", reason: "auth.missing_client_id"}
	}
	nonce := strings.TrimSpace(r.Header.Get("X-Nonce"))
	if nonce == "" {
		return nil, info, authError{statusCode: http.StatusUnauthorized, message: "missing x-nonce", reason: "auth.missing_nonce"}
	}

	rawTimestamp := strings.TrimSpace(r.Header.Get("X-Timestamp"))
	if rawTimestamp == "" {
		return nil, info, authError{statusCode: http.StatusUnauthorized, message: "missing x-timestamp", reason: "auth.missing_timestamp"}
	}
	seconds, err := strconv.ParseInt(rawTimestamp, 10, 64)
	if err != nil {
		return nil, info, authError{statusCode: http.StatusUnauthorized, message: "invalid x-timestamp", reason: "auth.timestamp_skew"}
	}
	requestTime := time.Unix(seconds, 0).UTC()
	if delta := time.Since(requestTime); delta > allowedSkew || delta < -allowedSkew {
		return nil, info, authError{statusCode: http.StatusUnauthorized, message: "request timestamp outside allowed skew", reason: "auth.timestamp_skew"}
	}

	signature := strings.TrimSpace(r.Header.Get("X-Signature"))
	if signature == "" {
		return nil, info, authError{statusCode: http.StatusUnauthorized, message: "missing x-signature", reason: "auth.bad_signature"}
	}

	bodyHash, cleanup, err := hashAndRestoreBody(r)
	if err != nil {
		var maxErr *http.MaxBytesError
		if errors.As(err, &maxErr) {
			return nil, info, authError{statusCode: http.StatusRequestEntityTooLarge, message: "request body too large", reason: "auth.payload_too_large"}
		}
		return nil, info, authError{statusCode: http.StatusUnauthorized, message: "read request body: " + err.Error(), reason: "auth.bad_signature"}
	}

	payload := strings.Join([]string{
		clientID,
		rawTimestamp,
		nonce,
		strings.ToUpper(r.Method),
		r.URL.RequestURI(),
		bodyHash,
	}, ".")
	mac := hmac.New(sha256.New, []byte(hmacSecret))
	_, _ = mac.Write([]byte(payload))
	expected := hex.EncodeToString(mac.Sum(nil))
	if !hmac.Equal([]byte(signature), []byte(expected)) {
		if cleanup != nil {
			cleanup()
		}
		return nil, info, authError{statusCode: http.StatusUnauthorized, message: "invalid x-signature", reason: "auth.bad_signature"}
	}
	if nonces != nil {
		ok, err := nonces.UseNonce(r.Context(), clientID, nonce, time.Now().UTC().Add(allowedSkew))
		if err != nil {
			if cleanup != nil {
				cleanup()
			}
			return nil, info, authError{statusCode: http.StatusInternalServerError, message: "record x-nonce: " + err.Error(), reason: "auth.nonce_store_error"}
		}
		if !ok {
			if cleanup != nil {
				cleanup()
			}
			return nil, info, authError{statusCode: http.StatusUnauthorized, message: "replayed x-nonce", reason: "auth.replayed_nonce"}
		}
	}

	return cleanup, info, nil
}

func hashAndRestoreBody(r *http.Request) (string, func(), error) {
	if r.Body == nil {
		sum := sha256.Sum256(nil)
		r.Body = io.NopCloser(bytes.NewReader(nil))
		return hex.EncodeToString(sum[:]), nil, nil
	}

	original := r.Body
	defer original.Close()

	hasher := sha256.New()
	tee := io.TeeReader(original, hasher)
	var memory bytes.Buffer
	_, err := io.CopyN(&memory, tee, hmacBodyMemoryThreshold+1)
	if err == nil {
		temp, err := os.CreateTemp("", "ironagent-hmac-body-*")
		if err != nil {
			return "", nil, err
		}
		cleanup := func() {
			_ = temp.Close()
			_ = os.Remove(temp.Name())
		}
		if _, err := temp.Write(memory.Bytes()); err != nil {
			cleanup()
			return "", nil, err
		}
		if _, err := io.Copy(temp, tee); err != nil {
			cleanup()
			return "", nil, err
		}
		if _, err := temp.Seek(0, io.SeekStart); err != nil {
			cleanup()
			return "", nil, err
		}
		r.Body = temp
		return hex.EncodeToString(hasher.Sum(nil)), cleanup, nil
	}
	if errors.Is(err, io.EOF) {
		r.Body = io.NopCloser(bytes.NewReader(memory.Bytes()))
		return hex.EncodeToString(hasher.Sum(nil)), nil, nil
	}
	return "", nil, err
}

type nonceCache struct {
	mu      sync.Mutex
	expires map[string]time.Time
}

func newNonceCache() *nonceCache {
	return &nonceCache{expires: make(map[string]time.Time)}
}

func (c *nonceCache) UseNonce(_ context.Context, clientID string, nonce string, expiresAt time.Time) (bool, error) {
	key := clientID + "\x00" + nonce
	now := time.Now().UTC()
	c.mu.Lock()
	defer c.mu.Unlock()
	for existing, expiry := range c.expires {
		if expiry.Before(now) {
			delete(c.expires, existing)
		}
	}
	if _, ok := c.expires[key]; ok {
		return false, nil
	}
	c.expires[key] = expiresAt
	return true, nil
}

func hasScope(scopes map[string]struct{}, required string) bool {
	if required == "" {
		required = "public"
	}
	if _, ok := scopes["*"]; ok {
		return true
	}
	_, ok := scopes[required]
	return ok
}

func requiredScope(method string, path string) string {
	method = strings.ToUpper(strings.TrimSpace(method))
	path = strings.TrimSpace(path)

	if path == "/mcp" || strings.HasPrefix(path, "/mcp/") {
		return "admin"
	}
	if strings.HasPrefix(path, "/assistants/") {
		return "proxy"
	}

	if method == http.MethodGet && path == "/metrics" {
		return "debug"
	}
	if method == http.MethodGet && (path == "/api/v1/metrics" || path == "/api/v1/task-queue") {
		return "debug"
	}
	if method == http.MethodGet && path == "/api/v1/metrics/prometheus" {
		return "debug"
	}
	if method == http.MethodGet && path == "/api/v1/runtime/scheduler/last-decision" {
		return "debug"
	}
	if method == http.MethodGet && path == "/api/v1/webhooks/dead-letter" {
		return "debug"
	}
	if method == http.MethodPost && strings.HasPrefix(path, "/api/v1/webhooks/dead-letter/") && strings.HasSuffix(path, "/replay") {
		return "admin"
	}

	if path == "/api/v1/runtime/registry" || strings.HasPrefix(path, "/api/v1/runtime/registry/") {
		return "runtime"
	}
	if path == "/api/v1/runtime/start" || path == "/api/v1/runtime" || path == "/api/v1/runtime/stop" {
		return "admin"
	}

	if strings.HasPrefix(path, "/api/v1/tasks/") {
		if method == http.MethodGet || strings.HasSuffix(path, "/cancel") {
			return "public"
		}
		return "admin"
	}
	if path == "/api/v1/files" {
		return "debug"
	}
	if isPublicFileRoute(method, path) {
		return "public"
	}
	if strings.HasPrefix(path, "/api/v1/files/") || path == "/api/v1/files/sync-results" {
		return "admin"
	}
	return "public"
}

func isPublicFileRoute(method string, path string) bool {
	if method == http.MethodPost && path == "/api/v1/files/upload" {
		return true
	}
	if method != http.MethodGet {
		return false
	}
	if !strings.HasPrefix(path, "/api/v1/files/") {
		return false
	}
	trimmed := strings.TrimPrefix(path, "/api/v1/files/")
	if trimmed == "" {
		return false
	}
	if strings.Contains(trimmed, "/") {
		return strings.HasSuffix(trimmed, "/download") && strings.Count(trimmed, "/") == 1
	}
	return true
}

type authError struct {
	statusCode int
	message    string
	// reason is a stable machine sub-reason that disambiguates auth failures
	// sharing the same status-derived reason_code (e.g. all 401 causes map to
	// upstream.unauthorized). Used additively in the gateway_auth_rejected log.
	reason string
}

func (e authError) Error() string {
	return e.message
}

func authErrorStatusCode(err error) int {
	var requestErr authError
	if errors.As(err, &requestErr) && requestErr.statusCode > 0 {
		return requestErr.statusCode
	}
	return http.StatusUnauthorized
}

func authErrorReason(err error) string {
	var requestErr authError
	if errors.As(err, &requestErr) {
		return requestErr.reason
	}
	return ""
}

// authFaultAttribution maps an auth HTTP status to the unified fault-attribution
// fields. auth does not import core, so these mirror the taxonomy as literals:
// 401/403/413 are the caller's request (upstream); a 500 is a gateway-internal
// failure such as a nonce-store error.
func authFaultAttribution(statusCode int) (domain, reason, owner string) {
	switch statusCode {
	case http.StatusForbidden:
		return "UPSTREAM", "upstream.forbidden", "upstream"
	case http.StatusRequestEntityTooLarge:
		return "UPSTREAM", "upstream.payload_too_large", "upstream"
	case http.StatusInternalServerError:
		return "GATEWAY", "gateway.internal_error", "gateway"
	default:
		return "UPSTREAM", "upstream.unauthorized", "upstream"
	}
}

func firstNonEmptyHeader(r *http.Request, keys ...string) string {
	for _, k := range keys {
		if v := strings.TrimSpace(r.Header.Get(k)); v != "" {
			return v
		}
	}
	return ""
}

func writeAuthError(w http.ResponseWriter, err error) {
	statusCode := authErrorStatusCode(err)
	code := "unauthorized"
	switch statusCode {
	case http.StatusForbidden:
		code = "forbidden"
	case http.StatusRequestEntityTooLarge:
		code = "request_too_large"
	case http.StatusInternalServerError:
		code = "internal_error"
	}
	writeJSON(w, statusCode, map[string]any{
		"error": map[string]any{
			"code":    code,
			"message": err.Error(),
		},
	})
}

func writeJSON(w http.ResponseWriter, statusCode int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(statusCode)
	_ = json.NewEncoder(w).Encode(payload)
}
