package southbound

import (
	"bytes"
	"fmt"
	"strings"
	"time"
)

type Time struct {
	time.Time
}

func (t *Time) UnmarshalJSON(data []byte) error {
	trimmed := bytes.TrimSpace(data)
	if bytes.Equal(trimmed, []byte("null")) || bytes.Equal(trimmed, []byte(`""`)) {
		t.Time = time.Time{}
		return nil
	}
	parsed, err := parseSouthboundTime(strings.Trim(string(trimmed), `"`))
	if err != nil {
		return err
	}
	t.Time = parsed
	return nil
}

func (t Time) MarshalJSON() ([]byte, error) {
	return t.Time.MarshalJSON()
}

func parseSouthboundTime(raw string) (time.Time, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return time.Time{}, nil
	}
	for _, layout := range []string{
		time.RFC3339Nano,
		"2006-01-02T15:04:05.999999",
		"2006-01-02T15:04:05",
		"2006-01-02 15:04:05.999999",
		"2006-01-02 15:04:05",
	} {
		if parsed, err := time.Parse(layout, raw); err == nil {
			return parsed.UTC(), nil
		}
	}
	return time.Time{}, fmt.Errorf("parse southbound time %q", raw)
}
