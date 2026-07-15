# IronAgent Hook Contract

This document defines the first IronAgent hook contract between the IronAgent execution plane and
the IronAgent decision plane.

## Version

- `protocol_version`: `2026-04-03`

## Endpoints

- `POST /api/v1/hooks/pre-execute`
- `POST /api/v1/hooks/post-execute`

Legacy aliases retained for the current scaffold:

- `POST /api/v1/security/check`
- `POST /api/v1/audit/log`

## Pre-execute request

```json
{
  "envelope": {
    "protocol_version": "2026-04-03",
    "request_id": "9d9b2c62-8609-4178-a7a3-4b05e40135bc",
    "source": "ironagent",
    "hook_name": "pre_execute",
    "occurred_at": "2026-04-03T09:00:00Z"
  },
  "assistant": {
    "assistant_id": "assistant-001",
    "name": "finance-helper",
    "capability_profile": "dangerous"
  },
  "session": {
    "session_id": "session-123",
    "run_id": "run-456",
    "conversation_id": "conv-789",
    "channel": "telegram",
    "user_id": "user-001"
  },
  "tool": {
    "name": "exec",
    "args": {
      "command": "ls -la"
    },
    "source": "core",
    "optional": false,
    "owner_only": false
  },
  "risk": {
    "level": "high",
    "reason": "shell execution",
    "tags": ["shell", "filesystem"]
  },
  "metadata": {
    "iron_agent_version": "placeholder",
    "node_id": "gateway-local"
  }
}
```

Field defaults when omitted: `assistant.capability_profile` defaults to `"onlyread"`, `risk.level` defaults to `"medium"`.

## Pre-execute response

```json
{
  "protocol_version": "2026-04-03",
  "request_id": "9d9b2c62-8609-4178-a7a3-4b05e40135bc",
  "decision": "pending_approval",
  "reason": "tool requires approval",
  "capability_profile": "dangerous",
  "fail_closed": true,
  "approval": {
    "required": true,
    "approval_id": "1d5d8c62-8b6d-4f17-b627-3642d1234567",
    "status": "pending",
    "expires_at": "2026-04-03T09:05:00Z"
  },
  "metadata": {},
  "tool_args": null
}
```

- `tool_args` (`object | null`): when workspace auto-placement rewrites the tool arguments
  (for example relocating `write`/`edit`/`apply_patch` targets into the assistant workspace),
  the rewritten arguments are returned here. When non-null, the execution plane must execute
  the tool with these arguments instead of the original `tool.args`.

## Decision semantics

- `allow`: IronAgent may execute the tool immediately
- `deny`: IronAgent must not execute the tool
- `pending_approval`: IronAgent should suspend execution until approval resolves or times out

## Demo capability matrix

- `chat`: deny every tool call
- `onlyread`: allow read and retrieval tools only
- `onlywrite`: allow file inspection and file modification, but not shell execution
- `work_no_exec`: allow file inspection and modification (level 2), but no shell, process, code execution, or subagent access; no tools require approval
- `dangerous`: allow high-risk execution paths, but still route shell-like operations to approval

## Post-execute request

```json
{
  "envelope": {
    "protocol_version": "2026-04-03",
    "request_id": "9d9b2c62-8609-4178-a7a3-4b05e40135bc",
    "source": "ironagent",
    "hook_name": "post_execute",
    "occurred_at": "2026-04-03T09:00:10Z"
  },
  "assistant": {
    "assistant_id": "assistant-001",
    "capability_profile": "dangerous"
  },
  "session": {
    "session_id": "session-123",
    "run_id": "run-456"
  },
  "tool": {
    "name": "exec",
    "args": {
      "command": "ls -la"
    }
  },
  "result": {
    "success": true,
    "duration_ms": 241,
    "output_summary": {
      "exit_code": 0
    }
  },
  "decision": "allow",
  "metadata": {}
}
```

- `result.error` (`string | null`, optional): error message when `success` is `false`.
- Note: the go-gateway also reports public API failures in this post-execute shape (sent to
  `/api/v1/audit/log`) with `envelope.source` = `"go-gateway"`, `assistant.capability_profile` =
  `"public-api"`, `decision` = `"deny"`, and `result.error` carrying the failure message.

## Post-execute response

```json
{
  "protocol_version": "2026-04-03",
  "request_id": "9d9b2c62-8609-4178-a7a3-4b05e40135bc",
  "status": "ok"
}
```
