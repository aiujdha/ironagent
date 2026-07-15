package core

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"regexp"
	"strings"
	"sync"
	"time"
	"unicode"
)

const (
	defaultTaskProgressCallbackBatchSize    = 5
	defaultTaskProgressCallbackInterval     = 90 * time.Second
	defaultTaskProgressCallbackStepMaxRunes = 150
	defaultTaskProgressCallbackStateTTL     = 30 * time.Minute
	defaultTaskProgressFinalFlushTimeout    = 250 * time.Millisecond
	taskProgressAssistantDeltaMergeKey      = "assistant_delta"
)

type TaskProgressStore interface {
	LoadTaskProgress(ctx context.Context, taskID string, attemptToken string) (TaskProgressState, bool, error)
	SaveTaskProgress(ctx context.Context, taskID string, attemptToken string, state TaskProgressState, ttl time.Duration) error
	DeleteTaskProgress(ctx context.Context, taskID string, attemptToken string) error
}

type TaskProgressState struct {
	NextSeq         int                `json:"nextSeq"`
	LastReportedSeq int                `json:"lastReportedSeq"`
	PendingSteps    []TaskProgressStep `json:"pendingSteps"`
	LastFlushAt     time.Time          `json:"lastFlushAt"`
	UpdatedAt       time.Time          `json:"updatedAt"`
}

type TaskProgressStep struct {
	Seq         int       `json:"seq"`
	Title       string    `json:"title"`
	Content     string    `json:"content"`
	EventType   string    `json:"eventType,omitempty"`
	ContentHash string    `json:"contentHash,omitempty"`
	Truncated   bool      `json:"truncated,omitempty"`
	MergeKey    string    `json:"mergeKey,omitempty"`
	Open        bool      `json:"open,omitempty"`
	At          time.Time `json:"at"`
}

type QueuedTaskProgressUpdate struct {
	TaskID        string
	ReplyText     string
	FromSeq       int
	ToSeq         int
	EventType     string
	Status        string
	Stage         string
	ErrorCode     string
	ErrorMessage  string
	ErrorLocation string
	Reason        string
}

type queuedTaskProgressEmitter struct {
	mu            sync.Mutex
	flushDone     chan struct{}
	taskID        string
	attemptToken  string
	store         TaskProgressStore
	hook          func(context.Context, QueuedTaskProgressUpdate) error
	batchSize     int
	interval      time.Duration
	stepMaxRunes  int
	stateTTL      time.Duration
	startedAt     time.Time
	state         TaskProgressState
	stopTicker    chan struct{}
	flushing      bool
	stopping      bool
	persistMu     sync.Mutex
	persistCond   *sync.Cond
	persistAction taskProgressPersistAction
	persistState  TaskProgressState
	persistStop   bool
	persistDone   chan struct{}
}

type taskProgressPersistAction int

const (
	taskProgressPersistNone taskProgressPersistAction = iota
	taskProgressPersistSave
	taskProgressPersistDelete
)

func (s *Service) newQueuedTaskProgressEmitter(
	ctx context.Context,
	item GatewayTaskQueueItem,
	options TaskWorkerOptions,
) *queuedTaskProgressEmitter {
	if !options.TaskProgressCallbackEnabled || s.queuedTaskProgressHook == nil {
		return nil
	}
	taskID := strings.TrimSpace(item.TaskID)
	attemptToken := strings.TrimSpace(item.AttemptToken)
	if taskID == "" || attemptToken == "" {
		return nil
	}
	batchSize := options.TaskProgressCallbackBatchSize
	if batchSize <= 0 {
		batchSize = defaultTaskProgressCallbackBatchSize
	}
	interval := options.TaskProgressCallbackInterval
	if interval <= 0 {
		interval = defaultTaskProgressCallbackInterval
	}
	stepMaxRunes := options.TaskProgressCallbackStepMaxRunes
	if stepMaxRunes <= 0 {
		stepMaxRunes = defaultTaskProgressCallbackStepMaxRunes
	}
	stateTTL := options.TaskProgressCallbackStateTTL
	if stateTTL <= 0 {
		stateTTL = defaultTaskProgressCallbackStateTTL
	}
	state := TaskProgressState{
		NextSeq:     1,
		LastFlushAt: time.Now().UTC(),
		UpdatedAt:   time.Now().UTC(),
	}
	if s.taskProgressStore != nil {
		if loaded, ok, err := s.taskProgressStore.LoadTaskProgress(ctx, taskID, attemptToken); err == nil && ok {
			state = normalizeTaskProgressState(loaded)
		}
	}
	emitter := &queuedTaskProgressEmitter{
		taskID:       taskID,
		attemptToken: attemptToken,
		store:        s.taskProgressStore,
		hook:         s.queuedTaskProgressHook,
		batchSize:    batchSize,
		interval:     interval,
		stepMaxRunes: stepMaxRunes,
		stateTTL:     stateTTL,
		startedAt:    state.UpdatedAt,
		state:        state,
	}
	if emitter.startedAt.IsZero() {
		emitter.startedAt = time.Now().UTC()
	}
	if emitter.store != nil {
		emitter.persistCond = sync.NewCond(&emitter.persistMu)
		emitter.persistDone = make(chan struct{})
		go emitter.runPersistWorker()
	}
	if interval > 0 {
		emitter.stopTicker = make(chan struct{})
		go emitter.runTicker(taskProgressFlushContext(ctx))
	}
	return emitter
}

func normalizeTaskProgressState(state TaskProgressState) TaskProgressState {
	if state.NextSeq <= 0 {
		state.NextSeq = 1
	}
	if state.LastFlushAt.IsZero() {
		state.LastFlushAt = time.Now().UTC()
	}
	for i := range state.PendingSteps {
		state.PendingSteps[i].EventType = normalizeTaskProgressCallbackEventType(state.PendingSteps[i].EventType)
	}
	return state
}

func (e *queuedTaskProgressEmitter) runTicker(ctx context.Context) {
	ticker := time.NewTicker(e.interval)
	defer ticker.Stop()
	for {
		select {
		case tickAt := <-ticker.C:
			_ = e.flush(ctx, false)
			e.addKeepaliveIfIdle(ctx, tickAt)
		case <-e.stopTicker:
			return
		}
	}
}

func (e *queuedTaskProgressEmitter) Stop(ctx context.Context) {
	if e == nil {
		return
	}
	if e.stopTicker != nil {
		select {
		case <-e.stopTicker:
		default:
			close(e.stopTicker)
		}
	}
	e.mu.Lock()
	e.stopping = true
	hasPending := len(e.state.PendingSteps) > 0
	isFlushing := e.flushing
	e.mu.Unlock()
	if hasPending && !isFlushing {
		e.flushAsync(ctx, true)
		return
	}
	if !hasPending && !isFlushing {
		e.queuePersistDelete()
		e.stopPersistWorkerAsync()
	}
}

func (e *queuedTaskProgressEmitter) AddRuntimeEvent(ctx context.Context, event RuntimeProgressEvent) {
	if e == nil {
		return
	}
	title, content, eventType, ok := taskProgressStepFromRuntimeEvent(event, e.stepMaxRunes)
	if !ok {
		return
	}
	if eventType == "delta" {
		e.AddMergeableStep(ctx, title, content, event.At, taskProgressAssistantDeltaMergeKey, eventType)
		return
	}
	e.addStep(ctx, title, content, event.At, eventType)
}

func (e *queuedTaskProgressEmitter) AddStep(ctx context.Context, title string, content string, at time.Time) {
	e.addStep(ctx, title, content, at, "task.progress")
}

func (e *queuedTaskProgressEmitter) addStep(ctx context.Context, title string, content string, at time.Time, eventType string) {
	if e == nil {
		return
	}
	eventType = normalizeTaskProgressCallbackEventType(eventType)
	title, content = wrapInternalTaskProgressStep(title, content)
	title, _, titleOK := cleanTaskProgressText(title, 80)
	content, truncated, contentOK := cleanTaskProgressText(content, e.stepMaxRunes)
	if !titleOK || !contentOK {
		return
	}
	if at.IsZero() {
		at = time.Now().UTC()
	}
	e.mu.Lock()
	closedOpen := e.closeOpenProgressStepsLocked()
	eventTypeBoundary := len(e.state.PendingSteps) > 0 &&
		normalizeTaskProgressCallbackEventType(e.state.PendingSteps[len(e.state.PendingSteps)-1].EventType) != eventType
	step := TaskProgressStep{
		Seq:         e.state.NextSeq,
		Title:       title,
		Content:     content,
		EventType:   eventType,
		ContentHash: taskProgressContentHash(content),
		Truncated:   truncated,
		At:          at.UTC(),
	}
	e.state.NextSeq++
	e.state.PendingSteps = append(e.state.PendingSteps, step)
	e.state.UpdatedAt = time.Now().UTC()
	e.queuePersistSaveLocked()
	drainFlush := closedOpen || eventTypeBoundary
	shouldFlush := drainFlush || e.shouldFlushPendingLocked(false)
	e.mu.Unlock()
	if shouldFlush {
		e.flushAsync(ctx, drainFlush)
	}
}

func (e *queuedTaskProgressEmitter) addKeepaliveIfIdle(ctx context.Context, at time.Time) {
	if e == nil || e.interval <= 0 {
		return
	}
	if at.IsZero() {
		at = time.Now().UTC()
	}
	at = at.UTC()
	e.mu.Lock()
	if e.stopping || e.flushing || e.state.LastReportedSeq <= 0 || len(e.state.PendingSteps) > 0 || at.Sub(e.state.LastFlushAt) < e.interval {
		e.mu.Unlock()
		return
	}
	content := "等待运行时返回后续进度或最终结果"
	if elapsed := at.Sub(e.startedAt); elapsed > 0 {
		content += "，已持续约" + formatTaskProgressDuration(elapsed)
	}
	title, _, titleOK := cleanTaskProgressText("任务仍在执行", 80)
	content, truncated, contentOK := cleanTaskProgressText(content, e.stepMaxRunes)
	if !titleOK || !contentOK {
		e.mu.Unlock()
		return
	}
	step := TaskProgressStep{
		Seq:         e.state.NextSeq,
		Title:       title,
		Content:     content,
		EventType:   "task.progress",
		ContentHash: taskProgressContentHash(content),
		Truncated:   truncated,
		At:          at,
	}
	e.state.NextSeq++
	e.state.PendingSteps = append(e.state.PendingSteps, step)
	e.state.UpdatedAt = time.Now().UTC()
	e.queuePersistSaveLocked()
	e.mu.Unlock()
	_ = e.flush(ctx, false)
}

func (e *queuedTaskProgressEmitter) AddMergeableStep(ctx context.Context, title string, content string, at time.Time, mergeKey string, eventType string) {
	if e == nil {
		return
	}
	mergeKey = strings.TrimSpace(mergeKey)
	if mergeKey == "" {
		e.addStep(ctx, title, content, at, eventType)
		return
	}
	eventType = normalizeTaskProgressCallbackEventType(eventType)
	title, _, titleOK := cleanTaskProgressText(title, 80)
	cleanedContent, truncated, contentOK := cleanTaskProgressText(content, e.stepMaxRunes)
	if !titleOK || !contentOK {
		return
	}
	if at.IsZero() {
		at = time.Now().UTC()
	}
	e.mu.Lock()
	eventTypeBoundary := len(e.state.PendingSteps) > 0 &&
		normalizeTaskProgressCallbackEventType(e.state.PendingSteps[len(e.state.PendingSteps)-1].EventType) != eventType
	if len(e.state.PendingSteps) > 0 {
		last := &e.state.PendingSteps[len(e.state.PendingSteps)-1]
		if last.Open && last.MergeKey == mergeKey {
			mergedContent := mergeTaskProgressText(last.Content, content)
			if cleaned, mergedTruncated, ok := cleanTaskProgressText(mergedContent, e.stepMaxRunes); ok {
				last.Title = title
				last.Content = cleaned
				last.EventType = eventType
				last.ContentHash = taskProgressContentHash(cleaned)
				last.Truncated = mergedTruncated
				last.At = at.UTC()
				e.state.UpdatedAt = time.Now().UTC()
				e.queuePersistSaveLocked()
				shouldFlush := e.shouldFlushPendingLocked(false)
				e.mu.Unlock()
				if shouldFlush {
					e.flushAsync(ctx, false)
				}
				return
			}
		}
	}
	step := TaskProgressStep{
		Seq:         e.state.NextSeq,
		Title:       title,
		Content:     cleanedContent,
		EventType:   eventType,
		ContentHash: taskProgressContentHash(cleanedContent),
		Truncated:   truncated,
		MergeKey:    mergeKey,
		Open:        true,
		At:          at.UTC(),
	}
	e.state.NextSeq++
	e.state.PendingSteps = append(e.state.PendingSteps, step)
	e.state.UpdatedAt = time.Now().UTC()
	e.queuePersistSaveLocked()
	shouldFlush := eventTypeBoundary || e.shouldFlushPendingLocked(false)
	e.mu.Unlock()
	if shouldFlush {
		e.flushAsync(ctx, false)
	}
}

func (e *queuedTaskProgressEmitter) Flush(ctx context.Context) error {
	return e.flush(ctx, true)
}

func (e *queuedTaskProgressEmitter) flush(ctx context.Context, drain bool) error {
	if e == nil {
		return nil
	}
	if ctx == nil {
		ctx = context.Background()
	}
	for {
		steps, err := e.reserveFlushBatch(ctx, drain)
		if err != nil || len(steps) == 0 {
			return err
		}
		err = e.dispatchFlushBatch(ctx, steps)
		morePending, finishErr := e.finishFlushBatch(ctx, steps, err)
		if err != nil {
			return err
		}
		if finishErr != nil {
			return finishErr
		}
		if !drain || !morePending {
			return nil
		}
	}
}

func (e *queuedTaskProgressEmitter) reserveFlushBatch(ctx context.Context, drain bool) ([]TaskProgressStep, error) {
	for {
		e.mu.Lock()
		if !e.flushing {
			break
		}
		done := e.flushDone
		e.mu.Unlock()
		if done == nil {
			continue
		}
		select {
		case <-done:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	defer e.mu.Unlock()
	flushableCount := e.flushablePendingCountLocked(drain)
	if flushableCount == 0 {
		return nil, nil
	}
	e.flushing = true
	e.flushDone = make(chan struct{})
	batchSize := flushableCount
	if e.batchSize > 0 && batchSize > e.batchSize {
		batchSize = e.batchSize
	}
	eventType := normalizeTaskProgressCallbackEventType(e.state.PendingSteps[0].EventType)
	for i := 1; i < batchSize; i++ {
		if normalizeTaskProgressCallbackEventType(e.state.PendingSteps[i].EventType) != eventType {
			batchSize = i
			break
		}
	}
	steps := make([]TaskProgressStep, batchSize)
	copy(steps, e.state.PendingSteps[:batchSize])
	return steps, nil
}

func (e *queuedTaskProgressEmitter) dispatchFlushBatch(ctx context.Context, steps []TaskProgressStep) error {
	replyText := formatTaskProgressMarkdown(steps)
	fromSeq := steps[0].Seq
	toSeq := steps[len(steps)-1].Seq
	return e.hook(ctx, QueuedTaskProgressUpdate{
		TaskID:    e.taskID,
		ReplyText: replyText,
		FromSeq:   fromSeq,
		ToSeq:     toSeq,
		EventType: normalizeTaskProgressCallbackEventType(steps[0].EventType),
	})
}

func (e *queuedTaskProgressEmitter) closeOpenProgressStepsLocked() bool {
	closed := false
	for i := range e.state.PendingSteps {
		if e.state.PendingSteps[i].Open {
			closed = true
		}
		e.state.PendingSteps[i].Open = false
	}
	return closed
}

func (e *queuedTaskProgressEmitter) shouldFlushPendingLocked(drain bool) bool {
	if e.flushing {
		return false
	}
	flushableCount := e.flushablePendingCountLocked(drain)
	if flushableCount == 0 {
		return false
	}
	return (e.batchSize > 0 && flushableCount >= e.batchSize) || time.Since(e.state.LastFlushAt) >= e.interval
}

func (e *queuedTaskProgressEmitter) flushablePendingCountLocked(drain bool) int {
	count := len(e.state.PendingSteps)
	if drain {
		return count
	}
	for count > 0 && e.state.PendingSteps[count-1].Open {
		count--
	}
	return count
}

func (e *queuedTaskProgressEmitter) finishFlushBatch(ctx context.Context, steps []TaskProgressStep, dispatchErr error) (bool, error) {
	e.mu.Lock()
	done := e.flushDone
	if dispatchErr != nil {
		morePending := len(e.state.PendingSteps) > 0
		e.flushing = false
		e.flushDone = nil
		e.mu.Unlock()
		if done != nil {
			close(done)
		}
		return morePending, nil
	}
	toSeq := steps[len(steps)-1].Seq
	remaining := e.state.PendingSteps[:0]
	for _, step := range e.state.PendingSteps {
		if step.Seq > toSeq {
			remaining = append(remaining, step)
		}
	}
	e.state.PendingSteps = remaining
	e.state.LastReportedSeq = toSeq
	e.state.LastFlushAt = time.Now().UTC()
	e.state.UpdatedAt = e.state.LastFlushAt
	morePending := len(e.state.PendingSteps) > 0
	stopping := e.stopping
	state := copyTaskProgressState(e.state)
	e.flushing = false
	e.flushDone = nil
	e.mu.Unlock()
	if done != nil {
		close(done)
	}
	if stopping && !morePending {
		e.queuePersistDelete()
		e.stopPersistWorkerAsync()
	} else {
		e.queuePersistSave(state)
	}
	return morePending, nil
}

func (e *queuedTaskProgressEmitter) flushAsync(ctx context.Context, drain bool) {
	if e == nil {
		return
	}
	go func() {
		_ = e.flush(taskProgressFlushContext(ctx), drain)
	}()
}

func taskProgressFlushContext(ctx context.Context) context.Context {
	if ctx == nil {
		return context.Background()
	}
	return context.WithoutCancel(ctx)
}

func copyTaskProgressState(state TaskProgressState) TaskProgressState {
	state.PendingSteps = append([]TaskProgressStep(nil), state.PendingSteps...)
	return state
}

func (e *queuedTaskProgressEmitter) queuePersistSaveLocked() {
	e.queuePersistSave(copyTaskProgressState(e.state))
}

func (e *queuedTaskProgressEmitter) queuePersistSave(state TaskProgressState) {
	e.queuePersist(taskProgressPersistSave, state)
}

func (e *queuedTaskProgressEmitter) queuePersistDelete() {
	e.queuePersist(taskProgressPersistDelete, TaskProgressState{})
}

func (e *queuedTaskProgressEmitter) queuePersist(action taskProgressPersistAction, state TaskProgressState) {
	if e == nil || e.store == nil || e.persistCond == nil || action == taskProgressPersistNone {
		return
	}
	e.persistMu.Lock()
	if e.persistStop {
		e.persistMu.Unlock()
		return
	}
	e.persistAction = action
	e.persistState = copyTaskProgressState(state)
	e.persistCond.Signal()
	e.persistMu.Unlock()
}

func (e *queuedTaskProgressEmitter) runPersistWorker() {
	defer close(e.persistDone)
	for {
		e.persistMu.Lock()
		for e.persistAction == taskProgressPersistNone && !e.persistStop {
			e.persistCond.Wait()
		}
		action := e.persistAction
		state := copyTaskProgressState(e.persistState)
		stop := e.persistStop
		e.persistAction = taskProgressPersistNone
		e.persistState = TaskProgressState{}
		e.persistMu.Unlock()

		switch action {
		case taskProgressPersistSave:
			_ = e.store.SaveTaskProgress(context.Background(), e.taskID, e.attemptToken, state, e.stateTTL)
		case taskProgressPersistDelete:
			_ = e.store.DeleteTaskProgress(context.Background(), e.taskID, e.attemptToken)
		}

		if stop {
			e.persistMu.Lock()
			shouldStop := e.persistAction == taskProgressPersistNone
			e.persistMu.Unlock()
			if shouldStop {
				return
			}
		}
	}
}

func (e *queuedTaskProgressEmitter) stopPersistWorkerAsync() {
	if e == nil || e.persistCond == nil {
		return
	}
	e.persistMu.Lock()
	e.persistStop = true
	e.persistCond.Signal()
	e.persistMu.Unlock()
}

func taskProgressStepFromRuntimeEvent(event RuntimeProgressEvent, stepMaxRunes int) (string, string, string, bool) {
	eventType, ok := taskProgressCallbackEventTypeFromRuntimeEvent(event)
	if !ok {
		return "", "", "", false
	}
	state := strings.ToLower(strings.TrimSpace(event.State))
	eventName := strings.ToLower(strings.TrimSpace(event.Event))
	if eventType == "delta" {
		content := firstNonBlankRawString(event.Text, event.Detail, event.Status)
		if _, _, ok := cleanTaskProgressText(content, stepMaxRunes); !ok {
			return "", "", "", false
		}
		return "assistant progress", content, eventType, true
	}
	if eventType == "tool.output" {
		content := firstNonEmptyString(event.Text, event.Detail, event.Status, event.State)
		content, _, ok = cleanTaskProgressText(content, stepMaxRunes)
		if !ok {
			return "", "", "", false
		}
		return "tool.output", content, eventType, true
	}
	switch state {
	case "chat_sent", "ack":
		title, content := wrapInternalTaskProgressStep(state, state)
		return title, content, eventType, true
	}
	title := firstNonEmptyString(event.ToolName, event.Event, event.State)
	titleFromToolName := strings.TrimSpace(event.ToolName) != ""
	if !titleFromToolName && isGenericToolProgressEvent(eventName) {
		title = firstNonEmptyString(event.State, event.Event)
	}
	if strings.EqualFold(strings.TrimSpace(title), "chat") && state != "" {
		title = "chat." + state
	}
	if !titleFromToolName {
		title = displayTaskProgressToken(title)
	}
	content := firstNonEmptyString(event.Status, event.Detail, event.Text, event.State)
	if strings.TrimSpace(content) == strings.TrimSpace(title) {
		content = firstNonEmptyString(event.State, event.Event)
	}
	content, _, ok = cleanTaskProgressText(content, stepMaxRunes)
	if !ok {
		return "", "", "", false
	}
	return title, content, eventType, true
}

func taskProgressCallbackEventTypeFromRuntimeEvent(event RuntimeProgressEvent) (string, bool) {
	state := strings.ToLower(strings.TrimSpace(event.State))
	eventName := strings.ToLower(strings.TrimSpace(event.Event))
	switch state {
	case "heartbeat", "final", "error":
		return "", false
	}
	switch eventName {
	case "heartbeat", "tool.heartbeat":
		return "", false
	}
	switch state {
	case "chat_sent", "ack":
		return "task.progress", true
	}
	switch eventName {
	case "chat.sent", "chat.send", "chat.ack", "ack":
		return "task.progress", true
	}
	if state == "delta" || state == "chat.delta" || eventName == "delta" || eventName == "chat.delta" {
		return "delta", true
	}
	for _, candidate := range []string{eventName, state} {
		if taskProgressAllowedCallbackEventType(candidate) {
			return normalizeTaskProgressCallbackEventType(candidate), true
		}
	}
	for _, candidate := range []string{eventName, state} {
		if eventType, ok := taskProgressToolCallbackEventType(candidate); ok {
			return eventType, true
		}
	}
	if eventName == "chat" && state == "progress" {
		return "chat.progress", true
	}
	if eventName == "chat" && isAssistantProgressToken(state) {
		return "chat.progress", true
	}
	if strings.HasPrefix(eventName, "chat.") || strings.HasPrefix(eventName, "assistant.") {
		if isAssistantProgressToken(eventName) {
			return "chat.progress", true
		}
	}
	if strings.TrimSpace(event.ToolName) != "" && (state == "progress" || eventName == "tool") {
		return "tool.progress", true
	}
	if isGenericToolProgressEvent(eventName) || strings.HasPrefix(state, "tool_") || strings.HasPrefix(state, "tool.") {
		if eventType, ok := taskProgressGenericToolStateCallbackEventType(state); ok {
			return eventType, true
		}
		if eventType, ok := taskProgressToolCallbackEventType(state); ok {
			return eventType, true
		}
	}
	return "", false
}

func taskProgressAllowedCallbackEventType(value string) bool {
	switch normalizeTaskProgressCallbackEventType(value) {
	case "chat.progress",
		"tool.progress",
		"delta",
		"tool.exec",
		"tool.output",
		"tool.process",
		"file.generated",
		"skill.started",
		"skill.completed":
		return true
	default:
		return false
	}
}

func normalizeTaskProgressCallbackEventType(value string) string {
	value = strings.ToLower(strings.TrimSpace(value))
	if value == "" {
		return "task.progress"
	}
	return value
}

func taskProgressVisibleTextStep(title string, event RuntimeProgressEvent, stepMaxRunes int) (string, string, bool) {
	content := firstNonEmptyString(event.Text, event.Detail, event.Status)
	content, _, ok := cleanTaskProgressText(content, stepMaxRunes)
	if !ok {
		return "", "", false
	}
	return title, content, true
}

func isAssistantProgressToken(value string) bool {
	normalized := strings.ToLower(strings.TrimSpace(value))
	if normalized == "" {
		return false
	}
	if strings.Contains(normalized, "heartbeat") ||
		strings.Contains(normalized, "final") ||
		strings.Contains(normalized, "error") {
		return false
	}
	tokens := strings.FieldsFunc(normalized, func(r rune) bool {
		return r == '.' || r == '_' || r == '-' || unicode.IsSpace(r)
	})
	for _, token := range tokens {
		switch token {
		case "thinking", "reasoning", "planning", "plan", "step", "progress":
			return true
		}
	}
	return false
}

func isGenericToolProgressEvent(value string) bool {
	normalized := strings.ToLower(strings.TrimSpace(value))
	switch normalized {
	case "tool", "tool-event", "tool-events":
		return true
	default:
		return false
	}
}

func isToolProgressToken(value string) bool {
	normalized := strings.ToLower(strings.TrimSpace(value))
	if normalized == "" {
		return false
	}
	if strings.Contains(normalized, "heartbeat") {
		return false
	}
	tokens := strings.FieldsFunc(normalized, func(r rune) bool {
		return r == '.' || r == '_' || r == '-' || unicode.IsSpace(r)
	})
	hasTool := false
	for _, token := range tokens {
		switch token {
		case "tool", "tools":
			hasTool = true
		}
	}
	if !hasTool {
		return false
	}
	for _, token := range tokens {
		switch token {
		case "progress", "running", "exec", "execute", "execution", "output", "read", "write", "call", "skill", "result":
			return true
		}
	}
	return normalized == "tool" || normalized == "tools"
}

func taskProgressToolCallbackEventType(value string) (string, bool) {
	if !isToolProgressToken(value) {
		return "", false
	}
	normalized := strings.ToLower(strings.TrimSpace(value))
	tokens := strings.FieldsFunc(normalized, func(r rune) bool {
		return r == '.' || r == '_' || r == '-' || unicode.IsSpace(r)
	})
	hasToken := func(values ...string) bool {
		for _, token := range tokens {
			for _, value := range values {
				if token == value {
					return true
				}
			}
		}
		return false
	}
	switch {
	case hasToken("output", "read", "write", "result"):
		return "tool.output", true
	case hasToken("exec", "execute", "execution", "run", "running", "call"):
		return "tool.exec", true
	case hasToken("process", "processing"):
		return "tool.process", true
	case hasToken("progress"):
		return "tool.progress", true
	case hasToken("skill"):
		return "tool.process", true
	default:
		return "tool.progress", true
	}
}

func taskProgressGenericToolStateCallbackEventType(value string) (string, bool) {
	normalized := strings.ToLower(strings.TrimSpace(value))
	if normalized == "" || strings.Contains(normalized, "heartbeat") {
		return "", false
	}
	tokens := strings.FieldsFunc(normalized, func(r rune) bool {
		return r == '.' || r == '_' || r == '-' || unicode.IsSpace(r)
	})
	hasToken := func(values ...string) bool {
		for _, token := range tokens {
			for _, value := range values {
				if token == value {
					return true
				}
			}
		}
		return false
	}
	switch {
	case hasToken("output", "read", "write", "result"):
		return "tool.output", true
	case hasToken("exec", "execute", "execution", "run", "running", "call"):
		return "tool.exec", true
	case hasToken("process", "processing"):
		return "tool.process", true
	case hasToken("progress"):
		return "tool.progress", true
	default:
		return "", false
	}
}

func displayTaskProgressToken(value string) string {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" {
		return ""
	}
	parts := strings.FieldsFunc(trimmed, func(r rune) bool {
		return r == '.' || r == '_' || r == '-' || unicode.IsSpace(r)
	})
	if len(parts) <= 1 {
		return trimmed
	}
	return strings.Join(parts, " ")
}

func wrapInternalTaskProgressStep(title string, content string) (string, string) {
	switch normalizeInternalTaskProgressToken(title) {
	case "runtime started":
		return "任务已启动", "运行环境已启动，开始处理请求"
	case "chat sent":
		return "请求已发送", "已将用户请求发送给运行时"
	case "ack":
		return "请求已接收", "运行时已确认接收，正在处理"
	default:
		return title, content
	}
}

func normalizeInternalTaskProgressToken(value string) string {
	value = strings.ToLower(strings.TrimSpace(value))
	if value == "" {
		return ""
	}
	parts := strings.FieldsFunc(value, func(r rune) bool {
		return r == '_' || r == '-' || unicode.IsSpace(r)
	})
	return strings.Join(parts, " ")
}

func formatTaskProgressMarkdown(steps []TaskProgressStep) string {
	if len(steps) == 0 {
		return ""
	}
	var builder strings.Builder
	fromSeq := steps[0].Seq
	toSeq := steps[len(steps)-1].Seq
	builder.WriteString("### 执行进度：第 ")
	builder.WriteString(intString(fromSeq))
	builder.WriteString("-")
	builder.WriteString(intString(toSeq))
	builder.WriteString(" 步\n\n")
	for _, step := range steps {
		builder.WriteString(intString(step.Seq))
		builder.WriteString(". **")
		builder.WriteString(markdownInline(step.Title))
		builder.WriteString("**：")
		builder.WriteString(markdownInline(step.Content))
		builder.WriteString("\n")
	}
	return strings.TrimRight(builder.String(), "\n") + "\n\n"
}

func intString(value int) string {
	if value == 0 {
		return "0"
	}
	negative := value < 0
	if negative {
		value = -value
	}
	var digits [20]byte
	i := len(digits)
	for value > 0 {
		i--
		digits[i] = byte('0' + value%10)
		value /= 10
	}
	if negative {
		i--
		digits[i] = '-'
	}
	return string(digits[i:])
}

func formatTaskProgressDuration(value time.Duration) string {
	seconds := int(value.Round(time.Second) / time.Second)
	if seconds < 1 {
		seconds = 1
	}
	if seconds < 60 {
		return intString(seconds) + "秒"
	}
	minutes := seconds / 60
	remainingSeconds := seconds % 60
	if minutes < 60 {
		if remainingSeconds == 0 {
			return intString(minutes) + "分钟"
		}
		return intString(minutes) + "分" + intString(remainingSeconds) + "秒"
	}
	hours := minutes / 60
	remainingMinutes := minutes % 60
	if remainingMinutes == 0 {
		return intString(hours) + "小时"
	}
	return intString(hours) + "小时" + intString(remainingMinutes) + "分钟"
}

func taskProgressContentHash(content string) string {
	sum := sha256.Sum256([]byte(content))
	return hex.EncodeToString(sum[:])
}

func mergeTaskProgressText(current string, next string) string {
	if strings.TrimSpace(next) == "" {
		return current
	}
	if strings.TrimSpace(current) == "" {
		return next
	}
	currentTrimmed := strings.TrimSpace(current)
	nextTrimmed := strings.TrimSpace(next)
	if strings.HasPrefix(next, current) || strings.HasPrefix(nextTrimmed, currentTrimmed) {
		return next
	}
	if strings.HasSuffix(current, next) || strings.HasSuffix(currentTrimmed, nextTrimmed) {
		return current
	}
	return current + next
}

func firstNonBlankRawString(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}

var (
	taskProgressWhitespacePattern = regexp.MustCompile(`\s+`)
	taskProgressBearerPattern     = regexp.MustCompile(`(?i)\bbearer\s+[A-Za-z0-9._~+/\-]+=*`)
	taskProgressSecretPattern     = regexp.MustCompile(`(?i)\b(api[_-]?key|access[_-]?token|token|password|passwd|secret|cookie)\s*[:=]\s*[^,\s;]+`)
	taskProgressJWTPattern        = regexp.MustCompile(`\b[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b`)
	taskProgressURLQueryPattern   = regexp.MustCompile(`(https?://[^\s?]+)\?[^\s]+`)
	taskProgressWorkspacePattern  = regexp.MustCompile(`/workspace/[^\s,;]+`)
	taskProgressDBURLPattern      = regexp.MustCompile(`(?i)\b(mysql|postgres|postgresql|redis)://[^\s]+`)
)

func cleanTaskProgressText(value string, maxRunes int) (string, bool, bool) {
	original := strings.TrimSpace(value)
	if original == "" {
		return "", false, false
	}
	if strings.Contains(original, "```") {
		return "[code content redacted]", false, true
	}
	trimmed := strings.TrimSpace(original)
	if len(trimmed) > 80 && ((strings.HasPrefix(trimmed, "{") && strings.HasSuffix(trimmed, "}")) ||
		(strings.HasPrefix(trimmed, "[") && strings.HasSuffix(trimmed, "]"))) {
		return "[structured content redacted]", false, true
	}
	var builder strings.Builder
	for _, r := range original {
		switch {
		case r == '\n' || r == '\r' || r == '\t':
			builder.WriteByte(' ')
		case unicode.IsControl(r):
			builder.WriteByte(' ')
		default:
			builder.WriteRune(r)
		}
	}
	cleaned := taskProgressWhitespacePattern.ReplaceAllString(strings.TrimSpace(builder.String()), " ")
	cleaned = taskProgressBearerPattern.ReplaceAllString(cleaned, "Bearer [redacted]")
	cleaned = taskProgressSecretPattern.ReplaceAllString(cleaned, "$1=[redacted]")
	cleaned = taskProgressJWTPattern.ReplaceAllString(cleaned, "[jwt-redacted]")
	cleaned = taskProgressURLQueryPattern.ReplaceAllString(cleaned, "$1?[redacted]")
	cleaned = taskProgressWorkspacePattern.ReplaceAllString(cleaned, "[workspace-path]")
	cleaned = taskProgressDBURLPattern.ReplaceAllString(cleaned, "$1://[redacted]")
	if cleaned == "" {
		return "", false, false
	}
	if maxRunes <= 0 {
		return cleaned, false, true
	}
	runes := []rune(cleaned)
	if len(runes) <= maxRunes {
		return cleaned, false, true
	}
	return string(runes[:maxRunes]) + "...", true, true
}

func markdownInline(value string) string {
	var builder strings.Builder
	for _, r := range value {
		switch r {
		case '\\', '*', '_', '[', ']', '`':
			builder.WriteByte('\\')
			builder.WriteRune(r)
		default:
			builder.WriteRune(r)
		}
	}
	return builder.String()
}
