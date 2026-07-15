# admin-session-260525-1630: 管理台助手入口 session 策略

- 模块：admin + security-service
- 创建日期：2026-05-25
- 编写人：top-player-ironagent
- 最近修改人：top-player-ironagent
- 最近修改日期：2026-05-25
- 等级：S2
- 状态：ready

## 背景

管理台（admin_web FastAPI + Streamlit 页面）的助手操作入口——"进入助手"按钮——每次调用 launch-url API 时都生成全新的 UI session key（`agent:{id}:ui-{timestamp}-{random}`），导致用户每次从管理台进入助手都像打开一个全新会话，看不到之前的对话历史。

代码中已经存在两种 session key 的生成路径：

- `_assistant_main_session_key()` → `agent:{id}:main`（固定主会话）
- `_assistant_fresh_session_key()` → `agent:{id}:ui-{ts}-{rand}`（每次新建）

但 launch-url API 强制使用 fresh session，没有给"进入助手"提供使用 main session 的选项。同时管理台也缺少独立的"新建对话"入口。

已有 spec `gateway-260514-1929` 规定了 launch-ticket 与控制会话的安全闭环，但未覆盖 main session 和 fresh session 的策略区分。

## 摘要

问题：管理台"进入助手"入口每次都生成新的 UI session，用户丢失之前的对话上下文。期望结果："进入助手"使用固定 main session（`agent:{id}:main`），保留对话连续性；独立的"新建对话"入口才使用 fresh session（`agent:{id}:ui-{ts}-{rand}`）。

## 问题

- 管理台"进入助手"（`GET /api/v1/admin/assistants/{id}/launch-url`）强制 `fresh_session=True`，生成的 session key 为 `agent:{id}:ui-{ts}-{rand}`。
- 用户重新点击"进入助手"后打开的是新 session，看不到之前的对话。
- 管理台没有独立的"新建对话"按钮或入口，用户无法在保留主会话的同时选择创建新对话。
- `_gateway_launch_url()` 的 `fresh_session` 参数默认值为 `False`（使用 main session），但唯一的外部调用点（launch-url API）显式传了 `True`，与默认设计意图相反。
- Streamlit 原生 `_render_gateway_launch_button()` 使用 `session_key="main"` —— 这个路径行为是正确的，但仅限 Streamlit 页面直接调用，Go Gateway proxy 路径无法使用。

## 参与方

- **管理员**：从管理台访问助手控制页，期望看到持续的对话上下文。
- **终端用户**：在控制页中与助手交互，期望对话不丢失。
- **API 调用方**：管理台前端调用 `launch-url` API，需要指定使用 main session 还是 fresh session。
- **助手 runtime**：不改变运行时协议；session key 仅影响 UI 层的会话绑定。
- **Gateway**：通过 `launch_ticket` 和 `session_key` 建立 control session，session key 决定 UI 侧绑定的对话上下文。
- **内部服务**：安全服务、管理后台按内部调用边界交换凭证，不参与 session 策略决策。

## 生效时机

- 发布后立即生效：代码发布后需要重启/recreate admin 服务；已打开页面刷新后生效。
- 无需重启助手 runtime。
- "新建对话"按钮（前端独立 UI）上线后另行生效。

## 权限与安全边界

- 谁可以操作：具备管理后台授权的管理员可以打开助手控制页。
- 谁可以查看：控制页只能查看当前 session key 绑定的对话上下文。
- 谁不能触发：无管理后台权限的管理员不能创建 launch ticket 或 control session。
- 是否需要审计：不需要新增审计；已有 launch ticket 审计覆盖入口操作。

## 非目标

- 本 spec 不改变 launch-ticket 的安全闭环（由 `gateway-260514-1929` 覆盖）。
- 本 spec 不修改 workspace-files 或审批 resolve 的鉴权路径。
- 本 spec 不新增数据库表或持久化状态。
- 本 spec 不改变 assistant runtime 的执行协议。
- 本 spec 不引入前端 session 列表/切换 UI（仅定义入口策略）。

## 行为

B1. 当管理员从管理台点击"进入助手"时，`launch-url` API 必须使用 main session（`session_key = agent:{id}:main`），保留该助手的对话连续性。

B2. 当管理员需要开始全新对话时，launch-url API 应支持 `fresh=true` 查询参数生成 fresh session。前端独立的"新建对话"按钮属于独立 UI 变更，不在本 spec 范围内（见非目标）。

B3. 当 `launch-url` API 被调用且未传 `fresh` 参数时，默认使用 main session（fresh=false），与函数签名默认值一致。

B4. 当管理台前端需要区分"进入助手"和"新建对话"时，launch-url API 提供 `fresh` 查询参数，由前端决定策略。

B5. 当使用 main session 进入助手时，control session 绑定 `agent:{id}:main` 为主会话；Gateway 的 UI 层应优先恢复该 session 绑定的对话上下文。

B6. 当使用 fresh session 时，control session 绑定全新的 session key，UI 层表现为空对话。

B7. 当 main session 和 fresh session 并存时，两者互不干扰：main session 不因 fresh session 的创建而丢失，fresh session 不覆盖 main session 的对话上下文。

B8. 本次变更不得改变 launch ticket 的安全闭环：ticket 仍必须绑定管理员、assistant、过期时间和使用状态；不得因 session 策略变化引入新的 token 泄漏路径。

## 待确认问题

- 管理台前端（admin_web Streamlit + 后续 WebUI 集成）的"新建对话"按钮的 UI 位置和样式由前端决定，不在本 spec 范围内。
- Streamlit 原生 `_render_gateway_launch_button()` 已经使用 `session_key="main"`，但与 launch-url API 走的是两套代码路径。后续如果统一入口，需要确认哪个路径优先。