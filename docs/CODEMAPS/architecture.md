<!-- Generated: 2026-06-11 | Files scanned: ~140 | Token estimate: ~900 -->

# IronAgent Architecture

## Overview
IronAgent is the enterprise security control layer for IronAgent. The stack is split into three planes: a Go northbound gateway (`go-gateway`, the single external entry point), a Python control plane (security service + admin web), and the IronAgent execution plane (assistant runtimes provisioned as Kubernetes Pods, plus the Node-based IronAgent gateway).

## Service Boundaries

```
                North (IM / upstream clients, MCP)
                          │  NodePort 30808 (prod single-entry overlay)
┌─────────────────────────▼─────────────────────────────────────┐
│  go-gateway (Go, :8080) — single northbound entry              │
│  • Public API: /api/v1/chat, tasks, approvals, files, health   │
│  • MCP endpoint: /mcp                                          │
│  • Admin UI login + proxy: /admin, /api/v1/admin|market|...    │
│  • Security API proxy: /api/v1/hooks|security|audit|approvals  │
│  • Assistant Control-UI proxy: /assistants/...                 │
│  • MySQL-backed task queue (gateway_task_queue) + task worker  │
│  • Redis cache / task progress / chat rate limit (Sentinel HA) │
│  • Webhook outbox + dead-letter, presigned file URLs           │
└──────────┬───────────────────────────┬────────────────────────┘
           │ southbound (internal service token)
┌──────────▼───────────┐    ┌──────────▼──────────────┐
│  security-service    │    │  admin (src/admin_web)  │
│  (FastAPI, :8000)    │    │  (FastAPI, :8501)       │
│  • Security decision │    │  • Web admin UI (SPA    │
│    /api/v1/hooks/*   │    │    at /static)          │
│  • Audit logging     │    │  • Assistants service   │
│  • Approvals         │    │    router               │
│  • Users / bindings  │    │  • Market routes        │
│  • Workspace files   │    │  • Users routes         │
│  • Collaboration     │    │  • Workspace files      │
│  • Control-UI relay  │    │  • Alembic auto-migrate │
└──────────┬───────────┘    └──────────┬──────────────┘
           │   Shared services (src/): assistants, approval,
           │   users, workspace_files, collaboration, market,
           │   model_registry, security, workspace, shared
           │
┌──────────▼────────────────────────────────────────────────────┐
│  Execution plane (Kubernetes)                                  │
│  • Assistant runtimes: one Pod per assistant, workspace via    │
│    PVC subPath mounts (src/runtime/kubernetes.py)              │
│  • ironagent-gateway (Node, :18789 gateway / :18790 bridge)    │
│  • ironagent-cli                                               │
│  • callback_receiver (:8099, webhook callback validation)      │
└────────────────────────────────────────────────────────────────┘

Data stores: MySQL 8.4 (primary DB, alembic head 029) · Redis 7.2
(Sentinel HA in prod) · SQLite (workspace file/item indexes) ·
audit JSONL · object storage (local / COS / OBS)
```

## Data Flow

1. **Northbound task flow**
   - `POST /api/v1/chat` (go-gateway) → MySQL task queue → task worker → assistant runtime → callback/webhook status push; `GET /api/v1/tasks/{task_id}` as fallback query
   - Task progress snapshots cached in Redis; webhook outbox with dead-letter replay

2. **IronAgent Hook Integration**
   - `POST /api/v1/hooks/pre-execute` → Security Decision Engine (security-service)
   - `POST /api/v1/hooks/post-execute` → Audit Logging
   - Both reachable directly or via the go-gateway security proxy

3. **Assistant Lifecycle**
   - Create → provision Kubernetes Pod (PVC subPath workspace + state mounts) → start IronAgent runtime → register in gateway runtime pool (heartbeat)
   - Legacy `containerd` backend kept via `AssistantRuntimeProvisioner` (runtime_backend setting)

4. **File Operations**
   - Upload/Push → security validation → workspace storage (local/COS/OBS object storage + SQLite metadata index) → audit event
   - Presigned upload/download URLs served by go-gateway

5. **Approvals**
   - Approval workflow in security-service; northbound query/decision via go-gateway `/api/v1/approvals/*`

## Key Modules

| Module | Purpose | Entry Points |
|--------|---------|--------------|
| go-gateway | Go northbound gateway: public API, MCP, admin/security/control-UI proxy, task queue & worker | go-gateway/cmd/server/main.go |
| security_service | FastAPI security & audit API (hooks, approvals, users, files, collaboration) | src/security_service/app.py (via src/main.py) |
| admin_web | FastAPI web admin plane (primary admin UI, serves SPA) | src/admin_web/app.py |
| admin | Legacy Streamlit management UI + shared admin helpers (capability_install, control_ui_launch, gateway_links) | src/admin/app.py |
| assistants_service | Assistant lifecycle API router (mounted in admin_web; standalone-capable) | src/assistants_service/app.py |
| approval | Approval workflow engine (queue, watchdog, timeout, gateway client) | src/approval/service.py |
| collaboration | Cross-assistant shared objects & task claim/start/complete API | src/collaboration/routes.py |
| workspace_files | File management with security (upload/download/delete, object storage sync, item index) | src/workspace_files/ |
| users | User & assistant binding management | src/users/service.py |
| market | Skill/plugin market (catalog, packages, capability audit) | src/market/service.py |
| model_registry | LLM provider/model registry | src/model_registry/service.py |
| runtime | Runtime provisioning: Kubernetes Pod (default in prod) or containerd command runner | src/runtime/factory.py |
| security | Capability profiles, pruning, request policy, whitelist | src/security/capability.py |
| callback_receiver | Minimal HTTP receiver (:8099) for webhook callback validation | src/callback_receiver/receiver.py |
| workspace | Workspace placement & roots modeling | src/workspace/placement.py |
| shared | Settings, logging, database, redis, object storage, repositories | src/shared/ |

## Deployment Topology

- **Kubernetes (kustomize)**: `deployment/kustomize/ironagent/base` (go-gateway, ironagent-gateway, ironagent-cli, admin, security-service, callback-receiver) + overlays. Production single-entry overlay `prod-k8s-single-entry-nodeport` exposes only go-gateway via NodePort 30808 and adds MySQL, Redis Sentinel HA (redis-ha StatefulSet + redis-sentinel), and local PVs.
- **Docker Compose** (`docker-compose.yml`): dev/standalone stack with mysql, security-service (:8000), admin (:8501).
