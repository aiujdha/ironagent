package taskstore

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	"ironagent/go-gateway/internal/runtimepool"
)

const upsertRuntimeSQL = `INSERT INTO gateway_runtimes (
		   runtime_id, assistant_id, pod_name, internal_base_url, gateway_url,
		   status, capacity, in_flight,
		   capabilities_json, drain_mode, last_heartbeat_at, last_error,
		   version, updated_at
		 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
		 ON DUPLICATE KEY UPDATE
		   runtime_id = VALUES(runtime_id),
		   pod_name = VALUES(pod_name),
			   internal_base_url = COALESCE(VALUES(internal_base_url), gateway_runtimes.internal_base_url),
			   gateway_url = COALESCE(VALUES(gateway_url), gateway_runtimes.gateway_url),
			   status = VALUES(status),
			   capacity = VALUES(capacity),
			   in_flight = gateway_runtimes.in_flight,
			   capabilities_json = VALUES(capabilities_json),
			   drain_mode = VALUES(drain_mode),
			   last_heartbeat_at = VALUES(last_heartbeat_at),
		   last_error = VALUES(last_error),
		   version = version + 1,
		   updated_at = VALUES(updated_at)`

const upsertRuntimeInstanceSQL = `INSERT INTO gateway_runtime_instances (
		   runtime_id, assistant_id, pod_name, internal_base_url, gateway_url,
		   status, capacity, in_flight,
		   capabilities_json, drain_mode, last_heartbeat_at, last_error,
		   version, updated_at
		 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
		 ON DUPLICATE KEY UPDATE
		   assistant_id = VALUES(assistant_id),
		   pod_name = VALUES(pod_name),
		   internal_base_url = COALESCE(VALUES(internal_base_url), gateway_runtime_instances.internal_base_url),
		   gateway_url = COALESCE(VALUES(gateway_url), gateway_runtime_instances.gateway_url),
		   status = VALUES(status),
		   capacity = VALUES(capacity),
		   in_flight = gateway_runtime_instances.in_flight,
		   capabilities_json = VALUES(capabilities_json),
		   drain_mode = VALUES(drain_mode),
		   last_heartbeat_at = VALUES(last_heartbeat_at),
		   last_error = VALUES(last_error),
		   version = version + 1,
		   updated_at = VALUES(updated_at)`

const heartbeatRuntimeSQL = `INSERT INTO gateway_runtimes (
		   runtime_id, assistant_id, pod_name, internal_base_url, gateway_url,
		   status, capacity, in_flight,
		   capabilities_json, drain_mode, last_heartbeat_at, last_error,
		   version, updated_at
		 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
		 ON DUPLICATE KEY UPDATE
		   runtime_id = VALUES(runtime_id),
		   pod_name = VALUES(pod_name),
		   internal_base_url = COALESCE(VALUES(internal_base_url), gateway_runtimes.internal_base_url),
		   gateway_url = COALESCE(VALUES(gateway_url), gateway_runtimes.gateway_url),
		   status = VALUES(status),
		   capacity = gateway_runtimes.capacity,
		   in_flight = gateway_runtimes.in_flight,
		   capabilities_json = VALUES(capabilities_json),
			   drain_mode = VALUES(drain_mode),
		   last_heartbeat_at = VALUES(last_heartbeat_at),
		   last_error = CASE
		     WHEN VALUES(status) IN ('ready', 'running', 'healthy')
		       AND (VALUES(last_error) IS NULL OR VALUES(last_error) = '') THEN NULL
		     WHEN VALUES(last_error) IS NULL OR VALUES(last_error) = '' THEN gateway_runtimes.last_error
		     ELSE VALUES(last_error)
		   END,
		   version = version + 1,
		   updated_at = VALUES(updated_at)`

const heartbeatRuntimeInstanceSQL = `INSERT INTO gateway_runtime_instances (
		   runtime_id, assistant_id, pod_name, internal_base_url, gateway_url,
		   status, capacity, in_flight,
		   capabilities_json, drain_mode, last_heartbeat_at, last_error,
		   version, updated_at
		 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
		 ON DUPLICATE KEY UPDATE
		   assistant_id = VALUES(assistant_id),
		   pod_name = VALUES(pod_name),
		   internal_base_url = COALESCE(VALUES(internal_base_url), gateway_runtime_instances.internal_base_url),
		   gateway_url = COALESCE(VALUES(gateway_url), gateway_runtime_instances.gateway_url),
		   status = VALUES(status),
		   capacity = gateway_runtime_instances.capacity,
		   in_flight = gateway_runtime_instances.in_flight,
		   capabilities_json = VALUES(capabilities_json),
			   drain_mode = VALUES(drain_mode),
		   last_heartbeat_at = VALUES(last_heartbeat_at),
		   last_error = CASE
		     WHEN VALUES(status) IN ('ready', 'running', 'healthy')
		       AND (VALUES(last_error) IS NULL OR VALUES(last_error) = '') THEN NULL
		     WHEN VALUES(last_error) IS NULL OR VALUES(last_error) = '' THEN gateway_runtime_instances.last_error
		     ELSE VALUES(last_error)
		   END,
		   version = version + 1,
		   updated_at = VALUES(updated_at)`

const insertRuntimeHeartbeatSQL = `INSERT INTO gateway_runtime_heartbeats (
		   assistant_id, runtime_id, pod_name, internal_base_url, gateway_url,
		   status, capacity, in_flight, last_error, received_at
		 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`

const upsertRuntimeAggregateSQL = `INSERT INTO gateway_runtimes (
		   runtime_id, assistant_id, pod_name, internal_base_url, gateway_url,
		   status, capacity, in_flight,
		   capabilities_json, drain_mode, last_heartbeat_at, last_error,
		   version, updated_at
		 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
		 ON DUPLICATE KEY UPDATE
		   runtime_id = VALUES(runtime_id),
		   pod_name = VALUES(pod_name),
		   internal_base_url = VALUES(internal_base_url),
		   gateway_url = VALUES(gateway_url),
		   status = VALUES(status),
		   capacity = VALUES(capacity),
		   in_flight = VALUES(in_flight),
		   capabilities_json = VALUES(capabilities_json),
		   drain_mode = VALUES(drain_mode),
		   last_heartbeat_at = VALUES(last_heartbeat_at),
		   last_error = VALUES(last_error),
		   version = version + 1,
		   updated_at = VALUES(updated_at)`

const selectRuntimeInstanceForLeaseSQL = `SELECT runtime_id, assistant_id, pod_name, status, capacity, in_flight,
		        internal_base_url, gateway_url, capabilities_json, drain_mode, last_heartbeat_at, last_error,
		        version, updated_at
		 FROM gateway_runtime_instances
		 WHERE assistant_id = ?
		   AND drain_mode = FALSE
		   AND status IN ('ready', 'running', 'healthy')
		   AND in_flight < capacity
			 AND (? <= 0 OR last_heartbeat_at IS NULL OR last_heartbeat_at >= ?)
			 ORDER BY (in_flight / GREATEST(capacity, 1)) ASC, in_flight ASC, runtime_id ASC
			 LIMIT 1
			 FOR UPDATE SKIP LOCKED`

const incrementRuntimeInstanceInFlightSQL = `UPDATE gateway_runtime_instances
		 SET in_flight = in_flight + 1, version = version + 1, updated_at = ?
		 WHERE runtime_id = ?
		   AND assistant_id = ?
		   AND drain_mode = FALSE
		   AND status IN ('ready', 'running', 'healthy')
		   AND in_flight < capacity
		   AND (? <= 0 OR last_heartbeat_at IS NULL OR last_heartbeat_at >= ?)`

const insertRuntimeLeaseSQL = `INSERT INTO gateway_runtime_leases (
		   lease_id, runtime_id, assistant_id, status, acquired_at, lease_expires_at, released_at, updated_at
		 ) VALUES (?, ?, ?, 'active', ?, ?, NULL, ?)`

func (s *SQLStore) UpsertRuntime(
	ctx context.Context,
	record runtimepool.RuntimeRecord,
) (runtimepool.RuntimeRecord, error) {
	record.AssistantID = strings.TrimSpace(record.AssistantID)
	if record.AssistantID == "" {
		return runtimepool.RuntimeRecord{}, fmt.Errorf("assistant_id is required")
	}
	if record.RuntimeID == "" {
		record.RuntimeID = record.AssistantID
	}
	if record.Status == "" {
		record.Status = "unknown"
	}
	now := time.Now().UTC()
	if record.UpdatedAt.IsZero() {
		record.UpdatedAt = now
	}
	if record.LastHeartbeatAt == nil {
		record.LastHeartbeatAt = &now
	}
	capabilitiesJSON, err := json.Marshal(record.Capabilities)
	if err != nil {
		return runtimepool.RuntimeRecord{}, fmt.Errorf("marshal runtime capabilities: %w", err)
	}

	_, err = s.db.ExecContext(
		ctx,
		upsertRuntimeInstanceSQL,
		record.RuntimeID,
		record.AssistantID,
		emptyToNil(record.PodName),
		emptyToNil(record.InternalBaseURL),
		emptyToNil(record.GatewayURL),
		record.Status,
		record.Capacity,
		record.InFlight,
		capabilitiesJSON,
		record.DrainMode,
		record.LastHeartbeatAt,
		emptyToNil(record.LastError),
		record.UpdatedAt,
	)
	if err != nil {
		return runtimepool.RuntimeRecord{}, err
	}
	if err := s.recomputeRuntimeAggregate(ctx, record.AssistantID); err != nil {
		return runtimepool.RuntimeRecord{}, err
	}
	if _, err := s.db.ExecContext(
		ctx,
		insertRuntimeHeartbeatSQL,
		record.AssistantID,
		record.RuntimeID,
		emptyToNil(record.PodName),
		emptyToNil(record.InternalBaseURL),
		emptyToNil(record.GatewayURL),
		record.Status,
		record.Capacity,
		record.InFlight,
		emptyToNil(record.LastError),
		record.LastHeartbeatAt,
	); err != nil {
		return runtimepool.RuntimeRecord{}, err
	}
	updated, ok, err := s.GetRuntime(ctx, record.AssistantID)
	if err != nil || !ok {
		return runtimepool.RuntimeRecord{}, err
	}
	return updated, nil
}

func (s *SQLStore) HeartbeatRuntime(
	ctx context.Context,
	record runtimepool.RuntimeRecord,
) (runtimepool.RuntimeRecord, error) {
	record.AssistantID = strings.TrimSpace(record.AssistantID)
	if record.AssistantID == "" {
		return runtimepool.RuntimeRecord{}, fmt.Errorf("assistant_id is required")
	}
	if record.RuntimeID == "" {
		record.RuntimeID = record.AssistantID
	}
	if record.Status == "" {
		record.Status = "unknown"
	}
	now := time.Now().UTC()
	if record.UpdatedAt.IsZero() {
		record.UpdatedAt = now
	}
	if record.LastHeartbeatAt == nil {
		record.LastHeartbeatAt = &now
	}
	capabilitiesJSON, err := json.Marshal(record.Capabilities)
	if err != nil {
		return runtimepool.RuntimeRecord{}, fmt.Errorf("marshal runtime capabilities: %w", err)
	}

	_, err = s.db.ExecContext(
		ctx,
		heartbeatRuntimeInstanceSQL,
		record.RuntimeID,
		record.AssistantID,
		emptyToNil(record.PodName),
		emptyToNil(record.InternalBaseURL),
		emptyToNil(record.GatewayURL),
		record.Status,
		record.Capacity,
		record.InFlight,
		capabilitiesJSON,
		record.DrainMode,
		record.LastHeartbeatAt,
		emptyToNil(record.LastError),
		record.UpdatedAt,
	)
	if err != nil {
		return runtimepool.RuntimeRecord{}, err
	}
	if err := s.recomputeRuntimeAggregate(ctx, record.AssistantID); err != nil {
		return runtimepool.RuntimeRecord{}, err
	}
	if _, err := s.db.ExecContext(
		ctx,
		insertRuntimeHeartbeatSQL,
		record.AssistantID,
		record.RuntimeID,
		emptyToNil(record.PodName),
		emptyToNil(record.InternalBaseURL),
		emptyToNil(record.GatewayURL),
		record.Status,
		record.Capacity,
		record.InFlight,
		emptyToNil(record.LastError),
		record.LastHeartbeatAt,
	); err != nil {
		return runtimepool.RuntimeRecord{}, err
	}
	updated, ok, err := s.GetRuntime(ctx, record.AssistantID)
	if err != nil || !ok {
		return runtimepool.RuntimeRecord{}, err
	}
	return updated, nil
}

func (s *SQLStore) GetRuntime(
	ctx context.Context,
	assistantID string,
) (runtimepool.RuntimeRecord, bool, error) {
	assistantID = strings.TrimSpace(assistantID)
	if assistantID == "" {
		return runtimepool.RuntimeRecord{}, false, nil
	}
	row := s.db.QueryRowContext(
		ctx,
		`SELECT runtime_id, assistant_id, pod_name, status, capacity, in_flight,
		        internal_base_url, gateway_url, capabilities_json, drain_mode, last_heartbeat_at, last_error,
		        version, updated_at
		 FROM gateway_runtimes WHERE assistant_id = ?`,
		assistantID,
	)
	record, err := scanRuntimeRecord(row)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return runtimepool.RuntimeRecord{}, false, nil
		}
		return runtimepool.RuntimeRecord{}, false, err
	}
	return record, true, nil
}

func (s *SQLStore) ListRuntimes(ctx context.Context) ([]runtimepool.RuntimeRecord, error) {
	rows, err := s.db.QueryContext(
		ctx,
		`SELECT runtime_id, assistant_id, pod_name, status, capacity, in_flight,
		        internal_base_url, gateway_url, capabilities_json, drain_mode, last_heartbeat_at, last_error,
		        version, updated_at
		 FROM gateway_runtimes ORDER BY assistant_id`,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	records := []runtimepool.RuntimeRecord{}
	for rows.Next() {
		record, err := scanRuntimeRecord(rows)
		if err != nil {
			return nil, err
		}
		records = append(records, record)
	}
	return records, rows.Err()
}

func (s *SQLStore) listRuntimeInstances(ctx context.Context, assistantID string) ([]runtimepool.RuntimeRecord, error) {
	assistantID = strings.TrimSpace(assistantID)
	if assistantID == "" {
		return nil, nil
	}
	rows, err := s.db.QueryContext(
		ctx,
		`SELECT runtime_id, assistant_id, pod_name, status, capacity, in_flight,
		        internal_base_url, gateway_url, capabilities_json, drain_mode, last_heartbeat_at, last_error,
		        version, updated_at
		 FROM gateway_runtime_instances WHERE assistant_id = ? ORDER BY runtime_id`,
		assistantID,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	records := []runtimepool.RuntimeRecord{}
	for rows.Next() {
		record, err := scanRuntimeRecord(rows)
		if err != nil {
			return nil, err
		}
		records = append(records, record)
	}
	return records, rows.Err()
}

func (s *SQLStore) recomputeRuntimeAggregate(ctx context.Context, assistantID string) error {
	assistantID = strings.TrimSpace(assistantID)
	if assistantID == "" {
		return nil
	}
	records, err := s.listRuntimeInstances(ctx, assistantID)
	if err != nil {
		return err
	}
	if len(records) == 0 {
		_, err := s.db.ExecContext(ctx, `DELETE FROM gateway_runtimes WHERE assistant_id = ?`, assistantID)
		return err
	}

	now := time.Now().UTC()
	selected := records[0]
	available := []runtimepool.RuntimeRecord{}
	capabilities := map[string]struct{}{}
	capacityTotal := 0
	inFlightTotal := 0
	var latestHeartbeat *time.Time
	var latestUpdated time.Time
	for _, record := range records {
		if record.UpdatedAt.After(latestUpdated) {
			latestUpdated = record.UpdatedAt
		}
		if record.LastHeartbeatAt != nil {
			if latestHeartbeat == nil || record.LastHeartbeatAt.After(*latestHeartbeat) {
				copied := *record.LastHeartbeatAt
				latestHeartbeat = &copied
			}
		}
		for _, capability := range record.Capabilities {
			capability = strings.ToLower(strings.TrimSpace(capability))
			if capability != "" {
				capabilities[capability] = struct{}{}
			}
		}
		if runtimeRecordAvailableForAggregate(record) {
			available = append(available, record)
			capacityTotal += maxInt(record.Capacity, 0)
			inFlightTotal += maxInt(record.InFlight, 0)
		}
	}
	if len(available) > 0 {
		sort.SliceStable(available, func(i, j int) bool {
			left := available[i]
			right := available[j]
			leftLoad := left.InFlight * maxInt(right.Capacity, 1)
			rightLoad := right.InFlight * maxInt(left.Capacity, 1)
			if leftLoad != rightLoad {
				return leftLoad < rightLoad
			}
			return strings.TrimSpace(left.RuntimeID) < strings.TrimSpace(right.RuntimeID)
		})
		selected = available[0]
		selected.Status = "ready"
		selected.Capacity = capacityTotal
		selected.InFlight = inFlightTotal
		selected.DrainMode = false
	} else {
		capacityTotal = 0
		inFlightTotal = 0
		for _, record := range records {
			capacityTotal += maxInt(record.Capacity, 0)
			inFlightTotal += maxInt(record.InFlight, 0)
			if record.UpdatedAt.After(selected.UpdatedAt) {
				selected = record
			}
		}
		selected.Capacity = capacityTotal
		selected.InFlight = inFlightTotal
		selected.DrainMode = true
		if strings.TrimSpace(selected.Status) == "" {
			selected.Status = "unhealthy"
		}
	}
	if latestHeartbeat != nil {
		selected.LastHeartbeatAt = latestHeartbeat
	}
	if !latestUpdated.IsZero() {
		selected.UpdatedAt = latestUpdated
	} else {
		selected.UpdatedAt = now
	}
	selected.AssistantID = assistantID
	selected.Capabilities = sortedCapabilityKeys(capabilities)

	capabilitiesJSON, err := json.Marshal(selected.Capabilities)
	if err != nil {
		return fmt.Errorf("marshal runtime aggregate capabilities: %w", err)
	}
	_, err = s.db.ExecContext(
		ctx,
		upsertRuntimeAggregateSQL,
		selected.RuntimeID,
		selected.AssistantID,
		emptyToNil(selected.PodName),
		emptyToNil(selected.InternalBaseURL),
		emptyToNil(selected.GatewayURL),
		selected.Status,
		selected.Capacity,
		selected.InFlight,
		capabilitiesJSON,
		selected.DrainMode,
		selected.LastHeartbeatAt,
		emptyToNil(selected.LastError),
		now,
	)
	return err
}

func (s *SQLStore) DeleteRuntime(ctx context.Context, assistantID string) error {
	assistantID = strings.TrimSpace(assistantID)
	if assistantID == "" {
		return nil
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, statement := range []string{
		`UPDATE gateway_runtime_leases SET status = 'released', released_at = ? WHERE assistant_id = ? AND status = 'active'`,
		`DELETE FROM gateway_runtime_instances WHERE assistant_id = ?`,
		`DELETE FROM gateway_runtimes WHERE assistant_id = ?`,
	} {
		if strings.Contains(statement, "released_at") {
			if _, err := tx.ExecContext(ctx, statement, time.Now().UTC(), assistantID); err != nil {
				return err
			}
			continue
		}
		if _, err := tx.ExecContext(ctx, statement, assistantID); err != nil {
			return err
		}
	}
	return tx.Commit()
}

func (s *SQLStore) ReserveRuntime(
	ctx context.Context,
	assistantID string,
	heartbeatTTL time.Duration,
) error {
	assistantID = strings.TrimSpace(assistantID)
	if assistantID == "" {
		return fmt.Errorf("assistant_id is required")
	}
	now := time.Now().UTC()
	threshold := now.Add(-heartbeatTTL)
	result, err := s.db.ExecContext(
		ctx,
		`UPDATE gateway_runtimes
		 SET in_flight = in_flight + 1, version = version + 1, updated_at = ?
		 WHERE assistant_id = ?
		   AND drain_mode = FALSE
		   AND status IN ('ready', 'running', 'healthy')
		   AND in_flight < capacity
		   AND (? <= 0 OR last_heartbeat_at IS NULL OR last_heartbeat_at >= ?)`,
		now,
		assistantID,
		int64(heartbeatTTL),
		threshold,
	)
	if err != nil {
		return err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if affected > 0 {
		return nil
	}
	if _, ok, err := s.GetRuntime(ctx, assistantID); err != nil || !ok {
		return err
	}
	return fmt.Errorf("target assistant %q is not ready or is at capacity", assistantID)
}

func (s *SQLStore) ReleaseRuntime(ctx context.Context, assistantID string) error {
	assistantID = strings.TrimSpace(assistantID)
	if assistantID == "" {
		return nil
	}
	_, err := s.db.ExecContext(
		ctx,
		`UPDATE gateway_runtimes
			 SET in_flight = GREATEST(in_flight - 1, 0),
			     version = version + 1,
			     updated_at = ?
			 WHERE assistant_id = ? AND in_flight > 0`,
		time.Now().UTC(),
		assistantID,
	)
	return err
}

func (s *SQLStore) ReserveRuntimeRoute(
	ctx context.Context,
	assistantID string,
	heartbeatTTL time.Duration,
) (runtimepool.RuntimeRecord, string, *time.Time, error) {
	assistantID = strings.TrimSpace(assistantID)
	if assistantID == "" {
		return runtimepool.RuntimeRecord{}, "", nil, fmt.Errorf("assistant_id is required")
	}
	now := time.Now().UTC()
	threshold := now.Add(-heartbeatTTL)
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return runtimepool.RuntimeRecord{}, "", nil, err
	}
	defer tx.Rollback()

	rows, err := tx.QueryContext(
		ctx,
		selectRuntimeInstanceForLeaseSQL,
		assistantID,
		int64(heartbeatTTL),
		threshold,
	)
	if err != nil {
		return runtimepool.RuntimeRecord{}, "", nil, err
	}
	var record runtimepool.RuntimeRecord
	if rows.Next() {
		record, err = scanRuntimeRecord(rows)
	}
	closeErr := rows.Close()
	if err != nil {
		return runtimepool.RuntimeRecord{}, "", nil, err
	}
	if closeErr != nil {
		return runtimepool.RuntimeRecord{}, "", nil, closeErr
	}
	if strings.TrimSpace(record.RuntimeID) == "" {
		if _, ok, err := s.GetRuntime(ctx, assistantID); err != nil || !ok {
			return runtimepool.RuntimeRecord{}, "", nil, err
		}
		return runtimepool.RuntimeRecord{}, "", nil, fmt.Errorf("target assistant %q is not ready or is at capacity", assistantID)
	}

	result, err := tx.ExecContext(
		ctx,
		incrementRuntimeInstanceInFlightSQL,
		now,
		record.RuntimeID,
		assistantID,
		int64(heartbeatTTL),
		threshold,
	)
	if err != nil {
		return runtimepool.RuntimeRecord{}, "", nil, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return runtimepool.RuntimeRecord{}, "", nil, err
	}
	if affected == 0 {
		return runtimepool.RuntimeRecord{}, "", nil, fmt.Errorf("target assistant %q is not ready or is at capacity", assistantID)
	}

	leaseID := newRuntimeLeaseID()
	expiresAt := runtimeLeaseExpiresAt(now, heartbeatTTL)
	_, err = tx.ExecContext(
		ctx,
		insertRuntimeLeaseSQL,
		leaseID,
		record.RuntimeID,
		assistantID,
		now,
		expiresAt,
		now,
	)
	if err != nil {
		return runtimepool.RuntimeRecord{}, "", nil, err
	}
	if err := tx.Commit(); err != nil {
		return runtimepool.RuntimeRecord{}, "", nil, err
	}
	if err := s.recomputeRuntimeAggregate(ctx, assistantID); err != nil {
		_ = s.ReleaseRuntimeRoute(context.Background(), leaseID, assistantID, record.RuntimeID)
		return runtimepool.RuntimeRecord{}, "", nil, err
	}
	updated, ok, err := s.getRuntimeInstance(ctx, record.RuntimeID)
	if err != nil || !ok {
		return runtimepool.RuntimeRecord{}, "", nil, err
	}
	return updated, leaseID, &expiresAt, nil
}

func (s *SQLStore) RenewRuntimeRouteLease(ctx context.Context, leaseID string, heartbeatTTL time.Duration) (*time.Time, error) {
	leaseID = strings.TrimSpace(leaseID)
	if leaseID == "" {
		return nil, nil
	}
	now := time.Now().UTC()
	expiresAt := runtimeLeaseExpiresAt(now, heartbeatTTL)
	result, err := s.db.ExecContext(
		ctx,
		`UPDATE gateway_runtime_leases
		 SET lease_expires_at = ?, updated_at = ?
		 WHERE lease_id = ? AND status = 'active'`,
		expiresAt,
		now,
		leaseID,
	)
	if err != nil {
		return nil, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return nil, err
	}
	if affected == 0 {
		return nil, fmt.Errorf("runtime lease %q is not active", leaseID)
	}
	return &expiresAt, nil
}

func (s *SQLStore) ReleaseRuntimeRoute(ctx context.Context, leaseID string, assistantID string, runtimeID string) error {
	leaseID = strings.TrimSpace(leaseID)
	assistantID = strings.TrimSpace(assistantID)
	runtimeID = strings.TrimSpace(runtimeID)
	if leaseID == "" {
		return s.ReleaseRuntime(ctx, assistantID)
	}
	now := time.Now().UTC()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()

	var storedRuntimeID string
	var storedAssistantID string
	row := tx.QueryRowContext(
		ctx,
		`SELECT runtime_id, assistant_id
		 FROM gateway_runtime_leases
		 WHERE lease_id = ? AND status = 'active'
		 FOR UPDATE`,
		leaseID,
	)
	if err := row.Scan(&storedRuntimeID, &storedAssistantID); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		return err
	}
	if runtimeID != "" && runtimeID != storedRuntimeID {
		return fmt.Errorf("runtime lease %q belongs to runtime %q, not %q", leaseID, storedRuntimeID, runtimeID)
	}
	if assistantID != "" && assistantID != storedAssistantID {
		return fmt.Errorf("runtime lease %q belongs to assistant %q, not %q", leaseID, storedAssistantID, assistantID)
	}

	_, err = tx.ExecContext(
		ctx,
		`UPDATE gateway_runtime_leases
		 SET status = 'released', released_at = ?, updated_at = ?
		 WHERE lease_id = ? AND status = 'active'`,
		now,
		now,
		leaseID,
	)
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(
		ctx,
		`UPDATE gateway_runtime_instances
		 SET in_flight = GREATEST(in_flight - 1, 0), version = version + 1, updated_at = ?
		 WHERE runtime_id = ? AND assistant_id = ? AND in_flight > 0`,
		now,
		storedRuntimeID,
		storedAssistantID,
	)
	if err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return err
	}
	return s.recomputeRuntimeAggregate(ctx, storedAssistantID)
}

func (s *SQLStore) getRuntimeInstance(ctx context.Context, runtimeID string) (runtimepool.RuntimeRecord, bool, error) {
	runtimeID = strings.TrimSpace(runtimeID)
	if runtimeID == "" {
		return runtimepool.RuntimeRecord{}, false, nil
	}
	row := s.db.QueryRowContext(
		ctx,
		`SELECT runtime_id, assistant_id, pod_name, status, capacity, in_flight,
		        internal_base_url, gateway_url, capabilities_json, drain_mode, last_heartbeat_at, last_error,
		        version, updated_at
		 FROM gateway_runtime_instances WHERE runtime_id = ?`,
		runtimeID,
	)
	record, err := scanRuntimeRecord(row)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return runtimepool.RuntimeRecord{}, false, nil
		}
		return runtimepool.RuntimeRecord{}, false, err
	}
	return record, true, nil
}

func (s *SQLStore) MarkExpired(ctx context.Context, heartbeatTTL time.Duration) (int, int, error) {
	if heartbeatTTL <= 0 {
		return 0, 0, nil
	}
	now := time.Now().UTC()
	expiredLeases, leaseAssistantIDs, err := s.expireRuntimeRouteLeases(ctx, now)
	if err != nil {
		return 0, 0, err
	}
	for _, assistantID := range leaseAssistantIDs {
		if err := s.recomputeRuntimeAggregate(ctx, assistantID); err != nil {
			return 0, expiredLeases, err
		}
	}

	rows, err := s.db.QueryContext(
		ctx,
		`SELECT DISTINCT assistant_id
		 FROM gateway_runtime_instances
		 WHERE status <> 'unhealthy'
		   AND last_heartbeat_at IS NOT NULL
		   AND last_heartbeat_at < ?`,
		now.Add(-heartbeatTTL),
	)
	if err != nil {
		return 0, expiredLeases, err
	}
	var assistantIDs []string
	for rows.Next() {
		var assistantID string
		if err := rows.Scan(&assistantID); err != nil {
			rows.Close()
			return 0, expiredLeases, err
		}
		assistantIDs = append(assistantIDs, assistantID)
	}
	if err := rows.Close(); err != nil {
		return 0, expiredLeases, err
	}

	result, err := s.db.ExecContext(
		ctx,
		`UPDATE gateway_runtime_instances
		 SET status = 'unhealthy', version = version + 1, updated_at = ?
		 WHERE status <> 'unhealthy'
		   AND last_heartbeat_at IS NOT NULL
		   AND last_heartbeat_at < ?`,
		now,
		now.Add(-heartbeatTTL),
	)
	if err != nil {
		return 0, expiredLeases, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return 0, expiredLeases, err
	}
	for _, assistantID := range assistantIDs {
		if err := s.recomputeRuntimeAggregate(ctx, assistantID); err != nil {
			return int(affected), expiredLeases, err
		}
	}

	legacyResult, err := s.db.ExecContext(
		ctx,
		`UPDATE gateway_runtimes
		 SET status = 'unhealthy', version = version + 1, updated_at = ?
		 WHERE status <> 'unhealthy'
		   AND last_heartbeat_at IS NOT NULL
		   AND last_heartbeat_at < ?`,
		now,
		now.Add(-heartbeatTTL),
	)
	if err != nil {
		return int(affected), expiredLeases, err
	}
	legacyAffected, err := legacyResult.RowsAffected()
	if err != nil {
		return int(affected), expiredLeases, err
	}
	if affected > 0 {
		return int(affected), expiredLeases, nil
	}
	return int(legacyAffected), expiredLeases, nil
}

type expiredRuntimeLease struct {
	LeaseID     string
	RuntimeID   string
	AssistantID string
}

type runtimeLeaseCounter struct {
	RuntimeID   string
	AssistantID string
}

func (s *SQLStore) expireRuntimeRouteLeases(ctx context.Context, now time.Time) (int, []string, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, nil, err
	}
	defer tx.Rollback()

	rows, err := tx.QueryContext(
		ctx,
		`SELECT lease_id, runtime_id, assistant_id
		 FROM gateway_runtime_leases
		 WHERE status = 'active' AND lease_expires_at < ?
		 FOR UPDATE`,
		now,
	)
	if err != nil {
		return 0, nil, err
	}
	leases := []expiredRuntimeLease{}
	for rows.Next() {
		var lease expiredRuntimeLease
		if err := rows.Scan(&lease.LeaseID, &lease.RuntimeID, &lease.AssistantID); err != nil {
			rows.Close()
			return 0, nil, err
		}
		leases = append(leases, lease)
	}
	if err := rows.Close(); err != nil {
		return 0, nil, err
	}
	if len(leases) == 0 {
		if err := tx.Commit(); err != nil {
			return 0, nil, err
		}
		return 0, nil, nil
	}

	counts := map[runtimeLeaseCounter]int{}
	assistantIDs := map[string]struct{}{}
	expiredCount := 0
	for _, lease := range leases {
		result, err := tx.ExecContext(
			ctx,
			`UPDATE gateway_runtime_leases
			 SET status = 'expired', released_at = ?, updated_at = ?
			 WHERE lease_id = ? AND status = 'active'`,
			now,
			now,
			lease.LeaseID,
		)
		if err != nil {
			return 0, nil, err
		}
		affected, err := result.RowsAffected()
		if err != nil {
			return 0, nil, err
		}
		if affected == 0 {
			continue
		}
		key := runtimeLeaseCounter{
			RuntimeID:   strings.TrimSpace(lease.RuntimeID),
			AssistantID: strings.TrimSpace(lease.AssistantID),
		}
		if key.RuntimeID == "" || key.AssistantID == "" {
			continue
		}
		expiredCount++
		counts[key]++
		assistantIDs[key.AssistantID] = struct{}{}
	}
	for key, count := range counts {
		if _, err := tx.ExecContext(
			ctx,
			`UPDATE gateway_runtime_instances
			 SET in_flight = GREATEST(in_flight - ?, 0), version = version + 1, updated_at = ?
			 WHERE runtime_id = ? AND assistant_id = ?`,
			count,
			now,
			key.RuntimeID,
			key.AssistantID,
		); err != nil {
			return 0, nil, err
		}
	}
	if err := tx.Commit(); err != nil {
		return 0, nil, err
	}

	out := make([]string, 0, len(assistantIDs))
	for assistantID := range assistantIDs {
		out = append(out, assistantID)
	}
	sort.Strings(out)
	return expiredCount, out, nil
}

type runtimeScanner interface {
	Scan(dest ...any) error
}

func scanRuntimeRecord(row runtimeScanner) (runtimepool.RuntimeRecord, error) {
	var record runtimepool.RuntimeRecord
	var podName sql.NullString
	var internalBaseURL sql.NullString
	var gatewayURL sql.NullString
	var capabilitiesJSON []byte
	var lastHeartbeatAt sql.NullTime
	var lastError sql.NullString
	if err := row.Scan(
		&record.RuntimeID,
		&record.AssistantID,
		&podName,
		&record.Status,
		&record.Capacity,
		&record.InFlight,
		&internalBaseURL,
		&gatewayURL,
		&capabilitiesJSON,
		&record.DrainMode,
		&lastHeartbeatAt,
		&lastError,
		&record.Version,
		&record.UpdatedAt,
	); err != nil {
		return runtimepool.RuntimeRecord{}, err
	}
	record.PodName = podName.String
	record.InternalBaseURL = internalBaseURL.String
	record.GatewayURL = gatewayURL.String
	record.LastError = lastError.String
	if lastHeartbeatAt.Valid {
		record.LastHeartbeatAt = &lastHeartbeatAt.Time
	}
	if len(capabilitiesJSON) > 0 {
		if err := json.Unmarshal(capabilitiesJSON, &record.Capabilities); err != nil {
			return runtimepool.RuntimeRecord{}, fmt.Errorf("unmarshal runtime capabilities: %w", err)
		}
	}
	return record, nil
}

func runtimeRecordAvailableForAggregate(record runtimepool.RuntimeRecord) bool {
	if record.DrainMode {
		return false
	}
	switch strings.ToLower(strings.TrimSpace(record.Status)) {
	case "ready", "running", "healthy":
		return strings.TrimSpace(record.InternalBaseURL) != "" || strings.TrimSpace(record.GatewayURL) != ""
	default:
		return false
	}
}

func sortedCapabilityKeys(values map[string]struct{}) []string {
	if len(values) == 0 {
		return nil
	}
	out := make([]string, 0, len(values))
	for value := range values {
		if strings.TrimSpace(value) != "" {
			out = append(out, strings.TrimSpace(value))
		}
	}
	sort.Strings(out)
	return out
}

func maxInt(left int, right int) int {
	if left > right {
		return left
	}
	return right
}

func runtimeLeaseExpiresAt(now time.Time, heartbeatTTL time.Duration) time.Time {
	if heartbeatTTL <= 0 {
		heartbeatTTL = 10 * time.Minute
	}
	return now.Add(heartbeatTTL * 2)
}

func newRuntimeLeaseID() string {
	var raw [12]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return fmt.Sprintf("gwrl-%d", time.Now().UnixNano())
	}
	return "gwrl-" + hex.EncodeToString(raw[:])
}
