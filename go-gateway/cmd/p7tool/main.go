package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

func main() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}

	var err error
	switch os.Args[1] {
	case "runtime-seed":
		err = runRuntimeSeed(os.Args[2:])
	case "chat-load":
		err = runChatLoad(os.Args[2:])
	case "callback-sim":
		err = runCallbackSim(os.Args[2:])
	default:
		usage()
		os.Exit(2)
	}
	if err != nil {
		log.Fatalf("p7tool failed: %v", err)
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, `usage:
  p7tool runtime-seed -gateway http://127.0.0.1:8080 -api-key KEY -client-id p7-runtime-seed -hmac-secret SECRET -count 2000
  p7tool chat-load -gateway http://127.0.0.1:8080 -api-key KEY -client-id p7-loadtest -hmac-secret SECRET -tasks 1000 -concurrency 50 -input-object-id OBJECT_ID
  p7tool callback-sim -listen :18081 -behavior ack|no_ack|status_500|timeout`)
}

type httpClient struct {
	baseURL    string
	apiKey     string
	clientID   string
	hmacSecret string
	client     *http.Client
}

func newHTTPClient(baseURL string, apiKey string, clientID string, hmacSecret string, timeout time.Duration) httpClient {
	return httpClient{
		baseURL:    strings.TrimRight(strings.TrimSpace(baseURL), "/"),
		apiKey:     strings.TrimSpace(apiKey),
		clientID:   strings.TrimSpace(clientID),
		hmacSecret: strings.TrimSpace(hmacSecret),
		client:     &http.Client{Timeout: timeout},
	}
}

func (c httpClient) doJSON(ctx context.Context, method string, path string, payload any) (int, []byte, error) {
	var body io.Reader
	var raw []byte
	if payload != nil {
		var err error
		raw, err = json.Marshal(payload)
		if err != nil {
			return 0, nil, err
		}
		body = bytes.NewReader(raw)
	}
	request, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, body)
	if err != nil {
		return 0, nil, err
	}
	request.Header.Set("Accept", "application/json")
	if payload != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	if c.apiKey != "" {
		request.Header.Set("X-API-Key", c.apiKey)
	}
	if c.hmacSecret != "" {
		if err := c.sign(request, raw); err != nil {
			return 0, nil, err
		}
	}
	response, err := c.client.Do(request)
	if err != nil {
		return 0, nil, err
	}
	defer response.Body.Close()
	responseBody, err := io.ReadAll(response.Body)
	if err != nil {
		return response.StatusCode, nil, err
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return response.StatusCode, responseBody, fmt.Errorf("%s %s returned %d: %s", method, path, response.StatusCode, strings.TrimSpace(string(responseBody)))
	}
	return response.StatusCode, responseBody, nil
}

func (c httpClient) sign(request *http.Request, body []byte) error {
	if c.clientID == "" {
		return fmt.Errorf("-client-id is required when -hmac-secret is set")
	}
	nonce, err := randomHex(16)
	if err != nil {
		return err
	}
	timestamp := strconv.FormatInt(time.Now().Unix(), 10)
	bodyHash := sha256.Sum256(body)
	payload := strings.Join([]string{
		c.clientID,
		timestamp,
		nonce,
		strings.ToUpper(request.Method),
		request.URL.RequestURI(),
		hex.EncodeToString(bodyHash[:]),
	}, ".")
	mac := hmac.New(sha256.New, []byte(c.hmacSecret))
	_, _ = mac.Write([]byte(payload))
	request.Header.Set("X-Client-ID", c.clientID)
	request.Header.Set("X-Timestamp", timestamp)
	request.Header.Set("X-Nonce", nonce)
	request.Header.Set("X-Signature", hex.EncodeToString(mac.Sum(nil)))
	return nil
}

func randomHex(size int) (string, error) {
	raw := make([]byte, size)
	if _, err := rand.Read(raw); err != nil {
		return "", fmt.Errorf("generate nonce: %w", err)
	}
	return hex.EncodeToString(raw), nil
}

func runRuntimeSeed(args []string) error {
	flags := flag.NewFlagSet("runtime-seed", flag.ExitOnError)
	gateway := flags.String("gateway", "http://127.0.0.1:8080", "Go Gateway base URL")
	apiKey := flags.String("api-key", "", "API key with runtime scope")
	clientID := flags.String("client-id", "p7-runtime-seed", "X-Client-ID used for HMAC")
	hmacSecret := flags.String("hmac-secret", "", "HMAC secret for protected gateways; public gateways can leave it empty")
	count := flags.Int("count", 2000, "runtime count")
	concurrency := flags.Int("concurrency", 50, "concurrent heartbeat requests")
	capacity := flags.Int("capacity", 10, "runtime capacity")
	capabilities := flags.String("capabilities", "analysis,chat", "comma-separated capabilities")
	timeout := flags.Duration("timeout", 10*time.Second, "HTTP timeout")
	if err := flags.Parse(args); err != nil {
		return err
	}

	client := newHTTPClient(*gateway, *apiKey, *clientID, *hmacSecret, *timeout)
	jobs := make(chan int)
	var okCount atomic.Int64
	var failCount atomic.Int64
	var wg sync.WaitGroup
	startedAt := time.Now()
	for i := 0; i < *concurrency; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for index := range jobs {
				assistantID := fmt.Sprintf("loadtest-runtime-%04d", index)
				payload := map[string]any{
					"assistant_id":      assistantID,
					"runtime_id":        "p7-" + assistantID,
					"pod_name":          assistantID + "-pod",
					"status":            "ready",
					"capacity":          *capacity,
					"in_flight":         index % max(1, *capacity/2),
					"capabilities":      splitCSV(*capabilities),
					"drain_mode":        false,
					"last_error":        "",
					"last_heartbeat_at": time.Now().UTC().Format(time.RFC3339Nano),
				}
				_, _, err := client.doJSON(context.Background(), http.MethodPost, "/api/v1/runtime/registry/"+assistantID+"/heartbeat", payload)
				if err != nil {
					failCount.Add(1)
					log.Printf("heartbeat failed assistant_id=%s error=%v", assistantID, err)
					continue
				}
				okCount.Add(1)
			}
		}()
	}
	for i := 0; i < *count; i++ {
		jobs <- i
	}
	close(jobs)
	wg.Wait()

	elapsed := time.Since(startedAt)
	fmt.Printf("runtime_seed count=%d ok=%d failed=%d elapsed=%s rate=%.2f/s\n",
		*count,
		okCount.Load(),
		failCount.Load(),
		elapsed,
		float64(okCount.Load())/elapsed.Seconds(),
	)
	return nil
}

func runChatLoad(args []string) error {
	flags := flag.NewFlagSet("chat-load", flag.ExitOnError)
	gateway := flags.String("gateway", "http://127.0.0.1:8080", "Go Gateway base URL")
	apiKey := flags.String("api-key", "", "API key with public scope; empty for default public no-auth gateways")
	hmacSecret := flags.String("hmac-secret", "", "HMAC secret for protected gateways; public gateways can leave it empty")
	tasks := flags.Int("tasks", 1000, "task count")
	concurrency := flags.Int("concurrency", 50, "concurrent chat requests")
	clientID := flags.String("client-id", "p7-loadtest", "client_id and X-Client-ID used for HMAC")
	userID := flags.String("user-id", "", "user_id")
	sourceAssistantID := flags.String("source-assistant-id", "demo-main", "source_assistant_id")
	targetAssistantID := flags.String("target-assistant-id", "", "target_assistant_id; empty lets scheduler select")
	inputObjectID := flags.String("input-object-id", "", "input_object_id")
	approvalMode := flags.String("approval-mode", "none", "approval_mode")
	callbackURL := flags.String("callback-url", "", "callback_url")
	requiredCapabilities := flags.String("required-capabilities", "analysis", "comma-separated required_capabilities")
	timeout := flags.Duration("timeout", 20*time.Second, "HTTP timeout")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if strings.TrimSpace(*inputObjectID) == "" {
		return fmt.Errorf("-input-object-id is required")
	}

	client := newHTTPClient(*gateway, *apiKey, *clientID, *hmacSecret, *timeout)
	jobs := make(chan int)
	results := make(chan requestResult, *tasks)
	var wg sync.WaitGroup
	startedAt := time.Now()
	for i := 0; i < *concurrency; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for index := range jobs {
				requestStartedAt := time.Now()
				payload := map[string]any{
					"client_id":             *clientID,
					"request_id":            fmt.Sprintf("p7-load-%d-%06d", startedAt.UnixNano(), index),
					"user_id":               strings.TrimSpace(*userID),
					"source_assistant_id":   strings.TrimSpace(*sourceAssistantID),
					"target_assistant_id":   strings.TrimSpace(*targetAssistantID),
					"input_object_id":       strings.TrimSpace(*inputObjectID),
					"instruction":           fmt.Sprintf("P7 load test task %d", index),
					"approval_mode":         strings.TrimSpace(*approvalMode),
					"callback_url":          strings.TrimSpace(*callbackURL),
					"required_capabilities": splitCSV(*requiredCapabilities),
				}
				statusCode, raw, err := client.doJSON(context.Background(), http.MethodPost, "/api/v1/chat", payload)
				results <- requestResult{
					StatusCode: statusCode,
					Latency:    time.Since(requestStartedAt),
					Error:      err,
					Body:       string(raw),
				}
			}
		}()
	}
	for i := 0; i < *tasks; i++ {
		jobs <- i
	}
	close(jobs)
	wg.Wait()
	close(results)

	summary := summarizeResults(results)
	elapsed := time.Since(startedAt)
	fmt.Printf("chat_load tasks=%d ok=%d failed=%d elapsed=%s qps=%.2f avg=%s p50=%s p95=%s p99=%s\n",
		*tasks,
		summary.OK,
		summary.Failed,
		elapsed,
		float64(summary.OK)/elapsed.Seconds(),
		summary.Avg,
		summary.P50,
		summary.P95,
		summary.P99,
	)
	for statusCode, count := range summary.ByStatus {
		fmt.Printf("status code=%d count=%d\n", statusCode, count)
	}
	if summary.FirstError != "" {
		fmt.Printf("first_error=%s\n", summary.FirstError)
	}
	printEndpoint(client, "/api/v1/task-queue")
	return nil
}

type requestResult struct {
	StatusCode int
	Latency    time.Duration
	Error      error
	Body       string
}

type resultSummary struct {
	OK         int
	Failed     int
	ByStatus   map[int]int
	Avg        time.Duration
	P50        time.Duration
	P95        time.Duration
	P99        time.Duration
	FirstError string
}

func summarizeResults(results <-chan requestResult) resultSummary {
	summary := resultSummary{ByStatus: make(map[int]int)}
	var latencies []time.Duration
	var total time.Duration
	for result := range results {
		summary.ByStatus[result.StatusCode]++
		if result.Error != nil {
			summary.Failed++
			if summary.FirstError == "" {
				summary.FirstError = result.Error.Error()
			}
			continue
		}
		summary.OK++
		latencies = append(latencies, result.Latency)
		total += result.Latency
	}
	sort.Slice(latencies, func(i, j int) bool { return latencies[i] < latencies[j] })
	if len(latencies) > 0 {
		summary.Avg = total / time.Duration(len(latencies))
		summary.P50 = percentile(latencies, 50)
		summary.P95 = percentile(latencies, 95)
		summary.P99 = percentile(latencies, 99)
	}
	return summary
}

func percentile(values []time.Duration, percentile int) time.Duration {
	if len(values) == 0 {
		return 0
	}
	index := (len(values)*percentile + 99) / 100
	if index <= 0 {
		index = 1
	}
	if index > len(values) {
		index = len(values)
	}
	return values[index-1]
}

func printEndpoint(client httpClient, path string) {
	_, raw, err := client.doJSON(context.Background(), http.MethodGet, path, nil)
	if err != nil {
		log.Printf("GET %s failed: %v", path, err)
		return
	}
	fmt.Printf("%s=%s\n", path, strings.TrimSpace(string(raw)))
}

func runCallbackSim(args []string) error {
	flags := flag.NewFlagSet("callback-sim", flag.ExitOnError)
	listen := flags.String("listen", ":18081", "listen address")
	behavior := flags.String("behavior", "ack", "ack|no_ack|status_500|timeout")
	delay := flags.Duration("delay", 0, "extra response delay")
	if err := flags.Parse(args); err != nil {
		return err
	}
	var received atomic.Int64
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer r.Body.Close()
		raw, _ := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		count := received.Add(1)
		var event struct {
			TaskID string `json:"taskId"`
			Status string `json:"status"`
			Stage  string `json:"stage"`
		}
		_ = json.Unmarshal(raw, &event)
		log.Printf("callback count=%d task_id=%s status=%s stage=%s", count, event.TaskID, event.Status, event.Stage)
		if *delay > 0 {
			time.Sleep(*delay)
		}
		switch *behavior {
		case "ack":
			writeJSON(w, http.StatusOK, map[string]any{
				"received": true,
				"taskId":   event.TaskID,
				"handled":  true,
			})
		case "no_ack":
			writeJSON(w, http.StatusOK, map[string]any{"received": true})
		case "status_500":
			writeJSON(w, http.StatusInternalServerError, map[string]any{"error": "injected status_500"})
		case "timeout":
			time.Sleep(10 * time.Minute)
		default:
			writeJSON(w, http.StatusBadRequest, map[string]any{"error": "unknown behavior"})
		}
	})
	log.Printf("callback simulator listening addr=%s behavior=%s", *listen, *behavior)
	return http.ListenAndServe(*listen, handler)
}

func writeJSON(w http.ResponseWriter, statusCode int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(statusCode)
	_ = json.NewEncoder(w).Encode(payload)
}

func splitCSV(raw string) []string {
	parts := strings.Split(raw, ",")
	out := make([]string, 0, len(parts))
	for _, part := range parts {
		trimmed := strings.TrimSpace(part)
		if trimmed != "" {
			out = append(out, trimmed)
		}
	}
	return out
}

func max(left int, right int) int {
	if left > right {
		return left
	}
	return right
}
