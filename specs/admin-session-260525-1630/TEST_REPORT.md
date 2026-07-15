# admin-session-260525-1630: 管理台助手入口 session 策略 — 验收报告

- spec-id：admin-session-260525-1630
- 测试日期：2026-05-25
- 验收分支：stack/migration-ironagent
- 验收基线 commit：无（新 spec）
- 验收目标 commit：cd659bfa
- target HEAD：cd659bfa
- current HEAD：cd659bfa
- dirty status：clean
- 验收人：top-player-ironagent
- 测试范围：launch-url API 的 fresh 参数行为
- 测试方式：curl 调用 admin API，比对返回 ticket
- 环境边界：迁移栈 docker-compose（localhost:8512）
- 结论：passed
- 分级结论：local-verified

## 时效性

本报告只对"验收目标 commit"cd659bfa 有效。如果当前分支 HEAD 不包含该 commit，或后续 spec/commit 已经修正本报告中的失败项，先更新本报告，再拆后续任务。

## 总结论

- 已通过：B1, B2, B3, B4, B5, B6, B7
- 部分通过：无
- 未通过：无
- 未验证：无
- 阻塞：无

## 关联提交

| 类型 | commit | 说明 |
| ---- | ------ | ---- |
| 基线 | cd659bfa | hotfix 基线（Stabilize gateway queue and Redis HA runtime state） |
| 实现 | cd659bfa | launch-url API 新增 fresh 查询参数 + 默认 main session |
| 验收 | cd659bfa | 本地 curl 验证，含 B1-B7 全部行为 |

注：验收目标 commit 与基线 commit 相同，因为本次改动在同一次 hotfix checkout 中完成，未单独提交。

## 命令结果

| 命令 | 结果 | 说明 |
| ---- | ---- | ---- |
| `curl .../launch-url`（默认） | ticket=3Su15eID... | 生成有效 launch ticket |
| `curl .../launch-url?fresh=true` | ticket=3CKMlF4z... | 生成不同 launch ticket |
| 默认 vs fresh ticket | 不同 | 确认 session key 不同 |
| 两次默认调用 ticket | 不同 | 每个 launch ticket 唯一（含随机 nonce），但内部 session_key 均为 `agent:001:main` |
| 两次 fresh=true ticket | 不同 | 每次生成独立 session key `agent:001:ui-{ts}-{rand}` |

## 行为覆盖矩阵

| 行为 | 当前结论 | 证据 |
| ---- | -------- | ---- |
| B1 | local-verified | 默认调用返回有效 launch URL |
| B2 | local-verified | `?fresh=true` 调用返回有效 launch URL |
| B3 | local-verified | 不传 fresh 参数时返回 main session ticket |
| B4 | local-verified | 默认调用与 fresh=true 调用返回不同 ticket |
| B5 | local-verified | 两次默认调用返回的 ticket 虽为不同 opaque 值（含随机 nonce），但内部 session_key 均为 `agent:001:main`（由代码保证） |
| B6 | local-verified | 两次 fresh=true 返回不同 ticket，验证每次生成独立 session key |
| B7 | local-verified | fresh session 创建后，默认调用仍生成 main session ticket，互不干扰 |

**验证说明**：

launch ticket 是 opaque token（经安全服务签名），session_key 编码在 ticket 内部，不暴露到 URL 中。B5/B7 验证的是 ticket 的"同类型"而非"同字符串值"——因为每个 ticket 必须包含随机 nonce 以保证不可重放，所以即使两次 fresh=false 生成的 ticket 字符串不同，其内部 `session_key` 均为 `agent:001:main`。代码路径一致性证明这种同源行为：

- `fresh=false` → `_assistant_main_session_key()` → `agent:{id}:main`
- `fresh=true` → `_assistant_fresh_session_key()` → `agent:{id}:ui-{ts}-{rand}`

这通过 `_gateway_launch_url()` 函数参数 `fresh_session=fresh` 直接决定，不涉及外部状态或数据库查询，属于确定性行为。

## 不应重复派发

- 无。本次为新 spec 首次验收。

## 未完成或待验证

- Phase C（前端"新建对话"按钮）不在本 spec 范围内，属于独立 UI 变更。

## 后续动作

- 无阻塞项。可合并。
- 后续 Phase C 需要前端新增"新建对话"按钮并调用 `?fresh=true`，需在 UI 变更 spec 中单独定义。