package taskstore

import (
	"database/sql"
	"os"
	"strings"
	"testing"
	"time"
)

func TestMySQLDSNFromSQLAlchemyURL(t *testing.T) {
	t.Parallel()

	dsn, err := MySQLDSNFromURL("mysql+pymysql://ironagent:p%40ss@mysql:3306/ironagent?charset=utf8mb4")
	if err != nil {
		t.Fatalf("MySQLDSNFromURL() error = %v", err)
	}
	for _, want := range []string{
		"ironagent:p@ss@tcp(mysql:3306)/ironagent?",
		"parseTime=true",
		"charset=utf8mb4",
	} {
		if !strings.Contains(dsn, want) {
			t.Fatalf("DSN %q does not contain %q", dsn, want)
		}
	}
}

func TestMySQLDSNFromRawDSN(t *testing.T) {
	t.Parallel()

	raw := "ironagent:ironagent@tcp(mysql:3306)/ironagent?parseTime=true"
	dsn, err := MySQLDSNFromURL(raw)
	if err != nil {
		t.Fatalf("MySQLDSNFromURL() error = %v", err)
	}
	if dsn != raw {
		t.Fatalf("DSN = %q, want raw input", dsn)
	}
}

func TestApplyDBPoolConfig(t *testing.T) {
	t.Parallel()

	db, err := sql.Open("mysql", "ironagent:ironagent@tcp(mysql:3306)/ironagent?parseTime=true")
	if err != nil {
		t.Fatalf("sql.Open() error = %v", err)
	}
	defer db.Close()

	applyDBPoolConfig(db, DBPoolConfig{
		MaxOpenConns:    12,
		MaxIdleConns:    4,
		ConnMaxLifetime: 30 * time.Minute,
		ConnMaxIdleTime: 5 * time.Minute,
	})

	stats := db.Stats()
	if stats.MaxOpenConnections != 12 {
		t.Fatalf("MaxOpenConnections = %d, want 12", stats.MaxOpenConnections)
	}
}

func TestHeartbeatRuntimeSQLPreservesExistingRoute(t *testing.T) {
	t.Parallel()

	for _, want := range []string{
		"internal_base_url = COALESCE(VALUES(internal_base_url), gateway_runtimes.internal_base_url)",
		"gateway_url = COALESCE(VALUES(gateway_url), gateway_runtimes.gateway_url)",
	} {
		if !strings.Contains(heartbeatRuntimeSQL, want) {
			t.Fatalf("heartbeatRuntimeSQL does not preserve route with %q:\n%s", want, heartbeatRuntimeSQL)
		}
	}
}

func TestRuntimeInstanceSQLPreservesSchedulerOwnedInFlight(t *testing.T) {
	t.Parallel()

	for name, statement := range map[string]string{
		"upsert":    upsertRuntimeInstanceSQL,
		"heartbeat": heartbeatRuntimeInstanceSQL,
	} {
		if !strings.Contains(statement, "gateway_runtime_instances.in_flight") {
			t.Fatalf("%s runtime instance SQL does not preserve in_flight:\n%s", name, statement)
		}
	}
}

func TestHeartbeatRuntimeSQLClearsRecoveredDrainMode(t *testing.T) {
	t.Parallel()

	for name, statement := range map[string]string{
		"runtime":  heartbeatRuntimeSQL,
		"instance": heartbeatRuntimeInstanceSQL,
	} {
		if !strings.Contains(statement, "drain_mode = VALUES(drain_mode)") {
			t.Fatalf("%s heartbeat SQL does not accept recovered drain_mode:\n%s", name, statement)
		}
		if strings.Contains(statement, "drain_mode OR VALUES(drain_mode)") {
			t.Fatalf("%s heartbeat SQL still preserves stale drain_mode:\n%s", name, statement)
		}
	}
}

func TestRuntimeRouteLeaseSQLUsesInstanceAndLeaseTables(t *testing.T) {
	t.Parallel()

	required := []string{
		"gateway_runtime_instances",
		"FOR UPDATE",
		"SKIP LOCKED",
		"in_flight < capacity",
		"gateway_runtime_leases",
	}
	source := strings.Join([]string{
		selectRuntimeInstanceForLeaseSQL,
		incrementRuntimeInstanceInFlightSQL,
		insertRuntimeLeaseSQL,
	}, "\n")
	for _, want := range required {
		if !strings.Contains(source, want) {
			t.Fatalf("ReserveRuntimeRoute source does not contain %q:\n%s", want, source)
		}
	}
}

func TestClaimQueuedTasksSQLUsesSkipLocked(t *testing.T) {
	t.Parallel()

	sourceBytes, err := os.ReadFile("mysql.go")
	if err != nil {
		t.Fatalf("read mysql.go: %v", err)
	}
	source := string(sourceBytes)
	for _, want := range []string{
		"FOR UPDATE SKIP LOCKED",
		"SELECT task_id",
		"UPDATE gateway_task_queue",
		"mysqlLockRetryable",
	} {
		if !strings.Contains(source, want) {
			t.Fatalf("ClaimQueuedTasks source does not contain %q", want)
		}
	}
}

func TestClaimQueuedTasksSplitsQueuedAndExpiredProcessingQueries(t *testing.T) {
	t.Parallel()

	sourceBytes, err := os.ReadFile("mysql.go")
	if err != nil {
		t.Fatalf("read mysql.go: %v", err)
	}
	source := string(sourceBytes)
	for _, want := range []string{
		"selectClaimableQueuedTaskIDs",
		"selectExpiredProcessingTaskIDs",
		"status IN ('queued', 'retry')",
		"status = 'processing'",
		"ORDER BY locked_until, created_at",
	} {
		if !strings.Contains(source, want) {
			t.Fatalf("ClaimQueuedTasks source does not contain %q", want)
		}
	}
	if strings.Contains(source, "OR (status = 'processing'") {
		t.Fatalf("ClaimQueuedTasks should not mix queued and expired processing rows in one OR query")
	}
}

func TestTaskQueueStatsByTargetAssistantSQLJoinsTaskRecord(t *testing.T) {
	t.Parallel()

	sourceBytes, err := os.ReadFile("mysql.go")
	if err != nil {
		t.Fatalf("read mysql.go: %v", err)
	}
	source := string(sourceBytes)
	for _, want := range []string{
		"func (s *SQLStore) TaskQueueStatsByTargetAssistant",
		"JOIN gateway_tasks t ON t.task_id = q.task_id",
		"t.target_assistant_id = ?",
		"q.status IN ('admitting', 'queued', 'processing', 'retry')",
	} {
		if !strings.Contains(source, want) {
			t.Fatalf("TaskQueueStatsByTargetAssistant source does not contain %q", want)
		}
	}
}

func TestTaskQueueCapacityWaitStateSQLUsesQueueColumns(t *testing.T) {
	t.Parallel()

	sourceBytes, err := os.ReadFile("mysql.go")
	if err != nil {
		t.Fatalf("read mysql.go: %v", err)
	}
	source := string(sourceBytes)
	for _, want := range []string{
		"capacity_wait_count",
		"capacity_wait_first_at",
		"capacity_wait_last_at",
		"capacity_wait_last_callback_at",
		"func (s *SQLStore) DeferQueuedTaskForCapacity",
		"func (s *SQLStore) FailQueuedTaskForCapacity",
		"func (s *SQLStore) RecordQueuedTaskCapacityWaitCallback",
	} {
		if !strings.Contains(source, want) {
			t.Fatalf("capacity wait SQL source does not contain %q", want)
		}
	}
}

func TestRuntimeLeaseExpirySQLReturnsSlots(t *testing.T) {
	t.Parallel()

	sourceBytes, err := os.ReadFile("mysql_runtime.go")
	if err != nil {
		t.Fatalf("read mysql_runtime.go: %v", err)
	}
	source := string(sourceBytes)
	for _, want := range []string{
		"expireRuntimeRouteLeases",
		"status = 'expired'",
		"released_at",
		"GREATEST(in_flight - ?, 0)",
		"recomputeRuntimeAggregate",
	} {
		if !strings.Contains(source, want) {
			t.Fatalf("runtime lease expiry source does not contain %q:\n%s", want, source)
		}
	}
}

func TestWebhookEnqueueSupersedesPendingRetryingSQL(t *testing.T) {
	t.Parallel()

	sourceBytes, err := os.ReadFile("mysql_webhook.go")
	if err != nil {
		t.Fatalf("read mysql_webhook.go: %v", err)
	}
	source := string(sourceBytes)
	for _, want := range []string{
		"webhook.SupersedesPendingRetrying",
		"superseded by newer callback event",
		"event_type = 'task.retrying'",
		"status = 'pending'",
		"event_seq < ?",
	} {
		if !strings.Contains(source, want) {
			t.Fatalf("mysql_webhook.go does not contain %q:\n%s", want, source)
		}
	}
}

func TestWebhookSupersedePendingRetryingSQL(t *testing.T) {
	t.Parallel()

	sourceBytes, err := os.ReadFile("mysql_webhook.go")
	if err != nil {
		t.Fatalf("read mysql_webhook.go: %v", err)
	}
	source := string(sourceBytes)
	for _, want := range []string{
		"func (s *SQLStore) SupersedePendingRetrying",
		"UPDATE gateway_event_outbox older",
		"JOIN gateway_event_outbox newer",
		"newer.event_seq > older.event_seq",
		"superseded by newer retrying callback event",
	} {
		if !strings.Contains(source, want) {
			t.Fatalf("mysql_webhook.go does not contain %q:\n%s", want, source)
		}
	}
}
