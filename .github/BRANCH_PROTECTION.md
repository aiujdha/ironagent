# 分支保护建议

本文档用于配置代码仓库分支保护。GitLab 与 GitHub 镜像应使用等价规则；公开 GitHub 仓库发布前必须配置 ruleset 或 branch protection。

## 1. 保护分支

建议保护：

- `main`
- `feat/go`
- `deploy/*`
- `release/*`

## 2. GitLab 必选规则

在 GitLab 中配置 protected branches：

| 配置项 | 要求 |
| --- | --- |
| Allowed to merge | Maintainers 或指定 reviewer 组 |
| Allowed to push | No one |
| Force push | Disabled |
| Code owner approval | Enabled，如果启用 CODEOWNERS |
| Require pipeline success | Enabled |
| Require all discussions resolved | Enabled |

## 3. GitHub 等价规则

对受保护分支启用：

- Require a pull request before merging。
- Require approvals，至少 1 人 review；生产分支建议 2 人。
- Dismiss stale pull request approvals when new commits are pushed。
- Require conversation resolution before merging。
- Require status checks to pass before merging。
- Require branches to be up to date before merging。
- Do not allow force pushes。
- Do not allow deletions。

## 4. 推荐必过检查

GitHub Actions 必选状态检查：

- `Python / lint and test`
- `Go Gateway / vet and test`
- `Secrets / full history`

Go Gateway 相关改动：

- `cd go-gateway && go test -count=1 ./...`
- `cd go-gateway && go vet ./...`

Python control plane 相关改动：

- 运行仓库已有 Python 测试。
- 如果存在 lint/type check 工作流，必须通过。

部署清单相关改动：

- `kubectl kustomize deployment/kustomize/ironagent/overlays/prod-nodeport`
- `kubectl kustomize deployment/kustomize/ironagent/overlays/prod-ingress`

文档相关改动：

- `git diff --check`
- 验证文档中的关键路径和命令仍存在。

## 5. 管理员绕过

管理员绕过只允许用于紧急修复。绕过后必须补交 PR 记录：

- 绕过原因。
- 涉及 commit。
- 已执行检查。
- 回滚方式。

## 6. 合并策略

建议：

- 默认使用 squash merge。
- PR 标题必须描述用户可见或系统可见结果。
- 禁止把多个无关主题塞进一个 PR。
- 架构方向变化必须先更新 `docs/PROJECT_LOGIC_SOURCE_OF_TRUTH.md`。
