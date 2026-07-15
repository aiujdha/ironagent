# IronAgent Project Context

## Project Overview

The IronAgent Python control plane is the enterprise security control layer for the IronAgent assistant runtime.

The architecture is intentionally split:

- The IronAgent WebUI / assistant runtime (`feat/webui` line) remains the execution plane
- The IronAgent Python control plane (this repo's `src/`) provides the decision plane
- The Go Gateway (`go-gateway/`) is the single northbound entry in front of both

## Current Priorities

1. Go Gateway as the only externally exposed entry (chat/tasks, callback/fallback, HTTP file APIs)
2. Webhook-first IM integration: callback is the main path, fallback is compensation query only
3. K8s deployment: assistant runtime as K8s Pod + PVC subPath, MySQL in-cluster as the control-plane store
4. Observability and fault attribution across upstream / gateway / runtime
5. Admin web console iteration (`src/admin_web`)

## Read These First

- `docs/START_HERE.md` — unified documentation entry for humans and AI
- `docs/PROJECT_LOGIC_SOURCE_OF_TRUTH.md` — authoritative system boundaries and protocol facts
- `docs/AI_CODING_RULES.md` — mandatory reading before AI-assisted code changes

## Working Rules

- Prefer Python 3.11+ and async-first patterns
- Use Pydantic Settings for configuration
- Use structlog for application logging
- Keep IronAgent integration isolated to the reserved `ironagent/` area
- Do not spread IronAgent-specific logic across unrelated Python modules
- Runtime agents should follow workspace AGENTS guidance: package three or more user-facing result files into a single `.zip`, without Gateway-side result rewriting.

## Deferred Scope

- Deep IronAgent fork integration beyond the hook seam (`ironagent/` is still a reserved placeholder)

## Coding Preferences

- Keep functions small and typed
- Use clear dataclasses or Pydantic models at boundaries
- Prefer fail-closed defaults for security decisions
