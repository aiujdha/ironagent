package presign

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/url"
	"strings"
	"sync"
	"time"
)

type Config struct {
	Secret string
	TTL    time.Duration
}

type UploadRequest struct {
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

type DownloadRequest struct {
	FileID      string `json:"file_id"`
	Disposition string `json:"disposition"`
}

type URLResponse struct {
	URL       string    `json:"url"`
	Method    string    `json:"method"`
	ExpiresAt time.Time `json:"expires_at"`
}

type Manager struct {
	secret    string
	ttl       time.Duration
	mu        sync.Mutex
	uploads   map[string]UploadRequest
	downloads map[string]DownloadRequest
	expires   map[string]time.Time
}

func NewManager(cfg Config) *Manager {
	ttl := cfg.TTL
	if ttl <= 0 {
		ttl = 15 * time.Minute
	}
	secret := strings.TrimSpace(cfg.Secret)
	if secret == "" {
		secret = randomHex(32)
	}
	return &Manager{
		secret:    secret,
		ttl:       ttl,
		uploads:   make(map[string]UploadRequest),
		downloads: make(map[string]DownloadRequest),
		expires:   make(map[string]time.Time),
	}
}

func (m *Manager) CreateUploadURL(baseURL string, request UploadRequest) (URLResponse, error) {
	if strings.TrimSpace(request.Filename) == "" {
		return URLResponse{}, fmt.Errorf("filename is required")
	}
	if strings.TrimSpace(request.AssistantID) == "" && strings.TrimSpace(request.UserID) == "" {
		return URLResponse{}, fmt.Errorf("assistant_id or user_id is required")
	}
	token, expiresAt := m.newToken("upload")
	m.mu.Lock()
	m.uploads[token] = request
	m.expires[token] = expiresAt
	m.mu.Unlock()
	return URLResponse{
		URL:       joinURL(baseURL, "/api/v1/files/presigned/upload/"+url.PathEscape(token)),
		Method:    "PUT",
		ExpiresAt: expiresAt,
	}, nil
}

func (m *Manager) CreateDownloadURL(baseURL string, request DownloadRequest) (URLResponse, error) {
	if strings.TrimSpace(request.FileID) == "" {
		return URLResponse{}, fmt.Errorf("file_id is required")
	}
	token, expiresAt := m.newToken("download")
	m.mu.Lock()
	m.downloads[token] = request
	m.expires[token] = expiresAt
	m.mu.Unlock()
	return URLResponse{
		URL:       joinURL(baseURL, "/api/v1/files/presigned/download/"+url.PathEscape(token)),
		Method:    "GET",
		ExpiresAt: expiresAt,
	}, nil
}

func (m *Manager) ResolveUpload(token string) (UploadRequest, error) {
	token = strings.TrimSpace(token)
	m.mu.Lock()
	defer m.mu.Unlock()
	if err := m.validateLocked(token, "upload"); err != nil {
		return UploadRequest{}, err
	}
	request := m.uploads[token]
	delete(m.uploads, token)
	delete(m.expires, token)
	return request, nil
}

func (m *Manager) ResolveDownload(token string) (DownloadRequest, error) {
	token = strings.TrimSpace(token)
	m.mu.Lock()
	defer m.mu.Unlock()
	if err := m.validateLocked(token, "download"); err != nil {
		return DownloadRequest{}, err
	}
	return m.downloads[token], nil
}

func (m *Manager) validateLocked(token string, kind string) error {
	expiresAt, ok := m.expires[token]
	if !ok {
		return fmt.Errorf("invalid presigned token")
	}
	if time.Now().UTC().After(expiresAt) {
		delete(m.uploads, token)
		delete(m.downloads, token)
		delete(m.expires, token)
		return fmt.Errorf("presigned token expired")
	}
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return fmt.Errorf("invalid presigned token")
	}
	base := kind + "." + parts[0] + "." + parts[1]
	if !hmac.Equal([]byte(parts[2]), []byte(m.sign(base))) {
		return fmt.Errorf("invalid presigned token")
	}
	return nil
}

func (m *Manager) newToken(kind string) (string, time.Time) {
	expiresAt := time.Now().UTC().Add(m.ttl)
	nonce := randomHex(16)
	expiry := fmt.Sprintf("%d", expiresAt.Unix())
	base := kind + "." + nonce + "." + expiry
	return nonce + "." + expiry + "." + m.sign(base), expiresAt
}

func (m *Manager) sign(base string) string {
	mac := hmac.New(sha256.New, []byte(m.secret))
	_, _ = mac.Write([]byte(base))
	return hex.EncodeToString(mac.Sum(nil))
}

func joinURL(baseURL string, path string) string {
	baseURL = strings.TrimRight(strings.TrimSpace(baseURL), "/")
	if baseURL == "" {
		return path
	}
	return baseURL + path
}

func randomHex(size int) string {
	raw := make([]byte, size)
	if _, err := rand.Read(raw); err != nil {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(raw)
}
