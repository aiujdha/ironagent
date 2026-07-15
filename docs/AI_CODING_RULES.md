# AI 编码规则

本文档约束后续 AI 协作开发方式。目标是减少无关改动、隐藏重构和口径漂移。

## 1. 成功标准先行

编码前必须先明确本轮成功标准。成功标准至少包含：

- 要解决的具体问题。
- 本轮允许修改的文件范围。
- 可验证结果。
- 不做的内容。

如果需求存在影响行为、安全、数据结构、接口契约或部署方式的歧义，必须先问清楚，不能猜。

## 2. 最小必要修改

只改当前问题必须修改的代码和文档。

禁止：

- 顺手重构。
- 顺手改格式。
- 顺手优化注释。
- 顺手重命名。
- 顺手抽象公共层。
- 添加未要求功能。
- 改动无关测试快照。

允许：

- 为修复当前问题增加必要测试。
- 为防止同类问题复发增加最小边界校验。
- 为接口、事件、状态变化同步更新对应契约文档。

## 3. 不碰无关脏改

工作区可能存在用户或其他协作者的未提交改动。

AI 必须遵守：

- 不回滚不是自己产生的改动。
- 不格式化包含无关脏改的文件。
- 不把无关脏文件混进本轮提交。
- 如果必须修改同一文件，先确认已有改动语义，再做最小局部补丁。

## 4. 安全默认 fail-closed

涉及鉴权、callback、文件、runtime、审批的改动，默认 fail-closed。

必须避免：

- 任意 callback URL。
- 生产环境接受私网 callback。
- 未签名外部请求。
- HMAC 不绑定请求目标。
- 文件字节进入 callback、MCP payload 或任务 JSON。
- Python southbound 暴露给外部。
- admin token 泄漏到日志、文档示例或上游协议。

## 5. 文档和事实源同步

如果改动影响以下任一内容，必须同步更新对应文档：

- 系统职责边界：`docs/PROJECT_LOGIC_SOURCE_OF_TRUTH.md`
- 接口字段：`docs/接口契约.md`
- callback 事件：`docs/事件契约.md`
- 对外状态或阶段：`docs/状态机.md`
- 本地启动：`docs/LOCAL_STARTUP.md`
- 部署步骤：`docs/go-gateway/integration-and-production-deployment.md`

禁止把接口字段、事件含义、状态机继续混写进一个大文档。

## 6. 验证要求

每轮改动后只跑与改动相关的最小检查。

常用检查：

- Go 代码：`cd go-gateway && go test -count=1 ./... && go vet ./...`
- Go 单包：`cd go-gateway && go test -count=1 ./internal/<package>`
- Python 代码：按仓库现有测试命令执行，不新增工具链假设。
- K8s 清单：`kubectl kustomize <overlay>`
- 文档改动：`git diff --check`

如果某项检查无法执行，必须说明原因。

## 7. 提交规则

提交前必须确认：

- 只 stage 本轮相关文件。
- `git status --short` 中无关脏改未被 stage。
- 没有密钥、token、数据库密码、个人路径被提交。
- PR 描述包含测试结果和风险说明。

禁止：

- 未经要求 amend commit。
- 使用 `git reset --hard`。
- 使用 `git checkout --` 回滚用户改动。
- force push 到受保护分支。

## 8. 架构约束

当前主线是：

```text
先把 Gateway 从薄编排器升级为有任务权威状态的调度控制面；
再谈 500-2000 pod 的稳定分发；
最后再扩展 WS/A2A 等协议形态。
```

不要优先做：

- WS/A2A。
- 给小熊猫暴露内部验证接口。
- 直接暴露 Python southbound。
- 没有持久化任务/事件前做多 Gateway 横向扩容。
- 把 Redis 当万能前置。
