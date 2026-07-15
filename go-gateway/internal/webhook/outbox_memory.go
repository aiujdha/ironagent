package webhook

import (
	"context"
	"sort"
	"strings"
	"sync"
	"time"
)

type MemoryOutboxStore struct {
	mu          sync.Mutex
	seqByTask   map[string]int64
	records     map[string]DeliveryRecord
	deadLetters map[string]DeadLetter
}

func NewMemoryOutboxStore() *MemoryOutboxStore {
	return &MemoryOutboxStore{
		seqByTask:   make(map[string]int64),
		records:     make(map[string]DeliveryRecord),
		deadLetters: make(map[string]DeadLetter),
	}
}

func (s *MemoryOutboxStore) NextEventSeq(_ context.Context, taskID string) (int64, error) {
	taskID = strings.TrimSpace(taskID)
	s.mu.Lock()
	defer s.mu.Unlock()
	s.seqByTask[taskID]++
	return s.seqByTask[taskID], nil
}

func (s *MemoryOutboxStore) Enqueue(_ context.Context, record DeliveryRecord) error {
	record.Event.EventID = strings.TrimSpace(record.Event.EventID)
	if record.Event.EventID == "" {
		return nil
	}
	now := time.Now().UTC()
	if record.CreatedAt.IsZero() {
		record.CreatedAt = now
	}
	if record.UpdatedAt.IsZero() {
		record.UpdatedAt = now
	}
	if strings.TrimSpace(record.Status) == "" {
		record.Status = "pending"
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if SupersedesPendingRetrying(record.Event) {
		s.supersedePendingRetryingLocked(record, now)
	}
	s.records[record.Event.EventID] = record
	return nil
}

func (s *MemoryOutboxStore) supersedePendingRetryingLocked(record DeliveryRecord, now time.Time) {
	taskID := strings.TrimSpace(record.Event.TaskID)
	callbackURL := strings.TrimSpace(record.CallbackURL)
	if taskID == "" || callbackURL == "" || record.Event.EventSeq <= 0 {
		return
	}
	for eventID, candidate := range s.records {
		if candidate.Status != "pending" {
			continue
		}
		if strings.TrimSpace(candidate.Event.EventType) != "task.retrying" {
			continue
		}
		if strings.TrimSpace(candidate.Event.TaskID) != taskID || strings.TrimSpace(candidate.CallbackURL) != callbackURL {
			continue
		}
		if candidate.Event.EventSeq <= 0 || candidate.Event.EventSeq >= record.Event.EventSeq {
			continue
		}
		candidate.Status = "dead"
		candidate.LastError = "superseded by newer callback event"
		candidate.NextAttemptAt = nil
		candidate.LockedBy = ""
		candidate.LockedUntil = nil
		candidate.UpdatedAt = now
		s.records[eventID] = candidate
	}
}

func (s *MemoryOutboxStore) SupersedePendingRetrying(_ context.Context, now time.Time) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	latest := map[string]int64{}
	for _, record := range s.records {
		if record.Status != "pending" || strings.TrimSpace(record.Event.EventType) != "task.retrying" {
			continue
		}
		key := pendingRetryingSupersedeKey(record)
		if key == "" || record.Event.EventSeq <= 0 {
			continue
		}
		if record.Event.EventSeq > latest[key] {
			latest[key] = record.Event.EventSeq
		}
	}
	superseded := 0
	for eventID, record := range s.records {
		if record.Status != "pending" || strings.TrimSpace(record.Event.EventType) != "task.retrying" {
			continue
		}
		key := pendingRetryingSupersedeKey(record)
		if key == "" || record.Event.EventSeq <= 0 || record.Event.EventSeq >= latest[key] {
			continue
		}
		record.Status = "dead"
		record.LastError = "superseded by newer retrying callback event"
		record.NextAttemptAt = nil
		record.LockedBy = ""
		record.LockedUntil = nil
		record.UpdatedAt = now
		s.records[eventID] = record
		superseded++
	}
	return superseded, nil
}

func pendingRetryingSupersedeKey(record DeliveryRecord) string {
	taskID := strings.TrimSpace(record.Event.TaskID)
	callbackURL := strings.TrimSpace(record.CallbackURL)
	if taskID == "" || callbackURL == "" {
		return ""
	}
	return taskID + "\x00" + callbackURL
}

func (s *MemoryOutboxStore) ClaimPending(
	_ context.Context,
	now time.Time,
	limit int,
	workerID string,
	leaseDuration time.Duration,
) ([]DeliveryRecord, error) {
	if limit <= 0 {
		limit = 100
	}
	if leaseDuration <= 0 {
		leaseDuration = 30 * time.Second
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	records := make([]DeliveryRecord, 0, limit)
	for _, record := range s.records {
		if record.Status != "pending" {
			continue
		}
		if record.NextAttemptAt != nil && record.NextAttemptAt.After(now) {
			continue
		}
		if s.hasBlockingEarlierEvent(record) {
			continue
		}
		records = append(records, record)
	}
	sort.Slice(records, func(i, j int) bool {
		if records[i].Event.TaskID == records[j].Event.TaskID {
			return records[i].Event.EventSeq < records[j].Event.EventSeq
		}
		return records[i].CreatedAt.Before(records[j].CreatedAt)
	})
	if len(records) > limit {
		records = records[:limit]
	}
	lockedUntil := now.Add(leaseDuration)
	for index := range records {
		record := records[index]
		record.Status = "sending"
		record.LockedBy = strings.TrimSpace(workerID)
		record.LockedUntil = &lockedUntil
		record.UpdatedAt = now
		s.records[record.Event.EventID] = record
		records[index] = record
	}
	return records, nil
}

func (s *MemoryOutboxStore) hasBlockingEarlierEvent(record DeliveryRecord) bool {
	taskID := strings.TrimSpace(record.Event.TaskID)
	if taskID == "" || record.Event.EventSeq <= 0 {
		return false
	}
	for _, candidate := range s.records {
		if strings.TrimSpace(candidate.Event.TaskID) != taskID {
			continue
		}
		if candidate.Event.EventSeq >= record.Event.EventSeq {
			continue
		}
		if candidate.Status != "delivered" && candidate.Status != "dead" {
			return true
		}
	}
	return false
}

func (s *MemoryOutboxStore) RecoverStuck(_ context.Context, now time.Time) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	recovered := 0
	for eventID, record := range s.records {
		if record.Status != "sending" || record.LockedUntil == nil || record.LockedUntil.After(now) {
			continue
		}
		record.Status = "pending"
		record.LockedBy = ""
		record.LockedUntil = nil
		record.UpdatedAt = now
		s.records[eventID] = record
		recovered++
	}
	return recovered, nil
}

func (s *MemoryOutboxStore) MarkAttempt(_ context.Context, record DeliveryRecord, nextAttemptAt time.Time) error {
	record.Status = "pending"
	record.NextAttemptAt = &nextAttemptAt
	record.LockedBy = ""
	record.LockedUntil = nil
	record.UpdatedAt = time.Now().UTC()
	s.mu.Lock()
	defer s.mu.Unlock()
	s.records[record.Event.EventID] = record
	return nil
}

func (s *MemoryOutboxStore) MarkDelivered(_ context.Context, record DeliveryRecord) error {
	now := time.Now().UTC()
	record.Status = "delivered"
	record.Delivered = true
	record.DeliveredAt = &now
	record.LockedBy = ""
	record.LockedUntil = nil
	record.UpdatedAt = now
	s.mu.Lock()
	defer s.mu.Unlock()
	s.records[record.Event.EventID] = record
	return nil
}

func (s *MemoryOutboxStore) MarkDead(_ context.Context, record DeliveryRecord, lastError string) error {
	now := time.Now().UTC()
	record.Status = "dead"
	record.LastError = lastError
	record.LockedBy = ""
	record.LockedUntil = nil
	record.UpdatedAt = now
	letter := DeadLetter{
		Event:       record.Event,
		CallbackURL: record.CallbackURL,
		Attempts:    record.Attempts,
		LastError:   lastError,
		FailedAt:    now,
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.records[record.Event.EventID] = record
	s.deadLetters[record.Event.EventID] = letter
	return nil
}

func (s *MemoryOutboxStore) Acknowledge(_ context.Context, ack ACK) (ACK, bool, error) {
	ack.EventID = strings.TrimSpace(ack.EventID)
	ack.TaskID = strings.TrimSpace(ack.TaskID)
	if ack.EventID == "" || ack.TaskID == "" {
		return ACK{}, false, nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	record, ok := s.records[ack.EventID]
	if !ok {
		return ACK{}, false, nil
	}
	if record.Event.TaskID != ack.TaskID {
		return ACK{}, false, nil
	}
	record.Acked = ack.Received && ack.Handled
	record.UpdatedAt = time.Now().UTC()
	s.records[ack.EventID] = record
	return ack, true, nil
}

func (s *MemoryOutboxStore) DeadLetters(_ context.Context, limit int) ([]DeadLetter, error) {
	if limit <= 0 {
		limit = 100
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	letters := make([]DeadLetter, 0, len(s.deadLetters))
	for _, letter := range s.deadLetters {
		letters = append(letters, letter)
	}
	sort.Slice(letters, func(i, j int) bool {
		return letters[i].FailedAt.Before(letters[j].FailedAt)
	})
	if len(letters) > limit {
		letters = letters[:limit]
	}
	return letters, nil
}

func (s *MemoryOutboxStore) ReplayDeadLetter(_ context.Context, eventID string, now time.Time) (DeliveryRecord, bool, error) {
	eventID = strings.TrimSpace(eventID)
	if eventID == "" {
		return DeliveryRecord{}, false, nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	record, ok := s.records[eventID]
	if !ok || record.Status != "dead" {
		return DeliveryRecord{}, false, nil
	}
	record.Status = "pending"
	record.Attempts = 0
	record.LastError = ""
	record.NextAttemptAt = nil
	record.LockedBy = ""
	record.LockedUntil = nil
	record.UpdatedAt = now
	s.records[eventID] = record
	delete(s.deadLetters, eventID)
	return record, true, nil
}
