# ui-260514-1732: WebUI 审批弹窗 - 技术方案

- 模块：ui
- 创建日期：2026-05-14 17:32
- 编写人：kolbaaa14-gif
- 最近修改人：kolbaaa14-gif
- 最近修改日期：2026-05-18
- 等级：S1
- 状态：blocked

## 背景

- 产品 spec：`./PRODUCT.md`
- 当前代码入口：
  - 当前 checkout 未发现 `ui/` 目录。
  - 历史目标入口 `ui/src/ui/controllers/ironagent-approval-poller.ts`、`ui/src/ui/views/exec-approval.ts`、`ui/src/ui/views/chat.ts`、`ui/src/ui/app-render.ts`、`ui/src/ui/app.ts` 在本仓库不存在。
  - 当前可见页面入口是 `src/admin_web/static/index.html`，但它属于 admin 页面，不等同于本 spec 描述的外部 WebUI 聊天页。
- 当前限制：只处理 WebUI 用户审批展示；系统/管理员审批仍由管理后台处理。由于当前仓库缺目标 WebUI 代码树，本 spec 在当前分支为 `blocked`，不能直接派发实现。

## 影响范围

- UI：聊天页审批展示、审批弹窗、工作台侧栏审批区、相关文案和测试。
- Admin：不修改。
- Security Service：不修改。
- Go Gateway：不修改。
- Assistant Runtime：不修改。
- Workspace：不修改。
- Deploy/Ops：不修改。

## 当前事实

- 上述 `ui/src/...` 事实来自历史/外部 WebUI 上下文，不是当前 checkout 可验证事实。
- 当前仓库存在 `src/approval/`、`src/security_service/routes.py`、`src/admin_web/static/index.html` 和 Go Gateway 审批公开 API，但缺聊天页 WebUI 实现入口。
- 因此本 spec 当前只能作为目标行为记录；实现前必须先确认外部 UI 仓库或重新开一个面向当前 admin 页面/Go Gateway 的 spec。

## 修改方案

- 修改点：
  - 当前聊天上下文中的用户审批通过弹窗处理。
  - 弹窗中对用户审批只展示“允许”和“拒绝”动作。
  - 系统/管理员审批继续排除出聊天页用户审批弹窗。
  - 保留现有 `handleExecApprovalDecision` 和 `resolveIronAgentApproval` 决策路径。
  - 移除侧栏审批区，不再保留审批摘要或审批操作入口。
  - 补充或更新对应 UI 单测。
- 不修改的范围：
  - 不改审批 API。
  - 不改鉴权 Header。
  - 不改后端状态机、审计、超时或自动拒绝。
  - 不改 runtime、workspace、部署配置。
- 兼容性处理：
  - 保持系统/管理员审批不在聊天页弹窗处理。

## 风险

- 风险：误把系统/管理员审批暴露为 WebUI 可直接处理，可能改变实际审批权限边界。缓解：继续按 `approvalScope` 排除系统/管理员审批。
- 风险：侧栏和弹窗同时出现操作入口会让用户重复点击。缓解：明确弹窗为主决策入口，侧栏最多保留摘要。
- 风险：多个审批排队时用户不知道还有后续项。缓解：弹窗保留待处理数量提示。

## 验证方式

- 单测：在确认目标 UI 仓库后，更新对应聊天页测试，覆盖用户审批会显示弹窗，并提供“允许/拒绝”动作。
- 单测：在目标 UI 仓库覆盖系统/管理员审批不进入聊天页用户审批弹窗。
- 单测：在目标 UI 仓库覆盖侧边栏不再展示审批区。
- 手工验证：打开 WebUI 聊天页，制造一条当前助手待审批项，确认弹窗出现、允许/拒绝按钮可用、成功后弹窗关闭。
- 手工验证：制造决策失败，确认弹窗保留并展示失败反馈。

## 后续事项

- 先确认目标代码仓库或把需求重写到当前 `src/admin_web/static/index.html` 范围，再从 `blocked` 推回 `ready`。
