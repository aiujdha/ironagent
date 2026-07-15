# Specs Rollout Planning

本文件记录 specs 流程的落地规划和后续演进。正式执行规则以 `specs/README.md` 和根目录 `AGENTS.md` 为准。

## 当前口径

项目采用 `S0/S1/S2/S3` 分级、`PRODUCT.md` / `TECH.md` 行为契约、验证证据闭环的轻量 specs 流程。

核心目标不是增加文档负担，而是在高风险、跨模块、权限、安全、runtime、workspace、deploy、持久化变更前先明确：

- 要解决的问题。
- 影响模块。
- 必须满足的行为。
- 风险和回滚方式。
- 验证证据。

## 已落地内容

| 文件                                            | 作用                          |
| ----------------------------------------------- | ----------------------------- |
| `specs/README.md`                               | specs 正式规则和提交范式      |
| `specs/_template/PRODUCT.md`                    | 产品行为模板                  |
| `specs/_template/TECH.md`                       | 技术方案模板                  |
| `.agents/skills/requirement-clarifier/SKILL.md` | 需求澄清 skill                |
| `AGENTS.md`                                     | Codex 进入仓库后的 specs 约束 |
| `specs/runtime-260513-1949/`                    | 能力策略生效规则试点 spec     |
| `scripts/spec_lint.py`                          | spec lint 脚本，本地经 `make check-specs` 执行 |

## 执行原则

- `S0` 不创建 spec 文件，但回复或 PR 必须说明目标、影响和验证方式。
- `S1/S2/S3` 使用 `specs/<module>-<YYMMDD>-<HHMM>/PRODUCT.md` 和 `TECH.md`。
- `S2/S3` 必须复核到 `ready` 后才能进入实现。
- specs-driven work 的分支、提交、PR 和 Codex 自动提交规则以 `specs/README.md` 为准。
- 用户确认“进入实现”前，不修改业务代码。
- 如果代码事实和 `TECH.md` 冲突，先停止并报告。
- 行为变化更新 `PRODUCT.md`。
- 技术方案变化更新 `TECH.md`。
- 权限、安全、runtime、workspace、deploy、storage、线上变更必须人工确认。

## 当前试点结论

能力策略第一版采用“重启后生效”：

- 未运行助手下次启动时加载最新策略。
- 运行中的助手不宣称热生效。
- 有活跃任务时等待当前任务完成后再重启，不立即中断任务。
- 第一版由 runtime 按重启后加载的策略执行。
- Security Service 第一版只保留审批和审计，不作为最终集中拦截点。
- 如果后续要做 Security Service 或 Gateway 集中拦截，单独开新的 S3 spec。

## 后续待办

| 事项                             | 建议时机                         |
| -------------------------------- | -------------------------------- |
| 在已有 PR/MR 模板中增加 specs 字段（模板已存在，尚无 specs 字段） | 提交范式使用稳定后               |
| spec-lint 接入 CI（脚本 `scripts/spec_lint.py` 已存在，`.gitlab-ci.yml` 尚未调用） | specs 使用稳定后                 |
| Codex prompts                    | 需要自动检查 spec/PR 同步时      |
| CODEOWNERS 约束 specs 高风险目录 | 团队 owner 明确后                |
| 更多团队 skill                   | 具体模块重复排障或验收流程稳定后 |

## 需要人工确认的点

- 是否进入具体 spec 的实现阶段。
- `chat/onlyread/onlywrite/dangerous` 等能力策略映射。
- 后端真实源码位置或容器读取方式。
- 是否为多轮任务创建 `PROJECT_LOG.md`。
- 是否在已有 PR/MR 模板中增加 specs 字段、将 spec-lint 接入 CI。
