package webhook

import "strings"

func SupersedesPendingRetrying(event Event) bool {
	switch strings.TrimSpace(event.EventType) {
	case "task.retrying",
		"task.completed",
		"task.failed",
		"task.cancelled",
		"task.canceled",
		"task.rejected":
		return true
	default:
		return false
	}
}
