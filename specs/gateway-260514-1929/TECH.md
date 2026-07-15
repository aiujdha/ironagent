# gateway-260514-1929: Control UI session minimum loop - 技术方案

- 模块：gateway
- 创建日期：2026-05-14
- 编写人：kolbaaa14-gif
- 最近修改人：kolbaaa14-gif
- 最近修改日期：2026-05-18
- 等级：S2
- 状态：ready

## 背景

- 产品 spec：`./PRODUCT.md`
- 当前事实源文档：`specs/README.md`、`specs/_template/PRODUCT.md`、`specs/_template/TECH.md`、`specs/runtime-260513-1949/PRODUCT.md`、`specs/runtime-260513-1949/TECH.md`
- Runtime registry multi-node consistency is governed by `specs/runtime-260518-1025/{PRODUCT,TECH}.md`; this control-session spec does not by itself prove runtime registry convergence.
- 当前代码入口：
  - `src/admin/control_ui_launch.py`
  - `src/admin/gateway_links.py`
  - `src/admin/pages/assistants.py`
  - `src/admin_web/app.py`
  - `src/security_service/launch_tickets.py`
  - `src/security_service/routes.py`
  - `src/security_service/control_ui_relay.py`
  - `src/shared/config.py`
  - `go-gateway/internal/httpapi/router.go`
  - `go-gateway/internal/core/service.go`
  - `go-gateway/internal/southbound/client.go`
  - `go-gateway/internal/southbound/types.go`
  - `tests/integration/test_control_ui_launch_ticket.py`
  - `tests/integration/test_control_ui_gateway_relay.py`
  - `go-gateway/internal/httpapi/router_test.go`
- 当前限制：
  - 当前 checkout 没有 `ui/src/...` 和旧 `src/gateway/*.ts` 文件；这些旧路径不能作为本分支实现入口。
  - 当前已有 Python 安全服务 ticket/relay 路由和 Go Gateway 单端口控制入口；后续实现或验收应优先围绕上述 Python/Go 文件和测试。
  - `src/shared/config.py` 中 `control_ui_launch_ticket_ttl_seconds` 默认值为 600；相关测试可通过 `IRON_AGENT_CONTROL_UI_LAUNCH_TICKET_TTL_SECONDS=90` 覆盖。
  - `src/admin/gateway_links.py` 在无法获得 launch ticket 时仍保留 fragment fallback 拼接 `gatewayUrl`、`token` 的路径；这只能作为兼容/降级风险，不应被当成最终安全闭环。
  - 控制页 workspace-files 和审批 resolve 的当前事实以 Go Gateway `control/api` 路由测试和安全服务 relay 测试为准；若切换到外部 IronAgent UI 仓库，需要另行确认目标代码树。

## 影响范围

- Admin: 生成一次性 launch ticket，打开控制页时只传 ticket，不把真实凭证带到浏览器。
- Security Service: 校验内部服务调用、ticket 创建权限、ticket resolve 权限，或作为 Gateway 服务端换取运行态凭证的来源。
- Go Gateway: 新增或收口 launch ticket resolve、HttpOnly control session、`control/api` 路由、workspace-files 代理和审批 resolve 代理。
- Assistant Runtime: 不改变运行时协议；只接收 Gateway 服务端已授权后的内部调用。
- Workspace: list/upload/download/delete 统一经 control session 授权，绑定 assistant scope。
- UI: 删除前端对内部服务凭证/gateway token/runtime token 的依赖，所有控制面 HTTP 请求走同源 `control/api`。
- Deploy/Ops: 确认内网 HTTP/HTTPS/反代/NodePort 下 Cookie 属性、入口暴露和健康检查。

## 修改方案

- 修改点：
  - 管理后台或安全服务创建短期一次性 `launch_ticket`，ticket 绑定管理员、assistant、来源、过期时间和使用状态。
  - Gateway 提供 ticket resolve 入口，校验成功后设置 HttpOnly Cookie 并返回或重定向到干净控制页 URL。
  - Gateway 新增 control session 解析层，统一处理 `/assistants/{assistant_id}/control/api/v1/**`。
  - workspace-files 的 list/upload/download/delete 全部走 control session，不再要求浏览器提供内部服务凭证。
  - 用户审批 resolve 新增 control API 代理，前端点击“允许 / 拒绝”时只调用 control API。
  - UI 删除或禁用 URL/hash/env/storage 内部服务凭证读取路径在控制页单入口下的使用。
- 不修改的范围：
  - 不修改系统/管理员审批所属后台审批流程。
  - 不修改助手 runtime 执行协议。
  - 不修改 workspace 文件存储模型和文件落点。
  - 不新增外部公开管理接口。
- 兼容性处理：
  - 旧 `/assistants/{id}/api/v1/**` 是否保留只作为评审项；默认实现目标是控制页使用 `/assistants/{id}/control/api/v1/**`。
  - 如果保留旧路径，必须记录过渡期、鉴权等价性和删除条件。
  - Cookie path 建议限定到 `/assistants/{assistant_id}/control`，避免跨助手共享。

## 方案变更

### 接口

- API 变化：
  - 新增或确认 `POST /api/v1/control-ui/launch-tickets`，由管理后台服务端调用，返回短期一次性 ticket 或完整控制页 URL。
  - 新增或确认 `GET/POST /assistants/{assistant_id}/control/launch` 或等价 resolve 入口，消费 `launch_ticket`，设置 HttpOnly Cookie，并跳转到 `/assistants/{assistant_id}/ui` 或控制页实际路由。
  - 新增或收口 `/assistants/{assistant_id}/control/api/v1/workspace-files**`。
  - 新增或收口 `/assistants/{assistant_id}/control/api/v1/approvals/{approval_id}/resolve`。
- Callback 变化：无新增外部 callback。
- 请求/响应兼容性：
  - 浏览器请求不得要求内部服务凭证、`gateway_token` 或 runtime token。
  - 401 表示未建立或已失效的 control session；403 表示 session 存在但无权访问目标 assistant、file 或 approval。
  - ticket resolve 成功后 URL 中不得保留 ticket。

### 状态与持久化

- 状态机变化：
  - `ticket_created -> ticket_resolved -> session_active -> session_expired`
  - `ticket_created -> ticket_expired`
  - `ticket_created -> ticket_rejected`
  - `ticket_resolved` 后 ticket 不可再次使用。
- 数据库/配置/文件变化：
  - 最小实现可使用 Gateway 内存态 ticket/session 存储，但必须明确 Gateway 重启后会话失效。
  - 如果使用数据库或安全服务持久化 ticket/session，需要增加清理过期记录的机制和审计字段。
- 迁移或兼容行为：
  - 不需要迁移既有 workspace 文件。
  - 如果历史控制页还依赖 URL/hash token，应在单入口路径下禁用，避免新旧鉴权混用。

### Runtime 语义

- 变更何时生效：发布并重启 Gateway/相关后台服务后，新打开的控制页生效。
- 是否支持热更新：不要求热更新；已有页面可提示重新从后台打开。
- 助手容器是否必须重启：不应要求助手容器重启。
- 对活跃会话或运行中任务的影响：不影响已运行任务；仅影响控制页访问 workspace-files 和用户审批 resolve 的鉴权路径。

### Workspace 影响

- Workspace 创建/挂载/同步/清理变化：不改变。
- 文件落点变化：不改变。
- Skill/plugin 目录变化：不改变。
- 控制面变化：workspace-files API 必须从 control session 中解析 assistant scope；不得信任浏览器传入的 `assistant_id` 覆盖 session scope。

### 权限与安全边界

- 认证变化：
  - 浏览器从一次性 `launch_ticket` 换取 HttpOnly Cookie。
  - Gateway 服务端持有或换取内部调用所需凭证。
- 授权变化：
  - control session 绑定 assistant scope、管理员身份、过期时间和可用能力集合。
  - workspace-files 和审批 resolve 都以 control session 为唯一浏览器授权来源。
- 谁可以操作：管理后台授权管理员可以创建 ticket；有效 control session 可操作绑定助手下允许的 workspace 和用户审批。
- 谁可以查看：只可查看绑定助手的 workspace 文件和审批状态。
- 谁不能触发：无 session、session 过期、跨 assistant、ticket replay、ticket 过期、CSRF 校验失败、非同源来源都不能触发写操作。
- 审计变化：ticket 创建/消费、workspace 写、审批 resolve、鉴权失败都需要可定位到管理员、assistant、请求路径和结果。
- 租户/用户隔离变化：不得用浏览器传入的 assistant/file/approval id 扩大 session scope；服务端必须校验资源归属。

### 部署与回滚

- 发布计划：
  - 先上线最小闭环：ticket resolve 设置 HttpOnly Cookie，控制页 `control/api` 能完成 session 授权探测。
  - 再接 workspace-files list/upload/download/delete。
  - 最后接用户审批 resolve。
- 健康检查：
  - Gateway 原有 `/health`、`/ready` 不应被控制页 SPA 或 `control/api` 路由覆盖。
  - 增加 control session 探测接口或测试用例验证 Cookie 建立和 401/403。
- 可观测性：
  - 日志中不得打印 ticket、Cookie、内部服务凭证、gateway token、runtime token。
  - 需要记录 ticket/session 的失败原因码，但不记录秘密值。

## 回滚方式

- 如何撤回配置：关闭或下线 control session 入口，恢复管理后台打开旧控制页入口。
- 是否需要重启容器：需要重启 Gateway；如果 ticket/session 存在后台服务，也需要重启或回滚对应服务。
- 是否需要恢复数据库/文件：不需要恢复 workspace 文件；如新增 ticket/session 表，仅需停止使用并按过期清理。
- 如何证明回滚成功：
  - 旧控制页入口可打开。
  - `/assistants/{id}/control/api/v1/**` 不再被新 UI 依赖。
  - 日志中没有新增 token 泄漏。
  - workspace-files 和审批 resolve 回到回滚前行为。

## 测试与证据

| 行为 | 验证方式                                                                                             | 证据                                                       |
| ---- | ---------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| B1   | 单元测试管理后台 ticket 创建响应；浏览器 URL/HTML/JSON 中不包含 admin/runtime/gateway token          | 测试输出、抓包或 Playwright 地址栏截图                     |
| B2   | 集成测试有效 ticket resolve；断言 `Set-Cookie` 包含 HttpOnly，URL 被重定向清理                       | HTTP 测试输出                                              |
| B3   | 集成测试过期、重复使用、assistant 不匹配、来源不匹配 ticket                                          | 401/403/410 响应和日志原因码                               |
| B4   | 控制页入口和响应扫描：单入口路径下不读取 storage token，不发送内部服务凭证；若涉及外部 UI，先确认目标仓库。 | Python/Go 单测、响应断言、代码扫描。                       |
| B5   | 集成测试 session 过期/缺失返回 401/403，UI 提示重新从后台打开                                        | HTTP 测试和 UI 测试                                        |
| B6   | Gateway 测试 workspace-files list/upload/download/delete 均通过 control session，跨 assistant 被拒绝 | `go test ./internal/httpapi` 和相关 Python 集成测试。       |
| B7   | Gateway 或安全服务测试审批 resolve 通过 control API，跨 assistant/已处理审批被拒绝                  | Go/Python 审批 resolve 测试输出。                          |
| B8   | 用户审批逐条弹出属于外部/助手 UI 行为；当前仓库缺 `ui/` 代码时只能记录为待目标仓库验证。              | 目标 UI 仓库测试或手工截图。                               |
| B9   | UI 测试系统/管理员审批不进入用户弹窗 resolve 路径                                                    | 视图测试输出                                               |
| B10  | 代码扫描和集成响应断言：token 不进入前端 JSON、storage、日志                                         | `rg` 结果和 HTTP 响应断言                                  |
| B11  | 内网 HTTP、HTTPS 反代两类环境的 Cookie 属性测试                                                      | curl/Playwright/部署验证记录                               |
| B12  | 回滚演练：关闭新入口后旧入口可用，workspace/审批回到旧行为                                           | 回滚命令和验证日志                                         |

## 风险与缓解

- Cookie `Secure` 判断错误会导致内网 HTTP 页面无法建立 session。缓解：实现时显式处理直连 HTTP、HTTPS 反代和 `X-Forwarded-Proto`。
- Cookie 鉴权写接口存在 CSRF 风险。缓解：写操作必须加 Origin/Referer 校验、CSRF token 或等价同源校验。
- Gateway 内存态 session 在重启后失效。缓解：UI 给出明确重新打开提示；如业务要求跨重启保留，则改用服务端持久化并重新评审。
- 旧 `/api/v1` 和新 `/control/api/v1` 并存可能造成鉴权绕行。缓解：过渡期必须保证鉴权等价，并设置删除条件。
- workspace file id 或 approval id 如果只按 URL 参数信任，会造成跨助手访问。缓解：服务端按 control session scope 校验资源归属。
- 日志或错误响应可能意外带出 ticket/token。缓解：引入秘密字段脱敏测试和响应断言。

## 后续事项

- 实现前确认目标分支中管理后台、安全服务的真实代码入口，避免按不存在的 `src/admin_web/app.py` 设计。
- 后续如果需要 control session 跨 Gateway 重启保留，应改为服务端持久化并重新评审。
- 后续如果删除旧 `/assistants/{id}/api/v1/**` 兼容路径，需要单独确认影响面和回滚窗口。
