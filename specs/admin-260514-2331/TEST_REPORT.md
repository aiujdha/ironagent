# admin-260514-2331 TEST_REPORT

## 当前验收状态汇总

| 项目 | 结论 |
| ---- | ---- |
| 报告状态 | 当前重新验收后的证据汇总 |
| 验收日期 | 2026-05-18 |
| 集成分支 | `codex/admin-260514-2331-reaccept-final` |
| 重新验收起点 HEAD | `9249130b793f80dfe83b32b4759dff33fce6f009` |
| 当前边界 | 本报告汇总已提交证据、round2 证据和当前 workspace sync 重新验收；未合并 `main` 或 `feat/go`，未触碰真实环境。 |
| 写回边界 | `PRODUCT.md` / `TECH.md` 仍保持 `ready`；本次纳入 workspace sync 源码改动、针对性测试和验收证据。 |

## 本次重新验收纳入范围

本次重新验收覆盖此前遗留的源码与证据日志改动：

- `src/admin_web/static/index.html`
- `src/security_service/file_routes.py`
- `output/admin-260514-parallel/b9-b10-browser/service-logs/admin-rerun-18212.log`
- `tests/unit/test_admin_web_app.py`
- `tests/unit/test_workspace_file_routes_extensions.py`
- `output/admin-260514-reaccept-current/`

`output/doc/`、`tmp/docs/` 仍为无关未跟踪文件，不纳入本报告。

## 证据目录

| 范围 | 目录 |
| ---- | ---- |
| B2/B5/B6 | `output/admin-260514-parallel/lifecycle-b2-b5-b6/` |
| B9/B10 round 1 | `output/admin-260514-parallel/b9-b10-browser/` |
| B9 round 2 | `output/admin-260514-parallel-round2/b9-healthy-fallback/` |
| B10 round 2 | `output/admin-260514-parallel-round2/b10-dialog-contract/`; `output/admin-260514-parallel-round2/b9-b10-code-contract/` |
| B10 non-advancing audit lane | `output/admin-260514-parallel-round2/b10-ui-lifecycle-audit/` |
| B11 | `output/admin-260514-parallel/runtime-health-sync/` |
| B12 | `output/admin-260514-parallel/lifecycle-audit-b12/` |
| B39 round 1 | `output/admin-260514-parallel/b39-docker-verify/` |
| B39 round 2 | `output/admin-260514-parallel-round2/b39-docker-rerun/` |
| workspace sync reaccept | `output/admin-260514-reaccept-current/` |
| expert round3 | `output/admin-260514-expert-round3/` |

## 当前重点 B 项状态

| B | 最新状态 | 证据 |
| ---- | ---- | ---- |
| B2 | verified | `lifecycle-b2-b5-b6/admin-create-unknown-user-404.json` 返回 HTTP 404；`data-store-checks.json` 显示未知用户没有自动创建 user/assistant/binding。 |
| B5 | verified | `delete-assistant-response.json` 返回 soft delete 结果，`runtime_disabled=true`、`workspace_removed=false`、`state_removed=false`；workspace/state 前后树一致。 |
| B6 | verified | `public-chat-soft-deleted-error.json`、`public-upload-soft-deleted-error.json`、`public-chat-no-available-assistant-error.json` 均返回明确“用户无可用助手”错误。 |
| B9 | verified | round2 `sse-curl-response.txt` 证明 SSE 端点 200、`text/event-stream`、`fallbackPollMs=4000`；`proxy-network-log.jsonl` 和 `proxy-network-summary.json` 证明强制 SSE 503 后 `/api/v1/admin/bootstrap/fast` 连续 18 次 HTTP 200，间隔约 4 秒。 |
| B10 | partial verified | round2 `b10-dialog-contract/SUMMARY.md` 证明启动按钮经浏览器点击到达 start endpoint，函数级 harness 证明 start 不调用 confirm，stop/restart/delete 会调用对应 confirm 文案；`b10-ui-lifecycle-audit/SUMMARY.md` 记录该 audit lane 未捕获 UI-origin lifecycle 操作，不参与推进；当前 in-app Browser 不能捕获原生 dialog event，所以仍不是 native-dialog-event verified。 |
| B11 | verified | `runtime-health-sync/02-uvicorn.log` 出现 `assistant_runtime_health_reconciled checked=1 updated=1`；`04-after-assistants.json` 显示 stale runtime 被纠偏为 failed；`05-audit-absence.txt` 证明未写正式审计。 |
| B12 | verified | start/stop/restart/delete API 均 200；`audit-filter-assistant-lifecycle.json`、JSON/CSV export 和 `local-data/audit.jsonl` 均有 4 条 `assistant_lifecycle` 正式审计。 |
| B26/B30 workspace sync regression | verified for current reaccept scope | `output/admin-260514-reaccept-current/pytest-workspace-sync.log` 覆盖 admin workspace summary、security workspace file list、workspace sync service 和用户文件集成；新增单测证明 `sync=true` 会触发 workspace 同步，且同步失败不会阻断文件列表返回。 |
| B39 | verified | round2 `b39-docker-rerun/EVIDENCE.md` 证明真实 Docker build 成功，容器内 import/font smoke 成功，`import-font-smoke.json` 校验指定依赖版本，`font-list.log` 和 smoke JSON 均指向 WenQuanYi 字体路径，`import-font-smoke.png` 为本次容器内生成。 |

## 验证命令与证据

| 命令或证据 | 结果 |
| ---- | ---- |
| `git diff --check` | 当前重新验收通过，见 `output/admin-260514-reaccept-current/git-diff-check.log`。 |
| `rg -n '^(<<<<<<<\|=======\|>>>>>>>)' src tests specs output/admin-260514-reaccept-current output/admin-260514-parallel/b9-b10-browser/service-logs/admin-rerun-18212.log` | 当前重新验收通过，无冲突标记；`conflict-scan.log` 中 exit code 1 表示无匹配。 |
| `go test ./internal/httpapi ./internal/webhook`（在 `go-gateway/` 下运行） | round1 通过。 |
| `.venv/bin/python -m pytest tests/unit/test_admin_web_app.py tests/unit/test_assistant_service.py tests/unit/test_audit_repository.py -q` | round1 通过。 |
| `.venv/bin/python -m pytest tests/integration/test_user_file_integration.py -q` | round1 通过。 |
| `.venv/bin/python -m pytest tests/unit/test_admin_web_app.py -k 'status_stream or confirm or assistant_status' -q` | round2 `b9-b10-code-contract/pytest.log` 通过，2 tests passed。 |
| `.venv/bin/python -m pytest tests/unit/test_admin_web_app.py tests/unit/test_workspace_file_routes_extensions.py tests/unit/test_workspace_sync_service.py tests/integration/test_user_file_integration.py -q` | 当前重新验收通过，83 tests passed，见 `output/admin-260514-reaccept-current/pytest-workspace-sync.log`。 |
| `.venv/bin/python -m py_compile src/admin_web/app.py src/security_service/file_routes.py src/workspace_files/sync_service.py` | 当前重新验收通过，见 `output/admin-260514-reaccept-current/py-compile.log`。 |
| `/usr/local/bin/docker build -f docker/Dockerfile.assistants -t ironagent-assistants:b39-round2-6bc2cb9a .` | round2 `docker-build-summary.json` 记录 exit code 0。 |
| 容器内 `scripts/assistant_pydeps_smoke.py` | round2 `container-smoke.log` 和 `validation-summary.json` 记录 exit code 0。 |

## 状态推进判断

`PRODUCT.md` / `TECH.md` 继续保持 `ready`，不自动推进为 `verified`。本次重新验收只覆盖 workspace sync 源码改动与相关回归测试，仍不是 B1-B40 全量验收矩阵。

## Expert round3 补充

2026-05-18 的专家 round3 增加了 5 条并行后续任务输出：

- Lane 1 gateway control session：补齐 control-session 审批 resolve 的代码级助手范围校验，并默认不向浏览器返回真实内部服务凭证。
- Lane 2 runtime capability evidence：新增 runtime workspace-only skills/plugins 环境约束和 `ironagent.plugin.json` workspace 识别；`specs/runtime-260513-1949/TEST_REPORT.md` 明确为 partial/code-present。
- Lane 3 market/IronAgent plugin package compatibility：支持 IronAgent plugin archive 使用 `ironagent.plugin.json`，plugin install 严格要求该文件。
- Lane 4 admin workspace reuse/reload：workspace item reuse 后同步目标 scope，并返回 runtime reload status/error。
- Lane 5 governance：修正本轮 5 个真实并行任务和提示词，保留 verified 证据门槛。

本轮 round3 仍属于代码级补充和治理记录，不改变 `PRODUCT.md` / `TECH.md` 的 `ready` 状态；涉及 gateway/runtime 的验证分别写入对应 spec 的 `TEST_REPORT.md` 或输出目录，不把 admin 报告扩展为全量 B1-B40 verified。

## 后续 specs 关系

2026-05-18 新增 `specs/runtime-260518-1025/`，用于定义 runtime 状态一致性、freshness、多 Gateway registry、Admin SSE/fallback 和部署前提。本报告中的 B11 结论只覆盖旧的 admin health-check 纠偏证据，不证明 `runtime-260518-1025` 已 implemented 或 verified。

当前可推进的行为证据：

- B9 可从 partial verified 推进为 verified。
- B39 可从 blocked/unverified 推进为 verified。
- B10 仍保持 partial verified，缺原生 dialog event 证据。

## 无关本地文件

`output/doc/`、`tmp/docs/` 仍为无关未跟踪文件，不纳入本报告。
