#!/usr/bin/env bash
set -u

usage() {
  cat <<'EOF'
Usage:
  bash scripts/telnet_connectivity_monitor.sh <host> <port> [duration] [interval_seconds] [connect_timeout_seconds]

Examples:
  bash scripts/telnet_connectivity_monitor.sh 192.0.2.10 8135 30m 5 3
  bash scripts/telnet_connectivity_monitor.sh 192.0.2.10 8135 1h 5 3

Notes:
  - This checks TCP connectivity only. It does not prove application-level code=0.
  - When duration is at least 1h, the final summary includes both first-30m and first-1h success rates.
  - Output is written under ./telnet-monitor-<host>-<port>-<timestamp>/.
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] || [ "$#" -lt 2 ]; then
  usage
  exit 1
fi

HOST="$1"
PORT="$2"
DURATION_RAW="${3:-30m}"
INTERVAL_SECONDS="${4:-5}"
CONNECT_TIMEOUT_SECONDS="${5:-3}"

parse_duration_seconds() {
  local raw="$1"
  case "$raw" in
    *h) echo "$(( ${raw%h} * 3600 ))" ;;
    *m) echo "$(( ${raw%m} * 60 ))" ;;
    *s) echo "${raw%s}" ;;
    *) echo "$raw" ;;
  esac
}

now_ms() {
  local value
  value="$(date +%s%3N 2>/dev/null || true)"
  case "$value" in
    ''|*[!0-9]*) ;;
    *)
      echo "$value"
      return
      ;;
  esac
  if command -v python3 >/dev/null 2>&1; then
    python3 - <<'PY'
import time
print(int(time.time() * 1000))
PY
    return
  fi
  echo "$(( $(date +%s) * 1000 ))"
}

timestamp() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

sanitize() {
  printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_'
}

run_with_timeout() {
  local seconds="$1"
  shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$seconds" "$@"
  elif command -v gtimeout >/dev/null 2>&1; then
    gtimeout "$seconds" "$@"
  else
    "$@"
  fi
}

tcp_probe() {
  if command -v telnet >/dev/null 2>&1; then
    run_with_timeout "$CONNECT_TIMEOUT_SECONDS" telnet "$HOST" "$PORT" </dev/null 2>&1
    return $?
  fi
  if command -v nc >/dev/null 2>&1; then
    run_with_timeout "$CONNECT_TIMEOUT_SECONDS" nc -vz "$HOST" "$PORT" 2>&1
    return $?
  fi
  echo "ERROR: neither telnet nor nc is available"
  return 127
}

is_success_output() {
  printf '%s\n' "$1" | grep -Eiq 'Connected to|Escape character is|open|succeeded|Connection to .* succeeded'
}

DURATION_SECONDS="$(parse_duration_seconds "$DURATION_RAW")"
case "$DURATION_SECONDS" in
  ''|*[!0-9]*)
    echo "ERROR: invalid duration: $DURATION_RAW"
    exit 1
    ;;
esac

case "$INTERVAL_SECONDS" in
  ''|*[!0-9]*)
    echo "ERROR: invalid interval_seconds: $INTERVAL_SECONDS"
    exit 1
    ;;
esac

case "$CONNECT_TIMEOUT_SECONDS" in
  ''|*[!0-9]*)
    echo "ERROR: invalid connect_timeout_seconds: $CONNECT_TIMEOUT_SECONDS"
    exit 1
    ;;
esac

if ! command -v telnet >/dev/null 2>&1 && ! command -v nc >/dev/null 2>&1; then
  echo "ERROR: neither telnet nor nc is available"
  exit 1
fi

if ! command -v timeout >/dev/null 2>&1 && ! command -v gtimeout >/dev/null 2>&1; then
  echo "WARN: timeout/gtimeout not found. A blocked telnet may wait for system TCP timeout."
fi

SAFE_HOST="$(sanitize "$HOST")"
START_TAG="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="./telnet-monitor-${SAFE_HOST}-${PORT}-${START_TAG}"
DETAIL_LOG="$OUT_DIR/detail.tsv"
RAW_LOG="$OUT_DIR/raw.log"
SUMMARY_FILE="$OUT_DIR/summary.txt"
mkdir -p "$OUT_DIR" || exit 1

START_EPOCH="$(date +%s)"
END_EPOCH="$(( START_EPOCH + DURATION_SECONDS ))"

printf 'time_utc\thost\tport\tresult\tlatency_ms\texit_code\tmessage\n' > "$DETAIL_LOG"

total=0
success=0
failure=0
total_30m=0
success_30m=0
failure_30m=0
total_1h=0
success_1h=0
failure_1h=0
max_latency=0
min_latency=''
last_result=''
last_change_time=''
current_down_start=''
down_windows=0
longest_down_seconds=0

echo "== TCP connectivity monitor =="
echo "host: $HOST"
echo "port: $PORT"
echo "duration: $DURATION_RAW (${DURATION_SECONDS}s)"
echo "interval_seconds: $INTERVAL_SECONDS"
echo "connect_timeout_seconds: $CONNECT_TIMEOUT_SECONDS"
echo "output_dir: $OUT_DIR"
echo "probe_tool: $(command -v telnet >/dev/null 2>&1 && echo telnet || echo nc)"
echo ""

while [ "$(date +%s)" -lt "$END_EPOCH" ]; do
  probe_time="$(timestamp)"
  start_ms="$(now_ms)"
  output="$(tcp_probe)"
  exit_code="$?"
  end_ms="$(now_ms)"
  now_epoch="$(date +%s)"
  latency_ms="$(( end_ms - start_ms ))"

  total="$(( total + 1 ))"
  if [ "$latency_ms" -gt "$max_latency" ]; then
    max_latency="$latency_ms"
  fi
  if [ -z "$min_latency" ] || [ "$latency_ms" -lt "$min_latency" ]; then
    min_latency="$latency_ms"
  fi

  compact_message="$(printf '%s' "$output" | tr '\r\n\t' ' ' | sed 's/  */ /g' | cut -c 1-300)"
  if is_success_output "$output"; then
    result="OK"
    success="$(( success + 1 ))"
  else
    result="FAIL"
    failure="$(( failure + 1 ))"
  fi

  elapsed_seconds="$(( now_epoch - START_EPOCH ))"
  if [ "$elapsed_seconds" -le 1800 ]; then
    total_30m="$(( total_30m + 1 ))"
    if [ "$result" = "OK" ]; then
      success_30m="$(( success_30m + 1 ))"
    else
      failure_30m="$(( failure_30m + 1 ))"
    fi
  if [ "$elapsed_seconds" -le 3600 ]; then
    total_1h="$(( total_1h + 1 ))"
    if [ "$result" = "OK" ]; then
      success_1h="$(( success_1h + 1 ))"
    else
      failure_1h="$(( failure_1h + 1 ))"
    fi

  if [ "$result" != "$last_result" ]; then
    last_change_time="$probe_time"
    last_result="$result"
  fi

  if [ "$result" = "FAIL" ]; then
    if [ -z "$current_down_start" ]; then
      current_down_start="$now_epoch"
      down_windows="$(( down_windows + 1 ))"
    fi
  else
    if [ -n "$current_down_start" ]; then
      down_seconds="$(( now_epoch - current_down_start ))"
      if [ "$down_seconds" -gt "$longest_down_seconds" ]; then
        longest_down_seconds="$down_seconds"
      fi
      current_down_start=''
    fi

  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$probe_time" "$HOST" "$PORT" "$result" "$latency_ms" "$exit_code" "$compact_message" \
    >> "$DETAIL_LOG"

  {
    echo "[$probe_time] result=$result latency_ms=$latency_ms exit_code=$exit_code"
    printf '%s\n' "$output"
    echo ""
  } >> "$RAW_LOG"

  echo "[$probe_time] $result latency=${latency_ms}ms $compact_message"
  sleep "$INTERVAL_SECONDS"
done

if [ -n "$current_down_start" ]; then
  final_down_seconds="$(( $(date +%s) - current_down_start ))"
  if [ "$final_down_seconds" -gt "$longest_down_seconds" ]; then
    longest_down_seconds="$final_down_seconds"
  fi

if [ "$total" -gt 0 ]; then
  success_rate="$(awk "BEGIN { printf \"%.2f\", ($success / $total) * 100 }")"
else
  success_rate="0.00"
fi

if [ "$total_30m" -gt 0 ]; then
  success_rate_30m="$(awk "BEGIN { printf \"%.2f\", ($success_30m / $total_30m) * 100 }")"
else
  success_rate_30m="0.00"
fi

if [ "$total_1h" -gt 0 ]; then
  success_rate_1h="$(awk "BEGIN { printf \"%.2f\", ($success_1h / $total_1h) * 100 }")"
else
  success_rate_1h="0.00"
fi

{
  echo "== Summary =="
  echo "host: $HOST"
  echo "port: $PORT"
  echo "duration: $DURATION_RAW (${DURATION_SECONDS}s)"
  echo "interval_seconds: $INTERVAL_SECONDS"
  echo "connect_timeout_seconds: $CONNECT_TIMEOUT_SECONDS"
  echo "total_probes: $total"
  echo "success: $success"
  echo "failure: $failure"
  echo "success_rate_percent: $success_rate"
  echo ""
  echo "== Window Success Rates =="
  echo "first_30m_total_probes: $total_30m"
  echo "first_30m_success: $success_30m"
  echo "first_30m_failure: $failure_30m"
  echo "first_30m_success_rate_percent: $success_rate_30m"
  echo "first_1h_total_probes: $total_1h"
  echo "first_1h_success: $success_1h"
  echo "first_1h_failure: $failure_1h"
  echo "first_1h_success_rate_percent: $success_rate_1h"
  echo ""
  echo "== Stability =="
  echo "min_latency_ms: ${min_latency:-0}"
  echo "max_latency_ms: $max_latency"
  echo "down_windows: $down_windows"
  echo "longest_down_seconds: $longest_down_seconds"
  echo "last_result: ${last_result:-N/A}"
  echo "last_change_time_utc: ${last_change_time:-N/A}"
  echo "detail_log: $DETAIL_LOG"
  echo "raw_log: $RAW_LOG"
} | tee "$SUMMARY_FILE"

echo ""
echo "Summary saved: $SUMMARY_FILE"
