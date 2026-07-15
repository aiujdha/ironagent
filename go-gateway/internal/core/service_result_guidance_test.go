package core

import (
	"strings"
	"testing"
)

func TestRuntimeResultGuidanceAppendsWhenPromptOnlyMentionsMarker(t *testing.T) {
	message := "请严格按照 Gateway 自动追加的 [Runtime result file handling] 指示创建结果文件。"

	got := withRuntimeResultFileGuidance(message, "southbound-task-001")

	if !strings.Contains(got, message) {
		t.Fatalf("original message missing from guidance: %s", got)
	}
	for _, want := range []string{
		"[Runtime result file handling]",
		"/workspace/temp/go-gateway-results/southbound-task-001",
		"/workspace/persistent/go-gateway-results/southbound-task-001",
		"不要把本次任务的结果文件写入其他任务目录",
		"终态 files[] 才是权威结果引用",
	} {
		if !strings.Contains(got, want) {
			t.Fatalf("guidance missing %q: %s", want, got)
		}
	}
	if strings.Contains(got, "实际绝对路径") {
		t.Fatalf("guidance should not ask runtime to restate result paths: %s", got)
	}
	if strings.Count(got, "[Runtime result file handling]") != 2 {
		t.Fatalf("marker count = %d, want original mention plus appended section: %s", strings.Count(got, "[Runtime result file handling]"), got)
	}
}

func TestRuntimeResultGuidanceDoesNotDuplicateActualTaskScopedPath(t *testing.T) {
	message := strings.Join([]string{
		"请生成文件。",
		"",
		"[Runtime result file handling]",
		"默认写入 /workspace/temp/go-gateway-results/southbound-task-002/。",
	}, "\n")

	got := withRuntimeResultFileGuidance(message, "southbound-task-002")

	if got != strings.TrimSpace(message) {
		t.Fatalf("guidance duplicated existing concrete section:\n%s", got)
	}
}
