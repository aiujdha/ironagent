<!-- Generated: 2026-06-11 | Files scanned: ~25 | Token estimate: ~1200 -->

# IronAgent Data Models

## Overview
IronAgent uses Pydantic models for data validation and dataclasses for internal state. The primary store is **MySQL** (`IRON_AGENT_STORAGE_BACKEND=mysql`, default; SQLAlchemy + PyMySQL, Alembic head `029_workspace_file_long_path_columns`). Workspace-file/event/sync-job metadata defaults to MySQL; the item index and explicit `IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND=sqlite` fallback use SQLite. A few stores (model registry, market catalog) and the legacy `json` backend still use JSON files. See "Storage Locations" below.

## Core Models

### User & User-Assistant Binding (src/users/models.py)

```python
UserRole = Literal["user", "admin", "developer"]

class User:
    user_id: str                    # Unique user identifier
    display_name: str               # Human-readable name
    employee_id: str                # Enterprise employee ID
    department: str                 # Organization department
    role: UserRole                  # Default "user"
    created_at: datetime            # Creation timestamp
    updated_at: datetime            # Last update timestamp

class UserAssistantBinding:
    user_id: str                    # FK to User
    assistant_id: str               # FK to Assistant
    alias: str                      # User-specific assistant name
    source_system: str              # Origin system
    bound_at: datetime              # Binding timestamp
```

### Assistant Record (src/assistants/models.py)

```python
CapabilityProfileName = str         # open string, not a fixed Literal
DEFAULT_CAPABILITY_PROFILES = ("chat", "onlyread", "onlywrite", "work_no_exec", "dangerous")
# CAPABILITY_PROFILES is discovered at import time from config/capabilities/*.yaml

class AssistantRecord:
    assistant_id: str               # Unique assistant ID
    name: str                       # Display name
    capability_profile: CapabilityProfileName  # Security profile (default "onlyread")
    notes: str                      # Admin notes
    iron_agent_config: AssistantIronAgentConfig   # LLM config
    workspace_config: AssistantWorkspaceConfig  # Workspace settings
    runtime: AssistantRuntimeRecord # Runtime state
    created_at: datetime            # Creation timestamp
    updated_at: datetime            # Last update timestamp
    deleted_at: datetime | None     # Soft delete marker (deleted == deleted_at is not None)
```

### Runtime Models (src/runtime/models.py)

```python
RuntimeStatusName = Literal["pending", "starting", "running", "paused",
                            "stopping", "stopped", "restarting", "failed"]
RuntimeSyncStateName = Literal["current", "pending", "stale", "degraded", "failed", "unknown"]
AssistantAuthMode = Literal["host_iron_agent", "provider_api_key"]
WorkspaceSelectionMode = Literal["default_root", "managed_root"]

class AssistantRuntimeRecord:
    container_name: str | None      # Container/Pod name
    container_id: str | None        # Container ID (containerd backend)
    workspace_root: str | None      # Workspace root path
    workspace_root_id: str | None   # Managed workspace root ID
    workspace_slug: str | None      # Workspace directory slug
    temp_dir / persistent_dir: str  # Bucket directory names
    state_root: str | None          # Runtime state path
    status: RuntimeStatusName       # Lifecycle status
    desired_state / observed_state: RuntimeStatusName  # Reconciliation pair
    sync_state: RuntimeSyncStateName  # Derived: current/pending/stale/degraded/failed
    image: str | None               # Runtime image
    gateway_host / gateway_port / gateway_url / gateway_token  # Runtime gateway access
    last_error / last_runtime_error: str
    last_observed_at / last_reconciled_at: datetime | None
    updated_at: datetime

class AssistantIronAgentConfig:
    primary_model: str              # Default "openai-codex/gpt-5.4"
    auth_mode: AssistantAuthMode    # host credentials vs provider API key
    provider_id: str                # Provider identifier
    provider_base_url: str          # API base URL
    provider_api_key: str           # API key (may be "***" placeholder; real key in headers)

class AssistantWorkspaceConfig:
    selection_mode: WorkspaceSelectionMode  # default_root | managed_root
    root_id: str                    # Workspace root ID (default "default")
    workspace_slug: str             # Optional explicit slug
    auto_place_outputs: bool        # Auto temp/persistent placement (default True)
```

### Workspace Files (src/workspace_files/models.py)

```python
TargetBucket = Literal["temp", "persistent", "skills", "plugins"]
FileEventAction = Literal["upload", "overwrite", "download", "delete",
                          "push", "sync", "ingest", "scan_detected"]
FileSyncStatus = Literal["pending", "synced", "synced_to_workspace", "failed", "uploaded",
                         "stored", "disabled", "deleted_from_workspace", "missing_from_workspace"]
FileRole = Literal["input", "output", "result"]
FileDirection = Literal["inbound", "outbound"]

class WorkspaceFile:
    file_id: str                    # UUID
    assistant_id: str               # Owning assistant
    bucket: TargetBucket            # temp/persistent/skills/plugins
    filename: str                   # Original filename
    relative_path: str              # Path within bucket
    direction: FileDirection        # inbound (pushed in) / outbound (result)
    file_role: FileRole             # input/output/result
    source_file_id: str | None      # Originating file for derived results
    owner_user_id / uploader_user_id: str
    size_bytes: int                 # File size
    content_type: str               # MIME type
    source_type / source_ref: str   # Origin (legacy alias: origin)
    storage_provider / storage_backend / storage_key / storage_path: str  # Object storage refs
    storage_status: FileSyncStatus  # Object-store side status
    object_key / object_url: str    # Object storage location
    checksum: str                   # SHA-256 (alias checksum_sha256)
    sync_status: FileSyncStatus     # Workspace sync status
    last_error: str                 # Last sync error (alias sync_error)
    workspace_last_modified_at / last_synced_at: datetime | None
    scan_fingerprint: str           # Result-scan dedup fingerprint
    created_at / updated_at: datetime
    deleted: bool                   # Soft delete flag

class WorkspaceFileEvent:
    event_id: str                   # UUID
    file_id: str                    # FK to WorkspaceFile
    assistant_id: str               # Assistant context
    action: FileEventAction         # What happened
    user_id: str                    # Who did it
    source_type / source_ref: str   # Origin
    detail: dict[str, Any]          # Additional context
    created_at: datetime            # Event timestamp

class ExternalPushRequest:
    push_id: str                    # UUID
    user_id: str                    # Pushing user
    assistant_id: str               # Target assistant
    filename: str                   # File being pushed
    bucket: TargetBucket            # Target bucket
    content_type: str               # MIME type
    storage_backend / storage_key / storage_url: str  # Staged object refs
    source_type / source_ref: str   # Push source
    status: Literal["accepted", "rejected", "pending_approval"]
    approval_id: str | None         # FK to ApprovalRequest
    staged_path: str | None         # Staging location
    error: str                      # Error if rejected
    created_at: datetime            # Push timestamp
```

### Approval Request (src/approval/models.py)

```python
@dataclass(slots=True)
class ApprovalRequest:
    id: str                         # UUID
    tool_name: str                  # Tool requiring approval
    assistant_id: str | None        # Assistant context
    capability_profile: str | None  # Security profile
    approval_scope: Literal["user", "admin"]  # Who can approve (default "admin")
    reason: str | None              # Why approval is needed
    target: str | None              # Target resource (file, etc.)
    status: str                     # "pending"/"approved"/"denied"/"expired"
    resolution: Literal["allow", "deny"] | None  # Final decision
    requested_at: datetime          # Request timestamp
    expires_at: datetime            # Expiration (IRON_AGENT_APPROVAL_TIMEOUT_MINUTES, default 15min)
    resolved_at: datetime | None    # When resolved
    gateway_url: str | None         # Gateway URL for notification
```

### Collaboration (src/collaboration/models.py)

```python
CollaborationTaskStatusName = Literal["pending_approval", "pending_claim", "claimed", "running",
                                      "completed", "failed", "expired", "cancelled", "rejected"]

class SharedWorkspaceObject:   # read-only shared object in object storage (COS by default)
    id, filename, sha256, storage_backend/bucket/key, source_assistant_id,
    visibility_scope ("task_only"|"shared_persistent"), retention_policy,
    status ("active"|"expired"|"soft_deleted"|"deleted"), expires_at, ...

class SharedObjectPermission:  # read grant to an assistant or task
class CollaborationTask:       # cross-assistant task: source/target assistant, input_object_id,
                               # instruction, status, approval_mode, result_object_id, timeout
class CollaborationClaim:      # claim_token + lease (lease_expires_at, status active/released/expired)
class CollaborationEvent:      # task event trail
```

### Gateway Task Store (go-gateway/internal/taskstore, MySQL)

Go Gateway persists its northbound task plane in the same MySQL database:
`gateway_tasks`, `gateway_task_queue`, `gateway_task_events`, `gateway_event_outbox`,
`gateway_event_sequences`, `gateway_webhook_deliveries`, `gateway_dead_letters`,
`gateway_idempotency_keys`, `gateway_hmac_nonces`, `gateway_runtime_instances`,
`gateway_runtime_leases`, `gateway_conversation_routes`. Models live in
go-gateway/internal/core/models.go; schema is managed by the Python Alembic migrations.

## Relationships

```
User ──┬───< UserAssistantBinding >───> Assistant
       │
       └───< created >───> WorkspaceFileEvent

Assistant ──┬───< has >───> WorkspaceFile
             ├───< has >───> WorkspaceFileEvent
             ├───< triggers >───> ApprovalRequest
             ├───< runs in >───> AssistantRuntimeRecord
             └───< source/target of >───> CollaborationTask

WorkspaceFile ──< has >───> WorkspaceFileEvent

ApprovalRequest ───< may be referenced by >───> ExternalPushRequest / CollaborationTask

CollaborationTask ──< claimed via >──> CollaborationClaim
                  └──< reads >──> SharedWorkspaceObject (via SharedObjectPermission)
```

## Storage Locations

**MySQL** (`IRON_AGENT_DATABASE_URL`, default `mysql+pymysql://...@mysql:3306/ironagent`; Alembic migrations in migrations/, head 029):
- Control plane: `assistants`, `approvals`, `audit_events`, `users`, `user_assistant_bindings`, `workspace_files`, `workspace_file_events`, `external_push_requests`
- Collaboration: `shared_workspace_objects`, `shared_object_permissions`, `collaboration_tasks`, `collaboration_claims`, `collaboration_events`
- Gateway: `gateway_*` tables (see above)
- Workspace file metadata and sync queue: `workspace_files`, `workspace_file_events`, `workspace_sync_jobs` by default

**SQLite** (fallback/index path for workspace-file metadata, src/workspace_files/):
- `data/workspace.db` — workspace_files + workspace_file_events only when `IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND=sqlite`
- `data/workspace_item_index.db` — assistant_workspace_items (installed skills/plugins index)
- `data/workspace_sync_jobs.db` — workspace_sync_jobs queue only when `IRON_AGENT_WORKSPACE_FILE_METADATA_BACKEND=sqlite`

**File system** (under `IRON_AGENT_STACK_ROOT/data/` by default; PVC subPath in K8s):
- `workspaces/{slug}/temp|persistent/` — assistant workspaces
- `workspace_objects/` — local object-storage backend (alternatives: Tencent COS / Huawei OBS)
- `market/catalog.json`, `market/packages/`, `market/uploads/` — market catalog & packages (JSON/files)
- `models/registry.json` — model registry (JSON)
- `audit/audit.jsonl` — audit log only when `IRON_AGENT_STORAGE_BACKEND=json` (legacy fallback; MySQL mode writes `audit_events`)
