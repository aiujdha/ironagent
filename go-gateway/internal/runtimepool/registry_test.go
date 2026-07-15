package runtimepool

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"ironagent/go-gateway/internal/core"
)

func TestNoStoreRegistriesDoNotShareRuntimeRecords(t *testing.T) {
	t.Parallel()

	gatewayA := NewRegistryWithStore(1, time.Minute, nil, nil)
	gatewayB := NewRegistryWithStore(1, time.Minute, nil, nil)

	if _, err := gatewayA.Heartbeat(RuntimeRecord{
		AssistantID:     "assistant-a",
		InternalBaseURL: "http://assistant-a:18789",
		Status:          "ready",
		Capacity:        1,
	}); err != nil {
		t.Fatalf("gatewayA.Heartbeat() error = %v", err)
	}

	if _, ok := gatewayA.Get("assistant-a"); !ok {
		t.Fatalf("gatewayA cannot read its own runtime")
	}
	if _, ok := gatewayB.Get("assistant-a"); ok {
		t.Fatalf("gatewayB read gatewayA runtime without a shared store")
	}

	if _, err := gatewayB.SelectTarget(context.Background(), core.RuntimeScheduleRequest{}); err == nil {
		t.Fatalf("gatewayB.SelectTarget() unexpectedly scheduled a runtime from gatewayA")
	}
}

func TestRegistriesWithSameStoreShareRuntimeRecords(t *testing.T) {
	t.Parallel()

	store := newFakeRuntimeStore()
	gatewayA := NewRegistryWithStore(1, time.Minute, store, nil)
	gatewayB := NewRegistryWithStore(1, time.Minute, store, nil)

	if _, err := gatewayA.Heartbeat(RuntimeRecord{
		AssistantID:     "shared-assistant",
		InternalBaseURL: "http://shared-assistant:18789",
		Status:          "ready",
		Capacity:        2,
	}); err != nil {
		t.Fatalf("gatewayA.Heartbeat() error = %v", err)
	}

	record, ok := gatewayB.Get("shared-assistant")
	if !ok {
		t.Fatalf("gatewayB cannot read runtime written by gatewayA")
	}
	if record.AssistantID != "shared-assistant" || record.Status != "ready" {
		t.Fatalf("gatewayB.Get() = %+v", record)
	}

	selected, err := gatewayB.SelectTarget(context.Background(), core.RuntimeScheduleRequest{})
	if err != nil {
		t.Fatalf("gatewayB.SelectTarget() error = %v", err)
	}
	if selected != "shared-assistant" {
		t.Fatalf("gatewayB.SelectTarget() = %q, want shared-assistant", selected)
	}

	if err := gatewayB.Reserve(context.Background(), "shared-assistant"); err != nil {
		t.Fatalf("gatewayB.Reserve() error = %v", err)
	}
	record, ok = gatewayA.Get("shared-assistant")
	if !ok {
		t.Fatalf("gatewayA cannot read runtime after gatewayB reserve")
	}
	if record.InFlight != 1 {
		t.Fatalf("gatewayA.Get().InFlight = %d, want 1", record.InFlight)
	}
}

func TestUpsertPreservesInFlight(t *testing.T) {
	t.Parallel()

	registry := NewRegistryWithStore(1, time.Minute, nil, nil)
	if _, err := registry.Upsert(RuntimeRecord{
		AssistantID:     "busy-assistant",
		InternalBaseURL: "http://busy-assistant:18789",
		Status:          "ready",
		Capacity:        1,
	}); err != nil {
		t.Fatalf("initial Upsert() error = %v", err)
	}
	if err := registry.Reserve(context.Background(), "busy-assistant"); err != nil {
		t.Fatalf("Reserve() error = %v", err)
	}
	if _, err := registry.Upsert(RuntimeRecord{
		AssistantID:     "busy-assistant",
		InternalBaseURL: "http://busy-assistant-new:18789",
		Status:          "ready",
		Capacity:        1,
		InFlight:        0,
	}); err != nil {
		t.Fatalf("second Upsert() error = %v", err)
	}
	record, ok := registry.Get("busy-assistant")
	if !ok {
		t.Fatalf("runtime not found after upsert")
	}
	if record.InFlight != 1 {
		t.Fatalf("InFlight = %d, want 1", record.InFlight)
	}
}

func TestSnapshotWithErrorReturnsStoreListFailure(t *testing.T) {
	t.Parallel()

	store := newFakeRuntimeStore()
	store.listErr = fmt.Errorf("database unavailable")
	registry := NewRegistryWithStore(1, time.Minute, store, nil)

	records, err := registry.SnapshotWithError(context.Background())
	if err == nil || !strings.Contains(err.Error(), "database unavailable") {
		t.Fatalf("SnapshotWithError() err = %v, want database unavailable", err)
	}
	if records != nil {
		t.Fatalf("SnapshotWithError() records = %#v, want nil", records)
	}
	if got := registry.Snapshot(); len(got) != 0 {
		t.Fatalf("Snapshot() = %#v, want empty fallback on error", got)
	}
}

func TestStaleHeartbeatTTLMarksRuntimeUnhealthyAndPreventsScheduling(t *testing.T) {
	t.Parallel()

	registry := NewRegistryWithStore(1, 10*time.Millisecond, nil, nil)
	staleHeartbeat := time.Now().UTC().Add(-time.Minute)
	if _, err := registry.Upsert(RuntimeRecord{
		AssistantID:     "stale-assistant",
		InternalBaseURL: "http://stale-assistant:18789",
		Status:          "ready",
		Capacity:        1,
		LastHeartbeatAt: &staleHeartbeat,
	}); err != nil {
		t.Fatalf("Upsert() error = %v", err)
	}

	if err := registry.Reserve(context.Background(), "stale-assistant"); err == nil {
		t.Fatalf("Reserve() unexpectedly accepted a stale runtime")
	}

	if _, err := registry.SelectTarget(context.Background(), core.RuntimeScheduleRequest{}); err == nil {
		t.Fatalf("SelectTarget() unexpectedly scheduled a stale runtime")
	}

	record, ok := registry.Get("stale-assistant")
	if !ok {
		t.Fatalf("stale runtime disappeared from registry")
	}
	if record.Status != "unhealthy" {
		t.Fatalf("stale runtime status = %q, want unhealthy", record.Status)
	}

	decision := registry.LastDecision()
	if decision.Reason != "no_eligible_runtime" {
		t.Fatalf("LastDecision().Reason = %q, want no_eligible_runtime", decision.Reason)
	}
	if len(decision.Candidates) != 1 {
		t.Fatalf("LastDecision().Candidates length = %d, want 1", len(decision.Candidates))
	}
	candidate := decision.Candidates[0]
	if candidate.Eligible {
		t.Fatalf("stale candidate is eligible: %+v", candidate)
	}
	if candidate.Reason != "stale_heartbeat" {
		t.Fatalf("stale candidate reason = %q, want stale_heartbeat", candidate.Reason)
	}
	if candidate.Status != "unhealthy" {
		t.Fatalf("stale candidate status = %q, want unhealthy", candidate.Status)
	}
}

func TestMissingRuntimeRoutePreventsScheduling(t *testing.T) {
	t.Parallel()

	registry := NewRegistryWithStore(1, time.Minute, nil, nil)
	if _, err := registry.Heartbeat(RuntimeRecord{
		AssistantID: "assistant-no-route",
		Status:      "ready",
		Capacity:    1,
	}); err != nil {
		t.Fatalf("Heartbeat() error = %v", err)
	}

	if _, err := registry.SelectTarget(context.Background(), core.RuntimeScheduleRequest{}); err == nil {
		t.Fatalf("SelectTarget() unexpectedly scheduled a runtime without route")
	}
	decision := registry.LastDecision()
	if len(decision.Candidates) != 1 {
		t.Fatalf("LastDecision().Candidates length = %d, want 1", len(decision.Candidates))
	}
	if decision.Candidates[0].Reason != "missing_route" {
		t.Fatalf("candidate reason = %q, want missing_route", decision.Candidates[0].Reason)
	}
}

func TestHeartbeatPreservesInFlightAndClearsRecoveredDrain(t *testing.T) {
	t.Parallel()

	registry := NewRegistryWithStore(1, time.Minute, nil, nil)
	if _, err := registry.Upsert(RuntimeRecord{
		AssistantID:     "assistant-owned",
		InternalBaseURL: "http://assistant-owned:18789",
		GatewayURL:      "http://public-assistant-owned:18789",
		Status:          "running",
		Capacity:        5,
		InFlight:        2,
		DrainMode:       true,
		LastError:       "manual drain",
	}); err != nil {
		t.Fatalf("Upsert() error = %v", err)
	}

	updated, err := registry.Heartbeat(RuntimeRecord{
		AssistantID: "assistant-owned",
		Status:      "running",
	})
	if err != nil {
		t.Fatalf("Heartbeat() error = %v", err)
	}

	if updated.InFlight != 2 {
		t.Fatalf("heartbeat changed in_flight = %d, want 2", updated.InFlight)
	}
	if updated.Capacity != 5 {
		t.Fatalf("heartbeat changed capacity = %d, want 5", updated.Capacity)
	}
	if updated.DrainMode {
		t.Fatalf("heartbeat kept recovered drain_mode")
	}
	if updated.InternalBaseURL != "http://assistant-owned:18789" || updated.GatewayURL != "http://public-assistant-owned:18789" {
		t.Fatalf("heartbeat changed route fields internal=%q gateway=%q", updated.InternalBaseURL, updated.GatewayURL)
	}
	if updated.LastError != "" {
		t.Fatalf("heartbeat kept recovered last_error = %q", updated.LastError)
	}
}

func TestHeartbeatPreservesLastErrorForUnavailableRuntime(t *testing.T) {
	t.Parallel()

	registry := NewRegistryWithStore(1, time.Minute, nil, nil)
	if _, err := registry.Upsert(RuntimeRecord{
		AssistantID:     "assistant-unhealthy",
		InternalBaseURL: "http://assistant-unhealthy:18789",
		Status:          "unhealthy",
		Capacity:        1,
		LastError:       "gateway healthz is not reachable",
	}); err != nil {
		t.Fatalf("Upsert() error = %v", err)
	}

	updated, err := registry.Heartbeat(RuntimeRecord{
		AssistantID: "assistant-unhealthy",
		Status:      "unhealthy",
	})
	if err != nil {
		t.Fatalf("Heartbeat() error = %v", err)
	}

	if updated.LastError != "gateway healthz is not reachable" {
		t.Fatalf("heartbeat changed last_error = %q", updated.LastError)
	}
}

type fakeRuntimeStore struct {
	mu      sync.Mutex
	records map[string]RuntimeRecord
	listErr error
}

func newFakeRuntimeStore() *fakeRuntimeStore {
	return &fakeRuntimeStore{records: make(map[string]RuntimeRecord)}
}

func (s *fakeRuntimeStore) UpsertRuntime(_ context.Context, record RuntimeRecord) (RuntimeRecord, error) {
	assistantID := strings.TrimSpace(record.AssistantID)
	if assistantID == "" {
		return RuntimeRecord{}, fmt.Errorf("assistant_id is required")
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	record.AssistantID = assistantID
	if existing, ok := s.records[assistantID]; ok {
		record.InFlight = existing.InFlight
	}
	record.Version = s.records[assistantID].Version + 1
	s.records[assistantID] = copyRuntimeRecord(record)
	return copyRuntimeRecord(record), nil
}

func (s *fakeRuntimeStore) HeartbeatRuntime(_ context.Context, record RuntimeRecord) (RuntimeRecord, error) {
	assistantID := strings.TrimSpace(record.AssistantID)
	if assistantID == "" {
		return RuntimeRecord{}, fmt.Errorf("assistant_id is required")
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	existing, existed := s.records[assistantID]
	record.AssistantID = assistantID
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
	s.records[assistantID] = copyRuntimeRecord(record)
	return copyRuntimeRecord(record), nil
}

func (s *fakeRuntimeStore) GetRuntime(_ context.Context, assistantID string) (RuntimeRecord, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	record, ok := s.records[strings.TrimSpace(assistantID)]
	return copyRuntimeRecord(record), ok, nil
}

func (s *fakeRuntimeStore) ListRuntimes(_ context.Context) ([]RuntimeRecord, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.listErr != nil {
		return nil, s.listErr
	}
	records := make([]RuntimeRecord, 0, len(s.records))
	for _, record := range s.records {
		records = append(records, copyRuntimeRecord(record))
	}
	sort.Slice(records, func(i, j int) bool {
		return records[i].AssistantID < records[j].AssistantID
	})
	return records, nil
}

func (s *fakeRuntimeStore) DeleteRuntime(_ context.Context, assistantID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.records, strings.TrimSpace(assistantID))
	return nil
}

func (s *fakeRuntimeStore) ReserveRuntime(_ context.Context, assistantID string, heartbeatTTL time.Duration) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	record, ok := s.records[strings.TrimSpace(assistantID)]
	if !ok {
		return fmt.Errorf("target assistant %q is not registered", assistantID)
	}
	now := time.Now().UTC()
	if !record.available(now, heartbeatTTL) {
		return fmt.Errorf("target assistant %q is not ready", assistantID)
	}
	if record.atCapacity() {
		return fmt.Errorf("target assistant %q is at capacity", assistantID)
	}
	record.InFlight++
	record.UpdatedAt = now
	record.Version++
	s.records[record.AssistantID] = copyRuntimeRecord(record)
	return nil
}

func (s *fakeRuntimeStore) ReleaseRuntime(_ context.Context, assistantID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	record, ok := s.records[strings.TrimSpace(assistantID)]
	if !ok {
		return nil
	}
	if record.InFlight > 0 {
		record.InFlight--
	}
	record.UpdatedAt = time.Now().UTC()
	record.Version++
	s.records[record.AssistantID] = copyRuntimeRecord(record)
	return nil
}

func (s *fakeRuntimeStore) MarkExpired(_ context.Context, heartbeatTTL time.Duration) (int, int, error) {
	if heartbeatTTL <= 0 {
		return 0, 0, nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	now := time.Now().UTC()
	expired := 0
	for assistantID, record := range s.records {
		if !record.stale(now, heartbeatTTL) || record.Status == "unhealthy" {
			continue
		}
		record.Status = "unhealthy"
		record.UpdatedAt = now
		record.Version++
		s.records[assistantID] = copyRuntimeRecord(record)
		expired++
	}
	return expired, 0, nil
}

func copyRuntimeRecord(record RuntimeRecord) RuntimeRecord {
	copied := record
	copied.Capabilities = append([]string(nil), record.Capabilities...)
	if record.LastHeartbeatAt != nil {
		lastHeartbeatAt := *record.LastHeartbeatAt
		copied.LastHeartbeatAt = &lastHeartbeatAt
	}
	return copied
}
