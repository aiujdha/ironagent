# Workspace File Sync API

> **Scope: southbound internal API.** The `/api/v1/workspace-files/*` endpoints described here are
> served by `security-service` (`src/security_service/file_routes.py`) and are part of the
> southbound/internal control plane. Except for the two `GET /api/v1/users/{user_id}/...` endpoints,
> they require the internal service token (`require_internal_service_token`). Do not confuse them
> with the northbound file capability (`/api/v1/files/*`, MCP/presigned flow) documented in
> `docs/接口契约.md`.

This document describes the current IronAgent workspace file flow for WebUI-style clients.

## Upload

Endpoint:

```text
POST /api/v1/workspace-files/upload
```

Important form fields:

- `assistant_id` or `user_id`
- `target_bucket`: `temp` or `persistent`
- `overwrite`
- `source_type`
- `source_ref`
- `uploader_user_id`
- `file`

Behavior:

- upload metadata is recorded by `security-service`
- file content is mirrored into the assistant workspace
- if COS is configured, object storage metadata is also recorded
- uploads are applied directly and recorded as `accepted`; no approval is triggered on upload
  (approval currently applies to deleting `persistent` files via
  `POST /api/v1/workspace-files/{file_id}/delete`)

## Push

Endpoint:

```text
POST /api/v1/workspace-files/push
```

Same form fields and behavior as upload; `user_id` is also used as the owner/uploader of the file.

## Sync Results

Endpoint:

```text
POST /api/v1/workspace-files/sync-results
```

Query parameters: `assistant_id`, `bucket`, `user_id`, `changed_after`, `result_path_prefix`,
`task_id`, `source_ref`. Runs an on-demand workspace result scan (same logic as the background
scan below), optionally scoped to a task result path.

## List

Endpoint:

```text
GET /api/v1/workspace-files?assistant_id=<id>&bucket=<optional>
```

Query parameters: `assistant_id` or `user_id` (one required), plus optional `bucket`, `direction`,
`file_role`, and `sync` (`sync=true` refreshes the assistant workspace via `WorkspaceSyncService`
before listing).

Important response fields per file:

- `origin` (alias of `source_type`; free-form string, not a closed enum)
  - caller-provided values such as `web_upload`
  - `assistant_result` / `capability_package` (written by the workspace scan)
  - `manual_import`, `cos_directory` (legacy object-storage import)
- `sync_status` (one of `pending`, `synced`, `failed`, `uploaded`, `stored`, `disabled`,
  `missing_from_workspace`, `deleted_from_workspace`; the internal value `synced_to_workspace`
  is reported as `synced`)
- `storage_backend`
- `storage_key`
- `storage_url`
- `workspace_path`
- `last_synced_at`

## User List

Endpoint:

```text
GET /api/v1/users/{user_id}/workspace-files?bucket=<optional>
```

Lists files owned by the user (`owner_user_id`); no internal service token required.
A per-user download variant also exists:
`GET /api/v1/users/{user_id}/workspace-files/{file_id}/download` (403 on owner mismatch).

## Overview

Endpoint:

```text
GET /api/v1/workspace-files/overview?assistant_id=<id>  (or user_id=<id>)
```

Returns `total`, `input_count`, `result_count`, and the 5 most recent result files.

## Download

Endpoint:

```text
GET /api/v1/workspace-files/{file_id}/download
```

Behavior:

- content is read from the assistant workspace only; if the workspace copy is missing,
  the endpoint returns 404 (`content_not_found`)
- the response carries `X-IronAgent-Download-Source` (currently always `workspace`) and
  `X-IronAgent-Storage-Backend` headers

Additional file endpoints (see `src/security_service/file_routes.py`):
`GET /api/v1/workspace-files/{file_id}`, `GET .../{file_id}/events`, `POST .../{file_id}/sync`,
`POST /api/v1/workspace-files/scan`, `GET .../{file_id}/download-info`, `POST .../{file_id}/delete`.

## Background Sync

`security-service` runs a workspace result scan loop when:

- `IRON_AGENT_WORKSPACE_RESULT_SCAN_ENABLED=true` (default: disabled)

Scan interval:

- `IRON_AGENT_WORKSPACE_RESULT_SCAN_INTERVAL_SECONDS` (default: 10)

Behavior:

- scans the `temp`, `persistent`, `skills`, and `plugins` buckets of every non-deleted assistant
- imports new assistant result files (`source_type=assistant_result`; `capability_package`
  for `skills`/`plugins`)
- updates changed files
- soft-deletes records whose assistant result / capability package file disappeared from
  the workspace (the record is marked deleted, not surfaced as `deleted_from_workspace`)
- inbound (uploaded) files are skipped by this scan

The `deleted_from_workspace` / `missing_from_workspace` statuses are set by the on-demand
workspace sync (`WorkspaceSyncService`, e.g. list with `sync=true`), not by this loop.
