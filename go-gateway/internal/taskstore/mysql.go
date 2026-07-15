package taskstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"strings"
	"time"

	"github.com/go-sql-driver/mysql"

	"ironagent/go-gateway/internal/core"
)

type SQLStore struct {
	db *sql.DB
}

type DBPoolConfig struct {
	MaxOpenConns    int
	MaxIdleConns    int
	ConnMaxLifetime time.Duration
	ConnMaxIdleTime time.Duration
}

func NewMySQLStoreFromURL(ctx context.Context, databaseURL string) (*SQLStore, error) {
	return NewMySQLStoreFromURLWithPool(ctx, databaseURL, DBPoolConfig{})
}

func NewMySQLStoreFromURLWithPool(
	ctx context.Context,
	databaseURL string,
	pool DBPoolConfig,
) (*SQLStore, error) {
	dsn, err := MySQLDSNFromURL(databaseURL)
	if err != nil {
		return nil, err
	}
	db, err := sql.Open("mysql", dsn)
	if err != nil {
		return nil, fmt.Errorf("open mysql task store: %w", err)
	}
	applyDBPoolConfig(db, pool)
	if err := db.PingContext(ctx); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("ping mysql task store: %w", err)
	}
	return &SQLStore{db: db}, nil
}

func applyDBPoolConfig(db *sql.DB, pool DBPoolConfig) {
	if db == nil {
		return
	}
	if pool.MaxOpenConns > 0 {
		db.SetMaxOpenConns(pool.MaxOpenConns)
	}
	if pool.MaxIdleConns > 0 {
		db.SetMaxIdleConns(pool.MaxIdleConns)
	}
	if pool.ConnMaxLifetime > 0 {
		db.SetConnMaxLifetime(pool.ConnMaxLifetime)
	}
	if pool.ConnMaxIdleTime > 0 {
		db.SetConnMaxIdleTime(pool.ConnMaxIdleTime)
	}
}

func MySQLDSNFromURL(databaseURL string) (string, error) {
	databaseURL = strings.TrimSpace(databaseURL)
	if databaseURL == "" {
		return "", fmt.Errorf("database url is required")
	}
	if !strings.Contains(databaseURL, "://") {
		return databaseURL, nil
	}

	parsed, err := url.Parse(databaseURL)
	if err != nil {
		return "", fmt.Errorf("parse database url: %w", err)
	}
	switch parsed.Scheme {
	case "mysql", "mysql+pymysql", "mysql+aiomysql":
	default:
		return "", fmt.Errorf("unsupported database url scheme %q", parsed.Scheme)
	}

	cfg := mysql.NewConfig()
	cfg.User = parsed.User.Username()
	cfg.Passwd, _ = parsed.User.Password()
	cfg.Net = "tcp"
	cfg.Addr = parsed.Host
	cfg.DBName = strings.TrimPrefix(parsed.Path, "/")
	cfg.ParseTime = true
	cfg.Loc = time.UTC
	cfg.Params = map[string]string{}
	for key, values := range parsed.Query() {
		if len(values) > 0 {
			cfg.Params[key] = values[len(values)-1]
		}
	}
	if cfg.Params["charset"] == "" {
		cfg.Params["charset"] = "utf8mb4"
	}
	return cfg.FormatDSN(), nil
}

func (s *SQLStore) Close() error {
	if s == nil || s.db == nil {
		return nil
	}
	return s.db.Close()
}

func (s *SQLStore) DBPoolStats() core.DBPoolStats {
	if s == nil || s.db == nil {
		return core.DBPoolStats{}
	}
	stats := s.db.Stats()
	return core.DBPoolStats{
		MaxOpenConnections: stats.MaxOpenConnections,
		OpenConnections:    stats.OpenConnections,
		InUse:              stats.InUse,
		Idle:               stats.Idle,
		WaitCount:          stats.WaitCount,
		WaitDurationSecs:   stats.WaitDuration.Seconds(),
		MaxIdleClosed:      stats.MaxIdleClosed,
		MaxIdleTimeClosed:  stats.MaxIdleTimeClosed,
		MaxLifetimeClosed:  stats.MaxLifetimeClosed,
	}
}

func (s *SQLStore) FindByIdempotency(
	ctx context.Context,
	clientID string,
	idempotencyKey string,
) (core.GatewayTaskRecord, bool, error) {
	clientID = strings.TrimSpace(clientID)
	idempotencyKey = strings.TrimSpace(idempotencyKey)
	if clientID == "" || idempotencyKey == "" {
		return core.GatewayTaskRecord{}, false, nil
	}
	var taskID string
	var requestHash string
	row := s.db.QueryRowContext(
		ctx,
		`SELECT task_id, request_hash FROM gateway_idempotency_keys
		 WHERE client_id = ? AND idempotency_key = ? AND task_id IS NOT NULL`,
		clientID,
		idempotencyKey,
	)
	if err := row.Scan(&taskID, &requestHash); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return core.GatewayTaskRecord{}, false, nil
		}
		return core.GatewayTaskRecord{}, false, err
	}
	record, ok, err := s.GetTask(ctx, taskID)
	record.IdempotencyKey = idempotencyKey
	record.RequestHash = requestHash
	return record, ok, err
}

func (s *SQLStore) GetTask(
	ctx context.Context,
	taskID string,
) (core.GatewayTaskRecord, bool, error) {
	taskID = strings.TrimSpace(taskID)
	if taskID == "" {
		return core.GatewayTaskRecord{}, false, nil
	}
	row := s.db.QueryRowContext(
		ctx,
		`SELECT task_json, client_id, request_id, conversation_id, user_id, employee_no,
		        callback_url, southbound_task_id, created_at, updated_at
		 FROM gateway_tasks WHERE task_id = ?`,
		taskID,
	)
	record, err := scanGatewayTaskRecord(row)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return core.GatewayTaskRecord{}, false, nil
		}
		return core.GatewayTaskRecord{}, false, err
	}
	return record, true, nil
}

func (s *SQLStore) FindByApprovalID(
	ctx context.Context,
	approvalID string,
) (core.GatewayTaskRecord, bool, error) {
	approvalID = strings.TrimSpace(approvalID)
	if approvalID == "" {
		return core.GatewayTaskRecord{}, false, nil
	}
	var taskID string
	row := s.db.QueryRowContext(
		ctx,
		`SELECT task_id FROM gateway_tasks WHERE approval_id = ? ORDER BY created_at DESC LIMIT 1`,
		approvalID,
	)
	if err := row.Scan(&taskID); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return core.GatewayTaskRecord{}, false, nil
		}
		return core.GatewayTaskRecord{}, false, err
	}
	return s.GetTask(ctx, taskID)
}

func (s *SQLStore) UpsertTask(ctx context.Context, record core.GatewayTaskRecord) error {
	record.Task.ID = strings.TrimSpace(record.Task.ID)
	if record.Task.ID == "" {
		return nil
	}
	if strings.TrimSpace(record.ClientID) == "" {
		record.ClientID = "default"
	}
	now := time.Now().UTC()
	if record.CreatedAt.IsZero() {
		record.CreatedAt = record.Task.CreatedAt
	}
	if record.CreatedAt.IsZero() {
		record.CreatedAt = now
	}
	record.UpdatedAt = now

	taskJSON, err := json.Marshal(record.Task)
	if err != nil {
		return fmt.Errorf("marshal task json: %w", err)
	}
	metadataJSON, err := json.Marshal(record.Task.Metadata)
	if err != nil {
		return fmt.Errorf("marshal task metadata: %w", err)
	}
	stage := core.NewTaskResponse(record.Task).Stage

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() {
		if err != nil {
			_ = tx.Rollback()
		}
	}()

	_, err = tx.ExecContext(
		ctx,
		`INSERT INTO gateway_tasks (
		   task_id, client_id, request_id, conversation_id, user_id, employee_no,
		   source_assistant_id, target_assistant_id, southbound_task_id, approval_id, status, stage,
		   fallback_query_url, callback_url, completed_at, failed_reason_code,
		   failed_reason_text, metadata_json, task_json, created_at, updated_at
		 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		 ON DUPLICATE KEY UPDATE
		   client_id = VALUES(client_id),
		   request_id = VALUES(request_id),
		   conversation_id = VALUES(conversation_id),
		   user_id = VALUES(user_id),
		   employee_no = VALUES(employee_no),
		   source_assistant_id = VALUES(source_assistant_id),
		   target_assistant_id = VALUES(target_assistant_id),
		   southbound_task_id = VALUES(southbound_task_id),
		   approval_id = VALUES(approval_id),
		   status = VALUES(status),
		   stage = VALUES(stage),
		   fallback_query_url = VALUES(fallback_query_url),
		   callback_url = VALUES(callback_url),
		   completed_at = VALUES(completed_at),
		   failed_reason_code = VALUES(failed_reason_code),
		   failed_reason_text = VALUES(failed_reason_text),
		   metadata_json = VALUES(metadata_json),
		   task_json = VALUES(task_json),
		   updated_at = VALUES(updated_at)`,
		record.Task.ID,
		record.ClientID,
		emptyToNil(record.RequestID),
		emptyToNil(record.ConversationID),
		emptyToNil(record.UserID),
		emptyToNil(record.EmployeeNo),
		emptyToNil(record.Task.SourceAssistantID),
		emptyToNil(record.Task.TargetAssistantID),
		emptyToNil(record.SouthboundTaskID),
		emptyToNil(record.Task.ApprovalID),
		record.Task.Status,
		emptyToNil(stage),
		emptyToNil(record.FallbackQueryURL),
		emptyToNil(record.CallbackURL),
		record.Task.CompletedAt,
		emptyToNil(record.Task.FailedReasonCode),
		emptyToNil(record.Task.FailedReasonText),
		metadataJSON,
		taskJSON,
		record.CreatedAt,
		record.UpdatedAt,
	)
	if err != nil {
		return err
	}

	if strings.TrimSpace(record.IdempotencyKey) != "" {
		_, err = tx.ExecContext(
			ctx,
			`INSERT INTO gateway_idempotency_keys (
			   client_id, idempotency_key, task_id, request_hash, created_at
				 ) VALUES (?, ?, ?, ?, ?)
				 ON DUPLICATE KEY UPDATE
				   task_id = IF(request_hash = VALUES(request_hash) AND task_id IS NULL, VALUES(task_id), task_id)`,
			record.ClientID,
			record.IdempotencyKey,
			record.Task.ID,
			record.RequestHash,
			record.CreatedAt,
		)
		if err != nil {
			return err
		}
	}

	err = tx.Commit()
	return err
}

func (s *SQLStore) CreateQueuedTask(
	ctx context.Context,
	record core.GatewayTaskRecord,
	item core.GatewayTaskQueueItem,
) (core.GatewayTaskRecord, bool, error) {
	record.Task.ID = strings.TrimSpace(record.Task.ID)
	item.TaskID = strings.TrimSpace(item.TaskID)
	if record.Task.ID == "" || item.TaskID == "" {
		return core.GatewayTaskRecord{}, false, nil
	}
	if strings.TrimSpace(record.ClientID) == "" {
		record.ClientID = "default"
	}
	now := time.Now().UTC()
	if record.CreatedAt.IsZero() {
		record.CreatedAt = record.Task.CreatedAt
	}
	if record.CreatedAt.IsZero() {
		record.CreatedAt = now
	}
	record.UpdatedAt = now
	if strings.TrimSpace(item.Status) == "" {
		item.Status = "queued"
	}
	if item.CreatedAt.IsZero() {
		item.CreatedAt = now
	}
	item.UpdatedAt = now

	taskJSON, err := json.Marshal(record.Task)
	if err != nil {
		return core.GatewayTaskRecord{}, false, fmt.Errorf("marshal task json: %w", err)
	}
	metadataJSON, err := json.Marshal(record.Task.Metadata)
	if err != nil {
		return core.GatewayTaskRecord{}, false, fmt.Errorf("marshal task metadata: %w", err)
	}
	requestJSON, err := json.Marshal(item.Request)
	if err != nil {
		return core.GatewayTaskRecord{}, false, fmt.Errorf("marshal queued request: %w", err)
	}
	stage := core.NewTaskResponse(record.Task).Stage

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return core.GatewayTaskRecord{}, false, err
	}
	defer tx.Rollback()

	idempotencyKey := strings.TrimSpace(record.IdempotencyKey)
	if idempotencyKey != "" {
		_, err = tx.ExecContext(
			ctx,
			`INSERT INTO gateway_idempotency_keys (
			   client_id, idempotency_key, task_id, request_hash, created_at
			 ) VALUES (?, ?, NULL, ?, ?)
			 ON DUPLICATE KEY UPDATE idempotency_key = idempotency_key`,
			record.ClientID,
			idempotencyKey,
			record.RequestHash,
			record.CreatedAt,
		)
		if err != nil {
			return core.GatewayTaskRecord{}, false, err
		}

		var existingTaskID sql.NullString
		var existingHash string
		row := tx.QueryRowContext(
			ctx,
			`SELECT task_id, request_hash
			 FROM gateway_idempotency_keys
			 WHERE client_id = ? AND idempotency_key = ?
			 FOR UPDATE`,
			record.ClientID,
			idempotencyKey,
		)
		if err := row.Scan(&existingTaskID, &existingHash); err != nil {
			return core.GatewayTaskRecord{}, false, err
		}
		if existingHash != "" && record.RequestHash != "" && existingHash != record.RequestHash {
			return core.GatewayTaskRecord{}, false, &core.ValidationError{Message: "idempotency_key was already used with a different request"}
		}
		if existingTaskID.Valid && strings.TrimSpace(existingTaskID.String) != "" {
			taskID := strings.TrimSpace(existingTaskID.String)
			if err := tx.Commit(); err != nil {
				return core.GatewayTaskRecord{}, false, err
			}
			existing, ok, err := s.GetTask(ctx, taskID)
			if err != nil || !ok {
				return existing, ok, err
			}
			existing.IdempotencyKey = idempotencyKey
			existing.RequestHash = existingHash
			return existing, true, nil
		}
	}

	_, err = tx.ExecContext(
		ctx,
		`INSERT INTO gateway_tasks (
		   task_id, client_id, request_id, conversation_id, user_id, employee_no,
		   source_assistant_id, target_assistant_id, southbound_task_id, approval_id, status, stage,
		   fallback_query_url, callback_url, completed_at, failed_reason_code,
		   failed_reason_text, metadata_json, task_json, created_at, updated_at
		 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		 ON DUPLICATE KEY UPDATE
		   updated_at = VALUES(updated_at)`,
		record.Task.ID,
		record.ClientID,
		emptyToNil(record.RequestID),
		emptyToNil(record.ConversationID),
		emptyToNil(record.UserID),
		emptyToNil(record.EmployeeNo),
		emptyToNil(record.Task.SourceAssistantID),
		emptyToNil(record.Task.TargetAssistantID),
		emptyToNil(record.SouthboundTaskID),
		emptyToNil(record.Task.ApprovalID),
		record.Task.Status,
		emptyToNil(stage),
		emptyToNil(record.FallbackQueryURL),
		emptyToNil(record.CallbackURL),
		record.Task.CompletedAt,
		emptyToNil(record.Task.FailedReasonCode),
		emptyToNil(record.Task.FailedReasonText),
		metadataJSON,
		taskJSON,
		record.CreatedAt,
		record.UpdatedAt,
	)
	if err != nil {
		return core.GatewayTaskRecord{}, false, err
	}

	_, err = tx.ExecContext(
		ctx,
		`INSERT INTO gateway_task_queue (
		   task_id, request_json, request_hash, status, attempts, last_error,
		   next_attempt_at, locked_by, attempt_token, locked_until, attempt_started_at,
		   last_progress_at, last_progress_status, last_progress_stage, last_progress_detail,
		   terminal_reason_code, terminal_reason_text, terminal_detail_json,
		   created_at, updated_at
		 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		 ON DUPLICATE KEY UPDATE
		   request_json = VALUES(request_json),
		   request_hash = VALUES(request_hash),
		   updated_at = VALUES(updated_at)`,
		item.TaskID,
		requestJSON,
		item.RequestHash,
		item.Status,
		item.Attempts,
		emptyToNil(item.LastError),
		item.NextAttemptAt,
		emptyToNil(item.LockedBy),
		emptyToNil(item.AttemptToken),
		item.LockedUntil,
		item.AttemptStartedAt,
		item.LastProgressAt,
		emptyToNil(item.LastProgressStatus),
		emptyToNil(item.LastProgressStage),
		emptyToNil(item.LastProgressDetail),
		emptyToNil(item.TerminalReasonCode),
		emptyToNil(item.TerminalReasonText),
		emptyToNil(item.TerminalDetailJSON),
		item.CreatedAt,
		item.UpdatedAt,
	)
	if err != nil {
		return core.GatewayTaskRecord{}, false, err
	}

	if idempotencyKey != "" {
		_, err = tx.ExecContext(
			ctx,
			`UPDATE gateway_idempotency_keys
			 SET task_id = ?
			 WHERE client_id = ? AND idempotency_key = ? AND task_id IS NULL`,
			record.Task.ID,
			record.ClientID,
			idempotencyKey,
		)
		if err != nil {
			return core.GatewayTaskRecord{}, false, err
		}
	}

	if err := tx.Commit(); err != nil {
		return core.GatewayTaskRecord{}, false, err
	}
	return record, false, nil
}

func (s *SQLStore) EnqueueTask(ctx context.Context, item core.GatewayTaskQueueItem) error {
	item.TaskID = strings.TrimSpace(item.TaskID)
	if item.TaskID == "" {
		return nil
	}
	if strings.TrimSpace(item.Status) == "" {
		item.Status = "queued"
	}
	now := time.Now().UTC()
	if item.CreatedAt.IsZero() {
		item.CreatedAt = now
	}
	item.UpdatedAt = now
	requestJSON, err := json.Marshal(item.Request)
	if err != nil {
		return fmt.Errorf("marshal queued request: %w", err)
	}
	_, err = s.db.ExecContext(
		ctx,
		`INSERT INTO gateway_task_queue (
		   task_id, request_json, request_hash, status, attempts, last_error,
		   next_attempt_at, locked_by, attempt_token, locked_until, attempt_started_at,
		   last_progress_at, last_progress_status, last_progress_stage, last_progress_detail,
		   terminal_reason_code, terminal_reason_text, terminal_detail_json,
		   created_at, updated_at
		 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		 ON DUPLICATE KEY UPDATE
		   request_json = VALUES(request_json),
		   request_hash = VALUES(request_hash),
		   updated_at = VALUES(updated_at)`,
		item.TaskID,
		requestJSON,
		item.RequestHash,
		item.Status,
		item.Attempts,
		emptyToNil(item.LastError),
		item.NextAttemptAt,
		emptyToNil(item.LockedBy),
		emptyToNil(item.AttemptToken),
		item.LockedUntil,
		item.AttemptStartedAt,
		item.LastProgressAt,
		emptyToNil(item.LastProgressStatus),
		emptyToNil(item.LastProgressStage),
		emptyToNil(item.LastProgressDetail),
		emptyToNil(item.TerminalReasonCode),
		emptyToNil(item.TerminalReasonText),
		emptyToNil(item.TerminalDetailJSON),
		item.CreatedAt,
		item.UpdatedAt,
	)
	return err
}

func (s *SQLStore) ActivateQueuedTask(ctx context.Context, taskID string) error {
	_, err := s.db.ExecContext(
		ctx,
		`UPDATE gateway_task_queue
		 SET status = 'queued', updated_at = ?
		 WHERE task_id = ? AND status = 'admitting'`,
		time.Now().UTC(),
		strings.TrimSpace(taskID),
	)
	return err
}

func (s *SQLStore) ClaimQueuedTasks(
	ctx context.Context,
	workerID string,
	limit int,
	leaseDuration time.Duration,
) ([]core.GatewayTaskQueueItem, error) {
	const maxClaimAttempts = 3
	var lastErr error
	for attempt := 1; attempt <= maxClaimAttempts; attempt++ {
		items, err := s.claimQueuedTasksOnce(ctx, workerID, limit, leaseDuration)
		if err == nil {
			return items, nil
		}
		lastErr = err
		if !mysqlLockRetryable(err) || attempt == maxClaimAttempts {
			break
		}
		backoff := time.Duration(attempt*attempt) * 25 * time.Millisecond
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(backoff):
		}
	}
	return nil, lastErr
}

func (s *SQLStore) claimQueuedTasksOnce(
	ctx context.Context,
	workerID string,
	limit int,
	leaseDuration time.Duration,
) ([]core.GatewayTaskQueueItem, error) {
	workerID = strings.TrimSpace(workerID)
	if workerID == "" {
		workerID = "mysql-worker"
	}
	if limit <= 0 {
		limit = 1
	}
	if leaseDuration <= 0 {
		leaseDuration = 30 * time.Second
	}
	now := time.Now().UTC()
	claimID := fmt.Sprintf("%s-%d", workerID, now.UnixNano())
	attemptToken := claimID
	lockedUntil := now.Add(leaseDuration)

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()

	taskIDs, err := selectClaimableQueuedTaskIDs(ctx, tx, now, limit)
	if err != nil {
		return nil, err
	}
	if len(taskIDs) < limit {
		staleTaskIDs, err := selectExpiredProcessingTaskIDs(ctx, tx, now, limit-len(taskIDs))
		if err != nil {
			return nil, err
		}
		taskIDs = append(taskIDs, staleTaskIDs...)
	}
	if len(taskIDs) == 0 {
		if err := tx.Commit(); err != nil {
			return nil, err
		}
		return nil, nil
	}

	for _, taskID := range taskIDs {
		_, err := tx.ExecContext(
			ctx,
			`UPDATE gateway_task_queue
				 SET status = 'processing',
				     locked_by = ?,
				     attempt_token = ?,
				     locked_until = ?,
				     attempts = attempts + 1,
				     attempt_started_at = ?,
				     last_progress_at = ?,
				     last_progress_status = 'claimed',
				     last_progress_stage = 'queued',
				     last_progress_detail = NULL,
				     terminal_reason_code = NULL,
				     terminal_reason_text = NULL,
				     terminal_detail_json = NULL,
				     updated_at = ?
			 WHERE task_id = ?`,
			claimID,
			attemptToken,
			lockedUntil,
			now,
			now,
			now,
			taskID,
		)
		if err != nil {
			return nil, err
		}
	}

	rows, err := tx.QueryContext(
		ctx,
		`SELECT task_id, request_json, request_hash, status, attempts, last_error,
		        next_attempt_at, locked_by, attempt_token, locked_until,
		        attempt_started_at, last_progress_at, last_progress_status,
		        last_progress_stage, last_progress_detail, terminal_reason_code,
		        terminal_reason_text, terminal_detail_json, capacity_wait_count,
		        capacity_wait_first_at, capacity_wait_last_at, capacity_wait_last_callback_at,
		        created_at, updated_at
		 FROM gateway_task_queue
		 WHERE locked_by = ? AND status = 'processing'
		 ORDER BY created_at`,
		claimID,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var items []core.GatewayTaskQueueItem
	for rows.Next() {
		item, err := scanQueuedTaskRow(rows)
		if err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return items, nil
}

func selectClaimableQueuedTaskIDs(
	ctx context.Context,
	tx *sql.Tx,
	now time.Time,
	limit int,
) ([]string, error) {
	return selectQueuedTaskIDs(
		ctx,
		tx,
		`SELECT task_id
		 FROM gateway_task_queue
		 WHERE status IN ('queued', 'retry')
		   AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
		 ORDER BY created_at
		 LIMIT ?
		 FOR UPDATE SKIP LOCKED`,
		now,
		limit,
	)
}

func selectExpiredProcessingTaskIDs(
	ctx context.Context,
	tx *sql.Tx,
	now time.Time,
	limit int,
) ([]string, error) {
	if limit <= 0 {
		return nil, nil
	}
	return selectQueuedTaskIDs(
		ctx,
		tx,
		`SELECT task_id
		 FROM gateway_task_queue
		 WHERE status = 'processing'
		   AND locked_until IS NOT NULL
		   AND locked_until <= ?
		   AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
		 ORDER BY locked_until, created_at
		 LIMIT ?
		 FOR UPDATE SKIP LOCKED`,
		now,
		now,
		limit,
	)
}

func selectQueuedTaskIDs(
	ctx context.Context,
	tx *sql.Tx,
	query string,
	args ...any,
) ([]string, error) {
	rows, err := tx.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var taskIDs []string
	for rows.Next() {
		var taskID string
		if err := rows.Scan(&taskID); err != nil {
			return nil, err
		}
		taskID = strings.TrimSpace(taskID)
		if taskID != "" {
			taskIDs = append(taskIDs, taskID)
		}
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return taskIDs, nil
}

func mysqlLockRetryable(err error) bool {
	if err == nil {
		return false
	}
	var mysqlErr *mysql.MySQLError
	if errors.As(err, &mysqlErr) {
		return mysqlErr.Number == 1205 || mysqlErr.Number == 1213
	}
	message := strings.ToLower(err.Error())
	return strings.Contains(message, "deadlock") ||
		strings.Contains(message, "lock wait timeout")
}

func (s *SQLStore) ExtendQueuedTaskLease(
	ctx context.Context,
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
	result, err := s.db.ExecContext(
		ctx,
		`UPDATE gateway_task_queue
		 SET locked_until = ?, updated_at = ?
		 WHERE task_id = ? AND locked_by = ? AND status = 'processing'`,
		now.Add(leaseDuration),
		now,
		taskID,
		lockedBy,
	)
	if err != nil {
		return false, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return false, err
	}
	return affected > 0, nil
}

func (s *SQLStore) RecordQueuedTaskProgress(
	ctx context.Context,
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
	result, err := s.db.ExecContext(
		ctx,
		`UPDATE gateway_task_queue
		 SET last_progress_at = ?, last_progress_status = ?, last_progress_stage = ?,
		     last_progress_detail = ?, updated_at = ?
		 WHERE task_id = ? AND locked_by = ? AND attempt_token = ?
		   AND status = 'processing'
		   AND (locked_until IS NULL OR locked_until > ?)`,
		at,
		emptyToNil(status),
		emptyToNil(stage),
		emptyToNil(detail),
		at,
		taskID,
		lockedBy,
		attemptToken,
		at,
	)
	if err != nil {
		return false, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return false, err
	}
	return affected > 0, nil
}

func (s *SQLStore) CompleteQueuedTask(ctx context.Context, taskID string, lockedBy string, attemptToken string) (bool, error) {
	now := time.Now().UTC()
	result, err := s.db.ExecContext(
		ctx,
		`UPDATE gateway_task_queue
			 SET status = 'completed', locked_by = NULL, locked_until = NULL,
			     capacity_wait_count = 0,
			     capacity_wait_first_at = NULL,
			     capacity_wait_last_at = NULL,
			     capacity_wait_last_callback_at = NULL,
			     updated_at = ?
			 WHERE task_id = ? AND locked_by = ? AND attempt_token = ? AND status = 'processing'
			   AND (locked_until IS NULL OR locked_until > ?)`,
		now,
		strings.TrimSpace(taskID),
		strings.TrimSpace(lockedBy),
		strings.TrimSpace(attemptToken),
		now,
	)
	if err != nil {
		return false, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return false, err
	}
	return affected > 0, nil
}

func (s *SQLStore) FailQueuedTask(
	ctx context.Context,
	taskID string,
	lockedBy string,
	attemptToken string,
	message string,
	retry bool,
	retryAt *time.Time,
	terminal *core.QueueTerminalReason,
) (bool, error) {
	status := "failed"
	if retry {
		status = "retry"
	}
	terminalCode := any(nil)
	terminalText := any(nil)
	terminalDetail := any(nil)
	if !retry && terminal != nil {
		terminalCode = emptyToNil(terminal.Code)
		terminalText = emptyToNil(terminal.Text)
		terminalDetail = emptyToNil(terminal.DetailJSON)
	}
	now := time.Now().UTC()
	result, err := s.db.ExecContext(
		ctx,
		`UPDATE gateway_task_queue
			 SET status = ?, last_error = ?, next_attempt_at = ?, locked_by = NULL,
			     locked_until = NULL, terminal_reason_code = ?, terminal_reason_text = ?,
			     terminal_detail_json = ?,
			     capacity_wait_count = 0,
			     capacity_wait_first_at = NULL,
			     capacity_wait_last_at = NULL,
			     capacity_wait_last_callback_at = NULL,
			     updated_at = ?
			 WHERE task_id = ? AND locked_by = ? AND attempt_token = ? AND status = 'processing'
			   AND (locked_until IS NULL OR locked_until > ?)`,
		status,
		strings.TrimSpace(message),
		retryAt,
		terminalCode,
		terminalText,
		terminalDetail,
		now,
		strings.TrimSpace(taskID),
		strings.TrimSpace(lockedBy),
		strings.TrimSpace(attemptToken),
		now,
	)
	if err != nil {
		return false, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return false, err
	}
	return affected > 0, nil
}

func (s *SQLStore) FailQueuedTaskForCapacity(
	ctx context.Context,
	taskID string,
	lockedBy string,
	attemptToken string,
	message string,
	terminal *core.QueueTerminalReason,
	state core.QueueCapacityWaitState,
) (bool, error) {
	terminalCode := any(nil)
	terminalText := any(nil)
	terminalDetail := any(nil)
	if terminal != nil {
		terminalCode = emptyToNil(terminal.Code)
		terminalText = emptyToNil(terminal.Text)
		terminalDetail = emptyToNil(terminal.DetailJSON)
	}
	now := time.Now().UTC()
	result, err := s.db.ExecContext(
		ctx,
		`UPDATE gateway_task_queue
			 SET status = 'failed',
			     last_error = ?,
			     next_attempt_at = NULL,
			     locked_by = NULL,
			     attempt_token = NULL,
			     locked_until = NULL,
			     attempt_started_at = NULL,
			     terminal_reason_code = ?,
			     terminal_reason_text = ?,
			     terminal_detail_json = ?,
			     capacity_wait_count = ?,
			     capacity_wait_first_at = ?,
			     capacity_wait_last_at = ?,
			     capacity_wait_last_callback_at = ?,
			     updated_at = ?
			 WHERE task_id = ? AND locked_by = ? AND attempt_token = ? AND status = 'processing'
			   AND (locked_until IS NULL OR locked_until > ?)`,
		strings.TrimSpace(message),
		terminalCode,
		terminalText,
		terminalDetail,
		state.Count,
		timePtrValue(state.FirstAt),
		timePtrValue(&state.LastAt),
		timePtrValue(state.LastCallbackAt),
		now,
		strings.TrimSpace(taskID),
		strings.TrimSpace(lockedBy),
		strings.TrimSpace(attemptToken),
		now,
	)
	if err != nil {
		return false, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return false, err
	}
	return affected > 0, nil
}

func (s *SQLStore) DeferQueuedTask(
	ctx context.Context,
	taskID string,
	lockedBy string,
	attemptToken string,
	message string,
	retryAt *time.Time,
) (bool, error) {
	now := time.Now().UTC()
	result, err := s.db.ExecContext(
		ctx,
		`UPDATE gateway_task_queue
			 SET status = 'retry',
			     last_error = ?,
			     next_attempt_at = ?,
			     locked_by = NULL,
			     attempt_token = NULL,
			     locked_until = NULL,
			     attempt_started_at = NULL,
			     attempts = GREATEST(attempts - 1, 0),
			     terminal_reason_code = NULL,
			     terminal_reason_text = NULL,
			     terminal_detail_json = NULL,
			     capacity_wait_count = 0,
			     capacity_wait_first_at = NULL,
			     capacity_wait_last_at = NULL,
			     capacity_wait_last_callback_at = NULL,
			     updated_at = ?
			 WHERE task_id = ? AND locked_by = ? AND attempt_token = ? AND status = 'processing'
			   AND (locked_until IS NULL OR locked_until > ?)`,
		strings.TrimSpace(message),
		retryAt,
		now,
		strings.TrimSpace(taskID),
		strings.TrimSpace(lockedBy),
		strings.TrimSpace(attemptToken),
		now,
	)
	if err != nil {
		return false, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return false, err
	}
	return affected > 0, nil
}

func (s *SQLStore) DeferQueuedTaskForCapacity(
	ctx context.Context,
	taskID string,
	lockedBy string,
	attemptToken string,
	message string,
	retryAt *time.Time,
	state core.QueueCapacityWaitState,
) (bool, error) {
	now := time.Now().UTC()
	result, err := s.db.ExecContext(
		ctx,
		`UPDATE gateway_task_queue
			 SET status = 'retry',
			     last_error = ?,
			     next_attempt_at = ?,
			     locked_by = NULL,
			     attempt_token = NULL,
			     locked_until = NULL,
			     attempt_started_at = NULL,
			     attempts = GREATEST(attempts - 1, 0),
			     terminal_reason_code = NULL,
			     terminal_reason_text = NULL,
			     terminal_detail_json = NULL,
			     capacity_wait_count = ?,
			     capacity_wait_first_at = ?,
			     capacity_wait_last_at = ?,
			     capacity_wait_last_callback_at = ?,
			     updated_at = ?
			 WHERE task_id = ? AND locked_by = ? AND attempt_token = ? AND status = 'processing'
			   AND (locked_until IS NULL OR locked_until > ?)`,
		strings.TrimSpace(message),
		retryAt,
		state.Count,
		timePtrValue(state.FirstAt),
		timePtrValue(&state.LastAt),
		timePtrValue(state.LastCallbackAt),
		now,
		strings.TrimSpace(taskID),
		strings.TrimSpace(lockedBy),
		strings.TrimSpace(attemptToken),
		now,
	)
	if err != nil {
		return false, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return false, err
	}
	return affected > 0, nil
}

func (s *SQLStore) RecordQueuedTaskCapacityWaitCallback(
	ctx context.Context,
	taskID string,
	notifiedAt time.Time,
) (bool, error) {
	if notifiedAt.IsZero() {
		notifiedAt = time.Now().UTC()
	}
	result, err := s.db.ExecContext(
		ctx,
		`UPDATE gateway_task_queue
			 SET capacity_wait_last_callback_at = ?, updated_at = ?
			 WHERE task_id = ? AND status IN ('retry', 'processing')`,
		notifiedAt.UTC(),
		notifiedAt.UTC(),
		strings.TrimSpace(taskID),
	)
	if err != nil {
		return false, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return false, err
	}
	return affected > 0, nil
}

func (s *SQLStore) CancelQueuedTask(ctx context.Context, taskID string, reason string) error {
	_, err := s.db.ExecContext(
		ctx,
		`UPDATE gateway_task_queue
		 SET status = 'cancelled', last_error = ?, locked_by = NULL,
		     locked_until = NULL,
		     capacity_wait_count = 0,
		     capacity_wait_first_at = NULL,
		     capacity_wait_last_at = NULL,
		     capacity_wait_last_callback_at = NULL,
		     updated_at = ?
		 WHERE task_id = ? AND status IN ('admitting', 'queued', 'retry', 'processing')`,
		strings.TrimSpace(reason),
		time.Now().UTC(),
		strings.TrimSpace(taskID),
	)
	return err
}

func (s *SQLStore) TaskQueueStats(ctx context.Context) (core.TaskQueueStats, error) {
	rows, err := s.db.QueryContext(
		ctx,
		`SELECT status, COUNT(*) FROM gateway_task_queue GROUP BY status`,
	)
	if err != nil {
		return core.TaskQueueStats{}, err
	}
	defer rows.Close()
	var stats core.TaskQueueStats
	for rows.Next() {
		var status string
		var count int
		if err := rows.Scan(&status, &count); err != nil {
			return core.TaskQueueStats{}, err
		}
		switch status {
		case "admitting":
			stats.Admitting = count
		case "queued":
			stats.Queued = count
		case "processing":
			stats.Processing = count
		case "retry":
			stats.Retry = count
		case "failed":
			stats.Failed = count
		case "completed":
			stats.Completed = count
		case "cancelled":
			stats.Cancelled = count
		}
	}
	stats.Active = stats.Admitting + stats.Queued + stats.Processing + stats.Retry
	return stats, rows.Err()
}

func (s *SQLStore) TaskQueueStatsByTargetAssistant(
	ctx context.Context,
	targetAssistantID string,
) (core.TaskQueueStats, error) {
	targetAssistantID = strings.TrimSpace(targetAssistantID)
	if targetAssistantID == "" {
		return core.TaskQueueStats{}, nil
	}
	rows, err := s.db.QueryContext(
		ctx,
		`SELECT q.status, COUNT(*)
		   FROM gateway_task_queue q
		   JOIN gateway_tasks t ON t.task_id = q.task_id
		  WHERE t.target_assistant_id = ?
		    AND q.status IN ('admitting', 'queued', 'processing', 'retry')
		  GROUP BY q.status`,
		targetAssistantID,
	)
	if err != nil {
		return core.TaskQueueStats{}, err
	}
	defer rows.Close()
	var stats core.TaskQueueStats
	for rows.Next() {
		var status string
		var count int
		if err := rows.Scan(&status, &count); err != nil {
			return core.TaskQueueStats{}, err
		}
		switch status {
		case "admitting":
			stats.Admitting = count
		case "queued":
			stats.Queued = count
		case "processing":
			stats.Processing = count
		case "retry":
			stats.Retry = count
		}
	}
	stats.Active = stats.Admitting + stats.Queued + stats.Processing + stats.Retry
	return stats, rows.Err()
}

func scanGatewayTaskRecord(row *sql.Row) (core.GatewayTaskRecord, error) {
	var taskJSON []byte
	var record core.GatewayTaskRecord
	var requestID sql.NullString
	var conversationID sql.NullString
	var userID sql.NullString
	var employeeNo sql.NullString
	var callbackURL sql.NullString
	var southboundTaskID sql.NullString
	if err := row.Scan(
		&taskJSON,
		&record.ClientID,
		&requestID,
		&conversationID,
		&userID,
		&employeeNo,
		&callbackURL,
		&southboundTaskID,
		&record.CreatedAt,
		&record.UpdatedAt,
	); err != nil {
		return core.GatewayTaskRecord{}, err
	}
	if err := json.Unmarshal(taskJSON, &record.Task); err != nil {
		return core.GatewayTaskRecord{}, fmt.Errorf("unmarshal task json: %w", err)
	}
	record.RequestID = requestID.String
	record.ConversationID = conversationID.String
	record.UserID = userID.String
	record.EmployeeNo = employeeNo.String
	record.CallbackURL = callbackURL.String
	record.SouthboundTaskID = southboundTaskID.String
	return record, nil
}

type queuedTaskScanner interface {
	Scan(dest ...any) error
}

func scanQueuedTaskRow(row queuedTaskScanner) (core.GatewayTaskQueueItem, error) {
	var item core.GatewayTaskQueueItem
	var requestJSON []byte
	var lastError sql.NullString
	var nextAttemptAt sql.NullTime
	var lockedBy sql.NullString
	var attemptToken sql.NullString
	var lockedUntil sql.NullTime
	var attemptStartedAt sql.NullTime
	var lastProgressAt sql.NullTime
	var lastProgressStatus sql.NullString
	var lastProgressStage sql.NullString
	var lastProgressDetail sql.NullString
	var terminalReasonCode sql.NullString
	var terminalReasonText sql.NullString
	var terminalDetailJSON sql.NullString
	var capacityWaitFirstAt sql.NullTime
	var capacityWaitLastAt sql.NullTime
	var capacityWaitLastCallbackAt sql.NullTime
	if err := row.Scan(
		&item.TaskID,
		&requestJSON,
		&item.RequestHash,
		&item.Status,
		&item.Attempts,
		&lastError,
		&nextAttemptAt,
		&lockedBy,
		&attemptToken,
		&lockedUntil,
		&attemptStartedAt,
		&lastProgressAt,
		&lastProgressStatus,
		&lastProgressStage,
		&lastProgressDetail,
		&terminalReasonCode,
		&terminalReasonText,
		&terminalDetailJSON,
		&item.CapacityWaitCount,
		&capacityWaitFirstAt,
		&capacityWaitLastAt,
		&capacityWaitLastCallbackAt,
		&item.CreatedAt,
		&item.UpdatedAt,
	); err != nil {
		return core.GatewayTaskQueueItem{}, err
	}
	if err := json.Unmarshal(requestJSON, &item.Request); err != nil {
		return core.GatewayTaskQueueItem{}, fmt.Errorf("unmarshal queued request: %w", err)
	}
	item.LastError = lastError.String
	if nextAttemptAt.Valid {
		item.NextAttemptAt = &nextAttemptAt.Time
	}
	item.LockedBy = lockedBy.String
	item.AttemptToken = attemptToken.String
	if lockedUntil.Valid {
		item.LockedUntil = &lockedUntil.Time
	}
	if attemptStartedAt.Valid {
		item.AttemptStartedAt = &attemptStartedAt.Time
	}
	if lastProgressAt.Valid {
		item.LastProgressAt = &lastProgressAt.Time
	}
	item.LastProgressStatus = lastProgressStatus.String
	item.LastProgressStage = lastProgressStage.String
	item.LastProgressDetail = lastProgressDetail.String
	item.TerminalReasonCode = terminalReasonCode.String
	item.TerminalReasonText = terminalReasonText.String
	item.TerminalDetailJSON = terminalDetailJSON.String
	if capacityWaitFirstAt.Valid {
		item.CapacityWaitFirstAt = &capacityWaitFirstAt.Time
	}
	if capacityWaitLastAt.Valid {
		item.CapacityWaitLastAt = &capacityWaitLastAt.Time
	}
	if capacityWaitLastCallbackAt.Valid {
		item.CapacityWaitLastCallbackAt = &capacityWaitLastCallbackAt.Time
	}
	return item, nil
}

func timePtrValue(value *time.Time) any {
	if value == nil || value.IsZero() {
		return nil
	}
	return value.UTC()
}

func emptyToNil(value string) any {
	value = strings.TrimSpace(value)
	if value == "" {
		return nil
	}
	return value
}
