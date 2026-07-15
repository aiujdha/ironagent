import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

StorageBackendName = Literal["json", "mysql"]
WorkspaceFileMetadataBackend = Literal["", "json", "mysql", "sqlite"]
ResolvedWorkspaceFileMetadataBackend = Literal["mysql", "sqlite"]
WorkspaceObjectStorageBackend = Literal["local", "cos", "obs"]
RuntimeBackendName = Literal["containerd", "kubernetes"]
ApprovalMode = Literal["enabled", "disabled"]

IRON_AGENT_ENV_PREFIX = "IRON_AGENT_"
IRONAGENT_ENV_PREFIX = "IRONAGENT_"


def _compat_env_names(field_name: str) -> list[str]:
    suffix = field_name.upper()
    names = [
        f"{IRONAGENT_ENV_PREFIX}{suffix}",
        f"{IRON_AGENT_ENV_PREFIX}{suffix}",
    ]
    if "IRON_AGENT" in suffix:
        compact_suffix = suffix.replace("IRON_AGENT", "IRONAGENT")
        names.extend(
            [
                f"{IRONAGENT_ENV_PREFIX}{compact_suffix}",
                f"{IRON_AGENT_ENV_PREFIX}{compact_suffix}",
            ]
        )
    if field_name.startswith("iron_agent_"):
        runtime_suffix = field_name.removeprefix("iron_agent_").upper()
        names.extend(
            [
                f"{IRONAGENT_ENV_PREFIX}RUNTIME_{runtime_suffix}",
                f"{IRON_AGENT_ENV_PREFIX}RUNTIME_{runtime_suffix}",
            ]
        )
    if field_name == "host_iron_agent_root":
        names.extend(
            [
                "IRONAGENT_HOST_IRONAGENT_ROOT",
                "IRON_AGENT_HOST_IRONAGENT_ROOT",
            ]
        )
    return list(dict.fromkeys(names))


def _compat_env_source() -> dict[str, str]:
    values: dict[str, str] = {}
    for field_name in Settings.model_fields:
        for env_name in _compat_env_names(field_name):
            if env_name in os.environ:
                values[field_name] = os.environ[env_name]
                break
    return values


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix=IRON_AGENT_ENV_PREFIX,
        extra="ignore",
        populate_by_name=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: Any,
        env_settings: Any,
        dotenv_settings: Any,
        file_secret_settings: Any,
    ) -> tuple[Any, ...]:
        return (
            init_settings,
            _compat_env_source,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )

    env: str = "development"
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    storage_backend: StorageBackendName = "mysql"
    seed_default_assistants: bool = False
    config_dir: Path = Field(default=Path("./config"))
    audit_log_path: Path = Field(default=Path("./data/audit/audit.jsonl"))
    audit_archive_path: Path = Field(default=Path("./data/audit/archive.jsonl"))
    audit_retention_days: int = 45
    assistants_store_path: Path = Field(default=Path("./data/assistants/assistants.json"))
    approval_store_path: Path = Field(default=Path("./data/approvals/approvals.json"))
    approval_mode: ApprovalMode = "disabled"
    approval_timeout_minutes: int = 15
    assistants_workspace_root: Path = Field(default=Path("./data/workspaces"))
    allowed_workspace_roots_json: str = ""
    default_workspace_root_id: str = "default"
    assistants_runtime_root: Path = Field(default=Path("./data/runtimes"))
    market_store_path: Path = Field(default=Path("./data/market/catalog.json"))
    market_package_root: Path = Field(default=Path("./data/market/packages"))
    market_upload_root: Path = Field(default=Path("./data/market/uploads"))
    model_registry_store_path: Path = Field(default=Path("./data/models/registry.json"))
    users_store_path: Path = Field(default=Path("./data/users/users.json"))
    user_bindings_store_path: Path = Field(default=Path("./data/users/bindings.json"))
    workspace_files_store_path: Path = Field(default=Path("./data/workspace_files/files.json"))
    workspace_file_events_store_path: Path = Field(
        default=Path("./data/workspace_files/events.json")
    )
    workspace_push_requests_store_path: Path = Field(
        default=Path("./data/workspace_files/push_requests.json")
    )
    workspace_file_metadata_backend: WorkspaceFileMetadataBackend = "mysql"
    workspace_file_sqlite_path: Path = Field(default=Path("./data/workspace.db"))
    workspace_item_index_sqlite_path: Path = Field(default=Path("./data/workspace_item_index.db"))
    workspace_file_sqlite_busy_timeout_ms: int = 30000
    workspace_file_sqlite_cache_size_mb: int = 64
    workspace_file_sqlite_wal_autocheckpoint: int = 1000
    iron_agent_workspace_file_sqlite_mount_path: str = Field(
        default="/home/node/.ironagent/workspace-metadata",
        validation_alias="IRON_AGENT_RUNTIME_WORKSPACE_FILE_SQLITE_MOUNT_PATH",
    )
    workspace_object_storage_backend: WorkspaceObjectStorageBackend = "local"
    workspace_object_storage_root: Path = Field(default=Path("./data/workspace_objects"))
    workspace_object_storage_prefix: str = "workspace-files"
    workspace_result_scan_enabled: bool = False
    workspace_result_scan_interval_seconds: int = 10
    workspace_sync_job_sqlite_path: Path = Field(default=Path("./data/workspace_sync_jobs.db"))
    workspace_sync_job_worker_enabled: bool = True
    workspace_sync_job_worker_interval_seconds: int = 2
    workspace_sync_job_worker_batch_size: int = 1
    workspace_sync_job_stale_after_seconds: int = 7200
    collaboration_object_storage_backend: str = "cos"
    collaboration_object_storage_bucket: str = ""
    collaboration_object_storage_region: str = ""
    collaboration_object_storage_endpoint: str = ""
    collaboration_object_storage_prefix: str = "collaboration/"
    collaboration_claim_lease_seconds: int = 300
    collaboration_auto_create_tables: bool = True
    host_iron_agent_root: Path = Field(default=Path.home() / ".ironagent")
    iron_agent_source_root: Path | None = Field(
        default=None,
        validation_alias="IRON_AGENT_RUNTIME_SOURCE_ROOT",
    )
    iron_agent_seed_workspace_skills: str = Field(
        default="",
        validation_alias="IRON_AGENT_RUNTIME_SEED_WORKSPACE_SKILLS",
    )
    host_iron_agent_agent_id: str = "main"
    runtime_backend: RuntimeBackendName = "containerd"
    iron_agent_service_url: str = Field(
        default="http://localhost:3000",
        validation_alias="IRON_AGENT_RUNTIME_SERVICE_URL",
    )
    iron_agent_container_image: str = Field(
        default="ironagent-runtime:dev",
        validation_alias="IRON_AGENT_RUNTIME_CONTAINER_IMAGE",
    )
    iron_agent_state_mount_path: str = Field(
        default="/home/node/.ironagent",
        validation_alias="IRON_AGENT_RUNTIME_STATE_MOUNT_PATH",
    )
    iron_agent_workspace_mount_path: str = Field(
        default="/workspace",
        validation_alias="IRON_AGENT_RUNTIME_WORKSPACE_MOUNT_PATH",
    )
    assistant_container_name_prefix: str = "ironagent-assistant"
    iron_agent_gateway_host: str = Field(
        default="127.0.0.1",
        validation_alias="IRON_AGENT_RUNTIME_GATEWAY_HOST",
    )
    iron_agent_gateway_publish_host: str = Field(
        default="",
        validation_alias="IRON_AGENT_RUNTIME_GATEWAY_PUBLISH_HOST",
    )
    iron_agent_gateway_access_host: str = Field(
        default="",
        validation_alias="IRON_AGENT_RUNTIME_GATEWAY_ACCESS_HOST",
    )
    iron_agent_gateway_port: int = Field(
        default=18789,
        validation_alias="IRON_AGENT_RUNTIME_GATEWAY_PORT",
    )
    iron_agent_gateway_bind: str = Field(
        default="lan",
        validation_alias="IRON_AGENT_RUNTIME_GATEWAY_BIND",
    )
    iron_agent_http_proxy: str = Field(
        default="",
        validation_alias="IRON_AGENT_RUNTIME_HTTP_PROXY",
    )
    iron_agent_https_proxy: str = Field(
        default="",
        validation_alias="IRON_AGENT_RUNTIME_HTTPS_PROXY",
    )
    iron_agent_no_proxy: str = Field(
        default="",
        validation_alias="IRON_AGENT_RUNTIME_NO_PROXY",
    )
    iron_agent_hooks_enabled: bool = Field(
        default=True,
        validation_alias="IRON_AGENT_RUNTIME_HOOKS_ENABLED",
    )
    iron_agent_hook_service_url: str = Field(
        default="http://host.docker.internal:8000",
        validation_alias="IRON_AGENT_RUNTIME_HOOK_SERVICE_URL",
    )
    iron_agent_hook_fail_closed: bool = Field(
        default=True,
        validation_alias="IRON_AGENT_RUNTIME_HOOK_FAIL_CLOSED",
    )
    iron_agent_hook_api_token: str = Field(
        default="",
        validation_alias="IRON_AGENT_RUNTIME_HOOK_API_TOKEN",
    )
    iron_agent_hook_timeout_ms: int = Field(
        default=5000,
        validation_alias="IRON_AGENT_RUNTIME_HOOK_TIMEOUT_MS",
    )
    iron_agent_approval_poll_ms: int = Field(
        default=1000,
        validation_alias="IRON_AGENT_RUNTIME_APPROVAL_POLL_MS",
    )
    iron_agent_approval_max_wait_ms: int = Field(
        default=300000,
        validation_alias="IRON_AGENT_RUNTIME_APPROVAL_MAX_WAIT_MS",
    )
    iron_agent_container_extra_host: str = Field(
        default="host.docker.internal:host-gateway",
        validation_alias="IRON_AGENT_RUNTIME_CONTAINER_EXTRA_HOST",
    )
    docker_binary: str = "docker"
    docker_socket_path: str = "/var/run/docker.sock"
    k8s_runtime_namespace: str = "ironagent"
    k8s_runtime_kubectl_binary: str = "kubectl"
    k8s_runtime_workspace_pvc: str = "assistant-workspaces"
    k8s_runtime_workspace_subpath_prefix: str = "assistants"
    k8s_runtime_state_dir: str = ".ironagent"
    k8s_runtime_init_image: str = ""
    k8s_runtime_image_pull_policy: str = "IfNotPresent"
    k8s_runtime_service_account_name: str = ""
    k8s_runtime_cpu_request: str = "100m"
    k8s_runtime_memory_request: str = "256Mi"
    k8s_runtime_cpu_limit: str = "1"
    k8s_runtime_memory_limit: str = "2Gi"
    k8s_runtime_delete_workspace_on_destroy: bool = False
    hook_timeout_ms: int = 5000
    internal_service_token: str = "change-me"
    control_ui_public_url: str = ""
    go_gateway_public_url: str = ""
    security_service_public_url: str = ""
    security_service_internal_url: str = ""
    control_ui_launch_ticket_ttl_seconds: int = 600
    control_ui_allowed_origins_json: str = ""
    control_ui_allowed_origin_regex: str = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
    admin_bootstrap_fast_cache_ttl_seconds: float = 10.0
    admin_assistant_status_cache_ttl_seconds: float = 5.0
    admin_assistant_status_fallback_poll_ms: int = 4000
    database_url: str = "mysql+pymysql://ironagent:ironagent@localhost:3306/ironagent?charset=utf8mb4"
    db_pool_size: int = 2
    db_max_overflow: int = 3
    db_pool_timeout_seconds: int = 30
    db_pool_recycle_seconds: int = 1800
    db_connect_timeout_seconds: int = 10
    redis_url: str = "redis://localhost:6379/0"
    cos_secret_id: str = ""
    cos_secret_key: str = ""
    cos_session_token: str = ""
    cos_bucket: str = ""
    cos_region: str = ""
    cos_endpoint: str = ""
    cos_prefix: str = ""
    obs_access_key_id: str = ""
    obs_secret_access_key: str = ""
    obs_bucket: str = ""
    obs_endpoint: str = ""
    obs_prefix: str = ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def resolve_workspace_file_metadata_backend(
    value: WorkspaceFileMetadataBackend | str | None = None,
    *,
    settings: Settings | None = None,
) -> ResolvedWorkspaceFileMetadataBackend:
    raw = value
    if raw is None:
        raw = (settings or get_settings()).workspace_file_metadata_backend
    return "sqlite" if str(raw or "").strip().lower() == "sqlite" else "mysql"


def approval_mode_disabled(settings: Settings | None = None) -> bool:
    return (settings or get_settings()).approval_mode == "disabled"
