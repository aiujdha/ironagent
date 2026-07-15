# admin-260514-2331: 后台管理、公开接口与助手运行闭环 - 技术方案

- 模块：admin
- 创建日期：2026-05-14 23:31
- 编写人：kolbaaa14-gif
- 最近修改人：kolbaaa14-gif
- 最近修改日期：2026-05-18
- 等级：S3
- 状态：ready

## 背景

- 产品 spec：`./PRODUCT.md`
- 当前事实源文档：
  - `specs/README.md`
  - `specs/_template/PRODUCT.md`
  - `specs/_template/TECH.md`
  - `specs/runtime-260513-1949/PRODUCT.md`
  - `specs/runtime-260513-1949/TECH.md`
  - `specs/runtime-260518-1025/PRODUCT.md`
  - `specs/runtime-260518-1025/TECH.md`
  - 用户提供的 `docs/archive/historical/IronAgent-API接口契约-curl复测报告-2026-05-08.md`（已归档）
- 当前代码入口：
  - `src/admin_web/app.py`
  - `src/admin_web/static/index.html`
  - `src/admin/control_ui_launch.py`
  - `src/admin/gateway_links.py`
  - `src/admin/pages/assistants.py`
  - `src/assistants/models.py`
  - `src/assistants/service.py`
  - `src/assistants_service/router.py`
  - `src/security_service/app.py`
  - `src/security_service/routes.py`
  - `src/security_service/user_routes.py`
  - `src/security_service/file_routes.py`
  - `src/security_service/launch_tickets.py`
  - `src/users/service.py`
  - `src/workspace_files/service.py`
  - `src/workspace_files/sync_service.py`
  - `src/market/routes.py`
  - `src/market/service.py`
  - `go-gateway/internal/httpapi/router.go`
  - `go-gateway/internal/core/service.go`
  - `go-gateway/internal/southbound/client.go`
  - `go-gateway/internal/southbound/types.go`
  - `docker/Dockerfile.assistants`
  - `scripts/assistant_pydeps_smoke.py`
- 当前限制：
  - 当前 checkout 已从旧 TypeScript `src/gateway/*.ts` / `ui/src/...` 形态切到 Python admin/security/workspace/market 服务加 Go Gateway；旧路径不能再作为实现入口。
  - `src/admin`、`src/users`、`src/market`、`src/workspace_files` 已有实现文件，后续实现必须先复核当前 Python/Go 入口，不能按早期“目录未实现”假设派发。
  - 公开 API 契约以复测报告为验收目标，但“创建助手自动创建缺失用户”不采用；本 spec 以“先创建用户，再创建助手，助手必须绑定已存在 `user_id`”为准。
  - 编码前必须在目标分支重新核实当前实现。本 spec 不能替代代码证据。
  - 当前工作区存在未提交源码改动时，验收报告必须注明证据对应的 HEAD 与工作区状态，不能把旧报告直接视作当前完整验证。

## 影响范围

- Admin：用户 CRUD、助手创建/详情/状态/启动/停止/重启/删除、能力装配、工作区文件、审计日志页面。
- Security Service：后台会话、公开 API 风险边界、审计写入、控制会话事件、失败请求记录。
- Go Gateway：公开业务 API、callback、任务状态、审批接口、control session、workspace-files 代理。
- Assistant Runtime：助手容器生命周期、runtime reload、Python 办公依赖、能力 readiness。
- Workspace：`temp`、`persistent`、`skills`、`plugins` 的展示、上传、下载、只读规则和 refresh/reload。
- Market Skill/Plugin：市场列表、版本选择、安装、升级、卸载、启用、禁用、reload 和失败状态。
- UI：后台管理页面、助理列表/详情、能力弹窗、文件视图、审计列表、登录过期恢复。
- Deploy/Ops：Python 镜像依赖、字体安装、健康检查、状态同步和回滚验证。

## 修改方案

- 修改点：
  - 新增或补齐后台用户、助手、状态、工作区文件、能力装配和审计页面。
  - 补齐后台会话的最小 `HttpOnly Cookie` 方案，避免浏览器脚本持有 admin/runtime key、token 或长期 ticket。
  - 将公开业务 API 按复测报告固化为 `curl` 可验收契约。
  - 建立助手状态推送、健康检查回写、生命周期操作和审计边界。
  - 将 skill/plugin 从市场按版本装配到单个助手，并自动触发 runtime reload。
  - 将 Python 办公依赖和中文字体打入助手镜像。
- 不修改的范围：
  - 不做管理后台 `curl` 契约。
  - 不做公开业务 API token、签名、限流。
  - 不做市场上传/发布。
  - 不做多助手绑定和多助手批量能力操作。
  - 不触碰真实环境。
- 兼容性处理：
  - 公开业务 API 对齐报告字段和错误语义。
  - 后台会话过期必须回登录页并返回原页面，不能依赖浏览器缓存 token。
  - 未来多助手预留通过“默认助手”概念承接，首期不暴露 `assistant_id` 给公开业务 API。

## 方案变更

### 接口

- 公开业务 API 首期支持：
  - `GET /api/v1/health`
  - `POST /api/v1/files/upload`
  - `GET /api/v1/files/{file_id}`
  - `GET /api/v1/files/{file_id}/download`
  - `POST /api/v1/chat`
  - `GET /api/v1/tasks/{task_id}`
  - `POST /api/v1/tasks/{task_id}/cancel`
  - `GET /api/v1/approvals/{approval_id}`
  - `POST /api/v1/approvals/{approval_id}/decision`
- 公开业务 API 不要求 `X-API-Key`、`X-Client-ID`、`X-Timestamp`、`X-Nonce`、`X-Signature`、Bearer token 或 Cookie。
- 管理后台接口不作为首期 `curl` 契约输出；后台页面可使用内部管理接口，但浏览器不得读取、保存或发送 admin/runtime 凭据。
- 后台需要接口或等价能力支撑：
  - 用户创建、查看、删除。
  - 助手创建、详情、状态、启动、停止、重启、软删除。
  - 助手状态 SSE/WebSocket 推送和轮询 fallback。
  - 工作区文件列表、上传、下载。
  - 市场能力列表、版本选择、安装、升级、卸载、启用、禁用、状态刷新、runtime reload。
  - 审计日志列表、筛选、导出 CSV/JSON。

### 状态与持久化

- 用户表或等价存储必须支持 `user_id` 存在性校验。
- 助手表或等价存储必须支持：
  - `assistant_id`
  - `user_id`
  - 默认助手标记或等价绑定关系
  - `status`
  - `last_status_at`
  - soft delete 标记
  - 容器名称
  - 工作目录
  - 能力配置摘要
- 公开可访问的 `file_id`、`task_id`、`approval_id` 必须使用高熵随机 ID。
- 审计日志至少需要：
  - 时间
  - 操作者
  - 用户
  - 助手
  - 操作类型
  - 目标类型
  - 目标 ID
  - 结果
  - 错误码
  - 错误信息
  - 请求来源摘要
  - trace/request id
- 审计日志默认保留 45 天，超过后允许归档或清理。

### Runtime 语义

- 助手状态以数据库记录为页面事实源，容器真实状态由后台每 10 秒健康检查异步回写。
- Runtime 状态一致性、freshness、SSE/fallback 降级、多 Gateway registry、shared MySQL/secret/heartbeat 前提，以及 saved config 与 applied runtime 的区分，详见 `specs/runtime-260518-1025/`；本 spec 的 B11 只定义 admin 最低健康检查纠偏要求。
- 健康检查纠偏不进入正式审计日志，但必须进入系统事件日志或运行日志。
- 启动、停止、重启、删除由后台触发后更新数据库状态，并向页面推送状态变化。
- 停止、重启、删除需要二次确认；启动不需要二次确认。
- soft delete 助手必须禁用运行态；公开 API 对该用户返回无可用助手错误。
- 能力安装、升级、卸载、启用、禁用后自动 runtime reload；reload 失败时配置可保存，但运行态必须显示未生效。

### Workspace 影响

- 每个助手工作区需要稳定暴露：
  - `temp`
  - `persistent`
  - `skills`
  - `plugins`
- 后台管理员可查看、下载任意助手的四类目录文件。
- 助手容器侧栏只能查看、下载当前助手的四类目录文件。
- 上传只允许进入 `temp` 或 `persistent`。
- `skills` 和 `plugins` 在文件视图只读，不能通过文件视图上传、删除或覆盖。
- 后台上传和助手侧栏上传成功后都触发对应助手 workspace refresh/reload。

### Market Skill/Plugin

- 本次假设市场或类 SkillHub 服务已存在能力包；不做市场上传/发布。
- 助理页面从市场列表选择 `skill/plugin` 和具体版本。
- 安装后固定版本，不自动升级。
- 升级必须由管理员手动选择新版本并确认。
- 首期不做独立回滚按钮；选择旧版本重新安装覆盖即为恢复旧版本。
- 操作范围只限当前选中助手，不做批量。
- 安装、升级、卸载、启用、禁用、状态刷新和 reload 都需要可观察结果。

### 权限与安全边界

- 后台首期只有超级管理员一种角色。
- 后台会话沿用现有登录方式，补齐 `HttpOnly Cookie` 会话和过期重进流程。
- 页面会话过期后自动跳转登录页，登录成功后返回原页面。
- 浏览器端不缓存、不读取、不持有内部服务凭证、gateway token、runtime token、API key 或长期 ticket。
- control session 的 ticket create、consume、expire、reject 必须审计。
- 公开业务 API 首期允许公网无鉴权且无限流，这是明确 S3 风险。
- 公开业务 API 的缓解措施只包括高熵 ID、`user_id` 归属校验、失败审计和后续补强计划。
- 审批详情和审批决策公开开放，不额外要求 `user_id`。

### 部署与回滚

- Python 镜像必须安装：
  - `numpy==1.26.4`
  - `openpyxl==3.1.5`
  - `python-docx==1.1.2`
  - `matplotlib==3.8.4`
  - `pandas>=2.1.0`
  - `python-pptx`
  - `pypdf`
  - `pdfplumber`
  - `reportlab`
  - `Pillow`
  - `tabulate`
  - apt package `fonts-wqy-zenhei`
- 发布计划必须支持先本地代码级验证，再进入真实环境验证。
- 如果任何安全边界、公网暴露、真实部署策略变化超出本 spec，必须另开确认。
- 可观测性需要覆盖后台操作日志、正式审计日志、系统事件日志、runtime reload 日志、callback 投递日志和 workspace refresh/reload 日志。

## 回滚方式

- 后台页面回滚：关闭新页面入口或回退到旧后台路由。
- 后台会话回滚：关闭新 `HttpOnly Cookie` 会话入口，恢复旧登录方式；同时确认浏览器不再访问新 control session 路径。
- 公开业务 API 回滚：关闭新增或变更接口路由，恢复旧 contract；如果已经暴露公网，必须同步下线入口并保留审计。
- 助手状态回滚：停止 SSE/WebSocket 推送，回到手动刷新或既有状态查询。
- 工作区文件回滚：关闭后台上传入口，保留只读列表和下载；已上传文件按 workspace 归属保留或按审计记录人工清理。
- 能力装配回滚：禁用新增能力装配入口，对已安装能力选择旧版本覆盖或禁用，并触发 runtime reload。
- Python 镜像回滚：回退到上一镜像标签，验证助手启动、Python import、中文字体和基础文件处理。
- 审计回滚：保留已写审计表，关闭导出入口或新筛选条件；不得删除已生成审计记录作为回滚手段。
- 回滚成功证据：后台页面状态、API 响应、容器状态、runtime 日志、workspace 文件树、审计日志和镜像版本。

## 测试与证据

| 行为 | 验证方式                                                                 | 证据                                                  |
| ---- | ------------------------------------------------------------------------ | ----------------------------------------------------- |
| B1   | 创建用户后查询用户列表或用户详情。                                       | 后台截图、API 响应、数据库记录。                      |
| B2   | 创建助手时分别传入存在和不存在的 `user_id`。                             | 成功响应、400/404 错误响应、审计记录。                |
| B3   | 使用只包含 `user_id` 的公开 API 发起上传和 chat。                        | curl 输出、任务记录、助手绑定记录。                   |
| B4   | 检查数据结构和文档是否保留默认助手语义，且公开 API 拒绝 `assistant_id`。 | schema/代码审计、curl 400。                           |
| B5   | 删除助手后检查状态、运行态和历史数据。                                   | 后台截图、容器状态、workspace 文件树、审计记录。      |
| B6   | 对已删除助手的用户调用公开 API。                                         | curl 错误响应。                                       |
| B7   | 打开助理列表并改变助手状态。                                             | 页面截图、状态更新时间。                              |
| B8   | 打开助理详情。                                                           | 页面截图显示能力配置、容器名称、工作目录、readiness。 |
| B9   | 正常连接 SSE/WebSocket，再模拟断开。                                     | 浏览器截图、网络日志、轮询请求记录。                  |
| B10  | 分别触发启动、停止、重启、删除。                                         | 操作截图、确认弹窗截图、审计记录。                    |
| B11  | 人工制造数据库状态与容器状态不一致。                                     | 状态回写记录、系统事件日志。                          |
| B12  | 执行人工生命周期操作。                                                   | 正式审计日志。                                        |
| B13  | 执行公开 API 最小 curl 套件，并确认无管理 curl 验收项。                  | curl 脚本输出、测试计划。                             |
| B14  | 调用 health。                                                            | curl 输出字段与复测报告一致。                         |
| B15  | 上传文件，并尝试额外字段 `assistant_id`。                                | curl 200 和 400 输出。                                |
| B16  | 上传成功后检查文件落点和响应字段。                                       | curl 输出、workspace 文件树。                         |
| B17  | 查询文件元信息的正向、缺 user_id、错 user_id、文件不存在。               | curl 200/400/403/404 输出。                           |
| B18  | 下载文件并比较内容。                                                     | curl 下载文件、cmp 或哈希结果。                       |
| B19  | chat 正向、缺 `request_id`、未知文件、额外 `assistant_id`。              | curl 202/400/422/400 输出。                           |
| B20  | 创建任务后查询任务。                                                     | curl 输出字段与 callback 字段对齐。                   |
| B21  | 对执行中和终态任务分别取消。                                             | curl 成功和 409 输出。                                |
| B22  | 配置 callback 接收端并模拟成功、失败、重试、dead-letter。                | callback 接收日志、Gateway 日志。                     |
| B23  | 查询审批详情、提交 approve/deny、提交非法 decision。                     | curl 200 和 400 输出。                                |
| B24  | 检查 ID 生成器和样例响应。                                               | 代码审计、测试断言。                                  |
| B25  | 对每类公开 API 制造失败请求。                                            | 正式审计日志记录。                                    |
| B26  | 后台下载不同助手目录文件。                                               | 后台截图、下载结果。                                  |
| B27  | 助手侧栏访问当前助手和非当前助手文件。                                   | 页面截图、403/不可见证据。                            |
| B28  | 后台和助手侧栏分别上传到 `temp`、`persistent`、`skills`、`plugins`。     | 上传成功/失败响应、workspace 文件树。                 |
| B29  | 在文件视图尝试修改 `skills` 或 `plugins`。                               | UI 禁用状态或 API 403/400。                           |
| B30  | 上传文件后检查 refresh/reload。                                          | runtime 日志、workspace refresh 事件。                |
| B31  | 在助理页面从市场选择能力和版本。                                         | 页面截图、市场列表响应。                              |
| B32  | 安装后检查固定版本，并确认新版本不会自动升级。                           | 配置记录、runtime 状态。                              |
| B33  | 选择旧版本覆盖安装。                                                     | 页面操作记录、版本记录。                              |
| B34  | 对单个助手执行安装、升级、卸载、启用、禁用。                             | 页面截图、runtime reload 日志。                       |
| B35  | 模拟 reload 失败。                                                       | 页面失败状态、审计记录字段。                          |
| B36  | 查询、筛选、导出审计日志。                                               | 页面截图、CSV/JSON 文件。                             |
| B37  | 验证 45 天保留策略配置和清理任务。                                       | 配置记录、清理日志。                                  |
| B38  | 创建、消费、过期、拒绝 control session，并模拟会话过期。                 | 审计日志、登录跳转和返回原页面证据。                  |
| B39  | 在助手容器内 import Python 包并生成含中文字体的图表。                    | 容器命令输出、生成文件。                              |
| B40  | 仅运行本地测试和代码审计，不访问真实环境。                               | 测试命令输出、审计记录。                              |

## 风险与缓解

- 风险：公开业务 API 允许公网无鉴权且无限流，调用方身份无法证明。缓解：首期必须使用高熵 ID、`user_id` 归属校验和全失败审计；后续安全补强应优先补 API token、签名、IP 白名单或限流。
- 风险：审批详情和审批决策不要求 `user_id`，一旦 `approval_id` 泄露即可尝试决策。缓解：`approval_id` 必须高熵随机，审批失败和决策全部审计；后续补强应至少绑定 `user_id` 或签名。
- 风险：浏览器安全策略导致 token/key/ticket 丢失。缓解：后台只依赖服务端会话和 `HttpOnly Cookie`；过期后自动登录并回到原页面。
- 风险：数据库状态与容器真实状态不一致。缓解：10 秒健康检查回写数据库，页面展示最后更新时间；纠偏写系统事件日志。
- 风险：能力安装成功但 runtime 未生效。缓解：安装配置态和运行态 readiness 分开展示，reload 失败时明确标红并审计。
- 风险：后台管理员可操作任意助手工作区文件，误操作影响范围大。缓解：上传只允许 `temp`/`persistent`，`skills`/`plugins` 文件视图只读，高风险写操作进入审计。
- 风险：Python 镜像依赖体积增加或包版本冲突。缓解：锁定关键版本，构建时 import 验证，保留上一镜像标签回滚。
- 风险：市场能力版本固定后存在安全修复滞后。缓解：后台显示可升级版本，由管理员手动确认升级，升级过程审计。

## 后续事项

- 2026-05-15 已完成专家评审，`PRODUCT.md` 和 `TECH.md` 状态已进入 `ready`。
- 实现前先处理或登记当前门禁阻塞：`pnpm build` 的 A2UI bundle 缺失，以及 `pnpm check` 的既有 lint 错误；最终 RC 前 `pnpm build`、`pnpm check` 必须通过。
- 实现阶段按阶段拆分：后台用户/助手基础、公开 API 合同、workspace 文件、能力市场装配、审计日志、Python 镜像、状态推送与健康检查。
- 后续安全补强建议单独开 S3：公开 API token/签名、限流、审批接口绑定用户、IP 白名单、管理 curl 契约。
