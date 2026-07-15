# IronAgent 环境变量读取位置与完整清单

数据来源：`src/shared/config.py`（辅以 `docker-compose.yml`、`Makefile`、`src/runtime/provisioner.py`、`deployment/kustomize/ironagent/**`）。本次刷新：2026-06-11。

Go Gateway 的 `GO_GATEWAY_*` 完整字典见 `go-gateway/docs/observability-runbook.md` §9，本文不重复（见第 5 节）。

## 1. 当前结论

控制面环境变量现在主要在 `src/shared/config.py` 里读取：

```python
IRON_AGENT_ENV_PREFIX = "IRON_AGENT_"
IRONAGENT_ENV_PREFIX = "IRONAGENT_"

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix=IRON_AGENT_ENV_PREFIX,
        extra="ignore",
        populate_by_name=True,
    )
```

此外 `settings_customise_sources` 在标准 env 源之前插入了 `_compat_env_source` 兼容源：按字段名派生 `IRONAGENT_*` / `IRON_AGENT_*` 名称（对 `iron_agent_*` 字段还派生 `IRONAGENT_RUNTIME_*` / `IRON_AGENT_RUNTIME_*` 形式）直接读进程环境变量。

读取规则：

| 来源 | 说明 |
|---|---|
| `_compat_env_source` 兼容源 | 优先级最高（仅次于构造参数）；按字段名派生的 `IRONAGENT_*` / `IRON_AGENT_*` 名称直接读进程环境变量。 |
| 进程环境变量（标准 env 源） | 例如容器 `environment:`、K8s ConfigMap/Secret 注入。 |
| 当前工作目录 `.env` | `pydantic-settings` 自动读取；当前仓库根目录没有真实 `.env`，只有 `.env.example` 和 `.env.live-db.example`。 |
| `src/shared/config.py` 默认值 | 环境变量缺省时使用。 |
| `Field(validation_alias=...)` | 有 alias 的字段在标准 env 源里只按 alias 名读取，例如 `IRON_AGENT_RUNTIME_CONTAINER_IMAGE`；兼容源还接受其 `IRONAGENT_` 前缀对应形式。 |
| `docker-compose.yml` | 通过 `${VAR:-default}` 从 shell 或 `.env` 插值，再注入服务容器。 |
| K8s manifests | `deployment/kustomize/ironagent/**/config/*.env` 和 `secret/*.env` 注入 Pod。 |
| Go Gateway | `go-gateway/internal/config/config.go` 单独读取 `GO_GATEWAY_*`，并兼容少量 `IRONAGENT_*` / `IRON_AGENT_*`。 |
| 助手运行容器 | `src/runtime/provisioner.py` 从 Settings 派生 `IRON_AGENT_*` 环境变量传进助手容器。 |

注意：

- 主配置前缀是 `IRON_AGENT_`（Settings 的 env_prefix）；`_compat_env_source` 兼容层还接受 `IRONAGENT_` 前缀及 `RUNTIME_` 派生名。更早的 legacy 前缀兼容层已于 2026-06-10 移除（commit `1c298d24`）。
- `.env.example` 里仍有 `IRON_AGENT_ADMIN_TOKEN`，但主配置实际读取的是 `IRON_AGENT_INTERNAL_SERVICE_TOKEN`。脚本里有少量兼容读取 `IRON_AGENT_ADMIN_TOKEN` / `ADMIN_TOKEN`，服务端主路径不用它。
- 本文不输出本机真实 secret 值，只列变量名、默认值和是否敏感。

## 2. Python 控制面 Settings 完整变量

这些变量由 `src/shared/config.py::Settings` 读取，通常通过 `get_settings()` 使用。

| 环境变量 | Settings 字段 | 默认值 | 敏感 |
|---|---|---|---|
| `IRON_AGENT_ENV` | `env` | `development` | 否 |
| `IRON_AGENT_HOST` | `host` | `0.0.0.0` | 否 |
| `IRON_AGENT_PORT` | `port` | `8000` | 否 |
| `IRON_AGENT_LOG_LEVEL` | `log_level` | `INFO` | 否 |
| `IRON_AGENT_STORAGE_BACKEND` | `storage_backend` | `mysql` | 否 |
| `IRON_AGENT_SEED_DEFAULT_ASSISTANTS` | `seed_default_assistants` | `False` | 否 |
| `IRON_AGENT_CONFIG_DIR` | `config_dir` | `./config` | 否 |
| `IRON_AGENT_AUDIT_LOG_PATH` | `audit_log_path` | `./data/audit/audit.jsonl` | 否 |
| `IRON_AGENT_AUDIT_ARCHIVE_PATH` | `audit_archive_path` | `./data/audit/archive.jsonl` | 否 |
| `IRON_AGENT_AUDIT_RETENTION_DAYS` | `audit_retention_days` | `45` | 否 |
| `IRON_AGENT_ASSISTANTS_STORE_PATH` | `assistants_store_path` | `./data/assistants/assistants.json` | 否 |
| `IRON_AGENT_APPROVAL_STORE_PATH` | `approval_store_path` | `./data/approvals/approvals.json` | 否 |
| `IRON_AGENT_APPROVAL_MODE` | `approval_mode` | `disabled` | 否 |
| `IRON_AGENT_APPROVAL_TIMEOUT_MINUTES` | `approval_timeout_minutes` | `15` | 否 |
| `IRON_AGENT_ASSISTANTS_WORKSPACE_ROOT` | `assistants_workspace_root` | `./data/workspaces` | 否 |
| `IRON_AGENT_ALLOWED_WORKSPACE_ROOTS_JSON` | `allowed_workspace_roots_json` | empty | 否 |
| `IRON_AGENT_DEFAULT_WORKSPACE_ROOT_ID` | `default_workspace_root_id` | `default` | 否 |
| `IRON_AGENT_ASSISTANTS_RUNTIME_ROOT` | `assistants_runtime_root` | `./data/runtimes` | 否 |
| `IRON_AGENT_MARKET_STORE_PATH` | `market_store_path` | `./data/market/catalog.json` | 否 |
| `IRON_AGENT_MARKET_PACKAGE_ROOT` | `market_package_root` | `./data/market/packages` | 否 |
| `IRON_AGENT_MARKET_UPLOAD_ROOT` | `market_upload_root` | `./data/market/uploads` | 否 |
| `IRON_AGENT_MODEL_REGISTRY_STORE_PATH` | `model_registry_store_path` | `./data/models/registry.json` | 否 |
| `IRON_AGENT_USERS_STORE_PATH` | `users_store_path` | `./data/users/users.json` | 否 |
| `IRON_AGENT_USER_BINDINGS_STORE_PATH` | `user_bindings_store_path` | `./data/users/bindings.json` | 否 |
| `IRON_AGENT_WORKSPACE_FILES_STORE_PATH` | `workspace_files_store_path` | `./data/workspace_files/files.json` | 否 |
| `IRON_AGENT_WORKSPACE_FILE_EVENTS_STORE_PATH` | `workspace_file_events_store_path` | `./data/workspace_files/events.json` | 否 |
| `IRON_AGENT_WORKSPACE_PUSH_REQUESTS_STORE_PATH` | `workspace_push_requests_store_path` | `./data/workspace_files/push_requests.json` | 否 |
| `IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND` | `workspace_file_metadata_backend` | `mysql` (set `sqlite` explicitly to route workspace file/event/sync-job metadata to SQLite) | 否 |
| `IRON_AGENT_WORKSPACE_FILE_SQLITE_PATH` | `workspace_file_sqlite_path` | `./data/workspace.db` | 否 |
| `IRON_AGENT_WORKSPACE_ITEM_INDEX_SQLITE_PATH` | `workspace_item_index_sqlite_path` | `./data/workspace_item_index.db` | 否 |
| `IRON_AGENT_WORKSPACE_FILE_SQLITE_BUSY_TIMEOUT_MS` | `workspace_file_sqlite_busy_timeout_ms` | `30000` | 否 |
| `IRON_AGENT_WORKSPACE_FILE_SQLITE_CACHE_SIZE_MB` | `workspace_file_sqlite_cache_size_mb` | `64` | 否 |
| `IRON_AGENT_WORKSPACE_FILE_SQLITE_WAL_AUTOCHECKPOINT` | `workspace_file_sqlite_wal_autocheckpoint` | `1000` | 否 |
| `IRON_AGENT_RUNTIME_WORKSPACE_FILE_SQLITE_MOUNT_PATH` | `iron_agent_workspace_file_sqlite_mount_path` | `/home/node/.ironagent/workspace-metadata` | 否 |
| `IRON_AGENT_WORKSPACE_OBJECT_STORAGE_BACKEND` | `workspace_object_storage_backend` | `local` | 否 |
| `IRON_AGENT_WORKSPACE_OBJECT_STORAGE_ROOT` | `workspace_object_storage_root` | `./data/workspace_objects` | 否 |
| `IRON_AGENT_WORKSPACE_OBJECT_STORAGE_PREFIX` | `workspace_object_storage_prefix` | `workspace-files` | 否 |
| `IRON_AGENT_WORKSPACE_RESULT_SCAN_ENABLED` | `workspace_result_scan_enabled` | `False` | 否 |
| `IRON_AGENT_WORKSPACE_RESULT_SCAN_INTERVAL_SECONDS` | `workspace_result_scan_interval_seconds` | `10` | 否 |
| `IRON_AGENT_WORKSPACE_SYNC_JOB_SQLITE_PATH` | `workspace_sync_job_sqlite_path` | `./data/workspace_sync_jobs.db` | 否 |
| `IRON_AGENT_WORKSPACE_SYNC_JOB_WORKER_ENABLED` | `workspace_sync_job_worker_enabled` | `True` | 否 |
| `IRON_AGENT_WORKSPACE_SYNC_JOB_WORKER_INTERVAL_SECONDS` | `workspace_sync_job_worker_interval_seconds` | `2` | 否 |
| `IRON_AGENT_WORKSPACE_SYNC_JOB_WORKER_BATCH_SIZE` | `workspace_sync_job_worker_batch_size` | `1` | 否 |
| `IRON_AGENT_WORKSPACE_SYNC_JOB_STALE_AFTER_SECONDS` | `workspace_sync_job_stale_after_seconds` | `7200` | 否 |
| `IRON_AGENT_COLLABORATION_OBJECT_STORAGE_BACKEND` | `collaboration_object_storage_backend` | `cos` | 否 |
| `IRON_AGENT_COLLABORATION_OBJECT_STORAGE_BUCKET` | `collaboration_object_storage_bucket` | empty | 否 |
| `IRON_AGENT_COLLABORATION_OBJECT_STORAGE_REGION` | `collaboration_object_storage_region` | empty | 否 |
| `IRON_AGENT_COLLABORATION_OBJECT_STORAGE_ENDPOINT` | `collaboration_object_storage_endpoint` | empty | 否 |
| `IRON_AGENT_COLLABORATION_OBJECT_STORAGE_PREFIX` | `collaboration_object_storage_prefix` | `collaboration/` | 否 |
| `IRON_AGENT_COLLABORATION_CLAIM_LEASE_SECONDS` | `collaboration_claim_lease_seconds` | `300` | 否 |
| `IRON_AGENT_COLLABORATION_AUTO_CREATE_TABLES` | `collaboration_auto_create_tables` | `True` | 否 |
| `IRON_AGENT_HOST_IRON_AGENT_ROOT` | `host_iron_agent_root` | `~/.ironagent` | 否 |
| `IRON_AGENT_RUNTIME_SOURCE_ROOT` | `iron_agent_source_root` | `None` | 否 |
| `IRON_AGENT_RUNTIME_SEED_WORKSPACE_SKILLS` | `iron_agent_seed_workspace_skills` | empty | 否 |
| `IRON_AGENT_HOST_IRON_AGENT_AGENT_ID` | `host_iron_agent_agent_id` | `main` | 否 |
| `IRON_AGENT_RUNTIME_BACKEND` | `runtime_backend` | `containerd` | 否 |
| `IRON_AGENT_RUNTIME_SERVICE_URL` | `iron_agent_service_url` | `http://localhost:3000` | 否 |
| `IRON_AGENT_RUNTIME_CONTAINER_IMAGE` | `iron_agent_container_image` | `ironagent-runtime:dev` | 否 |
| `IRON_AGENT_RUNTIME_STATE_MOUNT_PATH` | `iron_agent_state_mount_path` | `/home/node/.ironagent` | 否 |
| `IRON_AGENT_RUNTIME_WORKSPACE_MOUNT_PATH` | `iron_agent_workspace_mount_path` | `/workspace` | 否 |
| `IRON_AGENT_ASSISTANT_CONTAINER_NAME_PREFIX` | `assistant_container_name_prefix` | `ironagent-assistant` | 否 |
| `IRON_AGENT_RUNTIME_GATEWAY_HOST` | `iron_agent_gateway_host` | `127.0.0.1` | 否 |
| `IRON_AGENT_RUNTIME_GATEWAY_PUBLISH_HOST` | `iron_agent_gateway_publish_host` | empty | 否 |
| `IRON_AGENT_RUNTIME_GATEWAY_ACCESS_HOST` | `iron_agent_gateway_access_host` | empty | 否 |
| `IRON_AGENT_RUNTIME_GATEWAY_PORT` | `iron_agent_gateway_port` | `18789` | 否 |
| `IRON_AGENT_RUNTIME_GATEWAY_BIND` | `iron_agent_gateway_bind` | `lan` | 否 |
| `IRON_AGENT_RUNTIME_HTTP_PROXY` | `iron_agent_http_proxy` | empty | 否 |
| `IRON_AGENT_RUNTIME_HTTPS_PROXY` | `iron_agent_https_proxy` | empty | 否 |
| `IRON_AGENT_RUNTIME_NO_PROXY` | `iron_agent_no_proxy` | empty | 否 |
| `IRON_AGENT_RUNTIME_HOOKS_ENABLED` | `iron_agent_hooks_enabled` | `True` | 否 |
| `IRON_AGENT_RUNTIME_HOOK_SERVICE_URL` | `iron_agent_hook_service_url` | `http://host.docker.internal:8000` | 否 |
| `IRON_AGENT_RUNTIME_HOOK_FAIL_CLOSED` | `iron_agent_hook_fail_closed` | `True` | 否 |
| `IRON_AGENT_RUNTIME_HOOK_API_TOKEN` | `iron_agent_hook_api_token` | empty | 是 |
| `IRON_AGENT_RUNTIME_HOOK_TIMEOUT_MS` | `iron_agent_hook_timeout_ms` | `5000` | 否 |
| `IRON_AGENT_RUNTIME_APPROVAL_POLL_MS` | `iron_agent_approval_poll_ms` | `1000` | 否 |
| `IRON_AGENT_RUNTIME_APPROVAL_MAX_WAIT_MS` | `iron_agent_approval_max_wait_ms` | `300000` | 否 |
| `IRON_AGENT_RUNTIME_CONTAINER_EXTRA_HOST` | `iron_agent_container_extra_host` | `host.docker.internal:host-gateway` | 否 |
| `IRON_AGENT_DOCKER_BINARY` | `docker_binary` | `docker` | 否 |
| `IRON_AGENT_DOCKER_SOCKET_PATH` | `docker_socket_path` | `/var/run/docker.sock` | 否 |
| `IRON_AGENT_K8S_RUNTIME_NAMESPACE` | `k8s_runtime_namespace` | `ironagent` | 否 |
| `IRON_AGENT_K8S_RUNTIME_KUBECTL_BINARY` | `k8s_runtime_kubectl_binary` | `kubectl` | 否 |
| `IRON_AGENT_K8S_RUNTIME_WORKSPACE_PVC` | `k8s_runtime_workspace_pvc` | `assistant-workspaces` | 否 |
| `IRON_AGENT_K8S_RUNTIME_WORKSPACE_SUBPATH_PREFIX` | `k8s_runtime_workspace_subpath_prefix` | `assistants` | 否 |
| `IRON_AGENT_K8S_RUNTIME_STATE_DIR` | `k8s_runtime_state_dir` | `.ironagent` | 否 |
| `IRON_AGENT_K8S_RUNTIME_INIT_IMAGE` | `k8s_runtime_init_image` | empty | 否 |
| `IRON_AGENT_K8S_RUNTIME_IMAGE_PULL_POLICY` | `k8s_runtime_image_pull_policy` | `IfNotPresent` | 否 |
| `IRON_AGENT_K8S_RUNTIME_SERVICE_ACCOUNT_NAME` | `k8s_runtime_service_account_name` | empty | 否 |
| `IRON_AGENT_K8S_RUNTIME_CPU_REQUEST` | `k8s_runtime_cpu_request` | `100m` | 否 |
| `IRON_AGENT_K8S_RUNTIME_MEMORY_REQUEST` | `k8s_runtime_memory_request` | `256Mi` | 否 |
| `IRON_AGENT_K8S_RUNTIME_CPU_LIMIT` | `k8s_runtime_cpu_limit` | `1` | 否 |
| `IRON_AGENT_K8S_RUNTIME_MEMORY_LIMIT` | `k8s_runtime_memory_limit` | `2Gi` | 否 |
| `IRON_AGENT_K8S_RUNTIME_DELETE_WORKSPACE_ON_DESTROY` | `k8s_runtime_delete_workspace_on_destroy` | `False` | 否 |
| `IRON_AGENT_HOOK_TIMEOUT_MS` | `hook_timeout_ms` | `5000` | 否 |
| `IRON_AGENT_INTERNAL_SERVICE_TOKEN` | `internal_service_token` | `change-me` | 是 |
| `IRON_AGENT_CONTROL_UI_PUBLIC_URL` | `control_ui_public_url` | empty | 否 |
| `IRON_AGENT_GO_GATEWAY_PUBLIC_URL` | `go_gateway_public_url` | empty | 否 |
| `IRON_AGENT_SECURITY_SERVICE_PUBLIC_URL` | `security_service_public_url` | empty | 否 |
| `IRON_AGENT_SECURITY_SERVICE_INTERNAL_URL` | `security_service_internal_url` | empty | 否 |
| `IRON_AGENT_CONTROL_UI_LAUNCH_TICKET_TTL_SECONDS` | `control_ui_launch_ticket_ttl_seconds` | `600` | 否 |
| `IRON_AGENT_CONTROL_UI_ALLOWED_ORIGINS_JSON` | `control_ui_allowed_origins_json` | empty | 否 |
| `IRON_AGENT_CONTROL_UI_ALLOWED_ORIGIN_REGEX` | `control_ui_allowed_origin_regex` | `^https?://(localhost|127\.0\.0\.1)(:\d+)?$` | 否 |
| `IRON_AGENT_ADMIN_BOOTSTRAP_FAST_CACHE_TTL_SECONDS` | `admin_bootstrap_fast_cache_ttl_seconds` | `10.0` | 否 |
| `IRON_AGENT_ADMIN_ASSISTANT_STATUS_CACHE_TTL_SECONDS` | `admin_assistant_status_cache_ttl_seconds` | `5.0` | 否 |
| `IRON_AGENT_ADMIN_ASSISTANT_STATUS_FALLBACK_POLL_MS` | `admin_assistant_status_fallback_poll_ms` | `4000` | 否 |
| `IRON_AGENT_DATABASE_URL` | `database_url` | `mysql+pymysql://ironagent:ironagent@localhost:3306/ironagent?charset=utf8mb4` | 是 |
| `IRON_AGENT_DB_POOL_SIZE` | `db_pool_size` | `2` | 否 |
| `IRON_AGENT_DB_MAX_OVERFLOW` | `db_max_overflow` | `3` | 否 |
| `IRON_AGENT_DB_POOL_TIMEOUT_SECONDS` | `db_pool_timeout_seconds` | `30` | 否 |
| `IRON_AGENT_DB_POOL_RECYCLE_SECONDS` | `db_pool_recycle_seconds` | `1800` | 否 |
| `IRON_AGENT_DB_CONNECT_TIMEOUT_SECONDS` | `db_connect_timeout_seconds` | `10` | 否 |
| `IRON_AGENT_REDIS_URL` | `redis_url` | `redis://localhost:6379/0` | 是 |
| `IRON_AGENT_COS_SECRET_ID` | `cos_secret_id` | empty | 是 |
| `IRON_AGENT_COS_SECRET_KEY` | `cos_secret_key` | empty | 是 |
| `IRON_AGENT_COS_SESSION_TOKEN` | `cos_session_token` | empty | 是 |
| `IRON_AGENT_COS_BUCKET` | `cos_bucket` | empty | 否 |
| `IRON_AGENT_COS_REGION` | `cos_region` | empty | 否 |
| `IRON_AGENT_COS_ENDPOINT` | `cos_endpoint` | empty | 否 |
| `IRON_AGENT_COS_PREFIX` | `cos_prefix` | empty | 否 |
| `IRON_AGENT_OBS_ACCESS_KEY_ID` | `obs_access_key_id` | empty | 是 |
| `IRON_AGENT_OBS_SECRET_ACCESS_KEY` | `obs_secret_access_key` | empty | 是 |
| `IRON_AGENT_OBS_BUCKET` | `obs_bucket` | empty | 否 |
| `IRON_AGENT_OBS_ENDPOINT` | `obs_endpoint` | empty | 否 |
| `IRON_AGENT_OBS_PREFIX` | `obs_prefix` | empty | 否 |

## 3. Compose / Makefile 额外变量

这些变量不一定进入 `Settings`，但会影响本地启动、端口、MySQL 容器、挂载和镜像构建。

| 环境变量 | 读取位置 | 用途 |
|---|---|---|
| `IRON_AGENT_STACK_ROOT` | `Makefile`, `docker-compose.yml` | 本地栈根目录；Compose 挂载和数据路径基准。 |
| `IRON_AGENT_SECURITY_SERVICE_PORT` | `Makefile`, `docker-compose.yml` | security-service 暴露端口。 |
| `IRON_AGENT_ADMIN_PORT` | `Makefile`, `docker-compose.yml` | admin web 暴露端口，默认 `8501`。 |
| `IRON_AGENT_ADMIN_BIND_HOST` | `docker-compose.yml` | admin web 绑定的宿主机地址，默认 `127.0.0.1`。 |
| `IRON_AGENT_CONTROL_UI_PUBLIC_URL` | `Makefile`, `docker-compose.yml`, Settings | 控制 UI 对外访问 URL。 |
| `IRON_AGENT_SECURITY_SERVICE_PUBLIC_URL` | `Makefile`, `docker-compose.yml`, Settings | security-service 对外 URL。 |
| `IRON_AGENT_RUNTIME_SOURCE_ROOT` | `Makefile`, `docker-compose.yml`, Settings | IronAgent runtime 源码目录；Makefile 未设置时自动推导并经 COMPOSE_ENV 传入 compose。 |
| `IRON_AGENT_REPO_PATH` | `Makefile` | 构建 runtime 镜像时的 IronAgent 源码目录。 |
| `IRON_AGENT_IMAGE_TAG` | `Makefile` | 构建 runtime 镜像 tag，默认 `ironagent-runtime:dev`。 |
| `IRON_AGENT_MYSQL_PORT` | `docker-compose.yml` | MySQL 宿主机映射端口，默认 `3307`。 |
| `IRON_AGENT_MYSQL_ROOT_PASSWORD` | `docker-compose.yml` | MySQL root 密码。敏感。 |
| `IRON_AGENT_MYSQL_DATABASE` | `docker-compose.yml` | MySQL 初始化数据库名。 |
| `IRON_AGENT_MYSQL_USER` | `docker-compose.yml` | MySQL 初始化用户。 |
| `IRON_AGENT_MYSQL_PASSWORD` | `docker-compose.yml` | MySQL 初始化用户密码。敏感。 |
| `IRON_AGENT_DOCKER_SOCKET_GID` | `docker-compose.yml`, `.env.example` | Docker socket group id。 |
| `HOST_UID` | `docker-compose.yml` | Compose 容器用户 UID。 |
| `HOST_GID` | `docker-compose.yml` | Compose 容器用户 GID。 |
| `GO` | `Makefile` | Go 命令路径。 |
| `KUBECTL` | `Makefile` | kubectl 命令路径。 |
| `K8S_OVERLAY` | `Makefile` | 默认 Kustomize overlay。 |

## 4. 助手运行容器内派生变量

这些不是用户直接配置的全部输入，而是 `src/runtime/provisioner.py` 在创建/启动助手运行容器时派生注入。

| 容器内变量 | 来源/用途 |
|---|---|
| `HOME` | 固定为 `/home/node`。 |
| `TERM` | 固定为 `xterm-256color`。 |
| `IRON_AGENT_ASSISTANT_ID` | 当前助手 ID。 |
| `IRON_AGENT_RUNTIME_CONTAINER_IMAGE` | 当前 runtime 镜像名，来自 Settings。 |
| `IRON_AGENT_STATE_DIR` | 容器内 state 目录，来自 `IRON_AGENT_RUNTIME_STATE_MOUNT_PATH`。 |
| `IRON_AGENT_CONFIG_PATH` | 容器内运行配置路径，通常 `/home/node/.ironagent/ironagent.json`。 |
| `IRON_AGENT_WORKSPACE_DIR` | 容器内工作区目录，默认 `/workspace`。 |
| `IRON_AGENT_BUNDLED_SKILLS_DIR` | 容器内 skills 目录，默认 `/workspace/skills`。 |
| `IRON_AGENT_WORKSPACE_SKILLS_ONLY` | 固定 `1`，限制 workspace skills 来源。 |
| `IRON_AGENT_WORKSPACE_PLUGINS_ONLY` | 固定 `1`，限制 workspace plugins 来源。 |
| `IRON_AGENT_API_URL` | 容器访问控制面的 API URL。 |
| `IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND` | 当控制面配置 sqlite 元数据时注入 `sqlite`。 |
| `IRON_AGENT_WORKSPACE_FILE_SQLITE_PATH` | 容器内共享 SQLite 路径。 |
| `IRON_AGENT_HOOKS_ENABLED` | 是否开启 hooks。 |
| `IRON_AGENT_SERVICE_URL` | hook service URL。 |
| `IRON_AGENT_FAIL_CLOSED` | hook 失败时是否 fail closed。 |
| `IRON_AGENT_HOOK_TIMEOUT_MS` | hook 超时时间。 |
| `IRON_AGENT_APPROVAL_POLL_MS` | 审批轮询间隔；`IRON_AGENT_APPROVAL_MODE=disabled` 时不会进入等待审批链路。 |
| `IRON_AGENT_APPROVAL_MAX_WAIT_MS` | 审批最大等待时间；`IRON_AGENT_APPROVAL_MODE=disabled` 时不会进入等待审批链路。 |
| `IRON_AGENT_API_TOKEN` | hook API token，来自 `IRON_AGENT_RUNTIME_HOOK_API_TOKEN`。敏感。 |
| `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | 由 runtime proxy 设置派生。 |

## 5. Go Gateway 环境变量

这些由 `go-gateway/internal/config/config.go` 单独读取。`GO_GATEWAY_CONFIG_FILE` 指向 JSON 配置文件；环境变量会覆盖文件配置。`GO_GATEWAY_INTERNAL_SERVICE_TOKEN` 缺省可回退读 `IRONAGENT_INTERNAL_SERVICE_TOKEN` / `IRON_AGENT_INTERNAL_SERVICE_TOKEN`，`GO_GATEWAY_DATABASE_URL` 缺省可回退读 `IRONAGENT_DATABASE_URL` / `IRON_AGENT_DATABASE_URL`。

完整的 `GO_GATEWAY_*` 字典（99 个变量，含默认值、必填项与敏感性标注）见 `go-gateway/docs/observability-runbook.md` §9「环境变量完整字典」，本文不再重复维护。

## 6. K8s/基础设施变量

这些主要出现在 `deployment/kustomize/ironagent/**`，用于 MySQL、Redis、Gateway、CLI Pod 或 overlay patch。

| 环境变量 | 用途 |
|---|---|
| `MYSQL_ROOT_PASSWORD` | MySQL root 密码。敏感。 |
| `MYSQL_DATABASE` | MySQL 数据库名。 |
| `MYSQL_USER` | MySQL 用户。 |
| `MYSQL_PASSWORD` | MySQL 用户密码。敏感。 |
| `REDIS_USERNAME` | Redis 用户。敏感。 |
| `REDIS_PASSWORD` | Redis 密码。敏感。 |
| `POD_NAMESPACE` | Redis/Sentinel Pod 命名空间。 |
| `HOSTNAME` | Redis/Sentinel 运行时主机名。 |
| `HOME` | CLI/Gateway 容器 HOME。 |
| `TERM` | CLI/Gateway 终端类型。 |
| `TZ` | CLI/Gateway 时区。 |
| `BROWSER` | CLI 浏览器环境变量。 |
| `IRON_AGENT_GATEWAY_URL` | CLI 访问 gateway 的 URL。 |
| `IRON_AGENT_GATEWAY_BIND` | Gateway bind 模式。 |
| `IRON_AGENT_GATEWAY_PORT` | Gateway 端口。 |
| `IRON_AGENT_GATEWAY_ALLOWED_ORIGINS` | Gateway 允许来源。 |
| `IRON_AGENT_GATEWAY_TOKEN` | Gateway token。敏感。 |
| `IRON_AGENT_RUNTIME_DNS_SERVERS` | runtime DNS server 列表；当前 Settings 未显式建模，但部署文件使用。 |
| `IRON_AGENT_RUNTIME_GATEWAY_PUBLISH_PORT_START` | runtime gateway 发布端口起始；部署文件使用。 |
| `IRON_AGENT_RUNTIME_GATEWAY_PUBLISH_PORT_END` | runtime gateway 发布端口结束；部署文件使用。 |
| `IRON_AGENT_AUTO_MIGRATE` | 部署环境迁移开关；部署文件使用。 |

## 7. 脚本/测试临时变量

这些不是主服务配置，主要用于脚本、压测、callback 复测。

| 环境变量 | 读取位置 | 用途 |
|---|---|---|
| `IRON_AGENT_TEST_PROVIDER_API_KEY` | `scripts/e2e/measure_api_latency.py` | 压测 provider API key。敏感。 |
| `GO_GATEWAY_API_KEY` | `scripts/e2e/measure_api_latency.py` | 压测 Gateway API key。敏感。 |
| `ADMIN_TOKEN` | `scripts/create_user_and_assistant.py`, `scripts/create_test_assistants_1001_1002.sh` | 脚本兼容 token。敏感。 |
| `DB_URL` | `scripts/k8s/show_callback_packets_cn.sh` | 查询 callback 包用数据库 URL。敏感。 |
| `TASK_ID` | `scripts/k8s/show_callback_packets_cn.sh` | 查询任务 ID。 |
| `REQUEST_ID` | `scripts/k8s/show_callback_packets_cn.sh`, callback 验证脚本 | 查询/构造请求 ID。 |
| `MAX_CHARS` | `scripts/k8s/show_callback_packets_cn.sh` | 输出截断长度。 |
| `CONVERSATION_ID` | `scripts/k8s/validate_callback_files_flow_cn.sh` | callback 验证会话 ID。 |
| `USER_ID_VALUE` | `scripts/k8s/validate_callback_files_flow_cn.sh` | callback 验证用户 ID。 |
| `MESSAGE_VALUE` | `scripts/k8s/validate_callback_files_flow_cn.sh` | callback 验证消息内容。 |
| `FILE_ID_VALUE` | `scripts/k8s/validate_callback_files_flow_cn.sh` | callback 验证文件 ID。 |
| `TASK_ID_VALUE` | `scripts/test_xiaoxiongmao_taskcallback.sh` | 小熊猫 callback 测试任务 ID。 |
| `STATUS_VALUE` | `scripts/test_xiaoxiongmao_taskcallback.sh` | 小熊猫 callback 测试状态。 |
| `STAGE_VALUE` | `scripts/test_xiaoxiongmao_taskcallback.sh` | 小熊猫 callback 测试阶段。 |
| `REPLY_TEXT_VALUE` | `scripts/test_xiaoxiongmao_taskcallback.sh` | 小熊猫 callback 测试回复文本。 |
| `FILE_DOWNLOAD_URL_VALUE` | `scripts/test_xiaoxiongmao_taskcallback.sh` | 小熊猫 callback 测试文件下载 URL。 |
