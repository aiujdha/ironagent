package core

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestTaskProgressEmitterBatchesSanitizesAndFlushes(t *testing.T) {
	t.Parallel()

	store := &memoryTaskProgressStore{}
	var (
		mu      sync.Mutex
		updates []QueuedTaskProgressUpdate
	)
	service := &Service{}
	service.SetTaskProgressStore(store)
	service.SetQueuedTaskProgressHook(func(_ context.Context, update QueuedTaskProgressUpdate) error {
		mu.Lock()
		defer mu.Unlock()
		updates = append(updates, update)
		return nil
	})

	emitter := service.newQueuedTaskProgressEmitter(context.Background(), GatewayTaskQueueItem{
		TaskID:       "task-1",
		AttemptToken: "attempt-1",
	}, TaskWorkerOptions{
		TaskProgressCallbackEnabled:      true,
		TaskProgressCallbackBatchSize:    2,
		TaskProgressCallbackInterval:     time.Hour,
		TaskProgressCallbackStepMaxRunes: 30,
		TaskProgressCallbackStateTTL:     time.Hour,
	})
	if emitter == nil {
		t.Fatal("progress emitter = nil")
	}
	defer emitter.Stop(context.Background())

	emitter.AddStep(context.Background(), "runtime started", "runtime started", time.Now())
	emitter.AddStep(context.Background(), "调用工具", "开始调用转换工具 token=secret /workspace/temp/input.txt "+strings.Repeat("长", 40), time.Now())
	if err := emitter.Flush(context.Background()); err != nil {
		t.Fatalf("Flush() error = %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	if len(updates) != 1 {
		t.Fatalf("updates length = %d, want 1", len(updates))
	}
	update := updates[0]
	if update.FromSeq != 1 || update.ToSeq != 2 {
		t.Fatalf("range = %d-%d, want 1-2", update.FromSeq, update.ToSeq)
	}
	if !strings.Contains(update.ReplyText, "### 执行进度：第 1-2 步") {
		t.Fatalf("replyText missing range header: %q", update.ReplyText)
	}
	if !strings.Contains(update.ReplyText, "1. **任务已启动**：运行环境已启动，开始处理请求") {
		t.Fatalf("replyText missing first step: %q", update.ReplyText)
	}
	if strings.Contains(update.ReplyText, "secret") || strings.Contains(update.ReplyText, "/workspace/") {
		t.Fatalf("replyText leaked sensitive content: %q", update.ReplyText)
	}
	if !strings.Contains(update.ReplyText, "token=\\[redacted\\]") || !strings.Contains(update.ReplyText, "...") {
		t.Fatalf("replyText did not redact and truncate content: %q", update.ReplyText)
	}
}

func TestTaskProgressEmitterReturnsOnlyNewBatches(t *testing.T) {
	t.Parallel()

	var (
		mu      sync.Mutex
		updates []QueuedTaskProgressUpdate
	)
	service := &Service{}
	service.SetQueuedTaskProgressHook(func(_ context.Context, update QueuedTaskProgressUpdate) error {
		mu.Lock()
		defer mu.Unlock()
		updates = append(updates, update)
		return nil
	})
	emitter := service.newQueuedTaskProgressEmitter(context.Background(), GatewayTaskQueueItem{
		TaskID:       "task-2",
		AttemptToken: "attempt-2",
	}, TaskWorkerOptions{
		TaskProgressCallbackEnabled:      true,
		TaskProgressCallbackBatchSize:    2,
		TaskProgressCallbackInterval:     time.Hour,
		TaskProgressCallbackStepMaxRunes: 200,
	})
	if emitter == nil {
		t.Fatal("progress emitter = nil")
	}
	defer emitter.Stop(context.Background())

	for i := 0; i < 5; i++ {
		emitter.AddStep(context.Background(), "步骤", "执行第 "+intString(i+1)+" 个动作", time.Now())
	}
	if err := emitter.Flush(context.Background()); err != nil {
		t.Fatalf("Flush() error = %v", err)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(updates) != 3 {
		t.Fatalf("updates length = %d, want 3", len(updates))
	}
	expected := []struct {
		from int
		to   int
		text string
	}{
		{from: 1, to: 2, text: "### 执行进度：第 1-2 步"},
		{from: 3, to: 4, text: "### 执行进度：第 3-4 步"},
		{from: 5, to: 5, text: "### 执行进度：第 5-5 步"},
	}
	for i, want := range expected {
		if updates[i].FromSeq != want.from || updates[i].ToSeq != want.to {
			t.Fatalf("updates[%d] range = %d-%d, want %d-%d", i, updates[i].FromSeq, updates[i].ToSeq, want.from, want.to)
		}
		if !strings.Contains(updates[i].ReplyText, want.text) {
			t.Fatalf("updates[%d] replyText = %q, want header %q", i, updates[i].ReplyText, want.text)
		}
	}
	if strings.Contains(updates[1].ReplyText, "执行第 1 个动作") || strings.Contains(updates[2].ReplyText, "执行第 4 个动作") {
		t.Fatalf("replyText repeated historical steps: %#v", updates)
	}
}

func TestTaskProgressEmitterFlushesEveryTenStepsBeforeFinalDrain(t *testing.T) {
	t.Parallel()

	var (
		mu      sync.Mutex
		updates []QueuedTaskProgressUpdate
	)
	service := &Service{}
	service.SetQueuedTaskProgressHook(func(_ context.Context, update QueuedTaskProgressUpdate) error {
		mu.Lock()
		defer mu.Unlock()
		updates = append(updates, update)
		return nil
	})
	emitter := service.newQueuedTaskProgressEmitter(context.Background(), GatewayTaskQueueItem{
		TaskID:       "task-batch-ten",
		AttemptToken: "attempt-batch-ten",
	}, TaskWorkerOptions{
		TaskProgressCallbackEnabled:      true,
		TaskProgressCallbackBatchSize:    10,
		TaskProgressCallbackInterval:     time.Hour,
		TaskProgressCallbackStepMaxRunes: 200,
	})
	if emitter == nil {
		t.Fatal("progress emitter = nil")
	}
	defer emitter.Stop(context.Background())

	for i := 1; i <= 9; i++ {
		emitter.AddStep(context.Background(), "步骤", "执行第 "+intString(i)+" 个动作", time.Now())
	}
	if got := taskProgressUpdateCount(&mu, &updates); got != 0 {
		t.Fatalf("updates after 9 steps = %d, want 0", got)
	}

	emitter.AddStep(context.Background(), "步骤", "执行第 10 个动作", time.Now())
	waitForTaskProgressUpdates(t, &mu, &updates, 1)
	assertTaskProgressRange(t, updates[0], 1, 10)

	for i := 11; i <= 19; i++ {
		emitter.AddStep(context.Background(), "步骤", "执行第 "+intString(i)+" 个动作", time.Now())
	}
	if got := taskProgressUpdateCount(&mu, &updates); got != 1 {
		t.Fatalf("updates after 19 steps = %d, want 1", got)
	}

	emitter.AddStep(context.Background(), "步骤", "执行第 20 个动作", time.Now())
	waitForTaskProgressUpdates(t, &mu, &updates, 2)
	assertTaskProgressRange(t, updates[1], 11, 20)

	emitter.AddStep(context.Background(), "步骤", "执行第 21 个动作", time.Now())
	if err := emitter.Flush(context.Background()); err != nil {
		t.Fatalf("Flush() error = %v", err)
	}
	if got := taskProgressUpdateCount(&mu, &updates); got != 3 {
		t.Fatalf("updates after final drain = %d, want 3", got)
	}
	assertTaskProgressRange(t, updates[2], 21, 21)
}

func TestTaskProgressEmitterMergesAssistantDeltaIntoOneStep(t *testing.T) {
	t.Parallel()

	var (
		mu      sync.Mutex
		updates []QueuedTaskProgressUpdate
	)
	service := &Service{}
	service.SetQueuedTaskProgressHook(func(_ context.Context, update QueuedTaskProgressUpdate) error {
		mu.Lock()
		defer mu.Unlock()
		updates = append(updates, update)
		return nil
	})
	emitter := service.newQueuedTaskProgressEmitter(context.Background(), GatewayTaskQueueItem{
		TaskID:       "task-delta-merge",
		AttemptToken: "attempt-delta-merge",
	}, TaskWorkerOptions{
		TaskProgressCallbackEnabled:      true,
		TaskProgressCallbackBatchSize:    2,
		TaskProgressCallbackInterval:     time.Hour,
		TaskProgressCallbackStepMaxRunes: 200,
	})
	if emitter == nil {
		t.Fatal("progress emitter = nil")
	}
	defer emitter.Stop(context.Background())

	deltas := []string{
		"现在", "现在让我", "现在让我读取", "现在让我读取台账",
		"现在让我读取台账和", "现在让我读取台账和说明",
		"现在让我读取台账和说明文件", "现在让我读取台账和说明文件：",
		"现在让我读取台账和说明文件：现在我来", "现在让我读取台账和说明文件：现在我来编写处理脚本",
	}
	for _, text := range deltas {
		emitter.AddRuntimeEvent(context.Background(), RuntimeProgressEvent{
			State: "delta",
			Event: "chat",
			Text:  text,
		})
	}
	if got := taskProgressUpdateCount(&mu, &updates); got != 0 {
		t.Fatalf("updates after merged deltas = %d, want 0", got)
	}
	if err := emitter.Flush(context.Background()); err != nil {
		t.Fatalf("Flush() error = %v", err)
	}
	waitForTaskProgressUpdates(t, &mu, &updates, 1)
	mu.Lock()
	defer mu.Unlock()
	if len(updates) != 1 {
		t.Fatalf("updates = %#v, want one merged progress callback", updates)
	}
	update := updates[0]
	assertTaskProgressRange(t, update, 1, 1)
	if update.EventType != "delta" {
		t.Fatalf("eventType = %q, want delta", update.EventType)
	}
	if strings.Count(update.ReplyText, "assistant progress") != 1 {
		t.Fatalf("replyText split assistant progress into multiple steps: %q", update.ReplyText)
	}
	if !strings.Contains(update.ReplyText, "现在让我读取台账和说明文件：现在我来编写处理脚本") {
		t.Fatalf("replyText missing merged delta content: %q", update.ReplyText)
	}
}

func TestTaskProgressEmitterPreservesDeltaWhitespaceWhenMerging(t *testing.T) {
	t.Parallel()

	var updates []QueuedTaskProgressUpdate
	service := &Service{}
	service.SetQueuedTaskProgressHook(func(_ context.Context, update QueuedTaskProgressUpdate) error {
		updates = append(updates, update)
		return nil
	})
	emitter := service.newQueuedTaskProgressEmitter(context.Background(), GatewayTaskQueueItem{
		TaskID:       "task-delta-space",
		AttemptToken: "attempt-delta-space",
	}, TaskWorkerOptions{
		TaskProgressCallbackEnabled:      true,
		TaskProgressCallbackBatchSize:    5,
		TaskProgressCallbackInterval:     time.Hour,
		TaskProgressCallbackStepMaxRunes: 200,
	})
	if emitter == nil {
		t.Fatal("progress emitter = nil")
	}
	defer emitter.Stop(context.Background())

	emitter.AddRuntimeEvent(context.Background(), RuntimeProgressEvent{State: "delta", Event: "chat", Text: "Hello"})
	emitter.AddRuntimeEvent(context.Background(), RuntimeProgressEvent{State: "delta", Event: "chat", Text: " world"})
	if err := emitter.Flush(context.Background()); err != nil {
		t.Fatalf("Flush() error = %v", err)
	}
	if len(updates) != 1 {
		t.Fatalf("updates = %#v, want one merged progress callback", updates)
	}
	if !strings.Contains(updates[0].ReplyText, "Hello world") {
		t.Fatalf("replyText did not preserve delta whitespace: %q", updates[0].ReplyText)
	}
}

func TestTaskProgressEmitterFlushesOpenAssistantDeltaWhenNonDeltaArrives(t *testing.T) {
	t.Parallel()

	var (
		mu      sync.Mutex
		updates []QueuedTaskProgressUpdate
	)
	service := &Service{}
	service.SetQueuedTaskProgressHook(func(_ context.Context, update QueuedTaskProgressUpdate) error {
		mu.Lock()
		defer mu.Unlock()
		updates = append(updates, update)
		return nil
	})
	emitter := service.newQueuedTaskProgressEmitter(context.Background(), GatewayTaskQueueItem{
		TaskID:       "task-delta-close",
		AttemptToken: "attempt-delta-close",
	}, TaskWorkerOptions{
		TaskProgressCallbackEnabled:      true,
		TaskProgressCallbackBatchSize:    10,
		TaskProgressCallbackInterval:     time.Hour,
		TaskProgressCallbackStepMaxRunes: 200,
	})
	if emitter == nil {
		t.Fatal("progress emitter = nil")
	}
	defer emitter.Stop(context.Background())

	emitter.AddRuntimeEvent(context.Background(), RuntimeProgressEvent{
		State: "delta",
		Event: "chat",
		Text:  "我正在组织完整进度",
	})
	if got := taskProgressUpdateCount(&mu, &updates); got != 0 {
		t.Fatalf("updates after open delta = %d, want 0", got)
	}
	emitter.AddRuntimeEvent(context.Background(), RuntimeProgressEvent{
		State:    "progress",
		Event:    "tool.progress",
		ToolName: "file-reader",
		Detail:   "开始读取文件",
	})
	waitForTaskProgressUpdates(t, &mu, &updates, 2)
	mu.Lock()
	defer mu.Unlock()
	if len(updates) != 2 {
		t.Fatalf("updates = %#v, want delta and tool.progress callbacks", updates)
	}
	assertTaskProgressRange(t, updates[0], 1, 1)
	assertTaskProgressRange(t, updates[1], 2, 2)
	if updates[0].EventType != "delta" || updates[1].EventType != "tool.progress" {
		t.Fatalf("event types = %q/%q, want delta/tool.progress", updates[0].EventType, updates[1].EventType)
	}
	if !strings.Contains(updates[0].ReplyText, "我正在组织完整进度") ||
		!strings.Contains(updates[1].ReplyText, "file-reader") {
		t.Fatalf("unexpected progress callbacks after non-delta close: %#v", updates)
	}
}

func TestTaskProgressEmitterDoesNotFlushOpenAssistantDeltaOnInterval(t *testing.T) {
	t.Parallel()

	var (
		mu      sync.Mutex
		updates []QueuedTaskProgressUpdate
	)
	service := &Service{}
	service.SetQueuedTaskProgressHook(func(_ context.Context, update QueuedTaskProgressUpdate) error {
		mu.Lock()
		defer mu.Unlock()
		updates = append(updates, update)
		return nil
	})
	emitter := service.newQueuedTaskProgressEmitter(context.Background(), GatewayTaskQueueItem{
		TaskID:       "task-delta-interval",
		AttemptToken: "attempt-delta-interval",
	}, TaskWorkerOptions{
		TaskProgressCallbackEnabled:      true,
		TaskProgressCallbackBatchSize:    10,
		TaskProgressCallbackInterval:     20 * time.Millisecond,
		TaskProgressCallbackStepMaxRunes: 200,
	})
	if emitter == nil {
		t.Fatal("progress emitter = nil")
	}
	defer emitter.Stop(context.Background())

	emitter.AddRuntimeEvent(context.Background(), RuntimeProgressEvent{
		State: "delta",
		Event: "chat",
		Text:  "正在组织完整进度内容",
	})
	time.Sleep(80 * time.Millisecond)
	if got := taskProgressUpdateCount(&mu, &updates); got != 0 {
		t.Fatalf("updates after open delta interval = %d, want 0", got)
	}

	emitter.AddRuntimeEvent(context.Background(), RuntimeProgressEvent{
		State:    "progress",
		Event:    "tool.progress",
		ToolName: "file-reader",
		Detail:   "开始读取文件",
	})
	waitForTaskProgressUpdates(t, &mu, &updates, 2)
	mu.Lock()
	defer mu.Unlock()
	assertTaskProgressRange(t, updates[0], 1, 1)
	assertTaskProgressRange(t, updates[1], 2, 2)
	if updates[0].EventType != "delta" || updates[1].EventType != "tool.progress" {
		t.Fatalf("event types = %q/%q, want delta/tool.progress", updates[0].EventType, updates[1].EventType)
	}
	if !strings.Contains(updates[0].ReplyText, "正在组织完整进度内容") ||
		!strings.Contains(updates[1].ReplyText, "file-reader") {
		t.Fatalf("unexpected progress callback after closing delta: %#v", updates)
	}
}

func TestTaskProgressEmitterFlushesCompletedStepsOnInterval(t *testing.T) {
	t.Parallel()

	var (
		mu      sync.Mutex
		updates []QueuedTaskProgressUpdate
	)
	service := &Service{}
	service.SetQueuedTaskProgressHook(func(_ context.Context, update QueuedTaskProgressUpdate) error {
		mu.Lock()
		defer mu.Unlock()
		updates = append(updates, update)
		return nil
	})
	emitter := service.newQueuedTaskProgressEmitter(context.Background(), GatewayTaskQueueItem{
		TaskID:       "task-interval-flush",
		AttemptToken: "attempt-interval-flush",
	}, TaskWorkerOptions{
		TaskProgressCallbackEnabled:      true,
		TaskProgressCallbackBatchSize:    10,
		TaskProgressCallbackInterval:     20 * time.Millisecond,
		TaskProgressCallbackStepMaxRunes: 200,
	})
	if emitter == nil {
		t.Fatal("progress emitter = nil")
	}
	defer emitter.Stop(context.Background())

	for i := 1; i <= 3; i++ {
		emitter.AddStep(context.Background(), "步骤", "已完成第 "+intString(i)+" 个动作", time.Now())
	}

	waitForTaskProgressUpdates(t, &mu, &updates, 1)
	assertTaskProgressRange(t, updates[0], 1, 3)
}

func TestTaskProgressEmitterPushesKeepaliveWhenRuntimeIsSilent(t *testing.T) {
	t.Parallel()

	var (
		mu      sync.Mutex
		updates []QueuedTaskProgressUpdate
	)
	service := &Service{}
	service.SetQueuedTaskProgressHook(func(_ context.Context, update QueuedTaskProgressUpdate) error {
		mu.Lock()
		defer mu.Unlock()
		updates = append(updates, update)
		return nil
	})
	emitter := service.newQueuedTaskProgressEmitter(context.Background(), GatewayTaskQueueItem{
		TaskID:       "task-silent-runtime",
		AttemptToken: "attempt-silent-runtime",
	}, TaskWorkerOptions{
		TaskProgressCallbackEnabled:      true,
		TaskProgressCallbackBatchSize:    10,
		TaskProgressCallbackInterval:     20 * time.Millisecond,
		TaskProgressCallbackStepMaxRunes: 200,
	})
	if emitter == nil {
		t.Fatal("progress emitter = nil")
	}
	defer emitter.Stop(context.Background())

	emitter.AddStep(context.Background(), "runtime started", "runtime started", time.Now())
	emitter.AddStep(context.Background(), "assistant progress", "开始处理文件", time.Now())
	if err := emitter.Flush(context.Background()); err != nil {
		t.Fatalf("Flush() error = %v", err)
	}
	waitForTaskProgressUpdates(t, &mu, &updates, 2)

	mu.Lock()
	defer mu.Unlock()
	assertTaskProgressRange(t, updates[0], 1, 2)
	assertTaskProgressRange(t, updates[1], 3, 3)
	if !strings.Contains(updates[1].ReplyText, "任务仍在执行") ||
		!strings.Contains(updates[1].ReplyText, "等待运行时返回后续进度或最终结果") {
		t.Fatalf("keepalive callback = %q, want still-running text", updates[1].ReplyText)
	}
}

func TestTaskProgressEmitterDoesNotPushKeepaliveBeforeProgress(t *testing.T) {
	t.Parallel()

	var (
		mu      sync.Mutex
		updates []QueuedTaskProgressUpdate
	)
	service := &Service{}
	service.SetQueuedTaskProgressHook(func(_ context.Context, update QueuedTaskProgressUpdate) error {
		mu.Lock()
		defer mu.Unlock()
		updates = append(updates, update)
		return nil
	})
	emitter := service.newQueuedTaskProgressEmitter(context.Background(), GatewayTaskQueueItem{
		TaskID:       "task-no-progress",
		AttemptToken: "attempt-no-progress",
	}, TaskWorkerOptions{
		TaskProgressCallbackEnabled:      true,
		TaskProgressCallbackBatchSize:    10,
		TaskProgressCallbackInterval:     20 * time.Millisecond,
		TaskProgressCallbackStepMaxRunes: 200,
	})
	if emitter == nil {
		t.Fatal("progress emitter = nil")
	}
	defer emitter.Stop(context.Background())

	time.Sleep(80 * time.Millisecond)
	if got := taskProgressUpdateCount(&mu, &updates); got != 0 {
		t.Fatalf("updates before progress = %d, want 0", got)
	}
}

func TestTaskProgressMarkdownKeepsBlockBoundaryForConcatenation(t *testing.T) {
	t.Parallel()

	first := formatTaskProgressMarkdown([]TaskProgressStep{{
		Seq:     1,
		Title:   "任务已启动",
		Content: "运行环境已启动，开始处理请求",
		At:      time.Now(),
	}})
	second := formatTaskProgressMarkdown([]TaskProgressStep{{
		Seq:     2,
		Title:   "任务仍在执行",
		Content: "等待运行时返回后续进度或最终结果",
		At:      time.Now(),
	}})

	joined := first + second
	if strings.Contains(joined, "请求### 执行进度") {
		t.Fatalf("joined markdown lost heading boundary: %q", joined)
	}
	if !strings.Contains(joined, "请求\n\n### 执行进度：第 2-2 步") {
		t.Fatalf("joined markdown missing blank line before next heading: %q", joined)
	}
}

func TestTaskProgressEmitterDoesNotBlockRuntimeOnSlowDispatch(t *testing.T) {
	t.Parallel()

	started := make(chan struct{})
	release := make(chan struct{})
	var once sync.Once
	service := &Service{}
	service.SetQueuedTaskProgressHook(func(_ context.Context, _ QueuedTaskProgressUpdate) error {
		once.Do(func() {
			close(started)
		})
		<-release
		return nil
	})
	emitter := service.newQueuedTaskProgressEmitter(context.Background(), GatewayTaskQueueItem{
		TaskID:       "task-slow-dispatch",
		AttemptToken: "attempt-slow-dispatch",
	}, TaskWorkerOptions{
		TaskProgressCallbackEnabled:      true,
		TaskProgressCallbackBatchSize:    1,
		TaskProgressCallbackInterval:     time.Hour,
		TaskProgressCallbackStepMaxRunes: 200,
	})
	if emitter == nil {
		t.Fatal("progress emitter = nil")
	}

	done := make(chan struct{})
	go func() {
		emitter.AddStep(context.Background(), "runtime started", "runtime started", time.Now())
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(200 * time.Millisecond):
		close(release)
		t.Fatal("AddStep blocked on slow progress dispatch")
	}
	select {
	case <-started:
	case <-time.After(time.Second):
		close(release)
		t.Fatal("progress dispatch did not start")
	}
	close(release)
	if err := emitter.Flush(context.Background()); err != nil {
		t.Fatalf("Flush() error = %v", err)
	}
	emitter.Stop(context.Background())
}

func taskProgressUpdateCount(mu *sync.Mutex, updates *[]QueuedTaskProgressUpdate) int {
	mu.Lock()
	defer mu.Unlock()
	return len(*updates)
}

func waitForTaskProgressUpdates(t *testing.T, mu *sync.Mutex, updates *[]QueuedTaskProgressUpdate, want int) {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for {
		if got := taskProgressUpdateCount(mu, updates); got >= want {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %d progress updates, got %d", want, taskProgressUpdateCount(mu, updates))
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func assertTaskProgressRange(t *testing.T, update QueuedTaskProgressUpdate, from int, to int) {
	t.Helper()
	if update.FromSeq != from || update.ToSeq != to {
		t.Fatalf("range = %d-%d, want %d-%d", update.FromSeq, update.ToSeq, from, to)
	}
	wantHeader := "### 执行进度：第 " + intString(from) + "-" + intString(to) + " 步"
	if !strings.Contains(update.ReplyText, wantHeader) {
		t.Fatalf("replyText = %q, want header %q", update.ReplyText, wantHeader)
	}
}

func TestTaskProgressEmitterDoesNotBlockRuntimeOnSlowStoreSave(t *testing.T) {
	t.Parallel()

	storeStarted := make(chan struct{})
	releaseStore := make(chan struct{})
	store := &blockingTaskProgressStore{
		saveStarted: storeStarted,
		releaseSave: releaseStore,
	}
	service := &Service{}
	service.SetTaskProgressStore(store)
	service.SetQueuedTaskProgressHook(func(_ context.Context, _ QueuedTaskProgressUpdate) error {
		return nil
	})
	emitter := service.newQueuedTaskProgressEmitter(context.Background(), GatewayTaskQueueItem{
		TaskID:       "task-slow-store",
		AttemptToken: "attempt-slow-store",
	}, TaskWorkerOptions{
		TaskProgressCallbackEnabled:      true,
		TaskProgressCallbackBatchSize:    10,
		TaskProgressCallbackInterval:     time.Hour,
		TaskProgressCallbackStepMaxRunes: 200,
	})
	if emitter == nil {
		t.Fatal("progress emitter = nil")
	}

	done := make(chan struct{})
	go func() {
		emitter.AddStep(context.Background(), "runtime started", "runtime started", time.Now())
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(200 * time.Millisecond):
		close(releaseStore)
		t.Fatal("AddStep blocked on slow progress store save")
	}
	select {
	case <-storeStarted:
	case <-time.After(time.Second):
		close(releaseStore)
		t.Fatal("store save did not start")
	}
	close(releaseStore)
	emitter.Stop(context.Background())
}

func TestTaskProgressEmitterKeepsPendingBatchWhenDispatchFails(t *testing.T) {
	t.Parallel()

	var (
		attempts int
		updates  []QueuedTaskProgressUpdate
	)
	service := &Service{}
	service.SetQueuedTaskProgressHook(func(_ context.Context, update QueuedTaskProgressUpdate) error {
		attempts++
		if attempts == 1 {
			return errors.New("progress dispatch failed")
		}
		updates = append(updates, update)
		return nil
	})
	emitter := service.newQueuedTaskProgressEmitter(context.Background(), GatewayTaskQueueItem{
		TaskID:       "task-retry-dispatch",
		AttemptToken: "attempt-retry-dispatch",
	}, TaskWorkerOptions{
		TaskProgressCallbackEnabled:      true,
		TaskProgressCallbackBatchSize:    10,
		TaskProgressCallbackInterval:     time.Hour,
		TaskProgressCallbackStepMaxRunes: 200,
	})
	if emitter == nil {
		t.Fatal("progress emitter = nil")
	}
	defer emitter.Stop(context.Background())

	emitter.AddStep(context.Background(), "步骤", "第一次进度", time.Now())
	if err := emitter.Flush(context.Background()); err == nil {
		t.Fatal("first Flush() error = nil, want dispatch failure")
	}
	if err := emitter.Flush(context.Background()); err != nil {
		t.Fatalf("second Flush() error = %v", err)
	}
	if len(updates) != 1 {
		t.Fatalf("updates = %#v, want one retry delivery", updates)
	}
	if updates[0].FromSeq != 1 || updates[0].ToSeq != 1 || !strings.Contains(updates[0].ReplyText, "第一次进度") {
		t.Fatalf("retry update = %#v", updates[0])
	}
}

func TestTaskProgressEmitterSplitsRapidMixedEventTypesByContiguousPrefix(t *testing.T) {
	t.Parallel()

	var (
		mu      sync.Mutex
		updates []QueuedTaskProgressUpdate
	)
	service := &Service{}
	service.SetQueuedTaskProgressHook(func(_ context.Context, update QueuedTaskProgressUpdate) error {
		mu.Lock()
		defer mu.Unlock()
		updates = append(updates, update)
		return nil
	})
	emitter := service.newQueuedTaskProgressEmitter(context.Background(), GatewayTaskQueueItem{
		TaskID:       "task-mixed-aba",
		AttemptToken: "attempt-mixed-aba",
	}, TaskWorkerOptions{
		TaskProgressCallbackEnabled:      true,
		TaskProgressCallbackBatchSize:    10,
		TaskProgressCallbackInterval:     time.Hour,
		TaskProgressCallbackStepMaxRunes: 200,
	})
	if emitter == nil {
		t.Fatal("progress emitter = nil")
	}
	defer emitter.Stop(context.Background())

	emitter.AddRuntimeEvent(context.Background(), RuntimeProgressEvent{
		State: "delta",
		Event: "chat",
		Text:  "第一段",
	})
	emitter.AddRuntimeEvent(context.Background(), RuntimeProgressEvent{
		State:    "progress",
		Event:    "tool.progress",
		ToolName: "reader",
		Detail:   "读文件",
	})
	emitter.AddRuntimeEvent(context.Background(), RuntimeProgressEvent{
		State: "delta",
		Event: "chat",
		Text:  "第二段",
	})
	if err := emitter.Flush(context.Background()); err != nil {
		t.Fatalf("Flush() error = %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	if len(updates) != 3 {
		t.Fatalf("updates = %#v, want 3 single-event-type batches", updates)
	}
}

func TestTaskProgressRuntimeEventFiltering(t *testing.T) {
	t.Parallel()

	for _, event := range []RuntimeProgressEvent{
		{State: "final", Event: "chat", Text: "done"},
		{State: "heartbeat", Event: "tool.heartbeat", Text: "still alive"},
		{State: "error", Event: "chat", Text: "failed"},
		{State: "running", Event: "running", Text: "southbound task running"},
	} {
		if _, _, _, ok := taskProgressStepFromRuntimeEvent(event, 200); ok {
			t.Fatalf("event should be filtered: %#v", event)
		}
	}

	title, content, eventType, ok := taskProgressStepFromRuntimeEvent(RuntimeProgressEvent{
		State: "delta",
		Event: "chat",
		Text:  "我将先读取上传文件，再调用 skill 处理，并写入结果文件。",
	}, 200)
	if !ok || eventType != "delta" || title != "assistant progress" || content != "我将先读取上传文件，再调用 skill 处理，并写入结果文件。" {
		t.Fatalf("delta event = title %q content %q eventType %q ok %v", title, content, eventType, ok)
	}

	title, content, eventType, ok = taskProgressStepFromRuntimeEvent(RuntimeProgressEvent{
		State: "progress",
		Event: "chat",
		Text:  "正在分析任务步骤和文件路径",
	}, 200)
	if !ok || eventType != "chat.progress" || title != "chat progress" || content != "正在分析任务步骤和文件路径" {
		t.Fatalf("chat progress event = title %q content %q eventType %q ok %v", title, content, eventType, ok)
	}

	title, content, eventType, ok = taskProgressStepFromRuntimeEvent(RuntimeProgressEvent{State: "chat_sent", Event: "chat.sent"}, 200)
	if !ok || eventType != "task.progress" || title != "请求已发送" || content != "已将用户请求发送给运行时" {
		t.Fatalf("chat_sent event = title %q content %q eventType %q ok %v", title, content, eventType, ok)
	}

	title, content, eventType, ok = taskProgressStepFromRuntimeEvent(RuntimeProgressEvent{State: "ack", Event: "chat.ack"}, 200)
	if !ok || eventType != "task.progress" || title != "请求已接收" || content != "运行时已确认接收，正在处理" {
		t.Fatalf("ack event = title %q content %q eventType %q ok %v", title, content, eventType, ok)
	}

	title, content, eventType, ok = taskProgressStepFromRuntimeEvent(RuntimeProgressEvent{
		State:    "progress",
		Event:    "tool.progress",
		ToolName: "file-reader",
		Detail:   "正在读取\n用户上传文件 token=abc",
	}, 200)
	if !ok || eventType != "tool.progress" {
		t.Fatal("tool progress event was filtered")
	}
	if title != "file-reader" {
		t.Fatalf("title = %q, want file-reader", title)
	}
	if strings.Contains(content, "\n") || strings.Contains(content, "abc") || !strings.Contains(content, "token=[redacted]") {
		t.Fatalf("content was not cleaned: %q", content)
	}

	longOutput := strings.Repeat("读", 160)
	title, content, eventType, ok = taskProgressStepFromRuntimeEvent(RuntimeProgressEvent{
		State: "tool_output_read",
		Event: "tool-events",
		Text:  longOutput,
	}, 150)
	if !ok || eventType != "tool.output" {
		t.Fatal("tool output read event was filtered")
	}
	if title != "tool.output" {
		t.Fatalf("title = %q, want tool.output", title)
	}
	if !strings.HasSuffix(content, "...") {
		t.Fatalf("tool output content missing ellipsis: %q", content)
	}
	if got := len([]rune(strings.TrimSuffix(content, "..."))); got != 150 {
		t.Fatalf("tool output truncated rune length = %d, want 150", got)
	}

	title, content, eventType, ok = taskProgressStepFromRuntimeEvent(RuntimeProgressEvent{
		State:  "running",
		Event:  "tool.exec",
		Detail: "调用 skill 处理用户上传的文件",
	}, 150)
	if !ok || eventType != "tool.exec" || title != "tool exec" || content != "调用 skill 处理用户上传的文件" {
		t.Fatalf("tool exec event = title %q content %q eventType %q ok %v", title, content, eventType, ok)
	}

	title, content, eventType, ok = taskProgressStepFromRuntimeEvent(RuntimeProgressEvent{
		State:  "generated",
		Event:  "file.generated",
		Detail: "写入结果文件",
	}, 150)
	if !ok || eventType != "file.generated" || title != "file generated" || content != "写入结果文件" {
		t.Fatalf("file generated event = title %q content %q eventType %q ok %v", title, content, eventType, ok)
	}

	for _, event := range []RuntimeProgressEvent{
		{State: "process", Event: "tool.process", Detail: "处理数据"},
		{State: "started", Event: "skill.started", Detail: "启动 skill"},
		{State: "completed", Event: "skill.completed", Detail: "skill 完成"},
	} {
		_, _, eventType, ok = taskProgressStepFromRuntimeEvent(event, 150)
		if !ok || eventType != event.Event {
			t.Fatalf("allowed event %#v mapped to eventType %q ok %v", event, eventType, ok)
		}
	}

	for _, tc := range []struct {
		name      string
		event     RuntimeProgressEvent
		eventType string
		title     string
		content   string
	}{
		{
			name:      "tool output read event",
			event:     RuntimeProgressEvent{Event: "tool output read", Text: "工具输出内容"},
			eventType: "tool.output",
			title:     "tool.output",
			content:   "工具输出内容",
		},
		{
			name:      "tool write state",
			event:     RuntimeProgressEvent{State: "tool_write", Event: "chat", Text: "写入结果"},
			eventType: "tool.output",
			title:     "tool.output",
			content:   "写入结果",
		},
		{
			name:      "generic tool events running state",
			event:     RuntimeProgressEvent{State: "running", Event: "tool-events", Detail: "工具正在运行"},
			eventType: "tool.exec",
			title:     "running",
			content:   "工具正在运行",
		},
		{
			name:      "generic tool events processing state",
			event:     RuntimeProgressEvent{State: "processing", Event: "tool-events", Detail: "工具正在处理"},
			eventType: "tool.process",
			title:     "processing",
			content:   "工具正在处理",
		},
		{
			name:      "chat thinking state",
			event:     RuntimeProgressEvent{State: "thinking", Event: "chat", Text: "正在思考"},
			eventType: "chat.progress",
			title:     "chat thinking",
			content:   "正在思考",
		},
		{
			name:      "assistant plan event",
			event:     RuntimeProgressEvent{Event: "assistant.plan", Text: "准备计划"},
			eventType: "chat.progress",
			title:     "assistant plan",
			content:   "准备计划",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			title, content, eventType, ok := taskProgressStepFromRuntimeEvent(tc.event, 150)
			if !ok || eventType != tc.eventType || title != tc.title || content != tc.content {
				t.Fatalf("mapped to title %q content %q eventType %q ok %v, want title %q content %q eventType %q",
					title,
					content,
					eventType,
					ok,
					tc.title,
					tc.content,
					tc.eventType,
				)
			}
		})
	}
}

func TestTaskProgressTextCleanupTruncatesAtConfiguredLimit(t *testing.T) {
	t.Parallel()

	cleaned, truncated, ok := cleanTaskProgressText(strings.Repeat("中", 151), 150)
	if !ok {
		t.Fatal("cleanTaskProgressText() ok = false")
	}
	if !truncated {
		t.Fatal("truncated = false, want true")
	}
	if !strings.HasSuffix(cleaned, "...") {
		t.Fatalf("cleaned missing ellipsis: %q", cleaned)
	}
	if got := len([]rune(strings.TrimSuffix(cleaned, "..."))); got != 150 {
		t.Fatalf("truncated rune length = %d, want 150", got)
	}
}

type memoryTaskProgressStore struct {
	mu     sync.Mutex
	state  TaskProgressState
	ok     bool
	saves  int
	delete int
}

func (s *memoryTaskProgressStore) LoadTaskProgress(_ context.Context, _ string, _ string) (TaskProgressState, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.state, s.ok, nil
}

func (s *memoryTaskProgressStore) SaveTaskProgress(_ context.Context, _ string, _ string, state TaskProgressState, _ time.Duration) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.state = state
	s.ok = true
	s.saves++
	return nil
}

func (s *memoryTaskProgressStore) DeleteTaskProgress(_ context.Context, _ string, _ string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ok = false
	s.delete++
	return nil
}

type blockingTaskProgressStore struct {
	saveStarted chan struct{}
	releaseSave chan struct{}
	once        sync.Once
}

func (s *blockingTaskProgressStore) LoadTaskProgress(_ context.Context, _ string, _ string) (TaskProgressState, bool, error) {
	return TaskProgressState{}, false, nil
}

func (s *blockingTaskProgressStore) SaveTaskProgress(_ context.Context, _ string, _ string, _ TaskProgressState, _ time.Duration) error {
	s.once.Do(func() {
		close(s.saveStarted)
	})
	<-s.releaseSave
	return nil
}

func (s *blockingTaskProgressStore) DeleteTaskProgress(_ context.Context, _ string, _ string) error {
	return nil
}
