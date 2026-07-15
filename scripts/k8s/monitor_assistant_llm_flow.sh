#!/usr/bin/env bash
# Monitor assistant <-> model interaction and extract a Chinese timeline.

set -Eeuo pipefail

NS="ironagent-kf"
ASSISTANT_ID="apply-test"
CONTAINER=""
SINCE="30m"
TAIL_LINES="800"
OUT_BASE="output/assistant-llm-flow"
WATCH_SECONDS="300"
TASK_ID=""
KEYWORD=""
SESSION_KEY=""
EXPECTED_OUTPUT=""
SUBMIT_TASK="false"
PROMPT=""
PROMPT_FILE=""
GATEWAY_URL=""
LOCAL_GATEWAY_PORT="18080"
NO_PORT_FORWARD="false"
GATEWAY_API_KEY="${GO_GATEWAY_API_KEY:-}"
MAX_TEXT_CHARS="0"

FOLLOW_PIDS=()
PF_PID=""
POD_NAME=""
RUN_ID=""
OUT_DIR=""
RUN_LOG=""
TRACE_MARKER=""
TASK_POLL_LOG=""

usage() {
  cat <<'USAGE'
用法:
  bash scripts/k8s/monitor_assistant_llm_flow.sh [选项]

目标:
  监测“助手 -> IronAgent runtime -> 大模型 -> 工具/审批 -> workspace”的完整链路。
  重点验证：
    1. 是否卡在 Gateway 任务队列或 runtime WebSocket；
    2. 页面/控制 API 是否 401 或 token 过期；
    3. 是否触发 pending_approval 后未闭环；
    4. 模型是否调用了错误 skill / 错误路径；
    5. 日志里能看到的模型 delta、最终回复、工具输入输出是否完整。

默认行为:
  默认不主动提交任务，只开启一个复现窗口。窗口内请在助手页面发送待排查的消息。

常用选项:
  --assistant-id <id>        助手 ID，默认 apply-test。
  --namespace <ns>           K8s namespace，默认 ironagent-kf；不存在时自动尝试 ironagent。
  --container <name>         助手 Pod 容器名，默认自动选 ironagent-gateway 或第一个容器。
  --watch-seconds <n>        复现窗口秒数，默认 300；设为 0 则只抓历史日志。
  --since <duration>         历史日志范围，默认 30m。
  --tail <n>                 每个服务历史日志最大行数，默认 800。
  --task-id <id>             已知 Gateway task_id 时传入，脚本会轮询 /api/v1/tasks/{id}。
  --keyword <text>           额外聚焦关键词，例如 xlsx、财经快讯、openpyxl。
  --session-key <key>        聚焦会话，例如 agent:apply-test:main。
  --expected-output <path>   预期输出文件，例如 /workspace/persistent/report.xlsx。
  --out-dir <dir>            输出目录根路径，默认 output/assistant-llm-flow。

主动提交模式:
  --submit                   由脚本向 Gateway /api/v1/chat 提交 prompt。
  --prompt <text>            --submit 使用的 prompt。
  --prompt-file <file>       --submit 从文件读取 prompt。
  --gateway-url <url>        Gateway 地址；不提供时默认 port-forward deploy/go-gateway。
  --local-gateway-port <n>   port-forward 本地端口，默认 18080。
  --no-port-forward          不自动 port-forward，必须配合 --gateway-url。
  --gateway-api-key <key>    Gateway X-API-Key。未提供时只能走 public chat，请优先提供。

输出控制:
  --max-text-chars <n>       模型/工具输出单段最大字符数；0 表示不截断，默认 0。

输出文件:
  run.log                    中文执行日志。
  llm-flow-timeline.log      模型、工具、审批、队列、token 事件时间线。
  model-output.log           日志里解析到的模型/工具输出文本。
  root-cause-report.txt      根因候选报告。
  task-poll.jsonl            任务状态轮询记录。
  logs/*.log                 实时 follow 日志。
  k8s/*.txt / *.log          Pod、workspace、技能、环境、历史日志快照。

示例:
  # 页面手动复现 5 分钟，推荐先用这个
  bash scripts/k8s/monitor_assistant_llm_flow.sh --assistant-id apply-test --namespace ironagent-kf --watch-seconds 300 --keyword 财经快讯

  # 已知 task_id 时聚焦该任务
  bash scripts/k8s/monitor_assistant_llm_flow.sh --assistant-id apply-test --task-id gw-xxx --watch-seconds 120

  # 脚本主动提交任务并轮询
  bash scripts/k8s/monitor_assistant_llm_flow.sh --assistant-id apply-test --submit --prompt-file prompt.txt --gateway-api-key "$GO_GATEWAY_API_KEY"
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
    --task-id)
      TASK_ID="${2:-}"
      shift 2
      ;;
    --keyword)
      KEYWORD="${2:-}"
      shift 2
      ;;
    --session-key)
      SESSION_KEY="${2:-}"
      shift 2
      ;;
    --expected-output)
      EXPECTED_OUTPUT="${2:-}"
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
    --max-text-chars)
      MAX_TEXT_CHARS="${2:-}"
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

[[ -n "$ASSISTANT_ID" ]] || fail "--assistant-id 不能为空"

RUN_ID="$(date '+%Y%m%d%H%M%S')"
OUT_DIR="${OUT_BASE%/}/$(safe_name "$ASSISTANT_ID")-${RUN_ID}"
RUN_LOG="$OUT_DIR/run.log"
TASK_POLL_LOG="$OUT_DIR/task-poll.jsonl"
TRACE_MARKER="/workspace/temp/.iron_agent_llm_flow_${RUN_ID}.marker"

mkdir -p "$OUT_DIR/k8s" "$OUT_DIR/logs" "$OUT_DIR/http" "$OUT_DIR/analysis"
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
  local prompt_path="$OUT_DIR/prompt.txt"
  if [[ -n "$PROMPT_FILE" ]]; then
    [[ -f "$PROMPT_FILE" ]] || fail "prompt 文件不存在：${PROMPT_FILE}"
    cp "$PROMPT_FILE" "$prompt_path"
    printf '%s' "$prompt_path"
    return 0
  fi
  if [[ -n "$PROMPT" ]]; then
    printf '%s\n' "$PROMPT" >"$prompt_path"
    printf '%s' "$prompt_path"
    return 0
  fi
  cat >"$prompt_path" <<'PROMPT'
请说明当前工作空间中可用的 skill 列表，并输出你将使用哪个 skill 处理 Excel 生成任务。
PROMPT
  printf '%s' "$prompt_path"
}

start_follow_logs() {
  log "启动实时日志采集：助手 Pod、go-gateway、admin、security-service"
  (kubectl -n "$NS" logs -f "$POD_NAME" -c "$CONTAINER" --since=15s 2>&1 | sed -u 's/^/[assistant] /') \
    >"$OUT_DIR/logs/assistant-follow.log" &
  FOLLOW_PIDS+=("$!")

  local deploy
  for deploy in go-gateway admin security-service; do
    if kubectl -n "$NS" get deploy "$deploy" >/dev/null 2>&1; then
      (kubectl -n "$NS" logs -f "deploy/${deploy}" --since=15s 2>&1 | sed -u "s/^/[${deploy}] /") \
        >"$OUT_DIR/logs/${deploy}-follow.log" &
      FOLLOW_PIDS+=("$!")
    else
      warn "未找到 deploy/${deploy}，跳过实时日志"
    fi
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
  run_capture "Pod 列表" "$OUT_DIR/k8s/pods.txt" kubectl -n "$NS" get pods -o wide || true
  run_capture "Service 列表" "$OUT_DIR/k8s/services.txt" kubectl -n "$NS" get svc -o wide || true
  run_capture "近期事件" "$OUT_DIR/k8s/events.txt" kubectl -n "$NS" get events --sort-by=.lastTimestamp || true
  run_capture "助手 Pod 描述" "$OUT_DIR/k8s/assistant-pod-describe.txt" kubectl -n "$NS" describe pod "$POD_NAME" || true
  run_capture "助手历史日志" "$OUT_DIR/k8s/assistant-history.log" kubectl -n "$NS" logs "$POD_NAME" -c "$CONTAINER" --since="$SINCE" --tail="$TAIL_LINES" || true
  run_capture "助手上一轮日志" "$OUT_DIR/k8s/assistant-previous.log" kubectl -n "$NS" logs "$POD_NAME" -c "$CONTAINER" --previous --tail="$TAIL_LINES" || true

  local deploy
  for deploy in go-gateway admin security-service; do
    if kubectl -n "$NS" get deploy "$deploy" >/dev/null 2>&1; then
      run_capture "${deploy} 历史日志" "$OUT_DIR/k8s/${deploy}-history.log" kubectl -n "$NS" logs "deploy/${deploy}" --since="$SINCE" --tail="$TAIL_LINES" || true
    fi
  done
}

create_trace_marker() {
  run_capture "创建本轮追踪 marker" "$OUT_DIR/k8s/trace-marker.txt" \
    kubectl_exec sh -lc 'marker="$1"; mkdir -p "$(dirname "$marker")"; date -Iseconds > "$marker"; ls -lh "$marker"; echo "TRACE_MARKER_CREATED=$marker"' sh "$TRACE_MARKER" || true
}

collect_container_snapshot() {
  run_capture "助手关键环境变量" "$OUT_DIR/k8s/assistant-env.txt" \
    kubectl_exec sh -lc 'printenv | sort | grep -E "(ASSISTANT|IRON_AGENT|IRON_AGENT|WORKSPACE|MODEL|PROVIDER|BASE_URL|API|TOKEN|KEY|SECRET|PASSWORD|PYTHON|NODE|BUN)" | sed -E "s#(TOKEN|KEY|SECRET|PASSWORD|API_KEY)=.*#\\1=***MASKED***#I"' || true
  run_capture "助手进程快照" "$OUT_DIR/k8s/process-before.txt" \
    kubectl_exec sh -lc 'ps -ef 2>/dev/null || ps aux 2>/dev/null || true' || true
  run_capture "workspace 权限、容量、关键目录" "$OUT_DIR/k8s/workspace-before.txt" \
    kubectl_exec sh -lc 'id; pwd; df -h /workspace 2>/dev/null || true; ls -ld /workspace /workspace/temp /workspace/persistent /workspace/skills /workspace/plugins 2>&1 || true; echo "[skills]"; find /workspace/skills -maxdepth 3 -type f 2>/dev/null | sort | head -200 || true; echo "[plugins]"; find /workspace/plugins -maxdepth 3 -type f 2>/dev/null | sort | head -200 || true; echo "[recent files]"; find /workspace -maxdepth 4 -type f -printf "%TY-%Tm-%Td %TH:%TM %s %p\n" 2>/dev/null | sort | tail -160 || true' || true
  run_capture "Python/Excel 依赖摘要" "$OUT_DIR/k8s/python-deps.txt" \
    kubectl_exec python3 - <<'PY' || true
import importlib, json, os, sys
mods = ["openpyxl", "pandas", "xlsxwriter", "numpy", "olefile", "fitz"]
rows = []
for name in mods:
    try:
        mod = importlib.import_module(name)
        rows.append({"name": name, "ok": True, "version": getattr(mod, "__version__", "")})
    except Exception as exc:
        rows.append({"name": name, "ok": False, "error": repr(exc)})
print(json.dumps({"python": sys.version, "uid": os.getuid(), "modules": rows}, ensure_ascii=False, indent=2))
PY
}

collect_after_snapshot() {
  run_capture "助手进程复现后快照" "$OUT_DIR/k8s/process-after.txt" \
    kubectl_exec sh -lc 'ps -ef 2>/dev/null || ps aux 2>/dev/null || true' || true
  run_capture "workspace 复现后新增/变更文件" "$OUT_DIR/k8s/workspace-after.txt" \
    kubectl_exec sh -lc 'marker="$1"; echo "trace_marker=$marker"; [ -e "$marker" ] && ls -lh "$marker" || true; echo "[newer_than_marker]"; if [ -e "$marker" ]; then find /workspace -maxdepth 6 -type f -newer "$marker" -printf "%TY-%Tm-%Td %TH:%TM %s %p\n" 2>/dev/null | sort | tail -220 || true; fi; echo "[recent xlsx/csv]"; find /workspace -maxdepth 6 \( -iname "*.xlsx" -o -iname "*.xls" -o -iname "*.csv" \) -exec ls -lh {} \; 2>/dev/null | tail -160 || true' sh "$TRACE_MARKER" || true
  if [[ -n "$EXPECTED_OUTPUT" ]]; then
    run_capture "预期输出文件复现后检查" "$OUT_DIR/k8s/expected-output-after.txt" \
      kubectl_exec sh -lc 'marker="$1"; path="$2"; echo "expected_output=$path"; [ -e "$path" ] && ls -lh "$path" || echo "EXPECTED_OUTPUT_MISSING"; if [ -e "$path" ] && [ -e "$marker" ] && [ "$path" -nt "$marker" ]; then echo "CURRENT_RUN_EXACT_OUTPUT_PRESENT"; elif [ -e "$path" ]; then echo "EXACT_OUTPUT_EXISTS_BUT_OLD"; fi' sh "$TRACE_MARKER" "$EXPECTED_OUTPUT" || true
  fi
}

collect_session_artifacts() {
  run_capture "IronAgent 会话文件列表" "$OUT_DIR/k8s/ironagent-sessions-index.txt" \
    kubectl_exec sh -lc 'agent="$1"; dir="/workspace/.ironagent/agents/$agent/sessions"; echo "sessions_dir=$dir"; ls -lah "$dir" 2>&1 || true; find "$dir" -maxdepth 1 \( -name "*.jsonl" -o -name "*.json" \) -type f -printf "%TY-%Tm-%Td %TH:%TM:%TS %s %p\n" 2>/dev/null | sort || true' sh "$ASSISTANT_ID" || true

  run_capture "IronAgent 会话 JSONL 内容" "$OUT_DIR/k8s/ironagent-sessions-after.jsonl" \
    kubectl_exec sh -lc 'agent="$1"; limit="${2:-5000}"; dir="/workspace/.ironagent/agents/$agent/sessions"; echo "IRON_AGENT_SESSION_DUMP_DIR=$dir"; if [ ! -d "$dir" ]; then echo "IRON_AGENT_SESSION_DIR_MISSING"; exit 0; fi; find "$dir" -maxdepth 1 -type f -name "*.jsonl" -printf "%T@ %p\n" 2>/dev/null | sort -n | tail -5 | while IFS= read -r row; do f="${row#* }"; echo "===== SESSION_JSONL_FILE $f ====="; i=0; tail -n "$limit" "$f" 2>&1 | while IFS= read -r line; do i=$((i + 1)); printf "SESSION_JSONL assistant=%s file=%s line=%s %s\n" "$agent" "$f" "$i" "$line"; done; done' sh "$ASSISTANT_ID" "5000" || true

  run_capture "IronAgent 会话索引 JSON 内容" "$OUT_DIR/k8s/ironagent-sessions-index.jsonl" \
    kubectl_exec sh -lc 'agent="$1"; dir="/workspace/.ironagent/agents/$agent/sessions"; echo "IRON_AGENT_SESSION_INDEX_DIR=$dir"; for f in "$dir"/sessions.json "$dir"/*.json; do [ -f "$f" ] || continue; echo "===== SESSION_JSON_FILE $f ====="; i=0; cat "$f" 2>&1 | while IFS= read -r line; do i=$((i + 1)); printf "SESSION_JSON assistant=%s file=%s line=%s %s\n" "$agent" "$f" "$i" "$line"; done; done' sh "$ASSISTANT_ID" || true
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
    warn "Gateway port-forward 未启动成功，详情见 $OUT_DIR/http/go-gateway-port-forward.log"
    return 1
  fi
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

json_get() {
  local file="$1"
  local key="$2"
  python3 - "$file" "$key" <<'PY'
import json, sys
path, key = sys.argv[1:3]
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    sys.exit(1)
value = data
for part in key.split("."):
    if isinstance(value, dict):
        value = value.get(part, "")
    else:
        value = ""
        break
if value is None:
    value = ""
print(value if not isinstance(value, (dict, list)) else json.dumps(value, ensure_ascii=False))
PY
}

submit_chat_task() {
  local prompt_path body_file response_file request_id conversation_id
  prompt_path="$(prepare_prompt)"
  start_gateway_port_forward || true
  body_file="$OUT_DIR/http/chat-submit-body.json"
  response_file="$OUT_DIR/http/chat-submit-response.json"
  request_id="llm-flow-${ASSISTANT_ID}-${RUN_ID}"
  conversation_id="${SESSION_KEY:-agent:${ASSISTANT_ID}:main}"

  if [[ -z "$GATEWAY_API_KEY" ]]; then
    warn "未提供 --gateway-api-key，将走 public chat，请求可能无法强制指定 target_assistant_id=${ASSISTANT_ID}"
  fi

  python3 - "$prompt_path" "$body_file" "$request_id" "$conversation_id" "$ASSISTANT_ID" "$GATEWAY_API_KEY" <<'PY'
import json, sys
prompt_path, body_path, request_id, conversation_id, assistant_id, api_key = sys.argv[1:7]
message = open(prompt_path, encoding="utf-8").read().strip()
if api_key.strip():
    payload = {
        "client_id": "llm-flow-monitor",
        "user_id": "llm-flow-monitor",
        "request_id": request_id,
        "conversation_id": conversation_id,
        "source_assistant_id": "llm-flow-monitor",
        "target_assistant_id": assistant_id,
        "message": message,
        "instruction": message,
        "approval_mode": "none",
        "timeout_seconds": 600,
        "metadata": {"trace_run_id": request_id},
    }
else:
    payload = {
        "request_id": request_id,
        "user_id": "llm-flow-monitor",
        "conversation_id": conversation_id,
        "message": message,
    }
with open(body_path, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
PY

  log "提交 Gateway chat 任务"
  curl_gateway_json POST "/api/v1/chat" "$body_file" "$response_file" || true
  TASK_ID="$(json_get "$response_file" "task_id" 2>/dev/null || json_get "$response_file" "taskId" 2>/dev/null || true)"
  if [[ -n "$TASK_ID" ]]; then
    log "提交成功，task_id=${TASK_ID}"
  else
    warn "未能从提交响应中解析 task_id，响应见 ${response_file}"
  fi
}

poll_task_status() {
  [[ -n "$TASK_ID" ]] || return 0
  start_gateway_port_forward || true
  log "开始轮询任务状态：${TASK_ID}"
  local deadline now poll_file code status stage
  deadline=$(( $(date +%s) + WATCH_SECONDS ))
  if [[ "$WATCH_SECONDS" -le 0 ]]; then
    deadline=$(( $(date +%s) + 180 ))
  fi
  while true; do
    now="$(date +%s)"
    poll_file="$OUT_DIR/http/task-${TASK_ID}-${now}.json"
    curl_gateway_json GET "/api/v1/tasks/${TASK_ID}" "" "$poll_file" || true
    code="$(cat "${poll_file}.status" 2>/dev/null || true)"
    status="$(json_get "$poll_file" "status" 2>/dev/null || true)"
    stage="$(json_get "$poll_file" "stage" 2>/dev/null || true)"
    python3 - "$poll_file" "$code" <<'PY' >>"$TASK_POLL_LOG" 2>/dev/null || true
import json, sys, time
path, code = sys.argv[1:3]
try:
    payload = json.load(open(path, encoding="utf-8"))
except Exception:
    payload = {}
print(json.dumps({"time": time.strftime("%F %T"), "http_status": code, "payload": payload}, ensure_ascii=False))
PY
    log "任务轮询：http=${code:-?} status=${status:-?} stage=${stage:-?}"
    case "$status" in
      completed|failed|rejected|cancelled|succeeded|success)
        break
        ;;
    esac
    [[ "$now" -lt "$deadline" ]] || break
    sleep 5
  done
}

write_analyzer() {
  cat >"$OUT_DIR/analysis/analyze_llm_flow.py" <<'PY'
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any


SECRET_RE = re.compile(
    r"(?i)((?:api[_-]?key|authorization|token|secret|password|passwd)[\"'=:\s]+)([^\"'\s,}]+)"
)

EVENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("队列/任务", re.compile(r"pending_claim|queued|queue|ClaimQueued|ProcessQueued|task\.accepted|提交任务|任务轮询|task_id|request_id", re.I)),
    ("Runtime WebSocket", re.compile(r"运行时WebSocket|运行时聊天|connect\.challenge|chat\.send|chat-history|runId|sessionKey", re.I)),
    ("大模型调用", re.compile(r"llm|model|provider|openai|anthropic|dashscope|qwen|minimax|completion|chat.completions|tokens?|rate limit|429|context length", re.I)),
    ("模型输出", re.compile(r"assistant|delta|final|reply_text|replyText|content|Tool output|tool output|message", re.I)),
    ("工具调用", re.compile(r"tool|exec|read|write|openpyxl|xlsx|xls|csv|python3|pip install|SKILL\.md|/workspace/skills|/app/skills", re.I)),
    ("提示词/skill误判", re.compile(r"作为\s*skill|service_summary_v4.*skill|skill.*service_summary_v4|/workspace/skills/service_summary_v4|/app/skills/service_summary_v4|No such file.*skill|skill.*not found|技能.*不存在", re.I)),
    ("审批", re.compile(r"approval|pending_approval|requires approval|审批|Allow once|Always allow|deny|decision", re.I)),
    ("Token/Auth", re.compile(r"401|403|Unauthorized|Forbidden|expired|token|launch[-_ ]?ticket|session", re.I)),
    ("同步/路径", re.compile(r"workspace|sync|skills/status|plugins|No such file|not found|/workspace|/app/skills|/app/extensions", re.I)),
    ("错误/超时", re.compile(r"ERROR|WARN|Exception|Traceback|failed|失败|timeout|deadline|context canceled|connection refused|reset|5\\d\\d|Bad Gateway", re.I)),
]

TEXT_KEYS = {
    "text", "content", "delta", "reply", "reply_text", "replyText", "output",
    "message", "error", "reason", "command", "arguments", "result",
}


def mask(value: str) -> str:
    return SECRET_RE.sub(lambda m: m.group(1) + "***MASKED***", value)


def trim(value: str, max_chars: int) -> str:
    if max_chars > 0 and len(value) > max_chars:
        return value[:max_chars] + f"\n...[已截断，原始长度 {len(value)} 字符]"
    return value


def try_json(line: str) -> Any | None:
    stripped = line.strip()
    candidates = [stripped]
    idx = stripped.find("{")
    if idx > 0:
        candidates.append(stripped[idx:])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return None


def walk_text(obj: Any, path: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}" if path else str(key)
            if key in TEXT_KEYS and isinstance(value, str) and value.strip():
                out.append((child, value.strip()))
            out.extend(walk_text(value, child))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            out.extend(walk_text(value, f"{path}[{index}]"))
    return out


def flatten_selected(obj: Any) -> dict[str, Any]:
    selected: dict[str, Any] = {}
    keys = {
        "time", "timestamp", "level", "msg", "message", "event", "event_type",
        "assistant_id", "assistant_name", "task_id", "request_id", "run_id", "runId",
        "session_id", "session_key", "conversation_id", "status", "stage",
        "tool_name", "tool", "risk_level", "decision", "reason", "error",
        "model", "provider", "usage", "prompt_tokens", "completion_tokens",
    }

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                if k in keys and k not in selected:
                    selected[k] = v
                visit(v)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(obj)
    return selected


def should_focus(line: str, args: argparse.Namespace) -> bool:
    needles = [args.assistant_id, args.task_id, args.keyword, args.session_key]
    needles = [n for n in needles if n]
    if not needles:
        return True
    return any(n in line for n in needles)


def classify(line: str) -> list[str]:
    return [name for name, pattern in EVENT_PATTERNS if pattern.search(line)]


def iter_sources(root: Path):
    for sub in ("logs", "k8s", "http"):
        base = root / sub
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix in {".log", ".txt", ".json", ".jsonl", ".status"}:
                yield path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--assistant-id", default="")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--keyword", default="")
    parser.add_argument("--session-key", default="")
    parser.add_argument("--max-text-chars", type=int, default=0)
    args = parser.parse_args()

    root = Path(args.out_dir)
    timeline_path = root / "llm-flow-timeline.log"
    output_path = root / "model-output.log"
    report_path = root / "root-cause-report.txt"

    timeline_rows: list[str] = []
    output_rows: list[str] = []
    counters: dict[str, int] = {}
    evidence: dict[str, list[str]] = {}

    def add_evidence(key: str, line: str) -> None:
        counters[key] = counters.get(key, 0) + 1
        evidence.setdefault(key, [])
        if len(evidence[key]) < 12:
            evidence[key].append(line)

    for path in iter_sources(root):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for lineno, raw in enumerate(lines, 1):
            line = mask(raw)
            if not should_focus(line, args):
                continue
            labels = classify(line)
            if labels:
                timeline_rows.append(f"{path.relative_to(root)}:{lineno}: [{' / '.join(labels)}] {line}")
                for label in labels:
                    add_evidence(label, f"{path.relative_to(root)}:{lineno}: {line}")
            obj = try_json(line)
            if obj is not None:
                selected = flatten_selected(obj)
                if selected:
                    timeline_rows.append(
                        f"{path.relative_to(root)}:{lineno}: [JSON摘要] "
                        + json.dumps(selected, ensure_ascii=False, default=str)
                    )
                for key, text in walk_text(obj):
                    if not text.strip():
                        continue
                    if len(text.strip()) < 2:
                        continue
                    if re.search(r"(?i)(api[_-]?key|authorization|token|secret|password)", key):
                        continue
                    text = trim(mask(text), args.max_text_chars)
                    if re.search(r"(?i)(assistant|delta|reply|content|text|output|command|error|reason|message)", key):
                        output_rows.append(
                            f"===== {path.relative_to(root)}:{lineno} {key} =====\n{text}\n"
                        )

    timeline_path.write_text("\n".join(timeline_rows) + ("\n" if timeline_rows else ""), encoding="utf-8")
    output_path.write_text("\n".join(output_rows) + ("\n" if output_rows else ""), encoding="utf-8")

    report: list[str] = []
    report.append("助手-大模型交互链路诊断报告")
    report.append(f"assistant_id: {args.assistant_id or '未指定'}")
    if args.task_id:
        report.append(f"task_id: {args.task_id}")
    if args.session_key:
        report.append(f"session_key: {args.session_key}")
    if args.keyword:
        report.append(f"keyword: {args.keyword}")
    report.append("")
    report.append("事件计数:")
    for key in [name for name, _ in EVENT_PATTERNS]:
        report.append(f"- {key}: {counters.get(key, 0)}")
    report.append("")
    report.append("根因候选:")

    def has(label: str) -> bool:
        return counters.get(label, 0) > 0

    any_cause = False
    if has("队列/任务") and not has("Runtime WebSocket"):
        any_cause = True
        report.append("- 可能卡在 Gateway 任务队列或任务尚未被 worker/runtime 接手。看 task-poll.jsonl 与 go-gateway 日志中的 queued / pending_claim。")
    if has("Runtime WebSocket") and not has("模型输出") and not has("大模型调用"):
        any_cause = True
        report.append("- Runtime WebSocket 已接入，但日志里没有模型调用/输出证据，可能卡在 runtime 内部调度、模型排队，或当前镜像未打印模型流日志。")
    if has("Token/Auth"):
        any_cause = True
        report.append("- 发现 Token/Auth 相关证据，重点检查 401、launch ticket/session 过期，以及页面控制 API 是否持续失败。")
    if has("审批"):
        any_cause = True
        report.append("- 发现审批相关证据，若 pending_approval 后没有 resolve/allow，任务会停在等待审批。")
    if has("同步/路径"):
        any_cause = True
        report.append("- 发现 workspace/skill/plugin 路径或同步相关证据，需要核对模型实际访问的是 /workspace/skills 还是旧的 /app/skills。")
    if has("提示词/skill误判"):
        any_cause = True
        report.append("- 发现提示词/skill 误判证据。`persistent/service_summary_v4.md` 应作为输入数据文件，不应被模型当作 /workspace/skills/<name>/SKILL.md 类型的 skill。")
    if has("错误/超时"):
        any_cause = True
        report.append("- 发现错误或超时证据，优先看 llm-flow-timeline.log 中 [错误/超时] 附近上下文。")
    if not output_rows:
        report.append("- 当前采集日志没有解析到模型完整输出。若页面确实有输出，说明 runtime 没有把模型 delta/final 写入容器日志；此时只能通过 WebSocket/任务响应或增加 runtime debug 日志补齐。")
    if not any_cause:
        report.append("- 未发现明确阻塞特征。请扩大 --since/--tail，或传入 --task-id/--keyword 再跑一轮。")

    report.append("")
    report.append("关键证据样例:")
    for key in [name for name, _ in EVENT_PATTERNS]:
        rows = evidence.get(key, [])
        if not rows:
            continue
        report.append(f"\n[{key}]")
        report.extend(f"- {row}" for row in rows[:8])

    report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
PY
  chmod +x "$OUT_DIR/analysis/analyze_llm_flow.py"
}

analyze_logs() {
  write_analyzer
  log "生成模型/工具/审批时间线"
  python3 "$OUT_DIR/analysis/analyze_llm_flow.py" \
    --out-dir "$OUT_DIR" \
    --assistant-id "$ASSISTANT_ID" \
    --task-id "$TASK_ID" \
    --keyword "$KEYWORD" \
    --session-key "$SESSION_KEY" \
    --max-text-chars "$MAX_TEXT_CHARS"
}

print_finish() {
  log "追踪完成。输出目录：${OUT_DIR}"
  log "重点查看："
  log "  1) ${OUT_DIR}/root-cause-report.txt"
  log "  2) ${OUT_DIR}/llm-flow-timeline.log"
  log "  3) ${OUT_DIR}/model-output.log"
  log "  4) ${OUT_DIR}/logs/assistant-follow.log"
  if [[ -s "$TASK_POLL_LOG" ]]; then
    log "  5) ${TASK_POLL_LOG}"
  fi
}

main() {
  need_cmd kubectl
  need_cmd python3
  need_cmd sed
  need_cmd grep
  if [[ "$SUBMIT_TASK" == "true" || -n "$TASK_ID" ]]; then
    need_cmd curl
  fi

  resolve_namespace
  POD_NAME="$(find_assistant_pod || true)"
  [[ -n "$POD_NAME" ]] || fail "没有找到助手 Pod：assistant_id=${ASSISTANT_ID} namespace=${NS}"
  CONTAINER="$(select_container)"
  [[ -n "$CONTAINER" ]] || fail "无法确定助手容器名：pod=${POD_NAME}"

  log "开始助手-大模型交互追踪"
  log "namespace=${NS}"
  log "assistant_id=${ASSISTANT_ID}"
  log "pod=${POD_NAME}"
  log "container=${CONTAINER}"
  log "watch_seconds=${WATCH_SECONDS}"
  log "trace_marker=${TRACE_MARKER}"

  collect_history
  create_trace_marker
  collect_container_snapshot
  start_follow_logs

  if [[ "$SUBMIT_TASK" == "true" ]]; then
    submit_chat_task
  fi

  if [[ -n "$TASK_ID" && "$WATCH_SECONDS" -le 0 ]]; then
    poll_task_status
  elif [[ -n "$TASK_ID" ]]; then
    poll_task_status &
    FOLLOW_PIDS+=("$!")
  fi

  if [[ "$WATCH_SECONDS" -gt 0 ]]; then
    log "进入复现窗口 ${WATCH_SECONDS} 秒。请现在在助手页面发送待排查消息。"
    sleep "$WATCH_SECONDS"
  else
    log "watch_seconds=0，跳过等待窗口"
  fi

  stop_follow_logs
  collect_after_snapshot
  collect_session_artifacts
  analyze_logs
  print_finish
}

main "$@"
