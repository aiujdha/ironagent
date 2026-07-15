package runtimepool

import (
	"context"
	"fmt"
	"testing"
	"time"

	"ironagent/go-gateway/internal/core"
)

func TestRegistryScaleSelectionSmoke(t *testing.T) {
	t.Parallel()

	for _, runtimeCount := range []int{500, 1000, 2000} {
		runtimeCount := runtimeCount
		t.Run(fmt.Sprintf("%d_runtimes", runtimeCount), func(t *testing.T) {
			t.Parallel()

			registry := NewRegistryWithStore(10, time.Minute, nil, nil)
			for i := 0; i < runtimeCount; i++ {
				_, err := registry.Heartbeat(RuntimeRecord{
					AssistantID:     fmt.Sprintf("assistant-%04d", i),
					InternalBaseURL: fmt.Sprintf("http://assistant-%04d:18789", i),
					Status:          "ready",
					Capacity:        10,
					InFlight:        i % 7,
					Capabilities:    []string{"analysis", "chat"},
				})
				if err != nil {
					t.Fatalf("heartbeat runtime %d: %v", i, err)
				}
			}

			const iterations = 100
			startedAt := time.Now()
			for i := 0; i < iterations; i++ {
				selected, err := registry.SelectTarget(context.Background(), core.RuntimeScheduleRequest{
					SourceAssistantID:    "assistant-source",
					RequiredCapabilities: []string{"analysis"},
				})
				if err != nil {
					t.Fatalf("SelectTarget() error = %v", err)
				}
				if selected == "" {
					t.Fatalf("SelectTarget() returned empty assistant id")
				}
			}
			elapsed := time.Since(startedAt)
			t.Logf("runtime_count=%d iterations=%d total=%s avg=%s", runtimeCount, iterations, elapsed, elapsed/iterations)
		})
	}
}

func TestRegistryDeleteRemovesRuntime(t *testing.T) {
	t.Parallel()

	registry := NewRegistryWithStore(10, time.Minute, nil, nil)
	if _, err := registry.Heartbeat(RuntimeRecord{
		AssistantID:     "deleted-assistant",
		InternalBaseURL: "http://deleted-assistant:18789",
		Status:          "ready",
		Capacity:        1,
	}); err != nil {
		t.Fatalf("Heartbeat() error = %v", err)
	}

	if err := registry.Delete("deleted-assistant"); err != nil {
		t.Fatalf("Delete() error = %v", err)
	}

	if _, ok := registry.Get("deleted-assistant"); ok {
		t.Fatalf("deleted runtime is still registered")
	}
}
