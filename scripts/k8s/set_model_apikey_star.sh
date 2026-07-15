#!/usr/bin/env bash
set -euo pipefail

NS="${NS:-ironagent-kf}"
ADMIN="${ADMIN:-http://21.44.192.81:30808}"
MODEL_NAMES="${MODEL_NAMES:-minimax-m2.7,openai-codex/gpt-5.4}"
API_KEY_VALUE="${API_KEY_VALUE:-*}"
UPDATE_ASSISTANTS="${UPDATE_ASSISTANTS:-0}"

log() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    log "[错误] 缺少命令：$1"
    exit 1
}
  }

need_cmd kubectl
need_cmd curl
need_cmd python3

log "[信息] namespace=$NS"
log "[信息] admin=$ADMIN"
log "[信息] 目标模型=$MODEL_NAMES"
log "[信息] 测试 API Key 字面值=$API_KEY_VALUE"
log "[信息] 是否同步已绑定助手=$UPDATE_ASSISTANTS"

TOKEN="$(kubectl -n "$NS" get secret control-plane-shared-secret \
  -o jsonpath='{.data.IRON_AGENT_INTERNAL_SERVICE_TOKEN}' | base64 -d 2>/dev/null || true)"

if [ -z "$TOKEN" ]; then
  log "[错误] 没有读取到 IRON_AGENT_INTERNAL_SERVICE_TOKEN"
  log "[提示] 请确认 namespace 是否正确：kubectl -n $NS get secret control-plane-shared-secret"
  exit 1
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

MODELS_JSON="$WORKDIR/models.json"
PLAN_JSON="$WORKDIR/plan.json"

log "[信息] 拉取当前模型认证列表"
curl -sS "$ADMIN/api/v1/admin/models" \
  -H "X-IronAgent-Internal-Token: $TOKEN" \
  -H "Accept: application/json" \
  > "$MODELS_JSON"

python3 - "$MODELS_JSON" "$PLAN_JSON" "$MODEL_NAMES" <<'PY'
import json
import sys

models_path, plan_path, names_csv = sys.argv[1], sys.argv[2], sys.argv[3]
target_names = [item.strip() for item in names_csv.split(",") if item.strip()]
payload = json.load(open(models_path, encoding="utf-8"))
items = payload.get("items", [])
plan = []
missing = []

for name in target_names:
    matches = [item for item in items if item.get("name") == name]
    if not matches:
        missing.append(name)
        continue
    plan.extend(matches)

print("[信息] 当前模型列表：")
for item in items:
    print(
        f"  - id={item.get('id')} name={item.get('name')} "
        f"provider={item.get('provider')} url={item.get('url')} "
        f"apiKeySet={item.get('apiKeySet')} assistants={','.join(item.get('assistantIds') or [])}"
    )

if missing:
    print("[错误] 未找到目标模型：" + ", ".join(missing), file=sys.stderr)
    sys.exit(2)

json.dump({"items": plan}, open(plan_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("[信息] 将更新以下模型：")
for item in plan:
    print(f"  - {item.get('id')} / {item.get('name')} / {item.get('provider')}")
PY

log "[信息] 开始更新模型认证 API Key"
python3 - "$PLAN_JSON" "$ADMIN" "$TOKEN" "$API_KEY_VALUE" <<'PY'
import json
import subprocess
import sys

plan_path, admin, token, api_key = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
items = json.load(open(plan_path, encoding="utf-8"))["items"]

for item in items:
    model_id = item["id"]
    body = {
        "name": item.get("name") or "",
        "provider": item.get("provider") or "",
        "url": item.get("url") or "",
        "apiKey": api_key,
        "status": item.get("status") or "active",
    }
    print(f"[信息] 更新模型 {model_id} 的 apiKey 为字面值 *")
    result = subprocess.run(
        [
            "curl", "-sS", "-X", "PUT",
            f"{admin}/api/v1/admin/models/{model_id}",
            "-H", f"X-IronAgent-Internal-Token: {token}",
            "-H", "Content-Type: application/json",
            "--data", json.dumps(body, ensure_ascii=False),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        sys.exit(result.returncode)
    print(result.stdout.strip())
PY

log "[信息] 执行模型探活"
python3 - "$PLAN_JSON" "$ADMIN" "$TOKEN" <<'PY'
import json
import subprocess
import sys

plan_path, admin, token = sys.argv[1], sys.argv[2], sys.argv[3]
items = json.load(open(plan_path, encoding="utf-8"))["items"]

for item in items:
    model_id = item["id"]
    print(f"[信息] 探活模型 {model_id}")
    result = subprocess.run(
        [
            "curl", "-sS", "-X", "POST",
            f"{admin}/api/v1/admin/models/{model_id}/probe",
            "-H", f"X-IronAgent-Internal-Token: {token}",
            "-H", "Accept: application/json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        sys.exit(result.returncode)
PY

if [ "$UPDATE_ASSISTANTS" != "1" ]; then
  log "[完成] 已更新模型认证记录。注意：已创建助手的运行态配置不会自动继承这次修改。"
  log "[提示] 如果要让已绑定助手实际使用 *，请重新执行：UPDATE_ASSISTANTS=1 bash $0"
  exit 0
fi

ASSISTANT_PLAN="$WORKDIR/assistant-plan.json"
python3 - "$PLAN_JSON" "$ASSISTANT_PLAN" "$API_KEY_VALUE" <<'PY'
import json
import sys

plan_path, output_path, api_key = sys.argv[1], sys.argv[2], sys.argv[3]
items = json.load(open(plan_path, encoding="utf-8"))["items"]
updates = {}

for item in items:
    for assistant_id in item.get("assistantIds") or []:
        updates[assistant_id] = {
            "primary_model": item.get("name") or "",
            "auth_mode": "host_iron_agent" if item.get("provider") == "host_iron_agent" else "provider_api_key",
            "provider_id": "" if item.get("provider") == "host_iron_agent" else item.get("provider") or "",
            "provider_base_url": "" if item.get("provider") == "host_iron_agent" else item.get("url") or "",
            "provider_api_key": "" if item.get("provider") == "host_iron_agent" else api_key,
        }

json.dump(updates, open(output_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("[信息] 需要同步的助手：")
for assistant_id, config in updates.items():
    print(f"  - {assistant_id}: {config['primary_model']} / {config['provider_id'] or config['auth_mode']}")
PY

log "[信息] 开始同步已绑定助手配置并重启"
python3 - "$ASSISTANT_PLAN" "$ADMIN" "$TOKEN" <<'PY'
import json
import subprocess
import sys

plan_path, admin, token = sys.argv[1], sys.argv[2], sys.argv[3]
updates = json.load(open(plan_path, encoding="utf-8"))

for assistant_id, config in updates.items():
    body = {
        "provision_runtime": True,
        "primary_model": config["primary_model"],
        "provider_id": config["provider_id"],
        "provider_base_url": config["provider_base_url"],
        "provider_api_key": config["provider_api_key"],
    }
    print(f"[信息] 更新助手 {assistant_id} 的运行态模型配置")
    update = subprocess.run(
        [
            "curl", "-sS", "-f", "-X", "PUT",
            f"{admin}/api/v1/admin/assistants/{assistant_id}/model-config",
            "-H", f"X-IronAgent-Internal-Token: {token}",
            "-H", "Content-Type: application/json",
            "--data", json.dumps(body, ensure_ascii=False),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    print(update.stdout.strip())
    if update.returncode != 0:
        print(update.stderr, file=sys.stderr)
        sys.exit(update.returncode)

    print(f"[信息] 重启助手 {assistant_id}")
    restart = subprocess.run(
        [
            "curl", "-sS", "-X", "POST",
            f"{admin}/api/v1/admin/assistants/{assistant_id}/restart",
            "-H", f"X-IronAgent-Internal-Token: {token}",
            "-H", "Accept: application/json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    print(restart.stdout.strip())
    if restart.returncode != 0:
        print(restart.stderr, file=sys.stderr)
        sys.exit(restart.returncode)
PY

log "[完成] 模型认证与已绑定助手配置已处理。请等待 Pod 重启完成后检查 /home/node/.ironagent/ironagent.json。"
