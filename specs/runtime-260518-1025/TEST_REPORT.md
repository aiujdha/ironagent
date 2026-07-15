# runtime-260518-1025 TEST_REPORT

- Spec: `specs/runtime-260518-1025`
- Branch: `codex/admin-260514-2331-reaccept-final`
- Validation base HEAD: `b8178862650d4f2fa542a812bd4f3e3d0bf03f0a`
- Report updated: 2026-05-18 12:17 CST
- Scope: code repair plus local Admin API/browser, Docker healthz, and Go Gateway MySQL/no-DB runtime-state evidence.
- Result: `partial/local-verified, not deployment-verified`

## Evidence Boundary

This report records code, unit-test, local Admin API, local browser, local Docker, and local Go Gateway/MySQL evidence.

- No image build, image import, deployment, or live server update was performed.
- A local Admin Web server was started on `127.0.0.1:18080` in round 2 with isolated temp state under `/tmp/ironagent-runtime-verify-GjlgLT`.
- A local Admin Web server and SSE-blocking proxy were started in round 3 on `127.0.0.1:18180` and `127.0.0.1:18181`; both were stopped after evidence collection.
- Browser screenshot and local API/SSE artifacts were collected under `output/runtime-state-verify-round2/`; cookie/runtime token values were redacted before staging.
- Round 3 browser fallback evidence was collected under `output/runtime-state-verify-round3/browser-fallback/`; screenshot crops exclude token columns and saved request logs omit headers/cookies.
- Docker Desktop CLI was reachable at `/Applications/Docker.app/Contents/Resources/bin/docker` with server version `26.1.1`; round 3 started temporary local Docker containers to verify `probe_runtime()` healthz behavior, then removed them.
- Round 3 started temporary local MySQL `mysql:8.4` and two local Go Gateway processes to verify shared-DB registry behavior. A minimal runtime-table schema matching the current store fields was applied for this local check; this is not a full Alembic migration/deployment proof.
- Round 3 also started two no-DB local Go Gateway processes to verify the unsupported/no-shared-registry negative case.
- K8s is not configured for this pass: `/usr/local/bin/kubectl config current-context` returned `current-context is not set`.
- No multi-Admin environment was exercised.
- Historical evidence from `admin-260514-2331`, `runtime-260513-1949`, and `gateway-260514-1929` remains historical context only.
- Items that require deployment, K8s, multi-Admin, hidden/visible browser, or live proxy-failure evidence remain below fully `verified`.

## Changes Covered

- Docker runtime provisioning now writes `running` only when the published gateway `/healthz` is reachable. A running container with missing endpoint or failed healthz stays `starting` with an explicit runtime error.
- Docker runtime probe now also requires gateway `/healthz`; a running container no longer becomes healthy/current only because the Docker state is `running`.
- Go Gateway heartbeat now preserves scheduler-owned fields during heartbeat updates: `in_flight`, existing `capacity`, existing `drain_mode`, and existing `last_error`.
- Launch-ticket/browser proxy session paths no longer mark a runtime as `running` or clear `last_error` as a side effect of browser access. Browser-session proxying can still use the launch-ticket target without advertising scheduler health.
- Docker managed runtime env now includes `IRON_AGENT_WORKSPACE_DIR` alongside `IRON_AGENT_STATE_DIR` and `IRON_AGENT_CONFIG_PATH`; Docker and K8s runtime provisioner tests assert the workspace mount path is passed into runtimes.

## Commands Run

```bash
.venv/bin/python -m pytest tests/unit/test_runtime_provisioner.py tests/unit/test_assistant_service.py tests/unit/test_runtime_models.py tests/unit/test_admin_web_app.py -q
/opt/homebrew/bin/go test -count=1 ./internal/runtimepool ./internal/taskstore ./internal/httpapi ./internal/config
.venv/bin/python -m py_compile src/runtime/provisioner.py
git diff --check -- go-gateway/internal/httpapi/router.go go-gateway/internal/runtimepool/registry.go go-gateway/internal/runtimepool/registry_test.go go-gateway/internal/taskstore/mysql_runtime.go src/runtime/provisioner.py tests/unit/test_runtime_provisioner.py specs/runtime-260518-1025/TECH.md specs/runtime-260518-1025/TEST_REPORT.md
git diff --check
rg -n '^(<<<<<<<|=======|>>>>>>>)' go-gateway/internal/httpapi/router.go go-gateway/internal/runtimepool/registry.go go-gateway/internal/runtimepool/registry_test.go go-gateway/internal/taskstore/mysql_runtime.go specs/runtime-260518-1025/TECH.md specs/runtime-260518-1025/TEST_REPORT.md src/runtime/provisioner.py tests/unit/test_runtime_provisioner.py
#
# 内部验证命令 / admin-only southbound；不得作为浏览器或控制页方案。
curl -sS -H 'X-IronAgent-Internal-Token: [redacted-internal-service-token]' http://127.0.0.1:18080/api/v1/admin/bootstrap
curl -sS -H 'X-IronAgent-Internal-Token: [redacted-internal-service-token]' http://127.0.0.1:18080/api/v1/admin/bootstrap/fast
curl -sS --max-time 12 -N -H 'X-IronAgent-Internal-Token: [redacted-internal-service-token]' http://127.0.0.1:18080/api/v1/admin/assistants/status/stream
/usr/local/bin/docker info --format '{{json .ServerVersion}}'
/usr/local/bin/kubectl config current-context
.venv/bin/python -m pytest tests/unit/test_runtime_provisioner.py
.venv/bin/python -m py_compile src/runtime/provisioner.py src/runtime/kubernetes.py
.venv/bin/ruff check --select F401 src/runtime/kubernetes.py
.venv/bin/python -m pytest tests/unit/test_runtime_provisioner.py::test_k8s_probe_keeps_running_pod_starting_until_gateway_healthz -q
.venv/bin/python -m pytest tests/unit/test_runtime_provisioner.py::test_k8s_provisioner_generates_single_pod_with_workspace_pvc_subpaths tests/unit/test_assistant_service.py::test_sync_runtime_views_fills_k8s_workspace_metadata -q
go test -count=1 ./internal/runtimepool ./internal/taskstore ./internal/httpapi
/Applications/Docker.app/Contents/Resources/bin/docker version --format '{{.Server.Version}}'
/opt/homebrew/bin/go version
```

Observed result: tests, compile, diff checks, API calls, SSE capture, Docker healthz probe verification, shared-MySQL dual Gateway verification, and no-DB dual Gateway negative verification completed successfully. The K8s context check failed because no current context is configured.

Collected artifacts:

- `output/runtime-state-verify-round2/bootstrap.json`
- `output/runtime-state-verify-round2/bootstrap-fast.json`
- `output/runtime-state-verify-round2/status-stream.txt`
- `output/runtime-state-verify-round2/index.html`
- `output/runtime-state-verify-round2/admin-assistants-sync.png` (status badge crop only; token columns excluded)
- `output/runtime-state-verify-round3/browser-fallback/summary.md`
- `output/runtime-state-verify-round3/docker-healthz/summary.md`
- `output/runtime-state-verify-round3/gateway-mysql-live/summary.md`
- `output/runtime-state-verify-round3/gateway-nodb-live/summary.md`
- `output/runtime-state-verify-round3/k8s/summary.md`
- `output/runtime-state-verify-round3/env-var/summary.md`

Additional lint note:

```bash
.venv/bin/python -m ruff check src/runtime/provisioner.py tests/unit/test_runtime_provisioner.py
```

Observed result: failed only on pre-existing `E501` line-length findings in `src/runtime/provisioner.py:1055` and `src/runtime/provisioner.py:1813`; this pass did not change those unrelated lines.

## Acceptance Matrix

| ID | Current status | Evidence | Gap before `verified` |
| --- | --- | --- | --- |
| B1 | `partial/code-present` | Existing runtime model/service tests cover desired, observed, sync state, freshness, and runtime error fields. | No live API/DB evidence. |
| B2 | `partial/local-api-browser` | Existing Admin/status tests cover legacy lifecycle plus freshness fields; local bootstrap/fast/SSE payloads include `observedState`, `desiredState`, `syncState`, timestamps, runtime error, `pollingSource`, and `partial`. Browser screenshot shows status sync badge. | No deployed service evidence. |
| B3 | `verified/local-docker-provisioner; partial-overall` | Docker probe/provisioning unit tests cover healthz-required running state; K8s unit test covers Pod Running with failed healthz staying non-healthy. Round 3 real Docker containers showed `/healthz=200` becomes `running`, while `/healthz=503` and published-port-without-healthz both stay non-running with `last_error="gateway healthz is not reachable"`. | No K8s runtime log and no deployed Admin/API propagation evidence for this specific outage. |
| B4 | `partial/code-present` | Existing service test preserves deleted assistant when probe finds a running container. | Disabled/stopped drift stop/isolate behavior still needs broader evidence. |
| B5 | `partial/code-present` | Existing service test preserves pending state against equivalent stale probe. | Full start/stop/restart/delete/repair/reload pending matrix not covered. |
| B6 | `verified/local-docker-provisioner; partial-ui` | Existing Admin payload/frontend tests expose degraded/freshness fields. Local bootstrap/fast/SSE artifacts show `syncState=degraded`; browser screenshot shows status freshness. Round 3 real Docker healthz failures were surfaced as non-running probe output with explicit `last_error`. | No deployed induced runtime outage browser evidence. |
| B7 | `unverified` | No generation/hash implementation in this pass. | Needs saved-vs-applied generation/hash or equivalent runtime readiness proof. |
| B8 | `unverified` | No reload completion generation/hash proof in this pass. | Needs reload/restart target observation evidence. |
| B9 | `partial/code-present` | Runtime error summaries are carried in runtime fields and Admin payloads. | Formal audit/system-log behavior not newly verified. |
| B10 | `unverified` | No active-task restart/reload behavior was changed or validated. | Needs active task wait/defer/pending evidence. |
| B11 | `partial/local-sse-browser` | Existing Admin SSE/status snapshot tests cover freshness metadata. `status-stream.txt` captured two `assistant_status` events with freshness metadata and `fallbackPollMs=4000`. Round 3 browser/proxy evidence confirms the page attempts `/api/v1/admin/assistants/status/stream` before fallback. | Needs deployed network/SSE capture. |
| B12 | `verified/local-forced-sse-failure` | Existing fallback merge tests cover partial/fallback freshness fields. Round 3 local proxy returned 503 for the SSE endpoint; `fallback-timing.json` shows first post-SSE `/bootstrap/fast` after `4014ms` and later intervals near 4s, while browser state changed to `快速同步`. | Needs deployed browser/network evidence if claiming production behavior. |
| B13 | `blocked/partial-code-present` | Existing frontend string tests cover visibility refresh behavior. Round 3 attempted a real hidden-tab reproduction, but Codex Browser kept `document.hidden=false` and did not expose synthetic event dispatch in read-only evaluate scope. | Needs real browser hidden/visible proof outside Codex Browser limitations. |
| B14 | `verified/local-shared-mysql-gateway` | Go tests cover shared fake-store registry convergence and non-cached store paths. Round 3 started local MySQL `mysql:8.4` plus two Go Gateway processes: A heartbeat was visible from B; B drain was visible from A; A rejected drained preferred target; TTL marked the runtime `unhealthy`. | Needs deployed/shared-secret/full-migration evidence before production verified. |
| B15 | `verified/local-no-db-negative` | Go test proves no-store registries do not share runtime records. Round 3 started two no-DB Go Gateway processes: A read its own `nodb-r3` runtime with HTTP 200 and B returned HTTP 404 `runtime not found`. | Needs deployment documentation to keep no-DB multi-node marked unsupported. |
| B16 | `blocked/partial-code-present` | K8s unit test requires Pod IP plus gateway `/healthz` before healthy running. `/usr/local/bin/kubectl` exists, but `/usr/local/bin/kubectl config current-context` returned `current-context is not set`; no cluster was touched. | Needs approved real K8s context plus Pod phase/IP/healthz/RBAC/network/storage evidence. |
| B17 | `unverified` | No multi-Admin guard/leader/single-writer behavior in this pass. | Needs code/config/deploy evidence or explicit unsupported deployment guard. |
| B18 | `partial/local-evidence-present` | This report records branch, HEAD, scope, commands, residual gaps, and artifact paths under `output/runtime-state-verify-round2/` and `output/runtime-state-verify-round3/`. | Needs deploy/runtime/K8s/multi-Admin artifacts before fully verified claims. |
| B19 | `verified/local-shared-mysql-heartbeat; partial-proxy` | Go tests cover heartbeat preserving scheduler-owned fields; code stops browser proxy session from marking runtime running. Round 3 shared-MySQL dual Gateway evidence shows heartbeat does not clear `in_flight`, `drain_mode`, or `last_error`, and TTL does not restore stale runtime to healthy. | Needs live proxy failure evidence and deployed shared DB evidence before production verified. |

## Acceptance Decision

Current result: `partial/local-verified, not deployment-verified`.

This pass upgrades several local evidence lanes: Docker healthz probe behavior, forced-SSE browser fallback, shared-MySQL dual Gateway registry behavior, and no-DB dual Gateway negative behavior. The spec still cannot move to fully `verified` until deployment, K8s, multi-Admin, hidden/visible browser, and live proxy-failure evidence is collected for the remaining B items.
