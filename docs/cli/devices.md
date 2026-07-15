---
summary: "CLI reference for `ironagent devices` (device pairing + token rotation/revocation)"
read_when:
  - You are approving device pairing requests
  - You need to rotate or revoke device tokens
title: "devices"
---

# `ironagent devices`

Manage device pairing requests and device-scoped tokens.

## Commands

### `ironagent devices list`

List pending pairing requests and paired devices.

```
ironagent devices list
ironagent devices list --json
```

Pending request output includes the requested role and scopes so approvals can
be reviewed before you approve.

### `ironagent devices remove <deviceId>`

Remove one paired device entry.

```
ironagent devices remove <deviceId>
ironagent devices remove <deviceId> --json
```

### `ironagent devices clear --yes [--pending]`

Clear paired devices in bulk.

```
ironagent devices clear --yes
ironagent devices clear --yes --pending
ironagent devices clear --yes --pending --json
```

### `ironagent devices approve [requestId] [--latest]`

Approve a pending device pairing request. If `requestId` is omitted, IronAgent
automatically approves the most recent pending request.

Note: if a device retries pairing with changed auth details (role/scopes/public
key), IronAgent supersedes the previous pending entry and issues a new
`requestId`. Run `ironagent devices list` right before approval to use the
current ID.

```
ironagent devices approve
ironagent devices approve <requestId>
ironagent devices approve --latest
```

### `ironagent devices reject <requestId>`

Reject a pending device pairing request.

```
ironagent devices reject <requestId>
```

### `ironagent devices rotate --device <id> --role <role> [--scope <scope...>]`

Rotate a device token for a specific role (optionally updating scopes).

```
ironagent devices rotate --device <deviceId> --role operator --scope operator.read --scope operator.write
```

### `ironagent devices revoke --device <id> --role <role>`

Revoke a device token for a specific role.

```
ironagent devices revoke --device <deviceId> --role node
```

## Common options

- `--url <url>`: Gateway WebSocket URL (defaults to `gateway.remote.url` when configured).
- `--token <token>`: Gateway token (if required).
- `--password <password>`: Gateway password (password auth).
- `--timeout <ms>`: RPC timeout.
- `--json`: JSON output (recommended for scripting).

Note: when you set `--url`, the CLI does not fall back to config or environment credentials.
Pass `--token` or `--password` explicitly. Missing explicit credentials is an error.

## Notes

- Token rotation returns a new token (sensitive). Treat it like a secret.
- These commands require `operator.pairing` (or `operator.admin`) scope.
- `devices clear` is intentionally gated by `--yes`.
- If pairing scope is unavailable on local loopback (and no explicit `--url` is passed), list/approve can use a local pairing fallback.

## Token drift recovery checklist

Use this when Control UI or other clients keep failing with `AUTH_TOKEN_MISMATCH` or `AUTH_DEVICE_TOKEN_MISMATCH`.

1. Confirm current gateway token source:

```bash
ironagent config get gateway.auth.token
```

2. List paired devices and identify the affected device id:

```bash
ironagent devices list
```

3. Rotate operator token for the affected device:

```bash
ironagent devices rotate --device <deviceId> --role operator
```

4. If rotation is not enough, remove stale pairing and approve again:

```bash
ironagent devices remove <deviceId>
ironagent devices list
ironagent devices approve <requestId>
```

5. Retry client connection with the current shared token/password.

Related:

- [Dashboard auth troubleshooting](/web/dashboard#if-you-see-unauthorized-1008)
- [Gateway troubleshooting](/gateway/troubleshooting#dashboard-control-ui-connectivity)
