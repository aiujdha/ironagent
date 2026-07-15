# IronAgent Integration

This directory hosts the IronAgent-side security hook seam. The hook client
is implemented here; deeper fork/submodule integration remains deferred scope.

Implemented files under `ironagent/src/hooks/`:

- `security-hook.ts` — builds pre/post-execute hook requests and runs them
  against the security service (fail-closed by default)
- `hook-client.ts` — HTTP client for `/api/v1/hooks/pre-execute` and
  `/api/v1/hooks/post-execute`
- `hook-config.ts` — resolves configuration from `IRON_AGENT_SERVICE_URL`,
  `IRON_AGENT_HOOK_TIMEOUT_MS`, `IRON_AGENT_FAIL_CLOSED`, `IRON_AGENT_API_TOKEN`
- `types.ts` — hook protocol types (protocol version `2026-04-03`)
- `__tests__/security-hook.test.ts` — hook tests

Protocol contract: see `docs/api/ironagent-hook-contract.md`.
