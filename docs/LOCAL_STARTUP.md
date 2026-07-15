# 本地统一启动手册

本文档是本仓库本地启动的统一入口。不要每个人自行摸索不同命令；如果启动方式变化，先更新本文档。

## 1. 前置条件

需要：

- Python 3.11+
- Go 1.25+
- Docker Desktop 或兼容 Docker Engine
- `uv`
- `curl`

可选：

- MySQL，用于验证 Gateway task store、event outbox、runtime registry 持久化。
- Redis，用于验证 chat 限流、assistant 缓存、任务进度缓存等路径。Redis 是可选配置（`GO_GATEWAY_REDIS_REQUIRED` 默认 false，不配置 Redis 也能启动 Gateway）；需要时设置 `GO_GATEWAY_REDIS_ADDR`，完整变量见 `go-gateway/docs/observability-runbook.md` §9。

## 2. 启动 Python southbound

统一默认方式：Docker Compose。

除非正在定位框架级问题，否则不要绕过 Compose 手工分别启动 `security-service` 与 `admin`。这样可以保证 MySQL、端口、挂载路径、Docker socket 和环境变量一致。

首次启动前，先在仓库根目录准备 `.env`（`docker-compose.dev.yml` 的 `admin` 服务通过 `env_file` 引用它，干净 checkout 下缺这个文件会在 compose 解析阶段直接报 `env file .env not found`）：

```bash
cp .env.example .env
```

按 `.env.example` 内注释把 `IRON_AGENT_STACK_ROOT`、`IRON_AGENT_RUNTIME_SOURCE_ROOT` 等绝对路径填成本机实际值。

```bash
export IRON_AGENT_STACK_ROOT="$PWD"
export IRON_AGENT_RUNTIME_SOURCE_ROOT=<absolute-ironagent-source-root>
export IRON_AGENT_INTERNAL_SERVICE_TOKEN=change-me

docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build mysql security-service admin
```

`IRON_AGENT_RUNTIME_SOURCE_ROOT` 必须是绝对路径，并且必须指向可用于助手 runtime 的 IronAgent 源码目录。

如果修改了数据库 schema，启动后执行：

```bash
docker compose exec admin sh -lc '
  cd /app &&
  PYTHONPATH=/app \
  .venv/bin/alembic -c migrations/alembic.ini upgrade head
'
```

迁移自动化说明：

- dev overlay 的 `admin` 启动命令本身会先执行 `alembic upgrade head` 再启动服务；正式 `admin` 镜像入口也会在 `IRON_AGENT_AUTO_MIGRATE=1`（默认值）时自动迁移，仅当需要把迁移作为单独受控步骤执行时才设为 `0`。
- `security-service` 镜像打包了 `migrations/` 仅供诊断，不会自动执行迁移，避免两个服务在 schema 变更上竞态。

### 2.1 与 Makefile 启动目标的关系

仓库根目录的 `Makefile` 提供了另一组入口，与本文档的关系如下：

- `make up` / `make down` / `make ps` / `make build`：只使用基础 `docker-compose.yml`（不带 `docker-compose.dev.yml` overlay），以 `-d` 后台启动 `security-service` 与 `admin`（MySQL 作为依赖自动拉起），并自动探测 `IRON_AGENT_RUNTIME_SOURCE_ROOT`（在 `../ironagent-main` 等常见相邻目录中查找）、附带 `IRON_AGENT_CONTROL_UI_PUBLIC_URL` 等默认值。适合不改控制面代码、只需要把控制面跑起来的场景。
- 本文 §2 的带 dev overlay 的 compose 命令：把宿主机仓库挂载进容器并以 `--reload` 启动，适合开发调试 Python southbound 代码，是本文档的统一默认方式。
- `make dev` / `make admin`：在宿主机直接用 uvicorn 启动 Python 服务，属于 §8 禁止作为团队默认方式的“绕过 Docker Compose 分别启动”，仅限框架级排障时临时使用。

## 3. 启动 Go Gateway

统一默认方式：上游 public 接口不鉴权，内部 `admin/runtime/debug/proxy` scope 保留 API key。

```bash
cd go-gateway

GO_GATEWAY_LISTEN_ADDR=:8080 \
GO_GATEWAY_MODE=polling \
GO_GATEWAY_SECURITY_BASE_URL=http://127.0.0.1:8000 \
GO_GATEWAY_ADMIN_BASE_URL=http://127.0.0.1:8501 \
GO_GATEWAY_INTERNAL_SERVICE_TOKEN=change-me \
GO_GATEWAY_DATABASE_URL='mysql+pymysql://ironagent:ironagent@127.0.0.1:3307/ironagent?charset=utf8mb4' \
GO_GATEWAY_SCOPED_API_KEYS='runtime-key:runtime;debug-key:debug;admin-key:admin;proxy-key:proxy' \
GO_GATEWAY_PUBLIC_AUTH_DISABLED=true \
GO_GATEWAY_CALLBACK_SECRET=dev-callback-secret \
GO_GATEWAY_CALLBACK_ALLOWED_HOSTS=127.0.0.1,localhost \
GO_GATEWAY_CALLBACK_ALLOW_PRIVATE_HOSTS=true \
GO_GATEWAY_PRESIGN_SECRET=dev-presign-secret \
GO_GATEWAY_RUNTIME_BOOTSTRAP_ASSISTANTS=demo-main \
GO_GATEWAY_RUNTIME_SYNC_INTERVAL=5s \
GO_GATEWAY_LOG_LEVEL=debug \
go run ./cmd/server
```

如果要同时验证 `demo-main` 与 `demo-approval`，必须先确认两个 assistant 都真实存在且 runtime 为 `running`，再设置：

```bash
GO_GATEWAY_RUNTIME_BOOTSTRAP_ASSISTANTS=demo-main,demo-approval
```

不要把不存在或未启动的 assistant 写进 bootstrap 列表。

## 4. 健康检查

```bash
curl -sS http://127.0.0.1:8000/health
curl -sS http://127.0.0.1:8501/health
curl -sS http://127.0.0.1:8080/healthz
curl -sS http://127.0.0.1:8080/readyz
```

理想结果：

- `security-service` health 返回 200。
- `admin` health 返回 200。
- `/healthz` 返回 200。
- `/readyz` 在 southbound 和 runtime registry 可用时返回 200。
- `/api/v1/health` 返回各 component 状态。

## 5. 上游请求方式

上游调用 public 接口不需要鉴权 Header：

```bash
curl -sS http://127.0.0.1:8080/readyz
```

## 6. 最小 Chat smoke

```bash
curl -sS -X POST http://127.0.0.1:8080/api/v1/chat \
  -H 'Content-Type: application/json' \
  --data-binary '{
    "request_id": "local-smoke-001",
    "user_id": "e2e-main-0416",
    "conversation_id": "local-conv-001",
    "message": "Summarize the document in one sentence.",
    "target_assistant_id": "demo-main",
    "input_object_id": "f4fcace4-cf87-44e8-bfcd-28a90e6035ba"
  }'
```

理想结果：

- 返回 `task_id`。
- 返回统一 `status` 和 `stage`。
- 返回 `fallback_query_url`。
- 不返回 `claim_token`、`input_object_id`、Python southbound URL。

查询：

```bash
curl -sS http://127.0.0.1:8080/api/v1/tasks/<task_id>
```

## 7. 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `/readyz` 返回 503，`southbound_security=down` | `security-service` 未启动或地址错误。 | 检查 `GO_GATEWAY_SECURITY_BASE_URL`，确认 8000 端口可访问。 |
| `/readyz` 返回 503，`southbound_admin=down` | `admin` 未启动或地址错误。 | 检查 `GO_GATEWAY_ADMIN_BASE_URL`，确认 8501 端口可访问。 |
| `/readyz` 返回 503，runtime 为空 | 没有 runtime heartbeat 或 bootstrap。 | 本地设置 `GO_GATEWAY_RUNTIME_BOOTSTRAP_ASSISTANTS=<running assistant>`。 |
| `invalid api key` | 请求打到了内部 `admin/runtime/debug/proxy` scope，或显式关闭了 public 无鉴权。 | 上游只调用 public 接口；内部接口使用对应 scope 的 API key。 |
| callback URL 被拒绝 | host 不在 allowlist 或是私网地址。 | 本地设置 allowlist 和 `GO_GATEWAY_CALLBACK_ALLOW_PRIVATE_HOSTS=true`；生产不要打开 private hosts。 |
| 任务长期 `processing/tool_running` | Gateway 已完成 southbound 编排，但 runtime 执行侧未回写 complete/fail。 | 本地 smoke 可视为编排链路通过；完整闭环需要 runtime 执行侧配合。 |

## 8. 禁止的启动方式

禁止把以下方式作为团队默认启动方式：

- 随机端口启动 `uvicorn`。
- 把上游 public 接口改回 API key/HMAC 必填。
- 绕过 Docker Compose 分别启动 Python 服务。
- 使用进程内 Go Gateway store 代替 MySQL 做联调验收。
- 每个人维护自己的私有启动脚本但不提交到仓库。

如果确实需要临时偏离，必须在 PR 或排障记录中写明原因、命令和恢复方式。

## 9. 后续文档

- 全流程测试：`docs/go-gateway/Go-Gateway-全流程测试手册.md`
- 接口契约：`docs/接口契约.md`
- 事件契约：`docs/事件契约.md`
- 状态机：`docs/状态机.md`
- 部署手册：`docs/go-gateway/integration-and-production-deployment.md`
