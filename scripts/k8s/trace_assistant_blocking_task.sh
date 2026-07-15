#!/usr/bin/env bash
# Trace where an assistant chat task gets stuck while generating an Excel file.

set -Eeuo pipefail

NS="ironagent-kf"
ASSISTANT_ID="apply-test"
CONTAINER=""
SINCE="30m"
TAIL_LINES="260"
OUT_BASE="output/assistant-task-trace"
WATCH_SECONDS="300"
POLL_SECONDS="300"
POLL_INTERVAL="5"
SUBMIT_TASK="false"
TASK_ID=""
KEYWORD=""
EXPECTED_OUTPUT="/workspace/persistent/财经快讯总结_202605.xlsx"
PROMPT=""
PROMPT_FILE=""
GATEWAY_URL=""
LOCAL_GATEWAY_PORT="18080"
NO_PORT_FORWARD="false"
GATEWAY_API_KEY="${GO_GATEWAY_API_KEY:-}"

FOLLOW_PIDS=()
PF_PID=""
POD_NAME=""
RUN_ID=""
OUT_DIR=""
RUN_LOG=""
SUMMARY=""
MATCHED_ERRORS=""
TASK_POLL_LOG=""
TASK_LAST_JSON=""
CHAT_RESPONSE_JSON=""
PROMPT_TXT=""

usage() {
  cat <<'USAGE'
用法:
  bash scripts/k8s/trace_assistant_blocking_task.sh [选项]

目标:
  定位 apply-test 助手执行“读取工作区 md -> 生成 Excel”时卡住在哪一层。
  脚本会输出中文日志，并采集助手 Pod、go-gateway、admin、security-service 日志，
  同时检查 workspace 文件落盘、Python Excel 依赖、任务状态和常见阻塞特征。

默认行为:
  默认不主动提交任务，只开启 300 秒复现窗口。窗口内请在浏览器里执行图中的提示词。

选项:
  --assistant-id <id>        助手 ID，默认 apply-test。
  --namespace <ns>           Kubernetes namespace，默认 ironagent-kf；若不存在会自动尝试 ironagent。
  --container <name>         助手 Pod 内容器名，默认自动选 ironagent-gateway 或第一个容器。
  --since <duration>         采集最近多久的历史日志，默认 30m。
  --tail <n>                 每个服务历史日志最多采集多少行，默认 260。
  --out-dir <dir>            输出目录根路径，默认 output/assistant-task-trace。
  --watch-seconds <n>        复现窗口秒数，默认 300；设为 0 则不等待页面复现。
  --expected-output <path>   预期生成的文件路径，默认 /workspace/persistent/财经快讯总结_202605.xlsx。
  --task-id <id>             已知 Gateway task_id 时传入，脚本会轮询任务状态。
  --keyword <text>           额外关键词，用于从日志中提取相关行。
  --submit                   由脚本直接向 Gateway /api/v1/chat 提交默认/指定 prompt。
  --prompt <text>            --submit 时使用的 prompt。
  --prompt-file <file>       --submit 时从文件读取 prompt。
  --gateway-url <url>        Gateway 地址；不提供时默认 port-forward deploy/go-gateway 到本机。
  --local-gateway-port <n>   port-forward 本地端口，默认 18080。
  --no-port-forward          不自动 port-forward，必须配合 --gateway-url 使用。
  --gateway-api-key <key>    可选。Gateway 开启 API key 鉴权时使用，只写入脱敏日志。
  -h, --help                 显示帮助。

示例:
  # 最推荐：先开追踪窗口，再去页面手动执行图 1 的 prompt
  bash scripts/k8s/trace_assistant_blocking_task.sh --assistant-id apply-test --namespace ironagent-kf --watch-seconds 300

  # 如果已经知道 task_id，则聚焦轮询任务状态
  bash scripts/k8s/trace_assistant_blocking_task.sh --assistant-id apply-test --task-id gw-xxx --watch-seconds 120

  # 由脚本直接提交 prompt 复现
  bash scripts/k8s/trace_assistant_blocking_task.sh --assistant-id apply-test --submit --watch-seconds 0

输出:
  run.log                  全量中文执行日志。
  summary.txt              根因候选和关键证据。
  matched-errors.log       从所有日志中提取的错误/阻塞特征。
  task-poll.jsonl          任务状态轮询记录（当 --submit 或 --task-id 时生成）。
  logs/*.log               助手和控制面 follow 日志。
  k8s/*.txt                Pod、workspace、进程、依赖等快照。
USAGE
}

timestamp() {
  date '+%F %T'
}

safe_name() {
  printf '%s' "$1" | tr -c 'A-Za-z0-9_.-' '_' | sed 's/^_*//;s/_*$//'
}

log() {
  printf '[%s] [信息] %s\n' "$(timestamp)" "$*"
}

warn() {
  printf '[%s] [警告] %s\n' "$(timestamp)" "$*" >&2
}

fail() {
  printf '[%s] [错误] %s\n' "$(timestamp)" "$*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "缺少命令: $1"
}

cleanup() {
  local pid
  for pid in "${FOLLOW_PIDS[@]:-}"; do
    kill "$pid" >/dev/null 2>&1 || true
  done
  for pid in "${FOLLOW_PIDS[@]:-}"; do
    wait "$pid" >/dev/null 2>&1 || true
  done
  if [[ -n "${PF_PID:-}" ]]; then
    kill "$PF_PID" >/dev/null 2>&1 || true
    wait "$PF_PID" >/dev/null 2>&1 || true
  fi
}

on_error() {
  local line_no="$1"
  local exit_code=$?
  warn "脚本在第 ${line_no} 行失败，退出码=${exit_code}。已保留目前采集到的日志：${OUT_DIR:-未初始化}"
  cleanup
  exit "$exit_code"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --assistant-id)
      ASSISTANT_ID="${2:-}"
      shift 2
      ;;
    --namespace)
      NS="${2:-}"
      shift 2
      ;;
    --container)
      CONTAINER="${2:-}"
      shift 2
      ;;
    --since)
      SINCE="${2:-}"
      shift 2
      ;;
    --tail)
      TAIL_LINES="${2:-}"
      shift 2
      ;;
    --out-dir)
      OUT_BASE="${2:-}"
      shift 2
      ;;
    --watch-seconds)
      WATCH_SECONDS="${2:-}"
      shift 2
      ;;
    --poll-seconds)
      POLL_SECONDS="${2:-}"
      shift 2
      ;;
    --poll-interval)
      POLL_INTERVAL="${2:-}"
      shift 2
      ;;
    --expected-output)
      EXPECTED_OUTPUT="${2:-}"
      shift 2
      ;;
    --task-id)
      TASK_ID="${2:-}"
      shift 2
      ;;
    --keyword)
      KEYWORD="${2:-}"
      shift 2
      ;;
    --submit)
      SUBMIT_TASK="true"
      shift
      ;;
    --prompt)
      PROMPT="${2:-}"
      shift 2
      ;;
    --prompt-file)
      PROMPT_FILE="${2:-}"
      shift 2
      ;;
    --gateway-url)
      GATEWAY_URL="${2:-}"
      shift 2
      ;;
    --local-gateway-port)
      LOCAL_GATEWAY_PORT="${2:-}"
      shift 2
      ;;
    --no-port-forward)
      NO_PORT_FORWARD="true"
      shift
      ;;
    --gateway-api-key)
      GATEWAY_API_KEY="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "未知参数: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$ASSISTANT_ID" ]]; then
  echo "assistant-id 不能为空" >&2
  exit 2
fi

RUN_ID="$(date '+%Y%m%d%H%M%S')"
OUT_DIR="${OUT_BASE%/}/$(safe_name "$ASSISTANT_ID")-${RUN_ID}"
RUN_LOG="$OUT_DIR/run.log"
SUMMARY="$OUT_DIR/summary.txt"
MATCHED_ERRORS="$OUT_DIR/matched-errors.log"
TASK_POLL_LOG="$OUT_DIR/task-poll.jsonl"
TASK_LAST_JSON="$OUT_DIR/task-last.json"
CHAT_RESPONSE_JSON="$OUT_DIR/chat-submit-response.json"
PROMPT_TXT="$OUT_DIR/prompt.txt"
FOCUS_LINES="$OUT_DIR/focus-lines.log"
RELEVANT_LINES="$OUT_DIR/relevant-lines.log"
CONTROL_PLANE_FINDINGS="$OUT_DIR/control-plane-findings.log"
TRACE_MARKER="/workspace/temp/.iron_agent_task_trace_${RUN_ID}.marker"

mkdir -p "$OUT_DIR/k8s" "$OUT_DIR/logs" "$OUT_DIR/http"
touch "$RUN_LOG"
exec > >(tee -a "$RUN_LOG") 2>&1
trap 'on_error $LINENO' ERR
trap cleanup EXIT

run_capture() {
  local label="$1"
  local output_file="$2"
  shift 2
  log "采集：${label}"
  {
    echo "===== ${label} ====="
    echo "命令: $*"
    echo
    "$@"
  } >"$output_file" 2>&1 || {
    warn "采集失败：${label}，详情见 ${output_file}"
    return 1
}
  }

kubectl_exec() {
  kubectl -n "$NS" exec "$POD_NAME" -c "$CONTAINER" -- "$@"
}

resolve_namespace() {
  if kubectl get namespace "$NS" >/dev/null 2>&1; then
    return 0
  fi
  if [[ "$NS" != "ironagent" ]] && kubectl get namespace ironagent >/dev/null 2>&1; then
    warn "namespace ${NS} 不存在，自动切换到 ironagent"
    NS="ironagent"
    return 0
  fi
  fail "namespace 不存在或无权限访问：${NS}"
}

find_assistant_pod() {
  local exact="ironagent-assistant-${ASSISTANT_ID}-pod"
  if kubectl -n "$NS" get pod "$exact" >/dev/null 2>&1; then
    printf '%s' "$exact"
    return 0
  fi
  kubectl -n "$NS" get pods -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' \
    | grep -F "$ASSISTANT_ID" \
    | grep -F "assistant" \
    | head -1
}

select_container() {
  if [[ -n "$CONTAINER" ]]; then
    printf '%s' "$CONTAINER"
    return 0
  fi
  local names
  names="$(kubectl -n "$NS" get pod "$POD_NAME" -o jsonpath='{range .spec.containers[*]}{.name}{"\n"}{end}')"
  if printf '%s\n' "$names" | grep -qx 'ironagent-gateway'; then
    printf '%s' "ironagent-gateway"
    return 0
  fi
  printf '%s\n' "$names" | head -1
}

prepare_prompt() {
  if [[ -n "$PROMPT_FILE" ]]; then
    [[ -f "$PROMPT_FILE" ]] || fail "prompt 文件不存在：${PROMPT_FILE}"
    cp "$PROMPT_FILE" "$PROMPT_TXT"
    return 0
  fi
  if [[ -n "$PROMPT" ]]; then
    printf '%s\n' "$PROMPT" >"$PROMPT_TXT"
    return 0
  fi
  cat >"$PROMPT_TXT" <<'PROMPT'
请使用工作空间文件 persistent/service_summary_v4.md 作为输入，生成一份财经快讯总结 Excel，并将结果保存为 /workspace/persistent/财经快讯总结_202605.xlsx。
PROMPT
}

start_follow_logs() {
  log "启动实时日志采集：助手 Pod、go-gateway、admin、security-service"
  kubectl -n "$NS" logs -f "$POD_NAME" -c "$CONTAINER" --since=15s \
    >"$OUT_DIR/logs/assistant-follow.log" 2>&1 &
  FOLLOW_PIDS+=("$!")

  local deploy
  for deploy in go-gateway admin security-service; do
    kubectl -n "$NS" logs -f "deploy/${deploy}" --since=15s \
      >"$OUT_DIR/logs/${deploy}-follow.log" 2>&1 &
    FOLLOW_PIDS+=("$!")
  done
}

stop_follow_logs() {
  local pid
  for pid in "${FOLLOW_PIDS[@]:-}"; do
    kill "$pid" >/dev/null 2>&1 || true
  done
  for pid in "${FOLLOW_PIDS[@]:-}"; do
    wait "$pid" >/dev/null 2>&1 || true
  done
  FOLLOW_PIDS=()
}

collect_history() {
  run_capture "kubectl 当前上下文" "$OUT_DIR/k8s/current-context.txt" kubectl config current-context || true
  run_capture "namespace 状态" "$OUT_DIR/k8s/namespace.txt" kubectl get namespace "$NS" -o wide || true
  run_capture "Pod 列表" "$OUT_DIR/k8s/pods.txt" kubectl -n "$NS" get pods -o wide || true
  run_capture "Service 列表" "$OUT_DIR/k8s/services.txt" kubectl -n "$NS" get svc -o wide || true
  run_capture "Endpoints 列表" "$OUT_DIR/k8s/endpoints.txt" kubectl -n "$NS" get endpoints -o wide || true
  run_capture "近期事件" "$OUT_DIR/k8s/events.txt" kubectl -n "$NS" get events --sort-by=.lastTimestamp || true
  run_capture "助手 Pod 描述" "$OUT_DIR/k8s/assistant-pod-describe.txt" kubectl -n "$NS" describe pod "$POD_NAME" || true
  run_capture "助手历史日志" "$OUT_DIR/k8s/assistant-history.log" kubectl -n "$NS" logs "$POD_NAME" -c "$CONTAINER" --since="$SINCE" --tail="$TAIL_LINES" || true
  run_capture "助手上一轮日志" "$OUT_DIR/k8s/assistant-previous.log" kubectl -n "$NS" logs "$POD_NAME" -c "$CONTAINER" --previous --tail="$TAIL_LINES" || true

  local deploy
  for deploy in go-gateway admin security-service; do
    run_capture "${deploy} 历史日志" "$OUT_DIR/k8s/${deploy}-history.log" kubectl -n "$NS" logs "deploy/${deploy}" --since="$SINCE" --tail="$TAIL_LINES" || true
  done
}

create_trace_marker() {
  run_capture "创建本轮追踪 marker" "$OUT_DIR/k8s/trace-marker.txt" \
    kubectl_exec sh -lc 'marker="$1"; mkdir -p "$(dirname "$marker")"; date -Iseconds > "$marker"; ls -lh "$marker"; echo "TRACE_MARKER_CREATED=$marker"' sh "$TRACE_MARKER"
}

collect_container_snapshot() {
  run_capture "助手容器进程快照" "$OUT_DIR/k8s/process-before.txt" \
    kubectl_exec sh -lc 'ps -ef 2>/dev/null || ps aux 2>/dev/null || true'
  run_capture "助手容器关键环境变量" "$OUT_DIR/k8s/env-summary.txt" \
    kubectl_exec sh -lc 'printenv | sort | grep -E "^(ASSISTANT|IRON_AGENT|IRON_AGENT|WORKSPACE|PYTHON|PATH|HOME|USER)=" | sed -E "s/(TOKEN|KEY|SECRET|PASSWORD)=.*/\\1=***MASKED***/"'
  run_capture "workspace 权限与容量" "$OUT_DIR/k8s/workspace-permissions.txt" \
    kubectl_exec sh -lc 'id; pwd; df -h /workspace 2>/dev/null || true; ls -ld /workspace /workspace/temp /workspace/persistent /workspace/skills /workspace/plugins 2>&1 || true'
  run_capture "workspace 关键文件摘要" "$OUT_DIR/k8s/workspace-summary.txt" \
    kubectl_exec sh -lc 'echo "[persistent]"; find /workspace/persistent -maxdepth 3 -type f -printf "%TY-%Tm-%Td %TH:%TM %s %p\n" 2>/dev/null | sort | tail -120 || true; echo "[skills]"; find /workspace/skills -maxdepth 3 -type f 2>/dev/null | sort | head -160 || true; echo "[recent excel/csv]"; find /workspace -maxdepth 6 \( -iname "*.xlsx" -o -iname "*.xls" -o -iname "*.csv" \) -exec ls -lh {} \; 2>/dev/null | tail -120 || true'
  run_capture "Excel Python 依赖检查" "$OUT_DIR/k8s/python-deps.txt" \
    kubectl_exec python3 - <<'PY'
import importlib
import json
import os
import sys

mods = ["openpyxl", "pandas", "xlsxwriter", "numpy", "olefile", "fitz"]
result = {"python": sys.version, "uid": os.getuid(), "modules": []}
for name in mods:
    try:
        mod = importlib.import_module(name)
        result["modules"].append({"name": name, "ok": True, "version": getattr(mod, "__version__", "")})
    except Exception as exc:
        result["modules"].append({"name": name, "ok": False, "error": repr(exc)})
print(json.dumps(result, ensure_ascii=False, indent=2))
PY

  if [[ -n "$EXPECTED_OUTPUT" ]]; then
    run_capture "预期输出文件检查" "$OUT_DIR/k8s/expected-output-before.txt" \
      kubectl_exec sh -lc 'marker="$1"; path="$2"; echo "trace_marker=$marker"; [ -e "$marker" ] && ls -lh "$marker" || echo "TRACE_MARKER_MISSING"; echo "expected_output=$path"; parent=$(dirname "$path"); prefix=$(basename "$path" | sed "s/\\.[^.]*$//"); if [ -e "$path" ]; then ls -lh "$path"; file "$path" 2>/dev/null || true; if [ -e "$marker" ] && [ "$path" -nt "$marker" ]; then echo "CURRENT_RUN_EXACT_OUTPUT_PRESENT"; else echo "EXACT_OUTPUT_EXISTS_BUT_OLD"; fi; else echo "EXPECTED_OUTPUT_MISSING"; fi; echo "current_run_same_prefix_candidates:"; if [ -e "$marker" ]; then find "$parent" -maxdepth 1 -type f -name "${prefix}*" -newer "$marker" -exec ls -lh {} \; 2>/dev/null || true; fi; echo "historical_same_prefix_candidates:"; find "$parent" -maxdepth 1 -type f -name "${prefix}*" -exec ls -lh {} \; 2>/dev/null | tail -30 || true' sh "$TRACE_MARKER" "$EXPECTED_OUTPUT" || true
  fi
}

collect_after_snapshot() {
  run_capture "助手容器进程复现后快照" "$OUT_DIR/k8s/process-after.txt" \
    kubectl_exec sh -lc 'ps -ef 2>/dev/null || ps aux 2>/dev/null || true'
  run_capture "复现后 workspace 表格文件" "$OUT_DIR/k8s/workspace-excel-after.txt" \
    kubectl_exec sh -lc 'marker="$1"; echo "trace_marker=$marker"; [ -e "$marker" ] && ls -lh "$marker" || echo "TRACE_MARKER_MISSING"; echo "[current_run_excel_csv]"; if [ -e "$marker" ]; then find /workspace -maxdepth 6 \( -iname "*.xlsx" -o -iname "*.xls" -o -iname "*.csv" \) -newer "$marker" -exec ls -lh {} \; 2>/dev/null | tail -160 || true; fi; echo "[all_recent_excel_csv]"; find /workspace -maxdepth 6 \( -iname "*.xlsx" -o -iname "*.xls" -o -iname "*.csv" \) -exec ls -lh {} \; 2>/dev/null | tail -160 || true' sh "$TRACE_MARKER"
  if [[ -n "$EXPECTED_OUTPUT" ]]; then
    run_capture "复现后预期输出文件检查" "$OUT_DIR/k8s/expected-output-after.txt" \
      kubectl_exec sh -lc 'marker="$1"; path="$2"; echo "trace_marker=$marker"; [ -e "$marker" ] && ls -lh "$marker" || echo "TRACE_MARKER_MISSING"; echo "expected_output=$path"; parent=$(dirname "$path"); prefix=$(basename "$path" | sed "s/\\.[^.]*$//"); if [ -e "$path" ]; then ls -lh "$path"; file "$path" 2>/dev/null || true; if [ -e "$marker" ] && [ "$path" -nt "$marker" ]; then echo "CURRENT_RUN_EXACT_OUTPUT_PRESENT"; else echo "EXACT_OUTPUT_EXISTS_BUT_OLD"; fi; python3 - "$path" <<PY
import sys, zipfile
p = sys.argv[1]
try:
    with zipfile.ZipFile(p) as zf:
        names = set(zf.namelist())
    print("XLSX_ZIP_OK", "[Content_Types].xml" in names, "xl/workbook.xml" in names)
except Exception as exc:
    print("XLSX_VALIDATE_FAILED", repr(exc))
PY
else echo "EXPECTED_OUTPUT_MISSING"; fi; echo "current_run_same_prefix_candidates:"; if [ -e "$marker" ]; then find "$parent" -maxdepth 1 -type f -name "${prefix}*" -newer "$marker" -exec ls -lh {} \; 2>/dev/null || true; fi; echo "historical_same_prefix_candidates:"; find "$parent" -maxdepth 1 -type f -name "${prefix}*" -exec ls -lh {} \; 2>/dev/null | tail -30 || true' sh "$TRACE_MARKER" "$EXPECTED_OUTPUT" || true
  fi
}

start_gateway_port_forward() {
  if [[ -n "$GATEWAY_URL" ]]; then
    return 0
  fi
  if [[ "$NO_PORT_FORWARD" == "true" ]]; then
    fail "已指定 --no-port-forward，但没有提供 --gateway-url"
  fi
  GATEWAY_URL="http://127.0.0.1:${LOCAL_GATEWAY_PORT}"
  log "启动 Gateway port-forward：deploy/go-gateway -> ${GATEWAY_URL}"
  kubectl -n "$NS" port-forward deploy/go-gateway "${LOCAL_GATEWAY_PORT}:8080" \
    >"$OUT_DIR/http/go-gateway-port-forward.log" 2>&1 &
  PF_PID="$!"
  sleep 2
  if ! kill -0 "$PF_PID" >/dev/null 2>&1; then
    warn "Gateway port-forward 进程未启动成功，详情见 $OUT_DIR/http/go-gateway-port-forward.log"
    return 1
  fi
}

json_get() {
  local file="$1"
  local expr="$2"
  python3 - "$file" "$expr" <<'PY'
import json
import sys

path, expr = sys.argv[1:3]
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    sys.exit(1)
value = data
for part in expr.split("."):
    if not part:
        continue
    if isinstance(value, dict):
        value = value.get(part, "")
    else:
        value = ""
        break
if value is None:
    value = ""
if isinstance(value, (dict, list)):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
else:
    print(value)
PY
}

curl_gateway_json() {
  local method="$1"
  local path="$2"
  local body_file="$3"
  local out_file="$4"
  local status_file="${out_file}.status"
  local args=(-sS -o "$out_file" -w "%{http_code}" -X "$method" "${GATEWAY_URL%/}${path}" -H "Accept: application/json")
  if [[ -n "$GATEWAY_API_KEY" ]]; then
    args+=(-H "X-API-Key: ${GATEWAY_API_KEY}")
  fi
  if [[ -n "$body_file" ]]; then
    args+=(-H "Content-Type: application/json" --data-binary "@${body_file}")
  fi
  local code
  set +e
  code="$(curl "${args[@]}")"
  local curl_code=$?
  set -e
  printf '%s\n' "${code:-000}" >"$status_file"
  if [[ "$curl_code" -ne 0 ]]; then
    warn "Gateway 请求失败：curl_exit=${curl_code} ${method} ${path}"
    return "$curl_code"
  fi
  if [[ ! "$code" =~ ^2[0-9][0-9]$ ]]; then
    warn "Gateway 返回非 2xx：HTTP ${code} ${method} ${path}，响应见 ${out_file}"
    return 1
  fi
}

submit_chat_task() {
  prepare_prompt
  start_gateway_port_forward || true
  local body_file="$OUT_DIR/chat-submit-body.json"
  local request_id="trace-${ASSISTANT_ID}-${RUN_ID}"
  local conversation_id="trace-${ASSISTANT_ID}-${RUN_ID}"
  python3 - "$PROMPT_TXT" "$body_file" "$request_id" "$conversation_id" <<'PY'
import json
import sys

prompt_path, body_path, request_id, conversation_id = sys.argv[1:5]
message = open(prompt_path, encoding="utf-8").read().strip()
payload = {
    "request_id": request_id,
    "user_id": "trace-user",
    "conversation_id": conversation_id,
    "message": message,
    "files": [],
}
open(body_path, "w", encoding="utf-8").write(json.dumps(payload, ensure_ascii=False, indent=2))
PY
  log "提交 Gateway chat 任务：request_id=${request_id}"
  if curl_gateway_json POST /api/v1/chat "$body_file" "$CHAT_RESPONSE_JSON"; then
    TASK_ID="$(json_get "$CHAT_RESPONSE_JSON" task_id || true)"
    if [[ -z "$TASK_ID" ]]; then
      TASK_ID="$(json_get "$CHAT_RESPONSE_JSON" taskId || true)"
    fi
    if [[ -n "$TASK_ID" ]]; then
      log "Gateway 已创建任务：task_id=${TASK_ID}"
    else
      warn "chat 响应没有 task_id，响应见 ${CHAT_RESPONSE_JSON}"
    fi
  else
    warn "脚本直接提交任务失败；如果是 401，请改用页面手动复现或传 --gateway-api-key。"
    sed -n '1,120p' "$CHAT_RESPONSE_JSON" 2>/dev/null || true
  fi
}

poll_task_status() {
  [[ -n "$TASK_ID" ]] || return 0
  start_gateway_port_forward || true
  log "开始轮询任务状态：task_id=${TASK_ID}，最多 ${POLL_SECONDS} 秒"
  local start now last_status last_stage unchanged same_count poll_file status stage
  start="$(date +%s)"
  last_status=""
  last_stage=""
  same_count=0
  : >"$TASK_POLL_LOG"
  while true; do
    now="$(date +%s)"
    if (( now - start > POLL_SECONDS )); then
      warn "任务轮询超时：${POLL_SECONDS} 秒内没有进入终态"
      break
    fi
    poll_file="$OUT_DIR/http/task-${TASK_ID}-${now}.json"
    if curl_gateway_json GET "/api/v1/tasks/${TASK_ID}" "" "$poll_file"; then
      cp "$poll_file" "$TASK_LAST_JSON"
      python3 - "$poll_file" "$TASK_POLL_LOG" <<'PY'
import json
import sys
from datetime import datetime

payload = json.load(open(sys.argv[1], encoding="utf-8"))
event = {
    "time": datetime.now().isoformat(timespec="seconds"),
    "task_id": payload.get("task_id") or payload.get("id") or "",
    "status": payload.get("status") or "",
    "stage": payload.get("stage") or "",
    "approval_id": payload.get("approval_id") or "",
    "error": payload.get("error") or payload.get("error_message") or "",
    "reply_preview": str(payload.get("reply") or payload.get("content") or "")[:240],
}
with open(sys.argv[2], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
print(json.dumps(event, ensure_ascii=False))
PY
      status="$(json_get "$poll_file" status || true)"
      stage="$(json_get "$poll_file" stage || true)"
      if [[ "$status" == "$last_status" && "$stage" == "$last_stage" ]]; then
        same_count=$((same_count + 1))
      else
        same_count=0
        last_status="$status"
        last_stage="$stage"
      fi
      if (( same_count > 0 && same_count % 6 == 0 )); then
        warn "任务状态 ${same_count} 次轮询未变化：status=${status:-空} stage=${stage:-空}"
      fi
      case "$(printf '%s' "$status" | tr '[:upper:]' '[:lower:]')" in
        completed|succeeded|success|failed|error|cancelled|canceled|rejected|dead)
          log "任务进入终态：status=${status} stage=${stage}"
          break
          ;;
      esac
    else
      warn "任务状态查询失败，继续等待；响应文件：${poll_file}"
    fi
    sleep "$POLL_INTERVAL"
  done
}

wait_manual_window() {
  if [[ "$WATCH_SECONDS" =~ ^[0-9]+$ ]] && [[ "$WATCH_SECONDS" -gt 0 ]]; then
    log "进入页面复现窗口：${WATCH_SECONDS} 秒。请现在在 apply-test 页面执行图 1 的提示词。"
    sleep "$WATCH_SECONDS"
    log "页面复现窗口结束。"
  fi
}

grep_source_logs() {
  local pattern="$1"
  {
    grep -RInE "$pattern" "$OUT_DIR/k8s" "$OUT_DIR/logs" "$OUT_DIR/http" 2>/dev/null || true
    if [[ -f "$TASK_POLL_LOG" ]]; then
      grep -InE "$pattern" "$TASK_POLL_LOG" 2>/dev/null || true
    fi
    if [[ -f "$CHAT_RESPONSE_JSON" ]]; then
      grep -InE "$pattern" "$CHAT_RESPONSE_JSON" 2>/dev/null || true
    fi
  } | grep -vF "$MATCHED_ERRORS" \
    | grep -vF "$SUMMARY" \
    | grep -vF "$RUN_LOG" \
    || true
}

grep_focus_logs() {
  local value="$1"
  [[ -n "$value" ]] || return 0
  {
    grep -RInF "$value" "$OUT_DIR/k8s" "$OUT_DIR/logs" "$OUT_DIR/http" 2>/dev/null || true
    if [[ -f "$TASK_POLL_LOG" ]]; then
      grep -InF "$value" "$TASK_POLL_LOG" 2>/dev/null || true
    fi
    if [[ -f "$CHAT_RESPONSE_JSON" ]]; then
      grep -InF "$value" "$CHAT_RESPONSE_JSON" 2>/dev/null || true
    fi
  } | grep -vF "$MATCHED_ERRORS" \
    | grep -vF "$SUMMARY" \
    | grep -vF "$RUN_LOG" \
    | grep -vF "$FOCUS_LINES" \
    || true
}

build_relevant_lines() {
  {
    echo "===== assistant pod logs: ${POD_NAME} ====="
    for file in \
      "$OUT_DIR/k8s/assistant-history.log" \
      "$OUT_DIR/k8s/assistant-previous.log" \
      "$OUT_DIR/logs/assistant-follow.log"; do
      if [[ -f "$file" ]]; then
        echo "--- ${file} ---"
        cat "$file"
      fi
    done

    echo "===== task and submit logs ====="
    for file in "$TASK_POLL_LOG" "$TASK_LAST_JSON" "$CHAT_RESPONSE_JSON"; do
      if [[ -f "$file" ]]; then
        echo "--- ${file} ---"
        cat "$file"
      fi
    done

    echo "===== workspace/output snapshots ====="
    for file in \
      "$OUT_DIR/k8s/trace-marker.txt" \
      "$OUT_DIR/k8s/expected-output-before.txt" \
      "$OUT_DIR/k8s/expected-output-after.txt" \
      "$OUT_DIR/k8s/workspace-excel-after.txt" \
      "$OUT_DIR/k8s/process-before.txt" \
      "$OUT_DIR/k8s/process-after.txt" \
      "$OUT_DIR/k8s/python-deps.txt"; do
      if [[ -f "$file" ]]; then
        echo "--- ${file} ---"
        cat "$file"
      fi
    done

    echo "===== focused cross-service lines ====="
    if [[ -s "$FOCUS_LINES" ]]; then
      cat "$FOCUS_LINES"
    fi
  } >"$RELEVANT_LINES"
}

build_control_plane_findings() {
  local pattern
  pattern="sqlalchemy|aiomysql|pymysql|OperationalError|InterfaceError|Can't connect|Access denied|Connection refused|Lost connection|MySQL server has gone away|Too many connections|DATABASE_URL"
  {
    echo "===== 控制面环境风险，不直接作为助手 Excel 根因 ====="
    grep -RInE "$pattern" \
      "$OUT_DIR/k8s/admin-history.log" \
      "$OUT_DIR/logs/admin-follow.log" \
      "$OUT_DIR/k8s/go-gateway-history.log" \
      "$OUT_DIR/logs/go-gateway-follow.log" \
      "$OUT_DIR/k8s/security-service-history.log" \
      "$OUT_DIR/logs/security-service-follow.log" \
      2>/dev/null || true
  } >"$CONTROL_PLANE_FINDINGS"
}

grep_relevant_logs() {
  local pattern="$1"
  [[ -f "$RELEVANT_LINES" ]] || return 0
  grep -InE "$pattern" "$RELEVANT_LINES" 2>/dev/null || true
}

has_source_match() {
  local pattern="$1"
  grep_source_logs "$pattern" | grep -q .
}

has_relevant_match() {
  local pattern="$1"
  grep_relevant_logs "$pattern" | grep -q .
}

extract_errors() {
  log "提取阻塞/错误证据行"
  grep_source_logs \
    "Traceback|Exception|ERROR|Error|error|failed|失败|报错|panic|fatal|ModuleNotFoundError|No module named|ImportError|Permission denied|Read-only file system|No space left|No such file or directory|ls: cannot access|Command exited with code|pip install|pip show|openpyxl|xlsxwriter|pandas|pending_approval|approval_required|requires approval|waiting.*approval|需要审批|等待审批|401 Unauthorized|invalid api key|GatewayRequestError|timeout|timed out|超时|卡住|stuck|EXPECTED_OUTPUT_MISSING|XLSX_VALIDATE_FAILED" \
    >"$MATCHED_ERRORS"
  if [[ -s "$MATCHED_ERRORS" ]]; then
    log "已提取到证据，前 180 行如下："
    sed -n '1,180p' "$MATCHED_ERRORS"
  else
    warn "未匹配到明显错误关键字，需要查看 follow 日志和 task-poll.jsonl。"
  fi

  {
    if [[ -n "$TASK_ID" ]]; then
      echo "===== task_id: ${TASK_ID} ====="
      grep_focus_logs "$TASK_ID"
    fi
    echo "===== assistant_id: ${ASSISTANT_ID} ====="
    grep_focus_logs "$ASSISTANT_ID"
    if [[ -n "$KEYWORD" ]]; then
      echo "===== keyword: ${KEYWORD} ====="
      grep_focus_logs "$KEYWORD"
    fi
    if [[ -n "$EXPECTED_OUTPUT" ]]; then
      echo "===== expected_output: ${EXPECTED_OUTPUT} ====="
      grep_focus_logs "$EXPECTED_OUTPUT"
    fi
  } >"$FOCUS_LINES"
  if [[ -s "$FOCUS_LINES" ]] && grep -vq '^=====' "$FOCUS_LINES"; then
    log "已提取当前助手/task/关键词聚焦日志，前 120 行如下："
    sed -n '1,120p' "$FOCUS_LINES"
  fi
  build_relevant_lines
  build_control_plane_findings
}

append_cause() {
  local cause="$1"
  local pattern="$2"
  {
    echo
    echo "根因候选：${cause}"
    echo "关键证据："
    grep_source_logs "$pattern" | head -30 || true
  } >>"$SUMMARY"
}

append_relevant_cause() {
  local cause="$1"
  local pattern="$2"
  {
    echo
    echo "根因候选：${cause}"
    echo "关键证据："
    grep_relevant_logs "$pattern" | head -30 || true
  } >>"$SUMMARY"
}

summarize() {
  log "生成中文汇总"
  {
    echo "apply-test 助手任务阻塞排查汇总"
    echo "运行时间：$(timestamp)"
    echo "namespace：${NS}"
    echo "assistant_id：${ASSISTANT_ID}"
    echo "pod：${POD_NAME}"
    echo "container：${CONTAINER}"
    echo "输出目录：${OUT_DIR}"
    echo "预期输出：${EXPECTED_OUTPUT}"
    echo "本轮 marker：${TRACE_MARKER}"
    if [[ -n "$TASK_ID" ]]; then
      echo "task_id：${TASK_ID}"
    fi
    if [[ "$SUBMIT_TASK" == "true" ]]; then
      echo "提交模式：脚本直接提交 Gateway /api/v1/chat"
    else
      echo "提交模式：页面手动复现"
    fi
  } >"$SUMMARY"

  local found="false"
  if [[ -n "$EXPECTED_OUTPUT" ]] && [[ "$EXPECTED_OUTPUT" != *.xlsx && "$EXPECTED_OUTPUT" != *.xls && "$EXPECTED_OUTPUT" != *.csv ]]; then
    {
      echo
      echo "配置风险：expected-output 没有表格文件后缀。Excel 生成提示词最好写成明确的 .xlsx 文件路径。"
    } >>"$SUMMARY"
  fi
  if [[ -f "$TASK_POLL_LOG" && -s "$TASK_POLL_LOG" ]]; then
    {
      echo
      echo "任务轮询摘要："
      tail -12 "$TASK_POLL_LOG"
    } >>"$SUMMARY"
  fi
  if [[ -s "$FOCUS_LINES" ]] && grep -vq '^=====' "$FOCUS_LINES"; then
    {
      echo
      echo "聚焦日志：当前助手/task/关键词相关行已写入 ${FOCUS_LINES}"
      sed -n '1,50p' "$FOCUS_LINES"
    } >>"$SUMMARY"
  fi
  {
    echo
    echo "相关日志范围：根因判断只使用当前助手 Pod、当前 task、预期输出文件、关键词聚焦行和工作区快照；其他助手的 security-service 审计日志只保留在原始日志中，不参与根因候选。"
    echo "相关证据文件：${RELEVANT_LINES}"
  } >>"$SUMMARY"

  if [[ -s "$CONTROL_PLANE_FINDINGS" ]] && grep -vq '^=====' "$CONTROL_PLANE_FINDINGS"; then
    {
      echo
      echo "控制面环境风险：发现 admin/go-gateway/security-service 中有数据库或连接异常。"
      echo "说明：这类异常会影响后台页面、审计日志、工作区元数据或审批状态同步；但除非 task/当前助手日志也命中，否则不直接作为 apply-test Excel 未落盘的根因。"
      echo "证据文件：${CONTROL_PLANE_FINDINGS}"
      sed -n '1,35p' "$CONTROL_PLANE_FINDINGS"
    } >>"$SUMMARY"
  fi

  if has_relevant_match "ls: cannot access|No such file or directory|Command exited with code 2"; then
    append_relevant_cause "模型/skill 在生成 Excel 前先访问目标路径或目录，但该路径不存在。图 2 中的 ls 失败属于明确阻塞点；应先 mkdir -p 父目录，并把目标写成明确的 .xlsx 文件。" "ls: cannot access|No such file or directory|Command exited with code 2"
    found="true"
  fi
  if has_relevant_match "pip show openpyxl|pip install .*openpyxl|pip install .*xlsxwriter|pip install .*pandas"; then
    append_relevant_cause "当前助手执行链路进入了 Python 依赖检查/运行时安装分支。若之后无 Python 生成脚本或任务状态无变化，说明卡在依赖检查之后的下一步规划/执行，而不是已经完成生成。" "pip show openpyxl|pip install .*openpyxl|pip install .*xlsxwriter|pip install .*pandas|openpyxl"
    found="true"
  fi
  if has_relevant_match "ModuleNotFoundError|No module named|ImportError"; then
    append_relevant_cause "当前助手容器内 Python 依赖缺失或导入失败，Excel 生成代码无法启动。" "ModuleNotFoundError|No module named|ImportError"
    found="true"
  fi
  if has_relevant_match "Permission denied|Read-only file system|No space left on device"; then
    append_relevant_cause "当前助手 workspace 不可写、只读或磁盘空间不足，Excel 文件无法落盘。" "Permission denied|Read-only file system|No space left on device"
    found="true"
  fi
  if has_relevant_match "EXPECTED_OUTPUT_MISSING"; then
    append_relevant_cause "复现后预期 Excel 文件仍未落盘；注意历史同前缀文件不算本轮成功，只有 marker 之后修改的 current_run_same_prefix_candidates 才算本轮输出。" "EXPECTED_OUTPUT_MISSING|current_run_same_prefix_candidates|historical_same_prefix_candidates|EXACT_OUTPUT_EXISTS_BUT_OLD|预期输出"
    found="true"
  fi
  if has_relevant_match "CURRENT_RUN_EXACT_OUTPUT_PRESENT"; then
    append_relevant_cause "本轮已经生成了预期 Excel 文件；如果页面仍看不到，应转查 workspace 侧栏/文件列表同步。" "CURRENT_RUN_EXACT_OUTPUT_PRESENT|XLSX_ZIP_OK|预期输出"
    found="true"
  fi
  if has_relevant_match "pending_approval|approval_required|requires approval|waiting.*approval|需要审批|等待审批"; then
    append_relevant_cause "当前任务被审批链路拦截或审批状态未闭环，可能导致工具执行停在等待状态。" "pending_approval|approval_required|requires approval|waiting.*approval|需要审批|等待审批"
    found="true"
  fi
  if has_relevant_match "401 Unauthorized|invalid api key"; then
    append_relevant_cause "当前助手/task 相关请求出现 401；它可能导致审批轮询、任务状态或页面侧栏无法正常刷新。" "401 Unauthorized|invalid api key"
    found="true"
  fi
  if [[ -f "$TASK_POLL_LOG" ]] && tail -1 "$TASK_POLL_LOG" | grep -E '"status":"?(processing|queued|running|pending)' >/dev/null 2>&1; then
    append_cause "Gateway 任务在轮询超时后仍处于非终态，说明任务队列/runner/callback 至少有一段没有闭环；优先用 task_id 查 go-gateway 和助手 Pod 日志。" "\"status\".*(processing|queued|running|pending)|任务状态"
    found="true"
  fi
  if has_relevant_match "python.*openpyxl|Workbook|save\\("; then
    append_relevant_cause "当前助手相关日志中出现 Python Excel 生成线索；如果文件仍缺失，应检查 save 目标路径、异常吞掉或写入到非 /workspace/persistent 的位置。" "python.*openpyxl|Workbook|save\\(|xlsx"
    found="true"
  fi
  if [[ "$found" != "true" ]]; then
    {
      echo
      echo "根因候选：本轮日志没有抓到明确失败点。建议带 --task-id 重新跑，或把 --watch-seconds 拉长到覆盖完整阻塞过程。"
      echo "下一步优先看：${OUT_DIR}/logs/assistant-follow.log、${OUT_DIR}/logs/go-gateway-follow.log、${TASK_POLL_LOG}"
    } >>"$SUMMARY"
  fi

  log "汇总如下："
  cat "$SUMMARY"
}

main() {
  need_cmd kubectl
  need_cmd python3
  need_cmd curl

  log "开始追踪助手任务阻塞点"
  log "输出目录：${OUT_DIR}"
  resolve_namespace

  POD_NAME="$(find_assistant_pod || true)"
  if [[ -z "$POD_NAME" ]]; then
    run_capture "Pod 列表" "$OUT_DIR/k8s/pods.txt" kubectl -n "$NS" get pods -o wide || true
    fail "没有找到助手 Pod。assistant_id=${ASSISTANT_ID}，namespace=${NS}"
  fi
  CONTAINER="$(select_container)"
  [[ -n "$CONTAINER" ]] || fail "没有找到助手容器：${POD_NAME}"
  log "目标助手 Pod：${POD_NAME}"
  log "目标容器：${CONTAINER}"
  if [[ -n "$GATEWAY_API_KEY" ]]; then
    log "Gateway API Key：已提供，日志中不输出明文"
  fi

  collect_history
  create_trace_marker
  collect_container_snapshot
  start_follow_logs

  if [[ "$SUBMIT_TASK" == "true" ]]; then
    submit_chat_task
  fi
  if [[ -n "$TASK_ID" ]]; then
    poll_task_status
  else
    wait_manual_window
  fi

  stop_follow_logs
  collect_after_snapshot
  extract_errors
  summarize
  log "追踪完成。完整日志：${RUN_LOG}"
}

main "$@"
