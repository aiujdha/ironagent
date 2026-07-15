package core

import (
	"context"
	"time"
)

type GatewayTaskRecord struct {
	Task             TaskView
	ClientID         string
	RequestID        string
	ConversationID   string
	UserID           string
	EmployeeNo       string
	CallbackURL      string
	FallbackQueryURL string
	IdempotencyKey   string
	RequestHash      string
	SouthboundTaskID string
	CreatedAt        time.Time
	UpdatedAt        time.Time
}

type GatewayTaskQueueItem struct {
	TaskID                     string
	Request                    ChatRequest
	RequestHash                string
	Status                     string
	Attempts                   int
	LastError                  string
	NextAttemptAt              *time.Time
	LockedBy                   string
	AttemptToken               string
	LockedUntil                *time.Time
	AttemptStartedAt           *time.Time
	LastProgressAt             *time.Time
	LastProgressStatus         string
	LastProgressStage          string
	LastProgressDetail         string
	TerminalReasonCode         string
	TerminalReasonText         string
	TerminalDetailJSON         string
	CapacityWaitCount          int
	CapacityWaitFirstAt        *time.Time
	CapacityWaitLastAt         *time.Time
	CapacityWaitLastCallbackAt *time.Time
	CreatedAt                  time.Time
	UpdatedAt                  time.Time
}

type QueueCapacityWaitState struct {
	Count          int
	FirstAt        *time.Time
	LastAt         time.Time
	LastCallbackAt *time.Time
}

type TaskQueueStats struct {
	Admitting  int `json:"admitting"`
	Queued     int `json:"queued"`
	Processing int `json:"processing"`
	Retry      int `json:"retry"`
	Failed     int `json:"failed"`
	Completed  int `json:"completed"`
	Cancelled  int `json:"cancelled"`
	Active     int `json:"active"`
	MaxPending int `json:"max_pending,omitempty"`
}

type TaskQueueAssistantStatsProvider interface {
	TaskQueueStatsByTargetAssistant(ctx context.Context, targetAssistantID string) (TaskQueueStats, error)
}

type DBPoolStats struct {
	MaxOpenConnections int     `json:"max_open_connections"`
	OpenConnections    int     `json:"open_connections"`
	InUse              int     `json:"in_use"`
	Idle               int     `json:"idle"`
	WaitCount          int64   `json:"wait_count"`
	WaitDurationSecs   float64 `json:"wait_duration_seconds"`
	MaxIdleClosed      int64   `json:"max_idle_closed"`
	MaxIdleTimeClosed  int64   `json:"max_idle_time_closed"`
	MaxLifetimeClosed  int64   `json:"max_lifetime_closed"`
}

type ConversationRouteRecord struct {
	ClientID       string
	UserID         string
	ConversationID string
	AssistantID    string
	CreatedAt      time.Time
	UpdatedAt      time.Time
}

type TaskStore interface {
	FindByIdempotency(ctx context.Context, clientID string, idempotencyKey string) (GatewayTaskRecord, bool, error)
	FindByApprovalID(ctx context.Context, approvalID string) (GatewayTaskRecord, bool, error)
	GetTask(ctx context.Context, taskID string) (GatewayTaskRecord, bool, error)
	UpsertTask(ctx context.Context, record GatewayTaskRecord) error
}

type ConversationRouteStore interface {
	GetConversationRoute(ctx context.Context, clientID string, userID string, conversationID string) (ConversationRouteRecord, bool, error)
	BindConversationRoute(ctx context.Context, record ConversationRouteRecord) (ConversationRouteRecord, bool, error)
}

type QueuedTaskCreator interface {
	CreateQueuedTask(ctx context.Context, record GatewayTaskRecord, item GatewayTaskQueueItem) (GatewayTaskRecord, bool, error)
}

type TaskQueue interface {
	EnqueueTask(ctx context.Context, item GatewayTaskQueueItem) error
	ActivateQueuedTask(ctx context.Context, taskID string) error
	ClaimQueuedTasks(ctx context.Context, workerID string, limit int, leaseDuration time.Duration) ([]GatewayTaskQueueItem, error)
	ExtendQueuedTaskLease(ctx context.Context, taskID string, lockedBy string, leaseDuration time.Duration) (bool, error)
	RecordQueuedTaskProgress(ctx context.Context, taskID string, lockedBy string, attemptToken string, status string, stage string, detail string, at time.Time) (bool, error)
	CompleteQueuedTask(ctx context.Context, taskID string, lockedBy string, attemptToken string) (bool, error)
	DeferQueuedTask(ctx context.Context, taskID string, lockedBy string, attemptToken string, message string, retryAt *time.Time) (bool, error)
	DeferQueuedTaskForCapacity(ctx context.Context, taskID string, lockedBy string, attemptToken string, message string, retryAt *time.Time, state QueueCapacityWaitState) (bool, error)
	FailQueuedTask(ctx context.Context, taskID string, lockedBy string, attemptToken string, message string, retry bool, retryAt *time.Time, terminal *QueueTerminalReason) (bool, error)
	FailQueuedTaskForCapacity(ctx context.Context, taskID string, lockedBy string, attemptToken string, message string, terminal *QueueTerminalReason, state QueueCapacityWaitState) (bool, error)
	RecordQueuedTaskCapacityWaitCallback(ctx context.Context, taskID string, notifiedAt time.Time) (bool, error)
	CancelQueuedTask(ctx context.Context, taskID string, reason string) error
	TaskQueueStats(ctx context.Context) (TaskQueueStats, error)
}

type QueueTerminalReason struct {
	Code       string
	Text       string
	DetailJSON string
}

type DBPoolStatsProvider interface {
	DBPoolStats() DBPoolStats
}
