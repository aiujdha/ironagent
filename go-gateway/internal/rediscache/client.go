package rediscache

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"

	"ironagent/go-gateway/internal/core"
)

type Config struct {
	Mode                     string
	Addr                     string
	SentinelAddrs            []string
	SentinelMaster           string
	Username                 string
	Password                 string
	DB                       int
	KeyPrefix                string
	Timeout                  time.Duration
	MaxOpenConns             int
	MaxIdleConns             int
	AssistantResolutionTTL   time.Duration
	AssistantAvailabilityTTL time.Duration
	ChatRateLimitMax         int
	ChatRateLimitWindow      time.Duration
	ChatRateLimitFailClosed  bool
}

type Client struct {
	client                   redis.UniversalClient
	keyPrefix                string
	assistantResolutionTTL   time.Duration
	assistantAvailabilityTTL time.Duration
	chatRateLimitMax         int
	chatRateLimitWindow      time.Duration
	chatRateLimitFailClosed  bool
}

func New(ctx context.Context, cfg Config) (*Client, error) {
	mode := strings.ToLower(strings.TrimSpace(cfg.Mode))
	if mode == "" {
		mode = "standalone"
	}
	timeout := cfg.Timeout
	if timeout <= 0 {
		timeout = 500 * time.Millisecond
	}
	maxOpen := cfg.MaxOpenConns
	if maxOpen <= 0 {
		maxOpen = 16
	}
	maxIdle := cfg.MaxIdleConns
	if maxIdle <= 0 || maxIdle > maxOpen {
		maxIdle = maxOpen
	}

	var redisClient redis.UniversalClient
	switch mode {
	case "standalone":
		addr := strings.TrimSpace(cfg.Addr)
		if addr == "" {
			return nil, errors.New("redis addr is required")
		}
		redisClient = redis.NewClient(&redis.Options{
			Addr:                  addr,
			Username:              strings.TrimSpace(cfg.Username),
			Password:              cfg.Password,
			DB:                    cfg.DB,
			Protocol:              2,
			DialTimeout:           timeout,
			ReadTimeout:           timeout,
			WriteTimeout:          timeout,
			ContextTimeoutEnabled: true,
			PoolSize:              maxOpen,
			MaxActiveConns:        maxOpen,
			MaxIdleConns:          maxIdle,
			PoolTimeout:           timeout,
		})
	case "sentinel":
		sentinelAddrs := compactStrings(cfg.SentinelAddrs)
		sentinelMaster := strings.TrimSpace(cfg.SentinelMaster)
		if len(sentinelAddrs) == 0 {
			return nil, errors.New("redis sentinel addrs are required")
		}
		if sentinelMaster == "" {
			return nil, errors.New("redis sentinel master is required")
		}
		redisClient = redis.NewFailoverClient(&redis.FailoverOptions{
			MasterName:            sentinelMaster,
			SentinelAddrs:         sentinelAddrs,
			SentinelPassword:      cfg.Password,
			Username:              strings.TrimSpace(cfg.Username),
			Password:              cfg.Password,
			DB:                    cfg.DB,
			Protocol:              2,
			DialTimeout:           timeout,
			ReadTimeout:           timeout,
			WriteTimeout:          timeout,
			ContextTimeoutEnabled: true,
			PoolSize:              maxOpen,
			MaxActiveConns:        maxOpen,
			MaxIdleConns:          maxIdle,
			PoolTimeout:           timeout,
		})
	default:
		return nil, fmt.Errorf("unsupported redis mode %q", mode)
	}

	keyPrefix := strings.TrimSpace(cfg.KeyPrefix)
	if keyPrefix != "" && !strings.HasSuffix(keyPrefix, ":") {
		keyPrefix += ":"
	}
	client := &Client{
		client:                   redisClient,
		keyPrefix:                keyPrefix,
		assistantResolutionTTL:   cfg.AssistantResolutionTTL,
		assistantAvailabilityTTL: cfg.AssistantAvailabilityTTL,
		chatRateLimitMax:         cfg.ChatRateLimitMax,
		chatRateLimitWindow:      cfg.ChatRateLimitWindow,
		chatRateLimitFailClosed:  cfg.ChatRateLimitFailClosed,
	}
	if client.assistantResolutionTTL <= 0 {
		client.assistantResolutionTTL = 60 * time.Second
	}
	if client.assistantAvailabilityTTL <= 0 {
		client.assistantAvailabilityTTL = 30 * time.Second
	}
	if client.chatRateLimitWindow <= 0 {
		client.chatRateLimitWindow = time.Second
	}
	if err := client.Ping(ctx); err != nil {
		_ = client.Close()
		return nil, err
	}
	return client, nil
}

func (c *Client) Close() error {
	return c.client.Close()
}

func (c *Client) Ping(ctx context.Context) error {
	reply, err := c.client.Ping(ctx).Result()
	if err != nil {
		return err
	}
	if strings.ToUpper(reply) != "PONG" {
		return fmt.Errorf("unexpected redis ping reply %q", reply)
	}
	return nil
}

func (c *Client) GetAssistantForUser(ctx context.Context, userID string) (string, bool, error) {
	userID = strings.TrimSpace(userID)
	if userID == "" {
		return "", false, nil
	}
	value, err := c.client.Get(ctx, c.key("assistant_binding", userID)).Result()
	if errors.Is(err, redis.Nil) {
		return "", false, nil
	}
	if err != nil {
		return "", false, err
	}
	value = strings.TrimSpace(value)
	if value == "" {
		return "", false, nil
	}
	return value, true, nil
}

func (c *Client) SetAssistantForUser(ctx context.Context, userID string, assistantID string) error {
	userID = strings.TrimSpace(userID)
	assistantID = strings.TrimSpace(assistantID)
	if userID == "" || assistantID == "" {
		return nil
	}
	return c.setWithTTL(ctx, c.key("assistant_binding", userID), assistantID, c.assistantResolutionTTL)
}

func (c *Client) GetAssistantAvailable(ctx context.Context, assistantID string) (bool, bool, error) {
	assistantID = strings.TrimSpace(assistantID)
	if assistantID == "" {
		return false, false, nil
	}
	value, err := c.client.Get(ctx, c.key("assistant_available", assistantID)).Result()
	if errors.Is(err, redis.Nil) {
		return false, false, nil
	}
	if err != nil {
		return false, false, err
	}
	switch strings.TrimSpace(value) {
	case "1":
		return true, true, nil
	case "0":
		return false, true, nil
	default:
		return false, false, nil
	}
}

func (c *Client) SetAssistantAvailable(ctx context.Context, assistantID string, available bool) error {
	assistantID = strings.TrimSpace(assistantID)
	if assistantID == "" {
		return nil
	}
	value := "0"
	if available {
		value = "1"
	}
	return c.setWithTTL(ctx, c.key("assistant_available", assistantID), value, c.assistantAvailabilityTTL)
}

func (c *Client) LoadTaskProgress(ctx context.Context, taskID string, attemptToken string) (core.TaskProgressState, bool, error) {
	taskID = strings.TrimSpace(taskID)
	attemptToken = strings.TrimSpace(attemptToken)
	if taskID == "" || attemptToken == "" {
		return core.TaskProgressState{}, false, nil
	}
	value, err := c.client.Get(ctx, c.key("task_progress", taskID, attemptToken)).Result()
	if errors.Is(err, redis.Nil) {
		return core.TaskProgressState{}, false, nil
	}
	if err != nil {
		return core.TaskProgressState{}, false, err
	}
	var state core.TaskProgressState
	if err := json.Unmarshal([]byte(value), &state); err != nil {
		return core.TaskProgressState{}, false, err
	}
	return state, true, nil
}

func (c *Client) SaveTaskProgress(ctx context.Context, taskID string, attemptToken string, state core.TaskProgressState, ttl time.Duration) error {
	taskID = strings.TrimSpace(taskID)
	attemptToken = strings.TrimSpace(attemptToken)
	if taskID == "" || attemptToken == "" {
		return nil
	}
	if ttl <= 0 {
		ttl = 30 * time.Minute
	}
	payload, err := json.Marshal(state)
	if err != nil {
		return err
	}
	return c.client.Set(ctx, c.key("task_progress", taskID, attemptToken), string(payload), ttl).Err()
}

func (c *Client) DeleteTaskProgress(ctx context.Context, taskID string, attemptToken string) error {
	taskID = strings.TrimSpace(taskID)
	attemptToken = strings.TrimSpace(attemptToken)
	if taskID == "" || attemptToken == "" {
		return nil
	}
	return c.client.Del(ctx, c.key("task_progress", taskID, attemptToken)).Err()
}

func (c *Client) Allow(ctx context.Context, key string) (bool, error) {
	if c.chatRateLimitMax <= 0 {
		return true, nil
	}
	key = strings.TrimSpace(key)
	if key == "" {
		key = "anonymous"
	}
	window := c.chatRateLimitWindow
	if window <= 0 {
		window = time.Second
	}
	bucket := time.Now().UTC().UnixNano() / int64(window)
	redisKey := c.key("rate", "chat", safeKeyPart(key), strconv.FormatInt(bucket, 10))
	count, err := c.client.Eval(
		ctx,
		rateLimitScript,
		[]string{redisKey},
		int64((window+time.Second)/time.Millisecond),
	).Int64()
	if err != nil {
		if c.chatRateLimitFailClosed {
			return false, err
		}
		return true, nil
	}
	return count <= int64(c.chatRateLimitMax), nil
}

const rateLimitScript = `
local count = redis.call("INCR", KEYS[1])
if count == 1 then
  redis.call("PEXPIRE", KEYS[1], ARGV[1])
end
return count
`

func (c *Client) setWithTTL(ctx context.Context, key string, value string, ttl time.Duration) error {
	return c.client.Set(ctx, key, value, ttl).Err()
}

func (c *Client) key(parts ...string) string {
	out := strings.Builder{}
	out.WriteString(c.keyPrefix)
	for i, part := range parts {
		if i > 0 {
			out.WriteByte(':')
		}
		out.WriteString(safeKeyPart(part))
	}
	return out.String()
}

func compactStrings(values []string) []string {
	out := make([]string, 0, len(values))
	for _, value := range values {
		trimmed := strings.TrimSpace(value)
		if trimmed != "" {
			out = append(out, trimmed)
		}
	}
	return out
}

func safeKeyPart(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return "empty"
	}
	safe := len(raw) <= 96
	for _, r := range raw {
		switch {
		case r >= 'a' && r <= 'z':
		case r >= 'A' && r <= 'Z':
		case r >= '0' && r <= '9':
		case r == '-' || r == '_' || r == '.':
		default:
			safe = false
		}
	}
	if safe {
		return raw
	}
	sum := sha256.Sum256([]byte(raw))
	if len(raw) > 96 {
		return hex.EncodeToString(sum[:])
	}
	return "sha256." + hex.EncodeToString(sum[:])
}
