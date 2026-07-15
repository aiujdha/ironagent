package core

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"path"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"ironagent/go-gateway/internal/southbound"
)

type Southbound interface {
	GetUser(ctx context.Context, userID string) (southbound.User, error)
	ListUserAssistants(ctx context.Context, userID string) ([]southbound.UserAssistantBinding, error)
	GetAssistant(ctx context.Context, assistantID string) (southbound.Assistant, error)
	ListAssistantContext(ctx context.Context) ([]southbound.AssistantContextEntry, error)
	StartAssistant(ctx context.Context, assistantID string) (southbound.AssistantRuntimeControlResponse, error)
	StopAssistant(ctx context.Context, assistantID string) (southbound.AssistantRuntimeControlResponse, error)
	ResolveControlUILaunchTicket(ctx context.Context, ticket string, origin string) (southbound.ControlUILaunchTicketResolveResponse, error)
	CreateSharedObject(ctx context.Context, request southbound.SharedObjectCreateRequest) (southbound.SharedObject, error)
	CreateTask(ctx context.Context, request southbound.CreateTaskRequest) (southbound.Task, error)
	GetTask(ctx context.Context, taskID string) (southbound.Task, error)
	GetApproval(ctx context.Context, approvalID string) (southbound.ApprovalStatus, error)
	ListApprovals(ctx context.Context, status string, assistantID string, approvalScope string) ([]southbound.ApprovalStatus, error)
	ResolveApproval(ctx context.Context, approvalID, resolution string) (southbound.ApprovalResolution, error)
	CancelTask(ctx context.Context, taskID string, request southbound.CancelTaskRequest) (southbound.Task, error)
	ClaimTask(ctx context.Context, taskID string, assistantID string) (southbound.ClaimResponse, error)
	RenewTaskClaim(ctx context.Context, taskID string, request southbound.RenewClaimRequest) (southbound.ClaimResponse, error)
	StartTask(ctx context.Context, taskID string, request southbound.StartTaskRequest) (southbound.Task, error)
	CompleteTask(ctx context.Context, taskID string, request southbound.CompleteTaskRequest) (southbound.Task, error)
	FailTask(ctx context.Context, taskID string, request southbound.FailTaskRequest) (southbound.Task, error)
	UploadFile(ctx context.Context, request southbound.FileUploadRequest) (southbound.RawJSON, error)
	ListFiles(ctx context.Context, request southbound.FileListRequest) (southbound.RawJSON, error)
	SyncResults(ctx context.Context, request southbound.SyncResultsRequest) (southbound.WorkspaceScanSummary, error)
	GetFile(ctx context.Context, fileID string) (southbound.RawJSON, error)
	DownloadFile(ctx context.Context, fileID string, disposition string) (southbound.FileDownloadResponse, error)
	DownloadFileStream(ctx context.Context, fileID string, disposition string) (southbound.DownloadReader, error)
	DeleteFile(ctx context.Context, fileID string, userID string) (southbound.RawJSON, error)
	CheckHealth(ctx context.Context) (southbound.HealthSummary, error)
}

func (s *Service) UserExists(ctx context.Context, userID string) (bool, error) {
	userID = strings.TrimSpace(userID)
	if userID == "" {
		return false, nil
	}
	if s.southbound == nil {
		return false, nil
	}
	if _, err := s.southbound.GetUser(ctx, userID); err != nil {
		var httpErr *southbound.HTTPError
		if errors.As(err, &httpErr) && httpErr.StatusCode == http.StatusNotFound {
			return false, nil
		}
		return false, err
	}
	return true, nil
}

type RuntimeScheduler interface {
	SelectTarget(ctx context.Context, request RuntimeScheduleRequest) (string, error)
	Reserve(ctx context.Context, assistantID string) error
	Release(ctx context.Context, assistantID string)
}

type RuntimeRouteLeaseScheduler interface {
	ReserveRuntimeRoute(ctx context.Context, assistantID string) (RuntimeReservation, error)
	RenewRuntimeRouteLease(ctx context.Context, reservation RuntimeReservation) (RuntimeReservation, error)
	ReleaseRuntimeRoute(ctx context.Context, reservation RuntimeReservation)
}

type RuntimeRouteResolver interface {
	ResolveRuntimeRoute(ctx context.Context, assistantID string) (RuntimeRoute, bool, error)
}

type AssistantResolutionCache interface {
	GetAssistantForUser(ctx context.Context, userID string) (string, bool, error)
	SetAssistantForUser(ctx context.Context, userID string, assistantID string) error
	GetAssistantAvailable(ctx context.Context, assistantID string) (bool, bool, error)
	SetAssistantAvailable(ctx context.Context, assistantID string, available bool) error
}

type ChatRateLimiter interface {
	Allow(ctx context.Context, key string) (bool, error)
}

type chatAdmissionCheckedContextKey struct{}

func WithChatAdmissionChecked(ctx context.Context) context.Context {
	if ctx == nil {
		ctx = context.Background()
	}
	return context.WithValue(ctx, chatAdmissionCheckedContextKey{}, true)
}

func chatAdmissionChecked(ctx context.Context) bool {
	if ctx == nil {
		return false
	}
	checked, _ := ctx.Value(chatAdmissionCheckedContextKey{}).(bool)
	return checked
}

type RuntimeScheduleRequest struct {
	PreferredAssistantID string
	SourceAssistantID    string
	RequiredCapabilities []string
}

type Service struct {
	southbound                  Southbound
	scheduler                   RuntimeScheduler
	tasks                       TaskStore
	queue                       TaskQueue
	queueMaxPending             int
	queueMaxPendingPerAssistant int
	defaultTaskTimeoutSeconds   int
	queuedTaskHook              func(context.Context, TaskView)
	queuedTaskProgressHook      func(context.Context, QueuedTaskProgressUpdate) error
	taskProgressStore           TaskProgressStore
	logger                      *slog.Logger
	runtimeExecutor             RuntimeExecutor
	runtimeRefresher            func(context.Context, string) error
	runtimeRouteResolver        RuntimeRouteResolver
	resolveMu                   sync.Mutex
	resolveCache                map[string]assistantResolutionCacheEntry
	resolveInflight             map[string]*assistantResolutionCall
	availableCache              map[string]assistantAvailabilityCacheEntry
	assistantCache              AssistantResolutionCache
	chatRateLimiter             ChatRateLimiter
}

const noAvailableAssistantMessage = "用户无可用助手，请在后台重新创建助手后再继续调用"
const assistantResolutionCacheTTL = 30 * time.Second
const assistantAvailabilityCacheTTL = 30 * time.Second
const defaultTaskTimeoutSeconds = 600
const runtimeResultFileSyncMaxAttempts = 30
const runtimeResultFileSyncRetryInterval = time.Second
const runtimeResultFileRecoveryInitialDelay = 250 * time.Millisecond

const taskMetadataQueueCapacityWaitCount = "queue_capacity_wait_count"
const taskMetadataQueueCapacityWaitFirstAt = "queue_capacity_wait_first_at"
const taskMetadataQueueCapacityWaitLastAt = "queue_capacity_wait_last_at"
const taskMetadataQueueCapacityWaitLastCallbackAt = "queue_capacity_wait_last_callback_at"

type assistantResolutionCacheEntry struct {
	assistantID string
	expiresAt   time.Time
}

type assistantAvailabilityCacheEntry struct {
	available bool
	expiresAt time.Time
}

type assistantResolutionCall struct {
	done        chan struct{}
	assistantID string
	err         error
}

type southboundExecutionHooks struct {
	afterCreate     func(context.Context, southbound.Task)
	afterStart      func(context.Context, southbound.Task)
	runtimeProgress func(context.Context, RuntimeProgressEvent)
	resumeRunning   func(context.Context, southbound.Task) (TaskView, error)
	failAttempt     func(error)
	staleTimeout    time.Duration
	deadlineSource  string
	gatewayTaskID   string
	userID          string
	employeeNo      string
}

type queuedProgressRecorder func(context.Context, string, string, string) (bool, error)

var errQueuedAttemptLost = errors.New("queued task attempt is no longer active")

const leaseHeartbeatFailureThreshold = 3
const queuedSouthboundPollIntervalMin = 10 * time.Millisecond
const queuedSouthboundPollIntervalMax = 5 * time.Second
const queuedSouthboundCancelTimeout = 5 * time.Second

type QueueLeaseHeartbeatError struct {
	TaskID string
	Cause  error
}

func (e QueueLeaseHeartbeatError) Error() string {
	if e.Cause == nil {
		return "queue lease heartbeat failed"
	}
	return fmt.Sprintf("queue lease heartbeat failed for task %s: %v", e.TaskID, e.Cause)
}

func (e QueueLeaseHeartbeatError) Unwrap() error {
	return e.Cause
}

type retryableQueueError struct {
	Cause error
}

func (e retryableQueueError) Error() string {
	if e.Cause == nil {
		return "retryable queue error"
	}
	return e.Cause.Error()
}

func (e retryableQueueError) Unwrap() error {
	return e.Cause
}

type activeSouthboundTaskError struct {
	SouthboundTaskID string
	Status           string
	Timeout          time.Duration
}

func (e activeSouthboundTaskError) Error() string {
	status := strings.TrimSpace(firstNonEmptyString(e.Status, "running"))
	if strings.TrimSpace(e.SouthboundTaskID) == "" {
		return fmt.Sprintf("collaboration task is still %s after %s", status, e.Timeout)
	}
	return fmt.Sprintf("collaboration task %s is still %s after %s", e.SouthboundTaskID, status, e.Timeout)
}

type ClaimHeartbeatError struct {
	TaskID string
	Cause  error
}

func (e ClaimHeartbeatError) Error() string {
	if e.Cause == nil {
		return "collaboration claim heartbeat failed"
	}
	return fmt.Sprintf("collaboration claim heartbeat failed for task %s: %v", e.TaskID, e.Cause)
}

func (e ClaimHeartbeatError) Unwrap() error {
	return e.Cause
}

func NewService(southbound Southbound) *Service {
	return &Service{southbound: southbound}
}

func NewServiceWithScheduler(southbound Southbound, scheduler RuntimeScheduler) *Service {
	return &Service{southbound: southbound, scheduler: scheduler}
}

func NewServiceWithTaskStore(southbound Southbound, tasks TaskStore) *Service {
	return &Service{southbound: southbound, tasks: tasks}
}

func NewServiceWithOptions(southbound Southbound, scheduler RuntimeScheduler, tasks TaskStore) *Service {
	return &Service{southbound: southbound, scheduler: scheduler, tasks: tasks}
}

func NewServiceWithQueue(
	southbound Southbound,
	scheduler RuntimeScheduler,
	tasks TaskStore,
	queue TaskQueue,
) *Service {
	return &Service{southbound: southbound, scheduler: scheduler, tasks: tasks, queue: queue}
}

func (s *Service) SetTaskQueueMaxPending(maxPending int) {
	s.queueMaxPending = maxPending
}

func (s *Service) SetTaskQueueMaxPendingPerAssistant(maxPending int) {
	s.queueMaxPendingPerAssistant = maxPending
}

func (s *Service) SetDefaultTaskTimeout(timeout time.Duration) {
	if timeout <= 0 {
		s.defaultTaskTimeoutSeconds = 0
		return
	}
	seconds := int(timeout / time.Second)
	if timeout%time.Second != 0 {
		seconds++
	}
	if seconds <= 0 {
		seconds = 1
	}
	s.defaultTaskTimeoutSeconds = seconds
}

func (s *Service) SetQueuedTaskHook(hook func(context.Context, TaskView)) {
	s.queuedTaskHook = hook
}

func (s *Service) SetQueuedTaskProgressHook(hook func(context.Context, QueuedTaskProgressUpdate) error) {
	s.queuedTaskProgressHook = hook
}

func (s *Service) SetTaskProgressStore(store TaskProgressStore) {
	s.taskProgressStore = store
}

func (s *Service) SetLogger(logger *slog.Logger) {
	s.logger = logger
}

func (s *Service) SetRuntimeExecutor(executor RuntimeExecutor) {
	s.runtimeExecutor = executor
}

func (s *Service) SetRuntimeRefresher(refresher func(context.Context, string) error) {
	s.runtimeRefresher = refresher
}

func (s *Service) SetRuntimeRouteResolver(resolver RuntimeRouteResolver) {
	s.runtimeRouteResolver = resolver
}

func (s *Service) SetAssistantResolutionCache(cache AssistantResolutionCache) {
	s.assistantCache = cache
}

func (s *Service) SetChatRateLimiter(limiter ChatRateLimiter) {
	s.chatRateLimiter = limiter
}

func (s *Service) logInfo(message string, attrs ...any) {
	if s.logger == nil {
		return
	}
	s.logger.Info(message, attrs...)
}

func (s *Service) logWarn(message string, attrs ...any) {
	if s.logger == nil {
		return
	}
	s.logger.Warn(message, attrs...)
}

func chatRequestLogAttrs(request ChatRequest) []any {
	attrs := []any{
		"request_id", strings.TrimSpace(request.RequestID),
		"client_id", strings.TrimSpace(request.ClientID),
		"conversation_id", strings.TrimSpace(request.ConversationID),
		"user_id", strings.TrimSpace(request.UserID),
		"employee_no", strings.TrimSpace(request.EmployeeNo),
		"request_source_assistant_id", strings.TrimSpace(request.SourceAssistantID),
		"request_target_assistant_id", strings.TrimSpace(request.TargetAssistantID),
		"request_input_object_id", strings.TrimSpace(request.InputObjectID),
		"callback_configured", strings.TrimSpace(request.CallbackURL) != "",
		"input_files_count", len(request.Files),
		"workspace_files_count", len(workspaceFileRefsFromRequest(request)),
	}
	return append(attrs, chatQuestionLogAttrs(request)...)
}

const chatQuestionLogMaxRunes = 2048

func chatQuestionLogAttrs(request ChatRequest) []any {
	source := "message"
	question := strings.TrimSpace(request.Message)
	if question == "" {
		source = "instruction"
		question = strings.TrimSpace(request.Instruction)
	}
	if question == "" {
		return nil
	}
	runes := []rune(question)
	preview := question
	truncated := false
	if len(runes) > chatQuestionLogMaxRunes {
		preview = string(runes[:chatQuestionLogMaxRunes])
		truncated = true
	}
	sum := sha256.Sum256([]byte(question))
	return []any{
		"question_source", source,
		"question_text", preview,
		"question_chars", len(runes),
		"question_bytes", len([]byte(question)),
		"question_truncated", truncated,
		"question_sha256", hex.EncodeToString(sum[:]),
	}
}

func queuedTaskLogAttrs(item GatewayTaskQueueItem) []any {
	attrs := chatRequestLogAttrs(item.Request)
	return append(attrs,
		"task_id", strings.TrimSpace(item.TaskID),
		"queue_status", strings.TrimSpace(item.Status),
		"locked_by", strings.TrimSpace(item.LockedBy),
		"attempt_token", strings.TrimSpace(item.AttemptToken),
		"attempts", item.Attempts,
	)
}

func queuedTaskClaimLogAttrs(items []GatewayTaskQueueItem) []any {
	taskIDs := make([]string, 0, len(items))
	claimedTasks := make([]map[string]any, 0, len(items))
	for _, item := range items {
		taskID := strings.TrimSpace(item.TaskID)
		if taskID != "" {
			taskIDs = append(taskIDs, taskID)
		}
		claimedTasks = append(claimedTasks, map[string]any{
			"task_id":       taskID,
			"request_id":    strings.TrimSpace(item.Request.RequestID),
			"user_id":       strings.TrimSpace(item.Request.UserID),
			"queue_status":  strings.TrimSpace(item.Status),
			"attempts":      item.Attempts,
			"locked_by":     strings.TrimSpace(item.LockedBy),
			"attempt_token": strings.TrimSpace(item.AttemptToken),
		})
	}
	return []any{
		"claimed_task_ids", taskIDs,
		"claimed_tasks", claimedTasks,
	}
}

func taskViewResultLogAttrs(task TaskView) []any {
	return []any{
		"status", strings.TrimSpace(task.Status),
		"stage", strings.TrimSpace(NewTaskResponse(task).Stage),
		"source_assistant_id", strings.TrimSpace(task.SourceAssistantID),
		"target_assistant_id", strings.TrimSpace(task.TargetAssistantID),
		"result_workspace_file_id", strings.TrimSpace(task.ResultWorkspaceFileID),
		"result_workspace_file_ids", normalizeResultWorkspaceFileIDs(task.ResultWorkspaceFileID, metadataStringSlice(task.Metadata, taskMetadataRuntimeResultWorkspaceFileIDs)),
		"result_files_count", len(normalizeResultWorkspaceFileIDs(task.ResultWorkspaceFileID, metadataStringSlice(task.Metadata, taskMetadataRuntimeResultWorkspaceFileIDs))),
	}
}

func metadataStringSlice(metadata map[string]any, key string) []string {
	if len(metadata) == 0 {
		return nil
	}
	value, ok := metadata[key]
	if !ok {
		return nil
	}
	switch typed := value.(type) {
	case []string:
		return append([]string(nil), typed...)
	case []any:
		out := make([]string, 0, len(typed))
		for _, item := range typed {
			if text := strings.TrimSpace(rawString(item)); text != "" {
				out = append(out, text)
			}
		}
		return out
	default:
		if text := strings.TrimSpace(rawString(value)); text != "" {
			return []string{text}
		}
		return nil
	}
}

func metadataInt(metadata map[string]any, key string) int {
	if len(metadata) == 0 {
		return 0
	}
	value, ok := metadata[key]
	if !ok {
		return 0
	}
	switch typed := value.(type) {
	case int:
		return typed
	case int64:
		return int(typed)
	case float64:
		return int(typed)
	case json.Number:
		if n, err := typed.Int64(); err == nil {
			return int(n)
		}
	case string:
		var parsed int
		if _, err := fmt.Sscanf(strings.TrimSpace(typed), "%d", &parsed); err == nil {
			return parsed
		}
	}
	return 0
}

func metadataTime(metadata map[string]any, key string) (time.Time, bool) {
	if len(metadata) == 0 {
		return time.Time{}, false
	}
	value, ok := metadata[key]
	if !ok {
		return time.Time{}, false
	}
	switch typed := value.(type) {
	case time.Time:
		return typed.UTC(), true
	case string:
		if parsed, err := time.Parse(time.RFC3339Nano, strings.TrimSpace(typed)); err == nil {
			return parsed.UTC(), true
		}
	}
	return time.Time{}, false
}

func normalizeClientID(clientID string) string {
	clientID = strings.TrimSpace(clientID)
	if clientID == "" {
		return "default"
	}
	return clientID
}

func hashChatRequest(request ChatRequest) (string, error) {
	payload, err := json.Marshal(request)
	if err != nil {
		return "", fmt.Errorf("hash chat request: %w", err)
	}
	sum := sha256.Sum256(payload)
	return hex.EncodeToString(sum[:]), nil
}

func newGatewayTaskID() (string, error) {
	var raw [16]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return "", fmt.Errorf("generate gateway task id: %w", err)
	}
	return "gw-" + hex.EncodeToString(raw[:]), nil
}

func (s *Service) CreateChatTask(ctx context.Context, request ChatRequest) (TaskView, error) {
	request.ClientID = normalizeClientID(request.ClientID)
	request.RequestID = strings.TrimSpace(request.RequestID)
	if request.RequestID != "" {
		request.IdempotencyKey = request.RequestID
	}
	request.IdempotencyKey = strings.TrimSpace(request.IdempotencyKey)
	request = normalizeChatResultFileRequirement(request)
	if !chatAdmissionChecked(ctx) {
		if err := s.CheckChatAdmission(ctx, request); err != nil {
			return TaskView{}, err
		}
		ctx = WithChatAdmissionChecked(ctx)
	}
	requestHash := ""
	if s.tasks != nil && request.IdempotencyKey != "" {
		var err error
		requestHash, err = hashChatRequest(request)
		if err != nil {
			return TaskView{}, err
		}
		record, ok, err := s.tasks.FindByIdempotency(ctx, request.ClientID, request.IdempotencyKey)
		if err != nil {
			return TaskView{}, err
		}
		if ok {
			if record.RequestHash != "" && record.RequestHash != requestHash {
				return TaskView{}, &ValidationError{Message: "idempotency_key was already used with a different request"}
			}
			task := record.Task
			task.IdempotencyReplayed = true
			return task, nil
		}
	}

	if s.queue != nil {
		return s.enqueueChatTask(ctx, request, requestHash)
	}
	return s.executeAndSaveChatTask(ctx, request, requestHash)
}

func (s *Service) CheckChatAdmission(ctx context.Context, request ChatRequest) error {
	request.ClientID = normalizeClientID(request.ClientID)
	if err := s.checkChatRateLimit(ctx, request); err != nil {
		return err
	}
	if err := s.checkTaskQueueCapacity(ctx); err != nil {
		return err
	}
	if !s.taskQueueAssistantCapacityEnabled() {
		return nil
	}
	targetAssistantID := strings.TrimSpace(request.TargetAssistantID)
	if targetAssistantID == "" {
		var err error
		targetAssistantID, err = s.resolveChatPreferredTargetAssistantID(ctx, request)
		if err != nil {
			return err
		}
	}
	return s.checkTaskQueueAssistantCapacity(ctx, targetAssistantID)
}

func (s *Service) chatTaskTimeoutSeconds(requestTimeoutSeconds int) int {
	if requestTimeoutSeconds > 0 {
		return requestTimeoutSeconds
	}
	if s.defaultTaskTimeoutSeconds > 0 {
		return s.defaultTaskTimeoutSeconds
	}
	return defaultTaskTimeoutSeconds
}

func (s *Service) enqueueChatTask(ctx context.Context, request ChatRequest, requestHash string) (TaskView, error) {
	if strings.TrimSpace(request.Instruction) == "" && strings.TrimSpace(request.Message) == "" {
		return TaskView{}, &ValidationError{Message: "instruction or message is required"}
	}
	if !chatAdmissionChecked(ctx) {
		if err := s.checkTaskQueueCapacity(ctx); err != nil {
			return TaskView{}, err
		}
	}
	preferredTargetAssistantID, err := s.resolveChatPreferredTargetAssistantID(ctx, request)
	if err != nil {
		return TaskView{}, err
	}
	if strings.TrimSpace(request.TargetAssistantID) == "" {
		request.TargetAssistantID = preferredTargetAssistantID
	}
	if err := s.checkTaskQueueAssistantCapacity(ctx, request.TargetAssistantID); err != nil {
		s.logWarn(
			"gateway_task_assistant_queue_full",
			append([]any{"error", err}, chatRequestLogAttrs(request)...)...,
		)
		return TaskView{}, err
	}
	if strings.TrimSpace(request.TargetAssistantID) != "" {
		if err := s.bindChatConversationRoute(ctx, request, request.TargetAssistantID); err != nil {
			return TaskView{}, err
		}
	}
	now := time.Now().UTC()
	taskID, err := newGatewayTaskID()
	if err != nil {
		return TaskView{}, err
	}
	instruction := strings.TrimSpace(request.Instruction)
	if instruction == "" {
		instruction = strings.TrimSpace(request.Message)
	}
	createdBy := strings.TrimSpace(request.CreatedBy)
	if createdBy == "" {
		createdBy = strings.TrimSpace(request.UserID)
	}
	if createdBy == "" {
		createdBy = "go-gateway"
	}
	timeoutSeconds := s.chatTaskTimeoutSeconds(request.TimeoutSeconds)
	approvalMode := strings.TrimSpace(request.ApprovalMode)
	if approvalMode == "" {
		approvalMode = "none"
	}
	task := TaskView{
		ID:                taskID,
		SourceAssistantID: strings.TrimSpace(request.SourceAssistantID),
		TargetAssistantID: strings.TrimSpace(request.TargetAssistantID),
		InputObjectID:     strings.TrimSpace(request.InputObjectID),
		Instruction:       instruction,
		Status:            "pending_claim",
		ApprovalMode:      approvalMode,
		TimeoutSeconds:    timeoutSeconds,
		CreatedBy:         createdBy,
		CreatedAt:         now,
		Metadata:          map[string]any{"queue_status": "admitting"},
	}
	if chatRequestRequiresResultFile(request) {
		task.Metadata[taskMetadataRequiresResultFile] = true
	}
	taskRecord := GatewayTaskRecord{
		Task:           task,
		ClientID:       request.ClientID,
		RequestID:      strings.TrimSpace(request.RequestID),
		ConversationID: strings.TrimSpace(request.ConversationID),
		UserID:         strings.TrimSpace(request.UserID),
		EmployeeNo:     strings.TrimSpace(request.EmployeeNo),
		CallbackURL:    strings.TrimSpace(request.CallbackURL),
		IdempotencyKey: strings.TrimSpace(request.IdempotencyKey),
		RequestHash:    requestHash,
		CreatedAt:      now,
	}
	queueItem := GatewayTaskQueueItem{
		TaskID:      taskID,
		Request:     request,
		RequestHash: requestHash,
		Status:      "admitting",
		CreatedAt:   now,
	}
	if creator, ok := s.tasks.(QueuedTaskCreator); ok && s.queue != nil {
		created, replayed, err := creator.CreateQueuedTask(ctx, taskRecord, queueItem)
		if err != nil {
			s.logWarn(
				"gateway_task_admit_failed",
				append([]any{"task_id", taskID, "error", err}, chatRequestLogAttrs(request)...)...,
			)
			return TaskView{}, err
		}
		replayedTask := created.Task
		replayedTask.IdempotencyReplayed = replayed
		if replayed {
			return replayedTask, nil
		}
		task = created.Task
	} else {
		if err := s.queue.EnqueueTask(ctx, queueItem); err != nil {
			s.logWarn(
				"gateway_task_enqueue_failed",
				append([]any{"task_id", taskID, "error", err}, chatRequestLogAttrs(request)...)...,
			)
			return TaskView{}, err
		}
		if s.tasks != nil {
			if err := s.tasks.UpsertTask(ctx, taskRecord); err != nil {
				_ = s.queue.CancelQueuedTask(ctx, taskID, "task record write failed")
				s.logWarn(
					"gateway_task_record_write_failed",
					append([]any{"task_id", taskID, "error", err}, chatRequestLogAttrs(request)...)...,
				)
				return TaskView{}, err
			}
		}
	}
	s.logInfo(
		"gateway_task_accepted",
		append(
			[]any{
				"task_id", taskID,
				"queue_status", "admitting",
				"timeout_seconds", timeoutSeconds,
				"approval_mode", approvalMode,
			},
			chatRequestLogAttrs(request)...,
		)...,
	)
	return task, nil
}

func (s *Service) ActivateQueuedTask(ctx context.Context, taskID string) error {
	taskID = strings.TrimSpace(taskID)
	if taskID == "" {
		return &ValidationError{Message: "task_id is required"}
	}
	if s.queue == nil {
		return nil
	}
	if s.tasks != nil {
		record, ok, err := s.tasks.GetTask(ctx, taskID)
		if err != nil {
			return err
		}
		if !ok {
			return &ValidationError{Message: "task not found"}
		}
		if taskStatusIsTerminal(record.Task.Status) {
			return nil
		}
		if record.Task.Metadata == nil {
			record.Task.Metadata = map[string]any{}
		}
		record.Task.Metadata["queue_status"] = "queued"
		if err := s.tasks.UpsertTask(ctx, record); err != nil {
			return err
		}
	}
	if err := s.queue.ActivateQueuedTask(ctx, taskID); err != nil {
		s.logWarn("gateway_task_activate_failed", "task_id", taskID, "error", err)
		return err
	}
	s.logInfo("gateway_task_activated", "task_id", taskID, "queue_status", "queued")
	return nil
}

func (s *Service) StoredTaskRecord(ctx context.Context, taskID string) (GatewayTaskRecord, bool, error) {
	taskID = strings.TrimSpace(taskID)
	if taskID == "" || s.tasks == nil {
		return GatewayTaskRecord{}, false, nil
	}
	return s.tasks.GetTask(ctx, taskID)
}

func (s *Service) executeAndSaveChatTask(ctx context.Context, request ChatRequest, requestHash string) (TaskView, error) {
	// Sync path: plumb the deadline source from the caller's own timeout so a
	// gateway-default deadline is not mis-attributed to UPSTREAM (review #4).
	// staleTimeout stays 0 here, so the A1 widening guard (#7) keeps the hard cap.
	started, err := s.executeChatTaskSouthbound(ctx, request, &southboundExecutionHooks{
		deadlineSource: deadlineSourceForTimeoutSeconds(request.TimeoutSeconds),
		userID:         strings.TrimSpace(request.UserID),
		employeeNo:     strings.TrimSpace(request.EmployeeNo),
	})
	if err != nil {
		return TaskView{}, err
	}
	return s.saveCreatedTask(ctx, request, started, requestHash)
}

func (s *Service) executeChatTaskSouthbound(
	ctx context.Context,
	request ChatRequest,
	hooks *southboundExecutionHooks,
) (TaskView, error) {
	sourceAssistantID := resolveChatSourceAssistantID(request)
	workspaceFiles := workspaceFileRefsFromRequest(request)

	preferredTargetAssistantID, err := s.resolveChatPreferredTargetAssistantID(ctx, request)
	if err != nil {
		s.logWarn(
			"gateway_task_target_resolve_failed",
			append([]any{"error", err}, chatRequestLogAttrs(request)...)...,
		)
		return TaskView{}, err
	}
	targetAssistantID, err := s.selectTargetAssistantID(
		ctx,
		sourceAssistantID,
		preferredTargetAssistantID,
		request.RequiredCapabilities,
	)
	if err != nil {
		s.logWarn(
			"gateway_task_target_select_failed",
			append(
				[]any{
					"preferred_target_assistant_id", strings.TrimSpace(preferredTargetAssistantID),
					"required_capabilities_count", len(request.RequiredCapabilities),
					"error", err,
				},
				chatRequestLogAttrs(request)...,
			)...,
		)
		return TaskView{}, err
	}
	s.logInfo(
		"gateway_task_target_selected",
		append(
			[]any{
				"preferred_target_assistant_id", strings.TrimSpace(preferredTargetAssistantID),
				"selected_target_assistant_id", strings.TrimSpace(targetAssistantID),
				"required_capabilities_count", len(request.RequiredCapabilities),
			},
			chatRequestLogAttrs(request)...,
		)...,
	)
	if sourceAssistantID == targetAssistantID {
		return TaskView{}, &ValidationError{Message: "source_assistant_id and target_assistant_id must be different"}
	}
	if err := s.bindChatConversationRoute(ctx, request, targetAssistantID); err != nil {
		return TaskView{}, err
	}

	inputObjectID := strings.TrimSpace(request.InputObjectID)
	inputObjectSource := "provided_input_object"
	if inputObjectID == "" {
		if request.WorkspaceFile == nil {
			if len(workspaceFiles) > 0 {
				first := workspaceFiles[0]
				request.WorkspaceFile = &first
			} else {
				request.WorkspaceFile = workspaceFileFromFileIDs(request.Files)
			}
		}
		if request.WorkspaceFile != nil {
			inputObjectSource = "workspace_file"
			inputObjectID, err = s.createSharedObjectFromWorkspaceFile(ctx, sourceAssistantID, request)
		} else {
			inputObjectSource = "message"
			inputObjectID, err = s.createSharedObjectFromMessage(ctx, sourceAssistantID, request)
		}
		if err != nil {
			s.logWarn(
				"gateway_task_input_object_create_failed",
				append(
					[]any{
						"input_object_source", inputObjectSource,
						"source_assistant_id", sourceAssistantID,
						"target_assistant_id", targetAssistantID,
						"error", err,
					},
					chatRequestLogAttrs(request)...,
				)...,
			)
			return TaskView{}, err
		}
	}
	s.logInfo(
		"gateway_task_input_object_ready",
		append(
			[]any{
				"input_object_id", inputObjectID,
				"input_object_source", inputObjectSource,
				"source_assistant_id", sourceAssistantID,
				"target_assistant_id", targetAssistantID,
			},
			chatRequestLogAttrs(request)...,
		)...,
	)

	approvalMode := strings.TrimSpace(request.ApprovalMode)
	if approvalMode == "" {
		approvalMode = "none"
	}

	timeoutSeconds := s.chatTaskTimeoutSeconds(request.TimeoutSeconds)

	createdBy := strings.TrimSpace(request.CreatedBy)
	if createdBy == "" {
		createdBy = strings.TrimSpace(request.UserID)
	}
	if createdBy == "" {
		createdBy = "go-gateway"
	}

	instruction := strings.TrimSpace(request.Instruction)
	if instruction == "" {
		instruction = strings.TrimSpace(request.Message)
	}
	if instruction == "" {
		return TaskView{}, &ValidationError{Message: "instruction or message is required"}
	}
	instruction = withWorkspaceFileLookupGuidance(instruction, workspaceFiles)

	taskMetaJSON := taskMetaJSONFromChatRequest(request)
	task, err := s.southbound.CreateTask(ctx, southbound.CreateTaskRequest{
		SourceAssistantID: sourceAssistantID,
		TargetAssistantID: targetAssistantID,
		InputObjectID:     inputObjectID,
		Instruction:       instruction,
		ApprovalMode:      approvalMode,
		IdempotencyKey:    strings.TrimSpace(request.IdempotencyKey),
		CreatedBy:         createdBy,
		TimeoutSeconds:    timeoutSeconds,
		MaxDepth:          0,
		MetaJSON:          taskMetaJSON,
	})
	if err != nil {
		s.logWarn(
			"gateway_southbound_task_create_failed",
			append(
				[]any{
					"input_object_id", inputObjectID,
					"source_assistant_id", sourceAssistantID,
					"target_assistant_id", targetAssistantID,
					"error", err,
				},
				chatRequestLogAttrs(request)...,
			)...,
		)
		return TaskView{}, err
	}
	mergeSouthboundTaskMetaJSON(&task, taskMetaJSON)
	s.logInfo(
		"gateway_southbound_task_created",
		append(
			[]any{
				"southbound_task_id", task.ID,
				"task_status", task.Status,
				"input_object_id", inputObjectID,
				"source_assistant_id", sourceAssistantID,
				"target_assistant_id", targetAssistantID,
			},
			chatRequestLogAttrs(request)...,
		)...,
	)

	if hooks != nil && hooks.afterCreate != nil {
		hooks.afterCreate(ctx, task)
	}
	return s.startCreatedTask(ctx, task, hooks)
}

func (s *Service) saveCreatedTask(
	ctx context.Context,
	request ChatRequest,
	task TaskView,
	requestHash string,
) (TaskView, error) {
	if s.tasks == nil {
		return task, nil
	}
	task.IdempotencyReplayed = false
	record := GatewayTaskRecord{
		Task:             task,
		ClientID:         request.ClientID,
		RequestID:        strings.TrimSpace(request.RequestID),
		ConversationID:   strings.TrimSpace(request.ConversationID),
		UserID:           strings.TrimSpace(request.UserID),
		EmployeeNo:       strings.TrimSpace(request.EmployeeNo),
		CallbackURL:      strings.TrimSpace(request.CallbackURL),
		IdempotencyKey:   strings.TrimSpace(request.IdempotencyKey),
		RequestHash:      requestHash,
		SouthboundTaskID: task.ID,
		CreatedAt:        task.CreatedAt,
	}
	if err := s.tasks.UpsertTask(ctx, record); err != nil {
		return TaskView{}, err
	}
	return task, nil
}

func (s *Service) saveTaskView(ctx context.Context, task TaskView) (TaskView, error) {
	if s.tasks == nil {
		return task, nil
	}
	task.IdempotencyReplayed = false
	record, ok, err := s.tasks.GetTask(ctx, task.ID)
	if err != nil {
		return TaskView{}, err
	}
	if !ok {
		record = GatewayTaskRecord{
			Task:             task,
			SouthboundTaskID: task.ID,
			CreatedAt:        task.CreatedAt,
		}
	}
	record.Task = task
	if strings.TrimSpace(record.SouthboundTaskID) == "" {
		record.SouthboundTaskID = task.ID
	}
	if err := s.tasks.UpsertTask(ctx, record); err != nil {
		return TaskView{}, err
	}
	return task, nil
}

func (s *Service) saveQueuedTaskResult(
	ctx context.Context,
	item GatewayTaskQueueItem,
	task TaskView,
	southboundTaskID string,
) (TaskView, error) {
	if s.tasks == nil {
		return task, nil
	}
	task.ID = item.TaskID
	task.IdempotencyReplayed = false
	record, ok, err := s.tasks.GetTask(ctx, item.TaskID)
	if err != nil {
		return TaskView{}, err
	}
	if !ok {
		record = GatewayTaskRecord{
			ClientID:       item.Request.ClientID,
			RequestID:      strings.TrimSpace(item.Request.RequestID),
			ConversationID: strings.TrimSpace(item.Request.ConversationID),
			UserID:         strings.TrimSpace(item.Request.UserID),
			EmployeeNo:     strings.TrimSpace(item.Request.EmployeeNo),
			CallbackURL:    strings.TrimSpace(item.Request.CallbackURL),
			IdempotencyKey: strings.TrimSpace(item.Request.IdempotencyKey),
			RequestHash:    item.RequestHash,
			CreatedAt:      task.CreatedAt,
		}
	}
	if existingSouthboundTaskID := strings.TrimSpace(record.SouthboundTaskID); existingSouthboundTaskID != "" &&
		existingSouthboundTaskID != southboundTaskID &&
		!isGatewayQueueInProgress(record.Task) {
		return record.Task, nil
	}
	if taskStatusIsTerminal(record.Task.Status) {
		if record.Task.Status == "cancelled" {
			_, _ = s.southbound.CancelTask(ctx, southboundTaskID, southbound.CancelTaskRequest{
				ActorID: "go-gateway",
				Reason:  "cancelled before queued task finished",
			})
		}
		s.releaseTarget(ctx, task.TargetAssistantID)
		return record.Task, nil
	}
	preserveStoredRuntimeMetadata(&task, record.Task.Metadata)
	clearCapacityWaitMetadata(task.Metadata)
	record.Task = task
	record.SouthboundTaskID = southboundTaskID
	if strings.TrimSpace(record.IdempotencyKey) == "" {
		record.IdempotencyKey = strings.TrimSpace(item.Request.IdempotencyKey)
	}
	if strings.TrimSpace(record.RequestHash) == "" {
		record.RequestHash = item.RequestHash
	}
	if err := s.tasks.UpsertTask(ctx, record); err != nil {
		return TaskView{}, err
	}
	return task, nil
}

func (s *Service) saveQueuedTaskCheckpoint(
	ctx context.Context,
	item GatewayTaskQueueItem,
	task southbound.Task,
	southboundTaskID string,
	queueStatus string,
) (TaskView, error) {
	view, err := s.enrichTask(ctx, task)
	if err != nil {
		return TaskView{}, err
	}
	view.ID = item.TaskID
	view.IdempotencyReplayed = false
	if s.tasks == nil {
		return view, nil
	}
	southboundTaskID = strings.TrimSpace(southboundTaskID)
	if southboundTaskID == "" {
		southboundTaskID = task.ID
	}
	record, ok, err := s.tasks.GetTask(ctx, item.TaskID)
	if err != nil {
		return TaskView{}, err
	}
	if !ok {
		record = GatewayTaskRecord{
			ClientID:       item.Request.ClientID,
			RequestID:      strings.TrimSpace(item.Request.RequestID),
			ConversationID: strings.TrimSpace(item.Request.ConversationID),
			UserID:         strings.TrimSpace(item.Request.UserID),
			EmployeeNo:     strings.TrimSpace(item.Request.EmployeeNo),
			CallbackURL:    strings.TrimSpace(item.Request.CallbackURL),
			IdempotencyKey: strings.TrimSpace(item.Request.IdempotencyKey),
			RequestHash:    item.RequestHash,
			CreatedAt:      view.CreatedAt,
		}
	}
	if existingSouthboundTaskID := strings.TrimSpace(record.SouthboundTaskID); existingSouthboundTaskID != "" &&
		existingSouthboundTaskID != southboundTaskID &&
		!isGatewayQueueInProgress(record.Task) {
		return record.Task, nil
	}
	if taskStatusIsTerminal(record.Task.Status) {
		return record.Task, nil
	}
	mergeTaskMetadata(&view, record.Task.Metadata)
	if queueStatus = strings.TrimSpace(queueStatus); queueStatus != "" {
		if view.Metadata == nil {
			view.Metadata = map[string]any{}
		}
		view.Metadata["queue_status"] = queueStatus
	}
	if strings.TrimSpace(queueStatus) == "processing" {
		clearCapacityWaitMetadata(view.Metadata)
	}
	record.Task = view
	record.SouthboundTaskID = southboundTaskID
	if strings.TrimSpace(record.IdempotencyKey) == "" {
		record.IdempotencyKey = strings.TrimSpace(item.Request.IdempotencyKey)
	}
	if strings.TrimSpace(record.RequestHash) == "" {
		record.RequestHash = item.RequestHash
	}
	if record.CreatedAt.IsZero() {
		record.CreatedAt = view.CreatedAt
	}
	if err := s.tasks.UpsertTask(ctx, record); err != nil {
		return TaskView{}, err
	}
	return view, nil
}

func mergeTaskMetadata(view *TaskView, stored map[string]any) {
	if view == nil || len(stored) == 0 {
		return
	}
	for key, value := range stored {
		if view.Metadata == nil {
			view.Metadata = map[string]any{}
		}
		if _, exists := view.Metadata[key]; !exists {
			view.Metadata[key] = value
		}
	}
}

func (s *Service) getStoredTask(
	ctx context.Context,
	taskID string,
) (GatewayTaskRecord, bool, error) {
	if s.tasks == nil {
		return GatewayTaskRecord{}, false, nil
	}
	return s.tasks.GetTask(ctx, taskID)
}

func (s *Service) resolveTaskIDs(
	ctx context.Context,
	taskID string,
) (externalTaskID string, southboundTaskID string, stored GatewayTaskRecord, hasStored bool, err error) {
	taskID = strings.TrimSpace(taskID)
	if taskID == "" {
		return "", "", GatewayTaskRecord{}, false, &ValidationError{Message: "task_id is required"}
	}
	stored, hasStored, err = s.getStoredTask(ctx, taskID)
	if err != nil {
		return "", "", GatewayTaskRecord{}, false, err
	}
	if hasStored {
		externalTaskID = stored.Task.ID
		southboundTaskID = strings.TrimSpace(stored.SouthboundTaskID)
		if externalTaskID == "" {
			externalTaskID = taskID
		}
		if southboundTaskID == "" {
			return externalTaskID, "", stored, true, nil
		}
		return externalTaskID, southboundTaskID, stored, true, nil
	}
	return taskID, taskID, GatewayTaskRecord{}, false, nil
}

func (s *Service) GetRuntime(ctx context.Context, request RuntimeRequest) (RuntimeView, error) {
	assistantID, err := s.resolveAssistantID(ctx, request.AssistantID, request.UserID)
	if err != nil {
		return RuntimeView{}, err
	}
	assistant, err := s.southbound.GetAssistant(ctx, assistantID)
	if err != nil {
		return RuntimeView{}, err
	}
	return runtimeViewFromAssistant(assistant), nil
}

func (s *Service) ListRuntimeAssistantIDs(ctx context.Context) ([]string, error) {
	entries, err := s.southbound.ListAssistantContext(ctx)
	if err != nil {
		return nil, err
	}
	seen := make(map[string]struct{}, len(entries))
	for _, entry := range entries {
		assistantID := strings.TrimSpace(entry.ID)
		if assistantID == "" || !runtimeContextStatusDiscoverable(entry.Status) {
			continue
		}
		seen[assistantID] = struct{}{}
	}
	ids := make([]string, 0, len(seen))
	for assistantID := range seen {
		ids = append(ids, assistantID)
	}
	sort.Strings(ids)
	return ids, nil
}

func runtimeContextStatusDiscoverable(status string) bool {
	switch strings.ToLower(strings.TrimSpace(status)) {
	case "", "ready", "running", "healthy", "starting":
		return true
	default:
		return false
	}
}

func (s *Service) StartRuntime(ctx context.Context, request RuntimeRequest) (RuntimeView, error) {
	assistantID, err := s.resolveAssistantID(ctx, request.AssistantID, request.UserID)
	if err != nil {
		return RuntimeView{}, err
	}
	if _, err := s.southbound.StartAssistant(ctx, assistantID); err != nil {
		return RuntimeView{}, err
	}
	return s.GetRuntime(ctx, RuntimeRequest{AssistantID: assistantID})
}

func (s *Service) StopRuntime(ctx context.Context, request RuntimeRequest) (RuntimeView, error) {
	assistantID, err := s.resolveAssistantID(ctx, request.AssistantID, request.UserID)
	if err != nil {
		return RuntimeView{}, err
	}
	if _, err := s.southbound.StopAssistant(ctx, assistantID); err != nil {
		return RuntimeView{}, err
	}
	return s.GetRuntime(ctx, RuntimeRequest{AssistantID: assistantID})
}

func (s *Service) ResolveControlUILaunchTicket(
	ctx context.Context,
	ticket string,
	origin string,
) (ControlUILaunchSession, error) {
	if strings.TrimSpace(ticket) == "" {
		return ControlUILaunchSession{}, &ValidationError{Message: "launch ticket is required"}
	}
	resolved, err := s.southbound.ResolveControlUILaunchTicket(ctx, ticket, origin)
	if err != nil {
		return ControlUILaunchSession{}, err
	}
	return ControlUILaunchSession{
		AssistantID:  strings.TrimSpace(resolved.AssistantID),
		GatewayURL:   strings.TrimSpace(resolved.GatewayURL),
		GatewayToken: strings.TrimSpace(resolved.GatewayToken),
		SessionKey:   strings.TrimSpace(resolved.SessionKey),
		ExpiresAt:    resolved.ExpiresAt.Time,
	}, nil
}

func (s *Service) GetTask(ctx context.Context, taskID string) (TaskView, error) {
	externalTaskID, southboundTaskID, stored, hasStored, err := s.resolveTaskIDs(ctx, taskID)
	if err != nil {
		return TaskView{}, err
	}
	if hasStored && strings.TrimSpace(southboundTaskID) == "" {
		return stored.Task, nil
	}
	if hasStored && isGatewayQueueInProgress(stored.Task) {
		return stored.Task, nil
	}
	if hasStored && isGatewayQueueTerminalFailure(stored.Task) {
		return stored.Task, nil
	}

	task, err := s.southbound.GetTask(ctx, southboundTaskID)
	if err != nil {
		if hasStored {
			return stored.Task, nil
		}
		return TaskView{}, err
	}
	view, err := s.enrichTask(ctx, task)
	if err != nil {
		return TaskView{}, err
	}
	if hasStored {
		view.ID = externalTaskID
		preserveStoredRuntimeMetadata(&view, stored.Task.Metadata)
	}
	return s.saveTaskView(ctx, view)
}

func isGatewayQueueInProgress(task TaskView) bool {
	if taskStatusIsTerminal(task.Status) {
		return false
	}
	if task.Metadata == nil {
		return false
	}
	status, _ := task.Metadata["queue_status"].(string)
	switch strings.TrimSpace(status) {
	case "admitting", "queued", "claimed", "processing", "retry":
		return true
	default:
		return false
	}
}

func isGatewayQueueTerminalFailure(task TaskView) bool {
	if task.Status != "failed" || task.FailedReasonCode != "gateway_task_blocked_after_retries" {
		return false
	}
	if task.Metadata == nil {
		return false
	}
	status, _ := task.Metadata["queue_status"].(string)
	return status == "failed"
}

func preserveStoredRuntimeMetadata(view *TaskView, stored map[string]any) {
	if view == nil || len(stored) == 0 {
		return
	}
	for _, key := range []string{
		"runtime_reply_text",
		taskMetadataRuntimeRawReplyText,
		"runtime_status",
		taskMetadataRuntimeResultWorkspaceFileIDs,
		taskMetadataRuntimeResultFileSyncError,
		taskMetadataRuntimeResultFileCount,
		taskMetadataRuntimeResultTaskID,
		taskMetadataRuntimeResultPathPrefix,
		taskMetadataRequiresResultFile,
		"queue_error_location",
		"queue_last_error",
		"queue_retry_reason_code",
		"queue_next_attempt_at",
	} {
		value, ok := stored[key]
		if !ok {
			continue
		}
		if view.Metadata == nil {
			view.Metadata = map[string]any{}
		}
		if _, exists := view.Metadata[key]; !exists {
			view.Metadata[key] = value
		}
	}
}

func normalizeResultWorkspaceFileIDs(primary string, fileIDs []string) []string {
	out := make([]string, 0, len(fileIDs)+1)
	seen := map[string]struct{}{}
	appendID := func(value string) {
		value = strings.TrimSpace(value)
		if value == "" {
			return
		}
		if _, exists := seen[value]; exists {
			return
		}
		seen[value] = struct{}{}
		out = append(out, value)
	}
	appendID(primary)
	for _, fileID := range fileIDs {
		appendID(fileID)
	}
	return out
}

func normalizeChatResultFileRequirement(request ChatRequest) ChatRequest {
	if !chatRequestRequiresResultFile(request) {
		return request
	}
	request.RequiresResultFile = true
	if request.Metadata == nil {
		request.Metadata = map[string]string{}
	}
	request.Metadata[taskMetadataRequiresResultFile] = "true"
	return request
}

func chatRequestRequiresResultFile(request ChatRequest) bool {
	if request.RequiresResultFile {
		return true
	}
	return metadataStringRequiresResultFile(request.Metadata)
}

func metadataStringRequiresResultFile(metadata map[string]string) bool {
	if len(metadata) == 0 {
		return false
	}
	for _, key := range resultFileRequirementMetadataKeys() {
		if truthyMetadataString(metadata[key]) {
			return true
		}
	}
	return false
}

func metadataAnyRequiresResultFile(metadata map[string]any) bool {
	if len(metadata) == 0 {
		return false
	}
	for _, key := range resultFileRequirementMetadataKeys() {
		value, ok := metadata[key]
		if !ok {
			continue
		}
		if truthyMetadataAny(value) {
			return true
		}
	}
	return false
}

func resultFileRequirementMetadataKeys() []string {
	return []string{
		taskMetadataRequiresResultFile,
		"require_result_file",
		"result_file_required",
	}
}

func truthyMetadataAny(value any) bool {
	switch typed := value.(type) {
	case bool:
		return typed
	case string:
		return truthyMetadataString(typed)
	case int:
		return typed != 0
	case int64:
		return typed != 0
	case float64:
		return typed != 0
	default:
		return false
	}
}

func truthyMetadataString(value string) bool {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "1", "true", "t", "yes", "y", "on", "required":
		return true
	default:
		return false
	}
}

func taskMetaJSONFromChatRequest(request ChatRequest) map[string]any {
	metadata := make(map[string]any, len(request.Metadata)+1)
	for key, value := range request.Metadata {
		key = strings.TrimSpace(key)
		value = strings.TrimSpace(value)
		if key == "" || value == "" {
			continue
		}
		metadata[key] = value
	}
	if chatRequestRequiresResultFile(request) {
		metadata[taskMetadataRequiresResultFile] = true
	}
	if len(metadata) == 0 {
		return nil
	}
	return metadata
}

func mergeSouthboundTaskMetaJSON(task *southbound.Task, metadata map[string]any) {
	if task == nil || len(metadata) == 0 {
		return
	}
	if task.MetaJSON == nil {
		task.MetaJSON = map[string]any{}
	}
	for key, value := range metadata {
		if strings.TrimSpace(key) == "" || value == nil {
			continue
		}
		if _, exists := task.MetaJSON[key]; !exists {
			task.MetaJSON[key] = value
		}
	}
}

func taskRequiresResultFile(task southbound.Task) bool {
	return metadataAnyRequiresResultFile(task.MetaJSON)
}

func applyRuntimeResultFileMetadata(view *TaskView, fileIDs []string, syncErr error) {
	if view == nil {
		return
	}
	fileIDs = normalizeResultWorkspaceFileIDs(view.ResultWorkspaceFileID, fileIDs)
	if len(fileIDs) == 0 && syncErr == nil {
		return
	}
	if view.Metadata == nil {
		view.Metadata = map[string]any{}
	}
	if view.ResultWorkspaceFileID == "" && len(fileIDs) > 0 {
		view.ResultWorkspaceFileID = fileIDs[0]
	}
	setRuntimeResultFileMetadata(view.Metadata, fileIDs, syncErr)
}

func setRuntimeResultFileMetadata(metadata map[string]any, fileIDs []string, syncErr error) {
	if metadata == nil {
		return
	}
	fileIDs = normalizeResultWorkspaceFileIDs("", fileIDs)
	metadata[taskMetadataRuntimeResultFileCount] = len(fileIDs)
	if len(fileIDs) > 0 {
		metadata[taskMetadataRuntimeResultWorkspaceFileIDs] = append([]string(nil), fileIDs...)
	} else {
		delete(metadata, taskMetadataRuntimeResultWorkspaceFileIDs)
	}
	if syncErr != nil && strings.TrimSpace(syncErr.Error()) != "" {
		metadata[taskMetadataRuntimeResultFileSyncError] = strings.TrimSpace(syncErr.Error())
	} else {
		delete(metadata, taskMetadataRuntimeResultFileSyncError)
	}
}

func (s *Service) CompleteTask(
	ctx context.Context,
	taskID string,
	request CompleteTaskRequest,
) (TaskView, error) {
	taskID = strings.TrimSpace(taskID)
	if taskID == "" {
		return TaskView{}, &ValidationError{Message: "task_id is required"}
	}

	assistantID := strings.TrimSpace(request.AssistantID)
	if assistantID == "" {
		return TaskView{}, &ValidationError{Message: "assistant_id is required"}
	}
	claimToken := strings.TrimSpace(request.ClaimToken)
	if claimToken == "" {
		return TaskView{}, &ValidationError{Message: "claim_token is required"}
	}
	resultWorkspaceFileIDs := normalizeResultWorkspaceFileIDs(request.ResultWorkspaceFileID, request.ResultWorkspaceFileIDs)
	resultWorkspaceFileID := ""
	if len(resultWorkspaceFileIDs) > 0 {
		resultWorkspaceFileID = resultWorkspaceFileIDs[0]
	}
	if strings.TrimSpace(request.ResultObjectID) == "" && resultWorkspaceFileID == "" {
		return TaskView{}, &ValidationError{Message: "result_object_id or result_workspace_file_id is required"}
	}

	externalTaskID, southboundTaskID, _, _, err := s.resolveTaskIDs(ctx, taskID)
	if err != nil {
		return TaskView{}, err
	}
	if southboundTaskID == "" {
		return TaskView{}, &ValidationError{Message: "task is queued and has not started"}
	}

	existing, err := s.southbound.GetTask(ctx, southboundTaskID)
	if err != nil {
		return TaskView{}, err
	}
	task, err := s.southbound.CompleteTask(ctx, southboundTaskID, southbound.CompleteTaskRequest{
		AssistantID:            assistantID,
		ClaimToken:             claimToken,
		ResultObjectID:         strings.TrimSpace(request.ResultObjectID),
		ResultWorkspaceFileID:  resultWorkspaceFileID,
		ResultWorkspaceFileIDs: resultWorkspaceFileIDs,
	})
	if err != nil {
		s.logWarn(
			"gateway_public_task_complete_failed",
			"task_id", externalTaskID,
			"southbound_task_id", southboundTaskID,
			"assistant_id", assistantID,
			"result_object_id", strings.TrimSpace(request.ResultObjectID),
			"result_workspace_file_id", resultWorkspaceFileID,
			"result_workspace_file_ids", resultWorkspaceFileIDs,
			"result_files_count", len(resultWorkspaceFileIDs),
			"error", err,
		)
		return TaskView{}, err
	}
	s.releaseTarget(ctx, existing.TargetAssistantID)
	view, err := s.enrichTask(ctx, task)
	if err != nil {
		return TaskView{}, err
	}
	applyRuntimeResultFileMetadata(&view, resultWorkspaceFileIDs, nil)
	view.ID = externalTaskID
	s.logInfo(
		"gateway_public_task_completed",
		"task_id", externalTaskID,
		"southbound_task_id", southboundTaskID,
		"assistant_id", assistantID,
		"status", view.Status,
		"result_object_id", strings.TrimSpace(request.ResultObjectID),
		"result_workspace_file_id", resultWorkspaceFileID,
		"result_workspace_file_ids", resultWorkspaceFileIDs,
		"result_files_count", len(resultWorkspaceFileIDs),
	)
	return s.saveTaskView(ctx, view)
}

func (s *Service) cancelQueuedTask(ctx context.Context, taskID string, actorID string, reason string) (TaskView, error) {
	if s.tasks == nil {
		return TaskView{}, &ValidationError{Message: "task is queued and has not started"}
	}
	record, ok, err := s.tasks.GetTask(ctx, taskID)
	if err != nil {
		return TaskView{}, err
	}
	if !ok {
		return TaskView{}, &ValidationError{Message: "task not found"}
	}
	now := time.Now().UTC()
	if reason == "" {
		reason = "cancelled by " + strings.TrimSpace(actorID)
	}
	record.Task.Status = "cancelled"
	record.Task.FailedAt = &now
	record.Task.FailedReasonCode = "cancelled"
	record.Task.FailedReasonText = reason
	if record.Task.Metadata == nil {
		record.Task.Metadata = map[string]any{}
	}
	record.Task.Metadata["queue_status"] = "cancelled"
	if s.queue != nil {
		if err := s.queue.CancelQueuedTask(ctx, taskID, reason); err != nil {
			return TaskView{}, err
		}
	}
	if err := s.tasks.UpsertTask(ctx, record); err != nil {
		return TaskView{}, err
	}
	s.releaseTarget(ctx, record.Task.TargetAssistantID)
	return record.Task, nil
}

func (s *Service) FailTask(
	ctx context.Context,
	taskID string,
	request FailTaskRequest,
) (TaskView, error) {
	taskID = strings.TrimSpace(taskID)
	if taskID == "" {
		return TaskView{}, &ValidationError{Message: "task_id is required"}
	}

	assistantID := strings.TrimSpace(request.AssistantID)
	if assistantID == "" {
		return TaskView{}, &ValidationError{Message: "assistant_id is required"}
	}
	claimToken := strings.TrimSpace(request.ClaimToken)
	if claimToken == "" {
		return TaskView{}, &ValidationError{Message: "claim_token is required"}
	}
	if strings.TrimSpace(request.FailedReasonCode) == "" {
		return TaskView{}, &ValidationError{Message: "failed_reason_code is required"}
	}

	externalTaskID, southboundTaskID, _, _, err := s.resolveTaskIDs(ctx, taskID)
	if err != nil {
		return TaskView{}, err
	}
	if southboundTaskID == "" {
		return TaskView{}, &ValidationError{Message: "task is queued and has not started"}
	}

	existing, err := s.southbound.GetTask(ctx, southboundTaskID)
	if err != nil {
		return TaskView{}, err
	}
	task, err := s.southbound.FailTask(ctx, southboundTaskID, southbound.FailTaskRequest{
		AssistantID:      assistantID,
		ClaimToken:       claimToken,
		FailedReasonCode: strings.TrimSpace(request.FailedReasonCode),
		FailedReasonText: strings.TrimSpace(request.FailedReasonText),
	})
	if err != nil {
		s.logWarn(
			"gateway_public_task_fail_failed",
			"task_id", externalTaskID,
			"southbound_task_id", southboundTaskID,
			"assistant_id", assistantID,
			"failed_reason_code", strings.TrimSpace(request.FailedReasonCode),
			"error", err,
		)
		return TaskView{}, err
	}
	s.releaseTarget(ctx, existing.TargetAssistantID)
	view, err := s.enrichTask(ctx, task)
	if err != nil {
		return TaskView{}, err
	}
	view.ID = externalTaskID
	s.logWarn(
		"gateway_public_task_failed",
		"task_id", externalTaskID,
		"southbound_task_id", southboundTaskID,
		"assistant_id", assistantID,
		"status", view.Status,
		"failed_reason_code", strings.TrimSpace(request.FailedReasonCode),
	)
	return s.saveTaskView(ctx, view)
}

func (s *Service) ResolveTaskApproval(
	ctx context.Context,
	taskID string,
	request ApprovalResolutionRequest,
) (TaskView, error) {
	externalTaskID, southboundTaskID, _, _, err := s.resolveTaskIDs(ctx, taskID)
	if err != nil {
		return TaskView{}, err
	}
	if southboundTaskID == "" {
		return TaskView{}, &ValidationError{Message: "task is queued and has not started"}
	}

	task, err := s.southbound.GetTask(ctx, southboundTaskID)
	if err != nil {
		return TaskView{}, err
	}
	if strings.TrimSpace(task.ApprovalID) == "" {
		return TaskView{}, &ValidationError{Message: "task has no approval to resolve"}
	}

	resolution := strings.ToLower(strings.TrimSpace(request.Resolution))
	if resolution != "allow" && resolution != "deny" {
		return TaskView{}, &ValidationError{Message: "resolution must be allow or deny"}
	}

	if _, err := s.southbound.ResolveApproval(ctx, task.ApprovalID, resolution); err != nil {
		return TaskView{}, err
	}

	resolved, err := s.GetTask(ctx, externalTaskID)
	if err != nil {
		return TaskView{}, err
	}
	if resolution == "allow" && resolved.Status == "pending_claim" {
		return s.StartTask(ctx, externalTaskID)
	}
	return s.saveTaskView(ctx, resolved)
}

func (s *Service) GetApproval(ctx context.Context, approvalID string) (ApprovalResponse, error) {
	approvalID = strings.TrimSpace(approvalID)
	if approvalID == "" {
		return ApprovalResponse{}, &ValidationError{Message: "approval_id is required"}
	}

	approval, err := s.southbound.GetApproval(ctx, approvalID)
	if err != nil {
		return ApprovalResponse{}, err
	}

	taskID := ""
	if s.tasks != nil {
		record, ok, err := s.tasks.FindByApprovalID(ctx, approvalID)
		if err != nil {
			return ApprovalResponse{}, err
		}
		if ok {
			taskID = record.Task.ID
		}
	}
	return NewApprovalResponse(approvalViewFromSouthbound(approval), taskID), nil
}

func (s *Service) ListApprovals(
	ctx context.Context,
	status string,
	assistantID string,
	approvalScope string,
) ([]ApprovalResponse, error) {
	approvals, err := s.southbound.ListApprovals(ctx, status, assistantID, approvalScope)
	if err != nil {
		return nil, err
	}
	response := make([]ApprovalResponse, 0, len(approvals))
	for _, approval := range approvals {
		response = append(response, NewApprovalResponse(approvalViewFromSouthbound(approval), ""))
	}
	return response, nil
}

func (s *Service) ResolveApprovalDecision(
	ctx context.Context,
	approvalID string,
	request ApprovalDecisionRequest,
) (ApprovalDecisionResponse, error) {
	approvalID = strings.TrimSpace(approvalID)
	if approvalID == "" {
		return ApprovalDecisionResponse{}, &ValidationError{Message: "approval_id is required"}
	}
	resolution, err := southboundApprovalResolution(request.Decision)
	if err != nil {
		return ApprovalDecisionResponse{}, err
	}

	if s.tasks != nil {
		record, ok, err := s.tasks.FindByApprovalID(ctx, approvalID)
		if err != nil {
			return ApprovalDecisionResponse{}, err
		}
		if ok {
			task, err := s.ResolveTaskApproval(ctx, record.Task.ID, ApprovalResolutionRequest{Resolution: resolution})
			if err != nil {
				return ApprovalDecisionResponse{}, err
			}
			approval := ApprovalResponse{
				ApprovalID: approvalID,
				TaskID:     task.ID,
				Status:     "resolved",
				Decision:   publicApprovalDecision(resolution),
			}
			if task.Approval != nil {
				approval = NewApprovalResponse(*task.Approval, task.ID)
			}
			responseTask := NewTaskResponse(task)
			return ApprovalDecisionResponse{Approval: approval, Task: &responseTask}, nil
		}
	}

	if _, err := s.southbound.ResolveApproval(ctx, approvalID, resolution); err != nil {
		return ApprovalDecisionResponse{}, err
	}
	approval, err := s.GetApproval(ctx, approvalID)
	if err != nil {
		return ApprovalDecisionResponse{}, err
	}
	return ApprovalDecisionResponse{Approval: approval}, nil
}

func (s *Service) CancelTask(
	ctx context.Context,
	taskID string,
	request CancelTaskRequest,
) (TaskView, error) {
	taskID = strings.TrimSpace(taskID)
	if taskID == "" {
		return TaskView{}, &ValidationError{Message: "task_id is required"}
	}

	actorID := strings.TrimSpace(request.ActorID)
	if actorID == "" {
		actorID = "go-gateway"
	}

	externalTaskID, southboundTaskID, stored, hasStored, err := s.resolveTaskIDs(ctx, taskID)
	if err != nil {
		return TaskView{}, err
	}
	if hasStored && taskStatusIsTerminal(stored.Task.Status) {
		return TaskView{}, &ConflictError{Message: "任务已结束，不能取消"}
	}
	if southboundTaskID == "" {
		return s.cancelQueuedTask(ctx, externalTaskID, actorID, strings.TrimSpace(request.Reason))
	}

	existing, err := s.southbound.GetTask(ctx, southboundTaskID)
	if err != nil {
		return TaskView{}, err
	}
	if taskStatusIsTerminal(existing.Status) {
		return TaskView{}, &ConflictError{Message: "任务已结束，不能取消"}
	}
	task, err := s.southbound.CancelTask(ctx, southboundTaskID, southbound.CancelTaskRequest{
		ActorID: actorID,
		Reason:  strings.TrimSpace(request.Reason),
	})
	if err != nil {
		if isSouthboundCancelConflict(err) {
			return TaskView{}, &ConflictError{Message: "任务已结束，不能取消"}
		}
		return TaskView{}, err
	}
	s.releaseTarget(ctx, existing.TargetAssistantID)
	view, err := s.enrichTask(ctx, task)
	if err != nil {
		return TaskView{}, err
	}
	view.ID = externalTaskID
	if s.queue != nil {
		reason := strings.TrimSpace(request.Reason)
		if reason == "" {
			reason = "cancelled by " + actorID
		}
		if err := s.queue.CancelQueuedTask(ctx, externalTaskID, reason); err != nil {
			return TaskView{}, err
		}
	}
	return s.saveTaskView(ctx, view)
}

func (s *Service) StartTask(ctx context.Context, taskID string) (TaskView, error) {
	externalTaskID, southboundTaskID, _, _, err := s.resolveTaskIDs(ctx, taskID)
	if err != nil {
		return TaskView{}, err
	}
	if southboundTaskID == "" {
		return TaskView{}, &ValidationError{Message: "task is queued and has not started"}
	}

	task, err := s.southbound.GetTask(ctx, southboundTaskID)
	if err != nil {
		return TaskView{}, err
	}

	switch task.Status {
	case "running":
		view, err := s.enrichTask(ctx, task)
		if err != nil {
			return TaskView{}, err
		}
		view.ID = externalTaskID
		return s.saveTaskView(ctx, view)
	case "pending_approval":
		return TaskView{}, &ValidationError{Message: "task is still pending approval"}
	case "pending_claim":
		started, err := s.claimStartAndExecuteTask(ctx, task, nil)
		if err != nil {
			return TaskView{}, err
		}
		view, err := s.enrichTask(ctx, started)
		if err != nil {
			return TaskView{}, err
		}
		view.ID = externalTaskID
		return s.saveTaskView(ctx, view)
	default:
		view, err := s.enrichTask(ctx, task)
		if err != nil {
			return TaskView{}, err
		}
		view.ID = externalTaskID
		return s.saveTaskView(ctx, view)
	}
}

func (s *Service) startCreatedTask(
	ctx context.Context,
	task southbound.Task,
	hooks *southboundExecutionHooks,
) (TaskView, error) {
	if task.Status != "pending_claim" {
		if hooks != nil && (task.Status == "claimed" || task.Status == "running") {
			if hooks.resumeRunning != nil {
				return hooks.resumeRunning(ctx, task)
			}
			return TaskView{}, retryableQueueError{
				Cause: fmt.Errorf("collaboration task %s is still %s", task.ID, task.Status),
			}
		}
		return s.enrichTask(ctx, task)
	}

	started, err := s.claimStartAndExecuteTask(ctx, task, hooks)
	if err != nil {
		return TaskView{}, err
	}
	return s.enrichTask(ctx, started)
}

func (s *Service) claimStartAndExecuteTask(
	ctx context.Context,
	task southbound.Task,
	hooks *southboundExecutionHooks,
) (southbound.Task, error) {
	gwTaskID := firstNonEmptyString(hooksGatewayTaskID(hooks), task.ID)
	s.logInfo(
		"gateway_collaboration_task_claim_start",
		"task_id", gwTaskID, "southbound_task_id", task.ID,
		"target_assistant_id", task.TargetAssistantID,
		"status", task.Status,
	)
	reservation, err := s.reserveTarget(ctx, task.TargetAssistantID)
	if err != nil {
		s.logWarn(
			"gateway_runtime_reserve_failed",
			"task_id", gwTaskID, "southbound_task_id", task.ID,
			"target_assistant_id", task.TargetAssistantID,
			"error", err,
		)
		return southbound.Task{}, err
	}
	claim, err := s.claimSouthboundTask(ctx, task)
	if err != nil {
		s.releaseRuntimeReservation(context.WithoutCancel(ctx), reservation)
		s.logWarn(
			"gateway_collaboration_task_claim_failed",
			"task_id", gwTaskID, "southbound_task_id", task.ID,
			"target_assistant_id", task.TargetAssistantID,
			"error", err,
		)
		return southbound.Task{}, err
	}
	s.logInfo(
		"gateway_collaboration_task_claimed",
		"task_id", gwTaskID, "southbound_task_id", task.ID,
		"target_assistant_id", task.TargetAssistantID,
		"claim_assistant_id", strings.TrimSpace(claim.Claim.AssistantID),
		"claim_lease_expires_at", claim.Claim.LeaseExpiresAt.Time.UTC().Format(time.RFC3339Nano),
	)
	var failAttempt func(error)
	if hooks != nil {
		failAttempt = hooks.failAttempt
	}
	stopClaimHeartbeat := s.startSouthboundClaimHeartbeat(ctx, task.ID, task.TargetAssistantID, claim.Claim, reservation, failAttempt)
	defer stopClaimHeartbeat()
	started, err := s.southbound.StartTask(ctx, task.ID, southbound.StartTaskRequest{
		AssistantID: task.TargetAssistantID,
		ClaimToken:  claim.Claim.ClaimToken,
	})
	if err != nil {
		s.releaseRuntimeReservation(context.WithoutCancel(ctx), reservation)
		s.logWarn(
			"gateway_collaboration_task_start_failed",
			"task_id", gwTaskID, "southbound_task_id", task.ID,
			"target_assistant_id", task.TargetAssistantID,
			"error", err,
		)
		return southbound.Task{}, err
	}
	s.logInfo(
		"gateway_collaboration_task_started",
		"task_id", gwTaskID, "southbound_task_id", task.ID,
		"target_assistant_id", task.TargetAssistantID,
		"status", started.Status,
	)
	if hooks != nil && hooks.afterStart != nil {
		hooks.afterStart(ctx, started)
	}
	if s.runtimeExecutor == nil {
		s.releaseRuntimeReservation(context.WithoutCancel(ctx), reservation)
		s.logInfo(
			"gateway_runtime_execution_skipped",
			"task_id", gwTaskID, "southbound_task_id", task.ID,
			"target_assistant_id", task.TargetAssistantID,
			"reason", "runtime_executor_disabled",
		)
		return started, nil
	}
	return s.executeRuntimeTask(ctx, started, claim.Claim.ClaimToken, reservation, hooks)
}

func (s *Service) claimSouthboundTask(ctx context.Context, task southbound.Task) (southbound.ClaimResponse, error) {
	claim, err := s.southbound.ClaimTask(ctx, task.ID, task.TargetAssistantID)
	if err == nil {
		return claim, nil
	}
	if !isRetryableTransportError(err) {
		return southbound.ClaimResponse{}, err
	}
	observed, getErr := s.southbound.GetTask(ctx, task.ID)
	if getErr != nil {
		s.logWarn(
			"gateway_collaboration_task_claim_recheck_failed",
			"task_id", strings.TrimSpace(task.ID),
			"southbound_task_id", task.ID,
			"target_assistant_id", task.TargetAssistantID,
			"claim_error", err,
			"error", getErr,
		)
		return southbound.ClaimResponse{}, err
	}
	status := strings.ToLower(strings.TrimSpace(observed.Status))
	if strings.TrimSpace(observed.TargetAssistantID) != strings.TrimSpace(task.TargetAssistantID) {
		return southbound.ClaimResponse{}, err
	}
	if status == "running" || (status == "claimed" && observed.StartedAt != nil) {
		return southbound.ClaimResponse{}, retryableQueueError{
			Cause: fmt.Errorf("collaboration task %s is already %s after claim timeout", task.ID, firstNonEmptyString(status, "running")),
		}
	}
	if status != "claimed" || observed.StartedAt != nil {
		return southbound.ClaimResponse{}, err
	}
	s.logInfo(
		"gateway_collaboration_task_claim_recovering",
		"task_id", strings.TrimSpace(task.ID),
		"southbound_task_id", task.ID,
		"target_assistant_id", task.TargetAssistantID,
		"southbound_status", observed.Status,
		"claim_error", err,
	)
	recovered, retryErr := s.southbound.ClaimTask(ctx, task.ID, task.TargetAssistantID)
	if retryErr != nil {
		s.logWarn(
			"gateway_collaboration_task_claim_recovery_failed",
			"task_id", strings.TrimSpace(task.ID),
			"southbound_task_id", task.ID,
			"target_assistant_id", task.TargetAssistantID,
			"southbound_status", observed.Status,
			"claim_error", err,
			"error", retryErr,
		)
		return southbound.ClaimResponse{}, fmt.Errorf("%w; reclaim after observed claimed task failed: %v", err, retryErr)
	}
	s.logInfo(
		"gateway_collaboration_task_claim_recovered",
		"task_id", strings.TrimSpace(task.ID),
		"southbound_task_id", task.ID,
		"target_assistant_id", task.TargetAssistantID,
		"claim_assistant_id", strings.TrimSpace(recovered.Claim.AssistantID),
		"claim_lease_expires_at", recovered.Claim.LeaseExpiresAt.Time.UTC().Format(time.RFC3339Nano),
	)
	return recovered, nil
}

func (s *Service) executeRuntimeTask(
	ctx context.Context,
	task southbound.Task,
	claimToken string,
	reservation RuntimeReservation,
	hooks *southboundExecutionHooks,
) (southbound.Task, error) {
	// Prefer the gateway task id (gw-xxx) for cross-layer log correlation; fall
	// back to the southbound id (task.ID) on the sync path where the gateway id
	// is not threaded through the hooks. task.ID is always logged separately as
	// southbound_task_id.
	gwTaskID := firstNonEmptyString(hooksGatewayTaskID(hooks), task.ID)
	assistant, err := s.southbound.GetAssistant(ctx, task.TargetAssistantID)
	if err != nil {
		s.logWarn(
			"gateway_runtime_assistant_lookup_failed",
			"task_id", gwTaskID, "southbound_task_id", task.ID,
			"target_assistant_id", task.TargetAssistantID,
			"retryable", retryQueueWorkerError(err),
			"error", err,
		)
		return s.failRuntimeTaskMaybeRetry(ctx, task, claimToken, reservation, "runtime_lookup_failed", err, retryQueueWorkerError(err))
	}
	gatewayURL := strings.TrimSpace(assistant.Runtime.GatewayURL)
	resolverConfigured := s.runtimeRouteResolver != nil
	route := reservation.Route
	routeFound := runtimeRouteHasGateway(route)
	var routeErr error
	if !routeFound {
		route, routeFound, routeErr = s.resolveRuntimeRoute(ctx, task.TargetAssistantID)
	}
	if routeErr != nil && resolverConfigured {
		s.logWarn(
			"gateway_runtime_route_lookup_failed",
			"task_id", gwTaskID, "southbound_task_id", task.ID,
			"target_assistant_id", task.TargetAssistantID,
			"reserved_runtime_id", reservation.RuntimeID,
			"runtime_lease_id", reservation.LeaseID,
			"error", routeErr,
		)
		return s.failRuntimeTaskMaybeRetry(
			ctx,
			task,
			claimToken,
			reservation,
			"runtime_route_lookup_failed",
			routeErr,
			true,
		)
	}
	if routeFound {
		routeGatewayURL := firstNonEmptyString(route.InternalBaseURL, route.GatewayURL)
		if routeGatewayURL != "" {
			if gatewayURL != "" && gatewayURL != strings.TrimSpace(routeGatewayURL) {
				s.logWarn(
					"runtime_route_mismatch",
					"task_id", gwTaskID, "southbound_task_id", task.ID,
					"assistant_id", task.TargetAssistantID,
					"assistant_gateway_url", gatewayURL,
					"reserved_runtime_id", reservation.RuntimeID,
					"runtime_lease_id", reservation.LeaseID,
					"registry_internal_base_url", route.InternalBaseURL,
					"registry_gateway_url", route.GatewayURL,
					"source", "registry_preferred",
				)
			}
			gatewayURL = strings.TrimSpace(routeGatewayURL)
		}
	}
	if gatewayURL == "" {
		s.logWarn(
			"gateway_runtime_gateway_unavailable",
			"task_id", gwTaskID, "southbound_task_id", task.ID,
			"target_assistant_id", task.TargetAssistantID,
			"resolver_configured", resolverConfigured,
			"route_found", routeFound,
		)
		return s.failRuntimeTaskMaybeRetry(
			ctx,
			task,
			claimToken,
			reservation,
			"runtime_gateway_unavailable",
			fmt.Errorf("assistant %s has no runtime gateway_url", task.TargetAssistantID),
			resolverConfigured,
		)
	}

	timeoutMs := task.TimeoutSeconds * 1000
	if timeoutMs <= 0 {
		timeoutMs = defaultTaskTimeoutSeconds * 1000
	}
	runtimeStartedAt := time.Now().UTC()
	requiresResultFile := taskRequiresResultFile(task)
	staleTimeoutMs := int(hooksStaleTimeout(hooks) / time.Millisecond)
	s.logInfo(
		"gateway_runtime_execute_start",
		"task_id", gwTaskID, "southbound_task_id", task.ID,
		"target_assistant_id", task.TargetAssistantID,
		"runtime_id", firstNonEmptyString(reservation.RuntimeID, route.RuntimeID),
		"runtime_lease_id", reservation.LeaseID,
		"gateway_url", gatewayURL,
		"route_found", routeFound,
		"timeout_ms", timeoutMs,
		"stale_timeout_ms", staleTimeoutMs,
		"result_path_prefix", runtimeResultPathPrefix(task.ID),
		"requires_result_file", requiresResultFile,
	)
	result, prefetchedFiles, err := s.executeRuntimeChatWithResultFileRecovery(ctx, task, runtimeStartedAt, RuntimeExecutionRequest{
		AssistantID:    task.TargetAssistantID,
		GatewayURL:     gatewayURL,
		GatewayToken:   strings.TrimSpace(assistant.Runtime.GatewayToken),
		SessionKey:     runtimeTaskSessionKey(task),
		Message:        withRuntimeResultFileGuidance(task.Instruction, task.ID),
		RequestID:      task.ID,
		GatewayTaskID:  hooksGatewayTaskID(hooks),
		TimeoutMs:      timeoutMs,
		DeadlineSource: hooksDeadlineSource(hooks),
		StaleTimeoutMs: staleTimeoutMs,
		OnProgress: func(progressCtx context.Context, event RuntimeProgressEvent) error {
			if hooks != nil && hooks.runtimeProgress != nil {
				hooks.runtimeProgress(progressCtx, event)
			}
			return nil
		},
	}, requiresResultFile)
	if err != nil {
		if stale := queuedAttemptStaleCause(ctx); stale != nil {
			s.logWarn(
				"gateway_runtime_execute_stale",
				"task_id", gwTaskID, "southbound_task_id", task.ID,
				"target_assistant_id", task.TargetAssistantID,
				"user_id", hooksUserID(hooks),
				"employee_no", hooksEmployeeNo(hooks),
				"stale_timeout_ms", staleTimeoutMs,
				"error", stale,
			)
			_, _ = s.failRuntimeTask(context.WithoutCancel(ctx), task, claimToken, reservation, "gateway_task_stale_progress", stale)
			return southbound.Task{}, stale
		}
		if isRuntimeStaleTimeout(err) {
			s.logWarn(
				"gateway_runtime_execute_stale",
				"task_id", gwTaskID, "southbound_task_id", task.ID,
				"target_assistant_id", task.TargetAssistantID,
				"user_id", hooksUserID(hooks),
				"employee_no", hooksEmployeeNo(hooks),
				"stale_timeout_ms", staleTimeoutMs,
				"error", err,
			)
			_, _ = s.failRuntimeTask(context.WithoutCancel(ctx), task, claimToken, reservation, "gateway_task_stale_progress", err)
			return southbound.Task{}, err
		}
		failedAttr := queueWorkerFaultDomain(err)
		s.logWarn(
			"gateway_runtime_execute_failed",
			"task_id", gwTaskID, "southbound_task_id", task.ID,
			"target_assistant_id", task.TargetAssistantID,
			"user_id", hooksUserID(hooks),
			"employee_no", hooksEmployeeNo(hooks),
			"fault_domain", string(failedAttr.Domain),
			"reason_code", failedAttr.Reason,
			"fault_owner", string(failedAttr.Owner),
			"attribution_confidence", string(failedAttr.Confidence),
			"retryable", retryQueuedTaskError(err),
			"duration_ms", int(time.Since(runtimeStartedAt)/time.Millisecond),
			"error", err,
		)
		return s.failRuntimeTaskMaybeRetry(ctx, task, claimToken, reservation, "runtime_execution_failed", err, retryQueuedTaskError(err))
	}
	if strings.TrimSpace(result.ReplyText) == "" {
		s.logWarn(
			"gateway_runtime_empty_reply",
			"task_id", gwTaskID, "southbound_task_id", task.ID,
			"target_assistant_id", task.TargetAssistantID,
			"user_id", hooksUserID(hooks),
			"employee_no", hooksEmployeeNo(hooks),
			"fault_domain", string(FaultDomainRuntime),
			"reason_code", ReasonRuntimeEmptyReply,
			"fault_owner", string(FaultOwnerGateway),
			"attribution_confidence", string(ConfidenceHigh),
			"runtime_status", strings.TrimSpace(result.Status),
			"duration_ms", int(time.Since(runtimeStartedAt)/time.Millisecond),
		)
		return s.failRuntimeTask(
			ctx,
			task,
			claimToken,
			reservation,
			"runtime_empty_reply",
			errors.New("runtime completed without reply text"),
		)
	}
	if runtimeResultIndicatesFailure(result) {
		s.logWarn(
			"gateway_runtime_result_indicates_failure",
			"task_id", gwTaskID, "southbound_task_id", task.ID,
			"target_assistant_id", task.TargetAssistantID,
			"user_id", hooksUserID(hooks),
			"employee_no", hooksEmployeeNo(hooks),
			"fault_domain", string(FaultDomainRuntime),
			"reason_code", ReasonRuntimeDeclaredFailed,
			"fault_owner", string(FaultOwnerRuntime),
			"attribution_confidence", string(ConfidenceHigh),
			"runtime_status", strings.TrimSpace(result.Status),
			"reply_text_bytes", len([]byte(strings.TrimSpace(result.ReplyText))),
			"duration_ms", int(time.Since(runtimeStartedAt)/time.Millisecond),
		)
		return s.failRuntimeTask(
			ctx,
			task,
			claimToken,
			reservation,
			"runtime_execution_failed",
			errors.New(strings.TrimSpace(result.ReplyText)),
		)
	}

	// Stamp NONE only after the empty-reply and declared-failure checks, so a
	// runtime-content fault is not mis-logged as a clean success (review #5).
	s.logInfo(
		"gateway_runtime_execute_completed",
		"task_id", gwTaskID, "southbound_task_id", task.ID,
		"target_assistant_id", task.TargetAssistantID,
		"fault_domain", string(FaultDomainNone),
		"reason_code", ReasonOK,
		"fault_owner", string(FaultOwnerGateway),
		"attribution_confidence", string(ConfidenceHigh),
		"runtime_status", strings.TrimSpace(result.Status),
		"reply_text_bytes", len([]byte(strings.TrimSpace(result.ReplyText))),
		"reply_mentions_workspace_paths", len(runtimeReplyWorkspacePaths(result.ReplyText)) > 0,
		"duration_ms", int(time.Since(runtimeStartedAt)/time.Millisecond),
	)

	resultObjectID, err := s.createRuntimeReplyObject(ctx, task, result.ReplyText)
	if err != nil {
		s.logWarn(
			"gateway_runtime_reply_object_create_failed",
			"task_id", gwTaskID, "southbound_task_id", task.ID,
			"target_assistant_id", task.TargetAssistantID,
			"retryable", retryQueueWorkerError(err),
			"error", err,
		)
		return s.failRuntimeTaskMaybeRetry(ctx, task, claimToken, reservation, "runtime_result_object_failed", err, retryQueuedTaskError(err))
	}
	s.logInfo(
		"gateway_runtime_reply_object_created",
		"task_id", gwTaskID, "southbound_task_id", task.ID,
		"target_assistant_id", task.TargetAssistantID,
		"result_object_id", resultObjectID,
	)
	var resultWorkspaceFileIDs []string
	var resultFileSyncErr error
	if prefetchedFiles.found {
		resultWorkspaceFileIDs = append([]string(nil), prefetchedFiles.fileIDs...)
		resultFileSyncErr = prefetchedFiles.err
	} else {
		resultWorkspaceFileIDs, resultFileSyncErr = s.syncRuntimeResultFiles(
			ctx,
			task.TargetAssistantID,
			task.CreatedBy,
			runtimeStartedAt,
			task.ID,
			result.ReplyText,
			requiresResultFile,
		)
	}
	resultWorkspaceFileID := ""
	if len(resultWorkspaceFileIDs) > 0 {
		resultWorkspaceFileID = resultWorkspaceFileIDs[0]
	}
	if resultFileSyncErr != nil {
		s.logWarn(
			"gateway_runtime_result_files_sync_finished_with_error",
			"task_id", gwTaskID, "southbound_task_id", task.ID,
			"target_assistant_id", task.TargetAssistantID,
			"user_id", task.CreatedBy,
			"result_path_prefix", runtimeResultPathPrefix(task.ID),
			"result_workspace_file_id", resultWorkspaceFileID,
			"result_workspace_file_ids", resultWorkspaceFileIDs,
			"result_files_count", len(resultWorkspaceFileIDs),
			"error", resultFileSyncErr,
		)
	} else {
		s.logInfo(
			"gateway_runtime_result_files_synced",
			"task_id", gwTaskID, "southbound_task_id", task.ID,
			"target_assistant_id", task.TargetAssistantID,
			"user_id", task.CreatedBy,
			"result_path_prefix", runtimeResultPathPrefix(task.ID),
			"result_workspace_file_id", resultWorkspaceFileID,
			"result_workspace_file_ids", resultWorkspaceFileIDs,
			"result_files_count", len(resultWorkspaceFileIDs),
		)
	}
	if requiresResultFile {
		if resultFileSyncErr != nil {
			s.logWarn(
				"gateway_runtime_required_result_file_sync_failed",
				"task_id", gwTaskID, "southbound_task_id", task.ID,
				"target_assistant_id", task.TargetAssistantID,
				"user_id", task.CreatedBy,
				"result_path_prefix", runtimeResultPathPrefix(task.ID),
				"error", resultFileSyncErr,
			)
			return s.failRuntimeTask(
				ctx,
				task,
				claimToken,
				reservation,
				"result_file_sync_failed",
				fmt.Errorf("任务要求生成结果文件，但结果文件同步失败: %w", resultFileSyncErr),
			)
		}
		if len(resultWorkspaceFileIDs) == 0 {
			s.logWarn(
				"gateway_runtime_required_result_file_missing",
				"task_id", gwTaskID, "southbound_task_id", task.ID,
				"target_assistant_id", task.TargetAssistantID,
				"user_id", task.CreatedBy,
				"result_path_prefix", runtimeResultPathPrefix(task.ID),
			)
			return s.failRuntimeTask(
				ctx,
				task,
				claimToken,
				reservation,
				"result_file_missing",
				errors.New("任务要求生成结果文件，但未发现可下载的结果文件"),
			)
		}
	}
	completed, err := s.southbound.CompleteTask(ctx, task.ID, southbound.CompleteTaskRequest{
		AssistantID:            task.TargetAssistantID,
		ClaimToken:             claimToken,
		ResultObjectID:         resultObjectID,
		ResultWorkspaceFileID:  resultWorkspaceFileID,
		ResultWorkspaceFileIDs: resultWorkspaceFileIDs,
	})
	if err != nil {
		s.releaseRuntimeReservation(ctx, reservation)
		s.logWarn(
			"gateway_collaboration_task_complete_failed",
			"task_id", gwTaskID, "southbound_task_id", task.ID,
			"target_assistant_id", task.TargetAssistantID,
			"result_object_id", resultObjectID,
			"result_workspace_file_id", resultWorkspaceFileID,
			"result_workspace_file_ids", resultWorkspaceFileIDs,
			"result_files_count", len(resultWorkspaceFileIDs),
			"error", err,
		)
		return southbound.Task{}, err
	}
	s.releaseRuntimeReservation(ctx, reservation)
	s.logInfo(
		"gateway_collaboration_task_completed",
		"task_id", gwTaskID, "southbound_task_id", task.ID,
		"target_assistant_id", task.TargetAssistantID,
		"status", completed.Status,
		"result_object_id", resultObjectID,
		"result_workspace_file_id", resultWorkspaceFileID,
		"result_workspace_file_ids", resultWorkspaceFileIDs,
		"result_files_count", len(resultWorkspaceFileIDs),
		"result_file_sync_error", errorString(resultFileSyncErr),
	)
	if completed.MetaJSON == nil {
		completed.MetaJSON = map[string]any{}
	}
	reply := publicRuntimeResultReplyText(result.ReplyText, resultWorkspaceFileIDs, requiresResultFile)
	rawReply := strings.TrimSpace(result.ReplyText)
	if reply != "" {
		completed.MetaJSON["runtime_reply_text"] = reply
	}
	if rawReply != "" && rawReply != reply {
		completed.MetaJSON[taskMetadataRuntimeRawReplyText] = rawReply
	}
	if status := strings.TrimSpace(result.Status); status != "" {
		completed.MetaJSON["runtime_status"] = status
	}
	if taskID := strings.TrimSpace(task.ID); taskID != "" {
		completed.MetaJSON[taskMetadataRuntimeResultTaskID] = taskID
	}
	if requiresResultFile {
		completed.MetaJSON[taskMetadataRequiresResultFile] = true
	}
	if resultPathPrefix := runtimeResultPathPrefix(task.ID); resultPathPrefix != "" {
		completed.MetaJSON[taskMetadataRuntimeResultPathPrefix] = resultPathPrefix
	}
	setRuntimeResultFileMetadata(completed.MetaJSON, resultWorkspaceFileIDs, resultFileSyncErr)
	return completed, nil
}

type runtimeExecutionOutcome struct {
	result RuntimeExecutionResult
	err    error
}

type runtimeResultFilePrefetch struct {
	fileIDs []string
	err     error
	found   bool
}

func (s *Service) executeRuntimeChatWithResultFileRecovery(
	ctx context.Context,
	task southbound.Task,
	runtimeStartedAt time.Time,
	request RuntimeExecutionRequest,
	requiresResultFile bool,
) (RuntimeExecutionResult, runtimeResultFilePrefetch, error) {
	gwTaskID := firstNonEmptyString(request.GatewayTaskID, task.ID)
	if !requiresResultFile {
		result, err := s.runtimeExecutor.ExecuteChat(ctx, request)
		return result, runtimeResultFilePrefetch{}, err
	}
	runtimeCtx, cancelRuntime := context.WithCancel(ctx)
	defer cancelRuntime()
	watchCtx, stopWatch := context.WithCancel(ctx)
	defer stopWatch()
	resultCh := make(chan runtimeExecutionOutcome, 1)
	go func() {
		result, err := s.runtimeExecutor.ExecuteChat(runtimeCtx, request)
		resultCh <- runtimeExecutionOutcome{result: result, err: err}
	}()
	recoveryCh := s.watchRequiredRuntimeResultFiles(watchCtx, task, runtimeStartedAt)
	handlePrefetch := func(prefetch runtimeResultFilePrefetch) (RuntimeExecutionResult, runtimeResultFilePrefetch, error) {
		if !prefetch.found || prefetch.err != nil {
			if prefetch.err != nil {
				return RuntimeExecutionResult{}, runtimeResultFilePrefetch{}, prefetch.err
			}
			return RuntimeExecutionResult{}, runtimeResultFilePrefetch{}, ctx.Err()
		}
		cancelRuntime()
		s.logInfo(
			"gateway_runtime_result_file_recovered_before_final",
			"task_id", gwTaskID, "southbound_task_id", task.ID,
			"target_assistant_id", task.TargetAssistantID,
			"user_id", task.CreatedBy,
			"result_path_prefix", runtimeResultPathPrefix(task.ID),
			"result_workspace_file_ids", prefetch.fileIDs,
			"result_files_count", len(prefetch.fileIDs),
		)
		return RuntimeExecutionResult{
			ReplyText: "TASK_RESULT_READY",
			Status:    "ok",
		}, prefetch, nil
	}
	select {
	case outcome := <-resultCh:
		// An in_flight error (a prior run under the same idempotencyKey is still
		// executing and is unreachable on this reconnected connection) is returned
		// immediately as a retryable error — fail fast, do NOT block on result-file
		// recovery. Blocking here could pin the worker until the stale timeout, or
		// forever when stale-progress detection is disabled (StaleProgressTimeout=0:
		// the await ctx has no deadline, so worker+watcher goroutine+lease leak
		// permanently), and the stale cause would overwrite the in_flight
		// attribution. The required result file is task-scoped and re-discovered by
		// the next attempt's watcher, so failing fast loses nothing. This matches
		// the runtime-in-flight requirement doc's "fast return, don't block".
		return outcome.result, runtimeResultFilePrefetch{}, outcome.err
	case prefetch := <-recoveryCh:
		return handlePrefetch(prefetch)
	case <-ctx.Done():
		return RuntimeExecutionResult{}, runtimeResultFilePrefetch{}, ctx.Err()
	}
}

func (s *Service) watchRequiredRuntimeResultFiles(
	ctx context.Context,
	task southbound.Task,
	runtimeStartedAt time.Time,
) <-chan runtimeResultFilePrefetch {
	ch := make(chan runtimeResultFilePrefetch, 1)
	go func() {
		defer close(ch)
		timer := time.NewTimer(runtimeResultFileRecoveryInitialDelay)
		select {
		case <-ctx.Done():
			if !timer.Stop() {
				select {
				case <-timer.C:
				default:
				}
			}
			return
		case <-timer.C:
		}
		for {
			select {
			case <-ctx.Done():
				return
			default:
			}
			fileIDs, err := s.syncRuntimeResultFiles(
				ctx,
				task.TargetAssistantID,
				task.CreatedBy,
				runtimeStartedAt,
				task.ID,
				"",
				true,
			)
			if err == nil && len(fileIDs) > 0 {
				select {
				case ch <- runtimeResultFilePrefetch{fileIDs: fileIDs, found: true}:
				case <-ctx.Done():
				}
				return
			}
			if err != nil && !errors.Is(err, context.Canceled) {
				s.logWarn(
					"gateway_runtime_result_file_recovery_sync_failed",
					"southbound_task_id", task.ID,
					"target_assistant_id", task.TargetAssistantID,
					"user_id", task.CreatedBy,
					"result_path_prefix", runtimeResultPathPrefix(task.ID),
					"result_workspace_file_ids", fileIDs,
					"result_files_count", len(fileIDs),
					"error", err,
				)
			}
			timer := time.NewTimer(runtimeResultFileSyncRetryInterval)
			select {
			case <-ctx.Done():
				if !timer.Stop() {
					select {
					case <-timer.C:
					default:
					}
				}
				return
			case <-timer.C:
			}
		}
	}()
	return ch
}

func publicRuntimeResultReplyText(replyText string, resultWorkspaceFileIDs []string, requiresResultFile bool) string {
	if requiresResultFile && len(resultWorkspaceFileIDs) > 0 {
		return "TASK_RESULT_READY"
	}
	return strings.TrimSpace(replyText)
}

func runtimeTaskSessionKey(task southbound.Task) string {
	assistantID := strings.TrimSpace(task.TargetAssistantID)
	taskID := sanitizeExternalAssistantID(task.ID, 64)
	if taskID == "northbound" {
		taskID = "task"
	}
	if assistantID == "" {
		return "run-" + taskID
	}
	return "agent:" + assistantID + ":run-" + taskID
}

func withRuntimeResultFileGuidance(instruction string, taskID string) string {
	trimmed := strings.TrimSpace(instruction)
	if trimmed == "" || runtimeResultGuidanceAlreadyPresent(trimmed, taskID) {
		return trimmed
	}
	tempDir := runtimeResultWorkspaceDir(taskID, "temp")
	persistentDir := runtimeResultWorkspaceDir(taskID, "persistent")
	return trimmed + "\n\n[Runtime result file handling]\n" +
		"如果这个任务要求创建、生成、写入、保存、导出文件，或交付可下载产物，必须真实创建工作区文件。" +
		" 默认写入 " + tempDir + "；只有用户明确要求长期保存时才写入 " + persistentDir + "。" +
		" 不要把本次任务的结果文件写入其他任务目录。" +
		" 不要只在回复文本里描述“文件名”和“内容”来代替写文件。" +
		" 写入完成后，只回复简短完成摘要；不要在回复正文手写结果路径、run id 或 file id，终态 files[] 才是权威结果引用。" +
		" If no file output is requested, answer normally."
}

func runtimeResultGuidanceAlreadyPresent(instruction string, taskID string) bool {
	if !strings.Contains(instruction, "[Runtime result file handling]") {
		return false
	}
	prefix := runtimeResultPathPrefix(taskID)
	if prefix == "" {
		return strings.Contains(instruction, "/workspace/temp") ||
			strings.Contains(instruction, "/workspace/persistent")
	}
	return strings.Contains(instruction, prefix) ||
		strings.Contains(instruction, runtimeResultWorkspaceDir(taskID, "temp")) ||
		strings.Contains(instruction, runtimeResultWorkspaceDir(taskID, "persistent"))
}

func runtimeResultPathPrefix(taskID string) string {
	normalized := sanitizeExternalAssistantID(taskID, 64)
	if normalized == "" || normalized == "northbound" {
		return ""
	}
	return "go-gateway-results/" + normalized + "/"
}

func runtimeResultWorkspaceDir(taskID string, bucket string) string {
	bucket = strings.Trim(strings.ReplaceAll(bucket, "\\", "/"), "/")
	if bucket == "" {
		bucket = "temp"
	}
	prefix := strings.TrimSuffix(runtimeResultPathPrefix(taskID), "/")
	if prefix == "" {
		return "/workspace/" + bucket
	}
	return "/workspace/" + bucket + "/" + prefix
}

func runtimeResultIndicatesFailure(result RuntimeExecutionResult) bool {
	status := strings.ToLower(strings.TrimSpace(result.Status))
	if status == "failed" || status == "error" {
		return true
	}
	reply := strings.TrimSpace(result.ReplyText)
	return strings.HasPrefix(reply, "⚠️ Agent failed before reply:") ||
		strings.HasPrefix(reply, "Agent failed before reply:")
}

func taskStatusIsTerminal(status string) bool {
	switch strings.ToLower(strings.TrimSpace(status)) {
	case "completed", "failed", "cancelled", "rejected", "expired", "timeout":
		return true
	default:
		return false
	}
}

func hooksStaleTimeout(hooks *southboundExecutionHooks) time.Duration {
	if hooks == nil || hooks.staleTimeout <= 0 {
		return 0
	}
	return hooks.staleTimeout
}

func deadlineSourceForTimeoutSeconds(requestTimeoutSeconds int) string {
	if requestTimeoutSeconds > 0 {
		return DeadlineSourceClientSupplied
	}
	return DeadlineSourceGatewayDefault
}

func hooksDeadlineSource(hooks *southboundExecutionHooks) string {
	if hooks == nil || hooks.deadlineSource == "" {
		// Unknown source: fail-closed on attribution — do NOT blame the caller
		// (UPSTREAM) for a deadline we cannot prove they set; treat it as a
		// gateway-default deadline (GATEWAY). The hard cap is still preserved
		// because such paths leave StaleTimeoutMs==0, which disables the A1
		// widening (#7 guard), so the original timeout is enforced.
		return DeadlineSourceGatewayDefault
	}
	return hooks.deadlineSource
}

// hooksGatewayTaskID returns the gateway-facing task id (gw-xxx) threaded
// through the execution hooks, used so runtime/WS-layer logs correlate on the
// gateway task id rather than the southbound id. Empty when unavailable (e.g.
// the sync path), in which case the WS layer falls back to the southbound id.
func hooksGatewayTaskID(hooks *southboundExecutionHooks) string {
	if hooks == nil {
		return ""
	}
	return hooks.gatewayTaskID
}

// hooksUserID returns the publisher/originator user id threaded through the
// execution hooks, used so runtime/execute-series failure logs surface WHO
// submitted the task. Empty when unavailable.
func hooksUserID(hooks *southboundExecutionHooks) string {
	if hooks == nil {
		return ""
	}
	return hooks.userID
}

// hooksEmployeeNo returns the publisher/originator employee number threaded
// through the execution hooks. Empty when unavailable.
func hooksEmployeeNo(hooks *southboundExecutionHooks) string {
	if hooks == nil {
		return ""
	}
	return hooks.employeeNo
}

func isSouthboundCancelConflict(err error) bool {
	var httpErr *southbound.HTTPError
	if !errors.As(err, &httpErr) || httpErr.StatusCode != http.StatusBadRequest {
		return false
	}
	body := strings.ToLower(httpErr.Body)
	return strings.Contains(body, "can no longer be cancelled") ||
		strings.Contains(body, "cannot be cancelled")
}

func (s *Service) createRuntimeReplyObject(
	ctx context.Context,
	task southbound.Task,
	replyText string,
) (string, error) {
	content := strings.TrimSpace(replyText)
	if content == "" {
		content = "completed"
	}
	sum := sha256.Sum256([]byte(content))
	contentHash := hex.EncodeToString(sum[:])
	meta := map[string]any{"source": "runtime_reply"}
	setSharedObjectMetaString(meta, "collaboration_task_id", task.ID)
	setSharedObjectMetaString(meta, "target_assistant_id", task.TargetAssistantID)
	setSharedObjectMetaString(meta, "result_sha256", contentHash)
	obj, err := s.southbound.CreateSharedObject(ctx, southbound.SharedObjectCreateRequest{
		SourceAssistantID: task.TargetAssistantID,
		Filename:          "chat-reply.txt",
		ObjectType:        "document",
		ContentType:       "text/plain",
		SizeBytes:         int64(len([]byte(content))),
		SHA256:            contentHash,
		CreatedBy:         "go-gateway",
		IdempotencyKey:    runtimeReplyObjectIdempotencyKey(task.ID, contentHash),
		MetaJSON:          meta,
	})
	if err != nil {
		return "", err
	}
	return obj.ID, nil
}

type runtimeResultFile struct {
	fileID                  string
	userID                  string
	bucket                  string
	relativePath            string
	filename                string
	sourceRef               string
	updatedAt               time.Time
	createdAt               time.Time
	workspaceLastModifiedAt time.Time
}

type runtimeResultFileSelector struct {
	pathPrefix string
	replyPaths map[string]struct{}
}

func (s *Service) syncLatestRuntimeResultFile(
	ctx context.Context,
	assistantID string,
	userID string,
	startedAt time.Time,
) (string, error) {
	fileIDs, err := s.syncRuntimeResultFiles(ctx, assistantID, userID, startedAt, "", "", false)
	if len(fileIDs) == 0 {
		return "", err
	}
	return fileIDs[0], err
}

func (s *Service) syncRuntimeResultFiles(
	ctx context.Context,
	assistantID string,
	userID string,
	startedAt time.Time,
	taskID string,
	replyText string,
	requireResultFile bool,
) ([]string, error) {
	assistantID = strings.TrimSpace(assistantID)
	userID = strings.TrimSpace(userID)
	if assistantID == "" || userID == "" {
		return nil, nil
	}
	selector := newRuntimeResultFileSelector(taskID, replyText)
	attempts := 1
	if requireResultFile || selector.shouldRetryMissingFiles() {
		attempts = runtimeResultFileSyncMaxAttempts
	}
	var lastErr error
	for attempt := 0; attempt < attempts; attempt++ {
		s.logInfo(
			"gateway_runtime_result_files_sync_attempt_start",
			"task_id", taskID,
			"assistant_id", assistantID,
			"user_id", userID,
			"attempt", attempt+1,
			"max_attempts", attempts,
			"task_scoped", selector.taskScoped(),
			"result_path_prefix", selector.pathPrefix,
			"reply_paths_count", len(selector.replyPaths),
			"changed_after", logTimeValue(startedAt),
		)
		fileIDs, err := s.syncRuntimeResultFilesOnce(ctx, assistantID, userID, startedAt, taskID, selector)
		lastErr = err
		if len(fileIDs) > 0 || attempt == attempts-1 {
			s.logInfo(
				"gateway_runtime_result_files_sync_attempt_finished",
				"task_id", taskID,
				"assistant_id", assistantID,
				"user_id", userID,
				"attempt", attempt+1,
				"max_attempts", attempts,
				"result_files_count", len(fileIDs),
				"result_workspace_file_ids", fileIDs,
				"error", errorString(err),
			)
			return fileIDs, lastErr
		}
		s.logInfo(
			"gateway_runtime_result_files_sync_retry_wait",
			"task_id", taskID,
			"assistant_id", assistantID,
			"user_id", userID,
			"attempt", attempt+1,
			"max_attempts", attempts,
			"retry_delay_ms", int(runtimeResultFileSyncRetryInterval/time.Millisecond),
		)
		timer := time.NewTimer(runtimeResultFileSyncRetryInterval)
		select {
		case <-ctx.Done():
			if !timer.Stop() {
				select {
				case <-timer.C:
				default:
				}
			}
			return nil, ctx.Err()
		case <-timer.C:
		}
	}
	return nil, lastErr
}

func logTimeValue(value time.Time) string {
	if value.IsZero() {
		return ""
	}
	return value.UTC().Format(time.RFC3339Nano)
}

func (s *Service) syncRuntimeResultFilesOnce(
	ctx context.Context,
	assistantID string,
	userID string,
	startedAt time.Time,
	taskID string,
	selector runtimeResultFileSelector,
) ([]string, error) {
	var syncErr error
	changedAfter := startedAt
	if selector.taskScoped() {
		changedAfter = time.Time{}
	}
	syncErrors := 0
	for _, bucket := range []string{"temp", "persistent"} {
		if !selector.taskScoped() {
			s.logInfo(
				"gateway_runtime_result_files_sync_bucket_skipped",
				"task_id", taskID,
				"assistant_id", assistantID,
				"user_id", userID,
				"bucket", bucket,
				"reason", "missing_task_scope",
				"changed_after", logTimeValue(changedAfter),
			)
			continue
		}
		summary, err := s.southbound.SyncResults(ctx, southbound.SyncResultsRequest{
			AssistantID:      assistantID,
			Bucket:           bucket,
			UserID:           userID,
			ChangedAfter:     changedAfter,
			ResultPathPrefix: selector.pathPrefix,
			TaskID:           taskID,
			SourceRef:        taskID,
		})
		if err == nil {
			err = workspaceScanSummaryError(summary)
		}
		if err != nil {
			if syncErr == nil {
				syncErr = err
			}
			syncErrors++
			s.logWarn(
				"gateway_runtime_result_files_sync_bucket_failed",
				"task_id", taskID,
				"assistant_id", assistantID,
				"user_id", userID,
				"bucket", bucket,
				"result_path_prefix", selector.pathPrefix,
				"source_ref", taskID,
				"changed_after", logTimeValue(changedAfter),
				"summary_errors_count", len(summary.Errors),
				"error", err,
			)
		} else {
			s.logInfo(
				"gateway_runtime_result_files_sync_bucket_finished",
				"task_id", taskID,
				"assistant_id", assistantID,
				"user_id", userID,
				"bucket", bucket,
				"result_path_prefix", selector.pathPrefix,
				"source_ref", taskID,
				"changed_after", logTimeValue(changedAfter),
				"assistants_scanned", summary.AssistantsScanned,
				"files_scanned", summary.FilesScanned,
				"files_synced", summary.FilesSynced,
				"files_deleted", summary.FilesDeleted,
				"summary_errors_count", len(summary.Errors),
			)
		}
	}

	var candidates []runtimeResultFile
	listErrors := 0
	for _, bucket := range []string{"temp", "persistent"} {
		payload, err := s.southbound.ListFiles(ctx, southbound.FileListRequest{
			AssistantID: assistantID,
			Bucket:      bucket,
			Direction:   "outbound",
			FileRole:    "result",
		})
		if err != nil {
			if syncErr == nil {
				syncErr = err
			}
			listErrors++
			s.logWarn(
				"gateway_runtime_result_files_list_failed",
				"task_id", taskID,
				"assistant_id", assistantID,
				"user_id", userID,
				"bucket", bucket,
				"direction", "outbound",
				"file_role", "result",
				"error", err,
			)
			continue
		}
		candidates = append(candidates, runtimeResultFilesFromList(payload, userID, startedAt, bucket, selector)...)
	}
	sort.SliceStable(candidates, func(i, j int) bool {
		leftChangedAt := runtimeResultFileChangedAt(candidates[i])
		rightChangedAt := runtimeResultFileChangedAt(candidates[j])
		if !leftChangedAt.Equal(rightChangedAt) {
			return leftChangedAt.After(rightChangedAt)
		}
		if leftBucket, rightBucket := resultBucketRank(candidates[i].bucket), resultBucketRank(candidates[j].bucket); leftBucket != rightBucket {
			return leftBucket < rightBucket
		}
		if candidates[i].relativePath != candidates[j].relativePath {
			return candidates[i].relativePath < candidates[j].relativePath
		}
		if candidates[i].filename != candidates[j].filename {
			return candidates[i].filename < candidates[j].filename
		}
		return candidates[i].fileID < candidates[j].fileID
	})
	s.logInfo(
		"gateway_runtime_result_files_candidates_scanned",
		"task_id", taskID,
		"assistant_id", assistantID,
		"user_id", userID,
		"candidate_files_count", len(candidates),
		"sync_errors", syncErrors,
		"list_errors", listErrors,
		"task_scoped", selector.taskScoped(),
		"result_path_prefix", selector.pathPrefix,
		"reply_paths_count", len(selector.replyPaths),
	)
	if len(candidates) == 0 {
		return nil, syncErr
	}
	fileIDs := make([]string, 0, len(candidates))
	seen := map[string]struct{}{}
	for _, candidate := range candidates {
		fileID := strings.TrimSpace(candidate.fileID)
		if fileID == "" {
			continue
		}
		if _, exists := seen[fileID]; exists {
			continue
		}
		seen[fileID] = struct{}{}
		fileIDs = append(fileIDs, fileID)
	}
	return fileIDs, syncErr
}

func runtimeResultFilesFromList(
	payload southbound.RawJSON,
	userID string,
	startedAt time.Time,
	bucket string,
	selector runtimeResultFileSelector,
) []runtimeResultFile {
	rawFiles, ok := payload["files"]
	if !ok {
		return nil
	}
	var items []any
	switch typed := rawFiles.(type) {
	case []any:
		items = typed
	case []southbound.RawJSON:
		items = make([]any, 0, len(typed))
		for _, item := range typed {
			items = append(items, item)
		}
	default:
		return nil
	}
	var out []runtimeResultFile
	for _, item := range items {
		raw, ok := item.(map[string]any)
		if !ok {
			if typed, typedOK := item.(southbound.RawJSON); typedOK {
				raw = map[string]any(typed)
			} else {
				continue
			}
		}
		file := runtimeResultFileFromRaw(raw, bucket)
		if file.fileID == "" || file.userID != userID {
			continue
		}
		if !selector.matches(file) {
			continue
		}
		if !startedAt.IsZero() && !selector.taskScoped() {
			changedAt := runtimeResultFileChangedAt(file)
			if changedAt.IsZero() || changedAt.Before(startedAt.Add(-5*time.Second)) {
				continue
			}
		}
		out = append(out, file)
	}
	return out
}

func runtimeResultFileFromRaw(raw map[string]any, bucket string) runtimeResultFile {
	return runtimeResultFile{
		fileID: firstNonEmptyString(
			rawString(raw["file_id"]),
			rawString(raw["id"]),
		),
		userID: firstNonEmptyString(
			rawString(raw["owner_user_id"]),
			rawString(raw["user_id"]),
		),
		bucket: firstNonEmptyString(rawString(raw["bucket"]), bucket),
		relativePath: firstNonEmptyString(
			rawString(raw["relative_path"]),
			rawString(raw["workspace_relative_path"]),
		),
		filename:  rawString(raw["filename"]),
		sourceRef: rawString(raw["source_ref"]),
		updatedAt: rawTime(raw["updated_at"]),
		createdAt: rawTime(raw["created_at"]),
		workspaceLastModifiedAt: firstNonZeroTime(
			rawTime(raw["workspace_last_modified_at"]),
			rawTime(raw["workspace_mtime"]),
		),
	}
}

func runtimeResultFileChangedAt(file runtimeResultFile) time.Time {
	if !file.workspaceLastModifiedAt.IsZero() {
		return file.workspaceLastModifiedAt
	}
	if !file.updatedAt.IsZero() {
		return file.updatedAt
	}
	return file.createdAt
}

var runtimeWorkspacePathPattern = regexp.MustCompile(`/workspace/(?:temp|persistent)/[^\s"'<>]+`)

func newRuntimeResultFileSelector(taskID string, replyText string) runtimeResultFileSelector {
	return runtimeResultFileSelector{
		pathPrefix: normalizeRuntimeResultRelativePath(runtimeResultPathPrefix(taskID)),
		replyPaths: runtimeReplyWorkspacePaths(replyText),
	}
}

func (s runtimeResultFileSelector) taskScoped() bool {
	return s.pathPrefix != ""
}

func (s runtimeResultFileSelector) shouldRetryMissingFiles() bool {
	return s.taskScoped() && len(s.replyPaths) > 0
}

func (s runtimeResultFileSelector) matches(file runtimeResultFile) bool {
	if s.pathPrefix == "" && len(s.replyPaths) == 0 {
		return true
	}
	if s.pathPrefix != "" {
		for _, value := range runtimeResultFilePathValues(file) {
			if runtimeResultPathMatchesPrefix(value, s.pathPrefix) {
				return true
			}
		}
		return false
	}
	if len(s.replyPaths) > 0 {
		for _, value := range runtimeResultFilePathValues(file) {
			if _, ok := s.replyPaths[value]; ok {
				return true
			}
		}
	}
	return false
}

func runtimeResultPathMatchesPrefix(value string, prefix string) bool {
	value = normalizeRuntimeResultRelativePath(value)
	prefix = strings.TrimSuffix(normalizeRuntimeResultRelativePath(prefix), "/")
	if value == "" || prefix == "" {
		return false
	}
	if value == prefix || strings.HasPrefix(value, prefix+"/") {
		return true
	}
	bucketedPrefix := "/" + prefix
	return strings.HasSuffix(value, bucketedPrefix) || strings.Contains(value, bucketedPrefix+"/")
}

func runtimeResultFilePathValues(file runtimeResultFile) []string {
	raw := []string{file.relativePath, file.filename, file.sourceRef}
	out := make([]string, 0, len(raw)*2)
	seen := map[string]struct{}{}
	appendPath := func(value string) {
		normalized := normalizeRuntimeResultRelativePath(value)
		if normalized == "" {
			return
		}
		if _, exists := seen[normalized]; exists {
			return
		}
		seen[normalized] = struct{}{}
		out = append(out, normalized)
	}
	for _, value := range raw {
		appendPath(value)
		if bucketless := runtimeBucketlessPath(value); bucketless != "" {
			appendPath(bucketless)
		}
	}
	return out
}

func runtimeReplyWorkspacePaths(replyText string) map[string]struct{} {
	matches := runtimeWorkspacePathPattern.FindAllString(replyText, -1)
	if len(matches) == 0 {
		return nil
	}
	out := map[string]struct{}{}
	for _, match := range matches {
		normalized := normalizeRuntimeResultRelativePath(match)
		if normalized == "" {
			continue
		}
		out[normalized] = struct{}{}
		if bucketless := runtimeBucketlessPath(normalized); bucketless != "" {
			out[bucketless] = struct{}{}
		}
	}
	return out
}

func runtimeBucketlessPath(value string) string {
	normalized := normalizeRuntimeResultRelativePath(value)
	for _, bucket := range []string{"temp/", "persistent/"} {
		if strings.HasPrefix(normalized, bucket) {
			return strings.TrimPrefix(normalized, bucket)
		}
	}
	return ""
}

func normalizeRuntimeResultRelativePath(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return ""
	}
	value = strings.Trim(value, "`'\"“”‘’<>")
	value = strings.TrimRight(value, ".,;:，。；：、)]}】》")
	value = strings.ReplaceAll(value, "\\", "/")
	value = strings.TrimPrefix(value, "file://")
	value = strings.TrimPrefix(value, "/workspace/")
	value = strings.TrimLeft(value, "/")
	if value == "" {
		return ""
	}
	normalized := path.Clean(value)
	if normalized == "." {
		return ""
	}
	return normalized
}

func firstNonZeroTime(values ...time.Time) time.Time {
	for _, value := range values {
		if !value.IsZero() {
			return value
		}
	}
	return time.Time{}
}

func resultBucketRank(bucket string) int {
	switch strings.TrimSpace(bucket) {
	case "temp":
		return 0
	case "persistent":
		return 1
	default:
		return 2
	}
}

func rawString(value any) string {
	switch typed := value.(type) {
	case string:
		return strings.TrimSpace(typed)
	case []byte:
		return strings.TrimSpace(string(typed))
	default:
		return ""
	}
}

func rawTime(value any) time.Time {
	switch typed := value.(type) {
	case time.Time:
		return typed
	case *time.Time:
		if typed == nil {
			return time.Time{}
		}
		return *typed
	case string:
		text := strings.TrimSpace(typed)
		if text == "" {
			return time.Time{}
		}
		if parsed, err := time.Parse(time.RFC3339Nano, text); err == nil {
			return parsed
		}
		if parsed, err := time.Parse(time.RFC3339, text); err == nil {
			return parsed
		}
		return time.Time{}
	default:
		return time.Time{}
	}
}

func (s *Service) failRuntimeTask(
	ctx context.Context,
	task southbound.Task,
	claimToken string,
	reservation RuntimeReservation,
	code string,
	cause error,
) (southbound.Task, error) {
	message := strings.TrimSpace(cause.Error())
	failed, err := s.southbound.FailTask(ctx, task.ID, southbound.FailTaskRequest{
		AssistantID:      task.TargetAssistantID,
		ClaimToken:       claimToken,
		FailedReasonCode: code,
		FailedReasonText: message,
	})
	s.releaseRuntimeReservation(ctx, reservation)
	if err != nil {
		s.logWarn(
			"gateway_collaboration_task_fail_write_failed",
			"southbound_task_id", task.ID,
			"target_assistant_id", task.TargetAssistantID,
			"failed_reason_code", code,
			"failed_reason_text", message,
			"error", err,
		)
		return southbound.Task{}, err
	}
	if taskRequiresResultFile(task) {
		if failed.MetaJSON == nil {
			failed.MetaJSON = map[string]any{}
		}
		failed.MetaJSON[taskMetadataRequiresResultFile] = true
	}
	s.logWarn(
		"gateway_collaboration_task_failed",
		"southbound_task_id", task.ID,
		"target_assistant_id", task.TargetAssistantID,
		"status", failed.Status,
		"failed_reason_code", code,
		"failed_reason_text", message,
	)
	return failed, nil
}

func (s *Service) failRuntimeTaskMaybeRetry(
	ctx context.Context,
	task southbound.Task,
	claimToken string,
	reservation RuntimeReservation,
	code string,
	cause error,
	retry bool,
) (southbound.Task, error) {
	if retry && capacityWaitQueuedTaskError(cause) && queuedGatewayTaskID(task) != "" {
		cancelCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), queuedSouthboundCancelTimeout)
		_, cancelErr := s.southbound.CancelTask(cancelCtx, task.ID, southbound.CancelTaskRequest{
			ActorID: "go-gateway-task-worker",
			Reason:  "deferred for runtime capacity recovery: " + strings.TrimSpace(cause.Error()),
		})
		cancel()
		s.releaseRuntimeReservation(context.WithoutCancel(ctx), reservation)
		s.detachQueuedSouthboundTaskForCapacityDefer(context.WithoutCancel(ctx), task, cause)
		if cancelErr != nil && !isSouthboundCancelConflict(cancelErr) {
			s.logWarn(
				"gateway_collaboration_task_capacity_defer_cancel_failed",
				"southbound_task_id", task.ID,
				"target_assistant_id", task.TargetAssistantID,
				"failed_reason_code", code,
				"error", cancelErr,
			)
		}
		s.logWarn(
			"gateway_collaboration_task_deferred_for_capacity",
			"southbound_task_id", task.ID,
			"target_assistant_id", task.TargetAssistantID,
			"failed_reason_code", code,
			"retry", true,
			"error", cause,
		)
		return southbound.Task{}, retryableQueueError{Cause: cause}
	}
	if !retry {
		return s.failRuntimeTask(ctx, task, claimToken, reservation, code, cause)
	}
	s.logWarn(
		"gateway_collaboration_task_fail_for_retry",
		"southbound_task_id", task.ID,
		"target_assistant_id", task.TargetAssistantID,
		"failed_reason_code", code,
		"retry", true,
		"error", cause,
	)
	_, failErr := s.failRuntimeTask(context.WithoutCancel(ctx), task, claimToken, reservation, code, cause)
	if failErr != nil {
		return southbound.Task{}, failErr
	}
	return southbound.Task{}, retryableQueueError{Cause: cause}
}

func queuedGatewayTaskID(task southbound.Task) string {
	if task.MetaJSON == nil {
		return ""
	}
	if value, ok := task.MetaJSON["gateway_task_id"].(string); ok {
		return strings.TrimSpace(value)
	}
	return strings.TrimSpace(rawString(task.MetaJSON["gateway_task_id"]))
}

func (s *Service) detachQueuedSouthboundTaskForCapacityDefer(ctx context.Context, task southbound.Task, cause error) {
	if s.tasks == nil {
		return
	}
	taskID := queuedGatewayTaskID(task)
	if taskID == "" {
		return
	}
	record, ok, err := s.tasks.GetTask(ctx, taskID)
	if err != nil {
		s.logWarn(
			"gateway_task_capacity_defer_record_lookup_failed",
			"task_id", taskID,
			"southbound_task_id", task.ID,
			"error", err,
		)
		return
	}
	if !ok {
		return
	}
	capacityCancelled := taskIsCapacityDeferCancellation(record.Task)
	if taskStatusIsTerminal(record.Task.Status) && !capacityCancelled {
		return
	}
	if existingSouthboundTaskID := strings.TrimSpace(record.SouthboundTaskID); existingSouthboundTaskID != "" && !southboundTaskIDMatches(task, existingSouthboundTaskID) {
		return
	}
	if record.Task.Metadata == nil {
		record.Task.Metadata = map[string]any{}
	}
	if capacityCancelled {
		record.Task.Status = "running"
		record.Task.CompletedAt = nil
		record.Task.FailedAt = nil
		record.Task.FailedReasonCode = ""
		record.Task.FailedReasonText = ""
	}
	record.SouthboundTaskID = ""
	record.Task.Metadata["queue_status"] = "retry"
	record.Task.Metadata["queue_capacity_deferred_southbound_task_id"] = strings.TrimSpace(task.ID)
	if strings.TrimSpace(task.RootTaskID) != "" {
		record.Task.Metadata["queue_capacity_deferred_root_task_id"] = strings.TrimSpace(task.RootTaskID)
	}
	record.Task.Metadata["queue_capacity_deferred_reason"] = strings.TrimSpace(cause.Error())
	now := time.Now().UTC()
	record.UpdatedAt = now
	if err := s.tasks.UpsertTask(ctx, record); err != nil {
		s.logWarn(
			"gateway_task_capacity_defer_record_update_failed",
			"task_id", taskID,
			"southbound_task_id", task.ID,
			"error", err,
		)
	}
}

func southboundTaskIDMatches(task southbound.Task, taskID string) bool {
	taskID = strings.TrimSpace(taskID)
	if taskID == "" {
		return false
	}
	return taskID == strings.TrimSpace(task.ID) || taskID == strings.TrimSpace(task.RootTaskID)
}

func taskIsCapacityDeferCancellation(task TaskView) bool {
	return strings.TrimSpace(task.Status) == "cancelled" &&
		strings.Contains(strings.ToLower(strings.TrimSpace(task.FailedReasonText)), "deferred for runtime capacity recovery")
}

type TaskWorkerOptions struct {
	WorkerID                         string
	ClaimLimit                       int
	LeaseDuration                    time.Duration
	MaxAttempts                      int
	RetryDelay                       time.Duration
	CapacityWaitMaxAttempts          int
	CapacityWaitTimeout              time.Duration
	CapacityWaitCallbackInterval     time.Duration
	StaleProgressTimeout             time.Duration
	ActiveSouthboundDeferTimeout     time.Duration
	TaskProgressCallbackEnabled      bool
	TaskProgressCallbackBatchSize    int
	TaskProgressCallbackInterval     time.Duration
	TaskProgressCallbackStepMaxRunes int
	TaskProgressCallbackStateTTL     time.Duration
}

func (s *Service) TaskQueueStats(ctx context.Context) (TaskQueueStats, error) {
	if s.queue == nil {
		return TaskQueueStats{}, nil
	}
	stats, err := s.queue.TaskQueueStats(ctx)
	if err != nil {
		return TaskQueueStats{}, err
	}
	stats.Active = stats.Admitting + stats.Queued + stats.Processing + stats.Retry
	stats.MaxPending = s.queueMaxPending
	return stats, nil
}

func (s *Service) checkTaskQueueCapacity(ctx context.Context) error {
	if s.queue == nil || s.queueMaxPending <= 0 {
		return nil
	}
	stats, err := s.queue.TaskQueueStats(ctx)
	if err != nil {
		return err
	}
	if stats.Admitting+stats.Queued+stats.Processing+stats.Retry >= s.queueMaxPending {
		return &QueueFullError{Message: "task queue is full"}
	}
	return nil
}

func (s *Service) checkTaskQueueAssistantCapacity(ctx context.Context, targetAssistantID string) error {
	targetAssistantID = strings.TrimSpace(targetAssistantID)
	if !s.taskQueueAssistantCapacityEnabled() || targetAssistantID == "" {
		return nil
	}
	provider, ok := s.queue.(TaskQueueAssistantStatsProvider)
	if !ok {
		return nil
	}
	stats, err := provider.TaskQueueStatsByTargetAssistant(ctx, targetAssistantID)
	if err != nil {
		return err
	}
	if stats.Admitting+stats.Queued+stats.Processing+stats.Retry >= s.queueMaxPendingPerAssistant {
		return &QueueFullError{Message: "target assistant task queue is full"}
	}
	return nil
}

func (s *Service) taskQueueAssistantCapacityEnabled() bool {
	if s.queue == nil || s.queueMaxPendingPerAssistant <= 0 {
		return false
	}
	_, ok := s.queue.(TaskQueueAssistantStatsProvider)
	return ok
}

func (s *Service) DBPoolStats(_ context.Context) (DBPoolStats, bool) {
	if s.tasks != nil {
		if provider, ok := s.tasks.(DBPoolStatsProvider); ok {
			return provider.DBPoolStats(), true
		}
	}
	if s.queue != nil {
		if provider, ok := s.queue.(DBPoolStatsProvider); ok {
			return provider.DBPoolStats(), true
		}
	}
	return DBPoolStats{}, false
}

func (s *Service) Health(ctx context.Context) HealthReport {
	report := NewHealthReport()
	report.SetComponent("gateway", "up", "")
	report.SetComponent("agent_runner", "up", "southbound task orchestration is available")
	report.SetComponent("file_service", "unknown", "southbound health has not reported")
	report.SetComponent("container_scheduler", "unknown", "runtime registry not attached")

	if s.southbound == nil {
		report.SetComponent("southbound_security", "down", "southbound client is not configured")
		report.SetComponent("southbound_admin", "down", "southbound client is not configured")
		report.SetComponent("file_service", "down", "southbound client is not configured")
		return report.Finalize()
	}
	summary, err := s.southbound.CheckHealth(ctx)
	if err != nil {
		report.SetComponent("southbound", "down", err.Error())
		report.SetComponent("file_service", "down", err.Error())
		return report.Finalize()
	}
	fileStatus := "up"
	for _, component := range summary.Components {
		detail := component.Error
		if detail == "" && component.StatusCode > 0 {
			detail = fmt.Sprintf("http %d", component.StatusCode)
		}
		report.SetComponent(component.Name, component.Status, detail)
		if component.Name == "southbound_security" && component.Status != "up" {
			fileStatus = "down"
		}
	}
	report.SetComponent("file_service", fileStatus, "workspace-files depends on southbound_security")
	if _, err := s.TaskQueueStats(ctx); err != nil {
		report.SetComponent("task_queue", "down", err.Error())
	} else if s.queue != nil {
		report.SetComponent("task_queue", "up", "")
	} else {
		report.SetComponent("task_queue", "disabled", "")
	}
	return report.Finalize()
}

func (s *Service) ProcessQueuedTasks(ctx context.Context, options TaskWorkerOptions) (int, error) {
	if s.queue == nil {
		return 0, nil
	}
	if options.WorkerID == "" {
		options.WorkerID = "go-gateway-worker"
	}
	if options.ClaimLimit <= 0 {
		options.ClaimLimit = 1
	}
	if options.LeaseDuration <= 0 {
		options.LeaseDuration = 30 * time.Second
	}
	if options.MaxAttempts <= 0 {
		options.MaxAttempts = 3
	}
	if options.RetryDelay <= 0 {
		options.RetryDelay = time.Second
	}
	items, err := s.queue.ClaimQueuedTasks(ctx, options.WorkerID, options.ClaimLimit, options.LeaseDuration)
	if err != nil {
		s.logWarn(
			"gateway_task_queue_claim_failed",
			"worker_id", options.WorkerID,
			"claim_limit", options.ClaimLimit,
			"lease_duration_ms", int(options.LeaseDuration/time.Millisecond),
			"error", err,
		)
		return 0, err
	}
	if len(items) > 0 {
		s.logInfo(
			"gateway_task_queue_claimed",
			append(
				[]any{
					"worker_id", options.WorkerID,
					"claimed_count", len(items),
					"claim_limit", options.ClaimLimit,
					"lease_duration_ms", int(options.LeaseDuration / time.Millisecond),
				},
				queuedTaskClaimLogAttrs(items)...,
			)...,
		)
	}
	processingCtx := context.WithoutCancel(ctx)
	for _, item := range items {
		attemptStartedAt := time.Now()
		s.logInfo("gateway_task_attempt_started", queuedTaskLogAttrs(item)...)
		err := func() error {
			attemptCtx, stopProgress, failAttempt, progress := s.startQueuedTaskProgress(processingCtx, item, options.StaleProgressTimeout)
			defer stopProgress()
			stopLeaseHeartbeat := s.startQueuedTaskLeaseHeartbeat(attemptCtx, item, options.LeaseDuration, failAttempt)
			defer stopLeaseHeartbeat()
			err := s.ProcessQueuedChatTask(attemptCtx, item, options, failAttempt, progress)
			if cause := queuedAttemptCause(attemptCtx); cause != nil {
				return cause
			}
			return err
		}()
		if err != nil {
			if activeSouthboundQueuedTaskError(err) {
				now := time.Now().UTC()
				// H4 fix: enforce a task-level wall-clock cap on the active-southbound
				// defer loop. Once exceeded, terminalize to failed (emitting a terminal
				// callback) instead of deferring forever — the defer path never consumes
				// attempts, so without this cap the task never reaches a terminal state.
				if terminal := activeSouthboundTerminalReason(item, options, err, now); terminal != nil {
					if cancelled, cancelErr := s.queuedTaskCancelled(processingCtx, item.TaskID); cancelErr == nil && cancelled {
						s.logInfo("gateway_task_active_southbound_timeout_skipped_cancelled", queuedTaskLogAttrs(item)...)
						continue
					}
					failedCurrentAttempt, _ := s.queue.FailQueuedTask(processingCtx, item.TaskID, item.LockedBy, item.AttemptToken, err.Error(), false, nil, terminal)
					if !failedCurrentAttempt {
						s.logWarn(
							"gateway_task_attempt_active_southbound_timeout_record_skipped",
							append(queuedTaskLogAttrs(item), "terminal_reason_code", queueTerminalReasonCode(terminal), "error", err)...,
						)
						continue
					}
					s.logWarn(
						"gateway_task_attempt_active_southbound_timeout",
						append(queuedTaskLogAttrs(item), "terminal_reason_code", queueTerminalReasonCode(terminal), "error", err)...,
					)
					if failed, markErr := s.markQueuedTaskFailed(processingCtx, item, err, terminal, nil); markErr == nil && s.queuedTaskHook != nil {
						s.queuedTaskHook(processingCtx, failed)
					} else if markErr != nil {
						s.logWarn(
							"gateway_task_active_southbound_timeout_mark_failed",
							append(queuedTaskLogAttrs(item), "terminal_reason_code", queueTerminalReasonCode(terminal), "error", markErr)...,
						)
					}
					continue
				}
				next := now.Add(activeSouthboundRecheckDelay(options, item))
				retryAt := &next
				deferred, deferErr := s.queue.DeferQueuedTask(processingCtx, item.TaskID, item.LockedBy, item.AttemptToken, err.Error(), retryAt)
				if !deferred {
					s.logWarn(
						"gateway_task_attempt_active_southbound_defer_skipped",
						append(
							queuedTaskLogAttrs(item),
							"retry_at", logTimePtr(retryAt),
							"error", err,
							"defer_error", deferErr,
						)...,
					)
					continue
				}
				s.logWarn(
					"gateway_task_attempt_deferred_for_active_southbound",
					append(
						queuedTaskLogAttrs(item),
						"retry_at", logTimePtr(retryAt),
						"error", err,
					)...,
				)
				if s.markQueuedTaskRetrying(processingCtx, item, err, retryAt, nil) {
					s.dispatchQueuedTaskRetryUpdate(processingCtx, item, err, retryAt, "active southbound task is still terminalizing")
				}
				continue
			}
			if capacityWaitQueuedTaskError(err) {
				now := time.Now().UTC()
				capacityState := nextCapacityWaitState(item, now)
				if terminal := capacityWaitTerminalReason(capacityState, options, err, now); terminal != nil {
					failedCurrentAttempt, _ := s.queue.FailQueuedTaskForCapacity(processingCtx, item.TaskID, item.LockedBy, item.AttemptToken, err.Error(), terminal, capacityState)
					if !failedCurrentAttempt {
						s.logWarn(
							"gateway_task_attempt_capacity_wait_timeout_record_skipped",
							append(
								queuedTaskLogAttrs(item),
								"capacity_wait_count", capacityState.Count,
								"capacity_wait_first_at", logTimePtr(capacityState.FirstAt),
								"terminal_reason_code", queueTerminalReasonCode(terminal),
								"error", err,
							)...,
						)
						continue
					}
					s.logWarn(
						"gateway_task_attempt_capacity_wait_exhausted",
						append(
							queuedTaskLogAttrs(item),
							"capacity_wait_count", capacityState.Count,
							"capacity_wait_first_at", logTimePtr(capacityState.FirstAt),
							"terminal_reason_code", queueTerminalReasonCode(terminal),
							"error", err,
						)...,
					)
					if failed, markErr := s.markQueuedTaskFailed(processingCtx, item, err, terminal, &capacityState); markErr == nil && s.queuedTaskHook != nil {
						s.queuedTaskHook(processingCtx, failed)
					} else if markErr != nil {
						s.logWarn(
							"gateway_task_capacity_wait_terminal_mark_failed",
							append(
								queuedTaskLogAttrs(item),
								"capacity_wait_count", capacityState.Count,
								"terminal_reason_code", queueTerminalReasonCode(terminal),
								"error", markErr,
							)...,
						)
					}
					continue
				}
				next := time.Now().UTC().Add(jitteredRetryDelay(options.RetryDelay, item.Attempts, item.TaskID))
				retryAt := &next
				deferred, deferErr := s.queue.DeferQueuedTaskForCapacity(processingCtx, item.TaskID, item.LockedBy, item.AttemptToken, err.Error(), retryAt, capacityState)
				if !deferred {
					s.logWarn(
						"gateway_task_attempt_capacity_defer_skipped",
						append(
							queuedTaskLogAttrs(item),
							"retry_at", logTimePtr(retryAt),
							"error", err,
							"defer_error", deferErr,
						)...,
					)
					continue
				}
				s.logWarn(
					"gateway_task_attempt_deferred_for_capacity",
					append(
						queuedTaskLogAttrs(item),
						"retry_at", logTimePtr(retryAt),
						"capacity_wait_count", capacityState.Count,
						"capacity_wait_first_at", logTimePtr(capacityState.FirstAt),
						"error", err,
					)...,
				)
				if s.markQueuedTaskRetrying(processingCtx, item, err, retryAt, &capacityState) {
					if shouldDispatchCapacityWaitUpdate(capacityState, options, now) {
						detail := fmt.Sprintf("runtime capacity unavailable; capacity_wait_count=%d", capacityState.Count)
						if s.dispatchQueuedTaskRetryUpdate(processingCtx, item, err, retryAt, detail) {
							s.markQueuedTaskCapacityWaitCallback(processingCtx, item, now)
						}
					} else {
						s.logInfo(
							"gateway_task_capacity_wait_callback_suppressed",
							append(
								queuedTaskLogAttrs(item),
								"capacity_wait_count", capacityState.Count,
								"capacity_wait_first_at", logTimePtr(capacityState.FirstAt),
								"capacity_wait_last_callback_at", logTimePtr(capacityState.LastCallbackAt),
							)...,
						)
					}
				}
				continue
			}
			retryable := retryQueuedTaskError(err)
			retry := retryable && item.Attempts < options.MaxAttempts
			var retryAt *time.Time
			if retry {
				next := time.Now().UTC().Add(jitteredRetryDelay(options.RetryDelay, item.Attempts, item.TaskID))
				retryAt = &next
			}
			terminal := queueTerminalReason(item, err, options.StaleProgressTimeout, retry)
			failedCurrentAttempt, _ := s.queue.FailQueuedTask(processingCtx, item.TaskID, item.LockedBy, item.AttemptToken, err.Error(), retry, retryAt, terminal)
			if !failedCurrentAttempt {
				s.logWarn(
					"gateway_task_attempt_fail_record_skipped",
					append(
						queuedTaskLogAttrs(item),
						"retry", retry,
						"retry_at", logTimePtr(retryAt),
						"terminal_reason_code", queueTerminalReasonCode(terminal),
						"error", err,
					)...,
				)
				continue
			}
			s.logWarn(
				"gateway_task_attempt_failed",
				append(
					queuedTaskLogAttrs(item),
					"retry", retry,
					"retry_at", logTimePtr(retryAt),
					"terminal_reason_code", queueTerminalReasonCode(terminal),
					"attempt_duration_ms", int(time.Since(attemptStartedAt)/time.Millisecond),
					"error", err,
				)...,
			)
			if retry {
				if s.markQueuedTaskRetrying(processingCtx, item, err, retryAt, nil) {
					s.dispatchQueuedTaskRetryUpdate(processingCtx, item, err, retryAt, "retryable task attempt failed")
				}
			}
			if !retry {
				if failed, markErr := s.markQueuedTaskFailed(processingCtx, item, err, terminal, nil); markErr == nil && s.queuedTaskHook != nil {
					s.queuedTaskHook(processingCtx, failed)
				} else if markErr != nil {
					s.logWarn(
						"gateway_task_terminal_mark_failed",
						append(
							queuedTaskLogAttrs(item),
							"terminal_reason_code", queueTerminalReasonCode(terminal),
							"error", markErr,
						)...,
					)
				}
			}
			continue
		}
		if _, err := s.queue.CompleteQueuedTask(processingCtx, item.TaskID, item.LockedBy, item.AttemptToken); err != nil {
			s.logWarn("gateway_task_attempt_complete_record_failed", append(queuedTaskLogAttrs(item), "error", err)...)
		} else {
			s.logInfo(
				"gateway_task_attempt_completed",
				append(
					queuedTaskLogAttrs(item),
					"attempt_duration_ms", int(time.Since(attemptStartedAt)/time.Millisecond),
				)...,
			)
			if s.queuedTaskHook != nil && s.tasks != nil {
				if record, ok, getErr := s.tasks.GetTask(processingCtx, item.TaskID); getErr != nil {
					s.logWarn("gateway_task_terminal_callback_lookup_failed", append(queuedTaskLogAttrs(item), "error", getErr)...)
				} else if ok && queuedTaskStatusEmitsAfterComplete(record.Task.Status) {
					s.queuedTaskHook(context.WithoutCancel(processingCtx), record.Task)
				}
			}
		}
	}
	return len(items), nil
}

func queuedTaskStatusEmitsAfterComplete(status string) bool {
	switch strings.TrimSpace(status) {
	case "completed", "failed", "cancelled", "rejected", "pending_approval":
		return true
	default:
		return false
	}
}

func queueTerminalReasonCode(reason *QueueTerminalReason) string {
	if reason == nil {
		return ""
	}
	return strings.TrimSpace(reason.Code)
}

func logTimePtr(value *time.Time) string {
	if value == nil || value.IsZero() {
		return ""
	}
	return value.UTC().Format(time.RFC3339Nano)
}

func errorString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

func workspaceScanSummaryError(summary southbound.WorkspaceScanSummary) error {
	if len(summary.Errors) == 0 {
		return nil
	}
	const maxLoggedErrors = 3
	errorsToLog := summary.Errors
	truncated := false
	if len(errorsToLog) > maxLoggedErrors {
		errorsToLog = errorsToLog[:maxLoggedErrors]
		truncated = true
	}
	message := fmt.Sprintf("workspace result sync reported %d error(s): %s", len(summary.Errors), strings.Join(errorsToLog, "; "))
	if truncated {
		message += "; ..."
	}
	return errors.New(message)
}

func jitteredRetryDelay(base time.Duration, attempts int, seed string) time.Duration {
	if base <= 0 {
		base = time.Second
	}
	if attempts <= 0 {
		attempts = 1
	}
	delay := base * time.Duration(attempts)
	jitterWindow := base / 2
	if jitterWindow <= 0 {
		return delay
	}
	sum := sha256.Sum256([]byte(fmt.Sprintf("%s:%d", strings.TrimSpace(seed), attempts)))
	jitterNanos := int64(jitterWindow)
	if jitterNanos <= 0 {
		return delay
	}
	jitter := time.Duration(int64(sum[0]) * jitterNanos / 255)
	return delay + jitter
}

func (s *Service) startQueuedTaskProgress(
	ctx context.Context,
	item GatewayTaskQueueItem,
	staleTimeout time.Duration,
) (context.Context, func(), func(error), queuedProgressRecorder) {
	attemptCtx, cancel := context.WithCancelCause(ctx)
	taskID := strings.TrimSpace(item.TaskID)
	lockedBy := strings.TrimSpace(item.LockedBy)
	attemptToken := strings.TrimSpace(item.AttemptToken)
	targetAssistantID := strings.TrimSpace(item.Request.TargetAssistantID)
	var mu sync.Mutex
	lastProgressAt := time.Now().UTC()
	lastStatus := firstNonEmptyString(item.LastProgressStatus, item.Status)
	lastStage := firstNonEmptyString(item.LastProgressStage, "queued")
	lastDetail := item.LastProgressDetail
	if item.LastProgressAt != nil && !item.LastProgressAt.IsZero() {
		lastProgressAt = item.LastProgressAt.UTC()
	}

	staleError := func() RuntimeStaleTimeoutError {
		mu.Lock()
		defer mu.Unlock()
		return RuntimeStaleTimeoutError{
			Timeout:        staleTimeout,
			LastProgressAt: lastProgressAt,
			LastState:      strings.TrimSpace(firstNonEmptyString(lastStatus, lastStage)),
			LastText:       truncateProgressDetail(lastDetail),
		}
	}
	var timer *time.Timer
	if staleTimeout > 0 {
		timer = time.AfterFunc(staleTimeout, func() {
			stale := staleError()
			s.logWarn(
				"gateway_task_progress_stale",
				"task_id", taskID,
				"target_assistant_id", targetAssistantID,
				"locked_by", lockedBy,
				"attempt_token", attemptToken,
				"user_id", strings.TrimSpace(item.Request.UserID),
				"employee_no", strings.TrimSpace(item.Request.EmployeeNo),
				"stale_timeout_ms", int(staleTimeout/time.Millisecond),
				"last_progress_at", stale.LastProgressAt.UTC().Format(time.RFC3339Nano),
				"last_progress", stale.LastState,
				"last_progress_text", stale.LastText,
			)
			cancel(stale)
		})
	}
	progress := func(progressCtx context.Context, status string, stage string, detail string) (bool, error) {
		now := time.Now().UTC()
		status = strings.TrimSpace(status)
		stage = strings.TrimSpace(stage)
		detail = truncateProgressDetail(detail)
		mu.Lock()
		lastProgressAt = now
		if status != "" {
			lastStatus = status
		}
		if stage != "" {
			lastStage = stage
		}
		lastDetail = detail
		if timer != nil {
			timer.Reset(staleTimeout)
		}
		mu.Unlock()
		if s.queue == nil || taskID == "" || lockedBy == "" || attemptToken == "" {
			return true, nil
		}
		return s.queue.RecordQueuedTaskProgress(progressCtx, taskID, lockedBy, attemptToken, status, stage, detail, now)
	}
	stop := func() {
		if timer != nil {
			timer.Stop()
		}
		cancel(nil)
	}
	fail := func(err error) {
		if err == nil {
			return
		}
		cancel(err)
	}
	return attemptCtx, stop, fail, progress
}

func queuedAttemptCause(ctx context.Context) error {
	cause := context.Cause(ctx)
	if cause == nil || errors.Is(cause, context.Canceled) {
		return nil
	}
	return cause
}

func queuedAttemptStaleCause(ctx context.Context) error {
	cause := context.Cause(ctx)
	if isRuntimeStaleTimeout(cause) {
		return cause
	}
	return nil
}

func isRuntimeStaleTimeout(err error) bool {
	var stale RuntimeStaleTimeoutError
	return errors.As(err, &stale)
}

func truncateProgressDetail(detail string) string {
	detail = strings.TrimSpace(detail)
	if len([]rune(detail)) <= 512 {
		return detail
	}
	runes := []rune(detail)
	return string(runes[:512])
}

func queueTerminalReason(
	item GatewayTaskQueueItem,
	err error,
	staleTimeout time.Duration,
	retry bool,
) *QueueTerminalReason {
	if retry {
		return nil
	}
	var stale RuntimeStaleTimeoutError
	if errors.As(err, &stale) {
		detail := map[string]any{
			"attempts":           item.Attempts,
			"retries":            max(item.Attempts-1, 0),
			"stale_timeout_ms":   int(staleTimeout / time.Millisecond),
			"last_progress_at":   stale.LastProgressAt.UTC().Format(time.RFC3339Nano),
			"last_progress":      stale.LastState,
			"last_progress_text": stale.LastText,
			"last_error":         err.Error(),
			"error_location":     queueWorkerErrorLocation(err),
		}
		detailJSON, _ := json.Marshal(detail)
		return &QueueTerminalReason{
			Code:       "gateway_task_stale_progress",
			Text:       fmt.Sprintf("任务 %s 无业务进展，已强制中断", staleTimeout),
			DetailJSON: string(detailJSON),
		}
	}
	var queueLeaseErr QueueLeaseHeartbeatError
	if errors.As(err, &queueLeaseErr) {
		return &QueueTerminalReason{
			Code: "queue_lease_heartbeat_failed",
			Text: err.Error(),
		}
	}
	var claimHeartbeatErr ClaimHeartbeatError
	if errors.As(err, &claimHeartbeatErr) {
		return &QueueTerminalReason{
			Code: "collaboration_claim_heartbeat_failed",
			Text: err.Error(),
		}
	}
	detail := map[string]any{
		"attempts":       item.Attempts,
		"retries":        max(item.Attempts-1, 0),
		"last_error":     strings.TrimSpace(err.Error()),
		"error_location": queueWorkerErrorLocation(err),
	}
	detailJSON, _ := json.Marshal(detail)
	return &QueueTerminalReason{
		Code:       queueWorkerReasonCode(err),
		Text:       err.Error(),
		DetailJSON: string(detailJSON),
	}
}

// faultAttribution reads the unified fault stamp off an error chain (any error
// implementing FaultAttributed, e.g. AttributedRuntimeError or
// RuntimeStaleTimeoutError).
func faultAttribution(err error) (AttributedRuntimeError, bool) {
	var fa FaultAttributed
	if errors.As(err, &fa) {
		return fa.FaultAttribution(), true
	}
	return AttributedRuntimeError{}, false
}

// queueWorkerFaultDomain classifies any queue-worker error into the unified
// fault attribution (domain/reason/owner/confidence). Typed FaultAttributed
// errors win; a few legacy typed errors and string matches map to their
// domains; everything else is INDETERMINATE owned by the gateway — fail-closed,
// never default-blame the runtime.
func queueWorkerFaultDomain(err error) AttributedRuntimeError {
	if err == nil {
		return AttributedRuntimeError{Domain: FaultDomainNone, Reason: ReasonOK, Owner: FaultOwnerGateway, Confidence: ConfidenceHigh}
	}
	var retryable retryableQueueError
	if errors.As(err, &retryable) && retryable.Cause != nil {
		return queueWorkerFaultDomain(retryable.Cause)
	}
	if fa, ok := faultAttribution(err); ok {
		return fa
	}
	if activeSouthboundQueuedTaskError(err) {
		return AttributedRuntimeError{Domain: FaultDomainGateway, Reason: "gateway.southbound_active_timeout", Owner: FaultOwnerGateway, Confidence: ConfidenceMedium, Retryable: true, Cause: err}
	}
	if capacityWaitQueuedTaskError(err) {
		return AttributedRuntimeError{Domain: FaultDomainGateway, Reason: "gateway.capacity_unavailable", Owner: FaultOwnerGateway, Confidence: ConfidenceHigh, Retryable: true, Cause: err}
	}
	message := strings.ToLower(strings.TrimSpace(err.Error()))
	switch {
	case strings.Contains(message, "connect runtime websocket"):
		return AttributedRuntimeError{Domain: FaultDomainGateway, Reason: ReasonGatewayRuntimeDialFailed, Owner: FaultOwnerGateway, Confidence: ConfidenceLow, EvidenceNote: "matched-by-string", Cause: err}
	case strings.Contains(message, "runtime gateway_url"):
		return AttributedRuntimeError{Domain: FaultDomainGateway, Reason: "gateway.runtime_unconfigured", Owner: FaultOwnerGateway, Confidence: ConfidenceMedium, Cause: err}
	case strings.Contains(message, "result file"):
		return AttributedRuntimeError{Domain: FaultDomainGateway, Reason: "gateway.result_sync_failed", Owner: FaultOwnerGateway, Confidence: ConfidenceMedium, Cause: err}
	}
	// fail-closed: an unattributed failure is the gateway's own blind spot,
	// never the runtime's by default.
	return AttributedRuntimeError{Domain: FaultDomainIndeterminate, Reason: ReasonUndetermined, Owner: FaultOwnerGateway, Confidence: ConfidenceLow, EvidenceNote: "no typed stamp", Cause: err}
}

func queueWorkerReasonCode(err error) string {
	if err == nil {
		return "queue_worker_error"
	}
	var retryable retryableQueueError
	if errors.As(err, &retryable) && retryable.Cause != nil {
		return queueWorkerReasonCode(retryable.Cause)
	}
	if isRuntimeStaleTimeout(err) {
		return "gateway_task_stale_progress"
	}
	if activeSouthboundQueuedTaskError(err) {
		return "gateway_task_active_southbound_timeout"
	}
	if capacityWaitQueuedTaskError(err) {
		return "runtime_capacity_unavailable"
	}
	if fa, ok := faultAttribution(err); ok {
		switch fa.Reason {
		case ReasonGatewayInFlightUnreachable:
			return "runtime_in_flight"
		case ReasonGatewayDeadlineSelf, ReasonUpstreamTimeoutBudget:
			// Keep the legacy client-visible failed_reason_code; the deadline
			// attribution lives in the separate fault_domain/reason_code fields.
			return "runtime_execution_failed"
		}
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return "runtime_execution_failed"
	}
	message := strings.ToLower(strings.TrimSpace(err.Error()))
	switch {
	case strings.Contains(message, "wait runtime chat final"):
		return "runtime_execution_failed"
	case strings.Contains(message, "connect runtime websocket"):
		return "runtime_connection_failed"
	case strings.Contains(message, "runtime gateway_url"):
		return "runtime_gateway_unavailable"
	case strings.Contains(message, "result file"):
		return "runtime_result_file_failed"
	default:
		return "queue_worker_error"
	}
}

func queueWorkerErrorLocation(err error) string {
	if err == nil {
		return "queue_worker"
	}
	var retryable retryableQueueError
	if errors.As(err, &retryable) && retryable.Cause != nil {
		return queueWorkerErrorLocation(retryable.Cause)
	}
	if isRuntimeStaleTimeout(err) {
		return "runtime_ws.stale_progress"
	}
	if activeSouthboundQueuedTaskError(err) {
		return "southbound_task.poll_timeout"
	}
	if capacityWaitQueuedTaskError(err) {
		return "runtime_registry.capacity"
	}
	if fa, ok := faultAttribution(err); ok {
		switch fa.Reason {
		case ReasonGatewayInFlightUnreachable:
			return "runtime_ws.in_flight"
		case ReasonGatewayDeadlineSelf, ReasonUpstreamTimeoutBudget:
			return "runtime_ws.deadline"
		}
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return "runtime_ws.deadline"
	}
	message := strings.ToLower(strings.TrimSpace(err.Error()))
	switch {
	case strings.Contains(message, "wait runtime chat final"):
		return "runtime_ws.wait_final"
	case strings.Contains(message, "connect runtime websocket"):
		return "runtime_ws.connect"
	case strings.Contains(message, "runtime gateway_url"):
		return "runtime_registry.gateway_url"
	case strings.Contains(message, "result file"):
		return "runtime_result_files.sync"
	default:
		return "queue_worker"
	}
}

func (s *Service) startQueuedTaskLeaseHeartbeat(
	ctx context.Context,
	item GatewayTaskQueueItem,
	leaseDuration time.Duration,
	failAttempt func(error),
) func() {
	taskID := strings.TrimSpace(item.TaskID)
	lockedBy := strings.TrimSpace(item.LockedBy)
	if s.queue == nil || taskID == "" || lockedBy == "" || leaseDuration <= 0 {
		return func() {}
	}
	interval := leaseHeartbeatInterval(leaseDuration, 10*time.Second)
	done := make(chan struct{})
	var once sync.Once
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		failures := 0
		for {
			select {
			case <-ctx.Done():
				return
			case <-done:
				return
			case <-ticker.C:
				extendCtx, cancel := context.WithTimeout(ctx, leaseHeartbeatTimeout(interval))
				extended, err := s.queue.ExtendQueuedTaskLease(extendCtx, taskID, lockedBy, leaseDuration)
				cancel()
				if err == nil && extended {
					failures = 0
					continue
				}
				if err == nil && !extended {
					err = errQueuedAttemptLost
				}
				failures++
				if failures >= leaseHeartbeatFailureThreshold || errors.Is(err, errQueuedAttemptLost) {
					s.logWarn(
						"gateway_task_queue_lease_heartbeat_failed",
						"task_id", taskID,
						"locked_by", lockedBy,
						"failures", failures,
						"failure_threshold", leaseHeartbeatFailureThreshold,
						"lease_duration_ms", int(leaseDuration/time.Millisecond),
						"heartbeat_interval_ms", int(interval/time.Millisecond),
						"error", err,
					)
					if failAttempt != nil {
						failAttempt(QueueLeaseHeartbeatError{TaskID: taskID, Cause: err})
					}
					return
				}
			}
		}
	}()
	return func() {
		once.Do(func() {
			close(done)
		})
	}
}

func (s *Service) startSouthboundClaimHeartbeat(
	ctx context.Context,
	taskID string,
	assistantID string,
	claim southbound.Claim,
	reservation RuntimeReservation,
	failAttempt func(error),
) func() {
	taskID = strings.TrimSpace(taskID)
	assistantID = strings.TrimSpace(firstNonEmptyString(claim.AssistantID, assistantID))
	claimToken := strings.TrimSpace(claim.ClaimToken)
	if s.southbound == nil || taskID == "" || assistantID == "" || claimToken == "" {
		return func() {}
	}
	leaseDuration := time.Until(claim.LeaseExpiresAt.Time)
	if leaseDuration <= 0 {
		return func() {}
	}
	interval := leaseHeartbeatInterval(leaseDuration, time.Minute)
	done := make(chan struct{})
	var once sync.Once
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		failures := 0
		for {
			select {
			case <-ctx.Done():
				return
			case <-done:
				return
			case <-ticker.C:
				renewCtx, cancel := context.WithTimeout(ctx, leaseHeartbeatTimeout(interval))
				_, err := s.southbound.RenewTaskClaim(renewCtx, taskID, southbound.RenewClaimRequest{
					AssistantID: assistantID,
					ClaimToken:  claimToken,
				})
				if err == nil {
					reservation = s.renewRuntimeReservation(renewCtx, reservation)
					cancel()
					failures = 0
					continue
				}
				cancel()
				failures++
				if failures >= leaseHeartbeatFailureThreshold || !retryQueueWorkerError(err) {
					s.logWarn(
						"gateway_collaboration_claim_heartbeat_failed",
						"task_id", taskID,
						"assistant_id", assistantID,
						"failures", failures,
						"failure_threshold", leaseHeartbeatFailureThreshold,
						"claim_lease_expires_at", claim.LeaseExpiresAt.Time.UTC().Format(time.RFC3339Nano),
						"heartbeat_interval_ms", int(interval/time.Millisecond),
						"retryable", retryQueueWorkerError(err),
						"error", err,
					)
					if failAttempt != nil {
						failAttempt(ClaimHeartbeatError{TaskID: taskID, Cause: err})
					}
					return
				}
			}
		}
	}()
	return func() {
		once.Do(func() {
			close(done)
		})
	}
}

func leaseHeartbeatInterval(leaseDuration time.Duration, maxInterval time.Duration) time.Duration {
	interval := leaseDuration / 2
	if interval <= 0 {
		return leaseDuration
	}
	if maxInterval > 0 && interval > maxInterval {
		return maxInterval
	}
	return interval
}

func leaseHeartbeatTimeout(interval time.Duration) time.Duration {
	if interval <= 0 {
		return time.Second
	}
	if interval < time.Second {
		return interval
	}
	if interval > 2*time.Second {
		return 2 * time.Second
	}
	return interval
}

func retryQueueWorkerError(err error) bool {
	if err == nil {
		return false
	}
	if fa, ok := faultAttribution(err); ok {
		if fa.Retryable {
			return true
		}
		// A gateway-origin overall deadline is a hard no-retry (B1): re-running
		// under the same deadline only times out again. Any OTHER non-retryable
		// stamp means "not explicitly retryable", NOT "must not retry" — fall
		// through to the legacy transport/capacity net so transient dial /
		// connection-refused / at_capacity / quota / rate-limit failures keep
		// retrying as they did before the attribution layer existed.
		if fa.Reason == ReasonGatewayDeadlineSelf || fa.Reason == ReasonUpstreamTimeoutBudget {
			return false
		}
	}
	var retryable retryableQueueError
	if errors.As(err, &retryable) {
		return true
	}
	if errors.Is(err, context.Canceled) {
		return false
	}
	var httpErr *southbound.HTTPError
	if errors.As(err, &httpErr) {
		return httpErr.StatusCode == http.StatusTooManyRequests || httpErr.StatusCode >= http.StatusInternalServerError
	}
	if isRetryableTransportError(err) {
		return true
	}
	message := strings.ToLower(err.Error())
	return strings.Contains(message, "draining") ||
		strings.Contains(message, "at_capacity") ||
		strings.Contains(message, "at capacity") ||
		runtimeQuotaExceededMessage(message) ||
		strings.Contains(message, "rate limit") ||
		strings.Contains(message, "too many requests") ||
		strings.Contains(message, "runtime made no progress") ||
		strings.Contains(message, "no eligible runtime") ||
		strings.Contains(message, "missing_route") ||
		strings.Contains(message, "stale_heartbeat") ||
		strings.Contains(message, "unhealthy") ||
		strings.Contains(message, "not registered") ||
		strings.Contains(message, "not ready")
}

func retryQueuedTaskError(err error) bool {
	// faultAttribution and the legacy transport/capacity net are both handled by
	// retryQueueWorkerError; a non-retryable stamp (other than a gateway-origin
	// deadline) falls through to the legacy net there.
	return retryQueueWorkerError(err)
}

func activeSouthboundQueuedTaskError(err error) bool {
	var active activeSouthboundTaskError
	return errors.As(err, &active)
}

func activeSouthboundRecheckDelay(options TaskWorkerOptions, item GatewayTaskQueueItem) time.Duration {
	base := options.RetryDelay
	if base <= 0 {
		base = 30 * time.Second
	}
	if base < 5*time.Second {
		base = 5 * time.Second
	}
	if base > options.TaskProgressCallbackInterval && options.TaskProgressCallbackInterval > 0 {
		base = options.TaskProgressCallbackInterval
	}
	if base > 3*time.Minute {
		base = 3 * time.Minute
	}
	delay := jitteredRetryDelay(base, max(item.Attempts, 1), item.TaskID)
	if options.TaskProgressCallbackInterval > 0 && delay > options.TaskProgressCallbackInterval {
		delay = options.TaskProgressCallbackInterval
	}
	if delay > 3*time.Minute {
		delay = 3 * time.Minute
	}
	return delay
}

func nextCapacityWaitState(item GatewayTaskQueueItem, now time.Time) QueueCapacityWaitState {
	if now.IsZero() {
		now = time.Now().UTC()
	}
	state := QueueCapacityWaitState{
		Count:          item.CapacityWaitCount + 1,
		FirstAt:        item.CapacityWaitFirstAt,
		LastAt:         now,
		LastCallbackAt: item.CapacityWaitLastCallbackAt,
	}
	if state.Count <= 0 {
		state.Count = 1
	}
	if state.FirstAt == nil || state.FirstAt.IsZero() {
		first := now.UTC()
		state.FirstAt = &first
	}
	return state
}

func shouldDispatchCapacityWaitUpdate(state QueueCapacityWaitState, options TaskWorkerOptions, now time.Time) bool {
	if state.Count <= 1 {
		return true
	}
	interval := options.CapacityWaitCallbackInterval
	if interval <= 0 {
		return false
	}
	if state.LastCallbackAt == nil || state.LastCallbackAt.IsZero() {
		return true
	}
	return !now.Before(state.LastCallbackAt.Add(interval))
}

func capacityWaitTerminalReason(state QueueCapacityWaitState, options TaskWorkerOptions, cause error, now time.Time) *QueueTerminalReason {
	maxAttempts := options.CapacityWaitMaxAttempts
	timeout := options.CapacityWaitTimeout
	if maxAttempts <= 0 && timeout <= 0 {
		return nil
	}
	reason := ""
	if maxAttempts > 0 && state.Count > maxAttempts {
		reason = fmt.Sprintf("runtime capacity unavailable after %d wait attempts", state.Count)
	}
	if timeout > 0 && state.FirstAt != nil && !state.FirstAt.IsZero() && !now.Before(state.FirstAt.Add(timeout)) {
		timeoutReason := fmt.Sprintf("runtime capacity unavailable for %s", timeout)
		if reason == "" {
			reason = timeoutReason
		} else {
			reason += "; " + timeoutReason
		}
	}
	if reason == "" {
		return nil
	}
	detail := map[string]any{
		"capacityWaitCount": state.Count,
		"lastError":         strings.TrimSpace(cause.Error()),
	}
	if maxAttempts > 0 {
		detail["capacityWaitMaxAttempts"] = maxAttempts
	}
	if timeout > 0 {
		detail["capacityWaitTimeoutSeconds"] = timeout.Seconds()
	}
	if state.FirstAt != nil && !state.FirstAt.IsZero() {
		detail["capacityWaitFirstAt"] = state.FirstAt.UTC().Format(time.RFC3339Nano)
	}
	detail["capacityWaitLastAt"] = state.LastAt.UTC().Format(time.RFC3339Nano)
	detailJSON, _ := json.Marshal(detail)
	return &QueueTerminalReason{
		Code:       "runtime_capacity_timeout",
		Text:       reason + ": " + strings.TrimSpace(cause.Error()),
		DetailJSON: string(detailJSON),
	}
}

// activeSouthboundTerminalReason enforces a task-level wall-clock upper bound on
// the active-southbound defer loop. Without it a downstream task that stays
// "active" forever keeps being deferred (its attempts cancel out via the
// claim+1/defer-1 accounting) and never reaches a terminal state — the H4
// liveness hole. The deadline is anchored at the task's CreatedAt so it is
// monotonic across attempts and is never reset by a defer, mirroring Kubernetes
// activeDeadlineSeconds. CreatedAt includes queue wait time, so the configured
// timeout should be set comfortably larger than CapacityWaitTimeout.
func activeSouthboundTerminalReason(item GatewayTaskQueueItem, options TaskWorkerOptions, cause error, now time.Time) *QueueTerminalReason {
	timeout := options.ActiveSouthboundDeferTimeout
	if timeout <= 0 || item.CreatedAt.IsZero() {
		return nil
	}
	if now.Before(item.CreatedAt.Add(timeout)) {
		return nil
	}
	detail := map[string]any{
		"activeSouthboundDeferTimeoutSeconds": timeout.Seconds(),
		"taskCreatedAt":                       item.CreatedAt.UTC().Format(time.RFC3339Nano),
		"lastError":                           strings.TrimSpace(cause.Error()),
	}
	detailJSON, _ := json.Marshal(detail)
	return &QueueTerminalReason{
		Code:       "runtime_active_southbound_timeout",
		Text:       fmt.Sprintf("runtime southbound task still active after %s", timeout) + ": " + strings.TrimSpace(cause.Error()),
		DetailJSON: string(detailJSON),
	}
}

func capacityWaitQueuedTaskError(err error) bool {
	if err == nil || errors.Is(err, context.Canceled) || isRuntimeStaleTimeout(err) {
		return false
	}
	message := strings.ToLower(err.Error())
	return strings.Contains(message, "draining") ||
		strings.Contains(message, "at_capacity") ||
		strings.Contains(message, "at capacity") ||
		runtimeQuotaExceededMessage(message) ||
		strings.Contains(message, "no eligible runtime") ||
		strings.Contains(message, "missing_route") ||
		strings.Contains(message, "stale_heartbeat") ||
		strings.Contains(message, "unhealthy") ||
		strings.Contains(message, "not registered") ||
		strings.Contains(message, "not ready")
}

func runtimeQuotaExceededMessage(message string) bool {
	return strings.Contains(message, "allocated quota exceeded") ||
		strings.Contains(message, "quota exceeded")
}

func isRetryableTransportError(err error) bool {
	if err == nil || errors.Is(err, context.Canceled) {
		return false
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return true
	}
	if errors.Is(err, io.EOF) {
		return true
	}
	var urlErr *url.Error
	if errors.As(err, &urlErr) {
		return isRetryableTransportError(urlErr.Err)
	}
	var netErr net.Error
	if errors.As(err, &netErr) && (netErr.Timeout() || netErr.Temporary()) {
		return true
	}
	message := strings.ToLower(err.Error())
	return strings.Contains(message, "connection refused") ||
		strings.Contains(message, "connection reset") ||
		strings.Contains(message, "broken pipe") ||
		strings.Contains(message, "no such host") ||
		strings.Contains(message, "server misbehaving") ||
		strings.Contains(message, "i/o timeout") ||
		strings.Contains(message, "unexpected eof")
}

func (s *Service) ProcessQueuedChatTask(ctx context.Context, item GatewayTaskQueueItem, options TaskWorkerOptions, failAttempt func(error), progress queuedProgressRecorder) error {
	item.Request.ClientID = normalizeClientID(item.Request.ClientID)
	item.Request.RequestID = strings.TrimSpace(item.Request.RequestID)
	if item.Request.RequestID != "" {
		item.Request.IdempotencyKey = item.Request.RequestID
	}
	item.Request.IdempotencyKey = strings.TrimSpace(item.Request.IdempotencyKey)
	if item.Request.IdempotencyKey == "" {
		item.Request.IdempotencyKey = strings.TrimSpace(item.TaskID)
	}
	if item.Request.Metadata == nil {
		item.Request.Metadata = map[string]string{}
	}
	item.Request = normalizeChatResultFileRequirement(item.Request)
	item.Request.Metadata["gateway_task_id"] = strings.TrimSpace(item.TaskID)
	if item.Request.RequestID != "" {
		item.Request.Metadata["request_id"] = item.Request.RequestID
	}
	if item.Request.IdempotencyKey != "" {
		item.Request.Metadata["idempotency_key"] = item.Request.IdempotencyKey
	}
	if cancelled, err := s.queuedTaskCancelled(ctx, item.TaskID); err != nil || cancelled {
		if err != nil {
			s.logWarn("gateway_task_cancel_check_failed", append(queuedTaskLogAttrs(item), "error", err)...)
		} else {
			s.logInfo("gateway_task_cancelled_before_attempt", queuedTaskLogAttrs(item)...)
		}
		return err
	}

	progressEmitter := s.newQueuedTaskProgressEmitter(ctx, item, options)
	if progressEmitter != nil {
		defer progressEmitter.Stop(context.WithoutCancel(ctx))
	}

	hooks := &southboundExecutionHooks{
		afterCreate: func(ctx context.Context, task southbound.Task) {
			if progress != nil {
				_, _ = progress(ctx, "claimed", "queued", "southbound_task_id="+task.ID)
			}
			saved, err := s.saveQueuedTaskCheckpoint(ctx, item, task, task.ID, "claimed")
			if err != nil {
				s.logWarn("gateway_task_checkpoint_save_failed", append(queuedTaskLogAttrs(item), "southbound_task_id", task.ID, "checkpoint_queue_status", "claimed", "error", err)...)
			} else {
				s.logInfo("gateway_task_checkpoint_saved", append(queuedTaskLogAttrs(item), "southbound_task_id", task.ID, "checkpoint_queue_status", "claimed", "stored_status", saved.Status)...)
			}
		},
		afterStart: func(ctx context.Context, task southbound.Task) {
			if progress != nil {
				_, _ = progress(ctx, task.Status, "tool_running", "runtime started")
			}
			if progressEmitter != nil {
				progressEmitter.AddStep(ctx, "runtime started", "runtime started", time.Now().UTC())
			}
			saved, err := s.saveQueuedTaskCheckpoint(ctx, item, task, task.ID, "processing")
			if err == nil && s.queuedTaskHook != nil && saved.Status == "running" {
				s.queuedTaskHook(ctx, saved)
			}
			if err != nil {
				s.logWarn("gateway_task_checkpoint_save_failed", append(queuedTaskLogAttrs(item), "southbound_task_id", task.ID, "checkpoint_queue_status", "processing", "error", err)...)
			} else {
				s.logInfo("gateway_task_checkpoint_saved", append(queuedTaskLogAttrs(item), "southbound_task_id", task.ID, "checkpoint_queue_status", "processing", "stored_status", saved.Status)...)
			}
		},
		runtimeProgress: func(ctx context.Context, event RuntimeProgressEvent) {
			if progress != nil {
				stage := "tool_running"
				if event.State == "final" {
					stage = "done"
				}
				if event.State == "error" {
					stage = "error"
				}
				_, _ = progress(ctx, "running", stage, strings.TrimSpace(firstNonEmptyString(event.Text, event.State)))
			}
			if progressEmitter != nil {
				progressEmitter.AddRuntimeEvent(ctx, event)
			}
		},
		resumeRunning: func(ctx context.Context, task southbound.Task) (TaskView, error) {
			return s.waitForQueuedSouthboundTask(ctx, item, task, options, progress, progressEmitter)
		},
		failAttempt:    failAttempt,
		staleTimeout:   options.StaleProgressTimeout,
		deadlineSource: deadlineSourceForTimeoutSeconds(item.Request.TimeoutSeconds),
		gatewayTaskID:  item.TaskID,
		userID:         strings.TrimSpace(item.Request.UserID),
		employeeNo:     strings.TrimSpace(item.Request.EmployeeNo),
	}
	if existingTask, existingSouthboundTaskID, ok, err := s.getQueuedSouthboundTask(ctx, item); err != nil {
		s.logWarn("gateway_task_existing_southbound_lookup_failed", append(queuedTaskLogAttrs(item), "error", err)...)
		return err
	} else if ok {
		if shouldReplaceQueuedSouthboundTask(item, existingTask) {
			s.logInfo(
				"gateway_task_existing_southbound_replaced",
				append(
					queuedTaskLogAttrs(item),
					"southbound_task_id", existingSouthboundTaskID,
					"southbound_status", strings.TrimSpace(existingTask.Status),
					"last_error", strings.TrimSpace(item.LastError),
				)...,
			)
		} else {
			task, err := s.resumeQueuedSouthboundTask(ctx, item, existingTask, options, hooks, progress, progressEmitter)
			if err != nil {
				s.logWarn("gateway_task_existing_southbound_failed", append(queuedTaskLogAttrs(item), "southbound_task_id", existingSouthboundTaskID, "error", err)...)
				return err
			}
			return s.finishQueuedChatTask(ctx, item, task, existingSouthboundTaskID, progress, progressEmitter)
		}
	}
	task, err := s.executeChatTaskSouthbound(ctx, item.Request, hooks)
	if err != nil {
		s.logWarn("gateway_task_execution_failed", append(queuedTaskLogAttrs(item), "error", err)...)
		return err
	}
	southboundTaskID := task.ID
	return s.finishQueuedChatTask(ctx, item, task, southboundTaskID, progress, progressEmitter)
}

func (s *Service) finishQueuedChatTask(
	ctx context.Context,
	item GatewayTaskQueueItem,
	task TaskView,
	southboundTaskID string,
	progress queuedProgressRecorder,
	progressEmitter *queuedTaskProgressEmitter,
) error {
	if progress != nil {
		stage := NewTaskResponse(task).Stage
		ok, err := progress(ctx, task.Status, stage, "southbound_task_id="+southboundTaskID)
		if err != nil {
			s.logWarn("gateway_task_final_progress_record_failed", append(queuedTaskLogAttrs(item), "southbound_task_id", southboundTaskID, "error", err)...)
		}
		if !ok {
			s.logWarn("gateway_task_final_progress_record_skipped", append(queuedTaskLogAttrs(item), "southbound_task_id", southboundTaskID, "error", errQueuedAttemptLost)...)
		}
	}
	if progressEmitter != nil {
		flushCtx, cancelFlush := context.WithTimeout(context.WithoutCancel(ctx), defaultTaskProgressFinalFlushTimeout)
		if err := progressEmitter.Flush(flushCtx); err != nil {
			progressEmitter.flushAsync(context.WithoutCancel(ctx), true)
		}
		cancelFlush()
	}
	saved, err := s.saveQueuedTaskResult(ctx, item, task, southboundTaskID)
	if err != nil {
		s.logWarn("gateway_task_result_save_failed", append(queuedTaskLogAttrs(item), "southbound_task_id", southboundTaskID, "error", err)...)
	} else {
		s.logInfo(
			"gateway_task_result_saved",
			append(append(queuedTaskLogAttrs(item), "southbound_task_id", southboundTaskID), taskViewResultLogAttrs(saved)...)...,
		)
	}
	return err
}

func (s *Service) getQueuedSouthboundTask(ctx context.Context, item GatewayTaskQueueItem) (southbound.Task, string, bool, error) {
	if s.tasks == nil {
		return southbound.Task{}, "", false, nil
	}
	record, ok, err := s.tasks.GetTask(ctx, item.TaskID)
	if err != nil || !ok {
		return southbound.Task{}, "", false, err
	}
	southboundTaskID := strings.TrimSpace(record.SouthboundTaskID)
	if southboundTaskID == "" {
		return southbound.Task{}, "", false, nil
	}
	task, err := s.southbound.GetTask(ctx, southboundTaskID)
	if err != nil {
		return southbound.Task{}, southboundTaskID, false, err
	}
	return task, southboundTaskID, true, nil
}

func shouldReplaceQueuedSouthboundTask(item GatewayTaskQueueItem, task southbound.Task) bool {
	status := strings.ToLower(strings.TrimSpace(task.Status))
	switch status {
	case "failed", "timeout", "expired", "cancelled", "rejected":
		lastError := errors.New(strings.TrimSpace(item.LastError))
		return retryQueuedTaskError(lastError) || staleActiveSouthboundQueueError(lastError)
	default:
		return false
	}
}

func (s *Service) resumeQueuedSouthboundTask(
	ctx context.Context,
	item GatewayTaskQueueItem,
	task southbound.Task,
	options TaskWorkerOptions,
	hooks *southboundExecutionHooks,
	progress queuedProgressRecorder,
	progressEmitter *queuedTaskProgressEmitter,
) (TaskView, error) {
	s.logInfo(
		"gateway_task_existing_southbound_resume",
		append(
			queuedTaskLogAttrs(item),
			"southbound_task_id", strings.TrimSpace(task.ID),
			"southbound_status", strings.TrimSpace(task.Status),
		)...,
	)
	switch strings.ToLower(strings.TrimSpace(task.Status)) {
	case "pending_claim":
		return s.startCreatedTask(ctx, task, hooks)
	case "claimed":
		if task.StartedAt == nil {
			started, err := s.claimStartAndExecuteTask(ctx, task, hooks)
			if err != nil {
				return TaskView{}, err
			}
			return s.enrichTask(ctx, started)
		}
		return s.waitForQueuedSouthboundTask(ctx, item, task, options, progress, progressEmitter)
	case "running":
		return s.waitForQueuedSouthboundTask(ctx, item, task, options, progress, progressEmitter)
	default:
		return s.enrichTask(ctx, task)
	}
}

func (s *Service) waitForQueuedSouthboundTask(
	ctx context.Context,
	item GatewayTaskQueueItem,
	task southbound.Task,
	options TaskWorkerOptions,
	progress queuedProgressRecorder,
	progressEmitter *queuedTaskProgressEmitter,
) (TaskView, error) {
	southboundTaskID := strings.TrimSpace(task.ID)
	if southboundTaskID == "" {
		return TaskView{}, &ValidationError{Message: "southbound task id is required"}
	}
	pollInterval := queuedSouthboundPollInterval(options)
	timeout := time.Duration(task.TimeoutSeconds) * time.Second
	if timeout <= 0 {
		timeout = time.Duration(s.chatTaskTimeoutSeconds(item.Request.TimeoutSeconds)) * time.Second
	}
	deadline := time.Now().UTC().Add(timeout)
	lastLoggedStatus := ""
	for {
		view, err := s.enrichTask(ctx, task)
		if err != nil {
			return TaskView{}, err
		}
		status := strings.TrimSpace(view.Status)
		if taskStatusIsTerminal(status) {
			s.logInfo(
				"gateway_task_existing_southbound_terminal",
				append(
					queuedTaskLogAttrs(item),
					"southbound_task_id", southboundTaskID,
					"southbound_status", status,
				)...,
			)
			return view, nil
		}
		stage := NewTaskResponse(view).Stage
		if progress != nil {
			ok, err := progress(ctx, status, stage, "southbound_task_id="+southboundTaskID)
			if err != nil {
				return TaskView{}, err
			}
			if !ok {
				return TaskView{}, errQueuedAttemptLost
			}
		}
		saved, err := s.saveQueuedTaskCheckpoint(ctx, item, task, southboundTaskID, "processing")
		if err != nil {
			s.logWarn("gateway_task_checkpoint_save_failed", append(queuedTaskLogAttrs(item), "southbound_task_id", southboundTaskID, "checkpoint_queue_status", "processing", "error", err)...)
		} else {
			if s.queuedTaskHook != nil && saved.Status == "running" {
				s.queuedTaskHook(ctx, saved)
			}
			if status != lastLoggedStatus {
				s.logInfo("gateway_task_existing_southbound_wait", append(queuedTaskLogAttrs(item), "southbound_task_id", southboundTaskID, "southbound_status", status, "poll_interval_ms", int(pollInterval/time.Millisecond))...)
				lastLoggedStatus = status
			}
		}
		now := time.Now().UTC()
		if timeout > 0 && !now.Before(deadline) {
			s.cancelStaleQueuedSouthboundTask(context.WithoutCancel(ctx), item, task, firstNonEmptyString(status, "running"), timeout)
			return TaskView{}, activeSouthboundTaskError{
				SouthboundTaskID: southboundTaskID,
				Status:           firstNonEmptyString(status, "running"),
				Timeout:          timeout,
			}
		}
		wait := pollInterval
		if timeout > 0 && now.Add(wait).After(deadline) {
			wait = deadline.Sub(now)
			if wait <= 0 {
				wait = queuedSouthboundPollIntervalMin
			}
		}
		select {
		case <-ctx.Done():
			if cause := queuedAttemptCause(ctx); cause != nil {
				return TaskView{}, cause
			}
			return TaskView{}, ctx.Err()
		case <-time.After(wait):
		}
		task, err = s.southbound.GetTask(ctx, southboundTaskID)
		if err != nil {
			return TaskView{}, err
		}
	}
}

func staleActiveSouthboundQueueError(err error) bool {
	if err == nil {
		return false
	}
	if activeSouthboundQueuedTaskError(err) {
		return true
	}
	message := strings.ToLower(strings.TrimSpace(err.Error()))
	return strings.Contains(message, "collaboration task") &&
		(strings.Contains(message, "still running after") ||
			strings.Contains(message, "still claimed after"))
}

func (s *Service) cancelStaleQueuedSouthboundTask(
	ctx context.Context,
	item GatewayTaskQueueItem,
	task southbound.Task,
	status string,
	timeout time.Duration,
) {
	if s.southbound == nil {
		return
	}
	southboundTaskID := strings.TrimSpace(task.ID)
	if southboundTaskID == "" {
		return
	}
	cancelCtx, cancel := context.WithTimeout(ctx, queuedSouthboundCancelTimeout)
	defer cancel()
	reason := fmt.Sprintf("gateway queued task %s replaced stale southbound task after %s", strings.TrimSpace(item.TaskID), timeout)
	cancelled, err := s.southbound.CancelTask(cancelCtx, southboundTaskID, southbound.CancelTaskRequest{
		ActorID: "go-gateway-task-worker",
		Reason:  reason,
	})
	if err != nil {
		if isSouthboundCancelConflict(err) {
			s.logInfo(
				"gateway_task_existing_southbound_cancel_skipped_terminal",
				append(
					queuedTaskLogAttrs(item),
					"southbound_task_id", southboundTaskID,
					"southbound_status", strings.TrimSpace(status),
					"timeout_ms", int(timeout/time.Millisecond),
					"error", err,
				)...,
			)
			return
		}
		s.logWarn(
			"gateway_task_existing_southbound_cancel_failed",
			append(
				queuedTaskLogAttrs(item),
				"southbound_task_id", southboundTaskID,
				"southbound_status", strings.TrimSpace(status),
				"timeout_ms", int(timeout/time.Millisecond),
				"error", err,
			)...,
		)
		return
	}
	s.logWarn(
		"gateway_task_existing_southbound_cancelled_for_retry",
		append(
			queuedTaskLogAttrs(item),
			"southbound_task_id", southboundTaskID,
			"southbound_status", strings.TrimSpace(status),
			"cancelled_status", strings.TrimSpace(cancelled.Status),
			"timeout_ms", int(timeout/time.Millisecond),
		)...,
	)
}

func queuedSouthboundPollInterval(options TaskWorkerOptions) time.Duration {
	interval := options.RetryDelay
	if interval <= 0 {
		interval = time.Second
	}
	if interval < queuedSouthboundPollIntervalMin {
		return queuedSouthboundPollIntervalMin
	}
	if interval > queuedSouthboundPollIntervalMax {
		return queuedSouthboundPollIntervalMax
	}
	return interval
}

func (s *Service) queuedTaskCancelled(ctx context.Context, taskID string) (bool, error) {
	if s.tasks == nil {
		return false, nil
	}
	record, ok, err := s.tasks.GetTask(ctx, taskID)
	if err != nil || !ok {
		return false, err
	}
	return record.Task.Status == "cancelled", nil
}

// markQueuedTaskRetrying marks a task back to running for a pending retry and
// reports whether it actually did so (applied). Callers gate the task.retrying
// progress callback on this return value to avoid emitting a contradictory
// task.retrying after the task has already reached a real terminal state.
func (s *Service) markQueuedTaskRetrying(ctx context.Context, item GatewayTaskQueueItem, cause error, retryAt *time.Time, capacityState *QueueCapacityWaitState) bool {
	if s.tasks == nil {
		return false
	}
	record, ok, err := s.tasks.GetTask(ctx, item.TaskID)
	if err != nil || !ok {
		if err != nil {
			s.logWarn("gateway_task_retry_mark_lookup_failed", append(queuedTaskLogAttrs(item), "error", err)...)
		}
		return false
	}
	// State-machine monotonicity: a real terminal state (cancelled/failed/…)
	// must not be resurrected to running by a racing retry. Capacity-defer
	// cancellation is a reversible pseudo-terminal state and is exempted, mirroring
	// detachQueuedSouthboundTaskForCapacityDefer's guard.
	if taskStatusIsTerminal(record.Task.Status) && !taskIsCapacityDeferCancellation(record.Task) {
		s.logInfo("gateway_task_retry_mark_skipped_terminal", append(queuedTaskLogAttrs(item), "status", record.Task.Status)...)
		return false
	}
	now := time.Now().UTC()
	record.Task.Status = "running"
	record.Task.CompletedAt = nil
	record.Task.FailedAt = nil
	record.Task.FailedReasonCode = ""
	record.Task.FailedReasonText = ""
	if record.Task.StartedAt == nil {
		record.Task.StartedAt = &now
	}
	if record.Task.Metadata == nil {
		record.Task.Metadata = map[string]any{}
	}
	record.Task.Metadata["queue_status"] = "retry"
	record.Task.Metadata["queue_attempts"] = item.Attempts
	record.Task.Metadata["queue_last_error"] = strings.TrimSpace(cause.Error())
	record.Task.Metadata["queue_retry_reason_code"] = queueWorkerReasonCode(cause)
	record.Task.Metadata["queue_error_location"] = queueWorkerErrorLocation(cause)
	if capacityState != nil {
		setCapacityWaitMetadata(record.Task.Metadata, *capacityState)
	} else {
		clearCapacityWaitMetadata(record.Task.Metadata)
	}
	retryAttr := queueWorkerFaultDomain(cause)
	setFaultAttributionMetadata(record.Task.Metadata, retryAttr)
	if retryAt != nil && !retryAt.IsZero() {
		record.Task.Metadata["queue_next_attempt_at"] = retryAt.UTC().Format(time.RFC3339Nano)
	}
	record.UpdatedAt = now
	if err := s.tasks.UpsertTask(ctx, record); err != nil {
		s.logWarn(
			"gateway_task_retry_mark_failed",
			append(
				queuedTaskLogAttrs(item),
				"retry_at", logTimePtr(retryAt),
				"error_location", queueWorkerErrorLocation(cause),
				"error", err,
			)...,
		)
		return false
	}
	s.logInfo(
		"gateway_task_retry_marked",
		append(
			queuedTaskLogAttrs(item),
			"retry_at", logTimePtr(retryAt),
			"retry_reason_code", queueWorkerReasonCode(cause),
			"error_location", queueWorkerErrorLocation(cause),
			"fault_domain", string(retryAttr.Domain),
			"reason_code", retryAttr.Reason,
			"fault_owner", string(retryAttr.Owner),
			"attribution_confidence", string(retryAttr.Confidence),
		)...,
	)
	return true
}

func (s *Service) dispatchQueuedTaskRetryUpdate(ctx context.Context, item GatewayTaskQueueItem, cause error, retryAt *time.Time, detail string) bool {
	if s.queuedTaskProgressHook == nil {
		return false
	}
	reasonCode := queueWorkerReasonCode(cause)
	errorLocation := queueWorkerErrorLocation(cause)
	reply := queuedTaskRetryReplyText(item, cause, retryAt, errorLocation, detail)
	update := QueuedTaskProgressUpdate{
		TaskID:        strings.TrimSpace(item.TaskID),
		EventType:     "task.retrying",
		Status:        "retrying",
		Stage:         "retry_wait",
		ReplyText:     reply,
		ErrorCode:     reasonCode,
		ErrorMessage:  strings.TrimSpace(cause.Error()),
		ErrorLocation: errorLocation,
		Reason:        firstNonEmptyString(detail, strings.TrimSpace(cause.Error())),
	}
	if err := s.queuedTaskProgressHook(context.WithoutCancel(ctx), update); err != nil {
		s.logWarn(
			"gateway_task_retry_callback_enqueue_failed",
			append(
				queuedTaskLogAttrs(item),
				"retry_at", logTimePtr(retryAt),
				"retry_reason_code", reasonCode,
				"error_location", errorLocation,
				"callback_error", err,
			)...,
		)
		return false
	}
	return true
}

func queuedTaskRetryReplyText(item GatewayTaskQueueItem, cause error, retryAt *time.Time, errorLocation string, detail string) string {
	parts := []string{
		"任务本次 attempt 未完成，Gateway 已安排重试。",
		fmt.Sprintf("attempt=%d", item.Attempts),
	}
	if retryAt != nil && !retryAt.IsZero() {
		parts = append(parts, "retry_at="+retryAt.UTC().Format(time.RFC3339Nano))
	}
	if errorLocation != "" {
		parts = append(parts, "error_location="+errorLocation)
	}
	if detail = strings.TrimSpace(detail); detail != "" {
		parts = append(parts, "detail="+detail)
	}
	if cause != nil && strings.TrimSpace(cause.Error()) != "" {
		parts = append(parts, "error="+strings.TrimSpace(cause.Error()))
	}
	return strings.Join(parts, "\n")
}

func (s *Service) markQueuedTaskCapacityWaitCallback(ctx context.Context, item GatewayTaskQueueItem, notifiedAt time.Time) {
	if s.queue != nil {
		if ok, err := s.queue.RecordQueuedTaskCapacityWaitCallback(ctx, item.TaskID, notifiedAt); err != nil {
			s.logWarn("gateway_task_capacity_wait_callback_queue_mark_failed", append(queuedTaskLogAttrs(item), "error", err)...)
		} else if !ok {
			s.logWarn("gateway_task_capacity_wait_callback_queue_mark_skipped", queuedTaskLogAttrs(item)...)
		}
	}
	if s.tasks == nil {
		return
	}
	record, ok, err := s.tasks.GetTask(ctx, item.TaskID)
	if err != nil || !ok {
		if err != nil {
			s.logWarn("gateway_task_capacity_wait_callback_mark_lookup_failed", append(queuedTaskLogAttrs(item), "error", err)...)
		}
		return
	}
	if record.Task.Metadata == nil {
		record.Task.Metadata = map[string]any{}
	}
	record.Task.Metadata[taskMetadataQueueCapacityWaitLastCallbackAt] = notifiedAt.UTC().Format(time.RFC3339Nano)
	record.UpdatedAt = notifiedAt.UTC()
	if err := s.tasks.UpsertTask(ctx, record); err != nil {
		s.logWarn("gateway_task_capacity_wait_callback_mark_failed", append(queuedTaskLogAttrs(item), "error", err)...)
	}
}

// setFaultAttributionMetadata writes the four fault-attribution axes onto task
// metadata under the same keys the structured logs use, so both sinks stay in
// lockstep (a divergence here was the root of review finding #8).
func setFaultAttributionMetadata(metadata map[string]any, attr AttributedRuntimeError) {
	metadata["fault_domain"] = string(attr.Domain)
	metadata["reason_code"] = attr.Reason
	metadata["fault_owner"] = string(attr.Owner)
	metadata["attribution_confidence"] = string(attr.Confidence)
}

func setCapacityWaitMetadata(metadata map[string]any, state QueueCapacityWaitState) {
	if metadata == nil {
		return
	}
	metadata[taskMetadataQueueCapacityWaitCount] = state.Count
	if state.FirstAt != nil && !state.FirstAt.IsZero() {
		metadata[taskMetadataQueueCapacityWaitFirstAt] = state.FirstAt.UTC().Format(time.RFC3339Nano)
	} else {
		delete(metadata, taskMetadataQueueCapacityWaitFirstAt)
	}
	if !state.LastAt.IsZero() {
		metadata[taskMetadataQueueCapacityWaitLastAt] = state.LastAt.UTC().Format(time.RFC3339Nano)
	} else {
		delete(metadata, taskMetadataQueueCapacityWaitLastAt)
	}
	if state.LastCallbackAt != nil && !state.LastCallbackAt.IsZero() {
		metadata[taskMetadataQueueCapacityWaitLastCallbackAt] = state.LastCallbackAt.UTC().Format(time.RFC3339Nano)
	} else {
		delete(metadata, taskMetadataQueueCapacityWaitLastCallbackAt)
	}
}

func clearCapacityWaitMetadata(metadata map[string]any) {
	if metadata == nil {
		return
	}
	delete(metadata, taskMetadataQueueCapacityWaitCount)
	delete(metadata, taskMetadataQueueCapacityWaitFirstAt)
	delete(metadata, taskMetadataQueueCapacityWaitLastAt)
	delete(metadata, taskMetadataQueueCapacityWaitLastCallbackAt)
}

func (s *Service) markQueuedTaskFailed(ctx context.Context, item GatewayTaskQueueItem, cause error, terminal *QueueTerminalReason, capacityState *QueueCapacityWaitState) (TaskView, error) {
	if s.tasks == nil {
		return TaskView{}, nil
	}
	record, ok, err := s.tasks.GetTask(ctx, item.TaskID)
	if err != nil || !ok {
		return TaskView{}, err
	}
	// Terminal-state-by-outcome: a real terminal state (notably cancelled) wins
	// over a late worker failure verdict. Return the true terminal view so the
	// caller's hook emits the real terminal event, not a spurious failed one.
	// Capacity-defer cancellation is reversible and may still terminalize to failed.
	if taskStatusIsTerminal(record.Task.Status) && !taskIsCapacityDeferCancellation(record.Task) {
		s.logInfo("gateway_task_fail_mark_skipped_terminal", "task_id", item.TaskID, "status", record.Task.Status)
		return record.Task, nil
	}
	now := time.Now().UTC()
	record.Task.Status = "failed"
	record.Task.FailedAt = &now
	record.Task.FailedReasonCode = "queue_worker_error"
	record.Task.FailedReasonText = cause.Error()
	if terminal != nil {
		if strings.TrimSpace(terminal.Code) != "" {
			record.Task.FailedReasonCode = strings.TrimSpace(terminal.Code)
		}
		if strings.TrimSpace(terminal.Text) != "" {
			record.Task.FailedReasonText = strings.TrimSpace(terminal.Text)
		}
	}
	if record.Task.Metadata == nil {
		record.Task.Metadata = map[string]any{}
	}
	record.Task.Metadata["queue_status"] = "failed"
	record.Task.Metadata["queue_attempts"] = item.Attempts
	record.Task.Metadata["queue_last_error"] = strings.TrimSpace(cause.Error())
	record.Task.Metadata["queue_error_location"] = queueWorkerErrorLocation(cause)
	if capacityState != nil {
		setCapacityWaitMetadata(record.Task.Metadata, *capacityState)
	} else {
		clearCapacityWaitMetadata(record.Task.Metadata)
	}
	failAttr := queueWorkerFaultDomain(cause)
	setFaultAttributionMetadata(record.Task.Metadata, failAttr)
	if terminal != nil && strings.TrimSpace(terminal.DetailJSON) != "" {
		record.Task.Metadata["blocked_detail"] = terminal.DetailJSON
	}
	if err := s.tasks.UpsertTask(ctx, record); err != nil {
		return TaskView{}, err
	}
	// Surface the terminal failure + its attribution on the log stream (not just
	// DB metadata) so on-call can grep a single event for the fault domain of any
	// terminally-failed queued task — including capacity-wait-exhaustion and
	// indeterminate causes whose only other fault signal is this record's metadata.
	s.logWarn(
		"gateway_queued_task_terminal_failed",
		"task_id", item.TaskID,
		"queue_attempts", item.Attempts,
		"user_id", strings.TrimSpace(record.UserID),
		"employee_no", strings.TrimSpace(record.EmployeeNo),
		"failed_reason_code", record.Task.FailedReasonCode,
		"fault_domain", string(failAttr.Domain),
		"reason_code", failAttr.Reason,
		"fault_owner", string(failAttr.Owner),
		"attribution_confidence", string(failAttr.Confidence),
		"error", cause,
	)
	return record.Task, nil
}

func (s *Service) UploadFile(ctx context.Context, request FileUploadRequest) (southbound.RawJSON, error) {
	if strings.TrimSpace(request.AssistantID) == "" && strings.TrimSpace(request.UserID) == "" {
		return nil, &ValidationError{Message: "assistant_id or user_id is required"}
	}
	if strings.TrimSpace(request.Filename) == "" {
		return nil, &ValidationError{Message: "filename is required"}
	}
	if len(request.Content) == 0 && request.ContentReader == nil {
		return nil, &ValidationError{Message: "file content is required"}
	}

	targetBucket := strings.TrimSpace(request.TargetBucket)
	if targetBucket == "" {
		targetBucket = "temp"
	}
	assistantID := strings.TrimSpace(request.AssistantID)
	userID := strings.TrimSpace(request.UserID)
	if assistantID == "" && userID != "" {
		resolvedAssistantID, err := s.resolveAssistantID(ctx, "", userID)
		if err != nil {
			return nil, err
		}
		assistantID = resolvedAssistantID
	}

	return s.southbound.UploadFile(ctx, southbound.FileUploadRequest{
		AssistantID:    assistantID,
		UserID:         userID,
		TargetBucket:   targetBucket,
		Overwrite:      request.Overwrite,
		SourceType:     strings.TrimSpace(request.SourceType),
		SourceRef:      strings.TrimSpace(request.SourceRef),
		UploaderUserID: strings.TrimSpace(request.UploaderUserID),
		Filename:       strings.TrimSpace(request.Filename),
		ContentType:    strings.TrimSpace(request.ContentType),
		Content:        request.Content,
		ContentReader:  request.ContentReader,
	})
}

func (s *Service) ListFiles(ctx context.Context, request FileListRequest) (southbound.RawJSON, error) {
	if strings.TrimSpace(request.AssistantID) == "" && strings.TrimSpace(request.UserID) == "" {
		return nil, &ValidationError{Message: "assistant_id or user_id is required"}
	}
	return s.southbound.ListFiles(ctx, southbound.FileListRequest{
		AssistantID: strings.TrimSpace(request.AssistantID),
		UserID:      strings.TrimSpace(request.UserID),
		Bucket:      strings.TrimSpace(request.Bucket),
		Direction:   strings.TrimSpace(request.Direction),
		FileRole:    strings.TrimSpace(request.FileRole),
	})
}

func (s *Service) SyncResults(
	ctx context.Context,
	request SyncResultsRequest,
) (southbound.WorkspaceScanSummary, error) {
	if strings.TrimSpace(request.AssistantID) == "" {
		return southbound.WorkspaceScanSummary{}, &ValidationError{Message: "assistant_id is required"}
	}
	return s.southbound.SyncResults(ctx, southbound.SyncResultsRequest{
		AssistantID:      strings.TrimSpace(request.AssistantID),
		Bucket:           strings.TrimSpace(request.Bucket),
		UserID:           strings.TrimSpace(request.UserID),
		ChangedAfter:     request.ChangedAfter,
		ResultPathPrefix: strings.TrimSpace(request.ResultPathPrefix),
		TaskID:           strings.TrimSpace(request.TaskID),
		SourceRef:        strings.TrimSpace(request.SourceRef),
	})
}

func (s *Service) GetFile(ctx context.Context, fileID string) (southbound.RawJSON, error) {
	fileID = strings.TrimSpace(fileID)
	if fileID == "" {
		return nil, &ValidationError{Message: "file_id is required"}
	}
	return s.southbound.GetFile(ctx, fileID)
}

func (s *Service) DownloadFile(
	ctx context.Context,
	fileID string,
	disposition string,
) (FileDownloadResponse, error) {
	fileID = strings.TrimSpace(fileID)
	if fileID == "" {
		return FileDownloadResponse{}, &ValidationError{Message: "file_id is required"}
	}
	downloaded, err := s.DownloadFileStream(ctx, fileID, disposition)
	if err != nil {
		return FileDownloadResponse{}, err
	}
	defer downloaded.Body.Close()
	content, err := io.ReadAll(downloaded.Body)
	if err != nil {
		return FileDownloadResponse{}, err
	}
	return FileDownloadResponse{
		Content:            content,
		ContentType:        downloaded.ContentType,
		ContentDisposition: downloaded.ContentDisposition,
		DownloadSource:     downloaded.DownloadSource,
		StorageBackend:     downloaded.StorageBackend,
	}, nil
}

func (s *Service) DownloadFileStream(
	ctx context.Context,
	fileID string,
	disposition string,
) (FileDownloadStream, error) {
	fileID = strings.TrimSpace(fileID)
	if fileID == "" {
		return FileDownloadStream{}, &ValidationError{Message: "file_id is required"}
	}
	downloaded, err := s.southbound.DownloadFileStream(ctx, fileID, disposition)
	if err != nil {
		return FileDownloadStream{}, err
	}
	return FileDownloadStream{
		Body:               downloaded.ResponseBody,
		ContentType:        downloaded.ContentType,
		ContentDisposition: downloaded.ContentDisposition,
		DownloadSource:     downloaded.DownloadSource,
		StorageBackend:     downloaded.StorageBackend,
	}, nil
}

func (s *Service) DeleteFile(ctx context.Context, fileID string, request FileDeleteRequest) (southbound.RawJSON, error) {
	fileID = strings.TrimSpace(fileID)
	if fileID == "" {
		return nil, &ValidationError{Message: "file_id is required"}
	}
	return s.southbound.DeleteFile(ctx, fileID, strings.TrimSpace(request.UserID))
}

func resolveChatSourceAssistantID(request ChatRequest) string {
	if sourceAssistantID := strings.TrimSpace(request.SourceAssistantID); sourceAssistantID != "" {
		return sourceAssistantID
	}
	seed := strings.TrimSpace(request.ClientID)
	if seed == "" {
		seed = strings.TrimSpace(request.UserID)
	}
	if seed == "" {
		seed = strings.TrimSpace(request.CreatedBy)
	}
	if seed == "" {
		seed = "northbound"
	}
	const externalPrefix = "external-"
	return externalPrefix + sanitizeExternalAssistantID(seed, 64-len(externalPrefix))
}

func sanitizeExternalAssistantID(value string, maxLength int) string {
	value = strings.TrimSpace(value)
	if maxLength <= 0 {
		maxLength = 64
	}
	var builder strings.Builder
	lastDash := false
	for _, r := range strings.ToLower(value) {
		isAlphaNum := (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9')
		if isAlphaNum {
			builder.WriteRune(r)
			lastDash = false
			continue
		}
		if !lastDash {
			builder.WriteByte('-')
			lastDash = true
		}
	}
	normalized := strings.Trim(builder.String(), "-")
	if normalized == "" {
		return "northbound"
	}
	if len(normalized) > maxLength {
		normalized = strings.Trim(normalized[:maxLength], "-")
	}
	if normalized == "" {
		return "northbound"
	}
	return normalized
}

func (s *Service) resolveChatPreferredTargetAssistantID(
	ctx context.Context,
	request ChatRequest,
) (string, error) {
	if targetAssistantID := strings.TrimSpace(request.TargetAssistantID); targetAssistantID != "" {
		if err := s.validateExplicitConversationRoute(ctx, request, targetAssistantID); err != nil {
			return "", err
		}
		return targetAssistantID, nil
	}
	if route, ok, err := s.lookupChatConversationRoute(ctx, request); err != nil || ok {
		if err != nil {
			return "", err
		}
		return route.AssistantID, nil
	}
	userID := strings.TrimSpace(request.UserID)
	if userID == "" {
		return "", nil
	}
	return s.resolveAssistantID(ctx, "", userID)
}

func (s *Service) conversationRouteStore() ConversationRouteStore {
	if s.tasks == nil {
		return nil
	}
	store, _ := s.tasks.(ConversationRouteStore)
	return store
}

func (s *Service) lookupChatConversationRoute(
	ctx context.Context,
	request ChatRequest,
) (ConversationRouteRecord, bool, error) {
	store := s.conversationRouteStore()
	if store == nil {
		return ConversationRouteRecord{}, false, nil
	}
	clientID := normalizeClientID(request.ClientID)
	userID := strings.TrimSpace(request.UserID)
	conversationID := strings.TrimSpace(request.ConversationID)
	if userID == "" || conversationID == "" {
		return ConversationRouteRecord{}, false, nil
	}
	return store.GetConversationRoute(ctx, clientID, userID, conversationID)
}

func (s *Service) validateExplicitConversationRoute(
	ctx context.Context,
	request ChatRequest,
	targetAssistantID string,
) error {
	route, ok, err := s.lookupChatConversationRoute(ctx, request)
	if err != nil || !ok {
		return err
	}
	if strings.TrimSpace(route.AssistantID) == strings.TrimSpace(targetAssistantID) {
		return nil
	}
	return &ValidationError{
		Message: fmt.Sprintf(
			"conversation_id %q is already bound to assistant %q",
			strings.TrimSpace(request.ConversationID),
			strings.TrimSpace(route.AssistantID),
		),
	}
}

func (s *Service) bindChatConversationRoute(
	ctx context.Context,
	request ChatRequest,
	targetAssistantID string,
) error {
	store := s.conversationRouteStore()
	if store == nil {
		return nil
	}
	record := ConversationRouteRecord{
		ClientID:       normalizeClientID(request.ClientID),
		UserID:         strings.TrimSpace(request.UserID),
		ConversationID: strings.TrimSpace(request.ConversationID),
		AssistantID:    strings.TrimSpace(targetAssistantID),
	}
	if record.UserID == "" || record.ConversationID == "" || record.AssistantID == "" {
		return nil
	}
	_, _, err := store.BindConversationRoute(ctx, record)
	return err
}

func (s *Service) resolveAssistantID(ctx context.Context, assistantID string, userID string) (string, error) {
	assistantID = strings.TrimSpace(assistantID)
	if assistantID != "" {
		return assistantID, nil
	}

	userID = strings.TrimSpace(userID)
	if userID == "" {
		return "", &ValidationError{Message: "缺少用户标识，请传入 user_id"}
	}

	return s.resolveAssistantIDForUser(ctx, userID)
}

func (s *Service) resolveAssistantIDForUser(ctx context.Context, userID string) (string, error) {
	if assistantID, ok := s.cachedAssistantResolution(userID, time.Now().UTC()); ok {
		return assistantID, nil
	}
	if s.assistantCache != nil {
		assistantID, ok, err := s.assistantCache.GetAssistantForUser(ctx, userID)
		if err == nil && ok && strings.TrimSpace(assistantID) != "" {
			assistantID = strings.TrimSpace(assistantID)
			s.storeAssistantResolution(userID, assistantID, time.Now().UTC())
			return assistantID, nil
		}
	}
	call, leader := s.beginAssistantResolution(userID)
	if !leader {
		select {
		case <-call.done:
			return call.assistantID, call.err
		case <-ctx.Done():
			return "", ctx.Err()
		}
	}
	assistantID, err := s.resolveAssistantIDFromSouthbound(ctx, userID)
	s.finishAssistantResolution(userID, call, assistantID, err)
	if err == nil && strings.TrimSpace(assistantID) != "" && s.assistantCache != nil {
		_ = s.assistantCache.SetAssistantForUser(ctx, userID, assistantID)
	}
	return assistantID, err
}

func (s *Service) resolveAssistantIDFromSouthbound(ctx context.Context, userID string) (string, error) {
	bindings, err := s.southbound.ListUserAssistants(ctx, userID)
	if err != nil {
		return "", err
	}
	if len(bindings) == 0 {
		return "", &ValidationError{Message: noAvailableAssistantMessage}
	}
	sortUserAssistantBindings(bindings)
	for _, binding := range bindings {
		candidateID := strings.TrimSpace(binding.AssistantID)
		if candidateID == "" {
			continue
		}
		available, err := s.assistantAvailable(ctx, candidateID)
		if err != nil {
			return "", err
		}
		if available {
			return candidateID, nil
		}
	}
	return "", &ValidationError{Message: noAvailableAssistantMessage}
}

func (s *Service) assistantAvailable(ctx context.Context, assistantID string) (bool, error) {
	if available, ok := s.cachedAssistantAvailability(assistantID, time.Now().UTC()); ok {
		return available, nil
	}
	if s.assistantCache != nil {
		available, ok, err := s.assistantCache.GetAssistantAvailable(ctx, assistantID)
		if err == nil && ok {
			s.storeAssistantAvailability(assistantID, available, time.Now().UTC())
			return available, nil
		}
	}
	assistant, err := s.southbound.GetAssistant(ctx, assistantID)
	if err != nil {
		var httpErr *southbound.HTTPError
		if errors.As(err, &httpErr) && httpErr.StatusCode == http.StatusNotFound {
			if s.assistantCache != nil {
				_ = s.assistantCache.SetAssistantAvailable(ctx, assistantID, false)
			}
			return false, nil
		}
		return false, err
	}
	if strings.TrimSpace(assistant.AssistantID) == "" {
		s.storeAssistantAvailability(assistantID, false, time.Now().UTC())
		if s.assistantCache != nil {
			_ = s.assistantCache.SetAssistantAvailable(ctx, assistantID, false)
		}
		return false, nil
	}
	available := !assistant.Deleted && assistant.DeletedAt == nil
	s.storeAssistantAvailability(assistantID, available, time.Now().UTC())
	if s.assistantCache != nil {
		_ = s.assistantCache.SetAssistantAvailable(ctx, assistantID, available)
	}
	return available, nil
}

func (s *Service) cachedAssistantResolution(userID string, now time.Time) (string, bool) {
	userID = strings.TrimSpace(userID)
	if userID == "" {
		return "", false
	}
	s.resolveMu.Lock()
	defer s.resolveMu.Unlock()
	entry, ok := s.resolveCache[userID]
	if !ok || !now.Before(entry.expiresAt) || strings.TrimSpace(entry.assistantID) == "" {
		return "", false
	}
	return entry.assistantID, true
}

func (s *Service) storeAssistantResolution(userID string, assistantID string, now time.Time) {
	userID = strings.TrimSpace(userID)
	assistantID = strings.TrimSpace(assistantID)
	if userID == "" || assistantID == "" {
		return
	}
	s.resolveMu.Lock()
	defer s.resolveMu.Unlock()
	if s.resolveCache == nil {
		s.resolveCache = map[string]assistantResolutionCacheEntry{}
	}
	s.resolveCache[userID] = assistantResolutionCacheEntry{
		assistantID: assistantID,
		expiresAt:   now.Add(assistantResolutionCacheTTL),
	}
}

func (s *Service) beginAssistantResolution(userID string) (*assistantResolutionCall, bool) {
	userID = strings.TrimSpace(userID)
	s.resolveMu.Lock()
	defer s.resolveMu.Unlock()
	if s.resolveInflight == nil {
		s.resolveInflight = map[string]*assistantResolutionCall{}
	}
	if call, ok := s.resolveInflight[userID]; ok {
		return call, false
	}
	call := &assistantResolutionCall{done: make(chan struct{})}
	s.resolveInflight[userID] = call
	return call, true
}

func (s *Service) finishAssistantResolution(
	userID string,
	call *assistantResolutionCall,
	assistantID string,
	err error,
) {
	now := time.Now().UTC()
	s.resolveMu.Lock()
	if s.resolveInflight != nil {
		delete(s.resolveInflight, strings.TrimSpace(userID))
	}
	if err == nil && strings.TrimSpace(assistantID) != "" {
		if s.resolveCache == nil {
			s.resolveCache = map[string]assistantResolutionCacheEntry{}
		}
		s.resolveCache[strings.TrimSpace(userID)] = assistantResolutionCacheEntry{
			assistantID: strings.TrimSpace(assistantID),
			expiresAt:   now.Add(assistantResolutionCacheTTL),
		}
	}
	call.assistantID = assistantID
	call.err = err
	close(call.done)
	s.resolveMu.Unlock()
}

func (s *Service) checkChatRateLimit(ctx context.Context, request ChatRequest) error {
	if s.chatRateLimiter == nil {
		return nil
	}
	for _, key := range chatRateLimitKeys(request) {
		allowed, err := s.chatRateLimiter.Allow(ctx, key)
		if err != nil {
			return &QueueFullError{Message: "chat rate limit temporarily unavailable"}
		}
		if !allowed {
			return &QueueFullError{Message: "chat rate limit exceeded"}
		}
	}
	return nil
}

func chatRateLimitKeys(request ChatRequest) []string {
	keys := []string{"global"}
	key := strings.TrimSpace(request.ClientID)
	if key != "" && key != "default" {
		return append(keys, "client:"+key)
	}
	key = strings.TrimSpace(request.UserID)
	if key != "" {
		return append(keys, "user:"+key)
	}
	return append(keys, "anonymous")
}

func (s *Service) cachedAssistantAvailability(assistantID string, now time.Time) (bool, bool) {
	assistantID = strings.TrimSpace(assistantID)
	if assistantID == "" {
		return false, false
	}
	s.resolveMu.Lock()
	defer s.resolveMu.Unlock()
	entry, ok := s.availableCache[assistantID]
	if !ok || !now.Before(entry.expiresAt) {
		return false, false
	}
	return entry.available, true
}

func (s *Service) storeAssistantAvailability(assistantID string, available bool, now time.Time) {
	assistantID = strings.TrimSpace(assistantID)
	if assistantID == "" {
		return
	}
	s.resolveMu.Lock()
	defer s.resolveMu.Unlock()
	if s.availableCache == nil {
		s.availableCache = map[string]assistantAvailabilityCacheEntry{}
	}
	s.availableCache[assistantID] = assistantAvailabilityCacheEntry{
		available: available,
		expiresAt: now.Add(assistantAvailabilityCacheTTL),
	}
}

func sortUserAssistantBindings(bindings []southbound.UserAssistantBinding) {
	sort.SliceStable(bindings, func(i, j int) bool {
		left := bindings[i].BoundAt.Time
		right := bindings[j].BoundAt.Time
		if left.IsZero() {
			return false
		}
		if right.IsZero() {
			return true
		}
		return left.Before(right)
	})
}

func (s *Service) selectTargetAssistantID(
	ctx context.Context,
	sourceAssistantID string,
	preferredAssistantID string,
	requiredCapabilities []string,
) (string, error) {
	preferredAssistantID = strings.TrimSpace(preferredAssistantID)
	if s.scheduler == nil {
		if preferredAssistantID == "" {
			return "", &ValidationError{Message: "target_assistant_id is required in phase 1"}
		}
		return preferredAssistantID, nil
	}
	targetAssistantID, err := s.scheduler.SelectTarget(ctx, RuntimeScheduleRequest{
		PreferredAssistantID: preferredAssistantID,
		SourceAssistantID:    sourceAssistantID,
		RequiredCapabilities: requiredCapabilities,
	})
	if err != nil &&
		preferredAssistantID != "" &&
		s.runtimeRefresher != nil &&
		runtimeSchedulerErrorRefreshable(err) {
		if refreshErr := s.runtimeRefresher(ctx, preferredAssistantID); refreshErr == nil {
			targetAssistantID, err = s.scheduler.SelectTarget(ctx, RuntimeScheduleRequest{
				PreferredAssistantID: preferredAssistantID,
				SourceAssistantID:    sourceAssistantID,
				RequiredCapabilities: requiredCapabilities,
			})
		}
	}
	if err != nil {
		return "", &ValidationError{Message: err.Error()}
	}
	return strings.TrimSpace(targetAssistantID), nil
}

func runtimeSchedulerErrorRefreshable(err error) bool {
	if err == nil {
		return false
	}
	message := strings.ToLower(err.Error())
	return strings.Contains(message, "not registered") ||
		strings.Contains(message, "not ready") ||
		strings.Contains(message, "draining") ||
		strings.Contains(message, "missing_route") ||
		strings.Contains(message, "stale_heartbeat") ||
		strings.Contains(message, "unhealthy")
}

func (s *Service) reserveTarget(ctx context.Context, assistantID string) (RuntimeReservation, error) {
	assistantID = strings.TrimSpace(assistantID)
	if s.scheduler == nil {
		return RuntimeReservation{AssistantID: assistantID}, nil
	}
	if scheduler, ok := s.scheduler.(RuntimeRouteLeaseScheduler); ok {
		reservation, err := scheduler.ReserveRuntimeRoute(ctx, assistantID)
		if err != nil {
			return RuntimeReservation{}, &ValidationError{Message: err.Error()}
		}
		return reservation, nil
	}
	if err := s.scheduler.Reserve(ctx, assistantID); err != nil {
		return RuntimeReservation{}, &ValidationError{Message: err.Error()}
	}
	return RuntimeReservation{AssistantID: assistantID}, nil
}

func (s *Service) releaseRuntimeReservation(ctx context.Context, reservation RuntimeReservation) {
	if s.scheduler == nil {
		return
	}
	if scheduler, ok := s.scheduler.(RuntimeRouteLeaseScheduler); ok {
		scheduler.ReleaseRuntimeRoute(ctx, reservation)
		return
	}
	s.scheduler.Release(ctx, reservation.AssistantID)
}

func (s *Service) renewRuntimeReservation(ctx context.Context, reservation RuntimeReservation) RuntimeReservation {
	if s.scheduler == nil {
		return reservation
	}
	scheduler, ok := s.scheduler.(RuntimeRouteLeaseScheduler)
	if !ok {
		return reservation
	}
	renewed, err := scheduler.RenewRuntimeRouteLease(ctx, reservation)
	if err != nil {
		s.logWarn(
			"gateway_runtime_route_lease_renew_failed",
			"assistant_id", reservation.AssistantID,
			"runtime_id", reservation.RuntimeID,
			"lease_id", reservation.LeaseID,
			"error", err,
		)
		return reservation
	}
	return renewed
}

func (s *Service) releaseTarget(ctx context.Context, assistantID string) {
	if s.scheduler == nil {
		return
	}
	s.scheduler.Release(ctx, assistantID)
}

func (s *Service) resolveRuntimeRoute(
	ctx context.Context,
	assistantID string,
) (RuntimeRoute, bool, error) {
	if s.runtimeRouteResolver == nil {
		return RuntimeRoute{}, false, nil
	}
	return s.runtimeRouteResolver.ResolveRuntimeRoute(ctx, assistantID)
}

func runtimeRouteHasGateway(route RuntimeRoute) bool {
	return strings.TrimSpace(route.InternalBaseURL) != "" || strings.TrimSpace(route.GatewayURL) != ""
}

func runtimeViewFromAssistant(assistant southbound.Assistant) RuntimeView {
	return RuntimeView{
		AssistantID:   assistant.AssistantID,
		Name:          assistant.Name,
		Status:        assistant.Runtime.Status,
		ContainerName: assistant.Runtime.ContainerName,
		ContainerID:   assistant.Runtime.ContainerID,
		WorkspaceRoot: assistant.Runtime.WorkspaceRoot,
		StateRoot:     assistant.Runtime.StateRoot,
		Image:         assistant.Runtime.Image,
		GatewayHost:   assistant.Runtime.GatewayHost,
		GatewayPort:   assistant.Runtime.GatewayPort,
		GatewayURL:    assistant.Runtime.GatewayURL,
		LastError:     assistant.Runtime.LastError,
		UpdatedAt:     southboundTimePtr(assistant.Runtime.UpdatedAt),
	}
}

func (s *Service) createSharedObjectFromWorkspaceFile(
	ctx context.Context,
	sourceAssistantID string,
	request ChatRequest,
) (string, error) {
	file := request.WorkspaceFile
	if file == nil {
		return "", &ValidationError{Message: "workspace_file is required"}
	}
	if strings.TrimSpace(file.FileID) == "" {
		return "", &ValidationError{Message: "workspace_file.file_id is required"}
	}
	if strings.TrimSpace(file.Filename) == "" {
		return "", &ValidationError{Message: "workspace_file.filename is required"}
	}

	createdBy := strings.TrimSpace(request.CreatedBy)
	if createdBy == "" {
		createdBy = strings.TrimSpace(request.UserID)
	}
	if createdBy == "" {
		createdBy = "go-gateway"
	}

	objectType := strings.TrimSpace(file.ObjectType)
	if objectType == "" {
		objectType = "document"
	}

	obj, err := s.southbound.CreateSharedObject(ctx, southbound.SharedObjectCreateRequest{
		SourceAssistantID:     sourceAssistantID,
		Filename:              strings.TrimSpace(file.Filename),
		ObjectType:            objectType,
		ContentType:           strings.TrimSpace(file.ContentType),
		SizeBytes:             file.SizeBytes,
		SHA256:                strings.TrimSpace(file.SHA256),
		SourceWorkspaceFileID: strings.TrimSpace(file.FileID),
		CreatedBy:             createdBy,
		IdempotencyKey:        sharedObjectIdempotencyKey(request, "workspace", strings.TrimSpace(file.FileID)),
		MetaJSON:              sharedObjectMeta(request, map[string]any{"source": "workspace_file"}),
	})
	if err != nil {
		s.logWarn(
			"gateway_shared_object_create_failed",
			append(
				[]any{
					"source", "workspace_file",
					"source_assistant_id", strings.TrimSpace(sourceAssistantID),
					"source_workspace_file_id", strings.TrimSpace(file.FileID),
					"filename", strings.TrimSpace(file.Filename),
					"size_bytes", file.SizeBytes,
					"error", err,
				},
				chatRequestLogAttrs(request)...,
			)...,
		)
		return "", err
	}
	s.logInfo(
		"gateway_shared_object_created",
		append(
			[]any{
				"source", "workspace_file",
				"shared_object_id", obj.ID,
				"source_assistant_id", strings.TrimSpace(sourceAssistantID),
				"source_workspace_file_id", strings.TrimSpace(file.FileID),
				"filename", strings.TrimSpace(file.Filename),
				"size_bytes", file.SizeBytes,
			},
			chatRequestLogAttrs(request)...,
		)...,
	)
	return obj.ID, nil
}

func (s *Service) createSharedObjectFromMessage(
	ctx context.Context,
	sourceAssistantID string,
	request ChatRequest,
) (string, error) {
	content := strings.TrimSpace(request.Message)
	if content == "" {
		content = strings.TrimSpace(request.Instruction)
	}
	if content == "" {
		return "", &ValidationError{Message: "instruction or message is required"}
	}
	createdBy := strings.TrimSpace(request.CreatedBy)
	if createdBy == "" {
		createdBy = strings.TrimSpace(request.UserID)
	}
	if createdBy == "" {
		createdBy = "go-gateway"
	}
	sum := sha256.Sum256([]byte(content))
	obj, err := s.southbound.CreateSharedObject(ctx, southbound.SharedObjectCreateRequest{
		SourceAssistantID: sourceAssistantID,
		Filename:          "chat-message.txt",
		ObjectType:        "document",
		ContentType:       "text/plain",
		SizeBytes:         int64(len([]byte(content))),
		SHA256:            hex.EncodeToString(sum[:]),
		CreatedBy:         createdBy,
		IdempotencyKey:    sharedObjectIdempotencyKey(request, "message", hex.EncodeToString(sum[:])),
		MetaJSON:          sharedObjectMeta(request, map[string]any{"source": "message"}),
	})
	if err != nil {
		s.logWarn(
			"gateway_shared_object_create_failed",
			append(
				[]any{
					"source", "message",
					"source_assistant_id", strings.TrimSpace(sourceAssistantID),
					"size_bytes", int64(len([]byte(content))),
					"error", err,
				},
				chatRequestLogAttrs(request)...,
			)...,
		)
		return "", err
	}
	s.logInfo(
		"gateway_shared_object_created",
		append(
			[]any{
				"source", "message",
				"shared_object_id", obj.ID,
				"source_assistant_id", strings.TrimSpace(sourceAssistantID),
				"size_bytes", int64(len([]byte(content))),
			},
			chatRequestLogAttrs(request)...,
		)...,
	)
	return obj.ID, nil
}

func sharedObjectIdempotencyKey(request ChatRequest, kind string, seed string) string {
	base := firstNonEmptyString(request.IdempotencyKey, request.RequestID, request.ConversationID)
	if base == "" {
		return ""
	}
	base = sanitizeExternalAssistantID(base, 80)
	kind = sanitizeExternalAssistantID(kind, 24)
	sum := sha256.Sum256([]byte(kind + ":" + strings.TrimSpace(seed)))
	return base + "-" + kind + "-" + hex.EncodeToString(sum[:])[:16]
}

func runtimeReplyObjectIdempotencyKey(taskID string, contentHash string) string {
	taskID = sanitizeExternalAssistantID(taskID, 80)
	if taskID == "" {
		return ""
	}
	sum := sha256.Sum256([]byte("runtime_reply:" + strings.TrimSpace(contentHash)))
	return taskID + "-runtime-reply-" + hex.EncodeToString(sum[:])[:16]
}

func sharedObjectMeta(request ChatRequest, extra map[string]any) map[string]any {
	meta := map[string]any{}
	for key, value := range request.Metadata {
		key = strings.TrimSpace(key)
		value = strings.TrimSpace(value)
		if key == "" || value == "" || sensitiveMetadataKey(key) {
			continue
		}
		if len(key) > 64 {
			key = strings.TrimSpace(key[:64])
		}
		if len(value) > 512 {
			value = strings.TrimSpace(value[:512])
		}
		if key != "" && value != "" {
			meta[key] = value
		}
	}
	setSharedObjectMetaString(meta, "request_id", request.RequestID)
	setSharedObjectMetaString(meta, "conversation_id", request.ConversationID)
	setSharedObjectMetaString(meta, "user_id", request.UserID)
	setSharedObjectMetaString(meta, "employee_no", request.EmployeeNo)
	setSharedObjectMetaString(meta, "client_id", request.ClientID)
	setSharedObjectMetaString(meta, "idempotency_key", request.IdempotencyKey)
	for key, value := range extra {
		key = strings.TrimSpace(key)
		if key == "" || sensitiveMetadataKey(key) || value == nil {
			continue
		}
		meta[key] = value
	}
	return meta
}

func setSharedObjectMetaString(meta map[string]any, key string, value string) {
	value = strings.TrimSpace(value)
	if value == "" || sensitiveMetadataKey(key) {
		return
	}
	if len(value) > 512 {
		value = strings.TrimSpace(value[:512])
	}
	if value != "" {
		meta[key] = value
	}
}

func sensitiveMetadataKey(key string) bool {
	key = strings.ToLower(strings.TrimSpace(key))
	return strings.Contains(key, "token") ||
		strings.Contains(key, "secret") ||
		strings.Contains(key, "password") ||
		strings.Contains(key, "authorization") ||
		strings.Contains(key, "cookie")
}

func workspaceFileFromFileIDs(files []string) *WorkspaceFileRef {
	refs := workspaceFileRefsFromFileIDs(files)
	if len(refs) == 0 {
		return nil
	}
	return &refs[0]
}

func workspaceFileRefsFromRequest(request ChatRequest) []WorkspaceFileRef {
	if len(request.WorkspaceFiles) > 0 {
		return normalizeWorkspaceFileRefs(request.WorkspaceFiles)
	}
	if request.WorkspaceFile != nil {
		if ref, ok := normalizeWorkspaceFileRef(*request.WorkspaceFile); ok {
			return []WorkspaceFileRef{ref}
		}
	}
	return workspaceFileRefsFromFileIDs(request.Files)
}

func workspaceFileRefsFromFileIDs(files []string) []WorkspaceFileRef {
	refs := make([]WorkspaceFileRef, 0, len(files))
	for _, fileID := range files {
		fileID = strings.TrimSpace(fileID)
		if fileID == "" {
			continue
		}
		refs = append(refs, WorkspaceFileRef{
			FileID:     fileID,
			Filename:   fileID,
			ObjectType: "document",
		})
	}
	return refs
}

func normalizeWorkspaceFileRefs(files []WorkspaceFileRef) []WorkspaceFileRef {
	refs := make([]WorkspaceFileRef, 0, len(files))
	for _, file := range files {
		if ref, ok := normalizeWorkspaceFileRef(file); ok {
			refs = append(refs, ref)
		}
	}
	return refs
}

func normalizeWorkspaceFileRef(file WorkspaceFileRef) (WorkspaceFileRef, bool) {
	file.FileID = strings.TrimSpace(file.FileID)
	if file.FileID == "" {
		return WorkspaceFileRef{}, false
	}
	file.Filename = cleanWorkspaceLookupText(firstNonEmptyString(file.Filename, file.FileID))
	file.ObjectType = strings.TrimSpace(file.ObjectType)
	if file.ObjectType == "" {
		file.ObjectType = "document"
	}
	file.ContentType = strings.TrimSpace(file.ContentType)
	file.SHA256 = strings.TrimSpace(file.SHA256)
	file.Bucket = strings.TrimSpace(file.Bucket)
	file.RelativePath = cleanWorkspaceLookupPathSuffix(file.RelativePath)
	return file, true
}

func withWorkspaceFileLookupGuidance(instruction string, files []WorkspaceFileRef) string {
	refs := normalizeWorkspaceFileRefs(files)
	if len(refs) == 0 {
		return strings.TrimSpace(instruction)
	}
	var builder strings.Builder
	builder.WriteString(strings.TrimSpace(instruction))
	builder.WriteString("\n\n[Workspace file context]\n")
	builder.WriteString("When scanning, searching, or reading workspace files, check /workspace/temp first, then /workspace/persistent if the file is not found in temp. When a referenced file is named without an explicit /workspace/temp/ or /workspace/persistent/ path, apply the same temp-first lookup order. If the user gives an explicit workspace path, use that path.\n")
	builder.WriteString("Referenced files:\n")
	for index, file := range refs {
		builder.WriteString(fmt.Sprintf(
			"%d. file_id=%s filename=%s preferred_path=%s fallback_path=%s\n",
			index+1,
			cleanWorkspaceLookupText(file.FileID),
			cleanWorkspaceLookupText(file.Filename),
			workspaceLookupPath(file, "temp"),
			workspaceLookupPath(file, "persistent"),
		))
	}
	return strings.TrimRight(builder.String(), "\n")
}

func workspaceLookupPath(file WorkspaceFileRef, bucket string) string {
	suffix := file.RelativePath
	if suffix == "" {
		suffix = file.Filename
	}
	suffix = stripWorkspaceBucketPrefix(cleanWorkspaceLookupPathSuffix(suffix))
	if suffix == "" {
		suffix = cleanWorkspaceLookupText(firstNonEmptyString(file.Filename, file.FileID))
	}
	return "/workspace/" + bucket + "/" + suffix
}

func stripWorkspaceBucketPrefix(path string) string {
	path = cleanWorkspaceLookupPathSuffix(path)
	for _, bucket := range []string{"temp", "persistent", "skills", "plugins"} {
		if path == bucket {
			return ""
		}
		prefix := bucket + "/"
		if strings.HasPrefix(path, prefix) {
			return strings.TrimPrefix(path, prefix)
		}
	}
	return path
}

func cleanWorkspaceLookupPathSuffix(value string) string {
	value = strings.ReplaceAll(strings.TrimSpace(value), "\\", "/")
	if value == "" {
		return ""
	}
	parts := strings.Split(value, "/")
	cleaned := make([]string, 0, len(parts))
	for _, part := range parts {
		part = cleanWorkspaceLookupText(part)
		if part == "" || part == "." || part == ".." {
			continue
		}
		cleaned = append(cleaned, part)
	}
	return strings.Join(cleaned, "/")
}

func cleanWorkspaceLookupText(value string) string {
	value = strings.TrimSpace(value)
	value = strings.ReplaceAll(value, "\r", " ")
	value = strings.ReplaceAll(value, "\n", " ")
	value = strings.ReplaceAll(value, "\t", " ")
	return strings.Join(strings.Fields(value), " ")
}

func (s *Service) enrichTask(ctx context.Context, task southbound.Task) (TaskView, error) {
	view := TaskView{
		ID:                    task.ID,
		RootTaskID:            task.RootTaskID,
		ParentTaskID:          task.ParentTaskID,
		Depth:                 task.Depth,
		MaxDepth:              task.MaxDepth,
		SourceAssistantID:     task.SourceAssistantID,
		TargetAssistantID:     task.TargetAssistantID,
		InputObjectID:         task.InputObjectID,
		Instruction:           task.Instruction,
		Status:                task.Status,
		ApprovalMode:          task.ApprovalMode,
		ApprovalID:            task.ApprovalID,
		ResultObjectID:        task.ResultObjectID,
		ResultWorkspaceFileID: task.ResultWorkspaceFileID,
		IdempotencyKey:        task.IdempotencyKey,
		BudgetLimit:           task.BudgetLimit,
		TimeoutSeconds:        task.TimeoutSeconds,
		CreatedBy:             task.CreatedBy,
		CreatedAt:             task.CreatedAt.Time,
		AcceptedAt:            southboundTimePtr(task.AcceptedAt),
		StartedAt:             southboundTimePtr(task.StartedAt),
		CompletedAt:           southboundTimePtr(task.CompletedAt),
		FailedAt:              southboundTimePtr(task.FailedAt),
		FailedReasonCode:      task.FailedReasonCode,
		FailedReasonText:      task.FailedReasonText,
		Metadata:              task.MetaJSON,
	}

	if strings.TrimSpace(task.ApprovalID) == "" {
		return view, nil
	}

	approval, err := s.southbound.GetApproval(ctx, task.ApprovalID)
	if err != nil {
		return TaskView{}, err
	}

	approvalView := approvalViewFromSouthbound(approval)
	view.Approval = &approvalView
	return view, nil
}

func approvalViewFromSouthbound(approval southbound.ApprovalStatus) ApprovalView {
	return ApprovalView{
		ApprovalID:    approval.ApprovalID,
		AssistantID:   approval.AssistantID,
		Status:        approval.Status,
		Resolution:    approval.Resolution,
		ApprovalScope: approval.ApprovalScope,
		Reason:        approval.Reason,
		Target:        approval.Target,
		RequestedAt:   southboundTimePtr(approval.RequestedAt),
		ExpiresAt:     southboundTimePtr(approval.ExpiresAt),
		ResolvedAt:    southboundTimePtr(approval.ResolvedAt),
	}
}

func southboundTimePtr(value *southbound.Time) *time.Time {
	if value == nil {
		return nil
	}
	converted := value.Time
	return &converted
}

func southboundApprovalResolution(decision string) (string, error) {
	switch strings.ToLower(strings.TrimSpace(decision)) {
	case "approve", "allow":
		return "allow", nil
	case "deny", "reject":
		return "deny", nil
	default:
		return "", &ValidationError{Message: "decision must be approve or deny"}
	}
}
