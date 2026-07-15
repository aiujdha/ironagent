package metrics

import (
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

var global = newRegistry()

var latencyBuckets = []float64{0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10}

type registry struct {
	mu           sync.Mutex
	taskCreate   map[taskCreateKey]*timedCounter
	southbound   map[southboundKey]*timedCounter
	scheduler    map[schedulerKey]*schedulerCounter
	webhook      map[webhookKey]int64
	files        map[fileKey]*byteCounter
	proxy        map[runtimeProxyKey]*timedCounter
	proxyGauge   map[string]int64
	proxyCircuit map[string]int64
}

type timedCounter struct {
	Count          int64
	Errors         int64
	LatencySum     time.Duration
	LatencyMax     time.Duration
	LastStatusCode int
	Buckets        []int64
}

type schedulerCounter struct {
	Count         int64
	CandidateSum  int64
	LatencySum    time.Duration
	LatencyMax    time.Duration
	LastCandidate int
	Buckets       []int64
}

type byteCounter struct {
	Count int64
	Bytes int64
}

type southboundKey struct {
	Component   string
	Method      string
	Path        string
	StatusClass string
}

type taskCreateKey struct {
	Result string
	Status string
	Stage  string
}

type schedulerKey struct {
	Outcome string
	Reason  string
}

type webhookKey struct {
	Result    string
	EventType string
}

type fileKey struct {
	Direction string
}

type runtimeProxyKey struct {
	Kind        string
	StatusClass string
}

type SouthboundMetric struct {
	Component         string
	Method            string
	Path              string
	StatusClass       string
	Requests          int64
	Errors            int64
	LatencySumSeconds float64
	LatencyMaxSeconds float64
	LastStatusCode    int
	Buckets           []BucketMetric
}

type SchedulerMetric struct {
	Outcome            string
	Reason             string
	Count              int64
	CandidateSum       int64
	LatencySumSeconds  float64
	LatencyMaxSeconds  float64
	LastCandidateCount int
	Buckets            []BucketMetric
}

type TaskCreateMetric struct {
	Result            string
	Status            string
	Stage             string
	Requests          int64
	Errors            int64
	LatencySumSeconds float64
	LatencyMaxSeconds float64
	Buckets           []BucketMetric
}

type BucketMetric struct {
	LE    string
	Count int64
}

type WebhookMetric struct {
	Result    string
	EventType string
	Count     int64
}

type FileTransferMetric struct {
	Direction string
	Count     int64
	Bytes     int64
}

type RuntimeProxyMetric struct {
	Kind              string
	StatusClass       string
	Requests          int64
	Errors            int64
	LatencySumSeconds float64
	LatencyMaxSeconds float64
	LastStatusCode    int
	Buckets           []BucketMetric
}

type RuntimeProxyInFlightMetric struct {
	Kind     string
	InFlight int64
}

type RuntimeProxyCircuitMetric struct {
	Kind  string
	Count int64
}

func newRegistry() *registry {
	return &registry{
		taskCreate:   make(map[taskCreateKey]*timedCounter),
		southbound:   make(map[southboundKey]*timedCounter),
		scheduler:    make(map[schedulerKey]*schedulerCounter),
		webhook:      make(map[webhookKey]int64),
		files:        make(map[fileKey]*byteCounter),
		proxy:        make(map[runtimeProxyKey]*timedCounter),
		proxyGauge:   make(map[string]int64),
		proxyCircuit: make(map[string]int64),
	}
}

func ResetForTest() {
	global.mu.Lock()
	defer global.mu.Unlock()
	global.taskCreate = make(map[taskCreateKey]*timedCounter)
	global.southbound = make(map[southboundKey]*timedCounter)
	global.scheduler = make(map[schedulerKey]*schedulerCounter)
	global.webhook = make(map[webhookKey]int64)
	global.files = make(map[fileKey]*byteCounter)
	global.proxy = make(map[runtimeProxyKey]*timedCounter)
	global.proxyGauge = make(map[string]int64)
	global.proxyCircuit = make(map[string]int64)
}

func RecordTaskCreate(result string, status string, stage string, latency time.Duration, failed bool) {
	key := taskCreateKey{
		Result: normalizeLabel(result, "unknown"),
		Status: normalizeLabel(status, "unknown"),
		Stage:  normalizeLabel(stage, "unknown"),
	}
	global.mu.Lock()
	defer global.mu.Unlock()
	counter := global.taskCreate[key]
	if counter == nil {
		counter = newTimedCounter()
		global.taskCreate[key] = counter
	}
	recordTimedCounter(counter, latency, 0, failed)
}

func TaskCreateSnapshot() []TaskCreateMetric {
	global.mu.Lock()
	defer global.mu.Unlock()
	out := make([]TaskCreateMetric, 0, len(global.taskCreate))
	for key, counter := range global.taskCreate {
		out = append(out, TaskCreateMetric{
			Result:            key.Result,
			Status:            key.Status,
			Stage:             key.Stage,
			Requests:          counter.Count,
			Errors:            counter.Errors,
			LatencySumSeconds: counter.LatencySum.Seconds(),
			LatencyMaxSeconds: counter.LatencyMax.Seconds(),
			Buckets:           bucketSnapshot(counter.Buckets, counter.Count),
		})
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Result != out[j].Result {
			return out[i].Result < out[j].Result
		}
		if out[i].Status != out[j].Status {
			return out[i].Status < out[j].Status
		}
		return out[i].Stage < out[j].Stage
	})
	return out
}

func RecordSouthbound(component string, method string, path string, statusCode int, latency time.Duration, failed bool) {
	key := southboundKey{
		Component:   normalizeLabel(component, "unknown"),
		Method:      strings.ToUpper(normalizeLabel(method, "UNKNOWN")),
		Path:        normalizeLabel(path, "unknown"),
		StatusClass: statusClass(statusCode, failed),
	}
	global.mu.Lock()
	defer global.mu.Unlock()
	counter := global.southbound[key]
	if counter == nil {
		counter = newTimedCounter()
		global.southbound[key] = counter
	}
	recordTimedCounter(counter, latency, statusCode, failed)
}

func SouthboundSnapshot() []SouthboundMetric {
	global.mu.Lock()
	defer global.mu.Unlock()
	out := make([]SouthboundMetric, 0, len(global.southbound))
	for key, counter := range global.southbound {
		out = append(out, SouthboundMetric{
			Component:         key.Component,
			Method:            key.Method,
			Path:              key.Path,
			StatusClass:       key.StatusClass,
			Requests:          counter.Count,
			Errors:            counter.Errors,
			LatencySumSeconds: counter.LatencySum.Seconds(),
			LatencyMaxSeconds: counter.LatencyMax.Seconds(),
			LastStatusCode:    counter.LastStatusCode,
			Buckets:           bucketSnapshot(counter.Buckets, counter.Count),
		})
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Component != out[j].Component {
			return out[i].Component < out[j].Component
		}
		if out[i].Method != out[j].Method {
			return out[i].Method < out[j].Method
		}
		if out[i].Path != out[j].Path {
			return out[i].Path < out[j].Path
		}
		return out[i].StatusClass < out[j].StatusClass
	})
	return out
}

func RecordScheduler(outcome string, reason string, candidateCount int, latency time.Duration) {
	key := schedulerKey{
		Outcome: normalizeLabel(outcome, "unknown"),
		Reason:  normalizeLabel(reason, "unknown"),
	}
	global.mu.Lock()
	defer global.mu.Unlock()
	counter := global.scheduler[key]
	if counter == nil {
		counter = &schedulerCounter{Buckets: make([]int64, len(latencyBuckets))}
		global.scheduler[key] = counter
	}
	counter.Count++
	counter.CandidateSum += int64(candidateCount)
	counter.LatencySum += latency
	if latency > counter.LatencyMax {
		counter.LatencyMax = latency
	}
	counter.LastCandidate = candidateCount
	recordBuckets(counter.Buckets, latency)
}

func SchedulerSnapshot() []SchedulerMetric {
	global.mu.Lock()
	defer global.mu.Unlock()
	out := make([]SchedulerMetric, 0, len(global.scheduler))
	for key, counter := range global.scheduler {
		out = append(out, SchedulerMetric{
			Outcome:            key.Outcome,
			Reason:             key.Reason,
			Count:              counter.Count,
			CandidateSum:       counter.CandidateSum,
			LatencySumSeconds:  counter.LatencySum.Seconds(),
			LatencyMaxSeconds:  counter.LatencyMax.Seconds(),
			LastCandidateCount: counter.LastCandidate,
			Buckets:            bucketSnapshot(counter.Buckets, counter.Count),
		})
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Outcome != out[j].Outcome {
			return out[i].Outcome < out[j].Outcome
		}
		return out[i].Reason < out[j].Reason
	})
	return out
}

func newTimedCounter() *timedCounter {
	return &timedCounter{Buckets: make([]int64, len(latencyBuckets))}
}

func recordTimedCounter(counter *timedCounter, latency time.Duration, statusCode int, failed bool) {
	counter.Count++
	if failed {
		counter.Errors++
	}
	counter.LatencySum += latency
	if latency > counter.LatencyMax {
		counter.LatencyMax = latency
	}
	counter.LastStatusCode = statusCode
	recordBuckets(counter.Buckets, latency)
}

func recordBuckets(buckets []int64, latency time.Duration) {
	seconds := latency.Seconds()
	for i, upper := range latencyBuckets {
		if seconds <= upper {
			buckets[i]++
		}
	}
}

func bucketSnapshot(values []int64, count int64) []BucketMetric {
	out := make([]BucketMetric, 0, len(values)+1)
	for i, value := range values {
		out = append(out, BucketMetric{
			LE:    trimFloat(latencyBuckets[i]),
			Count: value,
		})
	}
	out = append(out, BucketMetric{LE: "+Inf", Count: count})
	return out
}

func trimFloat(value float64) string {
	raw := strconv.FormatFloat(value, 'f', -1, 64)
	return raw
}

func RecordWebhook(result string, eventType string) {
	key := webhookKey{
		Result:    normalizeLabel(result, "unknown"),
		EventType: normalizeLabel(eventType, "unknown"),
	}
	global.mu.Lock()
	defer global.mu.Unlock()
	global.webhook[key]++
}

func WebhookSnapshot() []WebhookMetric {
	global.mu.Lock()
	defer global.mu.Unlock()
	out := make([]WebhookMetric, 0, len(global.webhook))
	for key, count := range global.webhook {
		out = append(out, WebhookMetric{
			Result:    key.Result,
			EventType: key.EventType,
			Count:     count,
		})
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Result != out[j].Result {
			return out[i].Result < out[j].Result
		}
		return out[i].EventType < out[j].EventType
	})
	return out
}

func RecordFileTransfer(direction string, bytes int64) {
	if bytes < 0 {
		bytes = 0
	}
	key := fileKey{Direction: normalizeLabel(direction, "unknown")}
	global.mu.Lock()
	defer global.mu.Unlock()
	counter := global.files[key]
	if counter == nil {
		counter = &byteCounter{}
		global.files[key] = counter
	}
	counter.Count++
	counter.Bytes += bytes
}

func FileTransferSnapshot() []FileTransferMetric {
	global.mu.Lock()
	defer global.mu.Unlock()
	out := make([]FileTransferMetric, 0, len(global.files))
	for key, counter := range global.files {
		out = append(out, FileTransferMetric{
			Direction: key.Direction,
			Count:     counter.Count,
			Bytes:     counter.Bytes,
		})
	}
	sort.Slice(out, func(i, j int) bool {
		return out[i].Direction < out[j].Direction
	})
	return out
}

func RecordRuntimeProxy(kind string, statusCode int, latency time.Duration, failed bool) {
	key := runtimeProxyKey{
		Kind:        normalizeLabel(kind, "unknown"),
		StatusClass: statusClass(statusCode, failed),
	}
	global.mu.Lock()
	defer global.mu.Unlock()
	counter := global.proxy[key]
	if counter == nil {
		counter = newTimedCounter()
		global.proxy[key] = counter
	}
	recordTimedCounter(counter, latency, statusCode, failed)
}

func RuntimeProxySnapshot() []RuntimeProxyMetric {
	global.mu.Lock()
	defer global.mu.Unlock()
	out := make([]RuntimeProxyMetric, 0, len(global.proxy))
	for key, counter := range global.proxy {
		out = append(out, RuntimeProxyMetric{
			Kind:              key.Kind,
			StatusClass:       key.StatusClass,
			Requests:          counter.Count,
			Errors:            counter.Errors,
			LatencySumSeconds: counter.LatencySum.Seconds(),
			LatencyMaxSeconds: counter.LatencyMax.Seconds(),
			LastStatusCode:    counter.LastStatusCode,
			Buckets:           bucketSnapshot(counter.Buckets, counter.Count),
		})
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Kind != out[j].Kind {
			return out[i].Kind < out[j].Kind
		}
		return out[i].StatusClass < out[j].StatusClass
	})
	return out
}

func AddRuntimeProxyInFlight(kind string, delta int64) {
	key := normalizeLabel(kind, "unknown")
	global.mu.Lock()
	defer global.mu.Unlock()
	next := global.proxyGauge[key] + delta
	if next <= 0 {
		delete(global.proxyGauge, key)
		return
	}
	global.proxyGauge[key] = next
}

func RuntimeProxyInFlightSnapshot() []RuntimeProxyInFlightMetric {
	global.mu.Lock()
	defer global.mu.Unlock()
	out := make([]RuntimeProxyInFlightMetric, 0, len(global.proxyGauge))
	for kind, inFlight := range global.proxyGauge {
		out = append(out, RuntimeProxyInFlightMetric{
			Kind:     kind,
			InFlight: inFlight,
		})
	}
	sort.Slice(out, func(i, j int) bool {
		return out[i].Kind < out[j].Kind
	})
	return out
}

func RecordRuntimeProxyCircuitOpen(kind string) {
	key := normalizeLabel(kind, "unknown")
	global.mu.Lock()
	defer global.mu.Unlock()
	global.proxyCircuit[key]++
}

func RuntimeProxyCircuitSnapshot() []RuntimeProxyCircuitMetric {
	global.mu.Lock()
	defer global.mu.Unlock()
	out := make([]RuntimeProxyCircuitMetric, 0, len(global.proxyCircuit))
	for kind, count := range global.proxyCircuit {
		out = append(out, RuntimeProxyCircuitMetric{
			Kind:  kind,
			Count: count,
		})
	}
	sort.Slice(out, func(i, j int) bool {
		return out[i].Kind < out[j].Kind
	})
	return out
}

func statusClass(statusCode int, failed bool) string {
	if statusCode <= 0 {
		if failed {
			return "error"
		}
		return "unknown"
	}
	return string(rune('0'+statusCode/100)) + "xx"
}

func normalizeLabel(value string, fallback string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return fallback
	}
	return value
}
