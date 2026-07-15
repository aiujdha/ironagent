package webhook

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math/big"
	"net"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"sync"
	"time"

	"ironagent/go-gateway/internal/core"
	"ironagent/go-gateway/internal/metrics"
)

type Config struct {
	ClientID            string
	Secret              string
	MaxAttempts         int
	RetryDelay          time.Duration
	MaxRetryDelay       time.Duration
	HTTPTimeout         time.Duration
	FireAndForget       bool
	AcceptAnyResponse   bool
	QueueSize           int
	Store               OutboxStore
	StoreTimeout        time.Duration
	EnqueueMaxInFlight  int
	DeliveryConcurrency int
	WorkerInterval      time.Duration
	PendingLimit        int
	LeaseDuration       time.Duration
	DisableOutboxWorker bool
	WorkerID            string
	Logger              *slog.Logger
}

type Event struct {
	EventID          string    `json:"event_id"`
	EventSeq         int64     `json:"event_seq"`
	EventType        string    `json:"event_type"`
	TaskID           string    `json:"task_id"`
	RequestID        string    `json:"request_id,omitempty"`
	ConversationID   string    `json:"conversation_id,omitempty"`
	UserID           string    `json:"user_id,omitempty"`
	EmployeeNo       string    `json:"employee_no,omitempty"`
	Status           string    `json:"status"`
	Stage            string    `json:"stage"`
	FallbackQueryURL string    `json:"fallback_query_url,omitempty"`
	Message          string    `json:"message,omitempty"`
	ApprovalID       string    `json:"approval_id,omitempty"`
	RiskLevel        string    `json:"risk_level,omitempty"`
	Action           string    `json:"action,omitempty"`
	Resource         string    `json:"resource,omitempty"`
	Decision         string    `json:"decision,omitempty"`
	ReplyText        string    `json:"reply_text,omitempty"`
	Files            []FileRef `json:"files,omitempty"`
	ErrorCode        string    `json:"error_code,omitempty"`
	ErrorMessage     string    `json:"error_message,omitempty"`
	ErrorLocation    string    `json:"error_location,omitempty"`
	Reason           string    `json:"reason,omitempty"`
	ElapsedSeconds   int64     `json:"elapsed_seconds,omitempty"`
	CancelledBy      string    `json:"cancelled_by,omitempty"`
	Timestamp        time.Time `json:"timestamp"`
	Nonce            string    `json:"nonce"`
	Signature        string    `json:"signature"`
}

type FileRef struct {
	FileID      string `json:"file_id"`
	Filename    string `json:"filename,omitempty"`
	Size        int64  `json:"size,omitempty"`
	MimeType    string `json:"mime_type,omitempty"`
	DownloadURL string `json:"download_url,omitempty"`
}

type CallbackPayload = core.PublicTaskResponse
type CallbackFileRef = core.PublicTaskFileResponse

const (
	defaultStoreTimeout          = 2 * time.Second
	defaultEnqueueMaxInFlight    = 4
	maxCallbackResponseBodyBytes = 64 * 1024
)

type Subscription struct {
	CallbackURL    string
	RequestID      string
	ConversationID string
	UserID         string
	EmployeeNo     string
}

type ACK struct {
	Received bool   `json:"received"`
	EventID  string `json:"event_id"`
	TaskID   string `json:"task_id"`
	Handled  bool   `json:"handled"`
}

type callbackACKPayload struct {
	Received     *bool  `json:"received"`
	EventID      string `json:"event_id"`
	TaskID       string `json:"taskId"`
	LegacyTaskID string `json:"task_id"`
	Handled      *bool  `json:"handled"`
}

type callbackPostError struct {
	Kind       string
	StatusCode int
	Body       string
	Response   callbackPostResult
	Err        error
}

type callbackPostResult struct {
	Observed          bool
	FireAndForget     bool
	StatusCode        int
	Status            string
	HTTPOK            bool
	ResponseOK        bool
	AckObserved       bool
	AckOK             bool
	AckField          string
	ContentType       string
	ContentLength     int64
	TransferEncoding  string
	Body              string
	BodyBytes         int
	BodyReadError     string
	BodyTruncated     bool
	Duration          time.Duration
	AcceptAnyResponse bool
	RequestPacket     string
	ResponsePacket    string
}

func (e *callbackPostError) Error() string {
	switch {
	case e == nil:
		return ""
	case e.StatusCode > 0:
		if e.Body == "" {
			return fmt.Sprintf("callback returned %d", e.StatusCode)
		}
		return fmt.Sprintf("callback returned %d: %s", e.StatusCode, e.Body)
	case e.Err != nil:
		return e.Err.Error()
	default:
		return e.Kind
	}
}

func (e *callbackPostError) Unwrap() error {
	if e == nil {
		return nil
	}
	return e.Err
}

type DeliveryRecord struct {
	Event         Event      `json:"event"`
	CallbackURL   string     `json:"callback_url"`
	Status        string     `json:"status,omitempty"`
	Attempts      int        `json:"attempts"`
	Delivered     bool       `json:"delivered"`
	Acked         bool       `json:"acked"`
	LastError     string     `json:"last_error,omitempty"`
	NextAttemptAt *time.Time `json:"next_attempt_at,omitempty"`
	LockedBy      string     `json:"locked_by,omitempty"`
	LockedUntil   *time.Time `json:"locked_until,omitempty"`
	CreatedAt     time.Time  `json:"created_at,omitempty"`
	UpdatedAt     time.Time  `json:"updated_at"`
	DeliveredAt   *time.Time `json:"delivered_at,omitempty"`

	DeliveryMode           string `json:"delivery_mode,omitempty"`
	RequestPacket          string `json:"request_packet,omitempty"`
	ResponseObserved       bool   `json:"response_observed,omitempty"`
	ResponseStatusCode     int    `json:"response_status_code,omitempty"`
	ResponseStatus         string `json:"response_status,omitempty"`
	ResponseContentType    string `json:"response_content_type,omitempty"`
	ResponseContentLength  int64  `json:"response_content_length,omitempty"`
	ResponseBody           string `json:"response_body,omitempty"`
	ResponseBodyBytes      int    `json:"response_body_bytes,omitempty"`
	ResponseBodyReadError  string `json:"response_body_read_error,omitempty"`
	ResponseBodyTruncated  bool   `json:"response_body_truncated,omitempty"`
	ResponsePacket         string `json:"response_packet,omitempty"`
	ResponseDurationMillis int64  `json:"response_duration_ms,omitempty"`
	ResponseAcceptAny      bool   `json:"response_accept_any,omitempty"`
}

type DeadLetter struct {
	Event       Event     `json:"event"`
	CallbackURL string    `json:"callback_url"`
	Attempts    int       `json:"attempts"`
	LastError   string    `json:"last_error"`
	FailedAt    time.Time `json:"failed_at"`
}

type Dispatcher struct {
	client              *http.Client
	clientID            string
	secret              string
	maxAttempts         int
	retryDelay          time.Duration
	maxRetryDelay       time.Duration
	store               OutboxStore
	storeTimeout        time.Duration
	enqueueSlots        chan struct{}
	workerInterval      time.Duration
	pendingLimit        int
	deliveryConcurrency int
	leaseDuration       time.Duration
	outboxWorkerEnabled bool
	workerID            string
	fireAndForget       bool
	acceptAnyResponse   bool
	logger              *slog.Logger
	queue               chan deliveryJob
	wake                chan struct{}
	wg                  sync.WaitGroup
	mu                  sync.Mutex
	seqByTask           map[string]int64
	subscriptions       map[string]Subscription
	records             map[string]DeliveryRecord
	acks                map[string]ACK
	deadLetters         []DeadLetter
}

type deliveryJob struct {
	ctx    context.Context
	record DeliveryRecord
}

type OutboxStore interface {
	NextEventSeq(ctx context.Context, taskID string) (int64, error)
	Enqueue(ctx context.Context, record DeliveryRecord) error
	ClaimPending(ctx context.Context, now time.Time, limit int, workerID string, leaseDuration time.Duration) ([]DeliveryRecord, error)
	RecoverStuck(ctx context.Context, now time.Time) (int, error)
	MarkAttempt(ctx context.Context, record DeliveryRecord, nextAttemptAt time.Time) error
	MarkDelivered(ctx context.Context, record DeliveryRecord) error
	MarkDead(ctx context.Context, record DeliveryRecord, lastError string) error
	Acknowledge(ctx context.Context, ack ACK) (ACK, bool, error)
	DeadLetters(ctx context.Context, limit int) ([]DeadLetter, error)
	ReplayDeadLetter(ctx context.Context, eventID string, now time.Time) (DeliveryRecord, bool, error)
}

type outboxRetryingSuperseder interface {
	SupersedePendingRetrying(ctx context.Context, now time.Time) (int, error)
}

func NewDispatcher(cfg Config) *Dispatcher {
	maxAttempts := cfg.MaxAttempts
	if maxAttempts <= 0 {
		maxAttempts = 3
	}
	retryDelay := cfg.RetryDelay
	if retryDelay <= 0 {
		retryDelay = 250 * time.Millisecond
	}
	maxRetryDelay := cfg.MaxRetryDelay
	if maxRetryDelay <= 0 {
		maxRetryDelay = 30 * time.Second
	}
	timeout := cfg.HTTPTimeout
	if timeout <= 0 {
		timeout = 5 * time.Second
	}
	secret := strings.TrimSpace(cfg.Secret)
	if secret == "" {
		secret = randomHex(32)
	}
	clientID := strings.TrimSpace(cfg.ClientID)
	if clientID == "" {
		clientID = "ironagent-go-gateway"
	}
	queueSize := cfg.QueueSize
	if queueSize <= 0 {
		queueSize = 1024
	}
	storeTimeout := cfg.StoreTimeout
	if storeTimeout <= 0 {
		storeTimeout = defaultStoreTimeout
	}
	enqueueMaxInFlight := cfg.EnqueueMaxInFlight
	if enqueueMaxInFlight <= 0 {
		enqueueMaxInFlight = defaultEnqueueMaxInFlight
	}
	workerInterval := cfg.WorkerInterval
	if workerInterval <= 0 {
		workerInterval = 250 * time.Millisecond
	}
	deliveryConcurrency := cfg.DeliveryConcurrency
	if deliveryConcurrency <= 0 {
		deliveryConcurrency = 4
	}
	pendingLimit := cfg.PendingLimit
	if pendingLimit <= 0 {
		pendingLimit = 100
	}
	leaseDuration := cfg.LeaseDuration
	if leaseDuration <= 0 {
		leaseDuration = 30 * time.Second
	}
	workerID := strings.TrimSpace(cfg.WorkerID)
	if workerID == "" {
		workerID = clientID + "-" + randomHex(4)
	}
	dispatcher := &Dispatcher{
		client:              &http.Client{Timeout: timeout},
		clientID:            clientID,
		secret:              secret,
		maxAttempts:         maxAttempts,
		retryDelay:          retryDelay,
		maxRetryDelay:       maxRetryDelay,
		store:               cfg.Store,
		storeTimeout:        storeTimeout,
		workerInterval:      workerInterval,
		pendingLimit:        pendingLimit,
		deliveryConcurrency: deliveryConcurrency,
		leaseDuration:       leaseDuration,
		outboxWorkerEnabled: !cfg.DisableOutboxWorker,
		workerID:            workerID,
		fireAndForget:       cfg.FireAndForget,
		acceptAnyResponse:   cfg.AcceptAnyResponse,
		logger:              cfg.Logger,
		queue:               make(chan deliveryJob, queueSize),
		wake:                make(chan struct{}, 1),
		seqByTask:           make(map[string]int64),
		subscriptions:       make(map[string]Subscription),
		records:             make(map[string]DeliveryRecord),
		acks:                make(map[string]ACK),
	}
	if dispatcher.store != nil {
		dispatcher.enqueueSlots = make(chan struct{}, enqueueMaxInFlight)
	}
	go dispatcher.run()
	if dispatcher.store != nil && dispatcher.outboxWorkerEnabled {
		go dispatcher.runOutbox()
	}
	return dispatcher
}

func (d *Dispatcher) Register(taskID string, sub Subscription) {
	taskID = strings.TrimSpace(taskID)
	if taskID == "" || strings.TrimSpace(sub.CallbackURL) == "" {
		return
	}
	sub.CallbackURL = strings.TrimSpace(sub.CallbackURL)
	d.mu.Lock()
	defer d.mu.Unlock()
	d.subscriptions[taskID] = sub
}

func (d *Dispatcher) Subscription(taskID string) (Subscription, bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	sub, ok := d.subscriptions[strings.TrimSpace(taskID)]
	return sub, ok
}

func (d *Dispatcher) Dispatch(ctx context.Context, callbackURL string, event Event) (DeliveryRecord, error) {
	if d.store != nil {
		record, err := d.enqueueOutbox(ctx, callbackURL, event)
		if err != nil {
			return DeliveryRecord{}, err
		}
		return d.deliverOutbox(ctx, record)
	}
	record, err := d.prepareRecord(ctx, callbackURL, event)
	if err != nil {
		return DeliveryRecord{}, err
	}
	return d.deliver(ctx, record)
}

func (d *Dispatcher) DispatchAsync(ctx context.Context, callbackURL string, event Event) (DeliveryRecord, error) {
	if d.store != nil {
		record, err := d.enqueueOutbox(ctx, callbackURL, event)
		if err != nil {
			metrics.RecordWebhook("enqueue_failed", event.EventType)
			d.logWarn("callback事件入队失败", record, "error", err)
			return DeliveryRecord{}, err
		}
		d.logInfo("callback事件已入队", record, "mode", "outbox")
		d.wakeOutbox()
		return record, nil
	}
	record, err := d.prepareRecord(ctx, callbackURL, event)
	if err != nil {
		return DeliveryRecord{}, err
	}
	d.wg.Add(1)
	select {
	case d.queue <- deliveryJob{ctx: ctx, record: record}:
		d.logInfo("callback事件已入队", record, "mode", "memory")
		return record, nil
	default:
		d.wg.Done()
		record.LastError = "callback queue full"
		d.storeRecord(record)
		d.logWarn("callback事件入队失败，队列已满", record)
		metrics.RecordWebhook("dead_letter", record.Event.EventType)
		d.addDeadLetter(DeadLetter{
			Event:       record.Event,
			CallbackURL: record.CallbackURL,
			LastError:   record.LastError,
			FailedAt:    time.Now().UTC(),
		})
		return record, errors.New(record.LastError)
	}
}

func (d *Dispatcher) enqueueOutbox(
	ctx context.Context,
	callbackURL string,
	event Event,
) (DeliveryRecord, error) {
	storeCtx, cancel := d.storeContext(ctx)
	defer cancel()

	release, err := d.acquireEnqueueSlot(storeCtx)
	if err != nil {
		return DeliveryRecord{}, err
	}
	defer release()

	record, err := d.prepareRecord(storeCtx, callbackURL, event)
	if err != nil {
		return DeliveryRecord{}, err
	}
	if err := d.store.Enqueue(storeCtx, record); err != nil {
		return record, err
	}
	return record, nil
}

func (d *Dispatcher) acquireEnqueueSlot(ctx context.Context) (func(), error) {
	if d.enqueueSlots == nil {
		return func() {}, nil
	}
	select {
	case d.enqueueSlots <- struct{}{}:
		return func() { <-d.enqueueSlots }, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func (d *Dispatcher) storeContext(ctx context.Context) (context.Context, context.CancelFunc) {
	if ctx == nil {
		ctx = context.Background()
	}
	if d.storeTimeout <= 0 {
		return ctx, func() {}
	}
	return context.WithTimeout(ctx, d.storeTimeout)
}

func (d *Dispatcher) WaitIdle(timeout time.Duration) bool {
	done := make(chan struct{})
	go func() {
		d.wg.Wait()
		close(done)
	}()
	select {
	case <-done:
		return true
	case <-time.After(timeout):
		return false
	}
}

func (d *Dispatcher) prepareRecord(ctx context.Context, callbackURL string, event Event) (DeliveryRecord, error) {
	callbackURL = strings.TrimSpace(callbackURL)
	if callbackURL == "" {
		return DeliveryRecord{}, fmt.Errorf("callback_url is required")
	}
	event, err := d.prepareEvent(ctx, event)
	if err != nil {
		return DeliveryRecord{}, err
	}

	now := time.Now().UTC()
	record := DeliveryRecord{
		Event:       event,
		CallbackURL: callbackURL,
		Status:      "pending",
		CreatedAt:   now,
		UpdatedAt:   now,
	}
	if d.store == nil {
		d.storeRecord(record)
	}
	return record, nil
}

func (d *Dispatcher) run() {
	for job := range d.queue {
		_, _ = d.deliver(job.ctx, job.record)
		d.wg.Done()
	}
}

func (d *Dispatcher) runOutbox() {
	ticker := time.NewTicker(d.workerInterval)
	defer ticker.Stop()
	consecutiveFailures := 0
	processOnce := func(trigger string) {
		_, err := d.ProcessPending(context.Background())
		if err != nil {
			consecutiveFailures++
			if d.logger != nil {
				d.logger.Warn("callback_outbox_process_failed",
					"logged_at", time.Now().UTC().Format(time.RFC3339Nano),
					"worker_id", d.workerID,
					"trigger", trigger,
					"consecutive_failures", consecutiveFailures,
					"error", err.Error(),
				)
			}
			return
		}
		consecutiveFailures = 0
	}
	for {
		select {
		case <-d.wake:
			processOnce("wake")
		case <-ticker.C:
			processOnce("ticker")
		}
	}
}

func (d *Dispatcher) wakeOutbox() {
	if d.store == nil || !d.outboxWorkerEnabled {
		return
	}
	select {
	case d.wake <- struct{}{}:
	default:
	}
}

func (d *Dispatcher) ProcessPending(ctx context.Context) (int, error) {
	if d.store == nil {
		return 0, nil
	}
	storeCtx, cancel := d.storeContext(ctx)
	defer cancel()
	now := time.Now().UTC()
	if _, err := d.store.RecoverStuck(storeCtx, now); err != nil {
		return 0, err
	}
	if superseder, ok := d.store.(outboxRetryingSuperseder); ok {
		superseded, err := superseder.SupersedePendingRetrying(storeCtx, now)
		if err != nil {
			return 0, err
		}
		if superseded > 0 && d.logger != nil {
			d.logger.Info("callback retrying backlog已合并", "superseded_count", superseded)
		}
	}
	claimLimit := d.pendingLimit
	if d.deliveryConcurrency > 0 && claimLimit > d.deliveryConcurrency {
		claimLimit = d.deliveryConcurrency
	}
	records, err := d.store.ClaimPending(storeCtx, now, claimLimit, d.workerID, d.leaseDuration)
	if err != nil {
		return 0, err
	}
	concurrency := d.deliveryConcurrency
	if concurrency <= 1 || len(records) <= 1 {
		for _, record := range records {
			_, _ = d.deliverOutbox(ctx, record)
		}
		return len(records), nil
	}
	lanes := make([][]DeliveryRecord, 0, len(records))
	laneByTask := make(map[string]int, len(records))
	for _, record := range records {
		taskID := strings.TrimSpace(record.Event.TaskID)
		if taskID == "" {
			taskID = strings.TrimSpace(record.Event.EventID)
		}
		index, ok := laneByTask[taskID]
		if !ok {
			index = len(lanes)
			laneByTask[taskID] = index
			lanes = append(lanes, nil)
		}
		lanes[index] = append(lanes[index], record)
	}
	slots := make(chan struct{}, concurrency)
	var wg sync.WaitGroup
	for _, lane := range lanes {
		lane := lane
		slots <- struct{}{}
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer func() { <-slots }()
			for _, record := range lane {
				_, _ = d.deliverOutbox(ctx, record)
			}
		}()
	}
	wg.Wait()
	return len(records), nil
}

func (d *Dispatcher) deliver(ctx context.Context, record DeliveryRecord) (DeliveryRecord, error) {
	event := record.Event
	callbackURL := record.CallbackURL
	var lastErr error
	for attempt := 1; attempt <= d.maxAttempts; attempt++ {
		record.Attempts = attempt
		d.logInfo("开始推送callback事件", record, "attempt", attempt, "max_attempts", d.maxAttempts)
		result, err := d.postWithMode(ctx, record)
		record = recordDeliveryResult(record, result)
		if err == nil {
			record.Delivered = true
			record.UpdatedAt = time.Now().UTC()
			d.storeRecord(record)
			d.logInfo(
				"callback事件推送成功",
				record,
				append([]any{"attempt", attempt}, callbackResponseAttrs(result)...)...,
			)
			metrics.RecordWebhook("delivered", event.EventType)
			return record, nil
		}
		metrics.RecordWebhook("failed_attempt", event.EventType)
		lastErr = err
		record.LastError = err.Error()
		record.UpdatedAt = time.Now().UTC()
		d.storeRecord(record)
		if callbackPermanentFailure(err) {
			d.logWarn(
				"callback事件被接收端明确拒收，不再重试",
				record,
				append(
					[]any{"attempt", attempt, "max_attempts", d.maxAttempts},
					callbackFailureAttrs(err)...,
				)...,
			)
			break
		}
		if attempt < d.maxAttempts {
			d.logWarn(
				"callback事件推送失败，将重试",
				record,
				append(
					[]any{"attempt", attempt, "max_attempts", d.maxAttempts},
					callbackFailureAttrs(err)...,
				)...,
			)
			select {
			case <-ctx.Done():
				lastErr = ctx.Err()
				attempt = d.maxAttempts
			case <-time.After(d.retryDelay):
			}
		}
	}

	if lastErr == nil {
		lastErr = fmt.Errorf("callback delivery failed")
	}
	record.LastError = lastErr.Error()
	metrics.RecordWebhook("dead_letter", event.EventType)
	d.logWarn(
		"callback事件推送失败，进入dead-letter",
		record,
		append([]any{"attempts", record.Attempts}, callbackFailureAttrs(lastErr)...)...,
	)
	d.addDeadLetter(DeadLetter{
		Event:       event,
		CallbackURL: callbackURL,
		Attempts:    record.Attempts,
		LastError:   record.LastError,
		FailedAt:    time.Now().UTC(),
	})
	return record, lastErr
}

func (d *Dispatcher) deliverOutbox(ctx context.Context, record DeliveryRecord) (DeliveryRecord, error) {
	if d.store == nil {
		return d.deliver(ctx, record)
	}
	record.Attempts++
	record.UpdatedAt = time.Now().UTC()
	d.logInfo("开始推送callback事件", record, "attempt", record.Attempts, "max_attempts", d.maxAttempts)
	result, err := d.postWithMode(ctx, record)
	record = recordDeliveryResult(record, result)
	if err == nil {
		now := time.Now().UTC()
		record.Status = "delivered"
		record.Delivered = true
		record.DeliveredAt = &now
		record.UpdatedAt = now
		storeCtx, cancel := d.storeContext(ctx)
		markErr := d.store.MarkDelivered(storeCtx, record)
		cancel()
		if markErr != nil {
			return record, markErr
		}
		d.logInfo(
			"callback事件推送成功",
			record,
			append([]any{"attempt", record.Attempts}, callbackResponseAttrs(result)...)...,
		)
		metrics.RecordWebhook("delivered", record.Event.EventType)
		return record, nil
	}

	metrics.RecordWebhook("failed_attempt", record.Event.EventType)
	record.LastError = err.Error()
	record.Delivered = false
	if record.Attempts >= d.maxAttempts || callbackPermanentFailure(err) {
		record.Status = "dead"
		storeCtx, cancel := d.storeContext(ctx)
		markErr := d.store.MarkDead(storeCtx, record, record.LastError)
		cancel()
		if markErr != nil {
			return record, markErr
		}
		d.logWarn(
			callbackDeadLetterLogMessage(err),
			record,
			append([]any{"attempts", record.Attempts}, callbackFailureAttrs(err)...)...,
		)
		metrics.RecordWebhook("dead_letter", record.Event.EventType)
		return record, err
	}

	nextAttemptAt := time.Now().UTC().Add(d.nextRetryDelay(record.Attempts))
	record.Status = "pending"
	record.NextAttemptAt = &nextAttemptAt
	storeCtx, cancel := d.storeContext(ctx)
	markErr := d.store.MarkAttempt(storeCtx, record, nextAttemptAt)
	cancel()
	if markErr != nil {
		return record, markErr
	}
	d.logWarn(
		"callback事件推送失败，将重试",
		record,
		append(
			[]any{
				"attempt", record.Attempts,
				"max_attempts", d.maxAttempts,
				"next_attempt_at", nextAttemptAt.Format(time.RFC3339Nano),
			},
			callbackFailureAttrs(err)...,
		)...,
	)
	metrics.RecordWebhook("retry_scheduled", record.Event.EventType)
	return record, err
}

func (d *Dispatcher) ReplayDeadLetter(ctx context.Context, eventID string) (DeliveryRecord, bool, error) {
	if d.store == nil {
		return DeliveryRecord{}, false, nil
	}
	storeCtx, cancel := d.storeContext(ctx)
	defer cancel()
	record, ok, err := d.store.ReplayDeadLetter(storeCtx, eventID, time.Now().UTC())
	if err != nil || !ok {
		return record, ok, err
	}
	metrics.RecordWebhook("replay", record.Event.EventType)
	d.wakeOutbox()
	return record, true, nil
}

func (d *Dispatcher) Acknowledge(ack ACK) (ACK, bool) {
	ack.EventID = strings.TrimSpace(ack.EventID)
	ack.TaskID = strings.TrimSpace(ack.TaskID)
	if ack.EventID == "" || ack.TaskID == "" {
		if d.logger != nil {
			d.logger.Warn("callback ACK无效，缺少event_id或task_id", "event_id", ack.EventID, "task_id", ack.TaskID)
		}
		return ACK{}, false
	}
	if d.store != nil {
		storeCtx, cancel := d.storeContext(context.Background())
		stored, ok, err := d.store.Acknowledge(storeCtx, ack)
		cancel()
		if err != nil {
			if d.logger != nil {
				d.logger.Warn("callback ACK写入失败", "event_id", ack.EventID, "task_id", ack.TaskID, "error", err)
			}
			return ACK{}, false
		}
		if ok {
			metrics.RecordWebhook("ack", "unknown")
			if d.logger != nil {
				d.logger.Info("callback ACK已确认", "event_id", ack.EventID, "task_id", ack.TaskID, "stored", true)
			}
		} else if d.logger != nil {
			d.logger.Warn("callback ACK不匹配", "event_id", ack.EventID, "task_id", ack.TaskID)
		}
		return stored, ok
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	record, ok := d.records[ack.EventID]
	if !ok || record.Event.TaskID != ack.TaskID {
		if d.logger != nil {
			d.logger.Warn("callback ACK不匹配", "event_id", ack.EventID, "task_id", ack.TaskID)
		}
		return ACK{}, false
	}
	d.acks[ack.EventID] = ack
	record.Acked = ack.Received && ack.Handled
	record.UpdatedAt = time.Now().UTC()
	d.records[ack.EventID] = record
	metrics.RecordWebhook("ack", record.Event.EventType)
	if d.logger != nil {
		d.logger.Info("callback ACK已确认", "event_id", ack.EventID, "task_id", ack.TaskID, "event_type", record.Event.EventType)
	}
	return ack, ok
}

func (d *Dispatcher) DeadLetters() []DeadLetter {
	if d.store != nil {
		storeCtx, cancel := d.storeContext(context.Background())
		letters, err := d.store.DeadLetters(storeCtx, 100)
		cancel()
		if err == nil {
			return letters
		}
		return nil
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]DeadLetter, len(d.deadLetters))
	copy(out, d.deadLetters)
	return out
}

func (d *Dispatcher) DeadLetterCount() (int, bool) {
	if d.store != nil {
		return 0, false
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	return len(d.deadLetters), true
}

func (d *Dispatcher) Records() []DeliveryRecord {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]DeliveryRecord, 0, len(d.records))
	for _, record := range d.records {
		out = append(out, record)
	}
	return out
}

func (d *Dispatcher) prepareEvent(ctx context.Context, event Event) (Event, error) {
	event.TaskID = strings.TrimSpace(event.TaskID)
	event.EventType = strings.TrimSpace(event.EventType)
	if event.EventID == "" {
		event.EventID = randomHex(16)
	}
	if event.Nonce == "" {
		event.Nonce = randomHex(12)
	}
	if event.Timestamp.IsZero() {
		event.Timestamp = time.Now().UTC()
	}
	if event.EventSeq <= 0 {
		if d.store != nil {
			seq, err := d.store.NextEventSeq(ctx, event.TaskID)
			if err != nil {
				return Event{}, err
			}
			event.EventSeq = seq
		} else {
			d.mu.Lock()
			d.seqByTask[event.TaskID]++
			event.EventSeq = d.seqByTask[event.TaskID]
			d.mu.Unlock()
		}
	}
	event.Signature = d.sign(event)
	return event, nil
}

func (d *Dispatcher) postWithMode(ctx context.Context, record DeliveryRecord) (callbackPostResult, error) {
	if d.fireAndForget {
		return d.postFireAndForget(record)
	}
	result, err := d.post(ctx, record.CallbackURL, record.Event)
	if err == nil || !d.acceptAnyResponse {
		return result, err
	}
	var postErr *callbackPostError
	if errors.As(err, &postErr) && postErr != nil && postErr.Response.Observed {
		result = postErr.Response
		result.AcceptAnyResponse = true
		return result, nil
	}
	return result, err
}

func (d *Dispatcher) post(ctx context.Context, callbackURL string, event Event) (callbackPostResult, error) {
	payload, err := json.Marshal(NewCallbackPayload(event))
	if err != nil {
		return callbackPostResult{}, err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, callbackURL, bytes.NewReader(payload))
	if err != nil {
		return callbackPostResult{}, &callbackPostError{Kind: "request_build_failed", Err: err}
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "application/json")
	setCallbackEventHeaders(request, event)
	result := callbackPostResult{
		RequestPacket: formatCallbackRequestPacket(request, payload),
	}

	started := time.Now()
	response, err := d.client.Do(request)
	if err != nil {
		result.Duration = time.Since(started)
		return result, &callbackPostError{Kind: "network_error", Response: result, Err: err}
	}
	defer response.Body.Close()

	body, truncated, readErr := readCallbackResponseBody(response.Body)
	result.Observed = true
	result.StatusCode = response.StatusCode
	result.Status = response.Status
	result.HTTPOK = response.StatusCode >= http.StatusOK && response.StatusCode < http.StatusMultipleChoices
	result.AckOK, result.AckObserved, result.AckField = callbackResponseAckStatus(body)
	result.ResponseOK = result.HTTPOK && (!result.AckObserved || result.AckOK)
	result.ContentType = response.Header.Get("Content-Type")
	result.ContentLength = response.ContentLength
	result.TransferEncoding = strings.Join(response.TransferEncoding, ",")
	result.Body = body
	result.BodyBytes = len(body)
	if readErr != nil {
		result.BodyReadError = readErr.Error()
	}
	result.BodyTruncated = truncated
	result.Duration = time.Since(started)
	result.ResponsePacket = formatCallbackResponsePacket(response, body)
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		return result, &callbackPostError{
			Kind:       "http_status",
			StatusCode: response.StatusCode,
			Body:       body,
			Response:   result,
		}
	}
	if result.AckObserved && !result.AckOK {
		return result, &callbackPostError{
			Kind:     "ack_rejected",
			Body:     body,
			Response: result,
			Err:      fmt.Errorf("callback ack %s=false", result.AckField),
		}
	}
	return result, nil
}

func (d *Dispatcher) postFireAndForget(record DeliveryRecord) (callbackPostResult, error) {
	result := callbackPostResult{FireAndForget: true}
	payload, err := json.Marshal(NewCallbackPayload(record.Event))
	if err != nil {
		return result, err
	}
	request, err := http.NewRequestWithContext(context.Background(), http.MethodPost, record.CallbackURL, bytes.NewReader(payload))
	if err != nil {
		return result, &callbackPostError{Kind: "request_build_failed", Response: result, Err: err}
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "application/json")
	setCallbackEventHeaders(request, record.Event)
	request.Close = true
	result.RequestPacket = formatCallbackRequestPacket(request, payload)
	started := time.Now()
	err = d.writeAndClose(request)
	result.Duration = time.Since(started)
	if err != nil {
		var postErr *callbackPostError
		if errors.As(err, &postErr) && postErr != nil {
			postErr.Response = result
		}
		return result, err
	}
	return result, nil
}

func (d *Dispatcher) writeAndClose(request *http.Request) error {
	if request.URL == nil {
		return &callbackPostError{Kind: "request_build_failed", Err: errors.New("callback url is missing")}
	}
	address, err := callbackDialAddress(request.URL)
	if err != nil {
		return &callbackPostError{Kind: "request_build_failed", Err: err}
	}
	timeout := d.client.Timeout
	if timeout <= 0 {
		timeout = 5 * time.Second
	}
	dialer := net.Dialer{Timeout: timeout}
	conn, err := dialer.DialContext(context.Background(), "tcp", address)
	if err != nil {
		return &callbackPostError{Kind: "network_error", Err: err}
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(timeout))

	if request.URL.Scheme == "https" {
		tlsConn := tls.Client(conn, &tls.Config{MinVersion: tls.VersionTLS12, ServerName: request.URL.Hostname()})
		if err := tlsConn.Handshake(); err != nil {
			return &callbackPostError{Kind: "network_error", Err: err}
		}
		conn = tlsConn
	}
	if err := request.Write(conn); err != nil {
		return &callbackPostError{Kind: "network_error", Err: err}
	}
	return nil
}

func recordDeliveryResult(record DeliveryRecord, result callbackPostResult) DeliveryRecord {
	mode := "http_response"
	if result.FireAndForget {
		mode = "fire_and_forget"
	}
	record.DeliveryMode = mode
	record.RequestPacket = result.RequestPacket
	record.ResponseObserved = result.Observed
	record.ResponseStatusCode = result.StatusCode
	record.ResponseStatus = result.Status
	record.ResponseContentType = result.ContentType
	record.ResponseContentLength = result.ContentLength
	record.ResponseBody = result.Body
	record.ResponseBodyBytes = result.BodyBytes
	record.ResponseBodyReadError = result.BodyReadError
	record.ResponseBodyTruncated = result.BodyTruncated
	record.ResponsePacket = result.ResponsePacket
	record.ResponseDurationMillis = result.Duration.Milliseconds()
	record.ResponseAcceptAny = result.AcceptAnyResponse
	return record
}

func callbackDialAddress(rawURL *url.URL) (string, error) {
	switch rawURL.Scheme {
	case "http", "https":
	default:
		return "", fmt.Errorf("unsupported callback url scheme %q", rawURL.Scheme)
	}
	host := rawURL.Hostname()
	if host == "" {
		return "", fmt.Errorf("callback url host is required")
	}
	port := rawURL.Port()
	if port == "" {
		if rawURL.Scheme == "https" {
			port = "443"
		} else {
			port = "80"
		}
	}
	return net.JoinHostPort(host, port), nil
}

func callbackACKFromBody(body []byte, event Event) (ACK, bool, error) {
	if len(bytes.TrimSpace(body)) == 0 {
		return ACK{}, false, nil
	}
	var payload callbackACKPayload
	if err := json.Unmarshal(body, &payload); err != nil {
		return ACK{}, false, nil
	}
	taskID := strings.TrimSpace(payload.TaskID)
	if taskID == "" {
		taskID = strings.TrimSpace(payload.LegacyTaskID)
	}
	if taskID == "" {
		return ACK{}, false, nil
	}
	if taskID != event.TaskID {
		return ACK{}, false, fmt.Errorf("callback ack task_id %q does not match task_id %q", taskID, event.TaskID)
	}
	if eventID := strings.TrimSpace(payload.EventID); eventID != "" && eventID != event.EventID {
		return ACK{}, false, fmt.Errorf("callback ack event_id does not match internal event")
	}
	ack := ACK{
		Received: true,
		EventID:  event.EventID,
		TaskID:   event.TaskID,
		Handled:  true,
	}
	if payload.Received != nil {
		ack.Received = *payload.Received
	}
	if payload.Handled != nil {
		ack.Handled = *payload.Handled
	}
	return ack, true, nil
}

func NewCallbackPayload(event Event) CallbackPayload {
	timestamp := ""
	if !event.Timestamp.IsZero() {
		timestamp = event.Timestamp.UTC().Format(time.RFC3339Nano)
	}
	return core.NewPublicTaskPayload(core.PublicTaskPayloadInput{
		TaskID:        event.TaskID,
		Status:        event.Status,
		Stage:         event.Stage,
		EventID:       event.EventID,
		EventSeq:      event.EventSeq,
		EventType:     event.EventType,
		Timestamp:     timestamp,
		Nonce:         event.Nonce,
		Signature:     event.Signature,
		ReplyText:     callbackReplyTextMarkdown(event),
		Files:         callbackPayloadFiles(event),
		ErrorCode:     event.ErrorCode,
		ErrorMessage:  event.ErrorMessage,
		ErrorLocation: event.ErrorLocation,
		ErrorReason:   event.Reason,
	})
}

func callbackReplyTextMarkdown(event Event) string {
	replyText := strings.TrimSpace(event.ReplyText)
	if replyText == "" {
		return ""
	}
	needsBoundary := callbackReplyTextNeedsMarkdownBoundary(event)
	if callbackReplyTextAlreadyMarkdown(replyText) {
		if needsBoundary {
			return ensureMarkdownBlockBoundary(replyText)
		}
		return replyText
	}
	title := callbackReplyTextMarkdownTitle(event)
	if title == "" {
		if needsBoundary {
			return ensureMarkdownBlockBoundary(replyText)
		}
		return replyText
	}
	rendered := "### " + title + "\n\n" + replyText
	if needsBoundary {
		return ensureMarkdownBlockBoundary(rendered)
	}
	return rendered
}

func callbackReplyTextNeedsMarkdownBoundary(event Event) bool {
	switch strings.TrimSpace(event.EventType) {
	case "task.progress",
		"task.retrying",
		"chat.progress",
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

func ensureMarkdownBlockBoundary(value string) string {
	return strings.TrimRight(value, " \t\r\n") + "\n\n"
}

func callbackReplyTextAlreadyMarkdown(replyText string) bool {
	trimmed := strings.TrimSpace(replyText)
	return strings.HasPrefix(trimmed, "#") ||
		strings.HasPrefix(trimmed, "- ") ||
		strings.HasPrefix(trimmed, "* ") ||
		strings.HasPrefix(trimmed, "1. ") ||
		strings.HasPrefix(trimmed, "|")
}

func callbackReplyTextMarkdownTitle(event Event) string {
	switch strings.TrimSpace(event.EventType) {
	case "task.retrying":
		return "重试安排"
	}
	switch strings.TrimSpace(event.Status) {
	case "completed":
		return "执行结果"
	case "failed":
		return "执行失败"
	case "cancelled":
		return "任务已取消"
	case "rejected":
		return "任务已拒绝"
	case "processing":
		return "执行进度"
	default:
		return "执行状态"
	}
}

func setCallbackEventHeaders(request *http.Request, event Event) {
	if request == nil {
		return
	}
	if value := strings.TrimSpace(event.EventID); value != "" {
		request.Header.Set("X-IronAgent-Event-ID", value)
	}
	if event.EventSeq > 0 {
		request.Header.Set("X-IronAgent-Event-Seq", fmt.Sprintf("%d", event.EventSeq))
	}
	if value := strings.TrimSpace(event.EventType); value != "" {
		request.Header.Set("X-IronAgent-Event-Type", value)
	}
	if !event.Timestamp.IsZero() {
		request.Header.Set("X-IronAgent-Timestamp", event.Timestamp.UTC().Format(time.RFC3339Nano))
	}
	if value := strings.TrimSpace(event.Nonce); value != "" {
		request.Header.Set("X-IronAgent-Nonce", value)
	}
	if value := strings.TrimSpace(event.Signature); value != "" {
		request.Header.Set("X-IronAgent-Signature", value)
	}
}

func callbackPayloadFiles(event Event) []CallbackFileRef {
	return callbackFiles(event.Files)
}

func callbackFiles(files []FileRef) []CallbackFileRef {
	if len(files) == 0 {
		return nil
	}
	out := make([]core.PublicTaskFileResponse, 0, len(files))
	for _, file := range files {
		if strings.TrimSpace(file.FileID) == "" {
			continue
		}
		item := core.PublicTaskFileResponse{
			FileID:      file.FileID,
			FileName:    file.Filename,
			MimeType:    file.MimeType,
			DownloadURL: file.DownloadURL,
		}
		if file.Size > 0 {
			item.Size = fmt.Sprintf("%d", file.Size)
		}
		out = append(out, item)
	}
	return out
}

func (d *Dispatcher) logInfo(message string, record DeliveryRecord, attrs ...any) {
	if d.logger == nil {
		return
	}
	d.logger.Info(message, append(d.callbackLogAttrs(record), attrs...)...)
}

func (d *Dispatcher) logWarn(message string, record DeliveryRecord, attrs ...any) {
	if d.logger == nil {
		return
	}
	d.logger.Warn(message, append(d.callbackLogAttrs(record), attrs...)...)
}

func (d *Dispatcher) callbackLogAttrs(record DeliveryRecord) []any {
	files := callbackPayloadFiles(record.Event)
	attrs := []any{
		"logged_at", time.Now().UTC().Format(time.RFC3339Nano),
		"task_id", record.Event.TaskID,
		"event_id", record.Event.EventID,
		"event_type", record.Event.EventType,
		"event_seq", record.Event.EventSeq,
		"status", record.Event.Status,
		"stage", record.Event.Stage,
		"delivery_status", record.Status,
		"attempts", record.Attempts,
		"callback_files_count", len(files),
		"callback_has_files", len(files) > 0,
	}
	if len(files) > 0 {
		attrs = append(attrs,
			"callback_file_ids", callbackLogFileIDs(files),
			"callback_files", files,
			"callback_files_json", callbackLogFilesJSON(files),
		)
	}
	if host := callbackHost(record.CallbackURL); host != "" {
		attrs = append(attrs, "callback_host", host)
	}
	if record.LastError != "" {
		attrs = append(attrs, "last_error", record.LastError)
	}
	return attrs
}

func callbackLogFileIDs(files []CallbackFileRef) []string {
	ids := make([]string, 0, len(files))
	for _, file := range files {
		if fileID := strings.TrimSpace(file.FileID); fileID != "" {
			ids = append(ids, fileID)
		}
	}
	return ids
}

func callbackLogFilesJSON(files []CallbackFileRef) string {
	if len(files) == 0 {
		return "[]"
	}
	payload, err := json.Marshal(files)
	if err != nil {
		return "[]"
	}
	return string(payload)
}

func callbackHost(raw string) string {
	parsed, err := url.Parse(strings.TrimSpace(raw))
	if err != nil {
		return ""
	}
	return parsed.Host
}

func readCallbackResponseBody(reader io.Reader) (string, bool, error) {
	body, err := io.ReadAll(io.LimitReader(reader, maxCallbackResponseBodyBytes+1))
	if len(body) > maxCallbackResponseBodyBytes {
		return string(body[:maxCallbackResponseBodyBytes]), true, err
	}
	return string(body), false, err
}

func formatCallbackRequestPacket(request *http.Request, body []byte) string {
	if request == nil {
		return ""
	}
	var builder strings.Builder
	target := "/"
	if request.URL != nil {
		target = request.URL.RequestURI()
		if target == "" {
			target = "/"
		}
	}
	proto := request.Proto
	if proto == "" {
		proto = "HTTP/1.1"
	}
	fmt.Fprintf(&builder, "%s %s %s\r\n", request.Method, target, proto)
	host := request.Host
	if host == "" && request.URL != nil {
		host = request.URL.Host
	}
	fmt.Fprintf(&builder, "Host: %s\r\n", host)
	writeSortedHeaders(&builder, request.Header)
	fmt.Fprintf(&builder, "Content-Length: %d\r\n", len(body))
	if request.Close {
		builder.WriteString("Connection: close\r\n")
	}
	builder.WriteString("\r\n")
	builder.Write(body)
	return builder.String()
}

func formatCallbackResponsePacket(response *http.Response, body string) string {
	if response == nil {
		return ""
	}
	var builder strings.Builder
	proto := response.Proto
	if proto == "" {
		proto = "HTTP/1.1"
	}
	status := response.Status
	if status == "" && response.StatusCode > 0 {
		status = fmt.Sprintf("%d %s", response.StatusCode, http.StatusText(response.StatusCode))
	}
	fmt.Fprintf(&builder, "%s %s\r\n", proto, status)
	writeSortedHeaders(&builder, response.Header)
	builder.WriteString("\r\n")
	builder.WriteString(body)
	return builder.String()
}

func writeSortedHeaders(builder *strings.Builder, headers http.Header) {
	keys := make([]string, 0, len(headers))
	for key := range headers {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		values := headers.Values(key)
		for _, value := range values {
			fmt.Fprintf(builder, "%s: %s\r\n", key, value)
		}
	}
}

func callbackResponseAttrs(result callbackPostResult) []any {
	mode := "http_response"
	if result.FireAndForget {
		mode = "fire_and_forget"
	}
	attrs := []any{
		"callback_delivery_mode", mode,
		"callback_response_observed", result.Observed,
		"callback_response_ok", result.ResponseOK,
		"callback_response_http_ok", result.HTTPOK,
		"callback_response_ack_observed", result.AckObserved,
		"callback_response_ack_ok", result.AckOK,
		"callback_response_ack_field", result.AckField,
		"callback_request_packet", result.RequestPacket,
		"callback_response_duration_ms", result.Duration.Milliseconds(),
		"callback_response_status_code", result.StatusCode,
		"callback_response_status", result.Status,
		"callback_response_content_type", result.ContentType,
		"callback_response_content_length", result.ContentLength,
		"callback_response_transfer_encoding", result.TransferEncoding,
		"callback_response_body", result.Body,
		"callback_response_body_bytes", result.BodyBytes,
		"callback_response_body_read_error", result.BodyReadError,
		"callback_response_body_truncated", result.BodyTruncated,
		"callback_response_packet", result.ResponsePacket,
	}
	if result.AcceptAnyResponse {
		attrs = append(attrs, "callback_accept_any_response", true)
	}
	return attrs
}

func callbackResponseAckStatus(body string) (ok bool, observed bool, field string) {
	trimmed := bytes.TrimSpace([]byte(body))
	if len(trimmed) == 0 {
		return false, false, ""
	}
	var payload map[string]any
	if err := json.Unmarshal(trimmed, &payload); err != nil {
		return false, false, ""
	}
	for _, key := range []string{"ok", "success", "received", "handled", "accepted"} {
		value, exists := payload[key]
		if !exists {
			continue
		}
		if parsed, valueOK := callbackResponseBool(value); valueOK {
			return parsed, true, key
		}
	}
	return false, false, ""
}

func callbackResponseBool(value any) (bool, bool) {
	switch typed := value.(type) {
	case bool:
		return typed, true
	case string:
		switch strings.ToLower(strings.TrimSpace(typed)) {
		case "true", "ok", "success", "yes", "1":
			return true, true
		case "false", "fail", "failed", "no", "0":
			return false, true
		default:
			return false, false
		}
	case float64:
		return typed != 0, true
	default:
		return false, false
	}
}

func callbackFailureAttrs(err error) []any {
	if err == nil {
		return nil
	}
	attrs := []any{
		"failure_reason", callbackFailureReason(err),
		"failure_detail", err.Error(),
	}
	var postErr *callbackPostError
	if errors.As(err, &postErr) && postErr != nil {
		attrs = append(attrs, callbackResponseAttrs(postErr.Response)...)
		if postErr.StatusCode > 0 {
			attrs = append(attrs, "http_status", postErr.StatusCode)
		}
		if postErr.Body != "" {
			attrs = append(attrs, "response_body", postErr.Body)
		}
	}
	return attrs
}

func callbackPermanentFailure(err error) bool {
	var postErr *callbackPostError
	if !errors.As(err, &postErr) || postErr == nil {
		return false
	}
	if postErr.Kind != "http_status" || !callbackPermanentHTTPStatus(postErr.StatusCode) {
		return false
	}
	return callbackReceiverLimitBody(postErr.Body)
}

func callbackPermanentHTTPStatus(statusCode int) bool {
	switch statusCode {
	case http.StatusRequestTimeout, http.StatusTooEarly, http.StatusTooManyRequests:
		return false
	default:
		return statusCode >= http.StatusBadRequest && statusCode < http.StatusInternalServerError
	}
}

func callbackReceiverLimitBody(body string) bool {
	body = strings.TrimSpace(body)
	if body == "" {
		return false
	}
	var decoded any
	if err := json.Unmarshal([]byte(body), &decoded); err == nil {
		return callbackReceiverLimitJSON(decoded)
	}
	return callbackReceiverLimitPhrase(body)
}

func callbackReceiverLimitJSON(value any) bool {
	switch typed := value.(type) {
	case map[string]any:
		for _, key := range []string{"msg", "message", "error", "error_message", "reason"} {
			if callbackReceiverLimitJSON(typed[key]) {
				return true
			}
		}
	case []any:
		for _, item := range typed {
			if callbackReceiverLimitJSON(item) {
				return true
			}
		}
	case string:
		return callbackReceiverLimitPhrase(typed)
	}
	return false
}

func callbackReceiverLimitPhrase(value string) bool {
	body := strings.ToLower(strings.TrimSpace(value))
	return strings.Contains(body, "exceed max call times") ||
		strings.Contains(body, "exceeded max call times")
}

func callbackDeadLetterLogMessage(err error) string {
	if callbackPermanentFailure(err) {
		return "callback事件被接收端明确拒收，进入dead-letter"
	}
	return "callback事件推送失败，进入dead-letter"
}

func callbackFailureReason(err error) string {
	var postErr *callbackPostError
	if errors.As(err, &postErr) && postErr != nil {
		if callbackPermanentFailure(err) {
			return "receiver_limit"
		}
		if postErr.Kind == "http_status" {
			switch {
			case postErr.StatusCode >= 500:
				return "upstream_5xx"
			case postErr.StatusCode >= 400:
				return "upstream_4xx"
			default:
				return "upstream_non_2xx"
			}
		}
		if postErr.Kind != "" && postErr.Kind != "network_error" {
			return postErr.Kind
		}
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return "timeout"
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return "timeout"
	}
	var urlErr *url.Error
	if errors.As(err, &urlErr) {
		inner := strings.ToLower(urlErr.Err.Error())
		switch {
		case strings.Contains(inner, "connection refused"):
			return "connection_refused"
		case strings.Contains(inner, "no such host"):
			return "dns_error"
		case strings.Contains(inner, "i/o timeout"), strings.Contains(inner, "timeout"):
			return "timeout"
		default:
			return "network_error"
		}
	}
	return "unknown"
}

func (d *Dispatcher) sign(event Event) string {
	base := fmt.Sprintf(
		"%s.%d.%s.%s.%s.%s",
		event.EventID,
		event.EventSeq,
		event.EventType,
		event.TaskID,
		event.Timestamp.Format(time.RFC3339Nano),
		event.Nonce,
	)
	mac := hmac.New(sha256.New, []byte(d.secret))
	_, _ = mac.Write([]byte(base))
	return hex.EncodeToString(mac.Sum(nil))
}

func (d *Dispatcher) nextRetryDelay(attempts int) time.Duration {
	if attempts <= 0 {
		attempts = 1
	}
	delay := d.retryDelay
	for i := 1; i < attempts; i++ {
		if delay >= d.maxRetryDelay/2 {
			delay = d.maxRetryDelay
			break
		}
		delay *= 2
	}
	if delay > d.maxRetryDelay {
		delay = d.maxRetryDelay
	}
	if delay <= time.Millisecond {
		return delay
	}
	return delay/2 + randomDuration(delay/2)
}

func (d *Dispatcher) storeRecord(record DeliveryRecord) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.records[record.Event.EventID] = record
}

func (d *Dispatcher) addDeadLetter(letter DeadLetter) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.deadLetters = append(d.deadLetters, letter)
}

func randomHex(size int) string {
	raw := make([]byte, size)
	if _, err := rand.Read(raw); err != nil {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(raw)
}

func randomDuration(max time.Duration) time.Duration {
	if max <= 0 {
		return 0
	}
	n, err := rand.Int(rand.Reader, big.NewInt(int64(max)))
	if err != nil {
		return max / 2
	}
	return time.Duration(n.Int64())
}
