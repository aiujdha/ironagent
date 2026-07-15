# IronAgent

IronAgent 是一套企业级 AI 助手系统，由控制面（Admin、Security Service、Go Gateway 统一网关）和 runtime 执行面（WebUI、助手运行时）组成。

当前项目不是单一前端或单一后端仓库，实际由两条代码线共同交付：

| 分支 | 职责 | 主要产物 |
|---|---|---|
| `main` | 控制面主线（含 Go Gateway 与 K8s 部署） | Admin、Security Service、Go Gateway、单端口入口、callback/fallback、K8s overlay、助手管理、审批、审计、市场、工作空间元数据 |
| `feat/webui` | runtime 执行面线 | 聊天 UI、工作台、runtime Gateway（`ironagent-gateway`）、助手运行时镜像 |

Go Gateway 与 K8s 部署最初在 `feat/go` 落地，现已合入 `main` 并在 `main` 上持续开发；`feat/webui` 是独立历史线，不能当作普通 feature branch 直接整体合并，只能按功能边界选择性对齐。

## 当前标准架构

```text
上游系统 / 小熊猫 IM
  |
  |  HTTPS/HTTP（public 接口免鉴权头，需部署侧开启 GO_GATEWAY_PUBLIC_AUTH_DISABLED=true）
  v
Go Gateway
  |-- /api/v1/chat
  |-- /api/v1/tasks/{task_id}
  |-- /assistants/{assistant_id}/ui/
  |-- callback / fallback / presigned file URL
  |
  +--> Admin API
  +--> Security Service / hooks / approval
  +--> runtime 执行面（WebUI / assistant runtime）
  +--> MySQL / workspace metadata
```

当前 K8s 标准部署目标：

- 对外只暴露 Go Gateway 一个入口，默认 `NodePort 30808`。
- `admin`、`security-service`、`ironagent-gateway`、`mysql` 不直接对公网暴露。
- 助手运行时采用 `K8s Pod + PVC subPath`。
- 每个助手拥有独立工作空间目录：`assistant-workspaces/assistants/<assistant_id>/`。
- 助手容器内看到的工作目录是 `/workspace`。
- MySQL 作为 K8s 内部依赖部署，保存控制面核心数据。
- 工作空间对象后端当前默认 `local`，后续可通过配置切到 NAS/OBS/COS 方案。

## 模块边界

| 模块 | 对外职责 | 不应该承担的职责 |
|---|---|---|
| Go Gateway | 上游统一入口、内部 scope 鉴权（API key + HMAC 防重放）、chat/task、callback、fallback、runtime proxy | 不直接实现业务审批策略，不直接暴露内部服务 |
| Admin | 管理后台、助手 CRUD、模型配置、市场、审计、用户绑定、工作空间列表 | 不作为公网 API 入口 |
| Security Service | hooks、安全策略、审批、文件路由、Control UI relay | 不直接暴露公网 NodePort |
| runtime WebUI | 聊天页、工作台 UI、助手交互体验 | 不直接连接后台私有接口 |
| 助手 runtime Pod | 执行模型交互和工具调用，挂载自己的 `/workspace` | 不共享其他助手目录，不直接暴露公网端口 |

## 文档入口

新人、测试、运维和 AI Agent 接手前，按这个顺序阅读：

1. `docs/START_HERE.md`
2. `docs/PROJECT_LOGIC_SOURCE_OF_TRUTH.md`
3. `docs/AI_CODING_RULES.md`
4. `docs/接口契约.md`
5. `docs/事件契约.md`
6. `docs/状态机.md`
7. `docs/ironagent-k8s-single-entry-full-deploy-runbook.zh-CN.md`
8. `docs/archive/historical/ironagent-full-flow-test-report-2026-05-07.zh-CN.md`（历史验收报告，选读）
9. `docs/go-gateway/Go-Gateway-全流程测试手册.md`
10. `go-gateway/docs/observability-runbook.md`

接口、事件、状态机必须分开维护；不要把字段、事件含义和状态流转混写进一个大文档。

## 参与贡献

开始开发前请阅读 `CONTRIBUTING.md`。安全漏洞请按 `SECURITY.md` 私密报告，不要提交包含漏洞细节、凭据、私有 URL 或客户数据的公开 Issue。

准备公开镜像时还必须完成 `docs/OPEN_SOURCE_READINESS.md` 中的发布阻断项，尤其是凭据轮换、Git 历史清理和许可证确认。

## 许可证

本项目使用 [Apache License 2.0](LICENSE)。

## 部署入口

当前推荐 K8s 部署入口：

```bash
deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport
```

渲染前必须确认目录存在：

```bash
test -f deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport/kustomization.yaml
kubectl kustomize deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport \
  > /tmp/ironagent-prod-k8s-single-entry-nodeport.yaml
```

如果 `kubectl kustomize` 报 `not a valid directory` 或 `evalsymlink failure`，说明目标服务器代码版本没有包含 K8s runtime 单入口 overlay。不要继续部署，先同步包含该 overlay 的 `main` 最新代码。

部署到其他服务器时必须修改：

- 公网 IP / 域名：`GO_GATEWAY_PUBLIC_BASE_URL`、`IRON_AGENT_CONTROL_UI_PUBLIC_URL`、`IRON_AGENT_GO_GATEWAY_PUBLIC_URL`
- 允许来源：`IRON_AGENT_CONTROL_UI_ALLOWED_ORIGIN_REGEX`、`IRON_AGENT_GATEWAY_ALLOWED_ORIGINS`
- 本地 PV 节点和路径：`workspace-local-pv.yaml`、`mysql-local-pv.yaml`
- 镜像 tag：`runtime-k8s-pvc/kustomization.yaml`
- Secret：`control-plane-shared-secret`、`ironagent-gateway-secret`、`go-gateway-secret`

## 镜像

当前全量部署需要这些镜像：

| 镜像 | 来源分支 | 用途 |
|---|---|---|
| `localhost/ironagent-admin:<tag>` | `main` | Admin 后台 |
| `localhost/ironagent-security-service:<tag>` | `main` | 安全策略、hooks、审批、文件路由 |
| `localhost/go-gateway:<tag>` | `main` | 单端口统一入口 |
| `localhost/ironagent:<tag>` | `feat/webui` | runtime Gateway、WebUI、助手 runtime |
| `mysql:8.4` 或 `localhost/mysql:8.4` | 官方镜像 | K8s 内部 MySQL |

如果 `feat/webui` 有变更，必须重打 `ironagent` 镜像；只重打后端镜像无法更新聊天 UI 和助手 runtime 代码。

## 本地验证命令

```bash
uv run python -m compileall src
uv run pytest tests/unit/test_admin_web_app.py \
  tests/unit/test_assistant_service.py \
  tests/security/test_capability_enforce.py \
  tests/unit/test_gateway_links.py \
  tests/unit/test_workspace_file_service.py

(cd go-gateway && go test ./...)

kubectl kustomize deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport \
  > /tmp/ironagent-prod-k8s-single-entry-nodeport.yaml

bash scripts/check_single_entry_exposure.sh
```

## 功能验收重点

全流程验收必须覆盖：

- Go Gateway `/healthz`、`/readyz`、`/api/v1/chat`、`/api/v1/tasks/{task_id}`
- 上游 public 接口免鉴权头直通（部署侧 `GO_GATEWAY_PUBLIC_AUTH_DISABLED=true`）；内部 `admin/runtime/debug/proxy` scope 保留 API key 鉴权
- callback 推送与 fallback 查询
- 后台登录、模型配置、助手创建、助手重启、助手删除
- 新建助手进入 `/assistants/{assistant_id}/ui/`
- 助手 Pod 创建、重启后恢复、workspace PVC subPath 挂载
- `/workspace/persistent`、`/workspace/temp` 文件上传、下载、删除
- 助手列表、数据库记录、工作空间目录一致性
- 单端口暴露检查：公网只开放 Go Gateway

## AI 编码规则

AI 或开发者修改代码前必须阅读：

- `docs/AI_CODING_RULES.md`
- `docs/PROJECT_LOGIC_SOURCE_OF_TRUTH.md`
- `AGENTS.md`

核心约束：

- 只做当前任务最小必要修改。
- 禁止无关重构、批量格式化、顺手优化。
- 涉及接口、事件、状态机、部署配置时，必须同步更新对应文档。
- 线上热修只能短期验证；验证通过后必须回写代码、重打镜像、移除热修 ConfigMap。
- 文件元数据和覆盖/删除审批 pending 请求必须放在共享 PVC，例如 `IRON_AGENT_WORKSPACE_FILE_SQLITE_PATH=/workspace-root/control-plane/data/workspace_files/workspace-files.sqlite`、`IRON_AGENT_WORKSPACE_PUSH_REQUESTS_STORE_PATH=/workspace-root/control-plane/data/workspace_files/push_requests.json`；不要放在容器本地 `/app/...`。
