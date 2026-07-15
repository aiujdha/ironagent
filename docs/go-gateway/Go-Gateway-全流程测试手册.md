# Go Gateway End-to-End Test Runbook

更新日期：2026-06-11

本文是测试执行总纲，面向功能测试、运维测试和架构验收。它覆盖 Go Gateway、后台管理、security-service、IronAgent runtime、workspace 文件链路和 Chat 任务链路。

更细的接口和压测细节见：

- `docs/go-gateway/integration-and-production-deployment.md`
- `docs/go-gateway/p7-deployment-test-runbook.md`

原英文测试计划已归档到 `docs/archive/historical/full-flow-test-plan.md`，其中仍有效的安全对抗与 webhook 故障注入用例已并入本手册 §12、§13。

## 1. 测试目标

验证一套测试环境是否具备以下能力：

- Gateway 入口可用。
- HMAC/API key 鉴权正确。
- Python southbound admin/security 可达。
- Runtime registry 至少有一个 ready runtime。
- Workspace file upload/list/download 正常。
- Chat 请求能创建 shared object、创建 collaboration task、claim、start。
- fallback query 模型不泄露内部字段。
- metrics 能反映 runtime 与 task queue 状态。
- 常见部署回归问题能被预检发现。

## 2. 测试环境

| 项 | 要求 |
| --- | --- |
| Kubernetes | 单 Gateway `prod-nodeport` 测试环境 |
| Namespace | `ironagent` |
| Go Gateway | NodePort `30808` 或等价测试入口 |
| Admin | NodePort `30880` 或内网入口 |
| IronAgent WebUI | NodePort `30789` |
| MySQL | 已配置 `IRON_AGENT_DATABASE_URL` / `GO_GATEWAY_DATABASE_URL` |
| Secret | 已创建真实 `go-gateway-secret` |
| Runtime | 至少一个 assistant runtime 为 `running` |

变量示例：

```bash
export NS=ironagent
export NODE_PUBLIC_IP=<node-public-ip>
export GO_GATEWAY_BASE_URL="http://${NODE_PUBLIC_IP}:30808"
export ADMIN_BASE_URL="http://${NODE_PUBLIC_IP}:30880"
export IRON_AGENT_BASE_URL="http://${NODE_PUBLIC_IP}:30789"
export K="kubectl --kubeconfig=/etc/kubernetes/admin.conf"
```

## 3. 准入检查

### 3.1 Pod 状态

```bash
$K -n ${NS} get pods -o wide
$K -n ${NS} get svc -o wide
```

验收：

- `admin` 为 `Running`。
- `security-service` 为 `Running`。
- `ironagent-gateway` 为 `Running`。
- `go-gateway` 为 `Running`。
- 所有核心 Pod `READY=1/1`。

### 3.2 入口连通性

```bash
curl -i "${GO_GATEWAY_BASE_URL}/healthz"
curl -i "${GO_GATEWAY_BASE_URL}/readyz"
curl -i "${ADMIN_BASE_URL}/"
curl -i "${IRON_AGENT_BASE_URL}/"
```

验收：

- `/healthz` 返回 200。
- `/readyz` 返回 200。
- `readyz.details.container_scheduler.message` 包含 `registered=1 ready=1` 或更高。
- Admin 页面可打开。
- IronAgent 页面可打开。

## 4. 数据库 schema 回归测试

执行：

```bash
$K -n ${NS} exec deploy/admin -- sh -lc '
  cd /app &&
  .venv/bin/python - <<'"'"'PY'"'"'
from src.shared.config import get_settings
from sqlalchemy import create_engine, text

engine = create_engine(str(get_settings().database_url))
checks = {
    "gateway_tasks": [
        "client_id",
        "status",
        "stage",
        "fallback_query_url",
        "metadata_json",
    ],
    "workspace_files": ["storage_provider", "checksum"],
    "external_push_requests": ["storage_backend", "storage_key", "storage_url"],
}

with engine.connect() as conn:
    print("alembic_version", conn.execute(text("SELECT version_num FROM alembic_version")).scalar())
    for table, columns in checks.items():
        found = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME=:table"
                ),
                {"table": table},
            )
        }
        missing = [column for column in columns if column not in found]
        print(table, "ok" if not missing else "missing=" + ",".join(missing))
PY
'
```

验收：

- `alembic_version` 至少到 `027_gateway_queue_capacity_wait_state`（当前 head）。
- 三张表检查全部 `ok`。

风险点：

- 如果 admin 镜像缺少 migrations，说明镜像构建不完整。
- 如果旧表缺字段，重启服务不会修复，必须执行 migration。

## 5. runtime 路径回归测试

执行：

```bash
$K -n ${NS} exec deploy/admin -- sh -lc '
  cd /app &&
  .venv/bin/python - <<'"'"'PY'"'"'
from src.assistants.service import AssistantService

service = AssistantService()
for item in service.list_assistants():
    runtime = item.runtime
    if not runtime:
        continue
    print(item.assistant_id, runtime.status, runtime.workspace_root, runtime.state_root)
PY
'
```

验收：

- 要测试的 assistant runtime status 为 `running`。
- `workspace_root/state_root` 不包含 `/Users/`。
- `workspace_root/state_root` 不指向旧部署目录。
- 目标工作区下有 `persistent/` 和 `temp/`。

重点：

- `TypeError: Failed to fetch` 经常不是前端问题，而是旧路径、CSP、public base URL 或旧库字段共同导致。
- 重新部署后第一件事不是刷新页面，而是先看 runtime 路径和 `/readyz`。

## 6. 鉴权测试

获取 HMAC secret：

```bash
HMAC_SECRET=$($K -n ${NS} get secret go-gateway-secret \
  -o jsonpath='{.data.GO_GATEWAY_HMAC_SECRET}' | base64 -d)
```

合法请求：

```bash
scripts/go-gateway/hmac_request.py \
  --url "${GO_GATEWAY_BASE_URL}" \
  --method GET \
  --path /readyz \
  --api-key public-key \
  --client-id e2e-client \
  --secret "${HMAC_SECRET}"
```

测试点：

| 用例 | 操作 | 预期 |
| --- | --- | --- |
| 无 API key | 不传 `X-API-Key` | 401 |
| 错 API key | 使用不存在 key | 401 |
| 错 scope | 用 `admin-key` 调 public 路由 | 403 或明确 scope 错误 |
| 缺 HMAC header | 缺 `X-Nonce` 等 | 401 |
| nonce 重放 | 重用同一 nonce | 401 |
| 合法 HMAC | 使用工具生成签名 | 2xx |

重点：

- 不要把 HMAC secret 粘到文档、截图、测试报告。
- scope 不继承，按路由选择正确 key。

## 7. 文件链路测试

### 7.1 申请 upload URL

```bash
BODY='{
  "assistant_id": "demo-main",
  "target_bucket": "persistent",
  "filename": "e2e-workspace-file.txt",
  "content_type": "text/plain",
  "source_type": "manual-e2e",
  "source_ref": "gateway-e2e",
  "uploader_user_id": "e2e-user"
}'

scripts/go-gateway/hmac_request.py \
  --url "${GO_GATEWAY_BASE_URL}" \
  --method POST \
  --path /api/v1/files/upload-url \
  --api-key public-key \
  --client-id e2e-client \
  --secret "${HMAC_SECRET}" \
  --json "${BODY}"
```

预期：

- 返回 `url/method/expires_at`。
- `method=PUT`。

### 7.2 上传文件

```bash
curl -i -X PUT '<upload_url>' \
  -H 'Content-Type: text/plain' \
  --data-binary 'gateway e2e file content'
```

预期：

- HTTP 200。
- 返回 `file_id/status/filename`。

### 7.3 列出 workspace 文件

```bash
scripts/go-gateway/hmac_request.py \
  --url "${GO_GATEWAY_BASE_URL}" \
  --method GET \
  --path '/api/v1/files?assistant_id=demo-main&bucket=persistent' \
  --api-key public-key \
  --client-id e2e-client \
  --secret "${HMAC_SECRET}"
```

预期：

- 列表包含刚上传的 `file_id`。
- `total >= 1`。
- 页面工作空间卡片只展示该 assistant 的 `persistent/`、`temp/` 和必要辅助文件，不应展示整个宿主机工作区所有文件。

### 7.4 下载文件

```bash
BODY='{"file_id":"<file_id>","disposition":"attachment"}'

scripts/go-gateway/hmac_request.py \
  --url "${GO_GATEWAY_BASE_URL}" \
  --method POST \
  --path /api/v1/files/download-url \
  --api-key public-key \
  --client-id e2e-client \
  --secret "${HMAC_SECRET}" \
  --json "${BODY}"

curl -i '<download_url>'
```

预期：

- HTTP 200。
- 下载内容等于上传内容。

风险点：

- 如果上传返回 500 且日志有 `storage_provider` 或 `checksum`，先修 DB migration。
- 文件内容不应进入 Chat 请求体、MCP payload 或 callback body。

## 8. Chat 主链路测试

使用上一步 `file_id` 发起 Chat。

公开请求体只接受白名单字段：`request_id`、`user_id`、`conversation_id`、`message`、`files`、`requires_result_file`、`timeout_seconds`；出现其他字段（如旧版的 `client_id`、`source_assistant_id`、`target_assistant_id`、`workspace_file`、`callback_url`）会被拒绝，返回 400 `不支持的请求字段`。callback 地址不由请求传入，统一由 `GO_GATEWAY_DEFAULT_CALLBACK_URL` 配置。

```bash
BODY='{
  "request_id": "e2e-chat-001",
  "user_id": "e2e-user",
  "conversation_id": "e2e-conversation",
  "message": "Read the attached workspace file and return a short acknowledgement.",
  "files": ["<file_id>"]
}'

scripts/go-gateway/hmac_request.py \
  --url "${GO_GATEWAY_BASE_URL}" \
  --method POST \
  --path /api/v1/chat \
  --api-key public-key \
  --client-id e2e-client \
  --secret "${HMAC_SECRET}" \
  --json "${BODY}"
```

预期（HTTP 202）：

```json
{
  "task_id": "...",
  "status": "processing",
  "stage": "queued",
  "created_at": "..."
}
```

注意：响应不再返回 `fallback_query_url`，该 URL 只出现在 callback 事件里。

查询：

```bash
scripts/go-gateway/hmac_request.py \
  --url "${GO_GATEWAY_BASE_URL}" \
  --method GET \
  --path "/api/v1/tasks/<task_id>" \
  --api-key public-key \
  --client-id e2e-client \
  --secret "${HMAC_SECRET}"
```

预期：

- 返回外部 task 模型（camelCase：`taskId`、`status`、`stage`、`replyText` 等）。
- 不返回 `claim_token`。
- 不返回 `source_assistant_id`。
- 不返回 `target_assistant_id`。
- 不返回 `input_object_id`。

当前阶段判定：

- 如果状态到 `processing/tool_running`，说明 Gateway 已完成 `shared object -> collaboration task -> claim -> start`。
- runtime 执行已闭环：Gateway 通过 WebSocket 驱动 runtime 执行，任务完成后会自动进入 `completed/done` 并推送 callback。长时间停留在 `processing` 时优先检查 runtime 状态与 Gateway/runtime 日志。

清理：

```bash
BODY='{"actor_id":"e2e-client","reason":"cleanup after e2e test"}'

scripts/go-gateway/hmac_request.py \
  --url "${GO_GATEWAY_BASE_URL}" \
  --method POST \
  --path "/api/v1/tasks/<task_id>/cancel" \
  --api-key public-key \
  --client-id e2e-client \
  --secret "${HMAC_SECRET}" \
  --json "${BODY}"
```

## 9. 前端工作空间测试

在浏览器打开：

```text
http://<node-public-ip>:30789/chat?session=main
```

测试点：

| 用例 | 操作 | 预期 |
| --- | --- | --- |
| 首屏加载 | 打开聊天页 | 不出现 `TypeError: Failed to fetch` |
| 工作空间刷新 | 点击工作空间卡片刷新按钮 | 浏览器 Network 有新的 workspace/files 请求 |
| persistent 展示 | 展开 `persistent/` | 仅显示该 assistant persistent 文件 |
| temp 展示 | 展开 `temp/` | 即使为空也展示 `temp/` 文件夹 |
| 文件预览 | 点击预览按钮 | 能打开文件内容或预览弹层 |
| 文件下载 | 点击下载按钮 | 文件下载成功且内容正确 |
| 附件上传 | 对话框添加附件并发送 | 附件落到 `persistent/`，列表刷新可见 |

浏览器控制台重点看：

- 是否有 CSP 拒绝请求。
- 请求 URL 是否指向正确的 Gateway public base URL。
- 是否错误请求 admin NodePort 或旧 IP。
- 点击刷新按钮是否真的发出请求。

## 10. Metrics 测试

```bash
scripts/go-gateway/hmac_request.py \
  --url "${GO_GATEWAY_BASE_URL}" \
  --method GET \
  --path /metrics \
  --api-key debug-key \
  --client-id e2e-client \
  --secret "${HMAC_SECRET}" | rg 'go_gateway_'
```

验收：

- `go_gateway_runtime_registered_total >= 1`。
- `go_gateway_runtime_ready_total >= 1`。
- `go_gateway_task_queue_active` 不持续增长。
- Chat 测试后 task queue 指标有变化。

## 11. 日志测试

```bash
$K -n ${NS} logs deploy/go-gateway --since=10m --tail=200
$K -n ${NS} logs deploy/security-service --since=10m --tail=200
$K -n ${NS} logs deploy/admin --since=10m --tail=200
$K -n ${NS} logs deploy/ironagent-gateway --since=10m --tail=200
```

不应出现：

- 持续 `runtime sync failed`。
- 持续 assistant 404。
- `storage_provider` 缺默认值。
- `checksum` 缺列。
- `metadata_json` 缺列。
- 大量 `No API key found`，除非当前测试 assistant 明确未配置模型密钥。

## 12. 安全对抗测试（本地/独立实例，选做）

本组用例并入自已归档的英文测试计划，用于验证鉴权与 callback 防护边界。私网 callback、DNS rebind 用例需要修改 Gateway callback 配置后重启，建议在本地或独立测试实例执行。临时强约束配置示例：

```bash
GO_GATEWAY_HMAC_SECRET=dev-hmac-secret
GO_GATEWAY_MAX_UPLOAD_BYTES=1024
GO_GATEWAY_DEFAULT_CALLBACK_URL=<被测 callback 地址>
GO_GATEWAY_CALLBACK_ALLOWED_HOSTS=callback.example.com
GO_GATEWAY_CALLBACK_ALLOW_PRIVATE_HOSTS=false
```

HMAC 签名串格式：

```text
<client_id>.<timestamp>.<nonce>.<method>.<request_uri>.<hex_sha256(raw-request-body)>
```

对抗用例与预期：

| 用例 | 攻击方式 | 预期 |
| --- | --- | --- |
| 大 body | HMAC 请求体超过 auth cap | `413 request body too large` |
| presigned upload 超限 | 对 presigned PUT 上传超过 `GO_GATEWAY_MAX_UPLOAD_BYTES` | `413 上传文件过大` |
| ACK 缺少 task_id | `POST /api/v1/webhooks/ack` 只传 `event_id` | `400 unknown event_id` |
| 私网 callback | `GO_GATEWAY_DEFAULT_CALLBACK_URL` 指向私网/loopback 且禁止 private hosts | `POST /api/v1/chat` 返回 `400 callback_url host must not be private or loopback` |
| DNS rebind callback | allowlist 域名解析到私网/loopback/link-local | `400 callback_url host resolves to private or loopback address` |
| fallback URL 污染 | 注入 `X-Forwarded-Host: evil.example.com` | callback 事件中的 fallback URL 只来自 `GO_GATEWAY_PUBLIC_BASE_URL`，不反射请求 Host |

注意：

- callback 地址不由 `/api/v1/chat` 请求体传入，统一由 `GO_GATEWAY_DEFAULT_CALLBACK_URL` 配置。
- nonce 重放用例见 §6 鉴权测试。

## 13. Webhook 故障注入测试（本地/可控 callback，选做）

需要可控的 callback 接收端，建议在本地联调环境执行（Gateway `:8080`，API key `dev-key`，callback 收在 `:18081`）。

### 13.1 500 注入

```bash
cd <repo-root>/go-gateway
go run ./cmd/p7tool callback-sim -listen :18081 -behavior status_500
```

验收：

- Gateway 不阻塞 `POST /api/v1/chat`。
- `go_gateway_webhook_events_total{result="failed_attempt"}` 增长。
- 后续进入 retry。
- 达到最大尝试后进入 dead-letter。

### 13.2 查看 dead-letter

```bash
curl -sS -H 'X-API-Key: dev-key' \
  http://127.0.0.1:8080/api/v1/webhooks/dead-letter | python3 -m json.tool
```

预期：

```json
{
  "dead_letters": [
    {
      "event": {
        "event_id": "<event_id>",
        "task_id": "<task_id>"
      },
      "status": "dead",
      "last_error": "..."
    }
  ]
}
```

### 13.3 replay

```bash
curl -sS -X POST http://127.0.0.1:8080/api/v1/webhooks/dead-letter/<event_id>/replay \
  -H 'X-API-Key: dev-key' | python3 -m json.tool
```

预期：

- 返回 `event` 与 `status`，`status` 回到 `pending`。
- 修复 callback 接收端（`-behavior ack`）后事件可重新投递成功。

## 14. 测试结论模板

```text
测试日期：
测试环境：
Gateway commit/tag：
Control plane commit/tag：
IronAgent commit/tag：

准入检查：通过 / 不通过
数据库 schema：通过 / 不通过
runtime 路径：通过 / 不通过
鉴权：通过 / 不通过
文件上传：通过 / 不通过
文件列表：通过 / 不通过
文件下载：通过 / 不通过
Chat 编排：通过 / 不通过
前端工作空间：通过 / 不通过
Metrics：通过 / 不通过
日志检查：通过 / 不通过
安全对抗（选做）：通过 / 不通过 / 未执行
Webhook 故障注入（选做）：通过 / 不通过 / 未执行

遗留问题：
1.
2.

结论：
可继续联调 / 不可继续联调
```

## 15. 当前已知隐患

| 隐患 | 严重性 | 说明 | 建议 |
| --- | --- | --- | --- |
| bootstrap 不是生产机制 | 高 | `GO_GATEWAY_RUNTIME_BOOTSTRAP_ASSISTANTS` 只适合小规模测试 | 生产使用 runtime heartbeat |
| NodePort 无 TLS | 中高 | 测试入口可用，不适合生产公网 | 生产切 Ingress/API Gateway/TLS |
| 多 Gateway nonce 防重放 | 中 | 启用 `GO_GATEWAY_DATABASE_URL` 后 nonce 写入 MySQL；未启用数据库时仍是单实例内存窗口 | 多副本验证前必须执行 `014_gateway_hmac_nonces` 迁移并开启数据库配置 |
| 旧库兼容风险 | 中 | 长期复用测试库会积累历史 schema | 每次部署前跑 schema 预检 |
| workspace 前端依赖 public base URL | 中 | URL 或 CSP 错会导致 `Failed to fetch` | 部署后必须做浏览器 Network 验证 |
