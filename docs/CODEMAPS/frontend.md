<!-- Generated: 2026-06-11 | Files scanned: 20 | Token estimate: ~900 -->

# IronAgent Frontend

IronAgent has two admin surfaces plus a launch path into the per-assistant IronAgent WebUI:

1. **Admin Web (`src/admin_web/`)** — the primary web admin console: a FastAPI app serving a vanilla-JS single-page UI. This is what `make admin`, `docker/Dockerfile.admin`, and `docker-compose.dev.yml` run (uvicorn `src.admin_web.app:app`, port 8501).
2. **Streamlit Admin (`src/admin/`)** — the earlier Streamlit dashboard, still in the repo (legacy; no launch target runs it anymore). Its non-UI helpers are reused by Admin Web.
3. **IronAgent WebUI entry** — Admin Web mints launch URLs that open an assistant's chat UI through the go-gateway single-port proxy.

## Admin Web (src/admin_web) — primary surface

### Backend (src/admin_web/app.py)

`create_admin_web_app()` builds the FastAPI app (`FastAPI(title="IronAgent Admin Web")`):

- Serves the SPA at `/` (`static/index.html`) and assets under `/static`.
- Includes shared routers: `src/assistants_service/router.py` (`/api/v1/assistants`), `src/market/routes.py` (`/api/v1/market`), `src/security_service/user_routes.py` (`/api/v1/users`), `src/security_service/file_routes.py` (`/api/v1/workspace-files`).
- Admin-only API under `/api/v1/admin/...` (guarded by `require_internal_service_token`):
  - `bootstrap`, `bootstrap/fast` — full/partial initial payload
  - `assistants` CRUD + lifecycle: `start`, `stop`, `restart`, `pause`, `repair`, `repair-all`, `delete`, `model-config`, `launch-url`
  - `assistants/status` + SSE `assistants/status/stream`
  - `approvals`, `approvals/{id}/resolve`
  - `audit-logs` (paged filters), `audit-logs/export`, `audit-logs/retention/cleanup`
  - `capabilities` (custom capability profiles)
  - `users` CRUD + `users/{id}/assistants/bind` / unbind
  - workspace: `workspace-items` (list/install/toggle/reuse/delete), `workspace-files/summary`, `workspace-folders/summary`, `workspace-sync-jobs`
  - `models` — model registry CRUD + `models/{id}/probe`
- Health probes: `/health`, `/readyz`.
- Auth: requests normally carry the internal service token header. For loopback browsers, `/` and `/login` set a signed direct-session cookie (`iron_agent_admin_direct_session`); an HTTP middleware converts a valid cookie on loopback requests into the internal token header.
- Startup background tasks: `_assistant_runtime_health_loop` (refreshes the assistant runtime status cache feeding the SSE stream) and an optional workspace sync job worker (`workspace_sync_job_worker_enabled`).

### Bootstrap & runtimeProbeDegraded

- `/api/v1/admin/bootstrap` (full) probes assistant runtime status; `/api/v1/admin/bootstrap/fast` skips runtime/market probing, is cached in `app.state` (TTL `admin_bootstrap_fast_cache_ttl_seconds`), and is marked `pollingSource: "bootstrap_fast"`.
- Every loader is fail-soft: failures fall back to DB data and set degraded flags in the payload (`runtimeProbeDegraded`, plus market/user/workspace degraded markers) instead of failing the request; the last-resort payload sets `bootstrapFastFallback: true` with `runtimeProbeDegraded: true`.
- The SPA surfaces `runtimeProbeDegraded` as a degraded-status indicator and keeps polling/SSE to recover.

### SPA (src/admin_web/static/index.html)

- Single-file vanilla JS app (~7,000 lines), title "AI 能力配置中心"; lucide icons (`static/assets/lucide.min.js`), styles in `static/assets/admin.css`; no build step.
- Views switched via `data-view` + URL hash: `dashboard`, `assistants`, `users`, `whitelist`, `files`, `market`, `models`, `approvals`, `audit`.
- State: one top-level `state` object mutated by fetch handlers and re-rendered by per-view render functions.
- Startup: `bootstrapAdmin()` hydrates from inlined `window.__IRON_AGENT_BOOTSTRAP__` if present, otherwise fetches `bootstrap/fast` first (partial paint) then the full bootstrap.
- Live updates: `EventSource` on `/api/v1/admin/assistants/status/stream`, with timer-based polling fallback when SSE is unavailable.

## IronAgent WebUI entry (assistant chat)

`GET /api/v1/admin/assistants/{assistant_id}/launch-url` returns the URL the admin UI opens for an assistant. `_gateway_launch_url()` in `src/admin_web/app.py`:

1. Mints a short-lived launch ticket via the Security Service (`POST /api/v1/control-ui/launch-tickets`, `src/admin/control_ui_launch.py`) so gateway tokens never appear in query params.
2. Prefers the go-gateway single-port UI proxy: `{go_gateway_public_url}/assistants/{id}/ui/?launchTicket=...` (`build_go_gateway_assistant_ui_url`, `src/admin/gateway_links.py`).
3. Falls back to the direct control-UI chat URL with bootstrap params (`ironAgentApiUrl`/`launchTicket`, or `gatewayUrl`+`token`) carried in the URL fragment (`build_spdbpanda_chat_launch_url`).

## Streamlit Admin (src/admin) — legacy UI + shared helpers

**Page tree (Streamlit multipage)**
```
src/admin/app.py (Main Dashboard: metrics + assistant inventory table)
├── pages/
│   ├── dashboard.py (Home / Overview)
│   ├── assistants.py (Assistant Management)
│   ├── users.py (User Management)
│   ├── files.py (Workspace File Browser)
│   ├── user_portal.py (User-facing file portal: uploads, results, downloads)
│   ├── market.py (Market Catalog)
│   ├── approval.py (Approval Queue)
│   ├── audit_log.py (Audit Log Viewer)
│   └── whitelist.py (Security Whitelist)
└── components/ (empty placeholder)
```

**Non-UI helpers in `src/admin/` reused by Admin Web**

| File | Purpose |
|------|---------|
| src/admin/capability_install.py | `AssistantCapabilityInstallService` — install market/uploaded skill & plugin packages |
| src/admin/control_ui_launch.py | Mint control-UI launch tickets from the Security Service |
| src/admin/gateway_links.py | Build go-gateway UI proxy / control-UI chat launch URLs |
| src/admin/display_paths.py | Path display formatting |
| src/admin/utils.py | `mask_secret` |

## State Management

- Admin Web SPA: single in-page `state` object, hydrated from bootstrap payloads and SSE/polling updates.
- Streamlit pages: Streamlit session state.
- Both call the shared Python service layer (`AssistantService`, `ApprovalService`, `UserService`, `WorkspaceFileService`, `ModelRegistryService`...), which persists to MySQL via the repository layer (see docs/CODEMAPS/data.md).

## Key Files

| File | Purpose |
|------|---------|
| src/admin_web/app.py | Admin Web FastAPI app: bootstrap, admin API, SSE status stream, launch URLs |
| src/admin_web/static/index.html | Single-page admin UI ("AI 能力配置中心") |
| src/admin_web/static/assets/admin.css | SPA styles |
| src/admin_web/static/assets/lucide.min.js | Icon library |
| src/admin/app.py | Legacy Streamlit dashboard entry |
| src/admin/pages/*.py | Legacy Streamlit pages (incl. user_portal.py) |
| src/admin/gateway_links.py | Launch-URL builders (go-gateway UI proxy, control-UI chat) |
| src/admin/control_ui_launch.py | Launch-ticket minting client |
| src/admin/capability_install.py | Capability package install service |
