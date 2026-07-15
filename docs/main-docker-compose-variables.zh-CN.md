# main 分支 `docker-compose.yml` 变量说明

## 适用范围

- 适用文件：`docker-compose.yml`
- 适用分支：当前 `main`
- 目标：给运维/部署人员说明每个变量的用途、默认值与取数来源

## 取数来源说明

- `外部传入`：建议通过仓库根目录 `.env` 或启动命令前的 shell 环境传入。
- `Compose 默认值`：如果外部未传入，`docker-compose.yml` 会直接采用默认值。
- `路径派生`：容器内变量由 `IRON_AGENT_STACK_ROOT` 拼接得到，不建议再单独在 Compose 里改写。
- `固定值`：当前 `docker-compose.yml` 里写死的值。

## 一、宿主机侧需要提供或可覆盖的 Compose 入参

### 1. 必填路径类

| 变量名 | 服务 | 默认值 | 含义 | 取数来源 |
| --- | --- | --- | --- | --- |
| `IRON_AGENT_STACK_ROOT` | `security-service`、`admin` | 必填，无默认值 | IronAgent 主仓库在宿主机上的绝对路径；同时用于数据目录、配置目录和代码目录挂载 | 外部传入 |
| `IRON_AGENT_RUNTIME_SOURCE_ROOT` | `security-service`、`admin` | 必填，无默认值 | IronAgent 源码目录在宿主机上的绝对路径；用于把 IronAgent 能力与技能目录挂进容器 | 外部传入 |
| `IRON_AGENT_HOST_IRON_AGENT_ROOT` | `security-service`、`admin` | `/tmp/ironagent-host-ironagent` | 宿主机上的 IronAgent 状态目录挂载点 | 外部传入，未传时用 Compose 默认值 |

### 2. MySQL 与端口映射

| 变量名 | 服务 | 默认值 | 含义 | 取数来源 |
| --- | --- | --- | --- | --- |
| `IRON_AGENT_MYSQL_PORT` | `mysql` | `3307` | 宿主机暴露的 MySQL 端口，映射到容器 `3306` | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_MYSQL_ROOT_PASSWORD` | `mysql` | `root` | MySQL root 密码；会写入容器内 `MYSQL_ROOT_PASSWORD` | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_MYSQL_DATABASE` | `mysql` | `ironagent` | 初始化数据库名；会写入容器内 `MYSQL_DATABASE` | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_MYSQL_USER` | `mysql` | `ironagent` | 业务连接用户名；会写入容器内 `MYSQL_USER` | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_MYSQL_PASSWORD` | `mysql` | `ironagent` | 业务连接密码；会写入容器内 `MYSQL_PASSWORD` | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_SECURITY_SERVICE_PORT` | `security-service` | `8000` | 安全服务对宿主机暴露的端口 | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_ADMIN_PORT` | `admin` | `8501` | Streamlit 管理台对宿主机暴露的端口 | 外部传入，未传时用 Compose 默认值 |

### 3. 核心运行与存储

| 变量名 | 服务 | 默认值 | 含义 | 取数来源 |
| --- | --- | --- | --- | --- |
| `IRON_AGENT_STORAGE_BACKEND` | `security-service`、`admin` | `mysql` | 主存储后端类型，当前推荐使用 MySQL | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_DATABASE_URL` | `security-service`、`admin` | `mysql+pymysql://ironagent:ironagent@mysql:3306/ironagent?charset=utf8mb4` | 应用连接 MySQL 的完整 SQLAlchemy 连接串 | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_APPROVAL_MODE` | `security-service`、`admin` | `disabled` | 审批链路模式；默认需要审批的操作直接放行，但 P0 硬拒绝仍保留；如需恢复人工审批可设为 `enabled` | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_ALLOWED_WORKSPACE_ROOTS_JSON` | `security-service`、`admin` | 空字符串 | 额外可选工作空间根目录列表，JSON 字符串格式 | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_DEFAULT_WORKSPACE_ROOT_ID` | `security-service`、`admin` | `default` | 默认工作空间根目录 ID | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_WORKSPACE_OBJECT_STORAGE_BACKEND` | `security-service`、`admin` | `local` | 工作区文件对象存储后端，可选 `local`、`cos`、`obs` | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_WORKSPACE_OBJECT_STORAGE_ROOT` | `security-service`、`admin` | `/workspace/ironagent/data/workspace_objects` | 容器内用于本地对象存储的根目录 | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_WORKSPACE_OBJECT_STORAGE_PREFIX` | `security-service`、`admin` | `workspace-files` | 工作区对象存储键前缀 | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_WORKSPACE_RESULT_SCAN_ENABLED` | `security-service`、`admin` | `true` | 是否启用工作区结果扫描 | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_WORKSPACE_RESULT_SCAN_INTERVAL_SECONDS` | `security-service`、`admin` | `10` | 工作区结果扫描间隔，单位秒 | 外部传入，未传时用 Compose 默认值 |

说明：

- 代码配置模型 `src/shared/config.py` 里 `workspace_result_scan_enabled` 的默认值是 `false`，但当前 `docker-compose.yml` 显式把它覆盖成了 `true`。
- 如果把 `IRON_AGENT_WORKSPACE_OBJECT_STORAGE_BACKEND` 改为 `cos` 或 `obs`，还需要同步提供下文的 COS/OBS 凭证参数。

### 4. 协作对象存储

| 变量名 | 服务 | 默认值 | 含义 | 取数来源 |
| --- | --- | --- | --- | --- |
| `IRON_AGENT_COLLABORATION_OBJECT_STORAGE_BACKEND` | `security-service`、`admin` | `cos` | 协作对象存储后端类型 | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_COLLABORATION_OBJECT_STORAGE_BUCKET` | `security-service`、`admin` | 空字符串 | 协作对象存储桶名 | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_COLLABORATION_OBJECT_STORAGE_REGION` | `security-service`、`admin` | 空字符串 | 协作对象存储地域 | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_COLLABORATION_OBJECT_STORAGE_ENDPOINT` | `security-service`、`admin` | 空字符串 | 协作对象存储访问入口 | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_COLLABORATION_OBJECT_STORAGE_PREFIX` | `security-service`、`admin` | `heinu/` | 协作对象存储对象键前缀 | 外部传入，未传时用 Compose 默认值 |

### 5. COS / OBS 凭证与参数

| 变量名 | 服务 | 默认值 | 含义 | 取数来源 |
| --- | --- | --- | --- | --- |
| `IRON_AGENT_COS_SECRET_ID` | `security-service`、`admin` | 空字符串 | 腾讯云 COS `SecretId` | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_COS_SECRET_KEY` | `security-service`、`admin` | 空字符串 | 腾讯云 COS `SecretKey` | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_COS_SESSION_TOKEN` | `security-service`、`admin` | 空字符串 | 临时凭证场景的 Session Token | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_COS_BUCKET` | `security-service`、`admin` | 空字符串 | 工作区文件 / 市场等功能使用的 COS Bucket | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_COS_REGION` | `security-service`、`admin` | 空字符串 | COS 所在地域 | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_COS_ENDPOINT` | `security-service`、`admin` | 空字符串 | COS 自定义访问域名或标准 endpoint | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_COS_PREFIX` | `security-service`、`admin` | 空字符串 | COS 对象键前缀 | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_OBS_ACCESS_KEY_ID` | `security-service`、`admin` | 空字符串 | 华为云 OBS Access Key ID | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_OBS_SECRET_ACCESS_KEY` | `security-service`、`admin` | 空字符串 | 华为云 OBS Secret Access Key | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_OBS_BUCKET` | `security-service`、`admin` | 空字符串 | OBS Bucket 名称 | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_OBS_ENDPOINT` | `security-service`、`admin` | 空字符串 | OBS Endpoint | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_OBS_PREFIX` | `security-service`、`admin` | 空字符串 | OBS 对象键前缀 | 外部传入，未传时用 Compose 默认值 |

### 6. 管理台与控制面

| 变量名 | 服务 | 默认值 | 含义 | 取数来源 |
| --- | --- | --- | --- | --- |
| `IRON_AGENT_INTERNAL_SERVICE_TOKEN` | `security-service`、`admin` | `change-me` | 后台服务间内部访问凭证；生产环境必须改掉 | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_CONTROL_UI_PUBLIC_URL` | `security-service`、`admin` | 空字符串 | 对外可访问的控制台 URL，用于链接回跳或外链生成 | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_GO_GATEWAY_PUBLIC_URL` | `security-service`、`admin` | 空字符串 | 对外可访问的 Go Gateway 统一入口 URL；配置后管理台优先生成 `/assistants/{assistant_id}/ui/` 单端口入口 | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_SECURITY_SERVICE_PUBLIC_URL` | `security-service`、`admin` | 空字符串 | 对外可访问的安全服务 URL | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_SECURITY_SERVICE_INTERNAL_URL` | `security-service`、`admin` | `http://security-service:8000` | 管理台在 Docker 网络内部访问安全服务的地址 | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_CONTROL_UI_LAUNCH_TICKET_TTL_SECONDS` | `security-service`、`admin` | `90` | 控制台启动 ticket 的有效期，单位秒 | 外部传入，未传时用 Compose 默认值 |

### 7. IronAgent 运行时联动

| 变量名 | 服务 | 默认值 | 含义 | 取数来源 |
| --- | --- | --- | --- | --- |
| `IRON_AGENT_RUNTIME_SEED_WORKSPACE_SKILLS` | `security-service`、`admin` | `office-docs,local-pdf` | 初始化工作区时自动注入的技能列表 | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_RUNTIME_HOOK_SERVICE_URL` | `security-service`、`admin` | `http://host.docker.internal:8000` | 助手容器回调安全服务 Hook 的地址 | 外部传入，未传时用 Compose 默认值 |
| `IRON_AGENT_RUNTIME_CONTAINER_EXTRA_HOST` | `security-service`、`admin` | `host.docker.internal:host-gateway` | 给助手容器追加的额外主机映射 | 外部传入，未传时用 Compose 默认值 |

## 二、容器内由 `IRON_AGENT_STACK_ROOT` 派生的路径变量

下表这些变量虽然会进入容器环境，但当前 `docker-compose.yml` 的取值并不是直接从 `.env` 读取，而是由 `IRON_AGENT_STACK_ROOT` 拼接得出。

| 容器内变量名 | 服务 | 当前值模式 | 含义 | 取数来源 |
| --- | --- | --- | --- | --- |
| `IRON_AGENT_CONFIG_DIR` | `security-service`、`admin` | `${IRON_AGENT_STACK_ROOT}/config` | 配置目录 | 路径派生 |
| `IRON_AGENT_AUDIT_LOG_PATH` | `security-service`、`admin` | `${IRON_AGENT_STACK_ROOT}/data/audit/audit.jsonl` | 审计日志文件 | 路径派生 |
| `IRON_AGENT_ASSISTANTS_STORE_PATH` | `security-service`、`admin` | `${IRON_AGENT_STACK_ROOT}/data/assistants/assistants.json` | 助手元数据存储文件 | 路径派生 |
| `IRON_AGENT_APPROVAL_STORE_PATH` | `security-service`、`admin` | `${IRON_AGENT_STACK_ROOT}/data/approvals/approvals.json` | 审批单存储文件 | 路径派生 |
| `IRON_AGENT_ASSISTANTS_WORKSPACE_ROOT` | `security-service`、`admin` | `${IRON_AGENT_STACK_ROOT}/data/workspaces` | 助手工作区根目录 | 路径派生 |
| `IRON_AGENT_ASSISTANTS_RUNTIME_ROOT` | `security-service`、`admin` | `${IRON_AGENT_STACK_ROOT}/data/runtimes` | 助手运行时状态根目录 | 路径派生 |
| `IRON_AGENT_MARKET_STORE_PATH` | `security-service`、`admin` | `${IRON_AGENT_STACK_ROOT}/data/market/catalog.json` | 技能/插件市场目录索引 | 路径派生 |
| `IRON_AGENT_MARKET_PACKAGE_ROOT` | `security-service`、`admin` | `${IRON_AGENT_STACK_ROOT}/data/market/packages` | 市场包存放目录 | 路径派生 |
| `IRON_AGENT_MARKET_UPLOAD_ROOT` | `security-service`、`admin` | `${IRON_AGENT_STACK_ROOT}/data/market/uploads` | 市场上传暂存目录 | 路径派生 |

## 三、Compose 里的固定值

| 项目 | 当前值 | 含义 | 取数来源 |
| --- | --- | --- | --- |
| `IRON_AGENT_DOCKER_SOCKET_PATH` | `/var/run/docker.sock` | 容器内访问宿主机 Docker Socket 的路径 | 固定值 |
| `/var/run/docker.sock:/var/run/docker.sock` | 固定挂载 | 让控制面容器直接调用宿主机 Docker | 固定值 |
| `mysql-data` | Docker volume | MySQL 数据卷名称 | 固定值 |

## 四、推荐最小配置集

如果只是先把当前 `main` 分支启动起来，至少建议准备以下变量：

| 变量名 | 是否必须 | 说明 |
| --- | --- | --- |
| `IRON_AGENT_STACK_ROOT` | 必须 | 当前仓库绝对路径 |
| `IRON_AGENT_RUNTIME_SOURCE_ROOT` | 必须 | IronAgent 源码绝对路径 |
| `IRON_AGENT_INTERNAL_SERVICE_TOKEN` | 强烈建议修改 | 默认值 `change-me` 仅适合本机临时验证 |
| `IRON_AGENT_APPROVAL_MODE` | 按需 | 默认 `disabled` 去除人工审批；如需恢复原审批链路，可显式设为 `enabled` |
| `IRON_AGENT_MYSQL_ROOT_PASSWORD` | 强烈建议修改 | 生产环境不要继续使用默认口令 |
| `IRON_AGENT_MYSQL_PASSWORD` | 强烈建议修改 | 业务库账号密码 |
| `IRON_AGENT_DATABASE_URL` | 视情况 | 如不改数据库容器账号密码，可继续用默认值 |
| `IRON_AGENT_COS_*` / `IRON_AGENT_OBS_*` | 按需 | 仅在启用 COS/OBS 时需要 |
| `IRON_AGENT_COLLABORATION_OBJECT_STORAGE_*` | 按需 | 仅在启用协作对象存储时需要 |

## 五、检查到 `main` 缺失但启动链路会依赖的 Markdown 文档

当前 `main` 原先缺少 `docs/reference/templates/` 目录。结合 WebUI/IronAgent 的工作区模板加载逻辑，以下 Markdown 文件在首次初始化工作区时会被读取，缺失时会报 `Missing workspace template ... Ensure docs/reference/templates are packaged.`。

本次已补齐的文件如下：

| 文件路径 | 作用 | 补齐来源 |
| --- | --- | --- |
| `docs/reference/templates/AGENTS.md` | 工作区启动说明与长期行为约束 | 对齐现有 WebUI/IronAgent 模板集 |
| `docs/reference/templates/SOUL.md` | 代理人格与行为边界模板 | 对齐现有 WebUI/IronAgent 模板集 |
| `docs/reference/templates/TOOLS.md` | 本地环境/设备/工具说明模板 | 对齐现有 WebUI/IronAgent 模板集 |
| `docs/reference/templates/IDENTITY.md` | 代理身份信息模板 | 对齐现有 WebUI/IronAgent 模板集 |
| `docs/reference/templates/USER.md` | 用户信息模板 | 对齐现有 WebUI/IronAgent 模板集 |
| `docs/reference/templates/HEARTBEAT.md` | 心跳轮询任务模板 | 对齐现有 WebUI/IronAgent 模板集 |
| `docs/reference/templates/BOOTSTRAP.md` | 首次建工作区时的引导模板 | 对齐现有 WebUI/IronAgent 模板集 |

以下模板文件当前没有补到 `main`，原因是它们不属于当前启动链路的必需项：

- `AGENTS.dev.md`
- `IDENTITY.dev.md`
- `SOUL.dev.md`
- `TOOLS.dev.md`
- `USER.dev.md`
- `CLAUDE.md`
