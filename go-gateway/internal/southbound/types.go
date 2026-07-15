package southbound

import (
	"io"
	"time"
)

type User struct {
	UserID      string `json:"user_id"`
	DisplayName string `json:"display_name"`
	EmployeeID  string `json:"employee_id"`
	Department  string `json:"department"`
	Role        string `json:"role"`
	CreatedAt   Time   `json:"created_at"`
	UpdatedAt   Time   `json:"updated_at"`
}

type UserAssistantBinding struct {
	UserID       string `json:"user_id"`
	AssistantID  string `json:"assistant_id"`
	Alias        string `json:"alias"`
	SourceSystem string `json:"source_system"`
	BoundAt      Time   `json:"bound_at"`
}

type BindingListResponse struct {
	Bindings []UserAssistantBinding `json:"bindings"`
	Total    int                    `json:"total"`
}

type SharedObjectCreateRequest struct {
	SourceAssistantID     string         `json:"source_assistant_id"`
	Filename              string         `json:"filename"`
	ObjectType            string         `json:"object_type"`
	ContentType           string         `json:"content_type,omitempty"`
	SizeBytes             int64          `json:"size_bytes,omitempty"`
	SHA256                string         `json:"sha256,omitempty"`
	SourceWorkspaceFileID string         `json:"source_workspace_file_id,omitempty"`
	CreatedBy             string         `json:"created_by,omitempty"`
	IdempotencyKey        string         `json:"idempotency_key,omitempty"`
	MetaJSON              map[string]any `json:"meta_json,omitempty"`
}

type SharedObject struct {
	ID                    string         `json:"id"`
	ObjectType            string         `json:"object_type"`
	Filename              string         `json:"filename"`
	ContentType           string         `json:"content_type"`
	SizeBytes             int64          `json:"size_bytes"`
	SHA256                string         `json:"sha256"`
	SourceAssistantID     string         `json:"source_assistant_id"`
	SourceWorkspaceFileID string         `json:"source_workspace_file_id"`
	StorageKey            string         `json:"storage_key"`
	Status                string         `json:"status"`
	CreatedBy             string         `json:"created_by"`
	CreatedAt             Time           `json:"created_at"`
	MetaJSON              map[string]any `json:"meta_json"`
}

type CreateTaskRequest struct {
	SourceAssistantID string         `json:"source_assistant_id"`
	TargetAssistantID string         `json:"target_assistant_id"`
	InputObjectID     string         `json:"input_object_id"`
	Instruction       string         `json:"instruction"`
	CreatedBy         string         `json:"created_by,omitempty"`
	ApprovalMode      string         `json:"approval_mode"`
	IdempotencyKey    string         `json:"idempotency_key,omitempty"`
	TimeoutSeconds    int            `json:"timeout_seconds"`
	MaxDepth          int            `json:"max_depth"`
	MetaJSON          map[string]any `json:"meta_json,omitempty"`
}

type Task struct {
	ID                    string         `json:"id"`
	RootTaskID            string         `json:"root_task_id"`
	ParentTaskID          string         `json:"parent_task_id"`
	Depth                 int            `json:"depth"`
	MaxDepth              int            `json:"max_depth"`
	SourceAssistantID     string         `json:"source_assistant_id"`
	TargetAssistantID     string         `json:"target_assistant_id"`
	InputObjectID         string         `json:"input_object_id"`
	Instruction           string         `json:"instruction"`
	Status                string         `json:"status"`
	ApprovalMode          string         `json:"approval_mode"`
	ApprovalID            string         `json:"approval_id"`
	ResultObjectID        string         `json:"result_object_id"`
	ResultWorkspaceFileID string         `json:"result_workspace_file_id"`
	IdempotencyKey        string         `json:"idempotency_key"`
	BudgetLimit           *int           `json:"budget_limit"`
	TimeoutSeconds        int            `json:"timeout_seconds"`
	CreatedBy             string         `json:"created_by"`
	CreatedAt             Time           `json:"created_at"`
	AcceptedAt            *Time          `json:"accepted_at"`
	StartedAt             *Time          `json:"started_at"`
	CompletedAt           *Time          `json:"completed_at"`
	FailedAt              *Time          `json:"failed_at"`
	FailedReasonCode      string         `json:"failed_reason_code"`
	FailedReasonText      string         `json:"failed_reason_text"`
	MetaJSON              map[string]any `json:"meta_json"`
}

type AssistantRuntime struct {
	ContainerName string `json:"container_name"`
	ContainerID   string `json:"container_id"`
	WorkspaceRoot string `json:"workspace_root"`
	StateRoot     string `json:"state_root"`
	Status        string `json:"status"`
	Image         string `json:"image"`
	GatewayHost   string `json:"gateway_host"`
	GatewayPort   int    `json:"gateway_port"`
	GatewayURL    string `json:"gateway_url"`
	GatewayToken  string `json:"gateway_token"`
	LastError     string `json:"last_error"`
	UpdatedAt     *Time  `json:"updated_at"`
}

type Assistant struct {
	AssistantID       string           `json:"assistant_id"`
	Name              string           `json:"name"`
	CapabilityProfile string           `json:"capability_profile"`
	Runtime           AssistantRuntime `json:"runtime"`
	CreatedAt         *Time            `json:"created_at"`
	UpdatedAt         *Time            `json:"updated_at"`
	DeletedAt         *Time            `json:"deleted_at"`
	Deleted           bool             `json:"deleted"`
}

type AssistantContextEntry struct {
	ID     string `json:"id"`
	Status string `json:"status"`
}

type AssistantContextResponse struct {
	Assistants []AssistantContextEntry `json:"assistants"`
}

type AssistantRuntimeControlResponse struct {
	AssistantID string `json:"assistant_id"`
	Status      string `json:"status"`
}

type ControlUILaunchTicketResolveRequest struct {
	Ticket string `json:"ticket"`
}

type ControlUILaunchTicketResolveResponse struct {
	AssistantID  string `json:"assistant_id,omitempty"`
	GatewayURL   string `json:"gateway_url"`
	GatewayToken string `json:"gateway_token"`
	SessionKey   string `json:"session_key"`
	ExpiresAt    Time   `json:"expires_at"`
}

type ApprovalStatus struct {
	ApprovalID        string `json:"approval_id"`
	ToolName          string `json:"tool_name"`
	AssistantID       string `json:"assistant_id"`
	CapabilityProfile string `json:"capability_profile"`
	ApprovalScope     string `json:"approval_scope"`
	Reason            string `json:"reason"`
	Target            string `json:"target"`
	Status            string `json:"status"`
	Resolution        string `json:"resolution"`
	RequestedAt       *Time  `json:"requested_at"`
	ExpiresAt         *Time  `json:"expires_at"`
	ResolvedAt        *Time  `json:"resolved_at"`
}

type ApprovalResolutionRequest struct {
	Resolution string `json:"resolution"`
}

type ApprovalResolution struct {
	ID     string `json:"id"`
	Status string `json:"status"`
}

type CancelTaskRequest struct {
	ActorID string `json:"actor_id"`
	Reason  string `json:"reason,omitempty"`
}

type ClaimTaskRequest struct {
	AssistantID string `json:"assistant_id"`
}

type RenewClaimRequest struct {
	AssistantID string `json:"assistant_id"`
	ClaimToken  string `json:"claim_token"`
}

type Claim struct {
	ID             string `json:"id"`
	TaskID         string `json:"task_id"`
	AssistantID    string `json:"assistant_id"`
	ClaimToken     string `json:"claim_token"`
	ClaimedAt      Time   `json:"claimed_at"`
	LeaseExpiresAt Time   `json:"lease_expires_at"`
	Status         string `json:"status"`
}

type ClaimResponse struct {
	Task  Task  `json:"task"`
	Claim Claim `json:"claim"`
}

type StartTaskRequest struct {
	AssistantID string `json:"assistant_id"`
	ClaimToken  string `json:"claim_token"`
}

type CompleteTaskRequest struct {
	AssistantID            string   `json:"assistant_id"`
	ClaimToken             string   `json:"claim_token"`
	ResultObjectID         string   `json:"result_object_id,omitempty"`
	ResultWorkspaceFileID  string   `json:"result_workspace_file_id,omitempty"`
	ResultWorkspaceFileIDs []string `json:"result_workspace_file_ids,omitempty"`
}

type FailTaskRequest struct {
	AssistantID      string `json:"assistant_id"`
	ClaimToken       string `json:"claim_token"`
	FailedReasonCode string `json:"failed_reason_code"`
	FailedReasonText string `json:"failed_reason_text,omitempty"`
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
}

type FileListRequest struct {
	AssistantID string
	UserID      string
	Bucket      string
	Direction   string
	FileRole    string
}

type SyncResultsRequest struct {
	AssistantID      string
	Bucket           string
	UserID           string
	ChangedAfter     time.Time
	ResultPathPrefix string
	TaskID           string
	SourceRef        string
}

type WorkspaceScanSummary struct {
	AssistantsScanned int      `json:"assistants_scanned"`
	FilesScanned      int      `json:"files_scanned"`
	FilesSynced       int      `json:"files_synced"`
	FilesDeleted      int      `json:"files_deleted"`
	Errors            []string `json:"errors"`
}

type FileDownloadResponse struct {
	Content            []byte
	ContentType        string
	ContentDisposition string
	DownloadSource     string
	StorageBackend     string
}

type RawJSON map[string]any

type DownloadReader struct {
	ResponseBody       io.ReadCloser
	ContentType        string
	ContentDisposition string
	DownloadSource     string
	StorageBackend     string
}

type HealthSummary struct {
	Components []HealthComponent `json:"components"`
	CheckedAt  time.Time         `json:"checked_at"`
}

type HealthComponent struct {
	Name       string `json:"name"`
	Status     string `json:"status"`
	StatusCode int    `json:"status_code,omitempty"`
	Error      string `json:"error,omitempty"`
}
