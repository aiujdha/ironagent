# admin-session-260525-1630: 管理台助手入口 session 策略 — 技术方案

- 模块：admin + security-service
- 创建日期：2026-05-25
- 编写人：top-player-ironagent
- 最近修改人：top-player-ironagent
- 最近修改日期：2026-05-25
- 等级：S2
- 状态：ready

## 背景

- 产品 spec：`./PRODUCT.md`
- 已有 specs：`specs/gateway-260514-1929/{PRODUCT,TECH}.md`（control UI session 最小闭环）
- 当前代码入口：
  - `src/admin_web/app.py`（launch-url API 端点 + `_gateway_launch_url` 函数）
  - `src/admin/pages/assistants.py`（Streamlit 管理页面 + `_render_gateway_launch_button`）
  - `src/admin/gateway_links.py`（launch URL 构建工具函数）
- 当前限制：
  - `launch-url` API line 3682 硬编码 `fresh_session=True`
  - Streamlit `_render_gateway_launch_button` line 81 硬编码 `session_key="main"`
  - 没有独立的"新建对话"前端入口
  - 已有 `_assistant_main_session_key()` 和 `_assistant_fresh_session_key()` 两个函数，分别生成 `agent:{id}:main` 和 `agent:{id}:ui-{ts}-{rand}`

## 影响范围

- Admin（launch-url API）：修改 `fresh_session` 参数。
- Admin（Streamlit 页面）：不修改；已使用 main session。
- Admin Gateway launch URL 构建：不修改；函数签名已支持 `fresh_session` 参数。
- Gateway：不修改；session key 仅作为 UI 会话标识，不影响 Gateway 内部鉴权。
- Security Service：不修改；ticket 创建逻辑不变。
- 前端（admin_web Streamlit 或后续 WebUI）：新增"新建对话"入口时需要新增 API 调用。

## 修改方案

### Phase A：进入助手默认使用 main session（本次实现）

修改点：`src/admin_web/app.py` line 3682

```python
# 当前（每次新建 session）：
fresh_session=True,

# 改为（使用 main session）：
fresh_session=False,
```

或删除参数，利用函数默认值 `fresh_session=False`。

### Phase B：API 支持 fresh 查询参数（本次实现）

launch-url API 新增可选 `fresh` 查询参数：

```python
@app.get("/api/v1/admin/assistants/{assistant_id}/launch-url")
async def admin_assistant_launch_url(
    assistant_id: str,
    fresh: bool = Query(False),
    _admin: None = Depends(require_internal_service_token),
) -> dict[str, str | None]:
    ...
    gateway_launch_url = _gateway_launch_url(
        assistant_id=record.assistant_id,
        gateway_url=record.runtime.gateway_url,
        gateway_token=record.runtime.gateway_token,
        fresh_session=fresh,
    )
    ...
```

前端调用：
```
# 进入助手（main session）
GET /api/v1/admin/assistants/001/launch-url              # fresh=false（默认）

# 新建对话（fresh session）—— 独立 UI 按钮属于后续事项，API 已就绪
GET /api/v1/admin/assistants/001/launch-url?fresh=true
```

### 不修改的范围

- 不修改 `src/admin/gateway_links.py`：`build_gateway_launch_url()` 和 `build_go_gateway_assistant_ui_url()` 函数签名不变。
- 不修改 `src/admin/pages/assistants.py`：Streamlit 页面已正确使用 `session_key="main"`。
- 不修改 `src/security_service/launch_tickets.py`：ticket 创建逻辑不变。
- 不修改 go-gateway 代码：session key 不参与 Gateway 内部的鉴权决策。

### 兼容性处理

- `fresh` 查询参数可选，不传时默认 `false`（main session）。注意：这是有意的行为变化——之前默认每次新建 session，现在默认使用 main session。不传 `fresh` 参数的用户行为将从 fresh session 变为 main session。
- 响应结构完全兼容：返回值字段不变，仅 `gatewayLaunchUrl` 中的 session key 格式从 `agent:{id}:ui-*` 变为 `agent:{id}:main`。

## 方案变更

### 接口

- API 变化：
  - `GET /api/v1/admin/assistants/{id}/launch-url` 新增可选查询参数 `fresh`（bool，默认 `false`）。
  - `fresh=false` → 使用主会话 `agent:{id}:main`
  - `fresh=true` → 使用新会话 `agent:{id}:ui-{ts}-{rand}`
- 请求/响应兼容性：不传 `fresh` 参数时行为不变（但内部从 `fresh_session=True` 改为 `fresh_session=False`，即 main session）。

### 状态与持久化

无变化。session key 仅作为 launch ticket 的一个字段传入，不新增数据库表或持久化状态。

### Runtime 语义

- 变更何时生效：代码发布后需要重启/recreate admin 服务；已打开页面刷新后生效。
- 助手容器是否必须重启：否。
- 对活跃会话或运行中任务的影响：不影响已有运行中任务；仅影响新打开的"进入助手"入口使用的 session key。

### 权限与安全边界

无变化。session key 策略不影响 launch ticket 的安全校验。

### 部署与回滚

- 发布计划：先改 `fresh_session=True` → `False`（第一部分），单独验证。第二部分"新建对话"查询参数可与前端配合上线。
- 健康检查：不需要新增。
- 可观测性：不需要新增日志；ticket 创建审计不受影响。

## 回滚方式

- 如何撤回配置：将 `fresh_session=False` 改回 `True`，或回滚文件后重启管理后台。
- 是否需要重启容器：需要重启 admin 容器。
- 是否需要恢复数据库/文件：不需要。
- 如何证明回滚成功：launch-url API 返回的 URL session key 回退到 `agent:{id}:ui-*` 格式。

## 测试与证据

| 行为 | 验证方式 | 证据 |
|------|---------|------|
| B1 | 调用 `/api/v1/admin/assistants/001/launch-url`，检查返回 URL 中 session key 为 `agent:001:main` | curl 输出 |
| B2 | 调用 `/api/v1/admin/assistants/001/launch-url?fresh=true`，检查 session key 为 `agent:001:ui-*` | curl 输出 |
| B3 | 不传 `fresh` 参数调用，检查 session key 为 main | curl 输出 |
| B4 | 调用 `.../launch-url?fresh=true` 后再调用 `.../launch-url`（无 fresh），两次返回不同 session key 格式 | curl 输出 |
| B5 | 两次调用 `.../launch-url`（均无 fresh），检查两次 session key 均为 `agent:001:main`（同一主会话） | curl 输出 |
| B6 | 调用 `.../launch-url?fresh=true`，检查 session key 包含 `ui-` 后缀，且每次调用生成不同随机值 | curl 输出 |
| B7 | 先调用 `.../launch-url?fresh=true` 获得 fresh session，再调用 `.../launch-url`（无 fresh），检查 main session 不受 fresh session 影响（仍为 `agent:001:main`） | curl 输出 |

## 风险与缓解

- 从 `fresh_session=True` 改为 `False` 后，现有用户的"进入助手"行为会改变：之前每次进入都是新对话，进入后将恢复 main session 的历史对话。这是预期的行为变化，不是故障。
- 如果 main session 的对话上下文因某些原因损坏（如 session 关联的数据被清理），用户应该有一个"新建对话"的退路。Part 2 的 `?fresh=true` 参数提供了这个退路。

## 后续事项

- **Phase C（独立 UI 入口）**：前端（admin_web Streamlit 或后续 WebUI）新增"新建对话"按钮，调用 `?fresh=true`。该变更独立于本次 backend 改动，需在 UI 变更 spec 中单独定义按钮位置、样式和交互行为。当前 Streamlit 页面使用的 `_render_gateway_launch_button()` 是独立代码路径，直接调用 `build_go_gateway_assistant_ui_url()` 和 `mint_control_ui_launch_ticket(session_key="main")`，不经过 launch-url API。未来如果统一入口到 Go Gateway proxy 路径，需要确认两个代码路径的 session 策略一致。