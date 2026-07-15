package runtimepool

import (
	"context"
	"fmt"
	"log/slog"
	"sort"
	"strings"
	"sync"
	"time"

	"ironagent/go-gateway/internal/core"
	"ironagent/go-gateway/internal/metrics"
)

type RuntimeRecord struct {
	RuntimeID       string     `json:"runtime_id,omitempty"`
	AssistantID     string     `json:"assistant_id"`
	PodName         string     `json:"pod_name,omitempty"`
	InternalBaseURL string     `json:"internal_base_url,omitempty"`
	GatewayURL      string     `json:"gateway_url,omitempty"`
	Status          string     `json:"status"`
	Capacity        int        `json:"capacity"`
	InFlight        int        `json:"in_flight"`
	Capabilities    []string   `json:"capabilities,omitempty"`
	DrainMode       bool       `json:"drain_mode"`
	LastHeartbeatAt *time.Time `json:"last_heartbeat_at,omitempty"`
	LastError       string     `json:"last_error,omitempty"`
	Version         int64      `json:"version"`
	UpdatedAt       time.Time  `json:"updated_at"`
}

type SchedulerDecision struct {
	SourceAssistantID    string               `json:"source_assistant_id,omitempty"`
	PreferredAssistantID string               `json:"preferred_assistant_id,omitempty"`
	RequiredCapabilities []string             `json:"required_capabilities,omitempty"`
	SelectedAssistantID  string               `json:"selected_assistant_id,omitempty"`
	Reason               string               `json:"reason"`
	Candidates           []SchedulerCandidate `json:"candidates,omitempty"`
	CreatedAt            time.Time            `json:"created_at"`
}

type SchedulerCandidate struct {
	AssistantID     string     `json:"assistant_id"`
	Status          string     `json:"status"`
	Capacity        int        `json:"capacity"`
	InFlight        int        `json:"in_flight"`
	DrainMode       bool       `json:"drain_mode"`
	Capabilities    []string   `json:"capabilities,omitempty"`
	LastHeartbeatAt *time.Time `json:"last_heartbeat_at,omitempty"`
	Eligible        bool       `json:"eligible"`
	Reason          string     `json:"reason"`
	LoadNumerator   int        `json:"load_numerator"`
	LoadDenominator int        `json:"load_denominator"`
}

type Store interface {
	UpsertRuntime(ctx context.Context, record RuntimeRecord) (RuntimeRecord, error)
	HeartbeatRuntime(ctx context.Context, record RuntimeRecord) (RuntimeRecord, error)
	GetRuntime(ctx context.Context, assistantID string) (RuntimeRecord, bool, error)
	ListRuntimes(ctx context.Context) ([]RuntimeRecord, error)
	DeleteRuntime(ctx context.Context, assistantID string) error
	ReserveRuntime(ctx context.Context, assistantID string, heartbeatTTL time.Duration) error
	ReleaseRuntime(ctx context.Context, assistantID string) error
	MarkExpired(ctx context.Context, heartbeatTTL time.Duration) (instances int, leases int, err error)
}

type RouteLeaseStore interface {
	ReserveRuntimeRoute(ctx context.Context, assistantID string, heartbeatTTL time.Duration) (RuntimeRecord, string, *time.Time, error)
	RenewRuntimeRouteLease(ctx context.Context, leaseID string, heartbeatTTL time.Duration) (*time.Time, error)
	ReleaseRuntimeRoute(ctx context.Context, leaseID string, assistantID string, runtimeID string) error
}

type Registry struct {
	mu              sync.Mutex
	defaultCapacity int
	heartbeatTTL    time.Duration
	store           Store
	records         map[string]RuntimeRecord
	lastDecision    SchedulerDecision
	logger          *slog.Logger
}

func NewRegistry(defaultCapacity int) *Registry {
	if defaultCapacity <= 0 {
		defaultCapacity = 1
	}
	return &Registry{
		defaultCapacity: defaultCapacity,
		records:         make(map[string]RuntimeRecord),
	}
}

func NewRegistryWithStore(defaultCapacity int, heartbeatTTL time.Duration, store Store, logger *slog.Logger) *Registry {
	registry := NewRegistry(defaultCapacity)
	registry.heartbeatTTL = heartbeatTTL
	registry.store = store
	registry.logger = logger
	return registry
}

func (r *Registry) logInfo(message string, attrs ...any) {
	if r.logger == nil {
		return
	}
	r.logger.Info(message, attrs...)
}

func (r *Registry) logWarn(message string, attrs ...any) {
	if r.logger == nil {
		return
	}
	r.logger.Warn(message, attrs...)
}

func (r *Registry) Upsert(record RuntimeRecord) (RuntimeRecord, error) {
	record = r.normalizeRecord(record, false)
	if r.store != nil {
		return r.store.UpsertRuntime(context.Background(), record)
	}
	return r.upsertMemory(record)
}

func (r *Registry) Heartbeat(record RuntimeRecord) (RuntimeRecord, error) {
	if r.store != nil {
		record = r.normalizeRecord(record, true)
		return r.store.HeartbeatRuntime(context.Background(), record)
	}
	return r.upsertHeartbeatMemory(record)
}

func (r *Registry) upsertMemory(record RuntimeRecord) (RuntimeRecord, error) {
	record.AssistantID = strings.TrimSpace(record.AssistantID)
	if record.AssistantID == "" {
		return RuntimeRecord{}, fmt.Errorf("assistant_id is required")
	}

	r.mu.Lock()
	defer r.mu.Unlock()
	existing, existed := r.records[record.AssistantID]
	if existed {
		record.InFlight = existing.InFlight
	}
	record.Version = r.records[record.AssistantID].Version + 1
	r.records[record.AssistantID] = record
	return record, nil
}

func (r *Registry) upsertHeartbeatMemory(record RuntimeRecord) (RuntimeRecord, error) {
	record.AssistantID = strings.TrimSpace(record.AssistantID)
	if record.AssistantID == "" {
		return RuntimeRecord{}, fmt.Errorf("assistant_id is required")
	}

	r.mu.Lock()
	defer r.mu.Unlock()
	existing, existed := r.records[record.AssistantID]
	record = r.normalizeRecord(record, true)
	if existed {
		record.InFlight = existing.InFlight
		record.Capacity = existing.Capacity
		if record.InternalBaseURL == "" {
			record.InternalBaseURL = existing.InternalBaseURL
		}
		if record.GatewayURL == "" {
			record.GatewayURL = existing.GatewayURL
		}
		if existing.LastError != "" && record.LastError == "" && !runtimeStatusAvailable(record.Status) {
			record.LastError = existing.LastError
		}
	}
	record.Version = existing.Version + 1
	r.records[record.AssistantID] = record
	return record, nil
}

func (r *Registry) Get(assistantID string) (RuntimeRecord, bool) {
	if r.store != nil {
		_ = r.expireStale(context.Background())
		record, ok, err := r.store.GetRuntime(context.Background(), strings.TrimSpace(assistantID))
		if err != nil {
			return RuntimeRecord{}, false
		}
		return record, ok
	}
	r.expireStaleMemory(time.Now().UTC())
	r.mu.Lock()
	defer r.mu.Unlock()
	record, ok := r.records[strings.TrimSpace(assistantID)]
	return record, ok
}

func (r *Registry) ResolveRuntimeRoute(ctx context.Context, assistantID string) (core.RuntimeRoute, bool, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	assistantID = strings.TrimSpace(assistantID)
	if assistantID == "" {
		return core.RuntimeRoute{}, false, nil
	}
	if r.store != nil {
		if err := r.expireStale(ctx); err != nil {
			return core.RuntimeRoute{}, false, err
		}
		record, ok, err := r.store.GetRuntime(ctx, assistantID)
		if err != nil || !ok {
			return core.RuntimeRoute{}, ok, err
		}
		return runtimeRouteFromRecord(record), true, nil
	}
	r.expireStaleMemory(time.Now().UTC())
	r.mu.Lock()
	record, ok := r.records[assistantID]
	r.mu.Unlock()
	if !ok {
		return core.RuntimeRoute{}, false, nil
	}
	return runtimeRouteFromRecord(record), true, nil
}

func (r *Registry) Snapshot() []RuntimeRecord {
	records, _ := r.SnapshotWithError(context.Background())
	return records
}

func (r *Registry) SnapshotWithError(ctx context.Context) ([]RuntimeRecord, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	if r.store != nil {
		if err := r.expireStale(ctx); err != nil {
			return nil, err
		}
		records, err := r.store.ListRuntimes(ctx)
		if err != nil {
			return nil, err
		}
		return records, nil
	}
	r.expireStaleMemory(time.Now().UTC())
	r.mu.Lock()
	defer r.mu.Unlock()
	records := make([]RuntimeRecord, 0, len(r.records))
	for _, record := range r.records {
		records = append(records, record)
	}
	sort.Slice(records, func(i, j int) bool {
		return records[i].AssistantID < records[j].AssistantID
	})
	return records, nil
}

func (r *Registry) Delete(assistantID string) error {
	assistantID = strings.TrimSpace(assistantID)
	if assistantID == "" {
		return nil
	}
	if r.store != nil {
		return r.store.DeleteRuntime(context.Background(), assistantID)
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	delete(r.records, assistantID)
	return nil
}

func (r *Registry) LastDecision() SchedulerDecision {
	r.mu.Lock()
	defer r.mu.Unlock()
	return copyDecision(r.lastDecision)
}

func (r *Registry) SelectTarget(_ context.Context, request core.RuntimeScheduleRequest) (string, error) {
	startedAt := time.Now()
	outcome := "rejected"
	preferred := strings.TrimSpace(request.PreferredAssistantID)
	source := strings.TrimSpace(request.SourceAssistantID)
	required := normalizeCapabilities(request.RequiredCapabilities)
	now := time.Now().UTC()
	decision := SchedulerDecision{
		SourceAssistantID:    source,
		PreferredAssistantID: preferred,
		RequiredCapabilities: required,
		CreatedAt:            now,
	}
	defer func() {
		metrics.RecordScheduler(outcome, decision.Reason, len(decision.Candidates), time.Since(startedAt))
	}()
	if preferred != "" {
		if source != "" && preferred == source {
			decision.Reason = "preferred_source_assistant"
			r.setLastDecision(decision)
			return "", fmt.Errorf("target assistant %q rejected by scheduler: source_assistant", preferred)
		}
		if record, ok := r.Get(preferred); ok {
			candidate := evaluateCandidate(record, source, required, now, r.heartbeatTTL)
			decision.Candidates = []SchedulerCandidate{candidate}
			if !candidate.Eligible {
				decision.Reason = "preferred_" + candidate.Reason
				r.setLastDecision(decision)
				return "", fmt.Errorf("target assistant %q rejected by scheduler: %s", preferred, candidate.Reason)
			}
			decision.SelectedAssistantID = preferred
			decision.Reason = "preferred_selected"
			outcome = "selected"
			r.setLastDecision(decision)
			return preferred, nil
		}
		decision.Reason = "preferred_runtime_not_registered"
		r.setLastDecision(decision)
		return "", fmt.Errorf("target assistant %q is not registered", preferred)
	}

	records := r.Snapshot()
	sort.Slice(records, func(i, j int) bool {
		return records[i].AssistantID < records[j].AssistantID
	})
	var selected RuntimeRecord
	found := false
	for _, record := range records {
		candidate := evaluateCandidate(record, source, required, now, r.heartbeatTTL)
		decision.Candidates = append(decision.Candidates, candidate)
		if !candidate.Eligible {
			continue
		}
		if !found || lessLoaded(record, selected) {
			selected = record
			found = true
		}
	}
	if !found {
		decision.Reason = "no_eligible_runtime"
		r.setLastDecision(decision)
		return "", fmt.Errorf("target_assistant_id is required when no eligible runtime candidate exists")
	}
	decision.SelectedAssistantID = selected.AssistantID
	decision.Reason = "selected_lowest_load"
	outcome = "selected"
	r.setLastDecision(decision)
	return selected.AssistantID, nil
}

func (r *Registry) Reserve(_ context.Context, assistantID string) error {
	assistantID = strings.TrimSpace(assistantID)
	if assistantID == "" {
		return fmt.Errorf("assistant_id is required")
	}
	if r.store != nil {
		_ = r.expireStale(context.Background())
		return r.store.ReserveRuntime(context.Background(), assistantID, r.heartbeatTTL)
	}
	r.expireStaleMemory(time.Now().UTC())

	r.mu.Lock()
	defer r.mu.Unlock()
	record, ok := r.records[assistantID]
	if !ok {
		return fmt.Errorf("target assistant %q is not registered", assistantID)
	}
	if !record.available(time.Now().UTC(), r.heartbeatTTL) {
		return fmt.Errorf("target assistant %q is not ready", assistantID)
	}
	if record.atCapacity() {
		return fmt.Errorf("target assistant %q is at capacity", assistantID)
	}
	record.InFlight++
	record.UpdatedAt = time.Now().UTC()
	record.Version++
	r.records[assistantID] = record
	return nil
}

func (r *Registry) ReserveRuntimeRoute(ctx context.Context, assistantID string) (core.RuntimeReservation, error) {
	assistantID = strings.TrimSpace(assistantID)
	if assistantID == "" {
		return core.RuntimeReservation{}, fmt.Errorf("assistant_id is required")
	}
	if ctx == nil {
		ctx = context.Background()
	}
	if r.store != nil {
		_ = r.expireStale(ctx)
		if store, ok := r.store.(RouteLeaseStore); ok {
			record, leaseID, leaseExpiresAt, err := store.ReserveRuntimeRoute(ctx, assistantID, r.heartbeatTTL)
			if err != nil {
				return core.RuntimeReservation{}, err
			}
			return runtimeReservationFromRecord(record, leaseID, leaseExpiresAt), nil
		}
		if err := r.store.ReserveRuntime(ctx, assistantID, r.heartbeatTTL); err != nil {
			return core.RuntimeReservation{}, err
		}
		record, ok, err := r.store.GetRuntime(ctx, assistantID)
		if err != nil {
			r.store.ReleaseRuntime(context.Background(), assistantID)
			return core.RuntimeReservation{}, err
		}
		if !ok {
			r.store.ReleaseRuntime(context.Background(), assistantID)
			return core.RuntimeReservation{}, fmt.Errorf("target assistant %q is not registered", assistantID)
		}
		return runtimeReservationFromRecord(record, "", nil), nil
	}
	if err := r.Reserve(ctx, assistantID); err != nil {
		return core.RuntimeReservation{}, err
	}
	record, ok := r.Get(assistantID)
	if !ok {
		r.Release(ctx, assistantID)
		return core.RuntimeReservation{}, fmt.Errorf("target assistant %q is not registered", assistantID)
	}
	return runtimeReservationFromRecord(record, "", nil), nil
}

func (r *Registry) RenewRuntimeRouteLease(ctx context.Context, reservation core.RuntimeReservation) (core.RuntimeReservation, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	if r.store == nil || strings.TrimSpace(reservation.LeaseID) == "" {
		return reservation, nil
	}
	store, ok := r.store.(RouteLeaseStore)
	if !ok {
		return reservation, nil
	}
	expiresAt, err := store.RenewRuntimeRouteLease(ctx, reservation.LeaseID, r.heartbeatTTL)
	if err != nil {
		return reservation, err
	}
	reservation.LeaseExpiresAt = expiresAt
	return reservation, nil
}

func (r *Registry) ReleaseRuntimeRoute(ctx context.Context, reservation core.RuntimeReservation) {
	if ctx == nil {
		ctx = context.Background()
	}
	assistantID := strings.TrimSpace(firstNonEmptyString(reservation.AssistantID, reservation.Route.AssistantID))
	runtimeID := strings.TrimSpace(firstNonEmptyString(reservation.RuntimeID, reservation.Route.RuntimeID))
	leaseID := strings.TrimSpace(reservation.LeaseID)
	if r.store != nil {
		if store, ok := r.store.(RouteLeaseStore); ok && leaseID != "" {
			_ = store.ReleaseRuntimeRoute(ctx, leaseID, assistantID, runtimeID)
			return
		}
		_ = r.store.ReleaseRuntime(ctx, assistantID)
		return
	}
	r.Release(ctx, assistantID)
}

func (r *Registry) Release(_ context.Context, assistantID string) {
	assistantID = strings.TrimSpace(assistantID)
	if assistantID == "" {
		return
	}
	if r.store != nil {
		_ = r.store.ReleaseRuntime(context.Background(), assistantID)
		return
	}

	r.mu.Lock()
	defer r.mu.Unlock()
	record, ok := r.records[assistantID]
	if !ok {
		return
	}
	if record.InFlight > 0 {
		record.InFlight--
	}
	record.UpdatedAt = time.Now().UTC()
	record.Version++
	r.records[assistantID] = record
}

func (r *Registry) normalizeRecord(record RuntimeRecord, heartbeat bool) RuntimeRecord {
	record.AssistantID = strings.TrimSpace(record.AssistantID)
	record.RuntimeID = strings.TrimSpace(record.RuntimeID)
	record.PodName = strings.TrimSpace(record.PodName)
	record.InternalBaseURL = strings.TrimRight(strings.TrimSpace(record.InternalBaseURL), "/")
	record.GatewayURL = strings.TrimRight(strings.TrimSpace(record.GatewayURL), "/")
	record.Status = strings.TrimSpace(record.Status)
	record.Capabilities = normalizeCapabilities(record.Capabilities)
	if record.Status == "" {
		record.Status = "unknown"
	}
	if record.Capacity <= 0 {
		record.Capacity = r.defaultCapacity
	}
	if record.InFlight < 0 {
		record.InFlight = 0
	}
	now := time.Now().UTC()
	if heartbeat || record.LastHeartbeatAt == nil {
		record.LastHeartbeatAt = &now
	}
	if record.UpdatedAt.IsZero() {
		record.UpdatedAt = now
	}
	return record
}

func (r *Registry) setLastDecision(decision SchedulerDecision) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.lastDecision = copyDecision(decision)
}

func (r *Registry) expireStale(ctx context.Context) error {
	if r.store == nil || r.heartbeatTTL <= 0 {
		return nil
	}
	instances, leases, err := r.store.MarkExpired(ctx, r.heartbeatTTL)
	if err == nil {
		if instances > 0 {
			r.logWarn("runtime_heartbeat_expired",
				"expired_count", instances,
				"heartbeat_ttl", r.heartbeatTTL.String(),
			)
		}
		if leases > 0 {
			r.logInfo("runtime_route_lease_expired",
				"lease_count", leases,
			)
		}
	}
	return err
}

func (r *Registry) expireStaleMemory(now time.Time) {
	if r.heartbeatTTL <= 0 {
		return
	}
	r.mu.Lock()
	expired := 0
	for assistantID, record := range r.records {
		if !record.stale(now, r.heartbeatTTL) || record.Status == "unhealthy" {
			continue
		}
		record.Status = "unhealthy"
		record.UpdatedAt = now
		record.Version++
		r.records[assistantID] = record
		expired++
		var lastHeartbeatAt string
		if record.LastHeartbeatAt != nil {
			lastHeartbeatAt = record.LastHeartbeatAt.UTC().Format(time.RFC3339Nano)
		}
		r.logWarn("runtime_heartbeat_expired",
			"assistant_id", assistantID,
			"runtime_id", record.RuntimeID,
			"last_heartbeat_at", lastHeartbeatAt,
			"expired_count", 1,
		)
	}
	r.mu.Unlock()
	if expired > 0 {
		r.logInfo("runtime_heartbeat_expired_sweep",
			"expired_count", expired,
			"heartbeat_ttl", r.heartbeatTTL.String(),
		)
	}
}

func (r RuntimeRecord) available(now time.Time, heartbeatTTL time.Duration) bool {
	if r.DrainMode || r.stale(now, heartbeatTTL) {
		return false
	}
	return runtimeStatusAvailable(r.Status)
}

func runtimeStatusAvailable(status string) bool {
	switch strings.ToLower(strings.TrimSpace(status)) {
	case "ready", "running", "healthy":
		return true
	default:
		return false
	}
}

func (r RuntimeRecord) stale(now time.Time, heartbeatTTL time.Duration) bool {
	return heartbeatTTL > 0 && r.LastHeartbeatAt != nil && r.LastHeartbeatAt.Before(now.Add(-heartbeatTTL))
}

func (r RuntimeRecord) atCapacity() bool {
	return r.Capacity > 0 && r.InFlight >= r.Capacity
}

func lessLoaded(candidate RuntimeRecord, selected RuntimeRecord) bool {
	if selected.Capacity <= 0 || candidate.Capacity <= 0 {
		if candidate.InFlight != selected.InFlight {
			return candidate.InFlight < selected.InFlight
		}
		return candidate.AssistantID < selected.AssistantID
	}
	left := candidate.InFlight * selected.Capacity
	right := selected.InFlight * candidate.Capacity
	if left != right {
		return left < right
	}
	return candidate.AssistantID < selected.AssistantID
}

func evaluateCandidate(
	record RuntimeRecord,
	source string,
	requiredCapabilities []string,
	now time.Time,
	heartbeatTTL time.Duration,
) SchedulerCandidate {
	candidate := SchedulerCandidate{
		AssistantID:     record.AssistantID,
		Status:          record.Status,
		Capacity:        record.Capacity,
		InFlight:        record.InFlight,
		DrainMode:       record.DrainMode,
		Capabilities:    normalizeCapabilities(record.Capabilities),
		LastHeartbeatAt: record.LastHeartbeatAt,
		LoadNumerator:   record.InFlight,
		LoadDenominator: record.Capacity,
	}
	switch {
	case strings.TrimSpace(record.AssistantID) == "":
		candidate.Reason = "missing_assistant_id"
	case strings.TrimSpace(source) != "" && record.AssistantID == source:
		candidate.Reason = "source_assistant"
	case record.DrainMode:
		candidate.Reason = "draining"
	case record.stale(now, heartbeatTTL):
		candidate.Reason = "stale_heartbeat"
	case !runtimeStatusAvailable(record.Status):
		candidate.Reason = "unhealthy"
	case strings.TrimSpace(record.InternalBaseURL) == "" && strings.TrimSpace(record.GatewayURL) == "":
		candidate.Reason = "missing_route"
	case len(missingCapabilities(record.Capabilities, requiredCapabilities)) > 0:
		missing := missingCapabilities(record.Capabilities, requiredCapabilities)
		candidate.Reason = "missing_capabilities:" + strings.Join(missing, ",")
	case record.atCapacity():
		candidate.Reason = "at_capacity"
	default:
		candidate.Eligible = true
		candidate.Reason = "eligible"
	}
	return candidate
}

func runtimeRouteFromRecord(record RuntimeRecord) core.RuntimeRoute {
	return core.RuntimeRoute{
		RuntimeID:       strings.TrimSpace(record.RuntimeID),
		AssistantID:     strings.TrimSpace(record.AssistantID),
		PodName:         strings.TrimSpace(record.PodName),
		InternalBaseURL: strings.TrimRight(strings.TrimSpace(record.InternalBaseURL), "/"),
		GatewayURL:      strings.TrimRight(strings.TrimSpace(record.GatewayURL), "/"),
		Status:          strings.TrimSpace(record.Status),
		LastHeartbeatAt: record.LastHeartbeatAt,
		UpdatedAt:       record.UpdatedAt,
	}
}

func runtimeReservationFromRecord(record RuntimeRecord, leaseID string, leaseExpiresAt *time.Time) core.RuntimeReservation {
	return core.RuntimeReservation{
		LeaseID:        strings.TrimSpace(leaseID),
		RuntimeID:      strings.TrimSpace(record.RuntimeID),
		AssistantID:    strings.TrimSpace(record.AssistantID),
		Route:          runtimeRouteFromRecord(record),
		LeaseExpiresAt: leaseExpiresAt,
	}
}

func firstNonEmptyString(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func normalizeCapabilities(values []string) []string {
	if len(values) == 0 {
		return nil
	}
	seen := make(map[string]struct{}, len(values))
	normalized := make([]string, 0, len(values))
	for _, value := range values {
		value = strings.ToLower(strings.TrimSpace(value))
		if value == "" {
			continue
		}
		if _, ok := seen[value]; ok {
			continue
		}
		seen[value] = struct{}{}
		normalized = append(normalized, value)
	}
	sort.Strings(normalized)
	return normalized
}

func missingCapabilities(actual []string, required []string) []string {
	required = normalizeCapabilities(required)
	if len(required) == 0 {
		return nil
	}
	actual = normalizeCapabilities(actual)
	available := make(map[string]struct{}, len(actual))
	for _, capability := range actual {
		available[capability] = struct{}{}
	}
	var missing []string
	for _, capability := range required {
		if _, ok := available[capability]; !ok {
			missing = append(missing, capability)
		}
	}
	return missing
}

func copyDecision(decision SchedulerDecision) SchedulerDecision {
	copied := decision
	copied.RequiredCapabilities = append([]string(nil), decision.RequiredCapabilities...)
	copied.Candidates = append([]SchedulerCandidate(nil), decision.Candidates...)
	for i := range copied.Candidates {
		copied.Candidates[i].Capabilities = append([]string(nil), copied.Candidates[i].Capabilities...)
	}
	return copied
}
