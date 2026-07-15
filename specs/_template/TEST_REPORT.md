# <模块名-YYMMDD-HHMM>: <标题> - 验收报告

- spec-id：
- 测试日期：
- 验收分支：
- 验收基线 commit：
- 验收目标 commit：
- target HEAD：
- current HEAD：
- dirty status：clean/dirty（列出相关文件；无关改动单独说明）
- 验收人：
- 测试范围：
- 测试方式：
- 环境边界：
- 结论：pending/passed/partial/failed/stale
- 分级结论：unverified/blocked/code-present/local-verified/deployment-verified/verified

## 时效性

本报告只对“验收目标 commit”有效。如果当前分支 HEAD 不包含该 commit，或后续 spec/commit 已经修正本报告中的失败项，先更新本报告，再拆后续任务。

## 总结论

- 已通过：
- 部分通过：
- 未通过：
- 未验证：
- 阻塞：

## 关联提交

| 类型 | commit | 说明 |
| ---- | ------ | ---- |
| 基线 |        |      |
| 实现 |        |      |
| 集成 |        |      |
| 验收 |        |      |

## 命令结果

| 命令 | 结果 | 说明 |
| ---- | ---- | ---- |
|      |      |      |

## 行为覆盖矩阵

| 行为 | 当前结论 | 证据 |
| ---- | -------- | ---- |
| B1   |          |      |
| B2   |          |      |
| B3   |          |      |

当前结论建议使用：`unverified`、`blocked`、`code-present`、`local-verified`、`deployment-verified`、`verified`。

- `unverified`：未执行或证据不足。
- `blocked`：环境、依赖、权限或目标代码缺失，无法执行验收。
- `code-present`：代码或单测存在，但没有真实运行/页面/部署证据。
- `local-verified`：在当前 HEAD 的本地环境完成可复现验证，但不是部署证据。
- `deployment-verified`：在明确目标环境完成部署/集群/线上等验证。
- `verified`：满足该 B 项要求的全部证据；如只满足本地或部署子集，优先使用更具体状态。

## 不应重复派发

列出已经实现或已经集成的行为，避免后续按旧报告重复拆任务。

- 无。

## 未完成或待验证

列出真正还需要实现、修复或重新验收的行为。

- 无。

## 后续动作

明确下一步是继续实现、补验收、修复阻塞，还是可以合并。

- 无。
