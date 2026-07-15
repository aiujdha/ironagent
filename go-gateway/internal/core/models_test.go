package core

import (
	"encoding/json"
	"reflect"
	"testing"
	"time"
)

func TestNewTaskResponseRestoresResultFileIDsAfterJSONRoundTrip(t *testing.T) {
	t.Parallel()

	task := TaskView{
		ID:                    "task-json-result-files",
		Status:                "completed",
		ResultWorkspaceFileID: "file-primary",
		CreatedAt:             time.Now().UTC(),
		Metadata: map[string]any{
			taskMetadataRuntimeResultWorkspaceFileIDs: []string{
				"file-primary",
				"file-extra",
				" ",
				"file-extra",
			},
			"runtime_reply_text": "done",
		},
	}
	encoded, err := json.Marshal(task)
	if err != nil {
		t.Fatalf("json.Marshal(TaskView) error = %v", err)
	}
	var decoded TaskView
	if err := json.Unmarshal(encoded, &decoded); err != nil {
		t.Fatalf("json.Unmarshal(TaskView) error = %v", err)
	}
	if _, ok := decoded.Metadata[taskMetadataRuntimeResultWorkspaceFileIDs].([]any); !ok {
		t.Fatalf("round-tripped result file ids type = %T, want []any", decoded.Metadata[taskMetadataRuntimeResultWorkspaceFileIDs])
	}

	response := NewTaskResponse(decoded)
	got := make([]string, 0, len(response.Files))
	for _, file := range response.Files {
		got = append(got, file.FileID)
	}
	want := []string{"file-primary", "file-extra"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("response file ids = %#v, want %#v", got, want)
	}
	if response.ReplyText != "done" {
		t.Fatalf("ReplyText = %q, want done", response.ReplyText)
	}
}
