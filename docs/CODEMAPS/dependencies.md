<!-- Generated: 2026-06-11 | Files scanned: ~40 | Token estimate: ~850 -->

# IronAgent Dependencies

## External Services

### MySQL
- **Purpose**: Primary database for the Python control plane and the go-gateway (task queue, runtime registry, conversation routes, webhook outbox, nonces)
- **Version**: MySQL 8.4 (custom FIPS image, `docker/Dockerfile.mysql` → `ironagent-mysql:8.4-fips`)
- **Python access**: SQLAlchemy async engine; sync DSN `mysql+pymysql://` is rewritten to `mysql+aiomysql://` (`src/shared/database.py`)
- **Go access**: `go-sql-driver/mysql`
- **Configuration**: `IRON_AGENT_DATABASE_URL` (default `mysql+pymysql://ironagent:ironagent@localhost:3306/ironagent?charset=utf8mb4`), `IRON_AGENT_STORAGE_BACKEND` (default `mysql`), `GO_GATEWAY_DATABASE_URL`
- **Migrations**: Alembic (`migrations/`, head 029); admin container auto-runs `alembic upgrade head` when `IRON_AGENT_AUTO_MIGRATE=1`

### Redis (GA, gateway core dependency)
- **Purpose**: go-gateway assistant resolution cache, task progress store, and chat rate limiting; Python side client in `src/shared/redis.py`
- **Modes**: `standalone` or `sentinel` (`GO_GATEWAY_REDIS_MODE`); production overlay runs Sentinel HA (`redis-ha` StatefulSet + `redis-sentinel` Deployment, master `mymaster`, image `redis:7.2.10-alpine`)
- **Configuration**: `GO_GATEWAY_REDIS_*` (`_ADDR`, `_SENTINEL_ADDRS`, `_SENTINEL_MASTER`, `_REQUIRED`, `_KEY_PREFIX`, pool/timeout knobs); full dictionary in `go-gateway/docs/observability-runbook.md` §9
- Note: the gateway task queue itself is MySQL-backed (`gateway_task_queue`), not Redis

### Kubernetes
- **Purpose**: Default production runtime — one Pod per assistant with PVC subPath workspace/state mounts (`src/runtime/kubernetes.py`, kubectl-driven)
- **Configuration**: `IRON_AGENT_RUNTIME_BACKEND=kubernetes`, `IRON_AGENT_K8S_RUNTIME_*` (namespace `ironagent`, workspace PVC `assistant-workspaces`, init image, resource requests/limits)
- **Manifests**: `deployment/kustomize/ironagent/` (base + overlays; prod single-entry overlay exposes go-gateway via NodePort 30808)

### Docker (legacy / dev)
- **Purpose**: `containerd` runtime backend (`AssistantRuntimeProvisioner`) and the Docker Compose dev stack
- **Socket**: `/var/run/docker.sock` (`IRON_AGENT_DOCKER_SOCKET_PATH`)

### IronAgent (execution plane)
- **Purpose**: Assistant execution engine (Node); deployed as `ironagent-gateway` (:18789 gateway / :18790 bridge) and `ironagent-cli`
- **Integration**: HTTP hook protocol against security-service
  - `POST /api/v1/hooks/pre-execute` - Pre-execution security check
  - `POST /api/v1/hooks/post-execute` - Post-execution audit logging
- **Configuration**: `IRON_AGENT_RUNTIME_SOURCE_ROOT`, `IRON_AGENT_RUNTIME_HOOK_SERVICE_URL`, `IRON_AGENT_RUNTIME_CONTAINER_EXTRA_HOST`

### Object Storage
- **Purpose**: Workspace file objects and collaboration shared objects
- **Backends**: `local` (default for workspace files), Tencent COS (`cos-python-sdk-v5`), Huawei OBS (`esdk-obs-python`); collaboration default `cos`
- **Configuration**: `IRON_AGENT_WORKSPACE_OBJECT_STORAGE_*`, `IRON_AGENT_COLLABORATION_OBJECT_STORAGE_*`, `IRON_AGENT_COS_*`, `IRON_AGENT_OBS_*`

## Go Toolchain (go-gateway)

- **Go** 1.25 (`go-gateway/go.mod`)
- **github.com/go-sql-driver/mysql** v1.9.3 - MySQL driver
- **github.com/gorilla/websocket** v1.5.3 - Runtime WebSocket execution channel
- **github.com/redis/go-redis/v9** v9.7.0 - Redis client (standalone + sentinel failover)

## Third-Party Libraries (Python)

### Web Framework
- **FastAPI** >=0.116.0 - API framework (security-service, admin_web, assistants_service)
- **Uvicorn[standard]** >=0.35.0 - ASGI server
- **Streamlit** >=1.44.0 - Legacy admin UI (`src/admin`)

### Data Validation & Settings
- **Pydantic** >=2.11.0 - Data validation
- **Pydantic Settings** >=2.8.0 - Configuration management
- **PyYAML** >=6.0.2 - YAML parsing

### Database & Storage
- **SQLAlchemy[asyncio]** >=2.0.38 - Async ORM
- **PyMySQL** >=1.1.1 - MySQL sync driver (DSN scheme)
- **aiomysql** >=0.2.0 - MySQL async driver
- **aiosqlite** >=0.21.0 - SQLite async driver (workspace file/item indexes)
- **asyncpg** >=0.30.0 - PostgreSQL async driver (declared; primary DB is MySQL)
- **Redis** >=5.2.0 - Redis client
- **Alembic** >=1.14.0 - Database migrations
- **cos-python-sdk-v5** >=1.9.35 - Tencent COS SDK
- **esdk-obs-python** >=3.24.6.1 - Huawei OBS SDK

### HTTP & APIs
- **HTTPX** >=0.28.0 - Async HTTP client
- **python-multipart** >=0.0.20 - File upload parsing

### Security & Misc
- **cryptography** >=45.0.0 - Crypto primitives
- **xlwings** >=0.35.3 - Excel automation

### Logging & Monitoring
- **structlog** >=25.1.0 - Structured logging

## Shared Internal Modules

### src/shared/
- `config.py` - Settings management (Pydantic, env prefix `IRON_AGENT_`)
- `logging.py` - Structlog configuration
- `auth.py` - Admin/internal token authentication
- `database.py` - Async SQLAlchemy engines (MySQL/aiomysql, SQLite/aiosqlite)
- `repositories.py` - Shared repository helpers
- `redis.py` - Redis client
- `json_store.py` - JSON file-based storage (legacy stores)
- `object_storage.py` / `object_store.py` / `blob_storage.py` - Object storage abstraction (local/COS/OBS)
- `cos_network.py` - COS network helpers
- `async_utils.py` - Async helpers
- `exceptions.py` - Custom exceptions
- `models.py` - Shared Pydantic models
- `workspace_roots.py` / `workspace_paths.py` - Workspace root & path policy

### Service Dependencies

```
go-gateway (Go)
├── internal/southbound (security-service + admin HTTP clients)
├── internal/taskstore (MySQL task queue / runtime registry / webhook outbox)
├── internal/rediscache (cache, task progress, rate limit)
├── internal/runtimepool + runtimeexec (runtime registry, WebSocket exec)
├── internal/webhook (callback dispatcher, dead-letter)
└── internal/{httpapi,mcpapi,auth,presign,metrics,config,core}

security_service (src/main.py → src/security_service)
├── src/security/ (capability profiles, pruning, whitelist)
├── src/approval/ (approval workflow)
├── src/assistants/ + src/runtime/ (lifecycle, K8s provisioning)
├── src/workspace_files/ (file operations)
├── src/collaboration/ (shared objects, cross-assistant tasks)
├── src/users/ (user & binding management)
├── src/market/ (market routes)
└── src/shared/ (infrastructure)

admin_web (FastAPI)
├── src/assistants_service/router.py
├── src/market/routes.py
├── src/security_service/{file_routes,user_routes}.py
├── src/admin/ (capability_install, control_ui_launch, gateway_links)
├── src/model_registry/service.py
└── src/shared/config.py

admin (Streamlit, legacy)
└── src/admin/pages/ (dashboard, assistants, approval, files, market,
    users, user_portal, whitelist, audit_log)
```

## Build Dependencies

### Container Builds
- **Python services** (`docker/Dockerfile.security-service`, `Dockerfile.admin`, `Dockerfile.callback-receiver`): base `python:3.11-slim`, package manager `uv`, PyPI mirror `mirrors.cloud.tencent.com`, Debian mirror USTC
- **go-gateway** (`go-gateway/Dockerfile`): `golang:1.25-alpine` build stage → `alpine:3.22` runtime, apk mirror Aliyun
- **MySQL** (`docker/Dockerfile.mysql`): `ironagent-mysql:8.4-fips` (FIPS crypto compliance overlay)
- **Runtime image** (`docker/Dockerfile.ironagent`): parameterized base `${IRON_AGENT_RUNTIME_BASE_IMAGE}`

### Python Build Tools
- **hatchling** >=1.27.0 - Build backend
- **ruff** >=0.11.0 - Linting & formatting
- **mypy** >=1.15.0 - Type checking
- **pytest** >=8.3.0 / **pytest-asyncio** >=0.26.0 - Testing
- **types-PyYAML** >=6.0.12 - Type stubs

## Configuration Dependencies

### Required Environment Variables (Docker Compose)
- `IRON_AGENT_STACK_ROOT` - Absolute path to IronAgent repo
- `IRON_AGENT_RUNTIME_SOURCE_ROOT` - Absolute path to IronAgent source

### Key Optional Environment Variables
- `IRON_AGENT_DATABASE_URL` - MySQL DSN (default above)
- `IRON_AGENT_STORAGE_BACKEND` - Default: "mysql"
- `IRON_AGENT_RUNTIME_BACKEND` - "containerd" (default) or "kubernetes"
- `IRON_AGENT_INTERNAL_SERVICE_TOKEN` - Default: "change-me"
- `IRON_AGENT_CONFIG_DIR` - Default: "./config"
- `IRON_AGENT_AUDIT_LOG_PATH` - Default: "./data/audit/audit.jsonl"
- `IRON_AGENT_HOST_IRON_AGENT_ROOT` - Isolated IronAgent config root
- `GO_GATEWAY_*` - Gateway configuration (98 vars; complete dictionary in `go-gateway/docs/observability-runbook.md` §9)

## Network Dependencies

### Kubernetes (production, single-entry)
- **Northbound entry**: go-gateway Service, ClusterIP :8080; NodePort **30808** in `prod-k8s-single-entry-nodeport` overlay (only externally exposed port)
- **ClusterIP services**: security-service :8000, admin :8501, ironagent-gateway :18789/:18790, ironagent-callback-receiver :8099, mysql :3306, redis :6379 + redis-sentinel :26379

### Docker Compose (dev)
- MySQL: `${IRON_AGENT_MYSQL_PORT:-3307}:3306`
- Security Service: `${IRON_AGENT_SECURITY_SERVICE_PORT:-8000}:8000`
- Admin (admin_web): `127.0.0.1:${IRON_AGENT_ADMIN_PORT:-8501}:8501`
- Host access from containers: `host.docker.internal` (hook callbacks)

### External Network Access
- PyPI / apk / Debian mirrors (image builds)
- Tencent COS / Huawei OBS endpoints (object storage, when enabled)
- Optional: LLM provider APIs (configured per assistant)
