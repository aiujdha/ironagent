#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"
USER_ID="${USER_ID:-}"
API_KEY="${API_KEY:-}"
OUT_DIR="${OUT_DIR:-output/file-update-twice-$(date +%Y%m%d%H%M%S)}"
FILENAME="${FILENAME:-update-interface-same-name.txt}"
FILE_V1="${FILE_V1:-}"
FILE_V2="${FILE_V2:-}"
CONTENT_V1="${CONTENT_V1:-}"
CONTENT_V2="${CONTENT_V2:-}"
CONNECT_TIMEOUT="${CONNECT_TIMEOUT:-5}"
MAX_TIME="${MAX_TIME:-60}"
SKIP_HEALTH_CHECK="${SKIP_HEALTH_CHECK:-false}"
SECOND_UPLOAD_OVERWRITE="${SECOND_UPLOAD_OVERWRITE:-false}"

usage() {
  cat <<'EOF'
用法：
  bash scripts/k8s/test_file_update_twice_cn.sh --user-id USER_ID [选项]

用途：
  测试文件 update/覆盖语义：同一个 user_id 下，用相同文件名连续上传两次，
  校验第二次提交是否复用第一次的 file_id，并且下载内容是否变成第二版。

常用选项：
  --base-url URL       Gateway 地址，默认 $BASE_URL 或 http://127.0.0.1:8080。
  --user-id USER_ID    必填。该用户必须已绑定可用 assistant。
  --api-key KEY        可选，发送 X-API-Key；默认不带。
  --out-dir DIR        证据输出目录，默认 output/file-update-twice-<timestamp>。
  --filename NAME      上传时使用的同名文件名，默认 update-interface-same-name.txt。
  --file-v1 PATH       第一次上传的本地文件。
  --file-v2 PATH       第二次上传的本地文件。
  --content-v1 TEXT    不传 --file-v1 时，用这段文本生成第一次上传文件。
  --content-v2 TEXT    不传 --file-v2 时，用这段文本生成第二次上传文件。
  --second-overwrite   第二次上传额外带 overwrite=true。
                       当前 public 无鉴权入口可能拒绝额外字段；带 --api-key 时更适合使用。
  --skip-health-check  跳过 /healthz 和 /readyz。
  -h, --help           显示帮助。

示例：
  bash scripts/k8s/test_file_update_twice_cn.sh \
    --base-url http://192.0.2.10:30808 \
    --user-id callback-format-user-20260530104753

输出：
  REPORT.md              人可读报告
  summary.json           机器可读摘要
  upload-1.response.json 第一次上传响应
  upload-2.response.json 第二次上传响应
  download-after-*.bin   下载内容证据
EOF
}

log() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*"
}

die() {
  printf '[%s] ERROR: %s\n' "$(date '+%F %T')" "$*" >&2
  exit 1
}

require_arg() {
  local opt="$1"
  local value="${2:-}"
  [[ -n "$value" ]] || die "$opt 需要参数"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url)
      require_arg "$1" "${2:-}"
      BASE_URL="$2"
      shift 2
      ;;
    --user-id)
      require_arg "$1" "${2:-}"
      USER_ID="$2"
      shift 2
      ;;
    --api-key)
      require_arg "$1" "${2:-}"
      API_KEY="$2"
      shift 2
      ;;
    --out-dir)
      require_arg "$1" "${2:-}"
      OUT_DIR="$2"
      shift 2
      ;;
    --filename)
      require_arg "$1" "${2:-}"
      FILENAME="$2"
      shift 2
      ;;
    --file-v1)
      require_arg "$1" "${2:-}"
      FILE_V1="$2"
      shift 2
      ;;
    --file-v2)
      require_arg "$1" "${2:-}"
      FILE_V2="$2"
      shift 2
      ;;
    --content-v1)
      require_arg "$1" "${2:-}"
      CONTENT_V1="$2"
      shift 2
      ;;
    --content-v2)
      require_arg "$1" "${2:-}"
      CONTENT_V2="$2"
      shift 2
      ;;
    --second-overwrite|--overwrite-second)
      SECOND_UPLOAD_OVERWRITE="true"
      shift
      ;;
    --skip-health-check)
      SKIP_HEALTH_CHECK="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "未知参数：$1"
      ;;
  esac
done

[[ -n "$USER_ID" ]] || {
  usage >&2
  die "缺少 --user-id"
}

command -v curl >/dev/null 2>&1 || die "缺少 curl"
command -v python3 >/dev/null 2>&1 || die "缺少 python3"

BASE_URL="${BASE_URL%/}"
mkdir -p "$OUT_DIR"

AUTH_HEADERS=()
if [[ -n "$API_KEY" ]]; then
  AUTH_HEADERS=(-H "X-API-Key: ${API_KEY}")
fi

urlencode() {
  python3 - "$1" <<'PY'
import sys
from urllib.parse import quote

print(quote(sys.argv[1], safe=""))
PY
}

json_get_file_id() {
  python3 - "$1" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    payload = json.load(fh)
for key in ("file_id", "fileId", "id"):
    value = payload.get(key)
    if value:
        print(value)
        raise SystemExit(0)
raise SystemExit(1)
PY
}

json_get_field() {
  python3 - "$1" "$2" <<'PY'
import json
import sys

path, field = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as fh:
    payload = json.load(fh)
value = payload.get(field, "")
print("" if value is None else value)
PY
}

sha256_file() {
  python3 - "$1" <<'PY'
import hashlib
import sys

h = hashlib.sha256()
with open(sys.argv[1], "rb") as fh:
    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
        h.update(chunk)
print(h.hexdigest())
PY
}

write_default_file() {
  local path="$1"
  local version="$2"
  local content="$3"
  if [[ -n "$content" ]]; then
    printf '%s\n' "$content" >"$path"
  else
    {
      printf 'RUN_TS=%s\n' "$(date '+%F %T %z')"
      printf 'USER_ID=%s\n' "$USER_ID"
      printf 'FILENAME=%s\n' "$FILENAME"
      printf 'VERSION=%s\n' "$version"
    } >"$path"
  fi
}

prepare_upload_files() {
  local generated_v1="$OUT_DIR/generated-upload-v1.txt"
  local generated_v2="$OUT_DIR/generated-upload-v2.txt"

  if [[ -n "$FILE_V1" ]]; then
    [[ -f "$FILE_V1" ]] || die "--file-v1 不存在：$FILE_V1"
    cp "$FILE_V1" "$OUT_DIR/payload-v1"
  else
    write_default_file "$generated_v1" "v1" "$CONTENT_V1"
    cp "$generated_v1" "$OUT_DIR/payload-v1"
  fi

  if [[ -n "$FILE_V2" ]]; then
    [[ -f "$FILE_V2" ]] || die "--file-v2 不存在：$FILE_V2"
    cp "$FILE_V2" "$OUT_DIR/payload-v2"
  else
    write_default_file "$generated_v2" "v2" "$CONTENT_V2"
    cp "$generated_v2" "$OUT_DIR/payload-v2"
  fi
}

curl_capture() {
  local output="$1"
  local stderr_file="$2"
  shift 2
  : >"$output"
  : >"$stderr_file"
  local http_code curl_exit
  set +e
  http_code="$(curl -sS --connect-timeout "$CONNECT_TIMEOUT" --max-time "$MAX_TIME" -o "$output" -w "%{http_code}" "$@" 2>"$stderr_file")"
  curl_exit=$?
  set -e
  printf '%s\t%s\n' "$curl_exit" "$http_code"
}

assert_curl_2xx() {
  local label="$1"
  local result="$2"
  local stderr_file="$3"
  local response_file="$4"
  local curl_exit http_code
  curl_exit="$(printf '%s' "$result" | cut -f1)"
  http_code="$(printf '%s' "$result" | cut -f2)"
  log "$label HTTP=$http_code curl_exit=$curl_exit"
  [[ "$curl_exit" == "0" ]] || die "$label curl 失败：$(tr '\n' ' ' <"$stderr_file")"
  [[ "$http_code" =~ ^2[0-9][0-9]$ ]] || die "$label HTTP 非 2xx：$http_code，响应=$(tr '\n' ' ' <"$response_file")"
}

health_check() {
  [[ "$SKIP_HEALTH_CHECK" == "true" ]] && return

  local result
  result="$(curl_capture "$OUT_DIR/healthz.response" "$OUT_DIR/healthz.stderr" "${AUTH_HEADERS[@]}" "$BASE_URL/healthz")"
  assert_curl_2xx "GET /healthz" "$result" "$OUT_DIR/healthz.stderr" "$OUT_DIR/healthz.response"

  result="$(curl_capture "$OUT_DIR/readyz.response" "$OUT_DIR/readyz.stderr" "${AUTH_HEADERS[@]}" "$BASE_URL/readyz")"
  assert_curl_2xx "GET /readyz" "$result" "$OUT_DIR/readyz.stderr" "$OUT_DIR/readyz.response"
}

upload_once() {
  local payload="$1"
  local response="$2"
  local stderr_file="$3"
  local status_file="$4"
  local use_overwrite="${5:-false}"
  local form_args=(
    -F "user_id=${USER_ID}"
    -F "file=@${payload};filename=${FILENAME};type=text/plain"
  )
  local result
  if [[ "$use_overwrite" == "true" ]]; then
    form_args+=(-F "overwrite=true")
  fi
  result="$(curl_capture "$response" "$stderr_file" \
    "${AUTH_HEADERS[@]}" \
    "${form_args[@]}" \
    "$BASE_URL/api/v1/files/upload")"
  printf '%s\n' "$result" >"$status_file"
  assert_curl_2xx "POST /api/v1/files/upload" "$result" "$stderr_file" "$response"
}

download_file() {
  local file_id="$1"
  local output="$2"
  local stderr_file="$3"
  local status_file="$4"
  local encoded_file_id encoded_user_id result
  encoded_file_id="$(urlencode "$file_id")"
  encoded_user_id="$(urlencode "$USER_ID")"
  result="$(curl_capture "$output" "$stderr_file" \
    "${AUTH_HEADERS[@]}" \
    "$BASE_URL/api/v1/files/${encoded_file_id}/download?user_id=${encoded_user_id}")"
  printf '%s\n' "$result" >"$status_file"
  assert_curl_2xx "GET /api/v1/files/{file_id}/download" "$result" "$stderr_file" "$output"
}

get_file_meta() {
  local file_id="$1"
  local output="$2"
  local stderr_file="$3"
  local status_file="$4"
  local encoded_file_id encoded_user_id result
  encoded_file_id="$(urlencode "$file_id")"
  encoded_user_id="$(urlencode "$USER_ID")"
  result="$(curl_capture "$output" "$stderr_file" \
    "${AUTH_HEADERS[@]}" \
    "$BASE_URL/api/v1/files/${encoded_file_id}?user_id=${encoded_user_id}")"
  printf '%s\n' "$result" >"$status_file"
  assert_curl_2xx "GET /api/v1/files/{file_id}" "$result" "$stderr_file" "$output"
}

write_summary_and_report() {
  local ok="$1"
  local reason="$2"
  local first_file_id="$3"
  local second_file_id="$4"
  local v1_sha="$5"
  local v2_sha="$6"
  local download_after_first_sha="$7"
  local download_after_second_sha="$8"
  local first_size second_size
  first_size="$(json_get_field "$OUT_DIR/upload-1.response.json" "size_bytes" || true)"
  second_size="$(json_get_field "$OUT_DIR/upload-2.response.json" "size_bytes" || true)"

  python3 - "$OUT_DIR/summary.json" \
    "$ok" \
    "$reason" \
    "$BASE_URL" \
    "$USER_ID" \
    "$FILENAME" \
    "$OUT_DIR" \
    "$first_file_id" \
    "$second_file_id" \
    "${first_size:-0}" \
    "${second_size:-0}" \
    "$v1_sha" \
    "$v2_sha" \
    "$download_after_first_sha" \
    "$download_after_second_sha" <<'PY'
import json
import sys

(
    output_path,
    ok,
    reason,
    base_url,
    user_id,
    filename,
    out_dir,
    first_file_id,
    second_file_id,
    first_size,
    second_size,
    v1_sha,
    v2_sha,
    download_after_first_sha,
    download_after_second_sha,
) = sys.argv[1:]

def parse_int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0

payload = {
    "ok": ok == "true",
    "reason": reason,
    "base_url": base_url,
    "user_id": user_id,
    "filename": filename,
    "out_dir": out_dir,
    "first_file_id": first_file_id,
    "second_file_id": second_file_id,
    "same_file_id": first_file_id == second_file_id,
    "first_upload_size_bytes": parse_int(first_size),
    "second_upload_size_bytes": parse_int(second_size),
    "payload_v1_sha256": v1_sha,
    "payload_v2_sha256": v2_sha,
    "download_after_first_sha256": download_after_first_sha,
    "download_after_second_sha256": download_after_second_sha,
    "download_after_first_matches_v1": v1_sha == download_after_first_sha,
    "download_after_second_matches_v2": v2_sha == download_after_second_sha,
}
with open(output_path, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
    fh.write("\n")
PY

  cat >"$OUT_DIR/REPORT.md" <<EOF
# 文件 update 接口二次提交测试

- 结论：$([[ "$ok" == "true" ]] && printf '通过' || printf '失败')
- 原因：${reason}
- Gateway：${BASE_URL}
- user_id：${USER_ID}
- 文件名：${FILENAME}
- 第一次 file_id：${first_file_id}
- 第二次 file_id：${second_file_id}
- 是否复用同一 file_id：$([[ "$first_file_id" == "$second_file_id" ]] && printf '是' || printf '否')
- 第一次下载是否等于 v1：$([[ "$v1_sha" == "$download_after_first_sha" ]] && printf '是' || printf '否')
- 第二次后下载是否等于 v2：$([[ "$v2_sha" == "$download_after_second_sha" ]] && printf '是' || printf '否')

## 证据文件

- \`summary.json\`
- \`upload-1.response.json\`
- \`upload-2.response.json\`
- \`meta-after-second.response.json\`
- \`download-after-first.bin\`
- \`download-after-second.bin\`
EOF
}

main() {
  log "输出目录：$OUT_DIR"
  log "Gateway：$BASE_URL"
  log "user_id：$USER_ID"
  log "同名文件：$FILENAME"
  log "第二次上传 overwrite=true：$SECOND_UPLOAD_OVERWRITE"
  if [[ "$SECOND_UPLOAD_OVERWRITE" == "true" && -z "$API_KEY" ]]; then
    log "提示：未提供 API_KEY；如果 Gateway 严格执行 public 契约，额外 overwrite 字段可能被拒绝"
  fi

  prepare_upload_files
  health_check

  local first_file_id second_file_id v1_sha v2_sha dl1_sha dl2_sha ok reason
  v1_sha="$(sha256_file "$OUT_DIR/payload-v1")"
  v2_sha="$(sha256_file "$OUT_DIR/payload-v2")"

  upload_once "$OUT_DIR/payload-v1" "$OUT_DIR/upload-1.response.json" "$OUT_DIR/upload-1.stderr" "$OUT_DIR/upload-1.status"
  first_file_id="$(json_get_file_id "$OUT_DIR/upload-1.response.json")" || die "第一次上传响应缺少 file_id"
  log "第一次上传 file_id=$first_file_id"

  download_file "$first_file_id" "$OUT_DIR/download-after-first.bin" "$OUT_DIR/download-after-first.stderr" "$OUT_DIR/download-after-first.status"
  dl1_sha="$(sha256_file "$OUT_DIR/download-after-first.bin")"

  upload_once "$OUT_DIR/payload-v2" "$OUT_DIR/upload-2.response.json" "$OUT_DIR/upload-2.stderr" "$OUT_DIR/upload-2.status" "$SECOND_UPLOAD_OVERWRITE"
  second_file_id="$(json_get_file_id "$OUT_DIR/upload-2.response.json")" || die "第二次上传响应缺少 file_id"
  log "第二次上传 file_id=$second_file_id"

  get_file_meta "$first_file_id" "$OUT_DIR/meta-after-second.response.json" "$OUT_DIR/meta-after-second.stderr" "$OUT_DIR/meta-after-second.status"
  download_file "$first_file_id" "$OUT_DIR/download-after-second.bin" "$OUT_DIR/download-after-second.stderr" "$OUT_DIR/download-after-second.status"
  dl2_sha="$(sha256_file "$OUT_DIR/download-after-second.bin")"

  ok="true"
  reason="同名二次提交复用同一 file_id，且第二次后下载内容已更新为 v2"
  if [[ "$first_file_id" != "$second_file_id" ]]; then
    ok="false"
    reason="第二次提交没有复用第一次 file_id"
  elif [[ "$v1_sha" != "$dl1_sha" ]]; then
    ok="false"
    reason="第一次上传后下载内容不等于 v1"
  elif [[ "$v2_sha" != "$dl2_sha" ]]; then
    ok="false"
    reason="第二次提交后下载内容没有更新为 v2"
  fi

  write_summary_and_report "$ok" "$reason" "$first_file_id" "$second_file_id" "$v1_sha" "$v2_sha" "$dl1_sha" "$dl2_sha"
  log "报告：$OUT_DIR/REPORT.md"
  log "摘要：$OUT_DIR/summary.json"

  [[ "$ok" == "true" ]] || die "$reason"
  log "测试通过"
}

main "$@"
