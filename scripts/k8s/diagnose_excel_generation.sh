#!/usr/bin/env bash
# Diagnose Excel generation failures inside an IronAgent/IronAgent assistant pod.

set -Eeuo pipefail

NS="ironagent"
ASSISTANT_ID=""
CONTAINER=""
SINCE="30m"
TAIL_LINES="240"
OUT_BASE="output/excel-diagnose"
RUN_PROBE="true"
KEEP_PROBE_FILE="false"
PUBLIC_URL=""
WATCH_SECONDS="0"
TASK_ID=""
KEYWORD=""
EXPECTED_OUTPUT=""

usage() {
  cat <<'USAGE'
用法:
  bash scripts/k8s/diagnose_excel_generation.sh --assistant-id <助手ID> [选项]

选项:
  --assistant-id <id>      必填。要排查的助手 ID，例如 apply-test、111、0099。
  --namespace <ns>         Kubernetes namespace，默认 ironagent。
  --container <name>       助手 Pod 内容器名，默认自动选择 ironagent-gateway 或第一个容器。
  --since <duration>       拉取最近多久的日志，默认 30m。
  --tail <n>               每个服务最多拉取多少行日志，默认 240。
  --out-dir <dir>          输出目录根路径，默认 output/excel-diagnose。
  --public-url <url>       可选。外部入口，例如 http://192.0.2.10:30808，用于健康检查。
  --watch-seconds <n>      可选。进入复现窗口 n 秒；窗口内请在页面触发生成 Excel。
  --task-id <id>           可选。按任务 ID 单独提取相关日志行。
  --keyword <text>         可选。按关键词单独提取相关日志行，例如 xlsx、生成报表。
  --expected-output <path> 可选。预期生成的工作区文件路径，例如 /workspace/persistent/report.xlsx。
  --no-probe               不在助手容器内执行 Excel 生成探针，只收集日志和环境。
  --keep-probe-file        保留探针生成的 /workspace/temp/*.xlsx 文件。
  -h, --help               显示帮助。

输出:
  run.log                  中文排查日志。
  assistant-python-probe.log  Python 依赖和 Excel 生成探针输出。
  matched-errors.log       从所有日志里提取的错误证据行。
  summary.txt              直接原因判断和关键证据。

示例:
  bash scripts/k8s/diagnose_excel_generation.sh --assistant-id apply-test
  bash scripts/k8s/diagnose_excel_generation.sh --assistant-id apply-test --namespace ironagent-kf --watch-seconds 180
  bash scripts/k8s/diagnose_excel_generation.sh --assistant-id apply-test --task-id gw-xxx --keyword xlsx --expected-output /workspace/persistent/report.xlsx
  bash scripts/k8s/diagnose_excel_generation.sh --assistant-id 111 --public-url http://192.0.2.10:30808
  bash scripts/k8s/diagnose_excel_generation.sh --assistant-id 0099 --namespace ironagent --no-probe
USAGE
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
    --public-url)
      PUBLIC_URL="${2:-}"
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
    --expected-output)
      EXPECTED_OUTPUT="${2:-}"
      shift 2
      ;;
    --no-probe)
      RUN_PROBE="false"
      shift
      ;;
    --keep-probe-file)
      KEEP_PROBE_FILE="true"
      shift
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
  echo "缺少必填参数: --assistant-id" >&2
  usage >&2
  exit 2
fi

timestamp() {
  date '+%F %T'
}

safe_name() {
  printf '%s' "$1" | tr -c 'A-Za-z0-9_.-' '_' | sed 's/^_*//;s/_*$//'
}

SAFE_ASSISTANT_ID="$(safe_name "$ASSISTANT_ID")"
RUN_ID="$(date '+%Y%m%d%H%M%S')"
OUT_DIR="${OUT_BASE%/}/${SAFE_ASSISTANT_ID}-${RUN_ID}"
RUN_LOG="$OUT_DIR/run.log"
SUMMARY="$OUT_DIR/summary.txt"
MATCHED_ERRORS="$OUT_DIR/matched-errors.log"
FOCUS_LINES="$OUT_DIR/focus-lines.log"
PROBE_SCRIPT="$OUT_DIR/excel_probe.py"
PROBE_LOG="$OUT_DIR/assistant-python-probe.log"

mkdir -p "$OUT_DIR/k8s" "$OUT_DIR/http" "$OUT_DIR/repro"
touch "$RUN_LOG"

exec > >(tee -a "$RUN_LOG") 2>&1

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

write_probe_script() {
  cat >"$PROBE_SCRIPT" <<'PY'
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import sys
import traceback
import zipfile


MODULES = {
    "openpyxl": "openpyxl",
    "pandas": "pandas",
    "xlsxwriter": "xlsxwriter",
    "numpy": "numpy",
    "olefile": "olefile",
    "pymupdf/fitz": "fitz",
    "Pillow/PIL": "PIL",
}


def log(message: str) -> None:
    print(f"[探针] {message}", flush=True)


def module_version(module: object) -> str:
    return str(getattr(module, "__version__", "已安装但未提供 __version__"))


def import_module(label: str, module_name: str) -> dict[str, object]:
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - diagnostic script must capture exact failures.
        return {
            "name": label,
            "module": module_name,
            "ok": False,
            "error": repr(exc),
            "traceback": traceback.format_exc(),
        }
    return {
        "name": label,
        "module": module_name,
        "ok": True,
        "version": module_version(module),
    }


def check_workspace(path: Path) -> dict[str, object]:
    result: dict[str, object] = {
        "path": str(path),
        "exists": path.exists(),
        "is_dir": path.is_dir(),
        "writable": os.access(path, os.W_OK),
    }
    try:
        path.mkdir(parents=True, exist_ok=True)
        marker = path / ".iron_agent_excel_write_probe"
        marker.write_text("ok", encoding="utf-8")
        marker.unlink(missing_ok=True)
        result["write_probe"] = "ok"
    except Exception as exc:  # noqa: BLE001
        result["write_probe"] = "failed"
        result["error"] = repr(exc)
        result["traceback"] = traceback.format_exc()
    return result


def validate_xlsx(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"path": str(path), "ok": False, "error": "文件不存在"}
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            missing = [
                name
                for name in ("[Content_Types].xml", "xl/workbook.xml")
                if name not in names
            ]
        return {
            "path": str(path),
            "ok": not missing,
            "size_bytes": path.stat().st_size,
            "missing": missing,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "path": str(path),
            "ok": False,
            "error": repr(exc),
            "traceback": traceback.format_exc(),
        }


def generate_openpyxl(path: Path) -> dict[str, object]:
    try:
        import openpyxl

        path.parent.mkdir(parents=True, exist_ok=True)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "诊断"
        ws["A1"] = "Excel 生成探针"
        ws["A2"] = "中文写入测试"
        ws["B2"] = 123
        wb.save(path)
        loaded = openpyxl.load_workbook(path)
        value = loaded["诊断"]["A2"].value
        loaded.close()
        validation = validate_xlsx(path)
        return {
            "engine": "openpyxl",
            "ok": validation.get("ok") is True and value == "中文写入测试",
            "path": str(path),
            "validation": validation,
            "read_back": value,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "engine": "openpyxl",
            "ok": False,
            "path": str(path),
            "error": repr(exc),
            "traceback": traceback.format_exc(),
        }


def generate_pandas_xlsxwriter(path: Path) -> dict[str, object]:
    try:
        import pandas as pd

        path.parent.mkdir(parents=True, exist_ok=True)
        frame = pd.DataFrame(
            [
                {"项目": "中文写入测试", "数量": 1},
                {"项目": "依赖探针", "数量": 2},
            ]
        )
        with pd.ExcelWriter(path, engine="xlsxwriter") as writer:
            frame.to_excel(writer, index=False, sheet_name="诊断")
        validation = validate_xlsx(path)
        return {
            "engine": "pandas+xlsxwriter",
            "ok": validation.get("ok") is True,
            "path": str(path),
            "validation": validation,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "engine": "pandas+xlsxwriter",
            "ok": False,
            "path": str(path),
            "error": repr(exc),
            "traceback": traceback.format_exc(),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="/workspace/temp")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    log(f"Python: {sys.version}")
    log(f"当前用户 uid={os.getuid()} gid={os.getgid()} cwd={os.getcwd()}")
    log(f"目标输出目录: {output_dir}")

    report: dict[str, object] = {
        "python": sys.version,
        "uid": os.getuid(),
        "gid": os.getgid(),
        "workspace": {
            "/workspace": check_workspace(Path("/workspace")),
            str(output_dir): check_workspace(output_dir),
        },
        "imports": [import_module(label, module) for label, module in MODULES.items()],
        "generation": [],
    }

    for item in report["imports"]:  # type: ignore[index]
        if item.get("ok"):  # type: ignore[union-attr]
            log(f"依赖可导入: {item['name']} {item.get('version', '')}")  # type: ignore[index]
        else:
            log(f"依赖导入失败: {item['name']} -> {item.get('error')}")  # type: ignore[index, union-attr]

    openpyxl_path = output_dir / "iron_agent_excel_diag_openpyxl.xlsx"
    pandas_path = output_dir / "iron_agent_excel_diag_pandas_xlsxwriter.xlsx"
    generations = [
        generate_openpyxl(openpyxl_path),
        generate_pandas_xlsxwriter(pandas_path),
    ]
    report["generation"] = generations

    for item in generations:
        if item.get("ok"):
            log(f"Excel 生成成功: {item['engine']} -> {item['path']}")
        else:
            log(f"Excel 生成失败: {item['engine']} -> {item.get('error', '未知错误')}")
            if item.get("traceback"):
                print(item["traceback"], flush=True)

    if not args.keep:
        for path in (openpyxl_path, pandas_path):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    print("JSON_REPORT_BEGIN")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    print("JSON_REPORT_END")

    if not generations[0].get("ok"):
        return 20
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
PY
}

collect_logs() {
  run_capture "namespace 状态" "$OUT_DIR/k8s/namespace.txt" kubectl get namespace "$NS" -o wide
  run_capture "Pod 列表" "$OUT_DIR/k8s/pods.txt" kubectl -n "$NS" get pods -o wide
  run_capture "Service 列表" "$OUT_DIR/k8s/services.txt" kubectl -n "$NS" get svc -o wide
  run_capture "近期事件" "$OUT_DIR/k8s/events.txt" kubectl -n "$NS" get events --sort-by=.lastTimestamp
  run_capture "助手 Pod 描述" "$OUT_DIR/k8s/assistant-pod-describe.txt" kubectl -n "$NS" describe pod "$POD_NAME"
  run_capture "助手容器日志" "$OUT_DIR/k8s/assistant-container.log" kubectl -n "$NS" logs "$POD_NAME" -c "$CONTAINER" --since="$SINCE" --tail="$TAIL_LINES"
  run_capture "助手容器上一轮日志" "$OUT_DIR/k8s/assistant-container-previous.log" kubectl -n "$NS" logs "$POD_NAME" -c "$CONTAINER" --previous --tail="$TAIL_LINES" || true

  for deploy in go-gateway admin security-service; do
    run_capture "${deploy} 日志" "$OUT_DIR/k8s/${deploy}.log" kubectl -n "$NS" logs "deploy/${deploy}" --since="$SINCE" --tail="$TAIL_LINES" || true
  done
}

collect_container_state() {
  run_capture "助手容器环境变量摘要" "$OUT_DIR/k8s/assistant-env.txt" \
    kubectl_exec sh -lc 'printenv | sort | grep -E "^(ASSISTANT|IRON_AGENT|IRON_AGENT|WORKSPACE|PYTHON|PATH|HOME|USER)=" | sed -E "s/(TOKEN|KEY|SECRET|PASSWORD)=.*/\\1=***MASKED***/"'
  run_capture "workspace 目录权限和容量" "$OUT_DIR/k8s/workspace-permissions.txt" \
    kubectl_exec sh -lc 'id; pwd; df -h /workspace 2>/dev/null || true; ls -ld /workspace /workspace/temp /workspace/persistent /workspace/skills /workspace/plugins 2>&1 || true'
  run_capture "workspace 技能文件摘要" "$OUT_DIR/k8s/workspace-skills.txt" \
    kubectl_exec sh -lc 'find /workspace/skills -maxdepth 3 -type f 2>/dev/null | sort | head -160 || true'
  if [[ -n "$EXPECTED_OUTPUT" ]]; then
    run_capture "预期输出文件检查" "$OUT_DIR/k8s/expected-output.txt" \
      kubectl_exec sh -lc 'path="$1"; echo "expected_output=$path"; if [ -e "$path" ]; then ls -lh "$path"; file "$path" 2>/dev/null || true; else echo "EXPECTED_OUTPUT_MISSING"; parent=$(dirname "$path"); echo "parent=$parent"; ls -ld "$parent" 2>&1 || true; echo "parent_recent_files:"; find "$parent" -maxdepth 1 -type f -exec ls -lh {} \; 2>/dev/null | tail -40 || true; case "$path" in *.xlsx|*.xls|*.csv) ;; *) alt="${path}.xlsx"; echo "extensionless_candidate=$alt"; [ -e "$alt" ] && ls -lh "$alt" || true ;; esac; fi' sh "$EXPECTED_OUTPUT" || true
  fi
}

run_python_probe() {
  if [[ "$RUN_PROBE" != "true" ]]; then
    log "跳过 Excel 生成探针：已指定 --no-probe"
    return 0
  fi
  write_probe_script
  log "复制 Python 探针到助手容器 /tmp/iron_agent_excel_probe.py"
  if ! kubectl -n "$NS" cp -c "$CONTAINER" "$PROBE_SCRIPT" "${POD_NAME}:/tmp/iron_agent_excel_probe.py" >"$OUT_DIR/k8s/kubectl-cp-probe.log" 2>&1; then
    warn "kubectl cp 失败，改用 stdin 写入探针脚本。详情见 $OUT_DIR/k8s/kubectl-cp-probe.log"
    kubectl -n "$NS" exec -i "$POD_NAME" -c "$CONTAINER" -- sh -lc \
      'cat > /tmp/iron_agent_excel_probe.py && chmod 0755 /tmp/iron_agent_excel_probe.py' \
      <"$PROBE_SCRIPT" >"$OUT_DIR/k8s/kubectl-stdin-probe.log" 2>&1
  fi

  local keep_flag=""
  if [[ "$KEEP_PROBE_FILE" == "true" ]]; then
    keep_flag="--keep"
  fi

  log "执行 Excel 生成探针；完整输出写入 ${PROBE_LOG}"
  set +e
  kubectl_exec python3 /tmp/iron_agent_excel_probe.py --output-dir /workspace/temp $keep_flag >"$PROBE_LOG" 2>&1
  local code=$?
  set -e
  sed -n '1,220p' "$PROBE_LOG"
  if [[ "$code" -ne 0 ]]; then
    warn "Excel 探针返回非 0：${code}。这通常就是直接错误证据，继续提取根因。"
  fi
}

http_checks() {
  if [[ -z "$PUBLIC_URL" ]]; then
    return 0
  fi
  if ! command -v curl >/dev/null 2>&1; then
    warn "未安装 curl，跳过外部 HTTP 检查"
    return 0
  fi
  run_capture "外部入口 healthz" "$OUT_DIR/http/healthz.txt" curl -sS -i --max-time 8 "${PUBLIC_URL%/}/healthz" || true
  run_capture "审批列表接口" "$OUT_DIR/http/approvals.txt" curl -sS -i --max-time 8 "${PUBLIC_URL%/}/api/v1/approvals?status=pending" || true
}

collect_reproduction_window_logs() {
  if ! [[ "$WATCH_SECONDS" =~ ^[0-9]+$ ]] || [[ "$WATCH_SECONDS" -le 0 ]]; then
    return 0
  fi

  local pids=()
  local pid=""
  log "进入复现窗口：${WATCH_SECONDS} 秒。请现在在页面触发一次生成 Excel，脚本会同步抓取日志。"

  kubectl -n "$NS" logs -f "$POD_NAME" -c "$CONTAINER" --since=15s \
    >"$OUT_DIR/repro/assistant-follow.log" 2>&1 &
  pids+=("$!")

  for deploy in go-gateway admin security-service; do
    kubectl -n "$NS" logs -f "deploy/${deploy}" --since=15s \
      >"$OUT_DIR/repro/${deploy}-follow.log" 2>&1 &
    pids+=("$!")
  done

  sleep "$WATCH_SECONDS"

  for pid in "${pids[@]}"; do
    kill "$pid" >/dev/null 2>&1 || true
  done
  for pid in "${pids[@]}"; do
    wait "$pid" >/dev/null 2>&1 || true
  done

  log "复现窗口结束，开始采集复现后的工作区文件"
  run_capture "复现后 workspace 表格文件" "$OUT_DIR/k8s/workspace-excel-after-repro.txt" \
    kubectl_exec sh -lc 'find /workspace -maxdepth 6 \( -iname "*.xlsx" -o -iname "*.xls" -o -iname "*.csv" \) -exec ls -lh {} \; 2>/dev/null | tail -120 || true'
  if [[ -n "$EXPECTED_OUTPUT" ]]; then
    run_capture "复现后预期输出文件检查" "$OUT_DIR/k8s/expected-output-after-repro.txt" \
      kubectl_exec sh -lc 'path="$1"; echo "expected_output=$path"; if [ -e "$path" ]; then ls -lh "$path"; file "$path" 2>/dev/null || true; else echo "EXPECTED_OUTPUT_MISSING"; parent=$(dirname "$path"); echo "parent=$parent"; ls -ld "$parent" 2>&1 || true; echo "parent_recent_files:"; find "$parent" -maxdepth 1 -type f -exec ls -lh {} \; 2>/dev/null | tail -40 || true; case "$path" in *.xlsx|*.xls|*.csv) ;; *) alt="${path}.xlsx"; echo "extensionless_candidate=$alt"; [ -e "$alt" ] && ls -lh "$alt" || true ;; esac; fi' sh "$EXPECTED_OUTPUT" || true
  fi
}

grep_source_logs() {
  local pattern="$1"
  {
    grep -RInE "$pattern" "$OUT_DIR/k8s" "$OUT_DIR/http" "$OUT_DIR/repro" 2>/dev/null || true
    if [[ -f "$PROBE_LOG" ]]; then
      grep -InE "$pattern" "$PROBE_LOG" 2>/dev/null || true
    fi
  } | grep -vF "$PROBE_SCRIPT" \
    | grep -vF "$MATCHED_ERRORS" \
    | grep -vF "$SUMMARY" \
    | grep -vF "$RUN_LOG" \
    || true
}

has_source_match() {
  local pattern="$1"
  grep_source_logs "$pattern" | grep -q .
}

grep_focus_logs() {
  local value="$1"
  [[ -n "$value" ]] || return 0
  {
    grep -RInF "$value" "$OUT_DIR/k8s" "$OUT_DIR/http" "$OUT_DIR/repro" 2>/dev/null || true
    if [[ -f "$PROBE_LOG" ]]; then
      grep -InF "$value" "$PROBE_LOG" 2>/dev/null || true
    fi
  } | grep -vF "$PROBE_SCRIPT" \
    | grep -vF "$MATCHED_ERRORS" \
    | grep -vF "$SUMMARY" \
    | grep -vF "$RUN_LOG" \
    | grep -vF "$FOCUS_LINES" \
    || true
}

extract_errors() {
  log "提取错误证据行"
  grep_source_logs \
    "Traceback|Exception|ERROR|Error|error|failed|失败|报错|ModuleNotFoundError|No module named|ImportError|Permission denied|Read-only file system|No space left|No such file or directory|ls: cannot access|Command exited with code|pip install|openpyxl|xlsxwriter|pandas|pending_approval|401 Unauthorized|invalid api key|approval|审批|EXPECTED_OUTPUT_MISSING" \
    >"$MATCHED_ERRORS"

  if [[ -s "$MATCHED_ERRORS" ]]; then
    log "已提取到错误证据，前 160 行如下："
    sed -n '1,160p' "$MATCHED_ERRORS"
  else
    log "未从日志中匹配到明显错误关键字"
  fi

  {
    if [[ -n "$TASK_ID" ]]; then
      echo "===== task_id: ${TASK_ID} ====="
      grep_focus_logs "$TASK_ID"
    fi
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
    log "已提取 task_id/关键词相关日志，前 120 行如下："
    sed -n '1,120p' "$FOCUS_LINES"
  fi
}

append_cause() {
  local cause="$1"
  local evidence_pattern="$2"
  {
    echo
    echo "直接原因候选：${cause}"
    echo "证据："
    grep_source_logs "$evidence_pattern" | head -20 || true
  } >>"$SUMMARY"
}

summarize_root_cause() {
  log "生成直接原因判断"
  {
    echo "Excel 生成失败排查汇总"
    echo "运行时间：$(timestamp)"
    echo "namespace：${NS}"
    echo "assistant_id：${ASSISTANT_ID}"
    echo "pod：${POD_NAME}"
    echo "container：${CONTAINER}"
    echo "输出目录：${OUT_DIR}"
    if [[ "$WATCH_SECONDS" =~ ^[0-9]+$ ]] && [[ "$WATCH_SECONDS" -gt 0 ]]; then
      echo "复现窗口：${WATCH_SECONDS} 秒"
    fi
    if [[ -n "$TASK_ID" ]]; then
      echo "task_id：${TASK_ID}"
    fi
    if [[ -n "$KEYWORD" ]]; then
      echo "keyword：${KEYWORD}"
    fi
    if [[ -n "$EXPECTED_OUTPUT" ]]; then
      echo "expected_output：${EXPECTED_OUTPUT}"
    fi
  } >"$SUMMARY"

  local found="false"
  if [[ -s "$FOCUS_LINES" ]] && grep -vq '^=====' "$FOCUS_LINES"; then
    {
      echo
      echo "聚焦日志：已找到 task_id/关键词相关行，请优先查看 ${FOCUS_LINES}"
      sed -n '1,40p' "$FOCUS_LINES"
    } >>"$SUMMARY"
  fi
  if has_source_match "ModuleNotFoundError: No module named '(openpyxl|xlsxwriter|pandas)'|No module named (openpyxl|xlsxwriter|pandas)|ImportError.*(openpyxl|xlsxwriter|pandas)"; then
    append_cause "Python Excel 依赖缺失或导入失败，优先补镜像内 openpyxl/xlsxwriter/pandas 依赖。" "ModuleNotFoundError: No module named '(openpyxl|xlsxwriter|pandas)'|No module named (openpyxl|xlsxwriter|pandas)|ImportError.*(openpyxl|xlsxwriter|pandas)"
    found="true"
  fi
  if has_source_match "Permission denied|Read-only file system|No space left on device|\"write_probe\": \"failed\""; then
    append_cause "workspace 不可写、只读或磁盘空间不足，Excel 文件无法落到 /workspace/temp 或目标目录。" "Permission denied|Read-only file system|No space left on device|write_probe.*failed"
    found="true"
  fi
  if has_source_match "ls: cannot access|No such file or directory|Command exited with code 2"; then
    append_cause "执行链路在生成前把目标路径当成已存在文件/目录检查，实际路径不存在；应创建父目录并把输出写成明确的 .xlsx 文件路径。" "ls: cannot access|No such file or directory|Command exited with code 2"
    found="true"
  fi
  if has_source_match "pip install .*openpyxl|pip install .*xlsxwriter|pip install .*pandas"; then
    append_cause "助手运行中尝试 pip install Excel 依赖；内网环境不应运行时联网安装，依赖应预置在镜像内，skill/提示词也不应包含 pip install 兜底。" "pip install .*openpyxl|pip install .*xlsxwriter|pip install .*pandas"
    found="true"
  fi
  if has_source_match "EXPECTED_OUTPUT_MISSING"; then
    append_cause "预期输出文件未落盘；优先检查目标路径是否带 .xlsx 后缀、父目录是否存在、生成脚本是否真正执行到 save/write 阶段。" "EXPECTED_OUTPUT_MISSING|extensionless_candidate|parent_recent_files"
    found="true"
  fi
  if [[ -f "$PROBE_LOG" ]] && grep -InE "Excel 生成失败|BadZipFile|FileNotFoundError|OSError" "$PROBE_LOG" >/dev/null 2>&1; then
    append_cause "容器内 Excel 生成探针失败，直接错误见 assistant-python-probe.log 中的 traceback。" "Excel 生成失败|Traceback|BadZipFile|FileNotFoundError|OSError"
    found="true"
  fi
  if has_source_match "pending_approval|approval.*pending|requires approval"; then
    append_cause "操作被审批流程拦截，生成或上传文件未真正执行完成；需要看审批是否弹出、是否 resolved。" "pending_approval|审批|approval.*pending|requires approval"
    found="true"
  fi
  if has_source_match "401 Unauthorized|invalid api key"; then
    append_cause "页面控制面存在 401 噪声或认证失败；它可能影响审批轮询/侧边栏状态，但不一定是 Excel 文件生成本身失败。" "401 Unauthorized|invalid api key"
    found="true"
  fi
  if has_source_match "No such file or directory|package archive not found|skill.*not found|技能.*不存在"; then
    append_cause "skill 文件或安装包路径不一致，助手可能没有真正加载目标生成 Excel 的 skill。" "No such file or directory|package archive not found|skill.*not found|技能.*不存在"
    found="true"
  fi

  if [[ "$found" != "true" ]]; then
    {
      echo
      echo "直接原因候选：未发现明确失败点。"
      if [[ -f "$PROBE_LOG" ]] && grep -q "Excel 生成成功" "$PROBE_LOG"; then
        echo "低层探针结论：容器内 openpyxl / pandas+xlsxwriter 生成 xlsx 成功，依赖和 /workspace/temp 写入不是当前探针能复现的直接原因。"
      fi
      echo "建议：用 --watch-seconds 在用户页面复现生成动作，或带 --task-id/--keyword 重新运行。"
    } >>"$SUMMARY"
  fi

  log "汇总如下："
  cat "$SUMMARY"
}

main() {
  need_cmd kubectl
  log "开始排查 Excel 生成失败"
  log "输出目录：${OUT_DIR}"
  run_capture "kubectl 当前上下文" "$OUT_DIR/k8s/current-context.txt" kubectl config current-context || true

  if ! kubectl get namespace "$NS" >/dev/null 2>&1; then
    fail "namespace 不存在或无权限访问：${NS}"
  fi

  POD_NAME="$(find_assistant_pod || true)"
  if [[ -z "$POD_NAME" ]]; then
    run_capture "Pod 列表" "$OUT_DIR/k8s/pods.txt" kubectl -n "$NS" get pods -o wide || true
    fail "没有找到助手 Pod。请确认 assistant_id=${ASSISTANT_ID} 是否正确，或用 --namespace 指定正确命名空间。"
  fi
  CONTAINER="$(select_container)"
  if [[ -z "$CONTAINER" ]]; then
    fail "没有找到助手 Pod 的容器：${POD_NAME}"
  fi
  log "目标助手 Pod：${POD_NAME}"
  log "目标容器：${CONTAINER}"

  collect_logs
  collect_container_state
  run_python_probe
  collect_reproduction_window_logs
  http_checks
  extract_errors
  summarize_root_cause

  log "排查完成。完整日志：${RUN_LOG}"
}

main "$@"
