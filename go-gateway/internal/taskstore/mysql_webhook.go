package taskstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"ironagent/go-gateway/internal/webhook"
)

func (s *SQLStore) NextEventSeq(ctx context.Context, taskID string) (int64, error) {
	taskID = strings.TrimSpace(taskID)
	if taskID == "" {
		return 0, fmt.Errorf("task_id is required")
	}
	result, err := s.db.ExecContext(
		ctx,
		`INSERT INTO gateway_event_sequences (task_id, last_event_seq, updated_at)
		 VALUES (?, LAST_INSERT_ID(1), ?)
		 ON DUPLICATE KEY UPDATE
		   last_event_seq = LAST_INSERT_ID(last_event_seq + 1),
		   updated_at = VALUES(updated_at)`,
		taskID,
		time.Now().UTC(),
	)
	if err != nil {
		return 0, err
	}
	seq, err := result.LastInsertId()
	if err != nil {
		return 0, err
	}
	return seq, nil
}

func (s *SQLStore) Enqueue(ctx context.Context, record webhook.DeliveryRecord) error {
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
	payload, err := json.Marshal(record.Event)
	if err != nil {
		return fmt.Errorf("marshal webhook event: %w", err)
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if webhook.SupersedesPendingRetrying(record.Event) {
		if _, err := tx.ExecContext(
			ctx,
			`UPDATE gateway_event_outbox
			 SET status = 'dead',
			     last_error = 'superseded by newer callback event',
			     next_attempt_at = NULL,
			     locked_by = NULL,
			     locked_until = NULL,
			     updated_at = ?
			 WHERE task_id = ?
			   AND callback_url = ?
			   AND event_type = 'task.retrying'
			   AND status = 'pending'
			   AND event_seq < ?`,
			now,
			record.Event.TaskID,
			record.CallbackURL,
			record.Event.EventSeq,
		); err != nil {
			return err
		}
	}
	_, err = tx.ExecContext(
		ctx,
		`INSERT INTO gateway_event_outbox (
		   event_id, task_id, event_seq, event_type, callback_url, payload_json,
		   status, attempts, last_error, next_attempt_at, created_at, updated_at
		 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		 ON DUPLICATE KEY UPDATE
		   callback_url = VALUES(callback_url),
		   payload_json = VALUES(payload_json),
		   status = VALUES(status),
		   updated_at = VALUES(updated_at)`,
		record.Event.EventID,
		record.Event.TaskID,
		record.Event.EventSeq,
		record.Event.EventType,
		record.CallbackURL,
		payload,
		record.Status,
		record.Attempts,
		emptyToNil(record.LastError),
		record.NextAttemptAt,
		record.CreatedAt,
		record.UpdatedAt,
	)
	if err != nil {
		return err
	}
	return tx.Commit()
}

func (s *SQLStore) SupersedePendingRetrying(ctx context.Context, now time.Time) (int, error) {
	result, err := s.db.ExecContext(
		ctx,
		`UPDATE gateway_event_outbox older
		 JOIN gateway_event_outbox newer
		   ON newer.task_id = older.task_id
		  AND newer.callback_url = older.callback_url
		  AND newer.event_type = 'task.retrying'
		  AND newer.status = 'pending'
		  AND newer.event_seq > older.event_seq
		 SET older.status = 'dead',
		     older.last_error = 'superseded by newer retrying callback event',
		     older.next_attempt_at = NULL,
		     older.locked_by = NULL,
		     older.locked_until = NULL,
		     older.updated_at = ?
		 WHERE older.event_type = 'task.retrying'
		   AND older.status = 'pending'`,
		now,
	)
	if err != nil {
		return 0, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return 0, err
	}
	return int(affected), nil
}

func (s *SQLStore) ClaimPending(
	ctx context.Context,
	now time.Time,
	limit int,
	workerID string,
	leaseDuration time.Duration,
) ([]webhook.DeliveryRecord, error) {
	if limit <= 0 {
		limit = 100
	}
	if leaseDuration <= 0 {
		leaseDuration = 30 * time.Second
	}
	workerID = strings.TrimSpace(workerID)
	if workerID == "" {
		workerID = "go-gateway"
	}
	lockedUntil := now.Add(leaseDuration)

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()

	rows, err := tx.QueryContext(
		ctx,
		`SELECT event_id
			 FROM gateway_event_outbox
			 WHERE status = 'pending'
			   AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
			   AND NOT EXISTS (
			     SELECT 1
			     FROM gateway_event_outbox earlier
			     WHERE earlier.task_id = gateway_event_outbox.task_id
			       AND earlier.event_seq < gateway_event_outbox.event_seq
			       AND earlier.status NOT IN ('delivered', 'dead')
			   )
			 ORDER BY created_at ASC, event_seq ASC
			 LIMIT ?`,
		now,
		limit,
	)
	if err != nil {
		return nil, err
	}
	var eventIDs []string
	for rows.Next() {
		var eventID string
		if scanErr := rows.Scan(&eventID); scanErr != nil {
			_ = rows.Close()
			return nil, scanErr
		}
		eventIDs = append(eventIDs, eventID)
	}
	if err = rows.Close(); err != nil {
		return nil, err
	}
	if err = rows.Err(); err != nil {
		return nil, err
	}

	records := make([]webhook.DeliveryRecord, 0, len(eventIDs))
	for _, eventID := range eventIDs {
		result, execErr := tx.ExecContext(
			ctx,
			`UPDATE gateway_event_outbox
			 SET status = 'sending', locked_by = ?, locked_until = ?, updated_at = ?
			 WHERE event_id = ? AND status = 'pending'
			   AND (next_attempt_at IS NULL OR next_attempt_at <= ?)`,
			workerID,
			lockedUntil,
			now,
			eventID,
			now,
		)
		if execErr != nil {
			err = execErr
			return nil, err
		}
		affected, execErr := result.RowsAffected()
		if execErr != nil {
			err = execErr
			return nil, err
		}
		if affected == 0 {
			continue
		}

		row := tx.QueryRowContext(
			ctx,
			`SELECT payload_json, callback_url, status, attempts, last_error,
			        next_attempt_at, locked_by, locked_until, created_at, updated_at,
			        delivered_at, acked_at
			 FROM gateway_event_outbox
			 WHERE event_id = ?`,
			eventID,
		)
		record, scanErr := scanDeliveryRecord(row)
		if scanErr != nil {
			err = scanErr
			return nil, err
		}
		records = append(records, record)
	}
	err = tx.Commit()
	return records, err
}

func (s *SQLStore) RecoverStuck(ctx context.Context, now time.Time) (int, error) {
	result, err := s.db.ExecContext(
		ctx,
		`UPDATE gateway_event_outbox
		 SET status = 'pending', locked_by = NULL, locked_until = NULL, updated_at = ?
		 WHERE status = 'sending' AND locked_until IS NOT NULL AND locked_until <= ?`,
		now,
		now,
	)
	if err != nil {
		return 0, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return 0, err
	}
	return int(affected), nil
}

func requireOutboxLease(result sql.Result, err error) error {
	if err != nil {
		return err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if affected == 0 {
		return errors.New("callback outbox lease lost")
	}
	return nil
}

func (s *SQLStore) MarkAttempt(
	ctx context.Context,
	record webhook.DeliveryRecord,
	nextAttemptAt time.Time,
) error {
	record.UpdatedAt = time.Now().UTC()
	result, err := s.db.ExecContext(
		ctx,
		`UPDATE gateway_event_outbox
		 SET status = 'pending', attempts = ?, last_error = ?, next_attempt_at = ?,
		     locked_by = NULL, locked_until = NULL, updated_at = ?
		 WHERE event_id = ? AND status = 'sending' AND locked_by = ?`,
		record.Attempts,
		emptyToNil(record.LastError),
		nextAttemptAt,
		record.UpdatedAt,
		record.Event.EventID,
		record.LockedBy,
	)
	if err := requireOutboxLease(result, err); err != nil {
		return err
	}
	return s.insertDelivery(ctx, record, "failed", record.LastError)
}

func (s *SQLStore) MarkDelivered(ctx context.Context, record webhook.DeliveryRecord) error {
	now := time.Now().UTC()
	result, err := s.db.ExecContext(
		ctx,
		`UPDATE gateway_event_outbox
		 SET status = 'delivered', attempts = ?, last_error = NULL,
		     next_attempt_at = NULL, locked_by = NULL, locked_until = NULL,
		     delivered_at = ?, updated_at = ?
		 WHERE event_id = ? AND status = 'sending' AND locked_by = ?`,
		record.Attempts,
		now,
		now,
		record.Event.EventID,
		record.LockedBy,
	)
	if err := requireOutboxLease(result, err); err != nil {
		return err
	}
	record.DeliveredAt = &now
	return s.insertDelivery(ctx, record, "delivered", "")
}

func (s *SQLStore) MarkDead(ctx context.Context, record webhook.DeliveryRecord, lastError string) error {
	now := time.Now().UTC()
	payload, err := json.Marshal(record.Event)
	if err != nil {
		return fmt.Errorf("marshal webhook dead-letter event: %w", err)
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	result, err := tx.ExecContext(
		ctx,
		`UPDATE gateway_event_outbox
		 SET status = 'dead', attempts = ?, last_error = ?, next_attempt_at = NULL,
		     locked_by = NULL, locked_until = NULL, updated_at = ?
		 WHERE event_id = ? AND status = 'sending' AND locked_by = ?`,
		record.Attempts,
		lastError,
		now,
		record.Event.EventID,
		record.LockedBy,
	)
	if err := requireOutboxLease(result, err); err != nil {
		return err
	}
	_, err = tx.ExecContext(
		ctx,
		`INSERT INTO gateway_webhook_deliveries (
			   event_id, callback_url, attempt, status, error, created_at,
			   delivery_mode, request_packet, response_observed,
			   response_status_code, response_status, response_content_type,
			   response_content_length, response_body, response_body_bytes,
			   response_body_read_error, response_body_truncated,
			   response_packet, duration_ms
			 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
			 ON DUPLICATE KEY UPDATE
			   status = VALUES(status),
			   error = VALUES(error),
			   delivery_mode = VALUES(delivery_mode),
			   request_packet = VALUES(request_packet),
			   response_observed = VALUES(response_observed),
			   response_status_code = VALUES(response_status_code),
			   response_status = VALUES(response_status),
			   response_content_type = VALUES(response_content_type),
			   response_content_length = VALUES(response_content_length),
			   response_body = VALUES(response_body),
			   response_body_bytes = VALUES(response_body_bytes),
			   response_body_read_error = VALUES(response_body_read_error),
			   response_body_truncated = VALUES(response_body_truncated),
			   response_packet = VALUES(response_packet),
			   duration_ms = VALUES(duration_ms)`,
		record.Event.EventID,
		record.CallbackURL,
		record.Attempts,
		"failed",
		lastError,
		now,
		emptyToNil(record.DeliveryMode),
		emptyToNil(record.RequestPacket),
		record.ResponseObserved,
		zeroIntToNil(record.ResponseStatusCode),
		emptyToNil(record.ResponseStatus),
		emptyToNil(record.ResponseContentType),
		zeroInt64ToNil(record.ResponseContentLength),
		emptyToNil(record.ResponseBody),
		zeroIntToNil(record.ResponseBodyBytes),
		emptyToNil(record.ResponseBodyReadError),
		record.ResponseBodyTruncated,
		emptyToNil(record.ResponsePacket),
		zeroInt64ToNil(record.ResponseDurationMillis),
	)
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(
		ctx,
		`INSERT INTO gateway_dead_letters (
		   event_id, task_id, event_seq, event_type, callback_url,
		   attempts, last_error, payload_json, failed_at
		 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
		 ON DUPLICATE KEY UPDATE
		   attempts = VALUES(attempts),
		   last_error = VALUES(last_error),
		   payload_json = VALUES(payload_json),
		   failed_at = VALUES(failed_at)`,
		record.Event.EventID,
		record.Event.TaskID,
		record.Event.EventSeq,
		record.Event.EventType,
		record.CallbackURL,
		record.Attempts,
		lastError,
		payload,
		now,
	)
	if err != nil {
		return err
	}
	err = tx.Commit()
	return err
}

func (s *SQLStore) Acknowledge(ctx context.Context, ack webhook.ACK) (webhook.ACK, bool, error) {
	ack.EventID = strings.TrimSpace(ack.EventID)
	ack.TaskID = strings.TrimSpace(ack.TaskID)
	if ack.EventID == "" || ack.TaskID == "" {
		return webhook.ACK{}, false, nil
	}
	now := time.Now().UTC()
	result, err := s.db.ExecContext(
		ctx,
		`UPDATE gateway_event_outbox
		 SET acked_at = ?, updated_at = ?
		 WHERE event_id = ? AND task_id = ?`,
		now,
		now,
		ack.EventID,
		ack.TaskID,
	)
	if err != nil {
		return webhook.ACK{}, false, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return webhook.ACK{}, false, err
	}
	if affected == 0 {
		return webhook.ACK{}, false, nil
	}
	return ack, true, nil
}

func (s *SQLStore) DeadLetters(ctx context.Context, limit int) ([]webhook.DeadLetter, error) {
	if limit <= 0 {
		limit = 100
	}
	rows, err := s.db.QueryContext(
		ctx,
		`SELECT payload_json, callback_url, attempts, last_error, failed_at
		 FROM gateway_dead_letters
		 ORDER BY failed_at DESC
		 LIMIT ?`,
		limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	letters := []webhook.DeadLetter{}
	for rows.Next() {
		var payload []byte
		var letter webhook.DeadLetter
		if err := rows.Scan(
			&payload,
			&letter.CallbackURL,
			&letter.Attempts,
			&letter.LastError,
			&letter.FailedAt,
		); err != nil {
			return nil, err
		}
		if err := json.Unmarshal(payload, &letter.Event); err != nil {
			return nil, fmt.Errorf("unmarshal dead-letter event: %w", err)
		}
		letters = append(letters, letter)
	}
	return letters, rows.Err()
}

func (s *SQLStore) ReplayDeadLetter(ctx context.Context, eventID string, now time.Time) (webhook.DeliveryRecord, bool, error) {
	eventID = strings.TrimSpace(eventID)
	if eventID == "" {
		return webhook.DeliveryRecord{}, false, nil
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return webhook.DeliveryRecord{}, false, err
	}
	defer tx.Rollback()
	row := tx.QueryRowContext(
		ctx,
		`SELECT payload_json, callback_url, status, attempts, last_error,
		        next_attempt_at, locked_by, locked_until, created_at, updated_at,
		        delivered_at, acked_at
		 FROM gateway_event_outbox
		 WHERE event_id = ? AND status = 'dead'`,
		eventID,
	)
	record, err := scanDeliveryRecord(row)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return webhook.DeliveryRecord{}, false, nil
		}
		return webhook.DeliveryRecord{}, false, err
	}
	_, err = tx.ExecContext(
		ctx,
		`UPDATE gateway_event_outbox
		 SET status = 'pending', attempts = 0, last_error = NULL,
		     next_attempt_at = NULL, locked_by = NULL, locked_until = NULL,
		     updated_at = ?
		 WHERE event_id = ?`,
		now,
		eventID,
	)
	if err != nil {
		return webhook.DeliveryRecord{}, false, err
	}
	_, err = tx.ExecContext(ctx, `DELETE FROM gateway_dead_letters WHERE event_id = ?`, eventID)
	if err != nil {
		return webhook.DeliveryRecord{}, false, err
	}
	err = tx.Commit()
	if err != nil {
		return webhook.DeliveryRecord{}, false, err
	}
	record.Status = "pending"
	record.Attempts = 0
	record.LastError = ""
	record.NextAttemptAt = nil
	record.LockedBy = ""
	record.LockedUntil = nil
	record.UpdatedAt = now
	return record, true, nil
}

func (s *SQLStore) insertDelivery(
	ctx context.Context,
	record webhook.DeliveryRecord,
	status string,
	lastError string,
) error {
	_, err := s.db.ExecContext(
		ctx,
		`INSERT INTO gateway_webhook_deliveries (
		   event_id, callback_url, attempt, status, error, created_at,
		   delivery_mode, request_packet, response_observed,
		   response_status_code, response_status, response_content_type,
		   response_content_length, response_body, response_body_bytes,
		   response_body_read_error, response_body_truncated,
		   response_packet, duration_ms
		 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		 ON DUPLICATE KEY UPDATE
		   status = VALUES(status),
		   error = VALUES(error),
		   delivery_mode = VALUES(delivery_mode),
		   request_packet = VALUES(request_packet),
		   response_observed = VALUES(response_observed),
		   response_status_code = VALUES(response_status_code),
		   response_status = VALUES(response_status),
		   response_content_type = VALUES(response_content_type),
		   response_content_length = VALUES(response_content_length),
		   response_body = VALUES(response_body),
		   response_body_bytes = VALUES(response_body_bytes),
		   response_body_read_error = VALUES(response_body_read_error),
		   response_body_truncated = VALUES(response_body_truncated),
		   response_packet = VALUES(response_packet),
		   duration_ms = VALUES(duration_ms)`,
		record.Event.EventID,
		record.CallbackURL,
		record.Attempts,
		status,
		emptyToNil(lastError),
		time.Now().UTC(),
		emptyToNil(record.DeliveryMode),
		emptyToNil(record.RequestPacket),
		record.ResponseObserved,
		zeroIntToNil(record.ResponseStatusCode),
		emptyToNil(record.ResponseStatus),
		emptyToNil(record.ResponseContentType),
		zeroInt64ToNil(record.ResponseContentLength),
		emptyToNil(record.ResponseBody),
		zeroIntToNil(record.ResponseBodyBytes),
		emptyToNil(record.ResponseBodyReadError),
		record.ResponseBodyTruncated,
		emptyToNil(record.ResponsePacket),
		zeroInt64ToNil(record.ResponseDurationMillis),
	)
	return err
}

func zeroIntToNil(value int) any {
	if value == 0 {
		return nil
	}
	return value
}

func zeroInt64ToNil(value int64) any {
	if value == 0 {
		return nil
	}
	return value
}

type deliveryScanner interface {
	Scan(dest ...any) error
}

func scanDeliveryRecord(row deliveryScanner) (webhook.DeliveryRecord, error) {
	var payload []byte
	var record webhook.DeliveryRecord
	var status sql.NullString
	var lastError sql.NullString
	var nextAttemptAt sql.NullTime
	var lockedBy sql.NullString
	var lockedUntil sql.NullTime
	var deliveredAt sql.NullTime
	var ackedAt sql.NullTime
	if err := row.Scan(
		&payload,
		&record.CallbackURL,
		&status,
		&record.Attempts,
		&lastError,
		&nextAttemptAt,
		&lockedBy,
		&lockedUntil,
		&record.CreatedAt,
		&record.UpdatedAt,
		&deliveredAt,
		&ackedAt,
	); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return webhook.DeliveryRecord{}, err
		}
		return webhook.DeliveryRecord{}, err
	}
	if err := json.Unmarshal(payload, &record.Event); err != nil {
		return webhook.DeliveryRecord{}, fmt.Errorf("unmarshal webhook event: %w", err)
	}
	record.Status = status.String
	record.LastError = lastError.String
	if nextAttemptAt.Valid {
		record.NextAttemptAt = &nextAttemptAt.Time
	}
	record.LockedBy = lockedBy.String
	if lockedUntil.Valid {
		record.LockedUntil = &lockedUntil.Time
	}
	if deliveredAt.Valid {
		record.Delivered = true
		record.DeliveredAt = &deliveredAt.Time
	}
	record.Acked = ackedAt.Valid
	return record, nil
}
