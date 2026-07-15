<!-- Generated: 2026-06-11 | Files scanned: ~60 | Token estimate: ~1300 -->

# IronAgent Backend

## Services

| Service | Stack | Default Port | Entry |
|---------|-------|--------------|-------|
| go-gateway (northbound entry) | Go 1.x | :8080 (K8s NodePort 30808) | go-gateway/cmd/server |
| security-service | FastAPI | :8000 | src/security_service/app.py |
| admin-web | FastAPI | :8501 | src/admin_web/app.py |
| assistants-service (optional standalone) | FastAPI | :8100 | src/assistants_service/app.py |
| callback-receiver (default callback target) | Python http.server | :8099 | src/callback_receiver/receiver.py |

## API Routes

### Go Gateway (northbound, go-gateway/internal/httpapi/router.go)

Public business API (contract: docs/接口契约.md):
- `GET /health`, `GET /healthz`, `GET /readyz` → Health probes
- `GET /api/v1/health` → Per-user health check
- `GET /api/v1/users/verify` → User existence check
- `POST /api/v1/chat` → Create chat task
- `GET /api/v1/tasks/{task_id}` → Fallback task query
- `POST /api/v1/tasks/{task_id}/cancel` → Cancel task
- `GET /api/v1/approvals/{approval_id}` / `POST .../decision` → Approval query/decision
- `POST /api/v1/files/upload`, `GET /api/v1/files`, `GET /api/v1/files/{file_id}`, `GET /api/v1/files/{file_id}/download`, `POST /api/v1/files/{file_id}/delete` → File channel
- `POST /api/v1/files/upload-url` / `download-url` + `PUT/GET /api/v1/files/presigned/{...}/{token}` → Presigned file channel

Internal / runtime / ops routes (scope-gated by API-key auth middleware):
- `POST /api/v1/runtime/start|stop`, `GET /api/v1/runtime` → Runtime lifecycle
- `GET/PUT /api/v1/runtime/registry[/{assistant_id}]`, `POST .../heartbeat`, `GET /api/v1/runtime/scheduler/last-decision` → Runtime registry
- `POST /api/v1/tasks/{task_id}/start|complete|fail|approvals/resolve` → Task state (southbound/internal)
- `POST /api/v1/webhooks/ack`, `GET /api/v1/webhooks/dead-letter`, `POST /api/v1/webhooks/dead-letter/{event_id}/replay` → Callback ops
- `GET /api/v1/task-queue`, `GET /api/v1/metrics`, `GET /metrics` (Prometheus) → Observability
- `/admin/*`, `/api/v1/admin/*`, `/api/v1/market/*`, `/api/v1/workspace-files*` → Authenticated proxy to admin-web
- `/api/v1/hooks/*`, `/api/v1/security/*`, `/api/v1/audit/*`, `/api/v1/approvals*`, `/api/v1/assistants/*`, `/api/v1/control-ui/*` → Proxy to security-service
- `/assistants/*` → Proxy to assistant runtime (WebUI)

Full categorized route inventory: go-gateway/README.md.

### Security Service (:8000, src/security_service/)

**Health & Hooks** (routes.py)
- `GET /health` → Health check
- `POST /api/v1/hooks/pre-execute` (alias `POST /api/v1/security/check`) → Security decision engine
- `POST /api/v1/hooks/post-execute` (alias `POST /api/v1/audit/log`) → Audit logging

**Assistants & Approvals** (routes.py)
- `GET /api/v1/assistants/{assistant_id}` → Get assistant by ID
- `GET /api/v1/approvals`, `GET /api/v1/approvals/{approval_id}`, `POST /api/v1/approvals/{approval_id}/resolve` → Approval list/status/resolve
- `GET /api/v1/debug/auto-allow`, `DELETE /api/v1/auto-allow` → Session auto-allow debug

**Control UI** (routes.py, control_ui_relay.py, launch_tickets.py)
- `POST /api/v1/control-ui/launch-tickets` (internal token) / `POST .../resolve` → One-time launch tickets
- `WS /api/v1/control-ui/gateway-relay/ws`, `GET /api/v1/control-ui/assistant-context` → Gateway relay

**Users** (user_routes.py)
- `POST/GET /api/v1/users`, `GET/PUT/DELETE /api/v1/users/{user_id}`
- `POST /api/v1/users/{user_id}/assistants/bind`, `DELETE /api/v1/users/{user_id}/assistants/{assistant_id}/unbind`, `GET /api/v1/users/{user_id}/assistants`

**Workspace Files** (file_routes.py, southbound/internal API — see docs/workspace-file-sync-api.md)
- `POST /api/v1/workspace-files/upload|push|sync-results|scan`
- `GET /api/v1/workspace-files`, `GET .../overview`, `GET .../{file_id}`, `GET .../{file_id}/download|events|download-info`
- `POST /api/v1/workspace-files/{file_id}/sync|delete`
- `GET /api/v1/users/{user_id}/workspace-files[/{file_id}/download]`

**Market** — mounted at `/api/v1/market` (src/market/routes.py): skills/plugins upload, approve, install, download, delete

**Collaboration** — mounted at `/api/v1/collaboration` (src/collaboration/routes.py): shared objects, permissions, cross-assistant tasks (claim/renew/start/complete/fail/cancel), cleanup

### Admin Web (:8501, src/admin_web/app.py)

FastAPI app serving the admin SPA (static/) plus `/api/v1/admin/*`:
- Bootstrap & status: `GET /api/v1/admin/bootstrap[/fast]`, `GET /api/v1/admin/assistants/status[/stream]`
- Assistants: CRUD + `start|stop|pause|restart|repair|repair-all`, `PUT .../model-config`, `GET .../launch-url`
- Approvals: `GET /api/v1/admin/approvals`, `POST .../{approval_id}/resolve`
- Audit logs: `GET /api/v1/admin/audit-logs[/export]`, `POST .../retention/cleanup`
- Capabilities: `POST /api/v1/admin/capabilities`, `DELETE .../{profile_id}`
- Users & bindings: CRUD + bind/unbind
- Workspace: `.../workspace-items` (list/install/toggle/reuse/delete), `.../workspace-files/summary`, `.../workspace-folders/summary`, `.../workspace-sync-jobs`
- Model registry: `GET/POST/PUT/DELETE /api/v1/admin/models[/{model_id}]`, `POST .../probe`

### Assistants Service (:8100, src/assistants_service/)
- `GET /health` → Health check
- `GET /api/v1/assistants`, `GET /api/v1/assistants/{assistant_id}`
- `POST /api/v1/assistants`, `PUT /api/v1/assistants/{assistant_id}` (admin)

## Service → Repository Mapping

```
Security Decision Engine
├── SecurityDecisionEngine (src/security_service/decision.py)
├── ApprovalService (src/approval/service.py)
└── WhitelistService (src/security/whitelist.py)

Assistant Management
├── AssistantService (src/assistants/service.py)
└── RuntimeProvisioner (src/runtime/provisioner.py — containerd / kubernetes backends)

User Management
└── UserService (src/users/service.py)

Workspace Files
└── WorkspaceFileService (src/workspace_files/service.py — SQLite metadata + object storage sync)

Market
├── SkillRegistry (src/market/skill/registry.py)
├── PluginRegistry (src/market/plugin/registry.py)
└── MarketService (src/market/service.py)

Collaboration
└── CollaborationService (src/collaboration/service.py)

Model Registry
└── ModelRegistryService (src/model_registry/service.py)

Go Gateway task plane
├── Task store (go-gateway/internal/taskstore — MySQL gateway_* tables)
├── Webhook dispatcher / outbox (go-gateway/internal/webhook)
└── Redis cache / rate limit / progress (go-gateway/internal/rediscache)
```

## Key Files

| File | Purpose | Size |
|------|---------|------|
| go-gateway/internal/httpapi/router.go | Northbound routes, proxies, error mapping | ~5600 lines |
| go-gateway/internal/core/service.go | Gateway task orchestration core | ~6500 lines |
| src/security_service/app.py | FastAPI app factory + workers | 187 lines |
| src/security_service/routes.py | Hook, approval, control-ui endpoints | 644 lines |
| src/security_service/user_routes.py | User & binding endpoints | 189 lines |
| src/security_service/file_routes.py | Workspace file endpoints | 1075 lines |
| src/admin_web/app.py | Admin web app + /api/v1/admin/* | ~5300 lines |
| src/assistants_service/router.py | Assistant CRUD endpoints | 176 lines |
| src/approval/service.py | Approval workflow engine | 351 lines |
| src/workspace_files/service.py | File security & storage | ~2750 lines |
| src/shared/repositories.py | JSON/MySQL repository implementations | ~2100 lines |
| src/shared/auth.py | Admin/internal token auth | 55 lines |
| src/shared/config.py | Pydantic Settings (IRON_AGENT_*) | 284 lines |

## Middleware & Dependencies

- `require_admin_token` / `require_internal_service_token` → Token validation (src/shared/auth.py)
- go-gateway auth middleware → API-key scopes + HMAC/nonce for internal scopes; public scope controlled by `GO_GATEWAY_PUBLIC_AUTH_DISABLED` (go-gateway/internal/auth/middleware.go)
- Structlog logging (src/shared/logging.py)
- Pydantic settings management (src/shared/config.py)

## Data Storage

- **MySQL is the primary store** (`IRON_AGENT_STORAGE_BACKEND=mysql`, default): SQLAlchemy + PyMySQL (src/shared/database.py), schema managed by Alembic (migrations/, current head `029_workspace_file_long_path_columns`, 28 business tables + alembic_version).
  - Python control plane: assistants, approvals, audit_events, users, user_assistant_bindings, workspace_files, workspace_file_events, external_push_requests, collaboration_* / shared_*.
  - Go Gateway shares the same database: gateway_tasks, gateway_task_queue, gateway_task_events, gateway_event_outbox/sequences, gateway_webhook_deliveries, gateway_dead_letters, gateway_idempotency_keys, gateway_hmac_nonces, gateway_runtime_instances/leases, gateway_conversation_routes, etc.
- **Workspace-file metadata defaults to MySQL**. `workspace_files`, `workspace_file_events`, and `workspace_sync_jobs` use the SQLAlchemy/MySQL backend unless `IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND=sqlite` is set explicitly; item index remains SQLite (data/workspace_item_index.db, table assistant_workspace_items).
- **File objects**: workspace directories on disk/PVC + object storage sync (local / Tencent COS / Huawei OBS, src/shared/object_storage.py).
- **JSON files remain** for the model registry (data/models/registry.json), market catalog (data/market/catalog.json), and as the legacy fallback for assistants/approvals/users when `IRON_AGENT_STORAGE_BACKEND=json` (src/shared/json_store.py).
- **Audit log**: audit_events table in MySQL mode; JSONL append-only (data/audit/audit.jsonl) in JSON mode.
- **Redis**: used by go-gateway for assistant-resolution cache, task progress, and chat rate limiting (fail-closed).
