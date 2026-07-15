# Runtime `in_flight` 结果获取 — 需求说明（go-gateway → runtime 团队）

> 提出方：go-gateway 团队　|　日期：2026-06-10　|　目标仓：`iron-agent`
>
> 本文中的 runtime 侧 `file:line` 均来自 go-gateway 团队对 `iron-agent` 源码的调查，**请 runtime 团队复核确认**。

## 背景

go-gateway 通过 WebSocket 向 runtime 发 `chat.send`（带 `idempotencyKey` = gateway 的 task RequestID、`sessionKey`、`runId`）。在以下时序下，gateway 会用**相同 idempotencyKey** 在一条**新连接**上重发 `chat.send`：

- gateway 因超时/stale 主动断开旧连接后重试；
- 网络抖动导致重连。

此时若该 idempotencyKey 对应的 run **仍在执行中**，runtime 返回：

```ts
// src/gateway/server-methods/chat.ts:1515-1519
respond(true, { runId: clientRunId, status: "in_flight" }, undefined, { cached: true, runId: clientRunId });
```

## 问题：in_flight 之后，发起请求的新连接拿不到该 run 的结果

go-gateway 调查发现，runtime 目前**没有为 in_flight 场景提供干净的结果获取路径**：

1. **in_flight 响应极简**：只含 `{runId, status:"in_flight"}`，不含结果或进度。（`chat.ts:1515`）
2. **in_flight 路径不注册事件订阅**：该分支直接 `respond` 后 `return`，从不调用 `registerToolEventRecipient`，新连接不会被登记为该 run 的事件接收者。（`chat.ts:1513-1520`；对比正常路径在 `onAgentRunStart` 才注册，`chat.ts:1758`）
3. **dedupe 缓存不存完整结果**：run 完成时写入的 payload 仅为 `{runId, status:"ok"}` / `{runId, status:"error", summary}`，**无答案文本**；相同 idempotencyKey 在 run 完成后重发也只拿到状态，拿不到内容。（`chat.ts:1863-1871`、`server-shared.ts:3-8`）
4. **无显式订阅方法**：没有 `chat.subscribe` / `chat.await` 让新连接主动订阅某 `runId` 的事件流。
5. **chat final 虽是全局 broadcast**（`server-broadcast.ts:105-129`），新连接理论上可收到，但依赖①连接一直保持②final 不在新连接建立前就已 broadcast 完 —— **不可靠**。
6. `chat.history` 读的是 session transcript（`chat.ts:1205-1285`），不精确对应单个 `runId`，无法作为可靠的单 run 结果获取机制。

**后果**：in_flight 后 gateway 进入读循环等不到属于该 run 的 final，一直阻塞到 stale/overall deadline，浪费整个超时窗口，且最终仍拿不到结果。

## go-gateway 侧已做的临时处理（best-effort，非根治）

go-gateway 已落地一个最小缓解（commit 见下文）：识别 ack 的 `status:"in_flight"` 后**快速返回**一个可重试错误 `RuntimeInFlightError`，而非死等到超时。配合 gateway 已修复的取消（`chat.cancel`→`chat.abort` 生效），stale 重试时原 run 已被取消、controller 已删，重试相同 idempotencyKey 即可真正重新执行。

但这只是规避死等，**无法在不重跑的前提下取回正在执行的 run 的结果**。根治需要 runtime 侧支持。

## 需要 Runtime 侧提供的干净方案（任选其一，A 为首选）

### 方案 A（首选）：in_flight 路径把新连接登记为该 run 的事件接收者
在 `chat.ts:1513-1520` 的 in_flight 分支，于 `respond` 之前，将当前连接 `connId` 注册为该 `runId`（及同 session 活跃 run）的事件接收者，复用现有 `registerToolEventRecipient` 与 chat 事件投递机制。这样新连接即可收到该 run 后续的 progress/final，无需重跑。

### 方案 B：提供 `chat.await(runId)` / `chat.subscribe(runId)` 方法
新增一个方法，让任意连接按 `runId`（或 `sessionKey`+`idempotencyKey`）订阅/等待某个正在执行的 run 的结果流，直到 final/error。需纳入 `server-methods-list.ts` 白名单与 `method-scopes.ts` 权限。

### 方案 C（补充）：dedupe 缓存存储完整 final result
将 `setGatewayDedupeEntry`（`chat.ts:1863-1871`）写入的 payload 扩展为包含完整答案文本，使相同 idempotencyKey 在 run 完成后重发能直接拿到结果（幂等返回）。可与 A/B 叠加，覆盖"重发时 run 已完成"的场景。

## 验收标准

1. gateway 用相同 idempotencyKey 在新连接重发、runtime 回 in_flight 后，gateway 能在**不触发重跑**的前提下取得该 run 的最终结果（progress + final）。
2. run 已完成的情况下，相同 idempotencyKey 重发能幂等拿到完整结果（若采用方案 C）。
3. 不破坏现有去重语义（同 idempotencyKey 不重复执行）。

## 关联

- go-gateway 已修复：取消方法名 `chat.cancel`→`chat.abort`（取消此前 100% 失效）。
- go-gateway 已修复：gateway 默认超时不再砍持续出 progress 的健康任务 + deadline 不重试。
- 本需求是上述修复链的最后一环（in_flight 结果获取），其余已在 gateway 侧闭环。
