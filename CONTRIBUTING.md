# Contributing to IronAgent

感谢你参与 IronAgent。这个仓库包含 Python 控制面、Go Gateway 和部署清单；修改前请先确认组件边界，避免把 northbound、southbound 和 runtime 逻辑混在同一个改动中。

## 开始之前

1. 阅读 `docs/START_HERE.md`。
2. 阅读 `docs/PROJECT_LOGIC_SOURCE_OF_TRUTH.md` 和 `docs/AI_CODING_RULES.md`。
3. 涉及接口、事件或状态时，继续阅读对应的契约文档。
4. 对安全问题使用 GitHub 的私密漏洞报告，不要提交公开 Issue。

## 开发环境

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/) 和 Go 1.25+。

```bash
uv sync --dev
uv run pytest tests -q
uv run ruff check src tests migrations scripts

cd go-gateway
go test -count=1 ./...
go vet ./...
```

完整的本地启动流程见 `docs/LOCAL_STARTUP.md`。部署配置中的 `REPLACE_ME` 只能在本地或 Secret 管理系统中替换，禁止提交真实值。

## 提交变更

- 一个 PR 只解决一个明确问题。
- 优先增加能复现问题或证明行为的测试。
- 不做无关重构、批量格式化、重命名或注释清理。
- 接口、事件、状态机、系统边界或启动方式变化时，同步更新对应事实源文档。
- PR 标题使用动词描述结果，例如 `fix: reject expired callback nonces`。
- PR 描述必须说明原因、范围、验证证据、风险和回滚方式。

建议使用 `fix/`、`feat/`、`docs/` 或 `chore/` 作为分支名前缀。默认采用 squash merge，提交历史应当让审查者容易理解，而不是记录本地试错过程。

## Pull Request 检查

提交前至少执行与改动相关的检查：

```bash
git diff --check
uv run ruff check <本次修改的 Python 文件>
uv run pytest tests -q  # 修改 Python 时
make check-go      # 修改 Go Gateway 时
make check-specs   # 修改 specs 时
```

部署改动还必须渲染目标 Kustomize overlay，并运行 `bash scripts/check_single_entry_exposure.sh`。如果某项检查无法执行，请在 PR 中写明原因，不要直接删除检查项。

维护者可能要求拆分包含多个主题的 PR。涉及安全默认值的变更必须 fail closed，并明确兼容性和迁移影响。
