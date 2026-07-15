# IronAgent Specs

Specs 是放在仓库里的轻量需求契约，用来在写代码前对齐产品行为、技术实现和验证证据。

## Spec 分级策略

所有任务都需要有变更目标和验证方式；只有 `S1`、`S2`、`S3` 需要创建 specs 文件。先按最小可用等级处理，不为小任务制造文档负担。

| 等级 | 适用任务                                                 | 文档要求                                                   | 复核要求           |
| ---- | -------------------------------------------------------- | ---------------------------------------------------------- | ------------------ |
| `S0` | 明确小 bug、小文案、小样式、单测补充、不改行为的局部重构 | 不创建 specs 目录；在回复或 PR 中写清“变更目标 + 验证方式” | 不强制复核         |
| `S1` | 小功能、单模块行为变化、低风险配置或页面变化             | 创建 `PRODUCT.md` + `TECH.md`，可以简写                    | 视风险决定是否复核 |
| `S2` | 跨模块、接口、权限、runtime、workspace、部署、持久化变化 | 完整 spec + 验证矩阵 + 风险 + 回滚                         | 至少 1-2 轮复核    |
| `S3` | 安全、权限绕过、运行中容器、数据迁移、线上部署高风险     | 完整 spec + 验证矩阵 + 风险 + 回滚 + 对抗交流              | 至少 2 轮复核      |

`S0` 仍需在最终回复或 PR 描述中说明变更目标、影响范围和验证方式。`S0` 不创建 specs 文件。

`S2` / `S3` 通常适用于下面情况：

- API 契约、callback 字段、任务状态或状态语义
- 权限、能力策略、admin 访问、审计或安全边界
- 正在运行的助手容器、runtime 重新加载、重启行为或会话行为
- workspace 生命周期、文件落点、同步、清理或可见性
- skill/plugin 的上传、安装、启用、停用、卸载或 runtime 同步
- 持久化结构、迁移、对象存储或长期保存状态
- 部署、灰度、回滚、镜像标签或健康检查

## 最短使用方法

每次开始任务前，先只做一个判断：这次要不要创建 spec 文件。

1. 如果只是明确的小改动，判断为 `S0`，不建 spec 文件，直接实现；最后说明变更目标和验证方式。
2. 如果会改变一个模块的行为，判断为 `S1`，复制模板创建 `PRODUCT.md` 和 `TECH.md`，只填必要章节。
3. 如果影响多个模块、接口、权限、runtime、workspace、部署或持久化，判断为 `S2`，完整填写行为、风险、回滚和验证矩阵。
4. 如果涉及安全绕过、运行中容器、数据迁移或线上高风险变更，判断为 `S3`，按完整 spec 写，并至少复核 2 轮。

判断不清时，不要先写代码，先用 `.agents/skills/requirement-clarifier` 把需求问清楚。

常见例子：

| 任务                               | 等级 | 怎么做                                               |
| ---------------------------------- | ---- | ---------------------------------------------------- |
| 改一个按钮文案                     | `S0` | 不建 spec，最后说明改了什么、怎么看到结果            |
| 修一个明确复现的小 bug             | `S0` | 不建 spec，最后说明复现点和验证命令                  |
| admin 页面新增一个低风险字段展示   | `S1` | 建 `specs/admin-<YYMMDD>-<HHMM>/`，简写 PRODUCT/TECH |
| 修改任务状态接口字段               | `S2` | 建完整 spec，写清兼容性、验证矩阵和回滚              |
| 修改能力策略对运行中容器的生效规则 | `S3` | 建完整 spec，先复核安全边界，再写代码                |

## 目录规则

`S1`、`S2`、`S3` 每个工作项使用一个目录：

```text
specs/<模块名>-<YYMMDD>-<HHMM>/
  PRODUCT.md
  TECH.md
  TEST_REPORT.md  # S2/S3 实现或验收后必须更新；S1 按风险决定
```

示例：

```text
specs/runtime-260513-1949/
  PRODUCT.md
  TECH.md
```

规则：

- 目录名由模块名、日期和时间组成。
- 日期使用 `YYMMDD`。
- 时间使用 24 小时制 `HHMM`。
- 当前 `spec-id` 就是目录名，例如 `runtime-260513-1949`。
- 功能标题和背景写进 Markdown 文件里。
- 统一使用大写文件名：`PRODUCT.md` 和 `TECH.md`。
- `S1`、`S2`、`S3` 每个 spec 都必须同时包含 `PRODUCT.md` 和 `TECH.md`。
- `S2`、`S3` 进入 `implemented` 或 `verified` 前，必须新增或更新 `TEST_REPORT.md`。
- `TEST_REPORT.md` 可以在实现前作为历史审计报告存在，但如果报告目标 commit 落后于当前分支或被后续 spec 修正，必须显式标记为过期，不能继续作为派发实现任务的依据。

## 固定模块名

当前允许使用这些模块名：

- `admin`
- `security`
- `gateway`
- `runtime`
- `workspace`
- `market`
- `ui`
- `deploy`
- `im`
- `storage`
- `docs`
- `test`

新增模块名前，先确认它不能归入已有模块。

## 模块边界

| 模块        | 边界                                                         |
| ----------- | ------------------------------------------------------------ |
| `admin`     | 后台页面、用户、助手、审批、审计、模型、能力策略             |
| `security`  | 鉴权、授权、审批拦截、安全策略、审计落点                     |
| `gateway`   | Go Gateway、北向 API、callback/fallback、任务状态、路由      |
| `runtime`   | 助手容器、启动/停止/重启、运行时配置、会话、工具执行         |
| `workspace` | 工作区、文件落点、文件同步、`persistent/temp/skills/plugins` |
| `market`    | skill/plugin 市场、上传、安装、启用、停用、卸载、失败回滚    |
| `ui`        | 助手前端、admin 前端、页面交互、状态展示                     |
| `deploy`    | Docker/K8s、镜像、发布、回滚、健康检查                       |
| `im`        | 小熊猫/IM 消息、文件、轮询、callback 对接                    |
| `storage`   | MySQL/SQLite/对象存储、迁移、持久化结构                      |
| `docs`      | specs、团队规范、使用手册、验收文档                          |
| `test`      | 自动化测试、手工验收、测试数据、测试脚本                     |

## 文件职责

`PRODUCT.md` 说明从使用方视角看，系统必须满足哪些行为。使用方可以是管理员、终端用户、API 调用方、助手 runtime、workspace、skill/plugin 或内部服务。

`TECH.md` 说明当前系统要怎么改，以及每条产品行为怎么验证。

`TEST_REPORT.md` 说明实际验收发生在什么分支、什么基线、什么目标 commit、跑了哪些命令、每条行为当前结论是什么。它是验收证据，不是计划文档；过期报告必须先同步再用于拆任务。

实现不能和 spec 脱节。如果代码改变了计划行为或实际交付范围，必须在同一次改动里更新 spec。

## 元信息

每个 `PRODUCT.md` 和 `TECH.md` 顶部都必须包含元信息：

```md
- 模块：runtime
- 创建日期：2026-05-13 19:49
- 编写人：<git username>
- 最近修改人：<git username>
- 最近修改日期：2026-05-13 19:49
- 等级：S1/S2/S3
- 状态：draft
```

状态只使用下面 5 个：

| 状态          | 含义                                    |
| ------------- | --------------------------------------- |
| `draft`       | 需求或方案还没确认                      |
| `ready`       | spec 已确认，可以准备实现；不代表已完成 |
| `blocked`     | 卡在依赖、环境、权限、决策或外部资源    |
| `implemented` | 已实现并记录实现 commit，等待验证       |
| `verified`    | 已验证闭环，且有当前有效的验收报告      |

编写人和最近修改人统一使用 Git 用户名。

`verified` 不能只从代码提交推断，必须有 `TEST_REPORT.md` 证明。若 `TEST_REPORT.md` 的目标 commit 不包含当前分支 HEAD，或报告结论与后续 spec、后续提交明显冲突，应先视为“报告过期”，不能把旧失败项直接拆成新实现任务。

## S1 简化 spec 最低要求

`S1` 简化版 `PRODUCT.md` 至少包含：

- 背景
- 摘要
- 行为
- 非目标

`S1` 简化版 `TECH.md` 至少包含：

- 背景
- 影响范围
- 修改方案
- 风险
- 验证方式

`S1` 简化 spec 也必须提供验证证据。例如：

- 小功能：页面截图、API 响应或手工操作记录。
- 单模块行为变化：测试结果或关键流程验证记录。
- 低风险配置变化：admin 状态、配置响应或日志证据。

## 完整 spec 验证矩阵

`S2` / `S3` 必须在 `TECH.md` 中写验证矩阵，并覆盖 `PRODUCT.md` 的每个行为编号：

```md
| 行为 | 验证方式 | 证据 |
| ---- | -------- | ---- |
| B1   | ...      | ...  |
| B2   | ...      | ...  |
```

实现或集成完成后，`S2` / `S3` 还必须在 `TEST_REPORT.md` 中维护同一组行为编号的实际结论。`TECH.md` 的验证矩阵回答“准备怎么验”，`TEST_REPORT.md` 回答“实际验到了什么”。

## 验证证据类型

| 证据              | 适用场景                          |
| ----------------- | --------------------------------- |
| `curl` / API 响应 | 接口、权限、callback、任务状态    |
| admin 页面截图    | 后台配置、状态展示、增删改查      |
| 容器状态          | runtime、重启、生效状态、部署     |
| runtime 日志      | 工具执行、策略加载、错误路径      |
| 数据库记录        | 持久化、审计、状态流转            |
| workspace 文件树  | 文件落点、skill/plugin 安装、同步 |
| K8s/Docker 状态   | 发布、回滚、健康检查              |
| 手工操作记录      | UI 操作、复杂流程验收             |
| 自动化测试结果    | 单测、集成测试、回归测试          |

## 流程

```text
判断等级
  -> S0: 明确目标和验证方式
  -> 实现
  -> 验证
  -> 回复或 PR 记录结果

模糊需求 / 需要澄清的 S1 / S2 / S3
  -> requirement-clarifier
  -> PRODUCT.md
  -> TECH.md
  -> 实现
  -> TEST_REPORT.md 验证证据
  -> review
```

当需求模糊、跨模块、高风险或会改变行为时，先使用 `.agents/skills/requirement-clarifier`。小而明确的 `S0` 任务可以跳过澄清提问和文件 spec，但不能跳过变更目标与验证方式说明。

## 团队 skill 与个人 skill

### 团队 skill

团队 skill 放在 `.agents/skills/`，进入仓库管理。它适合多人复用、需要统一流程、会影响项目交付质量的能力。

当前团队 skill 规划：

| skill                     | 状态   | 用途                                           |
| ------------------------- | ------ | ---------------------------------------------- |
| `requirement-clarifier`   | 已创建 | 澄清模糊需求，形成可写入 spec 的边界           |
| `admin-smoke`             | 待创建 | 验证 admin 页面、接口、增删改查和权限风险      |
| `runtime-deploy`          | 待创建 | 处理镜像、部署、重启、健康检查和回滚验证       |
| `skill-plugin-maintainer` | 待创建 | 处理 skill/plugin 上传、安装、启用、卸载和同步 |
| `security-review`         | 待创建 | 检查鉴权、权限、能力策略、token 暴露和审计     |
| `workspace-debug`         | 待创建 | 排查 workspace、文件落点、同步和可见性问题     |

团队 skill 必须满足这些要求：

- 可以被多人 review 和维护。
- 不包含个人机器路径。
- 不包含密码、token、服务器凭据或真实敏感配置。
- 不依赖某个人的本地环境才能执行。
- 输出格式尽量稳定，方便进入 spec、PR 或验收记录。

### 个人 skill

个人 skill 放在个人本地环境，不进入仓库。它适合个人习惯、个人快捷流程、临时排障笔记或本机专用操作。

个人 skill 不能作为团队流程依据。如果某个个人 skill 被多人频繁使用，应先整理为通用流程，再转成团队 skill 入库。

## 开始编码规则

按等级决定何时开始实现：

- `S0`：目标、范围和验证方式清楚即可实现。
- `S1`：`PRODUCT.md` 和 `TECH.md` 简要完成即可；是否复核按风险判断。
- `S2`：spec 状态为 `ready`，且至少经过 1 轮复核。
- `S3`：spec 状态为 `ready`，至少经过 2 轮复核，必要时进行对抗交流。
- 所有等级都必须没有重大未决问题，边界清晰，验证方式明确。

紧急线上修复可以先实现，但事后必须按实际等级补齐记录；如果属于 `S1`、`S2`、`S3`，还必须补齐 spec 和复核记录，并在 `TECH.md` 说明原因。

## 可选文件触发规则

`DECISIONS.md` 和 `PROJECT_LOG.md` 采用“显式指令 + 规则触发”双机制。

必须创建 `DECISIONS.md` 的情况：

- 用户明确要求记录决策
- 存在两个以上可选方案，且选择会影响后续实现
- 涉及权限、安全、数据持久化、运行中容器、部署回滚
- 用户或团队做过明确取舍
- 当前不做某个方案，但未来可能再讨论

必须创建 `PROJECT_LOG.md` 的情况：

- 用户明确要求记录过程
- 预计任务超过 2 天
- 超过 1 个人参与
- 多个 agent 或多轮 Codex 会话参与
- 涉及线上部署、排障或回滚
- 需要记录已经验证了什么、当前阻塞是什么、下一步做什么

如果是否需要创建存在明显不确定，先询问用户，不默认新增。

## Spec 与代码提交规则

提交规则的目标是让每次代码变更都能追溯到对应 spec、行为编号和验证证据。

### 分支与 worktree

- Codex 自动开发必须在独立 branch 或 worktree 中工作，不直接改 `main` / `master`。
- 有 spec 的任务，分支名应包含 `spec-id`，例如 `codex/runtime-260513-1949-policy-restart`。
- 无 spec 的 `S0` 小任务，分支名应包含 `no-spec`，例如 `codex/no-spec-admin-copy`。
- 紧急修复使用 `hotfix/<short-slug>`，事后按实际等级补记录或补 spec。
- 如果团队已有任务分支命名，以团队分支为准，但 PR 描述必须能关联到 spec 或说明 `S0/no-spec` 原因。

### 提交信息

使用标准 `git add` + `git commit` 提交，只暂存本次变更涉及的文件。

提交标题使用项目现有的 conventional commits 风格，并在有 spec 时带上 `spec-id`：

```text
<type>(<scope>): <summary> [<spec-id>]
```

示例：

```text
docs(specs): define runtime capability policy behavior [runtime-260513-1949]
feat(runtime): reload policy on assistant restart [runtime-260513-1949]
feat(admin): show restart-required state [runtime-260513-1949]
test(runtime): verify dangerous tool denial after restart [runtime-260513-1949]
docs(specs): update specs submission rules
```

`type` 使用提交历史中已有的 `feat`、`fix`、`docs`、`test`、`refactor`、`chore`、`build`、`revert`；`scope` 可省略，优先使用固定模块名。

不允许使用无法追溯的提交信息，例如：

- `fix`
- `update`
- `wip`
- `misc`
- `改了一些东西`

### 提交正文

`S2`、`S3`，以及涉及 runtime、security、workspace、deploy、storage 的提交，建议写提交正文：

```text
Spec:
Behaviors:
Evidence:
Risk:
Rollback:
Tests:
```

示例：

```text
Spec: specs/runtime-260513-1949/
Behaviors: B1, B3, B5
Evidence: admin screenshot, runtime log
Risk: running assistant may still hold old policy before restart
Rollback: restore previous policy and restart affected assistants
Tests: pytest tests/runtime/test_policy_reload.py
```

Codex 自动生成的提交可以补充：

```text
Generated-by: Codex
```

### 提交粒度

- `S0`：可以一个提交完成，但提交或 PR 必须说明目标和验证方式。
- `S1`：spec 和代码可以同一个 PR；小改动可以同一个提交，但必须能看出目的和验证方式。
- `S2` / `S3`：优先拆成 spec、实现、测试、证据/文档提交。
- 纯需求讨论：只提交 `PRODUCT.md` / `TECH.md`。
- 纯文档/spec 规划：只提交 spec 或规划文档，不混入代码。
- 不允许把无关格式化、重命名、清理和功能实现混在同一个提交里。

推荐的 `S2` / `S3` 顺序：

```text
1. docs(specs): define runtime capability policy behavior [runtime-260513-1949]
2. feat(admin): show affected assistants and restart-required state [runtime-260513-1949]
3. feat(runtime): reload policy on assistant restart [runtime-260513-1949]
4. test(runtime): cover dangerous tool denial after restart [runtime-260513-1949]
5. docs(specs): update validation evidence [runtime-260513-1949]
```

### Spec 状态与提交关系

| 状态          | 允许的提交                                                 |
| ------------- | ---------------------------------------------------------- |
| `draft`       | 只允许 spec 修改，不建议写业务代码                         |
| `ready`       | 允许进入实现                                               |
| `blocked`     | 只允许补充说明、解除阻塞或调整方案                         |
| `implemented` | 允许补测试、补证据、修小问题                               |
| `verified`    | 只允许修文档或追加验收记录；行为变化必须重新进入 spec 流程 |

状态从 `ready` 变成 `implemented`，必须有代码实现提交。状态从 `implemented` 变成 `verified`，必须有验证证据。

`S2` / `S3` 状态从 `ready` 进入 `implemented` 时，必须在 `TECH.md` 或 `TEST_REPORT.md` 记录实现提交。`S2` / `S3` 状态从 `implemented` 进入 `verified` 时，必须更新 `TEST_REPORT.md`，并记录验收分支、基线 commit、目标 commit、验证命令和行为覆盖结论。

### 重复任务检查

从旧 spec 或旧 `TEST_REPORT.md` 继续拆任务前，必须先做重复任务检查：

1. 检查当前分支是否已经包含相关实现或集成提交。
2. 检查 `TEST_REPORT.md` 的验收目标 commit 是否被当前分支 HEAD 包含。
3. 检查后续 spec 是否已经修正旧报告中的门禁失败、构建失败或验收阻塞。
4. 检查旧失败项是否仍然能在当前代码中复现。
5. 如果报告过期，先创建 `spec-sync` / `re-acceptance` 任务，不直接派发实现任务。

重复任务检查的输出必须明确列出：

- 已完成且不应重复派发的行为编号。
- 仍未实现的行为编号。
- 已实现但缺少验收证据的行为编号。
- 需要重新验收而不是重新实现的行为编号。

### Spec 与代码同步

- 如果代码改变了产品行为，必须同步更新 `PRODUCT.md`。
- 如果实现方案、风险、验证方式变化，必须同步更新 `TECH.md`。
- 如果实现、集成或门禁修复改变了验收结论，必须同步更新 `TEST_REPORT.md`。
- 如果一个后续 spec 修复了旧 spec 中记录的阻塞项，必须回写旧 `TEST_REPORT.md`，至少说明“该旧结论已被哪个 spec/commit 修正”。
- 如果只交付一部分，必须在 PR 或 `TECH.md` 中写清楚未交付范围。
- PR 合并前，代码、spec、测试证据必须一致。

### Specs 流程文档入基线

`specs/README.md`、`specs/_template/**`、团队 specs skill 或防重规则这类流程文档，会影响后续所有任务派发，应优先进入长期基线分支，例如 `feat/go`。

- 如果流程文档修正在业务实现分支上产生，不能直接把该业务分支合入长期基线。
- 应从最新长期基线新建纯文档分支，只 cherry-pick 流程文档提交。
- 纯文档分支只允许包含 `specs/**`、`.agents/skills/**` 或明确的团队流程文档。
- 不得为了合入流程规则，顺带合入 admin、runtime、workspace、deploy 或 UI 实现提交。
- 新的实现、复验或集成任务，应优先基于已经包含最新 specs 流程规则的长期基线创建。

### PR 标题和描述

有 spec 的 PR 标题建议：

```text
[runtime-260513-1949] runtime: capability policy requires assistant restart
```

无 spec 的 `S0` 小任务：

```text
[no-spec] docs: fix admin setup typo
```

紧急修复：

```text
[hotfix] security: block unsafe exec approval bypass
```

PR 描述至少包含：

- Spec 路径或 `S0/no-spec` 原因。
- 变更模块。
- 未包含范围。
- 行为编号和验证证据。
- 测试命令和结果；未运行测试时写明原因。
- 风险与回滚方式。

### Codex 自动提交

- Codex 必须在独立 branch 或 worktree 中工作。
- Codex 可以提交 commit、打开 PR、修复 CI。
- Codex 不得自动 merge。
- Codex 每次提交必须关联 spec，或说明 `S0/no-spec` 原因。
- Codex 修复 review/CI 问题时，只能做最小修改。
- Codex 不得把无关格式化、重命名、大范围清理混入功能提交。
- 涉及 security、runtime、workspace、deploy、storage 的提交必须等待人工最终确认。

### 回滚提交

回滚提交标题建议：

```text
revert(runtime): revert policy restart-required rollout [runtime-260513-1949]
```

回滚提交正文必须写：

```text
Reverts:
Reason:
Rollback evidence:
Follow-up:
```

涉及 runtime、security、deploy、storage 的回滚，还必须在 `TECH.md` 或 PR 中补充：

- 如何回滚。
- 是否需要重启容器。
- 是否需要恢复数据库或文件。
- 如何证明回滚成功。

## 评审检查项

- spec 使用 `B1`、`B2`、`B3` 这类行为编号。
- `TECH.md` 把每个行为编号映射到验证证据。
- 实际交付范围不能大于真实实现范围。
- 如果只交付一部分，必须写清后续事项。
- 引用已有事实源文档，不重复复制大段内容。
