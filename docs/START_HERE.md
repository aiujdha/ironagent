# 项目规范文档入口

本文档是团队和 AI 进入本项目的统一入口。不要直接从零散文档或历史对话开始理解项目。

## 1. 新人接入阅读顺序

1. `docs/PROJECT_LOGIC_SOURCE_OF_TRUTH.md`
2. `docs/LOCAL_STARTUP.md`
3. `docs/接口契约.md`
4. `docs/事件契约.md`
5. `docs/状态机.md`
6. `docs/AI_CODING_RULES.md`
7. `.github/pull_request_template.md`
8. `.github/BRANCH_PROTECTION.md`
9. `docs/beginner-docker-deploy-to-use.zh-CN.md`（从零跑通 Docker 部署到使用，新手建议通读）
10. `docs/archive/historical/DOCUMENT_CLEANUP_REPORT-2026-04-27.md`（历史治理报告，选读）

读完后必须能回答：

- 小熊猫、IronAgent、Go Gateway、MCP、IronAgent Python control plane 分别负责什么。
- callback 是主链路，fallback 是补偿查询。
- 文件内容为什么不进 MCP、callback 或任务 JSON。
- `status`、`stage`、`event` 的职责区别。
- 本地统一启动命令是什么。
- AI 或开发者改代码时哪些行为禁止。

## 2. 日常开发查阅规则

修改接口前必须阅读：

- `docs/PROJECT_LOGIC_SOURCE_OF_TRUTH.md`
- `docs/接口契约.md`
- `docs/AI_CODING_RULES.md`

修改 callback/webhook 前必须阅读：

- `docs/PROJECT_LOGIC_SOURCE_OF_TRUTH.md`
- `docs/事件契约.md`
- `docs/状态机.md`
- `docs/AI_CODING_RULES.md`

修改任务状态流转前必须阅读：

- `docs/状态机.md`
- `docs/事件契约.md`
- `docs/AI_CODING_RULES.md`

修改文件上传下载、MCP 或 presigned URL 前必须阅读：

- `docs/PROJECT_LOGIC_SOURCE_OF_TRUTH.md`
- `docs/接口契约.md`
- `docs/AI_CODING_RULES.md`

修改本地启动、部署、K8s、镜像、Secret 前必须阅读：

- `docs/LOCAL_STARTUP.md`
- `docs/go-gateway/integration-and-production-deployment.md`
- `docs/ironagent-k8s-single-entry-full-deploy-runbook.zh-CN.md`
- `docs/environment-variables.md`
- `.github/BRANCH_PROTECTION.md`

排查线上故障、网关日志前必须阅读：

- `go-gateway/docs/observability-runbook.md`

使用 AI 写代码前必须阅读：

- `docs/AI_CODING_RULES.md`
- `AGENTS.md`

## 3. 开发任务开始前必须输出

每个开发任务开始前，开发者或 AI 必须先写清：

```md
## 本次任务理解

## 需要阅读的文档

## 预计修改文件

## 不修改范围

## 验收标准
```

如果任务不需要修改代码，也要说明验证方式。

## 4. PR 前检查

提交 PR 前必须确认：

- [ ] 已阅读 `docs/START_HERE.md`。
- [ ] 已遵守 `docs/AI_CODING_RULES.md`。
- [ ] 没有无关重构、格式化、注释清理或重命名。
- [ ] 没有提交密钥、token、密码、私有 URL、个人机器路径。
- [ ] 接口变化已更新 `docs/接口契约.md`，或本次不涉及接口变化。
- [ ] 事件变化已更新 `docs/事件契约.md`，或本次不涉及事件变化。
- [ ] 状态/阶段变化已更新 `docs/状态机.md`，或本次不涉及状态变化。
- [ ] 系统边界变化已更新 `docs/PROJECT_LOGIC_SOURCE_OF_TRUTH.md`，或本次不涉及边界变化。
- [ ] 本地启动变化已更新 `docs/LOCAL_STARTUP.md`，或本次不涉及启动变化。
- [ ] 已执行与改动相关的最小检查。

## 5. 文档维护规则

文档分工如下：

| 文档 | 维护内容 |
| --- | --- |
| `docs/PROJECT_LOGIC_SOURCE_OF_TRUTH.md` | 项目事实源、职责边界、路线方向。 |
| `docs/AI_CODING_RULES.md` | AI 和开发者最小修改规则。 |
| `docs/LOCAL_STARTUP.md` | 本地启动、调试、健康检查命令。 |
| `docs/ironagent-k8s-single-entry-full-deploy-runbook.zh-CN.md` | K8s 单入口全量部署现行 runbook。 |
| `docs/environment-variables.md` | IRON_AGENT_* 环境变量字典与读取位置。 |
| `docs/assistant-container-sqlite-admin-db-mapping.md` | 助手容器 SQLite 与后台管理持久化字段映射。 |
| `go-gateway/docs/observability-runbook.md` | 网关全链路日志事件、排障路径、GO_GATEWAY_* 字典。 |
| `docs/接口契约.md` | HTTP/API/MCP 字段和调用边界。 |
| `docs/事件契约.md` | callback/webhook 事件、ACK、retry、dead-letter。 |
| `docs/状态机.md` | `status`、`stage`、状态流转和 southbound 映射。 |
| `docs/archive/historical/DOCUMENT_CLEANUP_REPORT-2026-04-27.md` | 历史治理报告：文档盘点、冲突识别、归档/合并计划（不再更新）。 |
| `AGENTS.md` | AI 使用这些文档的入口规则。 |

禁止把接口字段、事件含义、状态机继续混写进一个大文档。
