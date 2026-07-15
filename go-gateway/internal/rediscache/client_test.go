package rediscache

import (
	"strings"
	"testing"
)

func TestCompactStrings(t *testing.T) {
	got := strings.Join(compactStrings([]string{" redis-sentinel:26379 ", "", " redis-sentinel-2:26379 "}), ",")
	want := "redis-sentinel:26379,redis-sentinel-2:26379"
	if got != want {
		t.Fatalf("compactStrings() = %q, want %q", got, want)
	}
}

func TestSafeKeyPart(t *testing.T) {
	if got := safeKeyPart("assistant-11100"); got != "assistant-11100" {
		t.Fatalf("safeKeyPart(simple) = %q", got)
	}
	if got := safeKeyPart("user:with:colon"); !strings.HasPrefix(got, "sha256.") {
		t.Fatalf("safeKeyPart(unsafe) = %q, want sha256 prefix", got)
	}
}
