# Go Gateway

当前目录承载 Go Gateway 到 Python control plane 的网关实现，故意不和仓库里现有的 IronAgent Node gateway 混名。

本文是模块入口速查。northbound 字段语义与错误码以 `../docs/接口契约.md` 为准，callback 事件契约见 `../docs/事件契约.md`，状态机见 `../docs/状态机.md`。

## 边界

- `polling` 只用于验证 Go Gateway 到 Python southbound 的任务链路，不代表 IM 最终对接形态。
- IM 正式对接目标是 `webhook-first + fallback-query-only`。
- 上游 IronAgent / 小熊猫 API 以已提供的接口文档为准；Go Gateway 不用 southbound 内部字段反推上游协议。
- 下游 Python control plane 可以本地启动 IronAgent，并通过配置把 Gateway 指向本地 `security-service` 与 `admin`。
- `claim_token`、`assistant_id`、`source_assistant_id`、`target_assistant_id`、`input_object_id`、`workspace_file_id`、Python southbound URL 都是 Gateway 内部细节，不进入小熊猫协议。
- 文件北向以 HTTP 文件接口为准，不使用 MCP。文件内容不进 MCP、callback 或任务接口。

## 接口清单

- `polling` 验证链路和 callback 状态推送均已接入，`GET /api/v1/tasks/{task_id}` 仍作为 fallback-query。
- public 入口（上游契约，字段语义见 `../docs/接口契约.md`）:
  - `GET /api/v1/health`
  - `POST /api/v1/chat`
  - `GET /api/v1/tasks/{task_id}`
  - `POST /api/v1/tasks/{task_id}/cancel`
  - `GET /api/v1/approvals/{approval_id}`
  - `POST /api/v1/approvals/{approval_id}/decision`
  - `POST /api/v1/files/upload`
  - `GET /api/v1/files/{file_id}`
  - `GET /api/v1/files/{file_id}/download`
- 内部入口（`admin/runtime/debug` scope）:
  - `POST /api/v1/tasks/{task_id}/start`
  - `POST /api/v1/tasks/{task_id}/complete`
  - `POST /api/v1/tasks/{task_id}/fail`
  - `POST /api/v1/tasks/{task_id}/approvals/resolve`
  - `POST /api/v1/runtime/start`
  - `GET /api/v1/runtime`
  - `POST /api/v1/runtime/stop`
  - `GET /api/v1/runtime/registry`
  - `GET /api/v1/runtime/scheduler/last-decision`
  - `GET /api/v1/runtime/registry/{assistant_id}`
  - `PUT /api/v1/runtime/registry/{assistant_id}`
  - `POST /api/v1/runtime/registry/{assistant_id}/heartbeat`
  - `GET /api/v1/files`
  - `POST /api/v1/files/upload-url`
  - `POST /api/v1/files/download-url`
  - `POST /api/v1/files/{file_id}/delete`
  - `PUT /api/v1/files/presigned/upload/{token}`
  - `POST /api/v1/files/sync-results`
  - `GET /api/v1/files/presigned/download/{token}`
  - `POST /api/v1/webhooks/ack`
  - `GET /api/v1/webhooks/dead-letter`
  - `POST /api/v1/webhooks/dead-letter/{event_id}/replay`
  - `GET /api/v1/task-queue`
  - `GET /api/v1/metrics`
  - `GET /api/v1/metrics/prometheus`
  - `/mcp`（MCP streamable HTTP 入口，`admin` scope）
- 健康检查与免鉴权入口:
  - `GET /health`
  - `GET /healthz`
  - `GET /readyz`
  - `GET /api/v1/users/verify`
- 反向代理入口:
  - `GET /metrics`（`debug` scope）
  - `/admin`、`/admin/login`、`/admin/`、`/api/v1/admin/`、`/api/v1/market/`、`/api/v1/workspace-files`（admin 反代）
  - `/api/v1/hooks/`、`/api/v1/security/`、`/api/v1/audit/`、`/api/v1/approvals`、`/api/v1/assistants/`、`/api/v1/control-ui/`（security-service 反代）
  - `/assistants/{assistant_id}/...`（助手 runtime 单入口反代，`proxy` scope）
- southbound:
  - binding resolve
  - workspace file bridge
  - collaboration object create
  - collaboration task claim/start/get/cancel
  - approval query/resolve
  - webhook callback dispatch
  - presigned URL upload/download wrapper

## 环境变量

完整的 `GO_GATEWAY_*` 环境变量字典（含 `GO_GATEWAY_PROCESS_ROLE`、Redis、task worker 并发、callback 等全部变量的类型、默认值与约束）见 `docs/observability-runbook.md` §9，本 README 不再维护重复清单。

默认 southbound 地址：

- security-service: `http://127.0.0.1:8000`
- admin: `http://127.0.0.1:8501`

本仓库本地 IronAgent 开发服务地址：

- security-service: `http://127.0.0.1:8000`
- admin: `http://127.0.0.1:8501`

本地 southbound 联调时应显式设置：

```bash
GO_GATEWAY_SECURITY_BASE_URL=http://127.0.0.1:8000
GO_GATEWAY_ADMIN_BASE_URL=http://127.0.0.1:8501
```

完整步骤见 `../docs/LOCAL_STARTUP.md`。

## 挂载配置

网关启动时会先读取配置文件，再用环境变量覆盖配置文件值。

- 默认配置路径：`/etc/ironagent/go-gateway.json`
- 显式配置路径：`GO_GATEWAY_CONFIG_FILE=/path/to/go-gateway.json`
- 示例配置：`config.example.json`

容器挂载示例：

```bash
docker run --rm \
  -p 8080:8080 \
  -v "$PWD/config.example.json:/etc/ironagent/go-gateway.json:ro" \
  ironagent-go-gateway
```

## 启动

```bash
cd go-gateway
GO_GATEWAY_INTERNAL_SERVICE_TOKEN=change-me \
GO_GATEWAY_SCOPED_API_KEYS='generated-public-key:public;generated-runtime-key:runtime;generated-debug-key:debug;generated-admin-key:admin;generated-proxy-key:proxy' \
GO_GATEWAY_PUBLIC_AUTH_DISABLED=false \
go run ./cmd/server
```

## P7 部署验证工具

```bash
cd go-gateway
go run ./cmd/p7tool runtime-seed -gateway http://127.0.0.1:8080 -api-key runtime-key -client-id p7-runtime-seed -hmac-secret '<hmac-secret>' -count 2000
go run ./cmd/p7tool chat-load -gateway http://127.0.0.1:8080 -tasks 1000 -concurrency 50 -input-object-id '<真实 input_object_id>'
go run ./cmd/p7tool callback-sim -listen :18081 -behavior no_ack
```

完整步骤见 `../docs/go-gateway/p7-deployment-test-runbook.md`。

## Header 约定

- 上游调用 `public` scope 的 northbound 接口默认需要 `X-API-Key`；如果启用 `GO_GATEWAY_HMAC_SECRET`，还需要 `X-Client-ID`、`X-Timestamp`、`X-Nonce` 与 `X-Signature`。
- `admin/runtime/debug/proxy` 接口不是上游接口，仍必须携带对应 scope 的 `X-API-Key`；如果启用 `GO_GATEWAY_HMAC_SECRET`，这些内部接口还需要 `X-Client-ID`、`X-Timestamp`、`X-Nonce` 与 `X-Signature`。
- `GO_GATEWAY_PUBLIC_AUTH_DISABLED` 默认关闭；如需兼容旧联调环境，可临时显式设置为 `true`。
- 旧配置 `GO_GATEWAY_API_KEYS` 仍视为 `*` 全权限，用于兼容现有联调。
- 新配置 `GO_GATEWAY_SCOPED_API_KEYS` 使用 `key:scope[,scope];key2:scope` 格式，支持 `public/admin/runtime/debug/proxy`。
- `debug` scope 可访问 `/metrics`、`/api/v1/metrics/prometheus`、`/api/v1/task-queue`、`/api/v1/runtime/scheduler/last-decision` 等观察接口。
- `runtime` scope 可访问 Runtime Registry 与 heartbeat 接口。
- `admin` scope 可访问 runtime start/stop、内部 task start/complete/fail、审批 resolve、dead-letter replay 等控制接口。

内部 HMAC 签名串：

```text
<client_id>.<timestamp>.<nonce>.<method>.<request_uri>.<hex_sha256(raw-request-body)>
```

## 行为要点

northbound 请求/响应字段、错误码与 callback body 的契约语义以 `../docs/接口契约.md`、`../docs/事件契约.md` 为准，此处只保留 Gateway 自身行为要点：

- `POST /api/v1/chat` 当前是“协作任务创建入口”，不是通用聊天协议全集；正式外部字段以 `../docs/接口契约.md` §7 为准。
- `request_id` 是 public northbound 必填字段并作为幂等键；内部兼容 `idempotency_key`。
- `files[]` 中的 `file_id` 会在接收任务前同步校验，不存在返回 `422`，不会先创建异步任务。
- 若没有 `input_object_id`，网关会基于 `files[]`、`workspace_file` 或纯 `message` 注册 shared object；多文件当前先取首个文件作为任务输入对象。
- 默认模式下，`POST /api/v1/chat` 会在任务进入 `pending_claim` 后立刻执行 `claim -> start`，响应只返回 `task_id`、`status`、`stage`、`created_at`。
- 设置 `GO_GATEWAY_TASK_WORKER_ENABLED=true` 后，`POST /api/v1/chat` 只做鉴权、幂等、入库和入队，立刻返回 Gateway task id 与 `processing/queued`；后台 task worker 再领取队列并执行 object create、task create、claim、start。
- 设置 `GO_GATEWAY_RUNTIME_EXECUTION_ENABLED=true` 后，task worker 会在 `claim -> start` 后通过目标助手 runtime 的 IronAgent WebSocket 执行 `chat.send`，并把 southbound task 闭环为 `completed` 或 `failed`。`GO_GATEWAY_RUNTIME_EXECUTION_TIMEOUT` 控制未显式传 `timeout_seconds` 时的单次执行上限，默认 10 分钟；未显式传 `timeout_seconds` 的任务整体还受 `GO_GATEWAY_TASK_WORKER_GATEWAY_DEFAULT_DEADLINE_BACKSTOP`（默认 20 分钟）兜底，详见 `docs/observability-runbook.md`。如果关闭 runtime 执行开关，任务只推进到 `running`，由外部 runtime 自行回调完成。
- `GO_GATEWAY_TASK_QUEUE_MAX_PENDING` 大于 0 时启用队列背压，`queued + processing + retry` 达到阈值后，新任务返回 `429 queue_full`。
- queued/retry/processing 中尚未写入 southbound id 的任务可以通过 `POST /api/v1/tasks/{task_id}/cancel` 取消，worker 不再领取已取消任务；若取消与执行并发发生，worker 保存结果前会再次检查并回滚为 cancelled。
- worker 执行 queued 任务后会按任务状态变化补发 callback 状态推送。
- `GET /api/v1/task-queue` 返回 `queued/processing/retry/failed/completed/cancelled/active/max_pending` 计数，用于观察队列积压；`GET /api/v1/metrics` 返回 task queue 与 runtime registry 的基础 JSON metrics。
- `/metrics` 与 `/api/v1/metrics/prometheus` 返回 Prometheus text format 指标，当前覆盖 health、task queue、runtime registry、scheduler latency、southbound latency/error、webhook lifecycle、file transfer bytes。
- `/healthz` 只表示进程存活；`/api/v1/health` 返回真实 component health；`/readyz` 基于真实 health，不 ready 时返回 `503`。
- Gateway 只根据 `user_id` 查找已有用户绑定助手；不存在时返回 `400 {"error":{"code":"bad_request","message":"用户无可用助手，请在后台重新创建助手后再继续调用"}}`。上游不需要也不允许传 `assistant_id` 或 `target_assistant_id`；用户与助手的创建和绑定必须在创建助手流程完成。
- Runtime Registry 支持 MySQL 持久化和 heartbeat；超过 `GO_GATEWAY_RUNTIME_HEARTBEAT_TTL` 未心跳会自动标记为 `unhealthy`，`drain_mode=true` 的 runtime 不接新任务。
- 助手 WebUI/Control UI 走 `/assistants/{assistant_id}/...` 单入口时，Gateway 会在合法 launch ticket 解析成功后用 ticket 中的 runtime 地址懒注册该 assistant；因此新增助手不需要为浏览器入口额外开放端口，也不需要写入 bootstrap 列表。
- 测试环境如没有 runtime sidecar 心跳，且需要在打开助手 UI 前让 `/readyz` 或 scheduler 先看到 runtime，可设置 `GO_GATEWAY_RUNTIME_DISCOVER_ASSISTANTS=true`，Gateway 会按 `GO_GATEWAY_RUNTIME_SYNC_INTERVAL` 从 Python control plane 的 assistant context 发现运行中 assistant 并同步真实 runtime 状态。生产仍建议使用 runtime heartbeat + MySQL 持久化。
- `GO_GATEWAY_RUNTIME_BOOTSTRAP_ASSISTANTS` 仍可作为本地或手工验证兜底列表；列表只能包含真实存在且 runtime 为 `running` 的 assistant，不应作为生产运行时注册主机制。
- `GET /api/v1/tasks/{task_id}` 已收敛为 fallback-query 外部响应，不返回 `claim_token`、`source_assistant_id`、`target_assistant_id`、`input_object_id`、`fallback_query_url`、`created_at`、`updated_at` 等内部字段。
- `GET /api/v1/tasks/{task_id}` 与 callback body 保持一致，返回 camelCase 业务字段（字段语义见 `../docs/接口契约.md` §8）；只有存在真实结果文件且文件元数据可查询、file_id 与用户归属一致时才返回 `files[]`，结果文件会在 `files[].download_url` 中返回可直接下载的 Gateway 地址，地址包含 `user_id` query。
- `POST /api/v1/tasks/{task_id}/start` 仍保留为内部验证入口，不会把 `claim_token` 暴露给 northbound。
- approval-level facade 已提供 `GET /api/v1/approvals/{approval_id}` 和 `POST /api/v1/approvals/{approval_id}/decision`，对外 `approve/deny` 映射到内部 `allow/deny`。
- 上游文件上传下载以直接文件接口为准：`POST /api/v1/files/upload`、`GET /api/v1/files/{file_id}`、`GET /api/v1/files/{file_id}/download`（请求字段见 `../docs/接口契约.md` §4–§6）。Gateway 根据 `user_id` 路由到绑定助手 workspace 的 `temp/` 目录。
- MCP 文件工具只保留为兼容或内部入口，不属于上游文件上传下载协议。
- 文件内容不通过 MCP payload、callback 或任务接口传输。
- Gateway 使用 `GO_GATEWAY_DEFAULT_CALLBACK_URL` 投递状态，不从 `POST /api/v1/chat` 请求体读取 `callback_url`。
- `GO_GATEWAY_DEFAULT_CALLBACK_URL` 的 host 必须配置在 `GO_GATEWAY_CALLBACK_ALLOWED_HOSTS`，且默认拒绝 localhost、私网和 link-local IP；本地联调才应显式开启 `GO_GATEWAY_CALLBACK_ALLOW_PRIVATE_HOSTS=true`。
- callback 请求 body 按小熊猫接口契约组织（字段见 `../docs/接口契约.md` §11 与 `../docs/事件契约.md`）；Gateway 不合成测试文件，没有真实结果文件时不返回 `files[]`。Gateway 不要求上游返回 ACK body，默认 HTTP 2xx 即视为本次推送成功。`GO_GATEWAY_CALLBACK_FIRE_AND_FORGET=true` 时，Gateway 写出 POST 请求后立即视为已投递，不再等待或校验接收端响应。业务请求只负责写入 outbox，worker 后台 claim 带租约的 pending 事件并投递；默认投递模式下，失败会记录推送时间、失败原因、HTTP 状态码和失败详情，并按指数退避加 jitter 重试，超限进入 dead-letter，可通过 replay 接口重放。上游长时间未收到 callback 时，使用 `GET /api/v1/tasks/{task_id}` 做 fallback 查询。
- approval allow 后会继续推进 `claim -> start`；deny 后对外状态为 `rejected`。
- 直接上传和 presigned 上传默认最大 `50MiB`，可通过 `GO_GATEWAY_MAX_UPLOAD_BYTES` 调整。
- `approval-resume` 被视为一期必需闭环，因此配置层强制要求 `GO_GATEWAY_INTERNAL_SERVICE_TOKEN`。
- 配置 `GO_GATEWAY_DATABASE_URL` 后会启用 MySQL 版 Gateway task store、task queue、webhook outbox 和 runtime registry；未配置时使用进程内 store，仅适合本地联调。
- 启用 MySQL store 前需要先执行仓库 Alembic 迁移，确保 `gateway_tasks`、`gateway_task_queue`、`gateway_task_events`、`gateway_idempotency_keys`、`gateway_event_outbox`、`gateway_event_sequences`、`gateway_webhook_deliveries`、`gateway_dead_letters`、`gateway_runtimes`、`gateway_runtime_heartbeats` 等表已创建。
