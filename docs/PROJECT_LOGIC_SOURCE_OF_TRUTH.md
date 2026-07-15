# 项目逻辑事实源

本文档用于统一小熊猫、IronAgent、Go Gateway、HTTP 文件接口、callback、fallback 的事实口径。后续讨论、开发、测试、部署文档如果与本文冲突，先以本文为准；如果方向变化，必须先更新本文，再改代码和其他文档。

## 1. 系统边界

| 组件 | 职责 | 不做什么 |
| --- | --- | --- |
| 小熊猫 | IM 入口、用户交互、接收 Gateway callback、必要时 fallback 查询任务状态。 | 不理解 IronAgent collaboration task、claim token、workspace 内部路径；不需要向 Gateway 返回业务 ACK。 |
| IronAgent | 上游业务系统与 IM 对接侧的协议边界，按接口文档提交任务、文件引用、用户身份和 callback 地址。 | 不直接访问 Python southbound，也不直接操作 assistant runtime。 |
| Go Gateway | 唯一外部入口，负责用户绑定解析、任务编排、runtime 路由、HTTP 文件接口、callback 事件投递、fallback 查询。 | 不要求上游携带 API Key/HMAC，不暴露 Python southbound URL，不把内部 task/claim/object 字段泄漏给上游。 |
| IronAgent Python control plane | 下游权威控制面，提供绑定、workspace files、collaboration objects/tasks、approval、assistant runtime 生命周期。 | 不直接面向小熊猫/IronAgent 公网调用。 |
| MCP | 兼容或内部文件管理工具入口，提供 upload_url、download_url、get、list、delete。 | 不作为上游文件上传下载主接口，不传文件字节，不传 base64，不承载任务状态事件。 |

## 2. 当前阶段路线

当前路线分两层推进，两层均已落地：

1. 第一层：Go Gateway 到 Python southbound 的最小闭环已跑通。
2. 第二层：Webhook Event Dispatcher 已落地（`go-gateway/internal/webhook/dispatcher.go`），callback outbox worker 默认开启（`GO_GATEWAY_CALLBACK_OUTBOX_WORKER_ENABLED` 默认 `true`），用于对接小熊猫。

`polling` 在一期只用于验证 southbound 链路，不代表 IM 最终对接形态。正式 IM 对接目标是 `webhook-first + fallback-query-only`。

## 3. 外部协议口径

上游只能看到这些概念：

- `user_id`
- `employee_no`
- `conversation_id`
- `request_id`
- `message`
- `files[]`
- `task_id`
- `status`
- `stage`
- `reply_text`
- `fallback_query_url`

说明：callback 地址不由 chat 请求传入。`POST /api/v1/chat` 请求体不含 `callback_url` 字段；callback 地址由 Gateway 部署配置 `GO_GATEWAY_DEFAULT_CALLBACK_URL`（配置文件键 `default_callback_url`）统一提供，详见 `docs/callback_url配置.md`。

上游不能看到这些内部概念：

- `claim_token`
- `source_assistant_id`
- `target_assistant_id`
- `input_object_id`
- `workspace_file_id`
- `collaboration object`
- Python southbound URL
- runtime workspace 真实路径

## 4. 文件事实口径

文件链路采用两层模型：

| 层级 | 事实口径 |
| --- | --- |
| 北向 | HTTP 文件接口：`POST /api/v1/files/upload`、`GET /api/v1/files/{file_id}`、`GET /api/v1/files/{file_id}/download`。 |
| 南向 | Go Gateway 复用 Python control plane 的 workspace-files HTTP 接口。 |
| 字节流 | 上游直接通过 HTTP 文件接口上传下载；Gateway 内部可继续使用 presigned URL 封装 southbound 字节流。 |

禁止把文件内容放进 callback body、任务接口 JSON 或日志。

助手工作空间事实：

- 新建助手时必须创建该助手对应的工作空间命名空间。
- 页面工作空间卡片只展示当前助手的 `persistent/` 和 `temp/`。
- 即使两个目录为空，也必须显示目录入口。
- 用户上传附件默认进入 `temp/`；大模型扫描、搜索或读取工作区文件时必须先看 `temp/`，找不到再看 `persistent/`；只有显式选择或明确要求长期保留时才进入 `persistent/`。
- 助手删除或覆盖 `persistent/` 文件时，应进入审批流程。
- 删除通过后，对象存储、workspace file 元数据和页面展示必须同步。
- 不能把整个项目目录或宿主机目录扫描结果展示给当前助手。

## 5. Callback 与 fallback

callback 是正式 IM 对接的主路径：

- Gateway 主动向部署配置的 callback 地址（`GO_GATEWAY_DEFAULT_CALLBACK_URL`）推送任务事件。
- Gateway 只判断本次 HTTP 推送是否成功，HTTP 2xx 即视为推送成功。
- 上游不需要返回 ACK body。
- Gateway 必须记录 callback 推送时间、成功/失败状态、失败原因、HTTP 状态码和失败详情。
- Gateway 必须支持 retry 和 dead-letter，用于运维排查和补偿，不作为上游消费成功的业务确认。
- 事件必须可幂等消费。

fallback 是补偿查询路径：

- `GET /api/v1/tasks/{task_id}` 只用于 callback 失败、超时、乱序、死信、人工排查后的查询。
- fallback 不能重新变成长期 polling 主链路。
- `fallback_query_url` 生产环境必须基于可信 `GO_GATEWAY_PUBLIC_BASE_URL` 生成。

## 6. 鉴权事实口径

小熊猫/IronAgent 到 Go Gateway：

- 调用 northbound public 接口不需要 `X-API-Key`。
- 不需要 `X-Client-ID`、`X-Timestamp`、`X-Nonce`、`X-Signature`。
- `admin`、`runtime`、`debug`、`proxy` scope 不是上游接口，仍保留内部鉴权。

Go Gateway 到 Python southbound：

- 由 Gateway 注入 `X-Admin-Token`。
- 上游不得持有或直连 Python southbound admin token。

Go Gateway 到小熊猫 callback：

- callback host 必须 allowlist。
- 生产默认拒绝 localhost、私网和 link-local callback 地址。
- Gateway 不要求小熊猫返回 ACK；如小熊猫长时间未收到 callback，应调用 fallback 查询接口校准任务状态。

## 7. 状态事实口径

对外只暴露统一状态模型，不能直接暴露 Python collaboration task 内部状态。

外部状态只使用：

- `processing`
- `pending_approval`
- `completed`
- `rejected`
- `failed`
- `timeout`
- `cancelled`

外部阶段只使用：

- `queued`
- `thinking`
- `tool_running`
- `uploading`
- `downloading`
- `waiting_approval`
- `finalizing`
- `done`
- `error`
- `blocked`

callback 与 fallback 必须复用同一套状态映射。

## 8. 部署事实口径

测试环境可以使用：

- 单 Gateway 实例。
- NodePort 临时入口。
- 多入口临时验证，例如 `ironagent-gateway:30789/30790` 加 `go-gateway:30808`。
- runtime bootstrap 验证小规模链路。
- 本地或测试 MySQL。

当前限制：

- 现阶段 Go Gateway 已开始补 Runtime Proxy：`/assistants/{assistant_id}/api/*` 和 `/assistants/{assistant_id}/ui/*` 已作为 HTTP proxy 最小闭环推进；UI launch ticket/session 已完成最小闭环；管理台在配置 `IRON_AGENT_GO_GATEWAY_PUBLIC_URL` 后可优先生成 Go Gateway 单端口 UI 入口；单入口 overlay 已演进为 `prod-k8s-single-entry-nodeport`（只暴露 Go Gateway `30808`，K8s runtime 形态）；Runtime Proxy 已开启长连接即时 flush，但助手 WS/SSE 仍需真实运行时专项验证。
- 当前 Go Gateway 已统一 northbound 任务、文件管理、callback/fallback、runtime registry 等控制面能力。
- 当前 assistant runtime 的原生 IronAgent Web/UI 仍需继续验证 WS/SSE 和部署入口收口，测试环境仍可能依赖 IronAgent gateway、runtime `gateway_url/gateway_port` 或部署层 Ingress/NodePort。
- “只开放一个端口即可访问所有助手/功能”已于 2026-05-07 通过全流程验收（仅 `go-gateway` 暴露 `30808`，见 `docs/archive/historical/ironagent-full-flow-test-report-2026-05-07.zh-CN.md`），不再是目标态；当前推荐 K8s 部署入口为 `deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport`。

任务队列与 Redis 事实口径：

- 任务队列落在 MySQL task store：worker 以 claim/lease 方式认领排队任务，积压上限由 `GO_GATEWAY_TASK_QUEUE_MAX_PENDING`、`GO_GATEWAY_TASK_QUEUE_MAX_PENDING_PER_ASSISTANT` 控制。
- Redis 不承载任务队列，承载缓存与限流：用户到 assistant 解析缓存、assistant 可用性缓存、task progress 状态、chat 限流计数。
- Redis 支持 `standalone` 与 `sentinel` 两种模式（`GO_GATEWAY_REDIS_MODE`）；sentinel 模式用于 HA，必须配置 `GO_GATEWAY_REDIS_SENTINEL_ADDRS` 与 `GO_GATEWAY_REDIS_SENTINEL_MASTER`。
- `GO_GATEWAY_REDIS_REQUIRED=true` 时 Redis 必须可用；chat 限流默认 fail-closed（`GO_GATEWAY_CHAT_RATE_LIMIT_FAIL_CLOSED` 默认 `true`）。

生产环境必须满足：

- 不可变镜像 tag。
- 真实 Secret，不使用 `CHANGE_ME`。
- Alembic migration 已执行。
- MySQL task store、event outbox、runtime registry 可用。
- runtime heartbeat producer 可持续注册运行时。
- `GO_GATEWAY_PUBLIC_BASE_URL` 显式配置。
- 只开放统一外部入口，不开放 Python southbound。
- 单端口覆盖所有助手 Web/UI/API 由 Go Gateway Runtime Proxy（`/assistants/{assistant_id}/api/*`、`/assistants/{assistant_id}/ui/*`）承担，也可在 Ingress/API Gateway 层完成统一路由。
- Kustomize/Helm 清单可渲染、可回滚。

## 9. 分支与代码线事实口径

`main` 与 `feat/webui` 是历史不连续的两条代码线，不能把 `feat/webui` 当普通 feature branch 直接回合并。后续只能按能力选择性收口，并记录缘由、操作方式、维护方式和迭代方式。

`feat/go` 代码线已于 2026-05-23 停止更新；此后 Go Gateway 开发统一在 `main` 上进行。

## 10. 文档索引

- 入口文档：`docs/START_HERE.md`
- AI 修改规则：`docs/AI_CODING_RULES.md`
- 本地统一启动：`docs/LOCAL_STARTUP.md`
- 接口契约：`docs/接口契约.md`
- 事件契约：`docs/事件契约.md`
- 状态机：`docs/状态机.md`
- 规模化进度（已归档）：`docs/archive/historical/scale-up-progress-tracker.md`
- 全流程测试：`docs/go-gateway/Go-Gateway-全流程测试手册.md`
- 部署说明：`docs/go-gateway/integration-and-production-deployment.md`
- 可观测性与排障 runbook：`go-gateway/docs/observability-runbook.md`（含完整 `GO_GATEWAY_*` 环境变量字典）
