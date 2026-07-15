package core

import (
	"io"
	"strconv"
	"strings"
	"time"
)

type ChatRequest struct {
	ClientID             string             `json:"client_id,omitempty"`
	UserID               string             `json:"user_id"`
	EmployeeNo           string             `json:"employee_no"`
	RequestID            string             `json:"request_id"`
	ConversationID       string             `json:"conversation_id"`
	Files                []string           `json:"files,omitempty"`
	RequiredCapabilities []string           `json:"required_capabilities,omitempty"`
	SourceAssistantID    string             `json:"source_assistant_id"`
	TargetAssistantID    string             `json:"target_assistant_id"`
	InputObjectID        string             `json:"input_object_id"`
	Message              string             `json:"message"`
	Instruction          string             `json:"instruction"`
	ApprovalMode         string             `json:"approval_mode"`
	IdempotencyKey       string             `json:"idempotency_key"`
	CallbackURL          string             `json:"callback_url"`
	RequiresResultFile   bool               `json:"requires_result_file,omitempty"`
	CreatedBy            string             `json:"created_by"`
	TimeoutSeconds       int                `json:"timeout_seconds"`
	Metadata             map[string]string  `json:"metadata,omitempty"`
	WorkspaceFile        *WorkspaceFileRef  `json:"workspace_file,omitempty"`
	WorkspaceFiles       []WorkspaceFileRef `json:"workspace_files,omitempty"`
}

type HealthReport struct {
	Status     string                     `json:"status"`
	Ready      bool                       `json:"ready"`
	Components map[string]string          `json:"components"`
	Details    map[string]HealthComponent `json:"details,omitempty"`
	Timestamp  time.Time                  `json:"timestamp"`
}

type HealthComponent struct {
	Status  string `json:"status"`
	Message string `json:"message,omitempty"`
}

func NewHealthReport() HealthReport {
	now := time.Now().UTC()
	return HealthReport{
		Status:     "healthy",
		Ready:      true,
		Components: make(map[string]string),
		Details:    make(map[string]HealthComponent),
		Timestamp:  now,
	}
}

func (h *HealthReport) SetComponent(name string, status string, message string) {
	if h.Components == nil {
		h.Components = make(map[string]string)
	}
	if h.Details == nil {
		h.Details = make(map[string]HealthComponent)
	}
	h.Components[name] = status
	h.Details[name] = HealthComponent{Status: status, Message: message}
}

func (h HealthReport) Finalize() HealthReport {
	h.Ready = true
	h.Status = "healthy"
	for _, status := range h.Components {
		switch status {
		case "down":
			h.Ready = false
			h.Status = "unhealthy"
			return h
		case "degraded":
			if h.Status == "healthy" {
				h.Status = "degraded"
			}
		}
	}
	return h
}

type WorkspaceFileRef struct {
	FileID       string `json:"file_id"`
	Filename     string `json:"filename"`
	ObjectType   string `json:"object_type"`
	ContentType  string `json:"content_type"`
	SizeBytes    int64  `json:"size_bytes"`
	SHA256       string `json:"sha256"`
	Bucket       string `json:"bucket,omitempty"`
	RelativePath string `json:"relative_path,omitempty"`
}

type TaskView struct {
	ID                    string         `json:"id"`
	RootTaskID            string         `json:"root_task_id,omitempty"`
	ParentTaskID          string         `json:"parent_task_id,omitempty"`
	Depth                 int            `json:"depth"`
	MaxDepth              int            `json:"max_depth"`
	SourceAssistantID     string         `json:"source_assistant_id"`
	TargetAssistantID     string         `json:"target_assistant_id"`
	InputObjectID         string         `json:"input_object_id"`
	Instruction           string         `json:"instruction"`
	Status                string         `json:"status"`
	ApprovalMode          string         `json:"approval_mode"`
	ApprovalID            string         `json:"approval_id,omitempty"`
	ResultObjectID        string         `json:"result_object_id,omitempty"`
	ResultWorkspaceFileID string         `json:"result_workspace_file_id,omitempty"`
	IdempotencyKey        string         `json:"idempotency_key,omitempty"`
	BudgetLimit           *int           `json:"budget_limit,omitempty"`
	TimeoutSeconds        int            `json:"timeout_seconds"`
	CreatedBy             string         `json:"created_by"`
	CreatedAt             time.Time      `json:"created_at"`
	AcceptedAt            *time.Time     `json:"accepted_at,omitempty"`
	StartedAt             *time.Time     `json:"started_at,omitempty"`
	CompletedAt           *time.Time     `json:"completed_at,omitempty"`
	FailedAt              *time.Time     `json:"failed_at,omitempty"`
	FailedReasonCode      string         `json:"failed_reason_code,omitempty"`
	FailedReasonText      string         `json:"failed_reason_text,omitempty"`
	Metadata              map[string]any `json:"meta_json,omitempty"`
	Approval              *ApprovalView  `json:"approval,omitempty"`
	IdempotencyReplayed   bool           `json:"-"`
}

const (
	taskMetadataRuntimeResultWorkspaceFileIDs = "runtime_result_workspace_file_ids"
	taskMetadataRuntimeResultFileSyncError    = "runtime_result_file_sync_error"
	taskMetadataRuntimeResultFileCount        = "runtime_result_file_count"
	taskMetadataRuntimeResultTaskID           = "runtime_result_task_id"
	taskMetadataRuntimeResultPathPrefix       = "runtime_result_path_prefix"
	taskMetadataRuntimeRawReplyText           = "runtime_raw_reply_text"
	taskMetadataRequiresResultFile            = "requires_result_file"
)

type ApprovalView struct {
	ApprovalID    string     `json:"approval_id"`
	AssistantID   string     `json:"assistant_id,omitempty"`
	Status        string     `json:"status"`
	Resolution    string     `json:"resolution,omitempty"`
	ApprovalScope string     `json:"approval_scope,omitempty"`
	Reason        string     `json:"reason,omitempty"`
	Target        string     `json:"target,omitempty"`
	RequestedAt   *time.Time `json:"requested_at,omitempty"`
	ExpiresAt     *time.Time `json:"expires_at,omitempty"`
	ResolvedAt    *time.Time `json:"resolved_at,omitempty"`
}

type ChatResponse struct {
	TaskID           string    `json:"task_id"`
	Status           string    `json:"status"`
	Stage            string    `json:"stage"`
	FallbackQueryURL string    `json:"-"`
	CreatedAt        time.Time `json:"created_at"`
}

func NewChatResponse(task TaskView) ChatResponse {
	return ChatResponse{
		TaskID:    task.ID,
		Status:    publicTaskStatus(task.Status),
		Stage:     publicTaskStage(task.Status),
		CreatedAt: task.CreatedAt,
	}
}

type TaskResponse struct {
	TaskID           string                `json:"task_id"`
	Status           string                `json:"status"`
	Stage            string                `json:"stage"`
	ReplyText        string                `json:"reply_text,omitempty"`
	Files            []TaskFileResponse    `json:"files,omitempty"`
	ResultFileID     string                `json:"-"`
	ErrorCode        string                `json:"error_code,omitempty"`
	ErrorMessage     string                `json:"error_message,omitempty"`
	ErrorLocation    string                `json:"error_location,omitempty"`
	Reason           string                `json:"reason,omitempty"`
	Approval         *TaskApprovalResponse `json:"-"`
	Failure          *TaskFailureResponse  `json:"-"`
	FallbackQueryURL string                `json:"-"`
	CreatedAt        time.Time             `json:"created_at"`
	UpdatedAt        *time.Time            `json:"updated_at,omitempty"`
}

type TaskFileResponse struct {
	FileID      string `json:"file_id"`
	Filename    string `json:"filename,omitempty"`
	Size        int64  `json:"size,omitempty"`
	MimeType    string `json:"mime_type,omitempty"`
	DownloadURL string `json:"download_url,omitempty"`
}

type PublicTaskResponse struct {
	TaskID        string                   `json:"taskId"`
	Status        string                   `json:"status"`
	Stage         string                   `json:"stage"`
	EventID       string                   `json:"eventId,omitempty"`
	EventSeq      int64                    `json:"eventSeq,omitempty"`
	EventType     string                   `json:"eventType,omitempty"`
	Timestamp     string                   `json:"timestamp,omitempty"`
	Nonce         string                   `json:"nonce,omitempty"`
	Signature     string                   `json:"signature,omitempty"`
	ReplyText     string                   `json:"replyText"`
	Files         []PublicTaskFileResponse `json:"files,omitempty"`
	ErrorCode     string                   `json:"errorCode,omitempty"`
	ErrorMessage  string                   `json:"errorMessage,omitempty"`
	ErrorLocation string                   `json:"errorLocation,omitempty"`
	ErrorReason   string                   `json:"errorReason,omitempty"`
}

type PublicTaskFileResponse struct {
	FileID      string `json:"fileId"`
	FileName    string `json:"fileName,omitempty"`
	Size        string `json:"size,omitempty"`
	MimeType    string `json:"mimeType,omitempty"`
	DownloadURL string `json:"download_url,omitempty"`
}

type PublicTaskPayloadInput struct {
	TaskID        string
	Status        string
	Stage         string
	EventID       string
	EventSeq      int64
	EventType     string
	Timestamp     string
	Nonce         string
	Signature     string
	ReplyText     string
	Files         []PublicTaskFileResponse
	ErrorCode     string
	ErrorMessage  string
	ErrorLocation string
	ErrorReason   string
}

type TaskApprovalResponse struct {
	ApprovalID string `json:"approval_id"`
	Status     string `json:"status"`
	Resolution string `json:"resolution,omitempty"`
	Reason     string `json:"reason,omitempty"`
}

type TaskFailureResponse struct {
	Code string `json:"code,omitempty"`
	Text string `json:"text,omitempty"`
}

func NewTaskResponse(task TaskView) TaskResponse {
	resultFileIDs := taskResultWorkspaceFileIDs(task)
	resultFileID := strings.TrimSpace(task.ResultWorkspaceFileID)
	if resultFileID == "" && len(resultFileIDs) > 0 {
		resultFileID = resultFileIDs[0]
	}
	response := TaskResponse{
		TaskID:       task.ID,
		Status:       publicTaskStatus(task.Status),
		Stage:        publicTaskStage(task.Status),
		ResultFileID: resultFileID,
		CreatedAt:    task.CreatedAt,
		UpdatedAt:    taskUpdatedAt(task),
	}
	for _, fileID := range resultFileIDs {
		response.Files = append(response.Files, TaskFileResponse{FileID: fileID})
	}
	if task.Metadata != nil {
		if reply, ok := task.Metadata["runtime_reply_text"].(string); ok {
			response.ReplyText = reply
		}
		if location, ok := task.Metadata["queue_error_location"].(string); ok {
			response.ErrorLocation = strings.TrimSpace(location)
		}
	}
	if task.Approval != nil {
		response.Approval = &TaskApprovalResponse{
			ApprovalID: task.Approval.ApprovalID,
			Status:     task.Approval.Status,
			Resolution: task.Approval.Resolution,
			Reason:     task.Approval.Reason,
		}
	}
	if task.FailedReasonCode != "" || task.FailedReasonText != "" {
		response.Failure = &TaskFailureResponse{
			Code: task.FailedReasonCode,
			Text: task.FailedReasonText,
		}
		response.ErrorCode = task.FailedReasonCode
		response.ErrorMessage = task.FailedReasonText
	}
	if response.Status == "rejected" && task.Approval != nil {
		response.Reason = task.Approval.Reason
	}
	if response.Status == "cancelled" && response.ErrorMessage != "" {
		response.Reason = response.ErrorMessage
	}
	return response
}

func taskResultWorkspaceFileIDs(task TaskView) []string {
	out := make([]string, 0, 1)
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
	appendID(task.ResultWorkspaceFileID)
	for _, value := range metadataStringList(task.Metadata, taskMetadataRuntimeResultWorkspaceFileIDs) {
		appendID(value)
	}
	return out
}

func metadataStringList(metadata map[string]any, key string) []string {
	if len(metadata) == 0 {
		return nil
	}
	value, ok := metadata[key]
	if !ok {
		return nil
	}
	switch typed := value.(type) {
	case []string:
		return typed
	case []any:
		out := make([]string, 0, len(typed))
		for _, item := range typed {
			if text, ok := item.(string); ok {
				out = append(out, text)
			}
		}
		return out
	case string:
		if strings.TrimSpace(typed) == "" {
			return nil
		}
		return []string{typed}
	default:
		return nil
	}
}

func NewPublicTaskResponse(task TaskView) PublicTaskResponse {
	response := NewTaskResponse(task)
	return NewPublicTaskPayload(PublicTaskPayloadInput{
		TaskID:        response.TaskID,
		Status:        response.Status,
		Stage:         response.Stage,
		ReplyText:     response.ReplyText,
		Files:         PublicTaskFilesFromTask(response.Files),
		ErrorCode:     response.ErrorCode,
		ErrorMessage:  response.ErrorMessage,
		ErrorLocation: response.ErrorLocation,
		ErrorReason:   firstNonEmptyString(response.Reason, response.ErrorMessage),
	})
}

func NewPublicTaskPayload(input PublicTaskPayloadInput) PublicTaskResponse {
	errorReason := firstNonEmptyString(input.ErrorReason, input.ErrorMessage)
	return PublicTaskResponse{
		TaskID:        input.TaskID,
		Status:        input.Status,
		Stage:         input.Stage,
		EventID:       input.EventID,
		EventSeq:      input.EventSeq,
		EventType:     input.EventType,
		Timestamp:     input.Timestamp,
		Nonce:         input.Nonce,
		Signature:     input.Signature,
		ReplyText:     input.ReplyText,
		Files:         input.Files,
		ErrorCode:     input.ErrorCode,
		ErrorMessage:  input.ErrorMessage,
		ErrorLocation: strings.TrimSpace(input.ErrorLocation),
		ErrorReason:   errorReason,
	}
}

func PublicTaskFilesFromTask(files []TaskFileResponse) []PublicTaskFileResponse {
	if len(files) == 0 {
		return nil
	}
	out := make([]PublicTaskFileResponse, 0, len(files))
	for _, file := range files {
		if strings.TrimSpace(file.FileID) == "" {
			continue
		}
		item := PublicTaskFileResponse{
			FileID:      file.FileID,
			FileName:    file.Filename,
			MimeType:    file.MimeType,
			DownloadURL: file.DownloadURL,
		}
		if file.Size > 0 {
			item.Size = strconv.FormatInt(file.Size, 10)
		}
		out = append(out, item)
	}
	return out
}

func firstNonEmptyString(values ...string) string {
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value != "" {
			return value
		}
	}
	return ""
}

func publicTaskStatus(status string) string {
	switch status {
	case "pending_approval":
		return "pending_approval"
	case "completed":
		return "completed"
	case "rejected":
		return "rejected"
	case "failed":
		return "failed"
	case "expired", "timeout":
		return "timeout"
	case "cancelled":
		return "cancelled"
	case "pending_claim", "claimed", "running":
		return "processing"
	default:
		return status
	}
}

func taskUpdatedAt(task TaskView) *time.Time {
	switch {
	case task.CompletedAt != nil:
		return task.CompletedAt
	case task.FailedAt != nil:
		return task.FailedAt
	case task.StartedAt != nil:
		return task.StartedAt
	case task.AcceptedAt != nil:
		return task.AcceptedAt
	default:
		return &task.CreatedAt
	}
}

func publicTaskStage(status string) string {
	switch status {
	case "pending_approval":
		return "waiting_approval"
	case "pending_claim", "claimed":
		return "queued"
	case "running":
		return "tool_running"
	case "completed":
		return "done"
	case "failed", "expired", "timeout":
		return "error"
	case "cancelled", "rejected":
		return "blocked"
	default:
		return ""
	}
}

type ApprovalResolutionRequest struct {
	Resolution string `json:"resolution"`
}

type ApprovalDecisionRequest struct {
	Decision string `json:"decision"`
	Comment  string `json:"comment,omitempty"`
}

type ApprovalResponse struct {
	ApprovalID  string     `json:"approval_id"`
	AssistantID string     `json:"assistant_id,omitempty"`
	TaskID      string     `json:"task_id,omitempty"`
	Status      string     `json:"status"`
	Decision    string     `json:"decision,omitempty"`
	Reason      string     `json:"reason,omitempty"`
	Action      string     `json:"action,omitempty"`
	Resource    string     `json:"resource,omitempty"`
	RequestedAt *time.Time `json:"requested_at,omitempty"`
	ExpiresAt   *time.Time `json:"expires_at,omitempty"`
	ResolvedAt  *time.Time `json:"resolved_at,omitempty"`
}

type ApprovalDecisionResponse struct {
	Approval ApprovalResponse `json:"approval"`
	Task     *TaskResponse    `json:"task,omitempty"`
}

func NewApprovalResponse(approval ApprovalView, taskID string) ApprovalResponse {
	return ApprovalResponse{
		ApprovalID:  approval.ApprovalID,
		AssistantID: approval.AssistantID,
		TaskID:      taskID,
		Status:      publicApprovalStatus(approval.Status),
		Decision:    publicApprovalDecision(approval.Resolution),
		Reason:      approval.Reason,
		Action:      approval.ApprovalScope,
		Resource:    approval.Target,
		RequestedAt: approval.RequestedAt,
		ExpiresAt:   approval.ExpiresAt,
		ResolvedAt:  approval.ResolvedAt,
	}
}

func publicApprovalStatus(status string) string {
	switch status {
	case "approved":
		return "approved"
	case "denied":
		return "denied"
	default:
		return status
	}
}

func publicApprovalDecision(resolution string) string {
	switch resolution {
	case "allow":
		return "approve"
	case "deny":
		return "deny"
	default:
		return resolution
	}
}

type RuntimeRequest struct {
	AssistantID string `json:"assistant_id"`
	UserID      string `json:"user_id"`
}

type RuntimeView struct {
	AssistantID   string     `json:"assistant_id"`
	Name          string     `json:"name,omitempty"`
	Status        string     `json:"status"`
	ContainerName string     `json:"container_name,omitempty"`
	ContainerID   string     `json:"container_id,omitempty"`
	WorkspaceRoot string     `json:"workspace_root,omitempty"`
	StateRoot     string     `json:"state_root,omitempty"`
	Image         string     `json:"image,omitempty"`
	GatewayHost   string     `json:"gateway_host,omitempty"`
	GatewayPort   int        `json:"gateway_port,omitempty"`
	GatewayURL    string     `json:"gateway_url,omitempty"`
	LastError     string     `json:"last_error,omitempty"`
	UpdatedAt     *time.Time `json:"updated_at,omitempty"`
}

type RuntimeRoute struct {
	RuntimeID       string
	AssistantID     string
	PodName         string
	InternalBaseURL string
	GatewayURL      string
	Status          string
	LastHeartbeatAt *time.Time
	UpdatedAt       time.Time
}

type RuntimeReservation struct {
	LeaseID        string
	RuntimeID      string
	AssistantID    string
	Route          RuntimeRoute
	LeaseExpiresAt *time.Time
}

type ControlUILaunchSession struct {
	AssistantID  string
	GatewayURL   string
	GatewayToken string
	SessionKey   string
	ExpiresAt    time.Time
}

type CancelTaskRequest struct {
	ActorID string `json:"actor_id"`
	Reason  string `json:"reason"`
}

type CompleteTaskRequest struct {
	AssistantID            string   `json:"assistant_id"`
	ClaimToken             string   `json:"claim_token"`
	ResultObjectID         string   `json:"result_object_id"`
	ResultWorkspaceFileID  string   `json:"result_workspace_file_id"`
	ResultWorkspaceFileIDs []string `json:"result_workspace_file_ids,omitempty"`
}

type FailTaskRequest struct {
	AssistantID      string `json:"assistant_id"`
	ClaimToken       string `json:"claim_token"`
	FailedReasonCode string `json:"failed_reason_code"`
	FailedReasonText string `json:"failed_reason_text"`
}

type FileUploadRequest struct {
	AssistantID    string
	UserID         string
	TargetBucket   string
	Overwrite      bool
	SourceType     string
	SourceRef      string
	UploaderUserID string
	Filename       string
	ContentType    string
	Content        []byte
	ContentReader  io.Reader
	ContentLength  int64
}

type FileListRequest struct {
	AssistantID string
	UserID      string
	Bucket      string
	Direction   string
	FileRole    string
}

type FileDeleteRequest struct {
	UserID string `json:"user_id"`
}

type SyncResultsRequest struct {
	AssistantID      string    `json:"assistant_id"`
	Bucket           string    `json:"bucket"`
	UserID           string    `json:"user_id,omitempty"`
	ChangedAfter     time.Time `json:"changed_after,omitempty"`
	ResultPathPrefix string    `json:"result_path_prefix,omitempty"`
	TaskID           string    `json:"task_id,omitempty"`
	SourceRef        string    `json:"source_ref,omitempty"`
}

type FileDownloadResponse struct {
	Content            []byte
	ContentType        string
	ContentDisposition string
	DownloadSource     string
	StorageBackend     string
}

type FileDownloadStream struct {
	Body               io.ReadCloser
	ContentType        string
	ContentDisposition string
	DownloadSource     string
	StorageBackend     string
}

type ValidationError struct {
	Message string
}

func (e *ValidationError) Error() string {
	return e.Message
}

type QueueFullError struct {
	Message string
}

func (e *QueueFullError) Error() string {
	return e.Message
}

type ConflictError struct {
	Message string
}

func (e *ConflictError) Error() string {
	return e.Message
}
