# Gateway 20 助手并发证明手册

目标：证明服务器上 20 个助手通过 Gateway 同时完成 `upload -> /chat -> task terminal -> callback -> result file download`。服务器侧只要求证明到 20 并发，不要求 40 并发。

## 证明边界

这套证明不把 `202 Accepted` 当成功，只把它作为接入成功证据。最终通过必须同时满足：

- 20 个 case 都完成文件上传。
- 20 个 `/api/v1/chat` 在同一个提交窗口内返回 task id。
- 20 个任务到达终态，默认要求 `completed/done`。
- 开启 `requires_result_file` 时，20 个任务都必须返回非空 `files[]`，且至少一个结果文件 `download_url` 可 GET 200。
- 开启 callback 强校验时，20 个任务都必须能在 DB/kubectl/receiver 证据里找到 callback。
- 开启 runtime 并发强校验时，MySQL 采样必须看到 20 个不同助手在同一运行窗口内有 `in_flight > 0`，且这些 in-flight runtime 不允许 `last_heartbeat_at IS NULL`。

## 准备 cases

生成模板：

```bash
cd /path/to/ironagent
python3 scripts/e2e/prove_gateway_20_assistants.py \
  --write-template output/gw20-cases.template.json \
  --template-count 20
```

把模板里的 `user_id` 和 `assistant_id` 替换成内网真实绑定关系。每一行必须是一组已经创建并绑定好的用户和助手。

如果需要批量创建 20 个测试用户/助手，可以用：

```bash
cd /path/to/ironagent

export ADMIN_BASE='http://192.0.2.10:30808'
export IRON_AGENT_INTERNAL_SERVICE_TOKEN='替换成真实内部token'
export RUN_ID="gw20-$(date +%Y%m%d%H%M%S)"

# 优先使用服务器模型注册表中已有模型。
export MODEL_ID='dashscope-coding-minimax-m2-5'

bash scripts/k8s/create_gateway_20_proof_assistants.sh
```

如果模型注册表不可用，可使用直连 OpenAI-compatible provider 配置：

```bash
export MODEL_ID=''
export PRIMARY_MODEL='MiniMax-M2.5'
export PROVIDER_ID='dashscope-coding'
export PROVIDER_BASE_URL='https://coding.dashscope.aliyuncs.com/v1'
export PROVIDER_API_KEY='替换成真实 API Key'

bash scripts/k8s/create_gateway_20_proof_assistants.sh
```

## 修复现有助手模型运行态 401

如果 fullflow 报错：

```text
runtime chat error: HTTP 401: invalid access token or token expired
```

只更新模型注册表还不够。已经创建的助手需要重新同步 `assistants.iron_agent_config` 并重生成 runtime 配置。部署包含 `PUT /api/v1/admin/assistants/{assistant_id}/model-config` 的 admin 后，按下面执行：

```bash
cd /path/to/ironagent

export ADMIN_BASE='http://192.0.2.10:30808'
export IRON_AGENT_INTERNAL_SERVICE_TOKEN='替换成真实内部token'

# 先确保模型注册表中的 key 是新的。不要把真实 key 写入日志。
python3 - <<'PY'
import json
import os
import urllib.request

admin = os.environ["ADMIN_BASE"].rstrip("/")
token = os.environ["IRON_AGENT_INTERNAL_SERVICE_TOKEN"]
model_id = "dashscope-coding-minimax-m2-5"
body = {
    "name": "MiniMax-M2.5",
    "provider": "dashscope-coding",
    "url": "https://coding.dashscope.aliyuncs.com/v1",
    "apiKey": os.environ["DASHSCOPE_API_KEY"],
    "status": "active",
}
req = urllib.request.Request(
    f"{admin}/api/v1/admin/models/{model_id}",
    data=json.dumps(body).encode("utf-8"),
    headers={
        "X-IronAgent-Internal-Token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    },
    method="PUT",
)
with urllib.request.urlopen(req, timeout=60) as resp:
    print(resp.status, resp.read().decode("utf-8"))
PY

# 再把 cases.json 里的现有助手同步到这个模型，并触发 restart。
python3 scripts/k8s/sync_assistant_model_config.py \
  --admin-base "$ADMIN_BASE" \
  --token "$IRON_AGENT_INTERNAL_SERVICE_TOKEN" \
  --cases-file output/gw20-cases.json \
  --model-id dashscope-coding-minimax-m2-5 \
  --timeout 120
```

也可以绕过模型注册表，直接把 DashScope Coding provider 写进每个助手：

```bash
python3 scripts/k8s/sync_assistant_model_config.py \
  --admin-base "$ADMIN_BASE" \
  --token "$IRON_AGENT_INTERNAL_SERVICE_TOKEN" \
  --cases-file output/gw20-cases.json \
  --primary-model 'MiniMax-M2.5' \
  --provider-id 'dashscope-coding' \
  --provider-base-url 'https://coding.dashscope.aliyuncs.com/v1' \
  --provider-api-key "$DASHSCOPE_API_KEY" \
  --timeout 120
```

修复后先等 `/readyz` 回到目标 ready 数，再重新运行 fullflow 证明脚本。

创建完成后先确认 `/readyz` 中 runtime ready 数足够，且这 20 个目标助手都为 `running`。证明脚本默认会做 ready preflight；未满 20 个 running 时会直接输出 `INCONCLUSIVE` 并停止。

也可以使用 CSV：

```csv
case_id,user_id,assistant_id
a01,user-01,assistant-01
a02,user-02,assistant-02
```

## 服务器证明命令

推荐在能访问 Gateway、Admin、MySQL、kubectl 的服务器上运行：

```bash
cd /path/to/ironagent

export GATEWAY_BASE='http://192.0.2.10:30808'
export ADMIN_BASE='http://192.0.2.10:30808'
export IRON_AGENT_INTERNAL_SERVICE_TOKEN='替换成真实内部token'
export PROOF_MYSQL_URL='mysql://USER:PASSWORD@HOST:3306/DBNAME'

python3 scripts/e2e/prove_gateway_20_assistants.py \
  --cases-file output/gw20-cases.json \
  --expected-count 20 \
  --concurrency 20 \
  --setup-concurrency 4 \
  --setup-mode fullflow \
  --requires-result-file \
  --require-result-files \
  --require-callback-log \
  --require-runtime-concurrency \
  --callback-mode db \
  --mysql-url "$PROOF_MYSQL_URL" \
  --callback-db-url "$PROOF_MYSQL_URL" \
  --poll-seconds 1200 \
  --poll-interval 10 \
  --max-submit-span-seconds 30 \
  --out-dir output/e2e-gateway-20-assistants-server
```

如果只是先检查 20 个目标助手是否 ready，可以运行同一脚本。它会先生成：

- `00-preflight-assistant-preflight.json`
- `00-preflight-readyz.*`
- `00-preflight-assistants.*`

未满 20 个 running 时不会进入压测阶段。

如果 MySQL 不能直连，但服务器能用 kubectl 查 callback 包，可先降低 runtime 强校验：

```bash
python3 scripts/e2e/prove_gateway_20_assistants.py \
  --cases-file output/gw20-cases.json \
  --expected-count 20 \
  --concurrency 20 \
  --setup-concurrency 4 \
  --setup-mode fullflow \
  --requires-result-file \
  --require-result-files \
  --require-callback-log \
  --callback-mode kubectl \
  --namespace ironagent \
  --poll-seconds 1200 \
  --poll-interval 10 \
  --out-dir output/e2e-gateway-20-assistants-server
```

这种模式可以证明 upload/chat/callback/files，但 runtime `in_flight` 并发会标记为证据不足，不能作为 20 runtime 槽位通过结论。

## 产物目录

脚本会生成：

- `manifest.json`：本轮运行参数，敏感 token 会脱敏。
- `summary.json`：机器可读结论。
- `REPORT.md`：人工报告。
- `runtime/runtime-samples.ndjson`：runtime `capacity/in_flight/heartbeat` 采样。
- `runtime/task-queue-samples.ndjson`：目标任务队列采样。
- `cases/<case_id>/04-*`：upload 元数据与下载验证。
- `cases/<case_id>/05-*`：`/chat` 请求和响应。
- `cases/<case_id>/06-*`：task 轮询过程和最终 payload。
- `cases/<case_id>/07-*`：结果文件下载。
- `cases/<case_id>/08-*`：callback DB/kubectl/日志证据。

## 通过标准

`REPORT.md` 顶部 `verdict=PASS` 才算完整通过。常见非通过含义：

- `FAIL`：功能失败，例如任务未完成、缺文件、callback 没证据。
- `PARTIAL`：任务完成了，但某些强门禁缺失，例如 callback 或 result file 不完整。
- `INCONCLUSIVE`：缺少 DB/kubectl/receiver 证据，无法证明 runtime 并发或 callback 投递。

## 关键阻塞点

- `requires_result_file` 已纳入 public `/chat` 字段说明，内网联调时必须同步接口文档。
- 如果 `GO_GATEWAY_CALLBACK_FIRE_AND_FORGET=true` 或 `GO_GATEWAY_CALLBACK_ACCEPT_ANY_RESPONSE=true`，不能证明 retry/dead-letter。
- 如果 MySQL 采样看到 `in_flight > 0` 且 `last_heartbeat_at IS NULL`，不能作为生产 runtime 槽位证明。
- 如果只看到 20 个 `202 Accepted`，但 `runtime/runtime-samples.ndjson` 没看到 20 个不同助手 `in_flight > 0`，不能证明 20 助手真实并发。
