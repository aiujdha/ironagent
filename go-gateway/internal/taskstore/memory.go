package taskstore

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	"ironagent/go-gateway/internal/core"
)

type MemoryStore struct {
	mu                 sync.RWMutex
	tasks              map[string]core.GatewayTaskRecord
	idempotency        map[string]string
	queue              map[string]core.GatewayTaskQueueItem
	conversationRoutes map[string]core.ConversationRouteRecord
}

func NewMemoryStore() *MemoryStore {
	return &MemoryStore{
		tasks:              make(map[string]core.GatewayTaskRecord),
		idempotency:        make(map[string]string),
		queue:              make(map[string]core.GatewayTaskQueueItem),
		conversationRoutes: make(map[string]core.ConversationRouteRecord),
	}
}

func (s *MemoryStore) FindByIdempotency(
	_ context.Context,
	clientID string,
	idempotencyKey string,
) (core.GatewayTaskRecord, bool, error) {
	key := idempotencyIndexKey(clientID, idempotencyKey)
	if key == "" {
		return core.GatewayTaskRecord{}, false, nil
	}

	s.mu.RLock()
	defer s.mu.RUnlock()
	taskID, ok := s.idempotency[key]
	if !ok {
		return core.GatewayTaskRecord{}, false, nil
	}
	record, ok := s.tasks[taskID]
	return record, ok, nil
}

func (s *MemoryStore) GetTask(_ context.Context, taskID string) (core.GatewayTaskRecord, bool, error) {
	taskID = strings.TrimSpace(taskID)
	if taskID == "" {
		return core.GatewayTaskRecord{}, false, nil
	}

	s.mu.RLock()
	defer s.mu.RUnlock()
	record, ok := s.tasks[taskID]
	return record, ok, nil
}

func (s *MemoryStore) FindByApprovalID(_ context.Context, approvalID string) (core.GatewayTaskRecord, bool, error) {
	approvalID = strings.TrimSpace(approvalID)
	if approvalID == "" {
		return core.GatewayTaskRecord{}, false, nil
	}

	s.mu.RLock()
	defer s.mu.RUnlock()
	for _, record := range s.tasks {
		if record.Task.ApprovalID == approvalID {
			return record, true, nil
		}
	}
	return core.GatewayTaskRecord{}, false, nil
}

func (s *MemoryStore) UpsertTask(_ context.Context, record core.GatewayTaskRecord) error {
	record.Task.ID = strings.TrimSpace(record.Task.ID)
	if record.Task.ID == "" {
		return nil
	}
	now := time.Now().UTC()
	if record.CreatedAt.IsZero() {
		record.CreatedAt = record.Task.CreatedAt
	}
	if record.CreatedAt.IsZero() {
		record.CreatedAt = now
	}
	record.UpdatedAt = now

	s.mu.Lock()
	defer s.mu.Unlock()
	s.tasks[record.Task.ID] = record
	if indexKey := idempotencyIndexKey(record.ClientID, record.IdempotencyKey); indexKey != "" {
		s.idempotency[indexKey] = record.Task.ID
	}
	return nil
}

func (s *MemoryStore) GetConversationRoute(
	_ context.Context,
	clientID string,
	userID string,
	conversationID string,
) (core.ConversationRouteRecord, bool, error) {
	key := conversationRouteKey(clientID, userID, conversationID)
	if key == "" {
		return core.ConversationRouteRecord{}, false, nil
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	record, ok := s.conversationRoutes[key]
	return record, ok, nil
}

func (s *MemoryStore) BindConversationRoute(
	_ context.Context,
	record core.ConversationRouteRecord,
) (core.ConversationRouteRecord, bool, error) {
	record.ClientID = normalizeMemoryClientID(record.ClientID)
	record.UserID = strings.TrimSpace(record.UserID)
	record.ConversationID = strings.TrimSpace(record.ConversationID)
	record.AssistantID = strings.TrimSpace(record.AssistantID)
	key := conversationRouteKey(record.ClientID, record.UserID, record.ConversationID)
	if key == "" || record.AssistantID == "" {
		return core.ConversationRouteRecord{}, false, nil
	}
	now := time.Now().UTC()
	if record.CreatedAt.IsZero() {
		record.CreatedAt = now
	}
	record.UpdatedAt = now

	s.mu.Lock()
	defer s.mu.Unlock()
	if existing, ok := s.conversationRoutes[key]; ok {
		if strings.TrimSpace(existing.AssistantID) != record.AssistantID {
			return existing, true, &core.ValidationError{
				Message: fmt.Sprintf(
					"conversation_id %q is already bound to assistant %q",
					record.ConversationID,
					existing.AssistantID,
				),
			}
		}
		existing.UpdatedAt = now
		s.conversationRoutes[key] = existing
		return existing, true, nil
	}
	s.conversationRoutes[key] = record
	return record, false, nil
}

func (s *MemoryStore) CreateQueuedTask(
	_ context.Context,
	record core.GatewayTaskRecord,
	item core.GatewayTaskQueueItem,
) (core.GatewayTaskRecord, bool, error) {
	record.Task.ID = strings.TrimSpace(record.Task.ID)
	item.TaskID = strings.TrimSpace(item.TaskID)
	if record.Task.ID == "" || item.TaskID == "" {
		return core.GatewayTaskRecord{}, false, nil
	}
	now := time.Now().UTC()
	if record.CreatedAt.IsZero() {
		record.CreatedAt = record.Task.CreatedAt
	}
	if record.CreatedAt.IsZero() {
		record.CreatedAt = now
	}
	record.UpdatedAt = now
	if item.Status == "" {
		item.Status = "queued"
	}
	if item.CreatedAt.IsZero() {
		item.CreatedAt = now
	}
	item.UpdatedAt = now

	s.mu.Lock()
	defer s.mu.Unlock()
	if indexKey := idempotencyIndexKey(record.ClientID, record.IdempotencyKey); indexKey != "" {
		if existingTaskID, ok := s.idempotency[indexKey]; ok {
			existing, ok := s.tasks[existingTaskID]
			if ok {
				if existing.RequestHash != "" && record.RequestHash != "" && existing.RequestHash != record.RequestHash {
					return core.GatewayTaskRecord{}, false, &core.ValidationError{Message: "idempotency_key was already used with a different request"}
				}
				return existing, true, nil
			}
		}
		s.idempotency[indexKey] = record.Task.ID
	}
	s.tasks[record.Task.ID] = record
	if existing, ok := s.queue[item.TaskID]; !ok || existing.Status == "failed" {
		s.queue[item.TaskID] = item
	}
	return record, false, nil
}

func (s *MemoryStore) EnqueueTask(_ context.Context, item core.GatewayTaskQueueItem) error {
	item.TaskID = strings.TrimSpace(item.TaskID)
	if item.TaskID == "" {
		return nil
	}
	now := time.Now().UTC()
	if item.Status == "" {
		item.Status = "queued"
	}
	if item.CreatedAt.IsZero() {
		item.CreatedAt = now
	}
	item.UpdatedAt = now

	s.mu.Lock()
	defer s.mu.Unlock()
	if existing, ok := s.queue[item.TaskID]; ok && existing.Status != "failed" {
		return nil
	}
	s.queue[item.TaskID] = item
	return nil
}

func (s *MemoryStore) ActivateQueuedTask(_ context.Context, taskID string) error {
	taskID = strings.TrimSpace(taskID)
	if taskID == "" {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	item, ok := s.queue[taskID]
	if !ok || item.Status != "admitting" {
		return nil
	}
	item.Status = "queued"
	item.UpdatedAt = time.Now().UTC()
	s.queue[taskID] = item
	return nil
}

func (s *MemoryStore) ClaimQueuedTasks(
	_ context.Context,
	workerID string,
	limit int,
	leaseDuration time.Duration,
) ([]core.GatewayTaskQueueItem, error) {
	workerID = strings.TrimSpace(workerID)
	if workerID == "" {
		workerID = "memory-worker"
	}
	if limit <= 0 {
		limit = 1
	}
	if leaseDuration <= 0 {
		leaseDuration = 30 * time.Second
	}
	now := time.Now().UTC()
	lockedUntil := now.Add(leaseDuration)
	claimID := fmt.Sprintf("%s-%d", workerID, now.UnixNano())
	attemptToken := claimID

	s.mu.Lock()
	defer s.mu.Unlock()
	items := make([]core.GatewayTaskQueueItem, 0, limit)
	taskIDs := make([]string, 0, len(s.queue))
	for taskID := range s.queue {
		taskIDs = append(taskIDs, taskID)
	}
	sort.Strings(taskIDs)
	for _, taskID := range taskIDs {
		if len(items) >= limit {
			break
		}
		item := s.queue[taskID]
		if !claimableQueueItem(item, now) {
			continue
		}
		item.Status = "processing"
		item.Attempts++
		item.LockedBy = claimID
		item.AttemptToken = attemptToken
		item.LockedUntil = &lockedUntil
		item.AttemptStartedAt = &now
		item.LastProgressAt = &now
		item.LastProgressStatus = "claimed"
		item.LastProgressStage = "queued"
		item.LastProgressDetail = ""
		item.TerminalReasonCode = ""
		item.TerminalReasonText = ""
		item.TerminalDetailJSON = ""
		item.UpdatedAt = now
		s.queue[taskID] = item
		items = append(items, item)
	}
	return items, nil
}

func (s *MemoryStore) ExtendQueuedTaskLease(
	_ context.Context,
	taskID string,
	lockedBy string,
	leaseDuration time.Duration,
) (bool, error) {
	taskID = strings.TrimSpace(taskID)
	lockedBy = strings.TrimSpace(lockedBy)
	if taskID == "" || lockedBy == "" {
		return false, nil
	}
	if leaseDuration <= 0 {
		leaseDuration = 30 * time.Second
	}
	now := time.Now().UTC()
	lockedUntil := now.Add(leaseDuration)

	s.mu.Lock()
	defer s.mu.Unlock()
	item, ok := s.queue[taskID]
	if !ok || item.Status != "processing" || item.LockedBy != lockedBy {
		return false, nil
	}
	item.LockedUntil = &lockedUntil
	item.UpdatedAt = now
	s.queue[taskID] = item
	return true, nil
}

func (s *MemoryStore) RecordQueuedTaskProgress(
	_ context.Context,
	taskID string,
	lockedBy string,
	attemptToken string,
	status string,
	stage string,
	detail string,
	at time.Time,
) (bool, error) {
	taskID = strings.TrimSpace(taskID)
	lockedBy = strings.TrimSpace(lockedBy)
	attemptToken = strings.TrimSpace(attemptToken)
	if taskID == "" || lockedBy == "" || attemptToken == "" {
		return false, nil
	}
	if at.IsZero() {
		at = time.Now().UTC()
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	item, ok := s.queue[taskID]
	if !ok || item.Status != "processing" || item.LockedBy != lockedBy || item.AttemptToken != attemptToken ||
		(item.LockedUntil != nil && !item.LockedUntil.After(at)) {
		return false, nil
	}
	item.LastProgressAt = &at
	item.LastProgressStatus = strings.TrimSpace(status)
	item.LastProgressStage = strings.TrimSpace(stage)
	item.LastProgressDetail = strings.TrimSpace(detail)
	item.UpdatedAt = at
	s.queue[taskID] = item
	return true, nil
}

func (s *MemoryStore) CompleteQueuedTask(_ context.Context, taskID string, lockedBy string, attemptToken string) (bool, error) {
	taskID = strings.TrimSpace(taskID)
	lockedBy = strings.TrimSpace(lockedBy)
	attemptToken = strings.TrimSpace(attemptToken)
	if taskID == "" || lockedBy == "" || attemptToken == "" {
		return false, nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	item, ok := s.queue[taskID]
	now := time.Now().UTC()
	if !ok || item.Status != "processing" || item.LockedBy != lockedBy || item.AttemptToken != attemptToken ||
		(item.LockedUntil != nil && !item.LockedUntil.After(now)) {
		return false, nil
	}
	item.Status = "completed"
	item.LockedBy = ""
	item.LockedUntil = nil
	resetQueueCapacityWaitState(&item)
	item.UpdatedAt = time.Now().UTC()
	s.queue[taskID] = item
	return true, nil
}

func (s *MemoryStore) CancelQueuedTask(_ context.Context, taskID string, reason string) error {
	taskID = strings.TrimSpace(taskID)
	if taskID == "" {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	item, ok := s.queue[taskID]
	if !ok {
		return nil
	}
	item.Status = "cancelled"
	item.LastError = strings.TrimSpace(reason)
	item.LockedBy = ""
	item.LockedUntil = nil
	resetQueueCapacityWaitState(&item)
	item.UpdatedAt = time.Now().UTC()
	s.queue[taskID] = item
	return nil
}

func (s *MemoryStore) FailQueuedTask(
	_ context.Context,
	taskID string,
	lockedBy string,
	attemptToken string,
	message string,
	retry bool,
	retryAt *time.Time,
	terminal *core.QueueTerminalReason,
) (bool, error) {
	taskID = strings.TrimSpace(taskID)
	lockedBy = strings.TrimSpace(lockedBy)
	attemptToken = strings.TrimSpace(attemptToken)
	if taskID == "" || lockedBy == "" || attemptToken == "" {
		return false, nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	item, ok := s.queue[taskID]
	now := time.Now().UTC()
	if !ok || item.Status != "processing" || item.LockedBy != lockedBy || item.AttemptToken != attemptToken ||
		(item.LockedUntil != nil && !item.LockedUntil.After(now)) {
		return false, nil
	}
	if retry {
		item.Status = "retry"
		item.NextAttemptAt = retryAt
	} else {
		item.Status = "failed"
		if terminal != nil {
			item.TerminalReasonCode = strings.TrimSpace(terminal.Code)
			item.TerminalReasonText = strings.TrimSpace(terminal.Text)
			item.TerminalDetailJSON = strings.TrimSpace(terminal.DetailJSON)
		}
	}
	item.LastError = strings.TrimSpace(message)
	item.LockedBy = ""
	item.LockedUntil = nil
	resetQueueCapacityWaitState(&item)
	item.UpdatedAt = time.Now().UTC()
	s.queue[taskID] = item
	return true, nil
}

func (s *MemoryStore) FailQueuedTaskForCapacity(
	_ context.Context,
	taskID string,
	lockedBy string,
	attemptToken string,
	message string,
	terminal *core.QueueTerminalReason,
	state core.QueueCapacityWaitState,
) (bool, error) {
	taskID = strings.TrimSpace(taskID)
	lockedBy = strings.TrimSpace(lockedBy)
	attemptToken = strings.TrimSpace(attemptToken)
	if taskID == "" || lockedBy == "" || attemptToken == "" {
		return false, nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	item, ok := s.queue[taskID]
	now := time.Now().UTC()
	if !ok || item.Status != "processing" || item.LockedBy != lockedBy || item.AttemptToken != attemptToken ||
		(item.LockedUntil != nil && !item.LockedUntil.After(now)) {
		return false, nil
	}
	item.Status = "failed"
	item.LastError = strings.TrimSpace(message)
	item.LockedBy = ""
	item.LockedUntil = nil
	item.AttemptStartedAt = nil
	item.AttemptToken = ""
	if terminal != nil {
		item.TerminalReasonCode = strings.TrimSpace(terminal.Code)
		item.TerminalReasonText = strings.TrimSpace(terminal.Text)
		item.TerminalDetailJSON = strings.TrimSpace(terminal.DetailJSON)
	}
	applyQueueCapacityWaitState(&item, state)
	item.UpdatedAt = now
	s.queue[taskID] = item
	return true, nil
}

func (s *MemoryStore) DeferQueuedTask(
	_ context.Context,
	taskID string,
	lockedBy string,
	attemptToken string,
	message string,
	retryAt *time.Time,
) (bool, error) {
	taskID = strings.TrimSpace(taskID)
	lockedBy = strings.TrimSpace(lockedBy)
	attemptToken = strings.TrimSpace(attemptToken)
	if taskID == "" || lockedBy == "" || attemptToken == "" {
		return false, nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	item, ok := s.queue[taskID]
	now := time.Now().UTC()
	if !ok || item.Status != "processing" || item.LockedBy != lockedBy || item.AttemptToken != attemptToken ||
		(item.LockedUntil != nil && !item.LockedUntil.After(now)) {
		return false, nil
	}
	item.Status = "retry"
	item.LastError = strings.TrimSpace(message)
	item.NextAttemptAt = retryAt
	item.LockedBy = ""
	item.LockedUntil = nil
	item.AttemptStartedAt = nil
	item.AttemptToken = ""
	if item.Attempts > 0 {
		item.Attempts--
	}
	resetQueueCapacityWaitState(&item)
	item.UpdatedAt = now
	s.queue[taskID] = item
	return true, nil
}

func (s *MemoryStore) DeferQueuedTaskForCapacity(
	_ context.Context,
	taskID string,
	lockedBy string,
	attemptToken string,
	message string,
	retryAt *time.Time,
	state core.QueueCapacityWaitState,
) (bool, error) {
	taskID = strings.TrimSpace(taskID)
	lockedBy = strings.TrimSpace(lockedBy)
	attemptToken = strings.TrimSpace(attemptToken)
	if taskID == "" || lockedBy == "" || attemptToken == "" {
		return false, nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	item, ok := s.queue[taskID]
	now := time.Now().UTC()
	if !ok || item.Status != "processing" || item.LockedBy != lockedBy || item.AttemptToken != attemptToken ||
		(item.LockedUntil != nil && !item.LockedUntil.After(now)) {
		return false, nil
	}
	item.Status = "retry"
	item.LastError = strings.TrimSpace(message)
	item.NextAttemptAt = retryAt
	item.LockedBy = ""
	item.LockedUntil = nil
	item.AttemptStartedAt = nil
	item.AttemptToken = ""
	if item.Attempts > 0 {
		item.Attempts--
	}
	applyQueueCapacityWaitState(&item, state)
	item.UpdatedAt = now
	s.queue[taskID] = item
	return true, nil
}

func (s *MemoryStore) RecordQueuedTaskCapacityWaitCallback(
	_ context.Context,
	taskID string,
	notifiedAt time.Time,
) (bool, error) {
	taskID = strings.TrimSpace(taskID)
	if taskID == "" {
		return false, nil
	}
	if notifiedAt.IsZero() {
		notifiedAt = time.Now().UTC()
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	item, ok := s.queue[taskID]
	if !ok {
		return false, nil
	}
	item.CapacityWaitLastCallbackAt = cloneTimePtr(&notifiedAt)
	item.UpdatedAt = notifiedAt.UTC()
	s.queue[taskID] = item
	return true, nil
}

func (s *MemoryStore) TaskQueueStats(_ context.Context) (core.TaskQueueStats, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var stats core.TaskQueueStats
	for _, item := range s.queue {
		switch item.Status {
		case "admitting":
			stats.Admitting++
		case "queued":
			stats.Queued++
		case "processing":
			stats.Processing++
		case "retry":
			stats.Retry++
		case "failed":
			stats.Failed++
		case "completed":
			stats.Completed++
		case "cancelled":
			stats.Cancelled++
		}
	}
	stats.Active = stats.Admitting + stats.Queued + stats.Processing + stats.Retry
	return stats, nil
}

func (s *MemoryStore) TaskQueueStatsByTargetAssistant(
	_ context.Context,
	targetAssistantID string,
) (core.TaskQueueStats, error) {
	targetAssistantID = strings.TrimSpace(targetAssistantID)
	if targetAssistantID == "" {
		return core.TaskQueueStats{}, nil
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	var stats core.TaskQueueStats
	for taskID, item := range s.queue {
		record, ok := s.tasks[taskID]
		if !ok || strings.TrimSpace(record.Task.TargetAssistantID) != targetAssistantID {
			continue
		}
		switch item.Status {
		case "admitting":
			stats.Admitting++
		case "queued":
			stats.Queued++
		case "processing":
			stats.Processing++
		case "retry":
			stats.Retry++
		}
	}
	stats.Active = stats.Admitting + stats.Queued + stats.Processing + stats.Retry
	return stats, nil
}

func applyQueueCapacityWaitState(item *core.GatewayTaskQueueItem, state core.QueueCapacityWaitState) {
	if item == nil {
		return
	}
	item.CapacityWaitCount = state.Count
	item.CapacityWaitFirstAt = cloneTimePtr(state.FirstAt)
	if !state.LastAt.IsZero() {
		item.CapacityWaitLastAt = cloneTimePtr(&state.LastAt)
	} else {
		item.CapacityWaitLastAt = nil
	}
	item.CapacityWaitLastCallbackAt = cloneTimePtr(state.LastCallbackAt)
}

func resetQueueCapacityWaitState(item *core.GatewayTaskQueueItem) {
	if item == nil {
		return
	}
	item.CapacityWaitCount = 0
	item.CapacityWaitFirstAt = nil
	item.CapacityWaitLastAt = nil
	item.CapacityWaitLastCallbackAt = nil
}

func cloneTimePtr(value *time.Time) *time.Time {
	if value == nil || value.IsZero() {
		return nil
	}
	cloned := value.UTC()
	return &cloned
}

func idempotencyIndexKey(clientID string, idempotencyKey string) string {
	clientID = strings.TrimSpace(clientID)
	idempotencyKey = strings.TrimSpace(idempotencyKey)
	if clientID == "" || idempotencyKey == "" {
		return ""
	}
	return clientID + "\x00" + idempotencyKey
}

func conversationRouteKey(clientID string, userID string, conversationID string) string {
	clientID = normalizeMemoryClientID(clientID)
	userID = strings.TrimSpace(userID)
	conversationID = strings.TrimSpace(conversationID)
	if userID == "" || conversationID == "" {
		return ""
	}
	return clientID + "\x00" + userID + "\x00" + conversationID
}

func normalizeMemoryClientID(clientID string) string {
	clientID = strings.TrimSpace(clientID)
	if clientID == "" {
		return "default"
	}
	return clientID
}

func claimableQueueItem(item core.GatewayTaskQueueItem, now time.Time) bool {
	if item.Status != "queued" && item.Status != "retry" && item.Status != "processing" {
		return false
	}
	if item.Status == "processing" && (item.LockedUntil == nil || item.LockedUntil.After(now)) {
		return false
	}
	if item.NextAttemptAt != nil && item.NextAttemptAt.After(now) {
		return false
	}
	return true
}
