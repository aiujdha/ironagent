package core

import (
	"strings"
	"testing"
)

func TestChatQuestionLogAttrsRecordsQuestion(t *testing.T) {
	attrs := logAttrsMap(chatQuestionLogAttrs(ChatRequest{Message: "  上游问题是什么  "}))

	if got := attrs["question_source"]; got != "message" {
		t.Fatalf("question_source = %#v, want message", got)
	}
	if got := attrs["question_text"]; got != "上游问题是什么" {
		t.Fatalf("question_text = %#v, want trimmed question", got)
	}
	if got := attrs["question_chars"]; got != 7 {
		t.Fatalf("question_chars = %#v, want 7", got)
	}
	if got := attrs["question_truncated"]; got != false {
		t.Fatalf("question_truncated = %#v, want false", got)
	}
	if got, _ := attrs["question_sha256"].(string); strings.TrimSpace(got) == "" {
		t.Fatal("question_sha256 is empty")
	}
}

func TestChatQuestionLogAttrsTruncatesLongQuestion(t *testing.T) {
	question := strings.Repeat("问", chatQuestionLogMaxRunes+1)
	attrs := logAttrsMap(chatQuestionLogAttrs(ChatRequest{Message: question}))

	text, ok := attrs["question_text"].(string)
	if !ok {
		t.Fatalf("question_text = %#v, want string", attrs["question_text"])
	}
	if got := len([]rune(text)); got != chatQuestionLogMaxRunes {
		t.Fatalf("logged question runes = %d, want %d", got, chatQuestionLogMaxRunes)
	}
	if got := attrs["question_chars"]; got != chatQuestionLogMaxRunes+1 {
		t.Fatalf("question_chars = %#v, want original length", got)
	}
	if got := attrs["question_truncated"]; got != true {
		t.Fatalf("question_truncated = %#v, want true", got)
	}
}

func TestQueuedTaskClaimLogAttrsIncludesTaskDetails(t *testing.T) {
	attrs := logAttrsMap(queuedTaskClaimLogAttrs([]GatewayTaskQueueItem{
		{
			TaskID:       "gw-1",
			Status:       "processing",
			LockedBy:     "worker-1",
			AttemptToken: "attempt-1",
			Attempts:     2,
			Request: ChatRequest{
				RequestID: "req-1",
				UserID:    "user-1",
			},
		},
	}))

	taskIDs, ok := attrs["claimed_task_ids"].([]string)
	if !ok || len(taskIDs) != 1 || taskIDs[0] != "gw-1" {
		t.Fatalf("claimed_task_ids = %#v, want [gw-1]", attrs["claimed_task_ids"])
	}
	tasks, ok := attrs["claimed_tasks"].([]map[string]any)
	if !ok || len(tasks) != 1 {
		t.Fatalf("claimed_tasks = %#v, want one task", attrs["claimed_tasks"])
	}
	if got := tasks[0]["request_id"]; got != "req-1" {
		t.Fatalf("claimed task request_id = %#v, want req-1", got)
	}
	if got := tasks[0]["attempts"]; got != 2 {
		t.Fatalf("claimed task attempts = %#v, want 2", got)
	}
}

func logAttrsMap(attrs []any) map[string]any {
	out := make(map[string]any)
	for i := 0; i+1 < len(attrs); i += 2 {
		key, _ := attrs[i].(string)
		if key == "" {
			continue
		}
		out[key] = attrs[i+1]
	}
	return out
}
