# Persona Memory Hook

An IronAgent hook that extracts **who-you-are-to-this-user** signals into a
persistent, evergreen per-user profile, so the agent's persona does not drift
as conversations get compacted.

## Why It Exists

IronAgent's context compaction is task-oriented (`Goal / Progress / Decisions`).
It preserves *what was done* but routinely drops *how the agent relates to this
user* — tone, preferences, friction points, established rapport. Over long
sessions this causes the persona to flatten back toward a generic chatbot.

This hook is the second layer of the **persona anchoring** strategy:

1. **Static layer** — `SOUL.md` now carries a `Persona Invariants` section that
   re-anchors the baseline every turn (see
   `deployment/kustomize/ironagent/base/files/templates/SOUL.md`).
2. **Dynamic layer (this hook)** — extracts relational signals per user into
   `memory/persona/<user_key>.md`, an evergreen file that compaction does not
   touch and temporal decay skips.

## How It Works

When a session ends (or is reset), the hook:

1. Reads the recent transcript tail.
2. Sends it to the IronAgent security-service persona endpoint, which uses the
   configured LLM to extract observations across four dimensions:
   - **User Profile** — background, communication style, recurring needs
   - **Rapport** — how the relationship has evolved
   - **Tone Calibration** — which tone fits which context for this user
   - **Friction / Redlines** — behaviors to avoid next time
3. Merges the result incrementally into `memory/persona/<user_key>.md`.
   Existing entries are preserved; only new observations are appended.
4. Writes nothing when the session had no persona signal — the profile is never
   polluted with forced guesses.

The profile path carries no date, so memory-core's temporal-decay pass skips
it. The persona profile is always injected at full strength.

## Files

- `persona-hook.ts` — hook handler that resolves the session and dispatches the
  extraction request.
- `persona-client.ts` — HTTP client for the `POST /api/v1/hooks/persona-extract`
  endpoint on the security-service.

## Configuration

| Env | Default | Description |
| --- | --- | --- |
| `IRON_AGENT_PERSONA_MEMORY_ENABLED` | `true` | Master switch |
| `IRON_AGENT_PERSONA_MEMORY_MESSAGES` | `15` | Transcript tail length scanned |
| `IRON_AGENT_PERSONA_MEMORY_TIMEOUT_MS` | `20000` | Extraction request timeout |

## Endpoint Contract

The hook calls `POST {IRON_AGENT_SERVICE_URL}/api/v1/hooks/persona-extract` with:

```json
{
  "session_key": "...",
  "user_key": "...",
  "messages": 15,
  "existing_profile": "... markdown ..."
}
```

The service returns the merged markdown profile, or `{"profile": null}` when
there is no new persona signal. The endpoint implementation lives in the
Python control plane (security-service).
