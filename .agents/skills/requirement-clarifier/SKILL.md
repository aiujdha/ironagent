---
name: requirement-clarifier
description: 在判断 S0/S1/S2/S3 或编写 PRODUCT.md、TECH.md 前，澄清模糊、跨模块、高风险或会改变行为的 IronAgent/IronAgent 需求；重点追问目标、参与方、范围、约束、假设和验收证据。
---

# Requirement Clarifier

在写 specs 或代码前，先澄清模糊的产品/工程需求。

## 什么时候使用

当需求在下面任一维度不清楚时，使用这个 skill：

- 期望结果
- 主要参与方或使用方
- 受影响模块
- 范围或非目标
- 权限、安全或审计边界
- runtime、容器、workspace、callback 或持久化行为
- 验收证据

当变更影响 Admin、Security Service、Go Gateway、Assistant Runtime、Workspace、Market Skill/Plugin、部署或持久化状态时，默认先使用它。

## 什么时候不用

下面这些情况可以不用 requirement-clarifier；如果判断为 `S0`，也不用创建 spec 文件，但必须记录变更目标和验证方式：

- 有明确复现和期望行为的小 bug
- 明确的单文件修改
- 文案修改或小 UI 调整
- 窄范围测试补充
- 局部重构且不改变行为
- 已经有批准过的 `PRODUCT.md` 和 `TECH.md`

## 工作流程

1. 用 2 到 4 句话复述需求，不额外扩大范围。
2. 区分已知事实、假设和缺失决策。
3. 识别受影响的 IronAgent/IronAgent 系统面。
4. 最多提出 5 个澄清问题。
5. 如果用户暂时不能回答，给出最小、保守的前进假设。
6. 推荐下一步产物：`S0` 直接实现、`S1` 简化 spec、`S2` / `S3` 完整 spec。

## 需要检查的项目面

- Admin：UI 行为、CRUD、状态展示、危险操作确认。
- Security Service：认证、审批、审计、策略执行。
- Go Gateway：北向 API、callback、fallback 查询、任务状态、路由。
- Assistant Runtime：容器启动/停止/重启、策略重新加载、会话行为。
- Workspace：`persistent`、`temp`、`skills`、`plugins`、文件同步、清理。
- Market Skill/Plugin：上传、安装、启用、停用、移除、runtime 同步。
- Deploy/Ops：镜像标签、发布、回滚、健康检查、日志。
- Persistence：MySQL/SQLite/对象存储/文件状态。

## 提问规则

- 先问问题，再问方案。
- 先问什么算成功，再问怎么实现。
- 每个问题都必须能帮助后续填写 spec。
- 优先问高价值问题，不做全面访谈。
- 一轮最多问 5 个问题。
- 不扩大范围。
- 不把澄清变成架构设计。
- 如果必须做假设，要明确标记为假设。

## 输出格式

```md
## 我理解的原始需求

## 已知事实

## 假设

## 缺失决策

## 建议等级

## 澄清问题

## 初步验收标准

## 最小可执行版本

## 推荐下一步
```

## 交接规则

- 如果行为已经清楚但实现不清楚，编写或请求 `TECH.md`。
- 如果行为还不清楚，先编写或请求 `PRODUCT.md`。
- 如果改动很小且局部明确，可以判断为 `S0`，直接实现并记录变更目标和验证方式。
- 如果存在跨模块、权限、runtime、workspace、部署或持久化变化，不按 `S0` 处理。
- 如果当前只能交付一部分，记录实际交付范围和后续事项。
