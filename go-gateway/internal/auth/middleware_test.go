package auth

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestMiddlewareAcceptsValidAPIKeyAndHMAC(t *testing.T) {
	t.Parallel()

	timestamp := strconv.FormatInt(time.Now().Unix(), 10)
	nonce := "nonce-1"
	clientID := "xiaoxiongmao"
	body := []byte(`{"hello":"world"}`)
	signature := sign(clientID, timestamp, nonce, http.MethodPost, "/api/v1/chat", body, "secret-key")

	handler := NewMiddleware(Config{
		APIKeys:     []string{"dev-key"},
		HMACSecret:  "secret-key",
		AllowedSkew: 5 * time.Minute,
	})(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))

	request := httptest.NewRequest(http.MethodPost, "/api/v1/chat", bytes.NewReader(body))
	request.Header.Set("X-API-Key", "dev-key")
	request.Header.Set("X-Client-ID", clientID)
	request.Header.Set("X-Timestamp", timestamp)
	request.Header.Set("X-Nonce", nonce)
	request.Header.Set("X-Signature", signature)

	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusNoContent {
		t.Fatalf("expected 204, got %d", recorder.Code)
	}
}

func TestMiddlewareRejectsInvalidHMAC(t *testing.T) {
	t.Parallel()

	timestamp := strconv.FormatInt(time.Now().Unix(), 10)
	clientID := "xiaoxiongmao"
	body := []byte(`{"hello":"world"}`)

	handler := NewMiddleware(Config{
		APIKeys:     []string{"dev-key"},
		HMACSecret:  "secret-key",
		AllowedSkew: 5 * time.Minute,
	})(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))

	request := httptest.NewRequest(http.MethodPost, "/api/v1/chat", bytes.NewReader(body))
	request.Header.Set("X-API-Key", "dev-key")
	request.Header.Set("X-Client-ID", clientID)
	request.Header.Set("X-Timestamp", timestamp)
	request.Header.Set("X-Nonce", "nonce-2")
	request.Header.Set("X-Signature", "bad-signature")

	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", recorder.Code)
	}
}

func TestMiddlewareLogsAuthRejectionWithFaultDomain(t *testing.T) {
	t.Parallel()

	var logged bytes.Buffer
	logger := slog.New(slog.NewJSONHandler(&logged, &slog.HandlerOptions{Level: slog.LevelWarn}))
	handler := NewMiddleware(Config{
		APIKeys:     []string{"dev-key"},
		HMACSecret:  "secret-key",
		AllowedSkew: 5 * time.Minute,
		Logger:      logger,
	})(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))

	request := httptest.NewRequest(http.MethodPost, "/api/v1/chat", bytes.NewReader([]byte(`{}`)))
	request.Header.Set("X-API-Key", "dev-key")
	request.Header.Set("X-Client-ID", "c1")
	request.Header.Set("X-Timestamp", strconv.FormatInt(time.Now().Unix(), 10))
	request.Header.Set("X-Nonce", "nonce-log")
	request.Header.Set("X-Signature", "bad-signature")
	request.Header.Set("X-Request-ID", "req-log-123")

	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", recorder.Code)
	}
	out := logged.String()
	for _, want := range []string{
		"gateway_auth_rejected",
		`"fault_domain":"UPSTREAM"`,
		"upstream.unauthorized",
		"upstream_ingress",
		"req-log-123",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("auth rejection log missing %q\nlog: %s", want, out)
		}
	}
}

func TestMiddlewareRejectsOversizedHMACBody(t *testing.T) {
	t.Parallel()

	timestamp := strconv.FormatInt(time.Now().Unix(), 10)
	nonce := "nonce-3"
	clientID := "xiaoxiongmao"
	body := []byte(`{"hello":"world"}`)
	signature := sign(clientID, timestamp, nonce, http.MethodPost, "/api/v1/chat", body, "secret-key")

	handler := NewMiddleware(Config{
		APIKeys:      []string{"dev-key"},
		HMACSecret:   "secret-key",
		AllowedSkew:  5 * time.Minute,
		MaxBodyBytes: 4,
	})(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))

	request := httptest.NewRequest(http.MethodPost, "/api/v1/chat", bytes.NewReader(body))
	request.Header.Set("X-API-Key", "dev-key")
	request.Header.Set("X-Client-ID", clientID)
	request.Header.Set("X-Timestamp", timestamp)
	request.Header.Set("X-Nonce", nonce)
	request.Header.Set("X-Signature", signature)

	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("expected 413, got %d", recorder.Code)
	}
}

func TestMiddlewareSpillsLargeHMACBodyAndRestoresIt(t *testing.T) {
	t.Parallel()

	timestamp := strconv.FormatInt(time.Now().Unix(), 10)
	nonce := "nonce-large-body"
	clientID := "xiaoxiongmao"
	body := []byte(strings.Repeat("a", hmacBodyMemoryThreshold+128))
	signature := sign(clientID, timestamp, nonce, http.MethodPost, "/api/v1/files/upload", body, "secret-key")

	handler := NewMiddleware(Config{
		APIKeys:     []string{"dev-key"},
		HMACSecret:  "secret-key",
		AllowedSkew: 5 * time.Minute,
	})(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		restored, err := io.ReadAll(r.Body)
		if err != nil {
			t.Fatalf("read restored body: %v", err)
		}
		if !bytes.Equal(restored, body) {
			t.Fatalf("restored body length = %d, want %d", len(restored), len(body))
		}
		w.WriteHeader(http.StatusNoContent)
	}))

	request := httptest.NewRequest(http.MethodPost, "/api/v1/files/upload", bytes.NewReader(body))
	request.Header.Set("X-API-Key", "dev-key")
	request.Header.Set("X-Client-ID", clientID)
	request.Header.Set("X-Timestamp", timestamp)
	request.Header.Set("X-Nonce", nonce)
	request.Header.Set("X-Signature", signature)

	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusNoContent {
		t.Fatalf("expected 204, got %d", recorder.Code)
	}
}

func TestMiddlewareRejectsReplayedHMACNonce(t *testing.T) {
	t.Parallel()

	timestamp := strconv.FormatInt(time.Now().Unix(), 10)
	nonce := "nonce-replay"
	clientID := "xiaoxiongmao"
	body := []byte(`{"hello":"world"}`)
	signature := sign(clientID, timestamp, nonce, http.MethodPost, "/api/v1/chat", body, "secret-key")

	handler := NewMiddleware(Config{
		APIKeys:     []string{"dev-key"},
		HMACSecret:  "secret-key",
		AllowedSkew: 5 * time.Minute,
	})(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))

	for i, want := range []int{http.StatusNoContent, http.StatusUnauthorized} {
		request := httptest.NewRequest(http.MethodPost, "/api/v1/chat", bytes.NewReader(body))
		request.Header.Set("X-API-Key", "dev-key")
		request.Header.Set("X-Client-ID", clientID)
		request.Header.Set("X-Timestamp", timestamp)
		request.Header.Set("X-Nonce", nonce)
		request.Header.Set("X-Signature", signature)

		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, request)
		if recorder.Code != want {
			t.Fatalf("request %d: expected %d, got %d", i+1, want, recorder.Code)
		}
	}
}

func TestMiddlewareUsesConfiguredNonceStore(t *testing.T) {
	t.Parallel()

	timestamp := strconv.FormatInt(time.Now().Unix(), 10)
	nonce := "nonce-store"
	clientID := "xiaoxiongmao"
	body := []byte(`{"hello":"world"}`)
	signature := sign(clientID, timestamp, nonce, http.MethodPost, "/api/v1/chat", body, "secret-key")
	store := &recordingNonceStore{used: make(map[string]struct{})}

	handler := NewMiddleware(Config{
		APIKeys:     []string{"dev-key"},
		HMACSecret:  "secret-key",
		AllowedSkew: 5 * time.Minute,
		NonceStore:  store,
	})(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))

	for i, want := range []int{http.StatusNoContent, http.StatusUnauthorized} {
		request := httptest.NewRequest(http.MethodPost, "/api/v1/chat", bytes.NewReader(body))
		request.Header.Set("X-API-Key", "dev-key")
		request.Header.Set("X-Client-ID", clientID)
		request.Header.Set("X-Timestamp", timestamp)
		request.Header.Set("X-Nonce", nonce)
		request.Header.Set("X-Signature", signature)

		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, request)
		if recorder.Code != want {
			t.Fatalf("request %d: expected %d, got %d", i+1, want, recorder.Code)
		}
	}
	if store.calls != 2 {
		t.Fatalf("nonce store calls = %d, want 2", store.calls)
	}
}

func TestMiddlewareDoesNotReadBodyWithoutHMAC(t *testing.T) {
	t.Parallel()

	handler := NewMiddleware(Config{
		APIKeys:      []string{"dev-key"},
		MaxBodyBytes: 4,
	})(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))

	request := httptest.NewRequest(http.MethodPost, "/api/v1/chat", failingReadCloser{})
	request.Header.Set("X-API-Key", "dev-key")

	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusNoContent {
		t.Fatalf("expected 204, got %d", recorder.Code)
	}
}

func TestMiddlewareAllowsUnauthenticatedPublicWhenConfigured(t *testing.T) {
	t.Parallel()

	handler := NewMiddleware(Config{
		ScopedAPIKeys:              []ScopedAPIKey{{Key: "admin-key", Scopes: []string{"admin"}}},
		HMACSecret:                 "secret-key",
		AllowedSkew:                5 * time.Minute,
		AllowPublicUnauthenticated: true,
	})(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))

	request := httptest.NewRequest(http.MethodPost, "/api/v1/chat", bytes.NewReader([]byte(`{"message":"hi"}`)))

	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusNoContent {
		t.Fatalf("expected 204, got %d", recorder.Code)
	}
}

func TestMiddlewareKeepsNonPublicScopesProtectedWhenPublicAuthDisabled(t *testing.T) {
	t.Parallel()

	handler := NewMiddleware(Config{
		ScopedAPIKeys:              []ScopedAPIKey{{Key: "admin-key", Scopes: []string{"admin"}}},
		AllowPublicUnauthenticated: true,
	})(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))

	request := httptest.NewRequest(http.MethodPost, "/api/v1/tasks/task-1/complete", bytes.NewReader([]byte(`{}`)))

	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", recorder.Code)
	}
}

func TestMiddlewareRejectsWrongPublicAPIKeyEvenWhenUnauthenticatedPublicAllowed(t *testing.T) {
	t.Parallel()

	handler := NewMiddleware(Config{
		ScopedAPIKeys:              []ScopedAPIKey{{Key: "public-key", Scopes: []string{"public"}}},
		AllowPublicUnauthenticated: true,
	})(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))

	request := httptest.NewRequest(http.MethodPost, "/api/v1/chat", bytes.NewReader([]byte(`{}`)))
	request.Header.Set("X-API-Key", "wrong-key")

	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", recorder.Code)
	}
}

func TestMiddlewareLegacyAPIKeyAllowsAllScopes(t *testing.T) {
	t.Parallel()

	handler := NewMiddleware(Config{
		APIKeys: []string{"dev-key"},
	})(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))

	request := httptest.NewRequest(http.MethodPost, "/api/v1/tasks/task-1/complete", bytes.NewReader([]byte(`{}`)))
	request.Header.Set("X-API-Key", "dev-key")

	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusNoContent {
		t.Fatalf("expected 204, got %d", recorder.Code)
	}
}

func TestMiddlewareRejectsKeyWithoutRequiredScope(t *testing.T) {
	t.Parallel()

	handler := NewMiddleware(Config{
		ScopedAPIKeys: []ScopedAPIKey{{Key: "public-key", Scopes: []string{"public"}}},
	})(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))

	request := httptest.NewRequest(http.MethodGet, "/api/v1/runtime/registry", nil)
	request.Header.Set("X-API-Key", "public-key")

	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusForbidden {
		t.Fatalf("expected 403, got %d", recorder.Code)
	}
}

func TestMiddlewareAcceptsRuntimeScope(t *testing.T) {
	t.Parallel()

	handler := NewMiddleware(Config{
		ScopedAPIKeys: []ScopedAPIKey{{Key: "runtime-key", Scopes: []string{"runtime"}}},
	})(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))

	request := httptest.NewRequest(http.MethodPost, "/api/v1/runtime/registry/demo-main/heartbeat", bytes.NewReader([]byte(`{}`)))
	request.Header.Set("X-API-Key", "runtime-key")

	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusNoContent {
		t.Fatalf("expected 204, got %d", recorder.Code)
	}
}

func TestMiddlewareAcceptsProxyScope(t *testing.T) {
	t.Parallel()

	handler := NewMiddleware(Config{
		ScopedAPIKeys: []ScopedAPIKey{{Key: "proxy-key", Scopes: []string{"proxy"}}},
	})(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))

	request := httptest.NewRequest(http.MethodGet, "/assistants/demo-main/api/v1/models", nil)
	request.Header.Set("X-API-Key", "proxy-key")

	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusNoContent {
		t.Fatalf("expected 204, got %d", recorder.Code)
	}
}

func TestMiddlewareAcceptsDebugScope(t *testing.T) {
	t.Parallel()

	handler := NewMiddleware(Config{
		ScopedAPIKeys: []ScopedAPIKey{{Key: "debug-key", Scopes: []string{"debug"}}},
	})(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))

	request := httptest.NewRequest(http.MethodGet, "/api/v1/metrics", nil)
	request.Header.Set("X-API-Key", "debug-key")

	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusNoContent {
		t.Fatalf("expected 204, got %d", recorder.Code)
	}
}

func sign(clientID string, timestamp string, nonce string, method string, target string, body []byte, secret string) string {
	bodyHash := sha256.Sum256(body)
	mac := hmac.New(sha256.New, []byte(secret))
	_, _ = mac.Write([]byte(clientID + "." + timestamp + "." + nonce + "." + method + "." + target + "." + hex.EncodeToString(bodyHash[:])))
	return hex.EncodeToString(mac.Sum(nil))
}

type failingReadCloser struct{}

func (failingReadCloser) Read([]byte) (int, error) {
	return 0, errors.New("body should not be read")
}

func (failingReadCloser) Close() error {
	return nil
}

type recordingNonceStore struct {
	calls int
	used  map[string]struct{}
}

func (s *recordingNonceStore) UseNonce(_ context.Context, clientID string, nonce string, _ time.Time) (bool, error) {
	s.calls++
	key := clientID + "\x00" + nonce
	if _, ok := s.used[key]; ok {
		return false, nil
	}
	s.used[key] = struct{}{}
	return true, nil
}

type errNonceStore struct{}

func (errNonceStore) UseNonce(_ context.Context, _ string, _ string, _ time.Time) (bool, error) {
	return false, errors.New("nonce store unavailable")
}

// TestMiddlewareLogsGatewayInternalErrorOn500 covers the SOLE auth arm that flips
// the fault domain UPSTREAM->GATEWAY: a nonce-store failure is the gateway's own
// internal error (middleware.go:242), not the caller's fault. recordingNonceStore
// always succeeds, so the 500/GATEWAY branch was otherwise unexercised — a
// regression mislabeling it UPSTREAM (blaming the caller for a gateway bug) would
// have shipped green.
func TestMiddlewareLogsGatewayInternalErrorOn500(t *testing.T) {
	t.Parallel()
	var logged bytes.Buffer
	logger := slog.New(slog.NewJSONHandler(&logged, &slog.HandlerOptions{Level: slog.LevelWarn}))
	timestamp := strconv.FormatInt(time.Now().Unix(), 10)
	nonce := "nonce-500"
	clientID := "c500"
	body := []byte(`{}`)
	signature := sign(clientID, timestamp, nonce, http.MethodPost, "/api/v1/chat", body, "secret-key")

	handler := NewMiddleware(Config{
		APIKeys:     []string{"dev-key"},
		HMACSecret:  "secret-key",
		AllowedSkew: 5 * time.Minute,
		NonceStore:  errNonceStore{},
		Logger:      logger,
	})(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))

	request := httptest.NewRequest(http.MethodPost, "/api/v1/chat", bytes.NewReader(body))
	request.Header.Set("X-API-Key", "dev-key")
	request.Header.Set("X-Client-ID", clientID)
	request.Header.Set("X-Timestamp", timestamp)
	request.Header.Set("X-Nonce", nonce)
	request.Header.Set("X-Signature", signature)

	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", recorder.Code)
	}
	out := logged.String()
	for _, want := range []string{"gateway_auth_rejected", `"fault_domain":"GATEWAY"`, "gateway.internal_error"} {
		if !strings.Contains(out, want) {
			t.Errorf("500 auth log missing %q\nlog: %s", want, out)
		}
	}
}
