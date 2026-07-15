from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import hmac
import importlib
import inspect
import io
import ipaddress
import json
import re
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import uuid4

import yaml
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import AliasChoices, BaseModel, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.datastructures import MutableHeaders

from src.admin.capability_install import AssistantCapabilityInstallService
from src.admin.control_ui_launch import mint_control_ui_launch_ticket
from src.admin.gateway_links import (
    build_go_gateway_assistant_ui_url,
    build_spdbpanda_chat_launch_url,
    normalize_control_ui_gateway_url,
)
from src.approval.service import ApprovalService
from src.assistants.models import (
    CAPABILITY_PROFILES,
    DEFAULT_CAPABILITY_PROFILES,
    CapabilityProfileName,
)
from src.assistants.service import MASKED_API_KEY, AssistantService
from src.assistants_service.router import router as assistants_router
from src.market.audit import capability_audit_event
from src.market.routes import router as market_router
from src.market.service import MarketService
from src.model_registry.service import ModelRegistryService
from src.runtime.models import AssistantIronAgentConfig, AssistantWorkspaceConfig
from src.security_service.file_routes import router as workspace_files_router
from src.security_service.schemas import HOOK_PROTOCOL_VERSION
from src.security_service.user_routes import router as users_router
from src.shared.auth import (
    INTERNAL_SERVICE_TOKEN_HEADER,
    INTERNAL_SERVICE_TOKEN_HEADERS,
    require_internal_service_token,
)
from src.shared.config import get_settings
from src.shared.database import dispose_database_engines
from src.shared.logging import get_logger
from src.shared.repositories import (
    AuditLogFilters,
    audit_payload_assistant_id,
    audit_payload_error_code,
    audit_payload_operation_type,
    audit_payload_result,
    audit_payload_user_id,
    build_audit_repository,
)
from src.shared.workspace_paths import runtime_workspace_path
from src.users.service import UserService
from src.workspace_files.item_index_writer import (
    record_workspace_item_removed,
    record_workspace_item_reused,
    record_workspace_item_toggle,
)
from src.workspace_files.models import WorkspaceFile
from src.workspace_files.readiness import check_workspace_file_metadata_readiness
from src.workspace_files.service import WorkspaceFileService
from src.workspace_files.sync_service import WorkspaceSyncService

log = get_logger(__name__)
ADMIN_DIRECT_SESSION_COOKIE = "iron_agent_admin_direct_session"
LEGACY_ADMIN_DIRECT_SESSION_COOKIE = "iron_agent_admin_direct_session"
ADMIN_DIRECT_SESSION_TTL_SECONDS = 30 * 60
ASSISTANT_RUNTIME_HEALTH_INTERVAL_SECONDS = 10.0
OPENAI_CODEX_RUNTIME_AUTH_FAILURE_MARKERS = (
    "oauth token refresh failed for openai-codex",
    "refresh_token_reused",
    "invalid_grant",
    "token refresh failed",
)
BOOTSTRAP_FAST_TIMEOUT_SECONDS = 5.0
BOOTSTRAP_FAST_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="bootstrap-fast",
)
CAPABILITY_REFRESH_TIMEOUT_SECONDS = 20.0
CAPABILITY_REFRESH_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="capability-refresh",
)
OPTIONAL_CAPABILITY_UPLOAD_FILE = File(default=None)
ASSISTANT_RESTARTING_HEALTH_GRACE_SECONDS = 60.0
ADMIN_RUNTIME_TASK_MAX_WORKERS = 4
AUDIT_EXPORT_DEFAULT_PAGE_SIZE = 1000
AUDIT_EXPORT_MAX_PAGE_SIZE = 5000
ADMIN_LIST_DEFAULT_PAGE_SIZE = 50
WORKSPACE_FOLDER_DEFAULT_PAGE_SIZE = 10
ADMIN_LIST_MAX_PAGE_SIZE = 200
DOCKER_LOOPBACK_PROXY_NETWORKS = (
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.65.0/24"),
)


def _bootstrap_fast_cache_ttl_seconds() -> float:
    return max(1.0, float(get_settings().admin_bootstrap_fast_cache_ttl_seconds))


def _assistant_status_cache_ttl_seconds() -> float:
    return max(1.0, float(get_settings().admin_assistant_status_cache_ttl_seconds))


def _assistant_status_fallback_poll_ms() -> int:
    return max(1000, int(get_settings().admin_assistant_status_fallback_poll_ms))


def _is_loopback_admin_request(request: Request) -> bool:
    host = (request.url.hostname or "").strip().lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return False
    client_host = ((request.client.host if request.client else "") or "").strip().lower()
    if client_host in {"127.0.0.1", "localhost", "::1", "testclient"}:
        return True
    if client_host.startswith("127."):
        return True
    try:
        client_ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    return any(client_ip in network for network in DOCKER_LOOPBACK_PROXY_NETWORKS)


def _b64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64_decode(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def _sign_admin_direct_session(payload: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return _b64_encode(digest)


def _mint_admin_direct_session(secret: str, *, now: int | None = None) -> str:
    expires_at = (now if now is not None else int(time.time())) + ADMIN_DIRECT_SESSION_TTL_SECONDS
    payload = f"admin:{expires_at}"
    return f"{_b64_encode(payload.encode('utf-8'))}.{_sign_admin_direct_session(payload, secret)}"


def _set_admin_direct_session_cookies(response: Response, secret: str) -> None:
    token = _mint_admin_direct_session(secret)
    for cookie_name in (ADMIN_DIRECT_SESSION_COOKIE, LEGACY_ADMIN_DIRECT_SESSION_COOKIE):
        response.set_cookie(
            cookie_name,
            token,
            max_age=ADMIN_DIRECT_SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
        )


def _valid_admin_direct_session(token: str, secret: str, *, now: int | None = None) -> bool:
    raw_payload, separator, signature = token.partition(".")
    if not separator:
        return False
    try:
        payload = _b64_decode(raw_payload).decode("utf-8")
        subject, raw_expires_at = payload.split(":", 1)
        expires_at = int(raw_expires_at)
    except (ValueError, UnicodeDecodeError):
        return False
    current_time = now if now is not None else int(time.time())
    if subject != "admin" or expires_at < current_time:
        return False
    expected = _sign_admin_direct_session(payload, secret)
    return hmac.compare_digest(signature, expected)


def _delete_workspace_item_package_records(
    *,
    assistant_id: str,
    kind: str,
    slug: str,
) -> int:
    try:
        deleted = WorkspaceFileService().delete_capability_package(
            assistant_id=assistant_id,
            bucket=kind,  # type: ignore[arg-type]
            slug=slug,
            user_id="admin-web",
        )
        log.info(
            "workspace_item_package_cleanup_done",
            assistant_id=assistant_id,
            kind=kind,
            slug=slug,
            workspace_records_deleted=deleted,
        )
        return deleted
    except Exception as exc:
        log.warning(
            "workspace_item_package_cleanup_failed",
            assistant_id=assistant_id,
            kind=kind,
            slug=slug,
            error=f"{type(exc).__name__}: {exc}",
        )
        return 0


class ApprovalResolutionRequest(BaseModel):
    resolution: str


def _approval_resolution_audit_payload(item: dict[str, Any]) -> dict[str, Any]:
    resolved_at = item.get("resolved_at")
    occurred_at = (
        resolved_at.isoformat() if hasattr(resolved_at, "isoformat") else str(resolved_at or "")
    )
    return {
        "envelope": {
            "request_id": item.get("id", ""),
            "hook_name": "approval_resolution",
            "occurred_at": occurred_at,
        },
        "assistant": {
            "assistant_id": item.get("assistant_id") or "",
            "capability_profile": item.get("capability_profile") or "",
        },
        "tool": {
            "name": item.get("tool_name") or "",
        },
        "decision": item.get("resolution") or "",
        "result": {
            "success": item.get("status") in {"approved", "denied"},
        },
        "approval": {
            "scope": item.get("approval_scope") or "",
            "status": item.get("status") or "",
            "target": item.get("target") or "",
            "reason": item.get("reason") or "",
        },
    }


def _url_origin_key(raw_url: str | None) -> tuple[str, str, int | None] | None:
    if not raw_url:
        return None
    parts = urlsplit(raw_url.strip())
    if not parts.scheme or not parts.netloc:
        return None
    host = (parts.hostname or "").strip().lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        host = "loopback"
    return (parts.scheme.lower(), host, parts.port)


def _resolve_control_ui_public_url(
    *,
    gateway_url: str | None = None,
    admin_origin_url: str | None = None,
) -> str:
    settings = get_settings()
    configured = settings.control_ui_public_url.strip().rstrip("/")
    if configured and _url_origin_key(configured) != _url_origin_key(admin_origin_url):
        return configured
    if gateway_url:
        return gateway_url.strip().rstrip("/")
    return configured or "http://127.0.0.1:5173"


def _browser_accessible_gateway_url(
    gateway_url: str | None,
    *,
    admin_origin_url: str | None = None,
) -> str | None:
    if not gateway_url:
        return gateway_url
    parts = urlsplit(gateway_url.strip())
    if not parts.scheme or not parts.netloc:
        return gateway_url
    hostname = (parts.hostname or "").strip().lower()
    if hostname not in {"host.docker.internal", "0.0.0.0", "::", "[::]"}:
        return gateway_url

    admin_parts = urlsplit((admin_origin_url or "").strip())
    admin_host = (admin_parts.hostname or "").strip().lower()
    if admin_host not in {"127.0.0.1", "localhost", "::1"}:
        return gateway_url

    netloc = "127.0.0.1"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit(parts._replace(netloc=netloc))


def _assistant_main_session_key(assistant_id: str) -> str:
    trimmed = assistant_id.strip()
    return f"agent:{trimmed}:main" if trimmed else "main"


def _assistant_fresh_session_key(assistant_id: str) -> str:
    trimmed = assistant_id.strip()
    suffix = f"ui-{int(time.time())}-{uuid4().hex[:8]}"
    return f"agent:{trimmed}:{suffix}" if trimmed else suffix


def _gateway_launch_url(
    *,
    assistant_id: str,
    gateway_url: str | None,
    gateway_token: str | None,
    fresh_session: bool = False,
    mint_launch_ticket: bool = True,
    admin_origin_url: str | None = None,
) -> str | None:
    if not gateway_url:
        return None
    normalized_gateway_url = normalize_control_ui_gateway_url(gateway_url)
    browser_gateway_url = _browser_accessible_gateway_url(
        gateway_url,
        admin_origin_url=admin_origin_url,
    )
    settings = get_settings()
    control_ui_url = _resolve_control_ui_public_url(
        gateway_url=browser_gateway_url or gateway_url,
        admin_origin_url=admin_origin_url,
    )
    session_key = (
        _assistant_fresh_session_key(assistant_id)
        if fresh_session
        else _assistant_main_session_key(assistant_id)
    )
    launch_ticket = None
    if (
        mint_launch_ticket
        and gateway_token
        and (settings.go_gateway_public_url.strip() or settings.security_service_public_url.strip())
    ):
        launch_ticket = mint_control_ui_launch_ticket(
            assistant_id=assistant_id,
            gateway_url=normalized_gateway_url or gateway_url,
            gateway_token=gateway_token,
            session_key=session_key,
            control_ui_url=control_ui_url,
        )
    go_gateway_launch_url = build_go_gateway_assistant_ui_url(
        go_gateway_public_url=settings.go_gateway_public_url,
        assistant_id=assistant_id,
        launch_ticket=launch_ticket.ticket if launch_ticket else None,
    )
    if go_gateway_launch_url:
        return go_gateway_launch_url
    return build_spdbpanda_chat_launch_url(
        control_ui_url=control_ui_url,
        gateway_url=browser_gateway_url or normalized_gateway_url or gateway_url,
        gateway_token=gateway_token,
        session_key=session_key,
        security_service_url=settings.security_service_public_url,
        launch_ticket=launch_ticket.ticket if launch_ticket else None,
    )


class AdminAssistantCreateRequest(BaseModel):
    assistant_id: str | None = None
    name: str
    capability_profile: str
    user_id: str = Field(default="", validation_alias=AliasChoices("user_id", "userId"))
    notes: str = ""
    model_id: str = ""
    primary_model: str = ""
    provider_id: str = ""
    provider_base_url: str = ""
    provider_api_key: str = ""
    workspace_slug: str = ""


class AdminAssistantModelConfigRequest(BaseModel):
    model_id: str = ""
    primary_model: str = ""
    provider_id: str = ""
    provider_base_url: str = ""
    provider_api_key: str = ""
    provision_runtime: bool = True


class AdminUserUpsertRequest(BaseModel):
    user_id: str | None = None
    display_name: str
    department: str = ""
    employee_id: str = ""
    role: str = "user"


class AdminBindAssistantRequest(BaseModel):
    assistant_id: str
    alias: str = ""
    source_system: str = "admin-web"


class WorkspaceItemToggleRequest(BaseModel):
    kind: str
    slug: str
    enabled: bool


class WorkspaceItemReuseRequest(BaseModel):
    kind: str
    slug: str
    target_assistant_id: str


class AdminWorkspaceSyncJobCreateRequest(BaseModel):
    assistant_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("assistant_id", "assistantId"),
    )
    scope_kind: str = Field(
        default="assistant",
        validation_alias=AliasChoices("scope_kind", "scopeKind"),
    )
    scope_slug: str | None = Field(
        default=None,
        validation_alias=AliasChoices("scope_slug", "scopeSlug"),
    )
    mode: str = "index_only"
    priority: str = "normal"
    reason: str = ""
    idempotency_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("idempotency_key", "idempotencyKey"),
    )


CAPABILITY_WORKSPACE_BUCKETS = {"skills", "plugins"}


def _workspace_capability_slug(record: WorkspaceFile) -> str:
    if record.bucket not in CAPABILITY_WORKSPACE_BUCKETS:
        return ""
    normalized = record.filename.replace("\\", "/").lstrip("/")
    if not normalized:
        return ""
    slug = normalized.split("/", 1)[0].strip()
    if not slug or slug.startswith("."):
        return ""
    return slug


def _catalog_by_workspace_key(
    skills_catalog: list[Any],
    plugins_catalog: list[Any],
) -> dict[tuple[str, str], Any]:
    catalog: dict[tuple[str, str], Any] = {}
    for item in skills_catalog:
        catalog[("skills", item.slug)] = item
    for item in plugins_catalog:
        catalog[("plugins", item.slug)] = item
    return catalog


def _latest_workspace_file(left: WorkspaceFile, right: WorkspaceFile) -> WorkspaceFile:
    left_time = left.updated_at or left.created_at
    right_time = right.updated_at or right.created_at
    return right if right_time >= left_time else left


def _metadata_string(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _metadata_from_skill_text(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", ""
    end_index = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() in {"---", "..."}),
        -1,
    )
    if end_index <= 1:
        return "", ""
    try:
        metadata = yaml.safe_load("\n".join(lines[1:end_index])) or {}
    except yaml.YAMLError:
        return "", ""
    if not isinstance(metadata, dict):
        return "", ""
    return (
        _metadata_string(metadata, "name", "display_name", "title"),
        _metadata_string(metadata, "version"),
    )


def _metadata_from_json_text(text: str) -> tuple[str, str]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return "", ""
    candidates: list[Any] = [payload]
    if isinstance(payload, dict):
        candidates.extend(payload.get(key) for key in ("manifest", "metadata", "skill", "package"))
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        name = _metadata_string(candidate, "name", "display_name", "title")
        version = _metadata_string(candidate, "version")
        if name or version:
            return name, version
    return "", ""


def _workspace_capability_metadata(
    *,
    assistant_id: str,
    bucket: str,
    records: list[WorkspaceFile],
    workspace_file_service: WorkspaceFileService | None,
) -> tuple[str, str]:
    if workspace_file_service is None:
        return "", ""
    try:
        bucket_dir = Path(workspace_file_service._bucket_dir(assistant_id, bucket)).resolve()
    except (OSError, ValueError):
        return "", ""
    candidates: list[tuple[int, WorkspaceFile]] = []
    for record in records:
        filename = record.filename.replace("\\", "/").strip("/")
        basename = filename.rsplit("/", 1)[-1]
        if bucket == "skills" and basename == "SKILL.md":
            candidates.append((0, record))
        elif basename in {"manifest.json", "_meta.json"}:
            candidates.append((1, record))
    for _priority, record in sorted(candidates, key=lambda item: item[0]):
        try:
            path = (bucket_dir / record.filename).resolve()
            path.relative_to(bucket_dir)
            text = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, ValueError):
            continue
        if path.name == "SKILL.md":
            name, version = _metadata_from_skill_text(text)
        else:
            name, version = _metadata_from_json_text(text)
        if name or version:
            return name, version
    return "", ""


def _workspace_file_payload(
    item: WorkspaceFile,
    *,
    workspace_file_service: WorkspaceFileService,
    include_workspace_paths: bool,
) -> dict[str, Any]:
    updated_at = item.updated_at or item.created_at
    return {
        "file_id": item.file_id,
        "assistant_id": item.assistant_id,
        "bucket": item.bucket,
        "filename": item.filename,
        "origin": item.origin,
        "source_type": item.source_type,
        "sync_status": item.sync_status,
        "relative_path": item.relative_path,
        "workspace_path": (
            runtime_workspace_path(item.bucket, item.filename)
            if include_workspace_paths
            else (item.relative_path or f"{item.bucket}/{item.filename}")
        ),
        "isCapabilityPackage": False,
        "latestUpdatedAt": updated_at.isoformat(timespec="seconds"),
    }


def _value_from(source: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(source, dict) and name in source:
            return source[name]
        if not isinstance(source, dict) and hasattr(source, name):
            return getattr(source, name)
    return default


def _isoformat_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat(timespec="seconds")
        except TypeError:
            return value.isoformat()
    return str(value)


def _call_kwargs_for(callable_obj: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return kwargs
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
        return kwargs
    return {key: value for key, value in kwargs.items() if key in signature.parameters}


async def _call_maybe_async(callable_obj: Any, **kwargs: Any) -> Any:
    call_kwargs = _call_kwargs_for(callable_obj, kwargs)
    if inspect.iscoroutinefunction(callable_obj):
        return await callable_obj(**call_kwargs)
    result = await asyncio.to_thread(callable_obj, **call_kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _admin_runtime_task_executor(app: FastAPI) -> ThreadPoolExecutor:
    executor = getattr(app.state, "admin_runtime_task_executor", None)
    if executor is None:
        executor = ThreadPoolExecutor(
            max_workers=ADMIN_RUNTIME_TASK_MAX_WORKERS,
            thread_name_prefix="admin-runtime-task",
        )
        app.state.admin_runtime_task_executor = executor
    return executor


async def _run_admin_runtime_task(
    app: FastAPI,
    func: Callable[..., Any],
    *args: Any,
) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_admin_runtime_task_executor(app), func, *args)


def _shutdown_admin_runtime_task_executor(app: FastAPI) -> None:
    executor = getattr(app.state, "admin_runtime_task_executor", None)
    if executor is None:
        return
    executor.shutdown(wait=False, cancel_futures=True)
    app.state.admin_runtime_task_executor = None


def _module_callable(module: Any, *names: str) -> Any:
    for name in names:
        candidate = getattr(module, name, None)
        if callable(candidate):
            return candidate
    return None


def _load_workspace_admin_index() -> Any:
    try:
        return importlib.import_module("src.workspace_files.admin_index")
    except ModuleNotFoundError as exc:
        if exc.name == "src.workspace_files.admin_index":
            raise HTTPException(
                status_code=501,
                detail="workspace admin index is not available",
            ) from exc
        raise


def _load_workspace_item_index() -> Any:
    try:
        return importlib.import_module("src.workspace_files.item_index")
    except ModuleNotFoundError as exc:
        if exc.name == "src.workspace_files.item_index":
            raise HTTPException(
                status_code=501,
                detail="workspace item index is not available",
            ) from exc
        raise


def _load_workspace_sync_job_service() -> Any:
    try:
        module = importlib.import_module("src.workspace_files.sync_jobs")
    except ModuleNotFoundError as exc:
        if exc.name == "src.workspace_files.sync_jobs":
            raise HTTPException(
                status_code=501,
                detail="workspace sync job service is not available",
            ) from exc
        raise
    service_cls = getattr(module, "WorkspaceSyncJobService", None)
    if service_cls is None:
        raise HTTPException(
            status_code=501,
            detail="WorkspaceSyncJobService is not available",
        )
    return service_cls()


def _validate_workspace_summary_page_size(page_size: int) -> None:
    if page_size > 200:
        raise HTTPException(status_code=400, detail="page_size must be <= 200")


def _validate_admin_list_page_size(page_size: int) -> None:
    if page_size > ADMIN_LIST_MAX_PAGE_SIZE:
        raise HTTPException(status_code=400, detail="page_size must be <= 200")


def _parse_workspace_updated_after(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid updated_after: {raw}") from exc


def _workspace_admin_index_repository() -> Any:
    from src.workspace_files.repository import build_workspace_file_repository

    return build_workspace_file_repository()


def _workspace_item_index_store() -> Any:
    from src.workspace_files.item_index import WorkspaceItemIndexStore

    return WorkspaceItemIndexStore()


def _workspace_sync_job_priority(value: str) -> int:
    return {"low": 200, "normal": 100, "high": 50}[value]


def _workspace_index_items(index_result: Any) -> list[Any]:
    for key in ("workspaceFiles", "workspace_files", "items", "records", "files"):
        items = _value_from(index_result, key, default=None)
        if items is not None:
            return list(items)
    return []


def _workspace_index_pagination(
    index_result: Any,
    *,
    page: int,
    page_size: int,
    items_count: int,
) -> dict[str, Any]:
    raw = _value_from(index_result, "pagination", default=None)
    payload = dict(raw) if isinstance(raw, dict) else {}
    total = _value_from(
        raw,
        "total",
        default=_value_from(index_result, "total", default=items_count),
    )
    try:
        total_int = int(total)
    except (TypeError, ValueError):
        total_int = items_count
    has_next = _value_from(
        raw,
        "has_next",
        "hasNext",
        default=_value_from(
            index_result,
            "has_next",
            "hasNext",
            default=total_int > page * page_size,
        ),
    )
    payload.update(
        {
            "page": int(_value_from(raw, "page", default=page) or page),
            "page_size": int(
                _value_from(raw, "page_size", "pageSize", default=page_size) or page_size
            ),
            "total": total_int,
            "has_next": bool(has_next),
        }
    )
    return payload


def _workspace_index_status(index_result: Any) -> dict[str, Any]:
    raw = _value_from(index_result, "indexStatus", "index_status", default=None)
    payload = dict(raw) if isinstance(raw, dict) else {}
    stale_count = _value_from(index_result, "stale_count", "staleCount", default=0) or 0
    last_indexed_at = _value_from(
        raw,
        "last_indexed_at",
        "lastIndexedAt",
        default=_value_from(index_result, "last_indexed_at", "lastIndexedAt", default=None),
    )
    payload.setdefault("last_indexed_at", _isoformat_or_none(last_indexed_at))
    payload.setdefault(
        "sync_status",
        _value_from(
            raw,
            "sync_status",
            "syncStatus",
            default=_value_from(index_result, "sync_status", "syncStatus", default="unknown"),
        )
        or "unknown",
    )
    payload.setdefault(
        "sync_error",
        _value_from(
            raw,
            "sync_error",
            "syncError",
            default=_value_from(index_result, "sync_error", "syncError", default=""),
        )
        or "",
    )
    payload.setdefault(
        "stale",
        bool(
            _value_from(
                raw,
                "stale",
                default=bool(_value_from(index_result, "stale", default=False) or stale_count),
            )
        ),
    )
    if stale_count:
        payload.setdefault("stale_count", stale_count)
    payload.setdefault("source", _value_from(raw, "source", default="db") or "db")
    return payload


def _workspace_file_summary_payload(item: Any) -> dict[str, Any]:
    bucket = str(_value_from(item, "bucket", default="") or "")
    filename = str(_value_from(item, "filename", default="") or "")
    relative_path = str(
        _value_from(item, "relative_path", "relativePath", default="")
        or (f"{bucket}/{filename}" if bucket and filename else "")
    )
    runtime_workspace_path = str(
        _value_from(item, "runtime_workspace_path", "runtimeWorkspacePath", default="") or ""
    )
    if not runtime_workspace_path and relative_path:
        runtime_workspace_path = f"/workspace/{relative_path.strip('/')}"
    updated_at = _value_from(
        item,
        "updated_at",
        "updatedAt",
        default=_value_from(item, "created_at", "createdAt", default=None),
    )
    source_type = str(_value_from(item, "source_type", "sourceType", "origin", default="") or "")
    return {
        "file_id": _value_from(item, "file_id", "fileId", default=""),
        "assistant_id": _value_from(item, "assistant_id", "assistantId", default=""),
        "bucket": bucket,
        "filename": filename,
        "origin": source_type,
        "source_type": source_type,
        "entry_type": _value_from(item, "entry_type", "entryType", default="file") or "file",
        "content_type": _value_from(item, "content_type", "contentType", default="") or "",
        "child_count": _value_from(item, "child_count", "childCount", default=0) or 0,
        "sync_status": _value_from(item, "sync_status", "syncStatus", default=""),
        "sync_error": _value_from(item, "sync_error", "syncError", "last_error", default="") or "",
        "relative_path": relative_path,
        "workspace_relative_path": relative_path,
        "runtime_workspace_path": runtime_workspace_path,
        "object_storage_status": _value_from(
            item,
            "object_storage_status",
            "objectStorageStatus",
            "storage_status",
            "storageStatus",
            default="",
        )
        or "",
        "workspace_path": _value_from(
            item,
            "workspace_path",
            "workspacePath",
            default=runtime_workspace_path or relative_path,
        )
        or runtime_workspace_path
        or relative_path,
        "size_bytes": _value_from(item, "size_bytes", "sizeBytes", default=0) or 0,
        "lastSyncedAt": _isoformat_or_none(
            _value_from(item, "last_synced_at", "lastSyncedAt", default=None)
        ),
        "latestUpdatedAt": _isoformat_or_none(updated_at),
        "isCapabilityPackage": bool(
            _value_from(item, "isCapabilityPackage", "is_capability_package", default=False)
        ),
        "packageSlug": _value_from(item, "packageSlug", "package_slug", default="") or "",
        "packageType": _value_from(item, "packageType", "package_type", default="") or "",
    }


def _assistant_workspace_summary_payload(
    index_result: Any,
    *,
    assistant_id: str,
    pagination: dict[str, Any],
    index_status: dict[str, Any],
) -> dict[str, Any]:
    raw = _value_from(index_result, "assistantSummary", "assistant_summary", default=None)
    payload = dict(raw) if isinstance(raw, dict) else {}
    payload.setdefault("assistant_id", assistant_id)
    payload.setdefault(
        "last_synced_at",
        _isoformat_or_none(_value_from(raw, "last_synced_at", "lastSyncedAt", default=None)),
    )
    payload.setdefault("last_indexed_at", index_status.get("last_indexed_at"))
    payload.setdefault("sync_status", index_status.get("sync_status", "unknown"))
    payload.setdefault("sync_error", index_status.get("sync_error", ""))
    payload.setdefault("stale", bool(index_status.get("stale", False)))
    payload.setdefault("files_count", pagination.get("total", 0))
    payload.setdefault(
        "capability_count",
        _value_from(raw, "capability_count", "capabilityCount", default=0) or 0,
    )
    payload.setdefault(
        "pending_job_id",
        _value_from(raw, "pending_job_id", "pendingJobId", default="") or "",
    )
    return payload


def _workspace_item_index_records(index_result: Any) -> list[Any]:
    records = _value_from(index_result, "items", "records", default=None)
    if records is None:
        return []
    return list(records)


def _workspace_item_index_status(index_result: Any) -> dict[str, Any]:
    raw = _value_from(index_result, "indexStatus", "index_status", default=None)
    if raw is None:
        raw = {}
    if isinstance(raw, dict):
        payload = dict(raw)
    else:
        payload = {}
        for name in (
            "source",
            "sync_status",
            "sync_error",
            "stale",
            "last_indexed_at",
            "stale_count",
        ):
            value = _value_from(raw, name, default=None)
            if value is not None:
                payload[name] = value
    if "last_indexed_at" in payload:
        payload["last_indexed_at"] = _isoformat_or_none(payload["last_indexed_at"])
    payload.setdefault("source", "db")
    payload.setdefault("sync_status", "unknown")
    payload.setdefault("sync_error", "")
    payload.setdefault("stale", False)
    return payload


def _workspace_item_summary_payload(item: Any) -> dict[str, Any]:
    return {
        "assistant_id": _value_from(item, "assistant_id", default=""),
        "kind": _value_from(item, "kind", default=""),
        "slug": _value_from(item, "slug", default=""),
        "name": _value_from(item, "name", default=""),
        "version": _value_from(item, "version", default=""),
        "enabled": bool(_value_from(item, "enabled", default=True)),
        "install_path": _value_from(item, "install_path", default=""),
        "files_count": _value_from(item, "files_count", default=0) or 0,
        "content_hash": _value_from(item, "content_hash", default="") or "",
        "source": _value_from(item, "source", default="db") or "db",
        "sync_status": _value_from(item, "sync_status", default="unknown") or "unknown",
        "runtime_reload_status": (
            _value_from(item, "runtime_reload_status", default="unknown") or "unknown"
        ),
        "runtime_reload_error": _value_from(item, "runtime_reload_error", default="") or "",
        "runtime_effective": bool(_value_from(item, "runtime_effective", default=False)),
        "last_indexed_at": _isoformat_or_none(_value_from(item, "last_indexed_at", default=None)),
        "last_synced_at": _isoformat_or_none(_value_from(item, "last_synced_at", default=None)),
        "last_error": _value_from(item, "last_error", default="") or "",
    }


def _workspace_item_install_payload(
    result: Any,
    *,
    warnings: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "assistant_id": result.assistant_id,
        "kind": result.kind,
        "type": "skill" if result.kind == "skills" else "plugin",
        "id": result.capability_id,
        "market_id": result.capability_id,
        "slug": result.slug,
        "name": result.name,
        "version": result.version,
        "install_path": result.install_path,
        "install_count": result.install_count,
        "sync_status": result.sync_status,
        "sync_error": result.sync_error,
        "runtime_reload_status": result.runtime_reload_status,
        "runtime_reload_error": result.runtime_reload_error,
        "runtime_effective": result.runtime_reload_status != "failed",
        "action": f"capability_{result.action}",
        "previous_version": result.previous_version,
        "warnings": [warning for warning in warnings if warning],
    }


def _workspace_summary_response(
    index_result: Any,
    *,
    page: int,
    page_size: int,
    sync: bool,
    assistant_ids: list[str] | None = None,
    assistant_id: str | None = None,
) -> dict[str, Any]:
    items = _workspace_index_items(index_result)
    pagination = _workspace_index_pagination(
        index_result,
        page=page,
        page_size=page_size,
        items_count=len(items),
    )
    index_status = _workspace_index_status(index_result)
    response: dict[str, Any] = {
        "workspaceFiles": [_workspace_file_summary_payload(item) for item in items],
        "pagination": pagination,
        "indexStatus": index_status,
        "compat": {
            "sync_requested": bool(sync),
            "sync_ignored": False,
        },
    }
    if assistant_ids is not None:
        response["assistantIds"] = assistant_ids
    if assistant_id is not None:
        response["assistantId"] = assistant_id
        response["assistantSummary"] = _assistant_workspace_summary_payload(
            index_result,
            assistant_id=assistant_id,
            pagination=pagination,
            index_status=index_status,
        )
    assistant_summaries = _value_from(
        index_result,
        "assistantSummaries",
        "assistant_summaries",
        default=None,
    )
    if assistant_summaries is not None:
        response["assistantSummaries"] = list(assistant_summaries)
    return response


def _workspace_folder_summary_payload(
    item: Any,
    assistants_by_id: dict[str, Any],
) -> dict[str, Any]:
    assistant_id = str(_value_from(item, "assistant_id", "assistantId", default="") or "")
    assistant = assistants_by_id.get(assistant_id)
    buckets = _value_from(item, "buckets", default=[]) or []
    return {
        "assistant_id": assistant_id,
        "assistantId": assistant_id,
        "assistant_name": getattr(assistant, "name", assistant_id),
        "assistantName": getattr(assistant, "name", assistant_id),
        "container_name": getattr(getattr(assistant, "runtime", None), "container_name", ""),
        "containerName": getattr(getattr(assistant, "runtime", None), "container_name", ""),
        "runtime_status": getattr(getattr(assistant, "runtime", None), "status", "pending"),
        "runtimeStatus": getattr(getattr(assistant, "runtime", None), "status", "pending"),
        "buckets": list(buckets),
        "files_count": int(_value_from(item, "files_count", "filesCount", default=0) or 0),
        "filesCount": int(_value_from(item, "files_count", "filesCount", default=0) or 0),
        "root_items_count": int(
            _value_from(item, "root_items_count", "rootItemsCount", default=len(buckets)) or 0
        ),
        "rootItemsCount": int(
            _value_from(item, "root_items_count", "rootItemsCount", default=len(buckets)) or 0
        ),
        "last_indexed_at": _isoformat_or_none(
            _value_from(item, "last_indexed_at", "lastIndexedAt", default=None)
        ),
        "lastIndexedAt": _isoformat_or_none(
            _value_from(item, "last_indexed_at", "lastIndexedAt", default=None)
        ),
        "last_synced_at": _isoformat_or_none(
            _value_from(item, "last_synced_at", "lastSyncedAt", default=None)
        ),
        "lastSyncedAt": _isoformat_or_none(
            _value_from(item, "last_synced_at", "lastSyncedAt", default=None)
        ),
        "sync_status": _value_from(item, "sync_status", "syncStatus", default="unknown"),
        "syncStatus": _value_from(item, "sync_status", "syncStatus", default="unknown"),
        "sync_error": _value_from(item, "sync_error", "syncError", default="") or "",
        "syncError": _value_from(item, "sync_error", "syncError", default="") or "",
        "stale": bool(_value_from(item, "stale", default=False)),
    }


def _workspace_folders_response(
    folder_result: Any,
    *,
    assistants: list[Any],
    sync: bool,
    pagination: dict[str, Any],
) -> dict[str, Any]:
    summaries = list(_value_from(folder_result, "summaries", "items", default=[]) or [])
    assistants_by_id = {
        str(getattr(assistant, "assistant_id", "") or ""): assistant for assistant in assistants
    }
    summaries_by_id = {
        str(_value_from(item, "assistant_id", "assistantId", default="") or ""): item
        for item in summaries
    }
    stale_count = int(_value_from(folder_result, "stale_count", "staleCount", default=0) or 0)
    total_files = int(_value_from(folder_result, "total_files", "totalFiles", default=0) or 0)
    if not total_files:
        total_files = sum(
            int(_value_from(item, "files_count", "filesCount", default=0) or 0)
            for item in summaries
        )
    last_indexed_at = _value_from(
        folder_result,
        "last_indexed_at",
        "lastIndexedAt",
        default=None,
    )
    return {
        "workspaceFolders": [
            _workspace_folder_summary_payload(
                summaries_by_id.get(assistant_id)
                or {
                    "assistant_id": assistant_id,
                    "buckets": [],
                    "files_count": 0,
                    "root_items_count": 0,
                    "sync_status": "empty",
                },
                assistants_by_id,
            )
            for assistant_id in assistants_by_id
        ],
        "pagination": pagination,
        "indexStatus": {
            "last_indexed_at": _isoformat_or_none(last_indexed_at),
            "sync_status": "unknown" if stale_count else "synced",
            "sync_error": "",
            "stale": stale_count > 0,
            "stale_count": stale_count,
            "files_count": total_files,
            "filesCount": total_files,
            "source": _value_from(folder_result, "source", default="db") or "db",
        },
        "assistantIds": list(assistants_by_id),
        "compat": {
            "sync_requested": bool(sync),
            "sync_ignored": False,
        },
    }


async def _query_workspace_item_index(
    *,
    assistant_id: str,
    kind: str | None,
    slug: str | None,
) -> Any:
    module = _load_workspace_item_index()
    query = _module_callable(
        module,
        "list_workspace_item_index_page",
        "list_workspace_items_page",
        "list_assistant_workspace_items_page",
        "query_workspace_items_page",
        "list_workspace_items",
        "list_assistant_workspace_items",
        "query_workspace_items",
        "list_installed_capabilities",
    )
    if query is None:
        raise HTTPException(
            status_code=501,
            detail="workspace item index query is not available",
        )

    filters_cls = getattr(module, "WorkspaceItemIndexFilters", None)
    filters = None
    if filters_cls is not None:
        filters = filters_cls(
            assistant_id=assistant_id,
            kind=kind,
            slug=slug,
        )
    return await _call_maybe_async(
        query,
        filters=filters,
        assistant_id=assistant_id,
        kind=kind,
        slug=slug,
        workspace_file_repository=_workspace_admin_index_repository(),
    )


async def _sync_workspace_summaries_if_requested(
    *,
    sync: bool,
    assistant_ids: list[str],
    assistant_service: AssistantService,
) -> None:
    if not sync or not assistant_ids:
        return
    sync_service = WorkspaceSyncService(assistant_service=assistant_service)
    for assistant_id in assistant_ids:
        try:
            await sync_service.async_sync_assistant_workspace(assistant_id)
        except Exception as exc:
            log.warning(
                "admin_workspace_summary_sync_failed",
                assistant_id=assistant_id,
                error=f"{type(exc).__name__}: {exc}",
            )


async def _query_workspace_folder_summaries(*, assistant_ids: list[str]) -> Any:
    module = _load_workspace_admin_index()
    query = _module_callable(
        module,
        "list_workspace_folder_summaries",
        "list_workspace_folders_summary",
        "query_workspace_folder_summaries",
    )
    if query is None:
        raise HTTPException(
            status_code=501,
            detail="workspace folder summary query is not available",
        )
    return await _call_maybe_async(
        query,
        repository=_workspace_admin_index_repository(),
        assistant_ids=assistant_ids,
    )


def _workspace_items_response(
    index_result: Any,
    *,
    assistant_id: str,
    force: bool,
) -> dict[str, Any]:
    return {
        "items": [
            _workspace_item_summary_payload(item)
            for item in _workspace_item_index_records(index_result)
        ],
        "indexStatus": _workspace_item_index_status(index_result),
        "compat": {
            "force_requested": bool(force),
            "force_scan_disabled": bool(force),
            "refresh_required": bool(force),
        },
        "assistantId": assistant_id,
    }


def _legacy_workspace_summary_records(
    *,
    assistant_ids: list[str],
    workspace_file_service: WorkspaceFileService,
    bucket: str | None,
) -> list[WorkspaceFile]:
    records: list[WorkspaceFile] = []
    for assistant_id in assistant_ids:
        assistant_records: list[WorkspaceFile] = []
        try:
            assistant_records = workspace_file_service.list_cos_workspace_summary_records(
                assistant_id
            )
        except Exception as exc:
            log.warning(
                "admin_workspace_summary_cos_list_failed",
                assistant_id=assistant_id,
                error=f"{type(exc).__name__}: {exc}",
            )
        if not assistant_records:
            try:
                assistant_records = workspace_file_service.list_files(
                    assistant_id,
                    bucket=bucket,  # type: ignore[arg-type]
                )
            except TypeError:
                assistant_records = workspace_file_service.list_files(assistant_id)
            except Exception as exc:
                log.warning(
                    "admin_workspace_summary_db_list_failed",
                    assistant_id=assistant_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
        records.extend(assistant_records)
    return records


def _legacy_workspace_summary_index_result(
    *,
    records: list[WorkspaceFile],
    page: int,
    page_size: int,
) -> dict[str, Any]:
    start = max(page - 1, 0) * page_size
    page_records = records[start : start + page_size]
    return {
        "items": page_records,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": len(records),
            "has_next": len(records) > start + page_size,
        },
        "indexStatus": {
            "sync_status": "synced",
            "sync_error": "",
            "stale": False,
            "source": "workspace_file_service",
        },
    }


async def _query_workspace_admin_index(**kwargs: Any) -> Any:
    module = _load_workspace_admin_index()
    index_page = _module_callable(module, "list_workspace_file_index_page")
    filters_cls = getattr(module, "WorkspaceFileIndexFilters", None)
    if index_page is not None and filters_cls is not None:
        filters = filters_cls(
            assistant_ids=kwargs.get("assistant_ids"),
            assistant_id=kwargs.get("assistant_id"),
            bucket=(kwargs.get("bucket") or None),
            source_type=kwargs.get("source_type"),
            sync_status=kwargs.get("sync_status"),
            updated_after=_parse_workspace_updated_after(kwargs.get("updated_after")),
            query=kwargs.get("query"),
            page=int(kwargs.get("page") or 1),
            page_size=int(kwargs.get("page_size") or 50),
            view=kwargs.get("view") or "flat",
            parent=kwargs.get("parent") or "",
        )
        try:
            return await _call_maybe_async(
                index_page,
                repository=_workspace_admin_index_repository(),
                filters=filters,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    query_page = _module_callable(
        module,
        "list_workspace_files_page",
        "list_workspace_summary_page",
        "query_workspace_files_page",
        "query_workspace_summary_page",
    )
    if query_page is None:
        raise HTTPException(
            status_code=501,
            detail="workspace admin index page query is not available",
        )
    try:
        return await _call_maybe_async(query_page, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _sync_job_payload(job: Any) -> dict[str, Any]:
    fields = (
        "job_id",
        "status",
        "idempotency_key",
        "mode",
        "assistant_id",
        "scope_kind",
        "scope_slug",
        "priority",
        "requested_by",
        "requested_from",
        "pending_position",
        "scanned_count",
        "created_count",
        "updated_count",
        "missing_count",
        "deleted_count",
        "orphan_count",
        "error_code",
        "error",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    )
    aliases = {
        "job_id": ("job_id", "jobId", "id"),
        "idempotency_key": ("idempotency_key", "idempotencyKey"),
        "assistant_id": ("assistant_id", "assistantId"),
        "scope_kind": ("scope_kind", "scopeKind"),
        "scope_slug": ("scope_slug", "scopeSlug"),
        "requested_by": ("requested_by", "requestedBy"),
        "requested_from": ("requested_from", "requestedFrom"),
        "pending_position": ("pending_position", "pendingPosition"),
        "scanned_count": ("scanned_count", "scannedCount"),
        "created_count": ("created_count", "createdCount"),
        "updated_count": ("updated_count", "updatedCount"),
        "missing_count": ("missing_count", "missingCount"),
        "deleted_count": ("deleted_count", "deletedCount"),
        "orphan_count": ("orphan_count", "orphanCount"),
        "error_code": ("error_code", "errorCode"),
        "started_at": ("started_at", "startedAt"),
        "finished_at": ("finished_at", "finishedAt"),
        "created_at": ("created_at", "createdAt"),
        "updated_at": ("updated_at", "updatedAt"),
    }
    payload: dict[str, Any] = {}
    for field in fields:
        value = _value_from(job, *aliases.get(field, (field,)), default=None)
        if field.endswith("_at"):
            value = _isoformat_or_none(value)
        if value is not None:
            payload[field] = value
    index_status = _value_from(job, "indexStatus", "index_status", default=None)
    if index_status is not None:
        payload["indexStatus"] = index_status
    return payload


def _unwrap_sync_job_create_result(result: Any) -> tuple[Any, bool]:
    if isinstance(result, tuple):
        job = result[0]
        reused = False
        if len(result) > 1 and isinstance(result[1], bool):
            reused = not result[1]
        return job, reused
    reused = bool(_value_from(result, "reused", "already_exists", "alreadyExists", default=False))
    created = _value_from(result, "created", default=None)
    if created is False:
        reused = True
    job = _value_from(result, "job", default=result)
    return job, reused


async def _create_workspace_sync_job(
    payload: AdminWorkspaceSyncJobCreateRequest,
    request: Request,
) -> tuple[Any, bool]:
    service = _load_workspace_sync_job_service()
    create_job = _module_callable(
        service,
        "create_or_get_job",
        "create_or_reuse_job",
        "acreate_or_reuse_job",
        "create_job",
        "enqueue_job",
    )
    if create_job is None:
        raise HTTPException(
            status_code=501,
            detail="workspace sync job create method is not available",
        )
    result = await _call_maybe_async(
        create_job,
        assistant_id=payload.assistant_id,
        scope_kind=payload.scope_kind,
        scope_slug=payload.scope_slug or "",
        mode=payload.mode,
        priority=_workspace_sync_job_priority(payload.priority),
        reason=payload.reason,
        idempotency_key=payload.idempotency_key,
        requested_by="admin-web",
        requested_from=request.client.host if request.client else "",
    )
    return _unwrap_sync_job_create_result(result)


async def _get_workspace_sync_job(job_id: str) -> Any:
    service = _load_workspace_sync_job_service()
    get_job = _module_callable(service, "get_job", "aget_job")
    if get_job is None:
        raise HTTPException(
            status_code=501,
            detail="workspace sync job get method is not available",
        )
    return await _call_maybe_async(get_job, job_id=job_id)


async def _workspace_sync_job_worker_loop(app: FastAPI) -> None:
    settings = get_settings()
    interval = max(1, settings.workspace_sync_job_worker_interval_seconds)
    batch_size = max(1, settings.workspace_sync_job_worker_batch_size)
    while True:
        processed = 0
        try:
            service = _load_workspace_sync_job_service()
            run_next = _module_callable(
                service,
                "run_next_pending_index_only_job",
                "run_next_pending_job",
                "arun_next_pending_job",
            )
            if run_next is None:
                log.warning("admin_workspace_sync_job_worker_unavailable")
            else:
                for _ in range(batch_size):
                    job = await _call_maybe_async(run_next)
                    if job is None:
                        break
                    processed += 1
                    log.info(
                        "admin_workspace_sync_job_finished",
                        job_id=_value_from(job, "job_id", "jobId", "id", default=""),
                        assistant_id=_value_from(job, "assistant_id", "assistantId", default=""),
                        status=_value_from(job, "status", default=""),
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "admin_workspace_sync_job_worker_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        await asyncio.sleep(0 if processed >= batch_size else interval)


def _workspace_file_records_for_bootstrap(
    assistants: list[Any],
    workspace_file_service: WorkspaceFileService,
) -> tuple[list[WorkspaceFile], bool]:
    records: list[WorkspaceFile] = []
    degraded = False
    for assistant in assistants:
        assistant_id = getattr(assistant, "assistant_id", "")
        if not assistant_id:
            continue
        try:
            cos_records = workspace_file_service.list_cos_workspace_summary_records(assistant_id)
        except (OSError, ValueError, SQLAlchemyError) as exc:
            cos_records = []
            log.warning(
                "admin_bootstrap_cos_workspace_files_failed",
                assistant_id=assistant_id,
                error=f"{type(exc).__name__}: {exc}",
            )
        if cos_records:
            records.extend(cos_records)
            continue
        # Fallback: query local database when COS is unavailable or returns empty
        try:
            db_records = workspace_file_service.list_files(assistant_id)
            if db_records:
                records.extend(db_records)
                continue
        except (OSError, ValueError, SQLAlchemyError) as exc:
            log.warning(
                "admin_bootstrap_db_fallback_failed",
                assistant_id=assistant_id,
                error=f"{type(exc).__name__}: {exc}",
            )
        degraded = True
    return records, degraded


def _workspace_capability_payloads(
    records: list[WorkspaceFile],
    *,
    catalog_by_key: dict[tuple[str, str], Any] | None = None,
    include_workspace_paths: bool = True,
    workspace_file_service: WorkspaceFileService | None = None,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    catalog_by_key = catalog_by_key or {}
    for record in records:
        slug = _workspace_capability_slug(record)
        if not slug:
            continue
        key = (record.assistant_id, record.bucket, slug)
        group = groups.get(key)
        if group is None:
            group = {
                "assistant_id": record.assistant_id,
                "bucket": record.bucket,
                "slug": slug,
                "files": [],
                "size": 0,
                "latest": record,
            }
            groups[key] = group
        group["files"].append(record)
        group["size"] += record.size_bytes
        group["latest"] = _latest_workspace_file(group["latest"], record)

    payloads: list[dict[str, Any]] = []
    for (assistant_id, bucket, slug), group in groups.items():
        latest = group["latest"]
        catalog_item = catalog_by_key.get((bucket, slug))
        workspace_path = f"{bucket}/{slug}/"
        if include_workspace_paths and workspace_file_service is not None:
            workspace_path = runtime_workspace_path(bucket, slug)
        updated_at = latest.updated_at or latest.created_at
        workspace_name, workspace_version = _workspace_capability_metadata(
            assistant_id=assistant_id,
            bucket=bucket,
            records=group["files"],
            workspace_file_service=workspace_file_service,
        )
        payloads.append(
            {
                "file_id": f"capability:{assistant_id}:{bucket}:{slug}",
                "assistant_id": assistant_id,
                "bucket": bucket,
                "filename": f"{slug}/",
                "origin": "capability_package",
                "source_type": "capability_package",
                "sync_status": latest.sync_status,
                "relative_path": f"{bucket}/{slug}/",
                "workspace_path": workspace_path,
                "isCapabilityPackage": True,
                "packageSlug": slug,
                "packageType": "skill" if bucket == "skills" else "plugin",
                "filesCount": len(group["files"]),
                "sizeBytes": group["size"],
                "latestUpdatedAt": updated_at.isoformat(timespec="seconds"),
                "name": workspace_name or getattr(catalog_item, "name", "") or slug,
                "version": workspace_version or getattr(catalog_item, "version", "") or "",
                "marketId": getattr(catalog_item, "id", "") or "",
            }
        )
    return sorted(
        payloads,
        key=lambda item: (
            str(item["assistant_id"]),
            str(item["bucket"]),
            str(item["packageSlug"]).lower(),
        ),
    )


def _installed_capabilities_for_assistant(
    *,
    assistant_id: str,
    assistant_service: AssistantService,
    workspace_file_service: WorkspaceFileService,
    catalog_by_key: dict[tuple[str, str], Any],
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    installed_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    cos_authoritative = False
    try:
        records: list[WorkspaceFile] = workspace_file_service.list_cos_capability_package_records(
            assistant_id,
            force_refresh=force_refresh,
        )
        cos_authoritative = bool(records)
        if not records:
            for bucket in ("skills", "plugins"):
                records.extend(
                    item
                    for item in workspace_file_service.list_files(assistant_id, bucket=bucket)
                    if item.source_type == "capability_package"
                )
        for item in _workspace_capability_payloads(
            records,
            catalog_by_key=catalog_by_key,
            workspace_file_service=workspace_file_service,
        ):
            kind = item["bucket"]
            slug = item["packageSlug"]
            installed_by_key[(kind, slug)] = {
                "id": slug,
                "slug": slug,
                "type": item["packageType"],
                "name": item["name"],
                "version": item["version"],
                "enabled": True,
                "filesCount": item["filesCount"],
                "install_path": item["workspace_path"],
                "sync_status": item["sync_status"],
            }
    except Exception as exc:
        log.warning(
            "admin_workspace_capability_sync_failed",
            assistant_id=assistant_id,
            error=f"{type(exc).__name__}: {exc}",
        )

    for workspace_item in assistant_service.list_workspace_items(assistant_id):
        key = (workspace_item.kind, workspace_item.slug)
        existing = installed_by_key.get(key)
        if existing is not None:
            existing["enabled"] = workspace_item.enabled
            existing["install_path"] = runtime_workspace_path(
                workspace_item.kind,
                workspace_item.slug,
            )
            existing["name"] = existing["name"] or workspace_item.name or workspace_item.slug
            existing["version"] = existing["version"] or workspace_item.version
            continue
        catalog_item = catalog_by_key.get(key)
        installed_by_key[key] = {
            "id": workspace_item.slug,
            "slug": workspace_item.slug,
            "type": "skill" if workspace_item.kind == "skills" else "plugin",
            "name": getattr(catalog_item, "name", "") or workspace_item.name or workspace_item.slug,
            "version": getattr(catalog_item, "version", "") or workspace_item.version,
            "enabled": workspace_item.enabled,
            "filesCount": 0,
            "install_path": runtime_workspace_path(workspace_item.kind, workspace_item.slug),
            "sync_status": "missing_from_cos" if cos_authoritative else "",
        }

    return sorted(
        installed_by_key.values(),
        key=lambda item: (item["type"], str(item["slug"]).lower()),
    )


class AdminModelUpdateRequest(BaseModel):
    name: str
    provider: str
    url: str = ""
    apiKey: str = ""
    status: str = "active"


_CAPABILITY_PROFILE_LABELS: dict[str, str] = {
    "chat": "仅对话",
    "onlyread": "只读空间",
    "onlywrite": "读写空间",
    "work_no_exec": "读写空间（无命令行）",
    "dangerous": "高危操作",
}
for p in CAPABILITY_PROFILES:
    if p not in _CAPABILITY_PROFILE_LABELS:
        _CAPABILITY_PROFILE_LABELS[p] = p


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip()).strip("-").lower()
    return slug


def _generate_assistant_slug(prefix: str = "assistant") -> str:
    return f"{prefix}-{uuid4().hex[:8]}"


def _assistant_name_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _resolve_assistant_identity(
    *,
    assistant_id: str | None,
    workspace_slug: str,
    name: str,
) -> tuple[str, str]:
    explicit_workspace_slug = workspace_slug.strip()
    resolved_assistant_id = (assistant_id or "").strip()

    if not resolved_assistant_id:
        resolved_assistant_id = _slugify(explicit_workspace_slug)
    if not resolved_assistant_id:
        resolved_assistant_id = _slugify(name)
    if not resolved_assistant_id:
        resolved_assistant_id = _generate_assistant_slug()

    resolved_workspace_slug = explicit_workspace_slug or resolved_assistant_id
    return resolved_assistant_id, resolved_workspace_slug


def _normalize_capability_profile(raw_profile: str) -> CapabilityProfileName:
    profile = raw_profile.strip().lower()
    if profile in CAPABILITY_PROFILES:
        return profile

    aliases: dict[str, CapabilityProfileName] = {
        "": "chat",
        "general": "chat",
        "default": "chat",
        "read": "onlyread",
        "readonly": "onlyread",
        "read_only": "onlyread",
        "write": "onlywrite",
        "writeonly": "onlywrite",
        "write_only": "onlywrite",
        "work": "work_no_exec",
        "no_exec": "work_no_exec",
        "noexec": "work_no_exec",
        "safe_write": "work_no_exec",
        "safe-write": "work_no_exec",
        "full": "dangerous",
        "danger": "dangerous",
    }
    return aliases.get(profile, "chat")


def _capability_profile_file_exists(profile: str) -> bool:
    if not profile:
        return False
    return (get_settings().config_dir / "capabilities" / f"{profile}.yaml").exists()


def _resolve_capability_profile(raw_profile: str) -> CapabilityProfileName:
    profile = raw_profile.strip().lower()
    normalized = _normalize_capability_profile(profile)
    if normalized != "chat" or profile in {"", "chat", "general", "default"}:
        return normalized
    if _capability_profile_file_exists(profile):
        return profile
    raise HTTPException(status_code=400, detail=f"unknown capability profile: {raw_profile}")


def _append_chat_session_path(base_url: str) -> str:
    parts = urlsplit(base_url.strip())
    path = parts.path.rstrip("/")
    if path.endswith("/chat") or path == "/chat":
        next_path = path or "/chat"
    else:
        next_path = f"{path}/chat" if path else "/chat"
    return urlunsplit(parts._replace(path=next_path, query=urlencode([("session", "main")])))


def _resolve_control_ui_chat_url(settings: Any) -> str:
    control_ui_url = settings.control_ui_public_url.strip()
    if control_ui_url:
        return _append_chat_session_path(control_ui_url)

    gateway_host = (getattr(settings, "iron_agent_gateway_host", "") or "").strip() or "127.0.0.1"
    if gateway_host in {"0.0.0.0", "::", "[::]"}:
        gateway_host = "127.0.0.1"
    gateway_port = getattr(settings, "iron_agent_gateway_port", 18789) or 18789
    return f"http://{gateway_host}:{gateway_port}/chat"


def _resolve_control_ui_allowed_origins(settings: Any) -> list[str]:
    candidates: list[str] = []
    raw_allowed_origins = (getattr(settings, "control_ui_allowed_origins_json", "") or "").strip()
    if raw_allowed_origins:
        try:
            parsed = json.loads(raw_allowed_origins)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            candidates.extend(str(item).strip() for item in parsed if str(item).strip())

    control_ui_url = settings.control_ui_public_url.strip()
    if control_ui_url:
        candidates.append(control_ui_url)
    if not candidates:
        gateway_host = (
            (getattr(settings, "iron_agent_gateway_host", "") or "").strip() or "127.0.0.1"
        )
        if gateway_host in {"0.0.0.0", "::", "[::]"}:
            gateway_host = "127.0.0.1"
        gateway_port = getattr(settings, "iron_agent_gateway_port", 18789) or 18789
        candidates.extend(
            [
                f"http://{gateway_host}:{gateway_port}",
                "http://127.0.0.1:19001",
                "http://localhost:19001",
            ]
        )

    origins: list[str] = []
    for candidate in candidates:
        parts = urlsplit(candidate.strip())
        if not parts.scheme or not parts.netloc:
            continue
        hostname = parts.hostname or ""
        port = parts.port
        netlocs = [parts.netloc]
        if hostname == "127.0.0.1":
            netlocs.append(f"localhost:{port}" if port else "localhost")
        elif hostname == "localhost":
            netlocs.append(f"127.0.0.1:{port}" if port else "127.0.0.1")
        for netloc in netlocs:
            origin = f"{parts.scheme}://{netloc}"
            if origin not in origins:
                origins.append(origin)
    return origins


def _sync_runtime_control_ui_allowed_origins(record: Any, settings: Any) -> bool:
    state_root = Path((getattr(record.runtime, "state_root", "") or "").strip())
    if not state_root:
        return False
    config_path = state_root / "ironagent.json"
    if not config_path.exists():
        return False
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False

    gateway = payload.setdefault("gateway", {})
    if not isinstance(gateway, dict):
        gateway = {}
        payload["gateway"] = gateway
    control_ui = gateway.setdefault("controlUi", {})
    if not isinstance(control_ui, dict):
        control_ui = {}
        gateway["controlUi"] = control_ui

    allowed_origins = [
        str(origin).strip()
        for origin in control_ui.get("allowedOrigins", [])
        if str(origin).strip()
    ]
    changed = False
    for origin in _resolve_control_ui_allowed_origins(settings):
        if origin not in allowed_origins:
            allowed_origins.append(origin)
            changed = True
    if not changed:
        return False

    control_ui["allowedOrigins"] = allowed_origins
    config_path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def _build_models(
    model_registry: ModelRegistryService,
    assistants: list[Any],
) -> list[dict[str, Any]]:
    models = []
    for item in model_registry.import_from_assistants(assistants):
        models.append(_build_model_payload(item, model_registry, assistants))
    return sorted(models, key=lambda item: (item["name"], item["provider"]))


def _masked_api_key(api_key: str) -> str:
    return MASKED_API_KEY if api_key.strip() else ""


def _build_model_payload(
    item: Any,
    model_registry: ModelRegistryService,
    assistants: list[Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": item.model_id,
        "name": item.name,
        "provider": item.provider,
        "url": item.url,
        "apiKey": _masked_api_key(item.api_key),
        "apiKeySet": bool(item.api_key.strip()),
        "status": item.status,
        "assistantIds": model_registry.assistant_ids_for_model(item, assistants),
    }


def _build_assistant_model_config_payload(config: AssistantIronAgentConfig) -> dict[str, Any]:
    return {
        "primary_model": config.primary_model,
        "auth_mode": config.auth_mode,
        "provider_id": config.provider_id,
        "provider_base_url": config.provider_base_url,
        "provider_api_key_set": bool(config.provider_api_key.strip()),
    }


def _build_capability_profiles() -> list[dict[str, Any]]:
    settings = get_settings()
    capability_dir = settings.config_dir / "capabilities"

    if not capability_dir.exists():
        capability_dir.mkdir(parents=True, exist_ok=True)
        source_dir = Path(__file__).parent.parent.parent / "config" / "capabilities"
        for profile in DEFAULT_CAPABILITY_PROFILES:
            target = capability_dir / f"{profile}.yaml"
            source = source_dir / f"{profile}.yaml"
            if source.exists():
                target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                continue
            payload = {
                "name": _CAPABILITY_PROFILE_LABELS.get(profile, profile),
                "level": "",
                "allowed_tools": [],
                "approval_tools": [],
            }
            target.write_text(
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

    profiles: list[dict[str, Any]] = []
    profile_order = {profile: index for index, profile in enumerate(CAPABILITY_PROFILES)}
    for path in sorted(
        capability_dir.glob("*.yaml"),
        key=lambda item: (profile_order.get(item.stem, len(profile_order)), item.stem),
    ):
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        profiles.append(
            {
                "id": path.stem,
                "name": payload.get("name", path.stem),
                "level": payload.get("level", ""),
                "allowed_tools": payload.get("allowed_tools", []),
                "approval_tools": payload.get("approval_tools", []),
            }
        )
    return profiles


def _tool_allowlist_from_capability_file(profile_id: str) -> list[str]:
    capability_path = get_settings().config_dir / "capabilities" / f"{profile_id}.yaml"
    payload = yaml.safe_load(capability_path.read_text(encoding="utf-8")) or {}
    return sorted(
        {
            str(tool).strip()
            for tool in [
                *payload.get("allowed_tools", []),
                *payload.get("approval_tools", []),
            ]
            if str(tool).strip()
        }
    )


def _sync_runtime_tool_allowlist(record: Any, allowed_tools: list[str]) -> bool | None:
    runtime = getattr(record, "runtime", None)
    state_root = Path((getattr(runtime, "state_root", "") or "").strip())
    if not state_root:
        return None
    config_path = state_root / "ironagent.json"
    if not config_path.exists():
        return None
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return False

    tools = payload.setdefault("tools", {})
    if not isinstance(tools, dict):
        tools = {}
        payload["tools"] = tools
    if tools.get("allow") == allowed_tools:
        return False

    tools["allow"] = allowed_tools
    config_path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def _sync_runtime_manifest_tool_allowlist(
    record: Any,
    allowed_tools: list[str],
) -> bool | None:
    assistant_id = str(getattr(record, "assistant_id", "") or "").strip()
    if not assistant_id:
        return None
    manifest_path = (
        get_settings().assistants_runtime_root / assistant_id / "k8s-runtime.yaml"
    ).resolve()
    if not manifest_path.exists():
        return None

    docs = list(yaml.safe_load_all(manifest_path.read_text(encoding="utf-8")))
    changed = False
    for doc in docs:
        if not isinstance(doc, dict) or doc.get("kind") != "Secret":
            continue
        string_data = doc.get("stringData")
        if not isinstance(string_data, dict) or "ironagent.json" not in string_data:
            continue
        try:
            payload = json.loads(str(string_data["ironagent.json"]))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        tools = payload.setdefault("tools", {})
        if not isinstance(tools, dict):
            tools = {}
            payload["tools"] = tools
        if tools.get("allow") == allowed_tools:
            continue
        tools["allow"] = allowed_tools
        string_data["ironagent.json"] = json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
        changed = True

    if not changed:
        return False
    manifest_path.write_text(
        yaml.safe_dump_all(docs, sort_keys=False, explicit_start=True),
        encoding="utf-8",
    )
    return True


def _refresh_assistants_for_capability_profile(profile_id: str) -> tuple[int, list[str]]:
    profile = profile_id.strip()
    if not profile:
        return 0, []
    service = AssistantService()
    refreshed = 0
    warnings: list[str] = []
    for assistant in service.list_assistants():
        if str(getattr(assistant, "capability_profile", "")).strip() != profile:
            continue
        try:
            allowed_tools = _tool_allowlist_from_capability_file(profile)
            config_changed = _sync_runtime_tool_allowlist(assistant, allowed_tools)
            manifest_changed = _sync_runtime_manifest_tool_allowlist(assistant, allowed_tools)
            service.upsert_assistant(
                assistant_id=assistant.assistant_id,
                name=assistant.name,
                capability_profile=assistant.capability_profile,
                notes=assistant.notes,
                model_config=assistant.iron_agent_config,
                workspace_config=assistant.workspace_config,
                provision_runtime=False,
            )
            refreshed += 1
            if config_changed is None and manifest_changed is None:
                warnings.append(f"{assistant.assistant_id}: runtime config not found")
        except Exception as exc:
            message = f"{assistant.assistant_id}: {type(exc).__name__}: {exc}"
            warnings.append(message)
            log.warning(
                "capability_profile_assistant_refresh_failed",
                profile_id=profile,
                assistant_id=assistant.assistant_id,
                error=f"{type(exc).__name__}: {exc}",
            )
    return refreshed, warnings


def _audit_occurred_at(payload: dict[str, Any]) -> str:
    envelope = payload.get("envelope") if isinstance(payload.get("envelope"), dict) else {}
    occurred_at = envelope.get("occurred_at") if isinstance(envelope, dict) else ""
    return str(occurred_at or "")


def _audit_success(payload: dict[str, Any]) -> bool | None:
    envelope = payload.get("envelope") if isinstance(payload.get("envelope"), dict) else {}
    hook_name = str(envelope.get("hook_name") or payload.get("hook_name") or "").strip().lower()
    decision = str(payload.get("decision", "")).strip().lower()
    if hook_name == "approval":
        if decision == "pending_approval":
            return None
        if decision == "timeout":
            return False
        if decision in {"allow", "deny"}:
            return True

    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    explicit = result.get("success") if isinstance(result, dict) else payload.get("success")
    if isinstance(explicit, bool):
        return explicit

    if decision in {"approved", "accepted", "deleted", "synced", "success"}:
        return True
    if decision in {"expired", "rejected", "failed", "error"}:
        return False
    return None


def _build_assistant_profile_index(assistants: list[Any]) -> dict[str, str]:
    return {str(item.assistant_id): str(item.capability_profile or "") for item in assistants}


def _audit_log_item(
    row: dict[str, Any],
    *,
    assistant_profile_by_id: dict[str, str],
) -> dict[str, Any]:
    envelope = row.get("envelope") if isinstance(row.get("envelope"), dict) else {}
    assistant = row.get("assistant") if isinstance(row.get("assistant"), dict) else {}
    tool = row.get("tool") if isinstance(row.get("tool"), dict) else {}
    request_id = str(envelope.get("request_id") or row.get("request_id", ""))
    hook_name = str(envelope.get("hook_name") or row.get("hook_name", ""))
    assistant_id = audit_payload_assistant_id(row)
    return {
        "audit_key": f"{request_id}\u001f{hook_name}",
        "request_id": request_id,
        "hook_name": hook_name,
        "time": _audit_occurred_at(row),
        "user_id": audit_payload_user_id(row),
        "assistant_id": assistant_id,
        "operation_type": audit_payload_operation_type(row),
        "tool": tool.get("name", row.get("tool_name", "")),
        "capability_profile": (
            assistant.get("capability_profile")
            or assistant_profile_by_id.get(assistant_id, "")
            or row.get("capability_profile", "")
        ),
        "decision": row.get("decision", ""),
        "result": audit_payload_result(row),
        "success": _audit_success(row),
        "error_code": audit_payload_error_code(row),
        "raw": row,
    }


def _parse_audit_time_filter(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid audit time filter: {raw}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def _audit_filters_from_query(
    *,
    start_time: str | None,
    end_time: str | None,
    user_id: str | None,
    assistant_id: str | None,
    operation_type: str | None,
    result: str | None,
    error_code: str | None,
) -> AuditLogFilters | None:
    filters = AuditLogFilters(
        occurred_from=_parse_audit_time_filter(start_time),
        occurred_to=_parse_audit_time_filter(end_time),
        user_id=(user_id or "").strip() or None,
        assistant_id=(assistant_id or "").strip() or None,
        operation_type=(operation_type or "").strip() or None,
        result=(result or "").strip() or None,
        error_code=(error_code or "").strip() or None,
    )
    if not any(
        [
            filters.occurred_from,
            filters.occurred_to,
            filters.user_id,
            filters.assistant_id,
            filters.operation_type,
            filters.result,
            filters.error_code,
        ]
    ):
        return None
    return filters


def _audit_page_payload(
    *,
    page: int,
    page_size: int,
    rows: list[dict[str, Any]],
    total: int,
    assistant_profile_by_id: dict[str, str],
) -> dict[str, Any]:
    total_pages = max(1, (total + page_size - 1) // page_size)
    normalized_page = min(max(1, page), total_pages)
    return {
        "auditLogs": [
            _audit_log_item(row, assistant_profile_by_id=assistant_profile_by_id) for row in rows
        ],
        "auditPagination": {
            "page": normalized_page,
            "pageSize": page_size,
            "total": total,
            "totalPages": total_pages,
            "hasPrevious": normalized_page > 1,
            "hasNext": normalized_page < total_pages,
        },
    }


def _audit_export_response(
    *,
    rows: list[dict[str, Any]],
    export_format: str,
    assistant_profile_by_id: dict[str, str],
) -> Response:
    items = [_audit_log_item(row, assistant_profile_by_id=assistant_profile_by_id) for row in rows]
    normalized_format = export_format.strip().lower()
    if normalized_format == "json":
        return Response(
            json.dumps(items, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="audit-logs.json"'},
        )
    if normalized_format != "csv":
        raise HTTPException(status_code=400, detail="format must be csv or json")

    buffer = io.StringIO()
    fieldnames = [
        "request_id",
        "time",
        "user_id",
        "assistant_id",
        "operation_type",
        "result",
        "error_code",
        "hook_name",
        "tool",
        "capability_profile",
        "decision",
        "success",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for item in items:
        writer.writerow(item)
    return Response(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="audit-logs.csv"'},
    )


def _workspace_item_index_readiness(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "parent": str(path.parent),
        "writable": False,
    }
    probe_path = path.parent / f".{path.name}.readyz-{uuid4().hex}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe_path.write_text("ok", encoding="utf-8")
        probe_path.unlink(missing_ok=True)
    except OSError as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    result["writable"] = True
    return result


def _admin_readiness_payload() -> tuple[dict[str, Any], int]:
    item_index = _workspace_item_index_readiness(get_settings().workspace_item_index_sqlite_path)
    try:
        workspace_metadata = check_workspace_file_metadata_readiness()
    except Exception as exc:
        workspace_metadata = {
            "status": "degraded",
            "error": f"{type(exc).__name__}: {exc}",
        }
    ready = item_index["writable"] is True and workspace_metadata.get("status") == "ok"
    return (
        {
            "status": "ok" if ready else "degraded",
            "checks": {
                "workspaceItemIndex": item_index,
                "workspaceFileMetadata": workspace_metadata,
            },
        },
        200 if ready else 503,
    )


def _with_audit_export_pagination_headers(
    response: Response,
    *,
    page: int,
    page_size: int,
    total: int,
    item_count: int,
) -> Response:
    total_pages = max(1, (total + page_size - 1) // page_size)
    values = {
        "Page": str(page),
        "Page-Size": str(page_size),
        "Total": str(total),
        "Total-Pages": str(total_pages),
        "Has-Next": "true" if page < total_pages else "false",
        "Truncated": "true" if total > item_count else "false",
    }
    for suffix, value in values.items():
        response.headers[f"X-IronAgent-Export-{suffix}"] = value
    return response


def _build_approval_audit_event(
    *,
    approval_id: str,
    resolution: str,
    approval: Any,
) -> dict[str, Any]:
    occurred_at = approval.resolved_at or approval.requested_at
    return {
        "envelope": {
            "protocol_version": HOOK_PROTOCOL_VERSION,
            "request_id": approval_id,
            "source": "admin_web",
            "hook_name": "approval_resolution",
            "occurred_at": occurred_at.isoformat(),
        },
        "assistant": {
            "assistant_id": approval.assistant_id,
            "capability_profile": approval.capability_profile or "",
        },
        "tool": {
            "name": approval.tool_name,
            "args": {
                "approval_id": approval_id,
                "approval_scope": approval.approval_scope,
                "target": approval.target,
            },
        },
        "decision": resolution,
        "result": {
            "success": resolution == "allow",
            "error": None,
            "output_summary": {
                "approval_status": approval.status,
            },
        },
        "metadata": {
            "reason": approval.reason,
            "target": approval.target,
            "approval_scope": approval.approval_scope,
            "approval_status": approval.status,
            "resolved_at": approval.resolved_at.isoformat() if approval.resolved_at else None,
        },
    }


def _operator_id_from_request(request: Request) -> str:
    return request.headers.get("x-admin-operator", "").strip() or "admin-web"


async def _append_capability_audit(
    *,
    request: Request,
    assistant_id: str,
    kind: str,
    slug: str,
    version: str,
    action: str,
    runtime_reload_status: str,
    runtime_reload_error: str,
) -> list[str]:
    warnings: list[str] = []
    try:
        await asyncio.to_thread(
            build_audit_repository().append_event,
            capability_audit_event(
                operator_id=_operator_id_from_request(request),
                assistant_id=assistant_id,
                kind=kind,
                capability_id=slug,
                slug=slug,
                version=version,
                action=action,
                runtime_reload_status=runtime_reload_status,
                runtime_reload_error=runtime_reload_error,
            ),
        )
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        warnings.append(f"audit append failed: {message}")
        log.warning(
            "capability_audit_append_failed",
            assistant_id=assistant_id,
            kind=kind,
            slug=slug,
            action=action,
            error=message,
        )
    return warnings


def _isoformat_datetime(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat(timespec="seconds")
    return str(value or "")


def _assistant_runtime_display_payload(
    *,
    status: str,
    observed_status: str,
    desired_status: str,
    sync_state: str,
    last_runtime_error: str,
    runtime_probe_degraded: bool,
    polling_source: str,
    partial: bool,
    last_observed_at: str,
) -> dict[str, Any]:
    display_status = status or observed_status or "unknown"
    display_severity = "neutral"
    display_label = {
        "running": "运行中",
        "starting": "启动中",
        "pending": "启动中",
        "stopping": "停止中",
        "restarting": "重启中",
        "paused": "已暂停",
        "stopped": "已停止",
        "failed": "失败",
    }.get(display_status, display_status or "未知")
    display_reason = ""

    if (
        status == "failed"
        or observed_status == "failed"
        or desired_status == "failed"
        or sync_state == "failed"
    ):
        display_status = "failed"
        display_severity = "error"
        display_label = "异常"
        display_reason = last_runtime_error or "runtime state failed"
    elif sync_state == "degraded":
        display_status = "degraded"
        display_severity = "warning"
        display_label = "同步降级"
        display_reason = last_runtime_error or "runtime sync degraded"
    elif runtime_probe_degraded or sync_state == "stale":
        display_status = "stale"
        display_severity = "warning"
        display_label = "状态待确认"
        display_reason = last_runtime_error or "runtime probe degraded"
    elif observed_status != desired_status:
        display_status = "drift"
        display_severity = "warning"
        display_label = "状态不一致"
        display_reason = f"desired={desired_status}; observed={observed_status}"
    elif partial:
        display_severity = "info"
        display_reason = "partial status payload"
    elif status == "running":
        display_severity = "success"
    elif status in {"starting", "pending", "stopping", "restarting"}:
        display_severity = "info"

    if not display_reason:
        display_reason = sync_state or polling_source

    return {
        "displayStatus": display_status,
        "displaySeverity": display_severity,
        "displayLabel": display_label,
        "displayReason": display_reason,
        "displaySource": polling_source,
        "displayPartial": partial,
        "displayObservedAt": last_observed_at,
    }


def _assistant_runtime_freshness_payload(
    runtime: Any,
    *,
    runtime_probe_degraded: bool,
    polling_source: str,
    partial: bool,
) -> dict[str, Any]:
    status = str(getattr(runtime, "status", "") or "pending")
    observed_status = str(
        getattr(runtime, "observed_status", None)
        or getattr(runtime, "observed_state", None)
        or status
    )
    desired_status = str(
        getattr(runtime, "desired_status", None)
        or getattr(runtime, "desired_state", None)
        or status
    )
    sync_state = str(getattr(runtime, "sync_state", "") or "").strip()
    if sync_state == "synced":
        sync_state = "current"
    if runtime_probe_degraded and sync_state not in {"failed", "degraded"}:
        sync_state = "stale"
    elif not sync_state:
        sync_state = "stale" if runtime_probe_degraded else "current"
    last_observed_at = _isoformat_datetime(
        getattr(runtime, "last_observed_at", None)
        or getattr(runtime, "observed_at", None)
        or getattr(runtime, "updated_at", "")
    )
    last_reconciled_at = _isoformat_datetime(
        getattr(runtime, "last_reconciled_at", None)
        or getattr(runtime, "reconciled_at", None)
        or getattr(runtime, "updated_at", "")
    )
    last_runtime_error = str(
        getattr(runtime, "last_runtime_error", None) or getattr(runtime, "last_error", "") or ""
    )
    return {
        "observedState": observed_status,
        "desiredState": desired_status,
        "observedStatus": observed_status,
        "desiredStatus": desired_status,
        "syncState": sync_state,
        "lastObservedAt": last_observed_at,
        "lastReconciledAt": last_reconciled_at,
        "lastRuntimeError": last_runtime_error,
        "runtimeProbeDegraded": runtime_probe_degraded,
        "pollingSource": polling_source,
        "partial": partial,
        **_assistant_runtime_display_payload(
            status=status,
            observed_status=observed_status,
            desired_status=desired_status,
            sync_state=sync_state,
            last_runtime_error=last_runtime_error,
            runtime_probe_degraded=runtime_probe_degraded,
            polling_source=polling_source,
            partial=partial,
            last_observed_at=last_observed_at,
        ),
    }


def _runtime_auth_failure_message(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    lower = normalized.lower()
    if any(marker in lower for marker in OPENAI_CODEX_RUNTIME_AUTH_FAILURE_MARKERS):
        return "OpenAI Codex OAuth 已失效，请在宿主机重新登录后重启或修复对应助手运行时"
    return ""


def _assistant_status_item_payload(
    item: Any,
    *,
    runtime_probe_degraded: bool = False,
) -> dict[str, Any]:
    runtime = getattr(item, "runtime", None)
    return {
        "id": str(getattr(item, "assistant_id", "") or ""),
        "status": str(getattr(runtime, "status", "") or "pending"),
        "container": getattr(runtime, "container_name", None),
        "gateway": getattr(runtime, "gateway_url", None),
        "lastStatusAt": _isoformat_datetime(getattr(runtime, "updated_at", "")),
        "lastError": str(getattr(runtime, "last_error", "") or ""),
        **_assistant_runtime_freshness_payload(
            runtime,
            runtime_probe_degraded=runtime_probe_degraded,
            polling_source="status_snapshot",
            partial=False,
        ),
    }


def _assistant_status_snapshot_sync() -> dict[str, Any]:
    service = AssistantService()
    runtime_probe_degraded = False
    try:
        assistants = _active_admin_assistants(service.list_assistants())
    except Exception as exc:
        log.warning(
            "admin_assistant_status_snapshot_failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        assistants = []
        runtime_probe_degraded = True
    assistant_items = []
    for item in assistants:
        assistant_items.append(
            _assistant_status_item_payload(
                item,
                runtime_probe_degraded=runtime_probe_degraded,
            )
        )
    return {
        "assistants": assistant_items,
        "runtimeProbeDegraded": runtime_probe_degraded,
        "fallbackPollMs": _assistant_status_fallback_poll_ms(),
        "pollingSource": "status_snapshot",
        "partial": False,
    }


def _cache_wall_time_after(seconds: float) -> str:
    return datetime.fromtimestamp(time.time() + seconds).astimezone().isoformat(timespec="seconds")


def _with_cache_metadata(
    payload: dict[str, Any],
    *,
    cache_status: str,
    ttl_seconds: float,
) -> dict[str, Any]:
    next_payload = dict(payload)
    next_payload["cacheStatus"] = cache_status
    next_payload.setdefault(
        "generatedAt",
        datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    next_payload["expiresAt"] = _cache_wall_time_after(ttl_seconds)
    return next_payload


async def _get_assistant_status_snapshot_cached(
    app: FastAPI,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    ttl_seconds = _assistant_status_cache_ttl_seconds()
    now = time.monotonic()
    cache = getattr(app.state, "assistant_status_cache", None)
    if (
        not force_refresh
        and cache
        and cache.get("payload") is not None
        and cache.get("expires_at", 0.0) > now
    ):
        return _with_cache_metadata(
            cache["payload"],
            cache_status="fresh",
            ttl_seconds=max(0.0, cache.get("expires_at", now) - now),
        )

    lock = getattr(app.state, "assistant_status_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app.state.assistant_status_lock = lock

    async with lock:
        now = time.monotonic()
        cache = getattr(app.state, "assistant_status_cache", None)
        if (
            not force_refresh
            and cache
            and cache.get("payload") is not None
            and cache.get("expires_at", 0.0) > now
        ):
            return _with_cache_metadata(
                cache["payload"],
                cache_status="fresh",
                ttl_seconds=max(0.0, cache.get("expires_at", now) - now),
            )

        payload = await asyncio.to_thread(_assistant_status_snapshot_sync)
        expires_at = time.monotonic() + ttl_seconds
        app.state.assistant_status_cache = {
            "payload": payload,
            "expires_at": expires_at,
        }
        return _with_cache_metadata(
            payload,
            cache_status="fresh",
            ttl_seconds=ttl_seconds,
        )


def _assistant_needs_background_runtime_sync(assistant: Any) -> bool:
    if _assistant_is_deleted(assistant):
        return False
    runtime = getattr(assistant, "runtime", None)
    if runtime is None:
        return True
    status = str(getattr(runtime, "status", "") or "").strip().lower()
    if status == "restarting":
        updated_at = getattr(runtime, "updated_at", None)
        if not hasattr(updated_at, "timestamp"):
            return False
        try:
            return time.time() - updated_at.timestamp() >= ASSISTANT_RESTARTING_HEALTH_GRACE_SECONDS
        except (OSError, OverflowError, TypeError, ValueError):
            return False
    return status in {"pending", "starting"}


def _run_restart_assistant_task(assistant_id: str) -> None:
    try:
        service = AssistantService()
        record = service.restart_assistant(assistant_id)
        if record is not None:
            _sync_runtime_control_ui_allowed_origins(record, get_settings())
    except Exception as exc:
        log.warning(
            "assistant_restart_background_failed",
            assistant_id=assistant_id,
            error=f"{type(exc).__name__}: {exc}",
        )


def _list_assistants_for_runtime_task(service: AssistantService) -> list[Any]:
    return _active_admin_assistants(service.list_assistants())


def _sync_runtime_views_for_runtime_task(
    service: AssistantService,
    assistants: list[Any] | None = None,
) -> tuple[list[Any], dict[str, Any], int]:
    if assistants is None:
        return service.sync_runtime_views()
    return service.sync_runtime_views(assistants)


async def _restart_assistant_in_background(app: FastAPI, assistant_id: str) -> None:
    await _run_admin_runtime_task(app, _run_restart_assistant_task, assistant_id)


def _track_admin_background_task(app: FastAPI, task: asyncio.Task[Any]) -> None:
    tasks = getattr(app.state, "admin_background_tasks", None)
    if tasks is None:
        tasks = set()
        app.state.admin_background_tasks = tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def _assistant_status_event_stream(app: FastAPI):
    while True:
        try:
            payload = await _get_assistant_status_snapshot_cached(app)
            yield "event: assistant_status\n"
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            await asyncio.sleep(ASSISTANT_RUNTIME_HEALTH_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "admin_assistant_status_stream_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            yield "event: assistant_status_error\n"
            yield 'data: {"error":"assistant status stream failed"}\n\n'
            await asyncio.sleep(ASSISTANT_RUNTIME_HEALTH_INTERVAL_SECONDS)


def _run_assistant_runtime_health_check() -> dict[str, int]:
    try:
        service = AssistantService()
        assistants = _list_assistants_for_runtime_task(service)
        sync_targets = [
            assistant
            for assistant in assistants
            if _assistant_needs_background_runtime_sync(assistant)
        ]
        if not sync_targets:
            return {"checked": 0, "updated": 0}
        _, _, updated_count = _sync_runtime_views_for_runtime_task(service, sync_targets)
    except Exception as exc:
        log.warning(
            "assistant_runtime_health_check_failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        return {"checked": 0, "updated": 0}

    if updated_count:
        log.info(
            "assistant_runtime_health_reconciled",
            checked=len(sync_targets),
            updated=updated_count,
        )
    return {"checked": len(sync_targets), "updated": updated_count}


async def _assistant_runtime_health_loop(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(ASSISTANT_RUNTIME_HEALTH_INTERVAL_SECONDS)
        await _run_admin_runtime_task(app, _run_assistant_runtime_health_check)


def _assistant_lifecycle_audit_event(
    *,
    request: Request,
    assistant_id: str,
    action: str,
    previous_status: str,
    next_status: str,
) -> dict[str, Any]:
    occurred_at = datetime.now().astimezone()
    operator_id = _operator_id_from_request(request)
    return {
        "envelope": {
            "protocol_version": HOOK_PROTOCOL_VERSION,
            "request_id": str(uuid4()),
            "source": "admin_web",
            "hook_name": "assistant_lifecycle",
            "occurred_at": occurred_at.isoformat(timespec="seconds"),
        },
        "operator": {
            "operator_id": operator_id,
        },
        "session": {
            "user_id": operator_id,
        },
        "assistant": {
            "assistant_id": assistant_id,
            "capability_profile": "",
        },
        "tool": {
            "name": action,
            "args": {
                "assistant_id": assistant_id,
                "previous_status": previous_status,
                "next_status": next_status,
            },
        },
        "decision": action,
        "operation_type": action,
        "result": {
            "success": True,
            "status": "success",
            "error": None,
            "output_summary": {
                "action": action,
                "previous_status": previous_status,
                "next_status": next_status,
            },
        },
        "metadata": {
            "operator_id": operator_id,
            "assistant_id": assistant_id,
            "action": action,
            "previous_status": previous_status,
            "next_status": next_status,
            "source": "manual_admin_lifecycle",
        },
    }


async def _append_assistant_lifecycle_audit(
    *,
    request: Request,
    assistant_id: str,
    action: str,
    previous_status: str,
    next_status: str,
) -> list[str]:
    warnings: list[str] = []
    try:
        await asyncio.to_thread(
            build_audit_repository().append_event,
            _assistant_lifecycle_audit_event(
                request=request,
                assistant_id=assistant_id,
                action=action,
                previous_status=previous_status,
                next_status=next_status,
            ),
        )
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        warnings.append(f"audit append failed: {message}")
        log.warning(
            "assistant_lifecycle_audit_append_failed",
            assistant_id=assistant_id,
            action=action,
            error=message,
        )
    return warnings


def _build_audit_page_sync(
    *,
    page: int = 1,
    page_size: int = 25,
    filters: AuditLogFilters | None = None,
) -> dict[str, Any]:
    normalized_page = max(1, page)
    normalized_page_size = min(max(1, page_size), 100)
    offset = (normalized_page - 1) * normalized_page_size
    try:
        ApprovalService().expire_pending_requests()
        audit_repository = build_audit_repository()
        audit_page = audit_repository.list_page(
            limit=normalized_page_size,
            offset=offset,
            filters=filters,
        )
    except Exception as exc:
        log.warning(
            "admin_audit_page_load_failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        return _audit_page_payload(
            page=normalized_page,
            page_size=normalized_page_size,
            rows=[],
            total=0,
            assistant_profile_by_id={},
        )
    total_pages = max(1, (audit_page.total + normalized_page_size - 1) // normalized_page_size)
    if normalized_page > total_pages:
        normalized_page = total_pages
        offset = (normalized_page - 1) * normalized_page_size
        audit_page = audit_repository.list_page(
            limit=normalized_page_size,
            offset=offset,
            filters=filters,
        )
    try:
        assistants = AssistantService().list_assistants()
    except Exception as exc:
        log.warning(
            "admin_audit_assistant_index_failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        assistants = []
    return _audit_page_payload(
        page=normalized_page,
        page_size=normalized_page_size,
        rows=audit_page.items,
        total=audit_page.total,
        assistant_profile_by_id=_build_assistant_profile_index(assistants),
    )


def _load_assistants_for_bootstrap(
    assistant_service: AssistantService,
) -> tuple[list[Any], bool]:
    try:
        assistants, _, _ = _sync_runtime_views_for_runtime_task(assistant_service)
        return assistants, False
    except Exception as exc:
        log.warning(
            "admin_bootstrap_runtime_probe_failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        try:
            return assistant_service.list_assistants(), True
        except Exception as list_exc:
            log.warning(
                "admin_bootstrap_assistant_fallback_failed",
                error=f"{type(list_exc).__name__}: {list_exc}",
            )
            return [], True


def _load_assistants_for_bootstrap_fast(
    assistant_service: AssistantService,
    *,
    probe_transient_runtime: bool = True,
) -> tuple[list[Any], bool]:
    assistants: list[Any] = []
    try:
        assistants = _active_admin_assistants(assistant_service.list_assistants())
        if not probe_transient_runtime:
            return assistants, False
        transient_assistants = [
            assistant for assistant in assistants if _assistant_runtime_needs_fast_sync(assistant)
        ]
        if not transient_assistants:
            return assistants, False

        updated_assistants, _, _ = _sync_runtime_views_for_runtime_task(
            assistant_service,
            transient_assistants,
        )
    except Exception as exc:
        log.warning(
            "admin_bootstrap_fast_assistant_load_failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        return assistants, True

    updated_by_id = {assistant.assistant_id: assistant for assistant in updated_assistants}
    return [updated_by_id.get(assistant.assistant_id, assistant) for assistant in assistants], False


def _assistant_runtime_needs_fast_sync(assistant: Any) -> bool:
    if _assistant_is_deleted(assistant):
        return False
    runtime = getattr(assistant, "runtime", None)
    status = str(getattr(runtime, "status", "") or "").strip().lower()
    return status in {"pending", "starting", "restarting"}


def _load_market_for_bootstrap(
    market_service: MarketService,
) -> tuple[list[Any], list[Any], bool]:
    try:
        # Use sync_cos=False for faster response (use cached data from last sync)
        # This prevents blocking on COS operations which can be slow
        return (
            market_service.list_skills(sync_cos=False),
            market_service.list_plugins(sync_cos=False),
            False,
        )
    except Exception as exc:
        log.warning(
            "admin_bootstrap_market_probe_failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        return [], [], True


def _approval_payload(item: Any) -> dict[str, Any]:
    return {
        "id": item.id,
        "kind": item.approval_scope,
        "name": item.tool_name,
        "owner": item.assistant_id or "",
        "assistantId": item.assistant_id or "",
        "assistantProfile": item.capability_profile or "",
        "status": item.status,
        "time": item.requested_at.isoformat(timespec="seconds"),
        "expiresAt": item.expires_at.isoformat(timespec="seconds"),
        "reason": item.reason or "",
        "operatedFile": item.target or "",
        "file": item.target or "",
        "userId": "",
    }


def _approval_payloads(records: Iterable[Any]) -> list[dict[str, Any]]:
    return [_approval_payload(item) for item in records]


def _admin_pagination(page: int, page_size: int, total: int) -> dict[str, Any]:
    normalized_page_size = min(max(1, page_size), ADMIN_LIST_MAX_PAGE_SIZE)
    total_pages = max(1, (total + normalized_page_size - 1) // normalized_page_size)
    normalized_page = min(max(1, page), total_pages)
    return {
        "page": normalized_page,
        "pageSize": normalized_page_size,
        "page_size": normalized_page_size,
        "total": total,
        "totalPages": total_pages,
        "total_pages": total_pages,
        "hasPrevious": normalized_page > 1,
        "has_previous": normalized_page > 1,
        "hasNext": normalized_page < total_pages,
        "has_next": normalized_page < total_pages,
    }


def _paginate_admin_records(
    records: list[Any],
    *,
    page: int,
    page_size: int,
) -> tuple[list[Any], dict[str, Any]]:
    pagination = _admin_pagination(page, page_size, len(records))
    offset = (pagination["page"] - 1) * pagination["page_size"]
    return records[offset : offset + pagination["page_size"]], pagination


def _assistant_matches_filters(
    item: Any,
    *,
    query: str | None,
    status: str | None,
    profile: str | None,
) -> bool:
    if _assistant_is_deleted(item):
        return False
    runtime = getattr(item, "runtime", None)
    if status:
        runtime_status = str(getattr(runtime, "status", "") or "").strip()
        if status in {"pending", "starting"}:
            if runtime_status not in {"pending", "starting"}:
                return False
        elif runtime_status != status:
            return False
    if profile and str(getattr(item, "capability_profile", "") or "").strip() != profile:
        return False
    if not query:
        return True
    needle = query.lower()
    haystack = " ".join(
        str(value or "")
        for value in (
            getattr(item, "assistant_id", ""),
            getattr(item, "name", ""),
            getattr(item, "capability_profile", ""),
            getattr(runtime, "status", ""),
            getattr(runtime, "container_name", ""),
            getattr(runtime, "workspace_root", ""),
            getattr(runtime, "gateway_url", ""),
        )
    ).lower()
    return needle in haystack


def _assistant_is_deleted(item: Any) -> bool:
    return bool(getattr(item, "deleted", False) or getattr(item, "deleted_at", None))


def _active_admin_assistants(assistants: list[Any]) -> list[Any]:
    return [item for item in assistants if not _assistant_is_deleted(item)]


async def _aget_active_admin_assistant(
    service: AssistantService,
    assistant_id: str,
) -> Any:
    assistant = await service.aget_assistant(assistant_id)
    if assistant is None or _assistant_is_deleted(assistant):
        raise HTTPException(status_code=404, detail="assistant not found")
    return assistant


def _filter_admin_assistants(
    assistants: list[Any],
    *,
    query: str | None,
    status: str | None,
    profile: str | None,
) -> list[Any]:
    normalized_query = (query or "").strip() or None
    normalized_status = (status or "").strip() or None
    normalized_profile = (profile or "").strip() or None
    return [
        item
        for item in assistants
        if _assistant_matches_filters(
            item,
            query=normalized_query,
            status=normalized_status,
            profile=normalized_profile,
        )
    ]


def _admin_assistant_payload(
    item: Any,
    *,
    assistant_service: AssistantService | None = None,
    include_workspace_items: bool,
    runtime_probe_degraded: bool,
    polling_source: str,
    partial: bool,
) -> dict[str, Any]:
    runtime = getattr(item, "runtime", None)
    assistant_id = str(getattr(item, "assistant_id", "") or "")
    gateway_url = getattr(runtime, "gateway_url", "") if runtime is not None else ""
    gateway_token = getattr(runtime, "gateway_token", "") if runtime is not None else ""
    installed = []
    if include_workspace_items and assistant_service is not None:
        installed = [
            {
                "id": workspace_item.slug,
                "type": "skill" if workspace_item.kind == "skills" else "plugin",
                "enabled": workspace_item.enabled,
            }
            for workspace_item in assistant_service.list_workspace_items(assistant_id)
        ]
    return {
        "id": assistant_id,
        "name": getattr(item, "name", assistant_id),
        "profile": getattr(item, "capability_profile", ""),
        "status": getattr(runtime, "status", "pending"),
        **_assistant_runtime_freshness_payload(
            runtime,
            runtime_probe_degraded=runtime_probe_degraded,
            polling_source=polling_source,
            partial=partial,
        ),
        "deleted": bool(getattr(item, "deleted_at", None)),
        "deletedAt": item.deleted_at.isoformat(timespec="seconds")
        if getattr(item, "deleted_at", None)
        else "",
        "container": getattr(runtime, "container_name", ""),
        "workspace": getattr(runtime, "workspace_root", ""),
        "gateway": gateway_url,
        "gatewayToken": gateway_token,
        "lastStatusAt": _isoformat_datetime(getattr(runtime, "updated_at", "")),
        "lastError": getattr(runtime, "last_error", ""),
        "assistantUrl": "",
        "gatewayLaunchUrl": "",
        "installed": installed,
    }


def _admin_assistants_page_response(
    *,
    assistants: list[Any],
    page: int,
    page_size: int,
    query: str | None,
    status: str | None,
    profile: str | None,
    assistant_service: AssistantService | None,
    include_workspace_items: bool,
    runtime_probe_degraded: bool,
    polling_source: str,
    partial: bool,
) -> dict[str, Any]:
    filtered = _filter_admin_assistants(
        assistants,
        query=query,
        status=status,
        profile=profile,
    )
    page_items, pagination = _paginate_admin_records(
        filtered,
        page=page,
        page_size=page_size,
    )
    return {
        "assistants": [
            _admin_assistant_payload(
                item,
                assistant_service=assistant_service,
                include_workspace_items=include_workspace_items,
                runtime_probe_degraded=runtime_probe_degraded,
                polling_source=polling_source,
                partial=partial,
            )
            for item in page_items
        ],
        "assistantPagination": pagination,
        "pagination": pagination,
        "runtimeProbeDegraded": runtime_probe_degraded,
        "pollingSource": polling_source,
        "partial": partial,
    }


def _build_bootstrap_sync(
    *,
    probe_runtime: bool = True,
    probe_fast_runtime: bool = True,
    include_workspace_items: bool = True,
    include_market: bool = True,
    include_approvals: bool = True,
    include_users: bool = True,
    include_user_bindings: bool = True,
    include_audit: bool = True,
    include_workspace_paths: bool = True,
    include_workspace_files: bool = False,
    initial_limit: int | None = None,
) -> dict[str, Any]:
    assistant_service = AssistantService()
    approval_service = ApprovalService()
    user_service = UserService()
    market_service = MarketService()
    model_registry = ModelRegistryService()
    audit_repository = build_audit_repository()
    workspace_file_service = WorkspaceFileService()

    if probe_runtime:
        assistants, runtime_probe_degraded = _load_assistants_for_bootstrap(assistant_service)
    else:
        assistants, runtime_probe_degraded = _load_assistants_for_bootstrap_fast(
            assistant_service,
            probe_transient_runtime=probe_fast_runtime,
        )
    assistants = _active_admin_assistants(assistants)
    visible_assistants = assistants[:initial_limit] if initial_limit is not None else assistants

    # Workspace files are COS-authoritative and can be comparatively expensive to
    # list. Fast bootstrap intentionally skips them so it cannot overwrite a full
    # workspace tree with a partial refresh.
    if include_workspace_files:
        all_local_files, workspace_metadata_degraded = _workspace_file_records_for_bootstrap(
            visible_assistants,
            workspace_file_service,
        )
    else:
        all_local_files, workspace_metadata_degraded = [], False

    if include_market:
        skills_catalog, plugins_catalog, market_probe_degraded = _load_market_for_bootstrap(
            market_service
        )
    else:
        skills_catalog, plugins_catalog, market_probe_degraded = [], [], False
    if initial_limit is not None:
        skills_catalog = skills_catalog[:initial_limit]
        plugins_catalog = plugins_catalog[:initial_limit]
    assistant_items = [
        _admin_assistant_payload(
            item,
            assistant_service=assistant_service,
            include_workspace_items=include_workspace_items,
            runtime_probe_degraded=runtime_probe_degraded,
            polling_source="bootstrap_full" if probe_runtime else "bootstrap_fast",
            partial=initial_limit is not None or not probe_runtime,
        )
        for item in visible_assistants
    ]
    assistant_profile_by_id = _build_assistant_profile_index(visible_assistants)

    skills = [
        {
            "id": item.id,
            "slug": item.slug,
            "name": item.name,
            "type": "skill",
            "owner": item.author,
            "status": item.status,
            "file": Path(item.package_path or item.local_archive_path or "").name,
            "time": item.created_at.isoformat(timespec="seconds"),
            "version": item.version,
            "installCount": item.install_count,
        }
        for item in skills_catalog
    ]
    plugins = [
        {
            "id": item.id,
            "slug": item.slug,
            "name": item.name,
            "type": "plugin",
            "owner": item.author,
            "status": item.status,
            "file": Path(item.package_path or item.local_archive_path or "").name,
            "time": item.created_at.isoformat(timespec="seconds"),
            "version": item.version,
            "installCount": item.install_count,
        }
        for item in plugins_catalog
    ]

    approval_probe_degraded = False
    approval_records = []
    if include_approvals:
        try:
            approval_records = sorted(
                approval_service.list_requests(),
                key=lambda item: item.requested_at,
                reverse=True,
            )
        except Exception as exc:
            log.warning(
                "admin_bootstrap_approval_load_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            approval_records = []
            approval_probe_degraded = True
    if initial_limit is not None:
        approval_records = approval_records[:initial_limit]
    approvals = _approval_payloads(approval_records)

    users = []
    user_probe_degraded = False
    user_records = []
    if include_users:
        try:
            user_records = user_service.list_users()
        except Exception as exc:
            log.warning(
                "admin_bootstrap_user_load_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            user_records = []
            user_probe_degraded = True
    if initial_limit is not None:
        user_records = user_records[:initial_limit]
    bindings_by_user: dict[str, list[Any]] = {}
    if include_users and include_user_bindings and user_records:
        try:
            bindings_by_user = user_service.list_bindings_grouped_by_user()
        except Exception as exc:
            log.warning(
                "admin_bootstrap_user_bindings_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            user_probe_degraded = True
    users = _admin_user_payloads(user_records, bindings_by_user)

    workspace_files: list[dict[str, Any]] | None = None
    try:
        visible_assistant_ids = {a.assistant_id for a in visible_assistants}
        raw_workspace_files = [
            item for item in all_local_files if item.assistant_id in visible_assistant_ids
        ]
        if include_workspace_files:
            workspace_files = [
                _workspace_file_payload(
                    item,
                    workspace_file_service=workspace_file_service,
                    include_workspace_paths=include_workspace_paths,
                )
                for item in raw_workspace_files
            ]
            workspace_files = sorted(
                workspace_files,
                key=lambda item: str(item.get("latestUpdatedAt") or ""),
                reverse=True,
            )
    except (OSError, ValueError, SQLAlchemyError) as exc:
        log.warning(
            "admin_bootstrap_workspace_payload_failed",
            degraded=workspace_metadata_degraded,
            error=f"{type(exc).__name__}: {exc}",
        )
        workspace_files = [] if include_workspace_files else None

    audit_probe_degraded = False
    if include_audit:
        try:
            audit_page = audit_repository.list_page(limit=25, offset=0)
        except Exception as exc:
            log.warning(
                "admin_bootstrap_audit_load_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            audit_page = type("AuditPage", (), {"items": [], "total": 0})()
            audit_probe_degraded = True
    else:
        audit_page = type("AuditPage", (), {"items": [], "total": 0})()
    audit_payload = _audit_page_payload(
        page=1,
        page_size=25,
        rows=audit_page.items,
        total=audit_page.total,
        assistant_profile_by_id=assistant_profile_by_id,
    )
    audit_logs = audit_payload["auditLogs"]

    audit_events = []
    for item in approvals[:3]:
        audit_events.append(
            {"text": f"{item['kind']} 审批待处理: {item['name']}", "time": item["time"]}
        )
    for item in audit_logs[:3]:
        audit_events.append(
            {
                "text": f"{item['hook_name']} / {item['tool']} / {item['decision']}",
                "time": item["request_id"],
            }
        )

    payload = {
        "items": [*skills, *plugins],
        "marketIncluded": include_market,
        "partial": initial_limit is not None,
        "pollingSource": "bootstrap_full" if probe_runtime else "bootstrap_fast",
        "approvals": approvals,
        "models": _build_models(model_registry, visible_assistants),
        "assistants": assistant_items,
        "capabilityProfiles": _build_capability_profiles(),
        "users": users,
        "auditEvents": audit_events[:5],
        "auditLogs": audit_logs,
        "auditPagination": audit_payload["auditPagination"],
        "marketProbeDegraded": market_probe_degraded,
        "runtimeProbeDegraded": runtime_probe_degraded,
        "approvalProbeDegraded": approval_probe_degraded,
        "userProbeDegraded": user_probe_degraded,
        "auditProbeDegraded": audit_probe_degraded,
    }
    if workspace_files is not None:
        payload["workspaceFiles"] = workspace_files
    return payload


def _bootstrap_fast_fallback_payload() -> dict[str, Any]:
    audit_payload = _audit_page_payload(
        page=1,
        page_size=25,
        rows=[],
        total=0,
        assistant_profile_by_id={},
    )
    return {
        "items": [],
        "marketIncluded": False,
        "partial": True,
        "pollingSource": "bootstrap_fast",
        "approvals": [],
        "models": [],
        "assistants": [],
        "capabilityProfiles": _build_capability_profiles(),
        "users": [],
        "auditEvents": [],
        "auditLogs": audit_payload["auditLogs"],
        "auditPagination": audit_payload["auditPagination"],
        "marketProbeDegraded": True,
        "runtimeProbeDegraded": True,
        "approvalProbeDegraded": True,
        "userProbeDegraded": True,
        "auditProbeDegraded": True,
        "bootstrapFastFallback": True,
    }


async def _build_fast_bootstrap_cached(app: FastAPI) -> dict[str, Any]:
    now = time.monotonic()
    ttl_seconds = _bootstrap_fast_cache_ttl_seconds()
    cache = getattr(app.state, "bootstrap_fast_cache", None)
    if cache and cache.get("payload") is not None and cache.get("expires_at", 0.0) > now:
        return _with_cache_metadata(
            cache["payload"],
            cache_status="fresh",
            ttl_seconds=max(0.0, cache.get("expires_at", now) - now),
        )

    lock = getattr(app.state, "bootstrap_fast_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app.state.bootstrap_fast_lock = lock

    async with lock:
        now = time.monotonic()
        cache = getattr(app.state, "bootstrap_fast_cache", None)
        if cache and cache.get("payload") is not None and cache.get("expires_at", 0.0) > now:
            return _with_cache_metadata(
                cache["payload"],
                cache_status="fresh",
                ttl_seconds=max(0.0, cache.get("expires_at", now) - now),
            )

        loop = asyncio.get_running_loop()
        build_payload = partial(
            _build_bootstrap_sync,
            probe_runtime=False,
            probe_fast_runtime=False,
            include_workspace_items=False,
            include_market=False,
            include_approvals=False,
            include_users=False,
            include_user_bindings=False,
            include_audit=False,
            include_workspace_paths=False,
            include_workspace_files=False,
            initial_limit=25,
        )
        try:
            payload = await asyncio.wait_for(
                loop.run_in_executor(BOOTSTRAP_FAST_EXECUTOR, build_payload),
                timeout=BOOTSTRAP_FAST_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            log.warning("admin_bootstrap_fast_timeout")
            if cache and cache.get("payload") is not None:
                payload = _with_cache_metadata(
                    cache["payload"],
                    cache_status="stale",
                    ttl_seconds=0.0,
                )
                payload["bootstrapFastCacheStale"] = True
                return payload
            return _with_cache_metadata(
                _bootstrap_fast_fallback_payload(),
                cache_status="fallback",
                ttl_seconds=0.0,
            )

        app.state.bootstrap_fast_cache = {
            "payload": payload,
            "expires_at": time.monotonic() + ttl_seconds,
        }
        return _with_cache_metadata(
            payload,
            cache_status="fresh",
            ttl_seconds=ttl_seconds,
        )


def _admin_user_payloads(
    user_records: list[Any],
    bindings_by_user: dict[str, list[Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "id": user.user_id,
            "name": user.display_name or user.user_id,
            "department": user.department,
            "role": user.role,
            "assistants": [
                binding.assistant_id for binding in bindings_by_user.get(user.user_id, [])
            ],
        }
        for user in user_records
    ]


def _build_admin_users_payload_sync() -> dict[str, Any]:
    user_service = UserService()
    try:
        user_records = user_service.list_users()
        bindings_by_user = user_service.list_bindings_grouped_by_user()
    except Exception as exc:
        log.warning(
            "admin_users_load_failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        return {"users": [], "userProbeDegraded": True}
    return {
        "users": _admin_user_payloads(user_records, bindings_by_user),
        "userProbeDegraded": False,
    }


def create_admin_web_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="IronAgent Admin Web", version="0.1.0")
    app.state.bootstrap_fast_cache = {"payload": None, "expires_at": 0.0}
    app.state.bootstrap_fast_lock = asyncio.Lock()
    app.state.assistant_status_cache = {"payload": None, "expires_at": 0.0}
    app.state.assistant_status_lock = asyncio.Lock()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_resolve_control_ui_allowed_origins(settings),
        allow_origin_regex=settings.control_ui_allowed_origin_regex,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(assistants_router)
    app.include_router(market_router)
    app.include_router(users_router)
    app.include_router(workspace_files_router)

    static_dir = Path(__file__).with_name("static")
    static_index = static_dir / "index.html"
    app.mount("/static", StaticFiles(directory=static_dir), name="admin-static")

    @app.middleware("http")
    async def local_admin_session_middleware(request: Request, call_next):
        header_names = {
            header.lower().encode("latin-1")
            for header in INTERNAL_SERVICE_TOKEN_HEADERS
        }
        has_admin_header = any(
            name.lower() in header_names
            for name, _ in request.scope.get("headers", [])
        )
        session_value = request.cookies.get(ADMIN_DIRECT_SESSION_COOKIE, "") or request.cookies.get(
            LEGACY_ADMIN_DIRECT_SESSION_COOKIE, ""
        )
        if (
            not has_admin_header
            and _is_loopback_admin_request(request)
            and _valid_admin_direct_session(
                session_value,
                settings.internal_service_token,
            )
        ):
            MutableHeaders(scope=request.scope)[INTERNAL_SERVICE_TOKEN_HEADER] = (
                settings.internal_service_token
            )
        return await call_next(request)

    @app.on_event("startup")
    async def start_assistant_runtime_health_loop() -> None:
        if getattr(app.state, "assistant_runtime_health_task", None) is None:
            app.state.assistant_runtime_health_task = asyncio.create_task(
                _assistant_runtime_health_loop(app)
            )
        if (
            settings.workspace_sync_job_worker_enabled
            and getattr(app.state, "workspace_sync_job_worker_task", None) is None
        ):
            app.state.workspace_sync_job_worker_task = asyncio.create_task(
                _workspace_sync_job_worker_loop(app)
            )

    @app.on_event("shutdown")
    async def stop_assistant_runtime_health_loop() -> None:
        task = getattr(app.state, "assistant_runtime_health_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        app.state.assistant_runtime_health_task = None
        task = getattr(app.state, "workspace_sync_job_worker_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        app.state.workspace_sync_job_worker_task = None
        _shutdown_admin_runtime_task_executor(app)
        await dispose_database_engines()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        payload, status_code = await asyncio.to_thread(_admin_readiness_payload)
        return JSONResponse(payload, status_code=status_code)

    @app.get("/api/v1/admin/assistants/status/stream")
    async def stream_assistant_status(
        _admin: None = Depends(require_internal_service_token),
    ) -> StreamingResponse:
        return StreamingResponse(
            _assistant_status_event_stream(app),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/v1/admin/assistants/status")
    async def get_assistant_status(
        refresh: bool = False,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        return await _get_assistant_status_snapshot_cached(app, force_refresh=refresh)

    @app.get("/api/v1/admin/bootstrap")
    async def admin_bootstrap(
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        return await asyncio.to_thread(_build_bootstrap_sync)

    @app.get("/api/v1/admin/bootstrap/fast")
    async def admin_bootstrap_fast(
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        return await _build_fast_bootstrap_cached(app)

    @app.get("/api/v1/admin/approvals")
    async def admin_approvals(
        status: str | None = Query(default=None),
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        normalized_status = (status or "").strip() or None
        records = await asyncio.to_thread(
            ApprovalService().list_requests,
            status=normalized_status,
        )
        records = sorted(records, key=lambda item: item.requested_at, reverse=True)
        return {
            "approvals": _approval_payloads(records),
            "approvalProbeDegraded": False,
        }

    @app.get("/api/v1/admin/assistants")
    async def list_admin_assistants(
        request: Request,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=ADMIN_LIST_DEFAULT_PAGE_SIZE, ge=1),
        query: str | None = Query(default=None),
        status: str | None = Query(default=None),
        profile: str | None = Query(default=None),
        sync: bool = Query(default=False),
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        _validate_admin_list_page_size(page_size)
        assistant_service = AssistantService()
        if sync:
            assistants, runtime_probe_degraded = await _run_admin_runtime_task(
                request.app,
                _load_assistants_for_bootstrap,
                assistant_service,
            )
        else:
            assistants = await asyncio.to_thread(assistant_service.list_assistants)
            runtime_probe_degraded = False
        return _admin_assistants_page_response(
            assistants=assistants,
            page=page,
            page_size=page_size,
            query=query,
            status=status,
            profile=profile,
            assistant_service=None,
            include_workspace_items=False,
            runtime_probe_degraded=runtime_probe_degraded,
            polling_source="assistants_page",
            partial=False,
        )

    @app.get("/api/v1/admin/assistants/{assistant_id}/launch-url")
    async def admin_assistant_launch_url(
        request: Request,
        assistant_id: str,
        fresh: bool = Query(False),
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, str | None]:
        service = AssistantService()
        record = await asyncio.to_thread(service.get_assistant, assistant_id)
        if record is None or _assistant_is_deleted(record):
            raise HTTPException(status_code=404, detail="assistant not found")

        gateway_launch_url = _gateway_launch_url(
            assistant_id=record.assistant_id,
            gateway_url=record.runtime.gateway_url,
            gateway_token=record.runtime.gateway_token,
            fresh_session=fresh,
            admin_origin_url=str(request.base_url),
        )
        if not gateway_launch_url:
            raise HTTPException(status_code=409, detail="assistant gateway is not available")
        return {
            "assistantUrl": gateway_launch_url,
            "gatewayLaunchUrl": gateway_launch_url,
            "fallbackAssistantUrl": "",
        }

    @app.get("/api/v1/admin/audit-logs")
    async def admin_audit_logs(
        page: int = Query(1, ge=1),
        page_size: int = Query(25, ge=1, le=100),
        start_time: str | None = None,
        end_time: str | None = None,
        user_id: str | None = None,
        assistant_id: str | None = None,
        operation_type: str | None = None,
        result: str | None = None,
        error_code: str | None = None,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        filters = _audit_filters_from_query(
            start_time=start_time,
            end_time=end_time,
            user_id=user_id,
            assistant_id=assistant_id,
            operation_type=operation_type,
            result=result,
            error_code=error_code,
        )
        return await asyncio.to_thread(
            _build_audit_page_sync,
            page=page,
            page_size=page_size,
            filters=filters,
        )

    @app.get("/api/v1/admin/audit-logs/export")
    async def export_admin_audit_logs(
        format: str = Query("csv", pattern="^(csv|json)$"),
        page: int = Query(1, ge=1),
        page_size: int = Query(
            AUDIT_EXPORT_DEFAULT_PAGE_SIZE,
            ge=1,
            le=AUDIT_EXPORT_MAX_PAGE_SIZE,
        ),
        start_time: str | None = None,
        end_time: str | None = None,
        user_id: str | None = None,
        assistant_id: str | None = None,
        operation_type: str | None = None,
        result: str | None = None,
        error_code: str | None = None,
        _admin: None = Depends(require_internal_service_token),
    ) -> Response:
        filters = _audit_filters_from_query(
            start_time=start_time,
            end_time=end_time,
            user_id=user_id,
            assistant_id=assistant_id,
            operation_type=operation_type,
            result=result,
            error_code=error_code,
        )

        def _build_response() -> Response:
            assistant_profile_by_id = _build_assistant_profile_index(
                AssistantService().list_assistants()
            )
            normalized_page = max(1, page)
            normalized_page_size = min(max(1, page_size), AUDIT_EXPORT_MAX_PAGE_SIZE)
            offset = (normalized_page - 1) * normalized_page_size
            audit_repository = build_audit_repository()
            audit_page = audit_repository.list_page(
                limit=normalized_page_size,
                offset=offset,
                filters=filters,
            )
            total_pages = max(
                1,
                (audit_page.total + normalized_page_size - 1) // normalized_page_size,
            )
            if normalized_page > total_pages:
                normalized_page = total_pages
                offset = (normalized_page - 1) * normalized_page_size
                audit_page = audit_repository.list_page(
                    limit=normalized_page_size,
                    offset=offset,
                    filters=filters,
                )
            response = _audit_export_response(
                rows=audit_page.items,
                export_format=format,
                assistant_profile_by_id=assistant_profile_by_id,
            )
            return _with_audit_export_pagination_headers(
                response,
                page=normalized_page,
                page_size=normalized_page_size,
                total=audit_page.total,
                item_count=len(audit_page.items),
            )

        return await asyncio.to_thread(_build_response)

    @app.post("/api/v1/admin/audit-logs/retention/cleanup")
    async def cleanup_admin_audit_logs(
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        settings = get_settings()
        result = await asyncio.to_thread(
            build_audit_repository().cleanup_expired,
            retention_days=settings.audit_retention_days,
            archive_path=settings.audit_archive_path,
        )
        return {
            "retention_days": result.retention_days,
            "cutoff": result.cutoff.isoformat(),
            "archived": result.archived,
            "deleted": result.deleted,
            "remaining": result.remaining,
            "archive_path": str(settings.audit_archive_path),
        }

    @app.post("/api/v1/admin/capabilities")
    async def create_or_update_capability(
        payload: dict[str, Any],
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        settings = get_settings()
        capability_dir = settings.config_dir / "capabilities"
        capability_dir.mkdir(parents=True, exist_ok=True)

        name = payload.get("id") or payload.get("name")
        if not name:
            raise HTTPException(status_code=400, detail="id or name required")

        safe_name = _slugify(name)
        if not safe_name:
            raise HTTPException(status_code=400, detail="invalid id")

        data = {
            "name": payload.get("name", safe_name),
            "level": payload.get("level", 0),
            "allowed_tools": payload.get("allowed_tools", []),
            "approval_tools": payload.get("approval_tools", []),
        }
        if "description" in payload:
            data["description"] = payload["description"]
        if "demo_focus" in payload:
            data["demo_focus"] = payload["demo_focus"]

        path = capability_dir / f"{safe_name}.yaml"
        # preserve other custom keys if file exists
        if path.exists():
            existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for k, v in existing.items():
                if k not in data:
                    data[k] = v

        path.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        loop = asyncio.get_running_loop()
        try:
            refreshed, warnings = await asyncio.wait_for(
                loop.run_in_executor(
                    CAPABILITY_REFRESH_EXECUTOR,
                    _refresh_assistants_for_capability_profile,
                    safe_name,
                ),
                timeout=CAPABILITY_REFRESH_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            refreshed = 0
            warnings = ["capability assistant refresh timed out"]
        return {
            "id": safe_name,
            "status": "success",
            "refreshed_assistants": refreshed,
            "warnings": warnings,
        }

    @app.delete("/api/v1/admin/capabilities/{profile_id}")
    async def delete_capability(
        profile_id: str,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        settings = get_settings()
        capability_dir = settings.config_dir / "capabilities"

        safe_name = _slugify(profile_id)
        if not safe_name:
            raise HTTPException(status_code=400, detail="invalid profile_id")

        path = capability_dir / f"{safe_name}.yaml"
        if not path.exists():
            raise HTTPException(status_code=404, detail="capability profile not found")

        await asyncio.to_thread(path.unlink, missing_ok=True)
        return {"id": safe_name, "status": "deleted"}

    @app.post("/api/v1/admin/approvals/{approval_id}/resolve")
    async def resolve_approval(
        approval_id: str,
        payload: ApprovalResolutionRequest,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        service = ApprovalService()
        workspace_file_service = WorkspaceFileService()
        audit_repository = build_audit_repository()
        if payload.resolution not in {"allow", "deny"}:
            raise HTTPException(status_code=400, detail="invalid resolution")
        item = await asyncio.to_thread(service.resolve_request, approval_id, payload.resolution)
        if item is None:
            raise HTTPException(status_code=404, detail="approval not found")

        apply_status = "not_applicable"
        warnings: list[str] = []
        try:
            applied = await asyncio.to_thread(
                workspace_file_service.apply_approval_resolution,
                approval_id,
            )
            if applied is not None:
                apply_status = "applied"
        except Exception as exc:
            apply_status = "failed"
            message = f"{type(exc).__name__}: {exc}"
            warnings.append(f"workspace apply failed: {message}")
            log.warning(
                "approval_resolution_workspace_apply_failed",
                approval_id=approval_id,
                resolution=payload.resolution,
                error=message,
            )

        try:
            await asyncio.to_thread(
                audit_repository.append_event,
                _build_approval_audit_event(
                    approval_id=approval_id,
                    resolution=payload.resolution,
                    approval=item,
                ),
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            warnings.append(f"audit append failed: {message}")
            log.warning(
                "approval_resolution_audit_append_failed",
                approval_id=approval_id,
                resolution=payload.resolution,
                error=message,
            )

        return {
            "id": item.id,
            "status": item.status,
            "apply_status": apply_status,
            "warnings": warnings,
        }

    @app.post("/api/v1/admin/assistants")
    async def create_assistant(
        payload: AdminAssistantCreateRequest,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        service = AssistantService()
        assistant_id, workspace_slug = _resolve_assistant_identity(
            assistant_id=payload.assistant_id,
            workspace_slug=payload.workspace_slug,
            name=payload.name,
        )
        user_id = payload.user_id.strip()
        if not user_id:
            log.warning(
                "创建助手失败，缺少用户标识",
                name=payload.name,
                assistant_id=assistant_id,
            )
            raise HTTPException(status_code=400, detail="user_id is required")
        log.info(
            "创建助手开始",
            assistant_id=assistant_id,
            name=payload.name,
            user_id=user_id,
            capability_profile=payload.capability_profile,
            workspace_slug=workspace_slug,
        )

        existing_assistants = await asyncio.to_thread(service.list_assistants)
        normalized_name = _assistant_name_key(payload.name)
        for existing in existing_assistants:
            if existing.assistant_id == assistant_id:
                log.warning(
                    "创建助手失败，助手ID已存在",
                    assistant_id=assistant_id,
                    user_id=user_id,
                )
                raise HTTPException(
                    status_code=409,
                    detail=f"assistant id already exists: {assistant_id}",
                )
            if _assistant_name_key(existing.name) == normalized_name:
                log.warning(
                    "创建助手失败，助手名称已存在",
                    name=payload.name.strip(),
                    assistant_id=assistant_id,
                    user_id=user_id,
                )
                raise HTTPException(
                    status_code=409,
                    detail=f"assistant name already exists: {payload.name.strip()}",
                )

        user_service = UserService()
        if await asyncio.to_thread(user_service.get_user, user_id) is None:
            log.warning(
                "创建助手失败，用户不存在",
                assistant_id=assistant_id,
                user_id=user_id,
            )
            raise HTTPException(status_code=404, detail=f"user not found: {user_id}")

        model_registry = ModelRegistryService()
        workspace_sync_service = WorkspaceSyncService(assistant_service=service)
        if payload.model_id.strip():
            model_config = model_registry.resolve_model_config(payload.model_id.strip())
            if model_config is None:
                await asyncio.to_thread(model_registry.import_from_assistants)
                model_config = model_registry.resolve_model_config(payload.model_id.strip())
            if model_config is None:
                log.warning(
                    "创建助手失败，模型不存在",
                    assistant_id=assistant_id,
                    user_id=user_id,
                    model_id=payload.model_id.strip(),
                )
                raise HTTPException(status_code=404, detail="model not found")
        else:
            auth_mode = "host_iron_agent"
            provider_id = ""
            provider_base_url = ""
            provider_api_key = ""
            primary_model = payload.primary_model.strip()
            if (
                payload.provider_id.strip()
                or payload.provider_base_url.strip()
                or payload.provider_api_key.strip()
            ):
                auth_mode = "provider_api_key"
                provider_id = payload.provider_id.strip()
                provider_base_url = payload.provider_base_url.strip()
                provider_api_key = payload.provider_api_key.strip()
                # A brand-new assistant has no stored secret to fall back to, so
                # the masked placeholder can never be a real key here.
                if provider_api_key == MASKED_API_KEY:
                    raise HTTPException(
                        status_code=400,
                        detail="provider_api_key cannot be the masked placeholder",
                    )
            model_config = AssistantIronAgentConfig(
                primary_model=primary_model or AssistantIronAgentConfig().primary_model,
                auth_mode=auth_mode,
                provider_id=provider_id,
                provider_base_url=provider_base_url,
                provider_api_key=provider_api_key,
            )
        workspace_config = AssistantWorkspaceConfig(
            workspace_slug=workspace_slug,
        )
        try:
            record = await asyncio.to_thread(
                service.upsert_assistant,
                assistant_id=assistant_id,
                name=payload.name,
                capability_profile=_resolve_capability_profile(payload.capability_profile),
                notes=payload.notes,
                model_config=model_config,
                workspace_config=workspace_config,
            )
        except (ValidationError, ValueError) as exc:
            log.warning(
                "创建助手失败，助手记录保存异常",
                assistant_id=assistant_id,
                user_id=user_id,
                error=str(exc),
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log.info(
            "创建助手记录完成",
            assistant_id=record.assistant_id,
            user_id=user_id,
            status=record.runtime.status,
        )
        await asyncio.to_thread(_sync_runtime_control_ui_allowed_origins, record, settings)
        try:
            await workspace_sync_service.async_sync_assistant_workspace(record.assistant_id)
        except Exception as exc:
            log.warning(
                "助手工作空间初始同步失败",
                assistant_id=record.assistant_id,
                user_id=user_id,
                error=str(exc),
            )
        response: dict[str, Any] = {
            "assistant_id": record.assistant_id,
            "status": record.runtime.status,
        }
        try:
            binding = await asyncio.to_thread(
                user_service.bind_assistant,
                user_id,
                record.assistant_id,
                alias=payload.name.strip() or record.assistant_id,
                source_system="admin-assistant-create",
            )
        except ValueError as exc:
            log.warning(
                "创建助手绑定用户失败",
                assistant_id=record.assistant_id,
                user_id=user_id,
                error=str(exc),
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log.info(
            "创建助手绑定用户完成",
            assistant_id=record.assistant_id,
            user_id=user_id,
            source_system="admin-assistant-create",
        )
        response["binding"] = {
            "user_id": binding.user_id,
            "assistant_id": binding.assistant_id,
        }
        log.info(
            "创建助手完成",
            assistant_id=record.assistant_id,
            user_id=user_id,
            status=record.runtime.status,
        )
        return response

    @app.put("/api/v1/admin/assistants/{assistant_id}/model-config")
    async def update_assistant_model_config(
        assistant_id: str,
        payload: AdminAssistantModelConfigRequest,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        service = AssistantService()
        existing = await asyncio.to_thread(service.get_assistant, assistant_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="assistant not found")

        if payload.model_id.strip():
            model_registry = ModelRegistryService()
            model_config = await asyncio.to_thread(
                model_registry.resolve_model_config,
                payload.model_id.strip(),
            )
            if model_config is None:
                await asyncio.to_thread(
                    model_registry.import_from_assistants,
                    await asyncio.to_thread(service.list_assistants),
                )
                model_config = await asyncio.to_thread(
                    model_registry.resolve_model_config,
                    payload.model_id.strip(),
                )
            if model_config is None:
                raise HTTPException(status_code=404, detail="model not found")
        else:
            primary_model = payload.primary_model.strip()
            if (
                payload.provider_id.strip()
                or payload.provider_base_url.strip()
                or payload.provider_api_key.strip()
            ):
                # Explicit provider fields → provider_api_key mode. A masked
                # placeholder echoed back from the UI keeps the stored secret
                # (handled in _merge_model_config).
                model_config = AssistantIronAgentConfig(
                    primary_model=primary_model or existing.iron_agent_config.primary_model,
                    auth_mode="provider_api_key",
                    provider_id=payload.provider_id.strip(),
                    provider_base_url=payload.provider_base_url.strip(),
                    provider_api_key=payload.provider_api_key.strip(),
                )
            else:
                # No provider fields supplied: this is a primary-model-only
                # update, so preserve the existing auth mode and provider
                # credentials instead of silently resetting to host auth.
                model_config = existing.iron_agent_config.model_copy(
                    update={
                        "primary_model": primary_model
                        or existing.iron_agent_config.primary_model
                    }
                )

        try:
            record = await asyncio.to_thread(
                service.upsert_assistant,
                assistant_id=existing.assistant_id,
                name=existing.name,
                capability_profile=existing.capability_profile,
                notes=existing.notes,
                model_config=model_config,
                workspace_config=existing.workspace_config,
                provision_runtime=payload.provision_runtime,
            )
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return {
            "assistant_id": record.assistant_id,
            "status": record.runtime.status,
            "provision_runtime": payload.provision_runtime,
            "model_config": _build_assistant_model_config_payload(record.iron_agent_config),
        }

    @app.post("/api/v1/admin/assistants/{assistant_id}/repair")
    async def repair_assistant(
        assistant_id: str,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        service = AssistantService()
        record = await asyncio.to_thread(service.repair_runtime_state, assistant_id)
        if record is None:
            raise HTTPException(status_code=404, detail="assistant not found")
        return {"assistant_id": record.assistant_id, "status": record.runtime.status}

    @app.post("/api/v1/admin/assistants/repair-all")
    async def repair_all_assistants(
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        service = AssistantService()
        repaired, skipped = await asyncio.to_thread(service.repair_out_of_sync_runtimes)
        return {"repaired": repaired, "skipped": skipped}

    @app.post("/api/v1/admin/assistants/{assistant_id}/restart")
    async def restart_assistant(
        request: Request,
        assistant_id: str,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        service = AssistantService()
        existing = await asyncio.to_thread(service.get_assistant, assistant_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="assistant not found")
        record = await asyncio.to_thread(service.mark_assistant_restarting, assistant_id)
        if record is None:
            raise HTTPException(status_code=404, detail="assistant not found")
        warnings = await _append_assistant_lifecycle_audit(
            request=request,
            assistant_id=record.assistant_id,
            action="assistant_restart",
            previous_status=existing.runtime.status,
            next_status=record.runtime.status,
        )
        _track_admin_background_task(
            request.app,
            asyncio.create_task(_restart_assistant_in_background(request.app, record.assistant_id)),
        )
        return {
            "assistant_id": record.assistant_id,
            "status": record.runtime.status,
            "accepted": True,
            "warnings": warnings,
        }

    @app.post("/api/v1/admin/assistants/{assistant_id}/start")
    async def start_assistant(
        request: Request,
        assistant_id: str,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        service = AssistantService()
        existing = await asyncio.to_thread(service.get_assistant, assistant_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="assistant not found")
        record = await asyncio.to_thread(service.start_assistant, assistant_id)
        if record is None:
            raise HTTPException(status_code=404, detail="assistant not found")
        warnings = await _append_assistant_lifecycle_audit(
            request=request,
            assistant_id=record.assistant_id,
            action="assistant_start",
            previous_status=existing.runtime.status,
            next_status=record.runtime.status,
        )
        return {
            "assistant_id": record.assistant_id,
            "status": record.runtime.status,
            "warnings": warnings,
        }

    @app.post("/api/v1/admin/assistants/{assistant_id}/stop")
    async def stop_assistant(
        request: Request,
        assistant_id: str,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        service = AssistantService()
        existing = await asyncio.to_thread(service.get_assistant, assistant_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="assistant not found")
        record = await asyncio.to_thread(service.stop_assistant, assistant_id)
        if record is None:
            raise HTTPException(status_code=404, detail="assistant not found")
        warnings = await _append_assistant_lifecycle_audit(
            request=request,
            assistant_id=record.assistant_id,
            action="assistant_stop",
            previous_status=existing.runtime.status,
            next_status=record.runtime.status,
        )
        return {
            "assistant_id": record.assistant_id,
            "status": record.runtime.status,
            "warnings": warnings,
        }

    @app.post("/api/v1/admin/assistants/{assistant_id}/pause")
    async def pause_assistant(
        assistant_id: str,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        service = AssistantService()
        record = await asyncio.to_thread(service.pause_assistant, assistant_id)
        if record is None:
            raise HTTPException(status_code=404, detail="assistant not found")
        return {"assistant_id": record.assistant_id, "status": record.runtime.status}

    @app.delete("/api/v1/admin/assistants/{assistant_id}")
    async def delete_assistant(
        request: Request,
        assistant_id: str,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        service = AssistantService()
        existing = await asyncio.to_thread(service.get_assistant, assistant_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="assistant not found")
        result = await asyncio.to_thread(service.delete_assistant, assistant_id)
        if result is None:
            raise HTTPException(status_code=404, detail="assistant not found")
        warnings = await _append_assistant_lifecycle_audit(
            request=request,
            assistant_id=result.assistant_id,
            action="assistant_delete",
            previous_status=existing.runtime.status,
            next_status="deleted",
        )
        return {
            "assistant_id": result.assistant_id,
            "status": "deleted",
            "deleted": result.soft_deleted,
            "deleted_at": result.deleted_at.isoformat(timespec="seconds"),
            "runtime_disabled": result.runtime_disabled,
            "container_removed": result.container_removed,
            "workspace_removed": result.workspace_removed,
            "state_removed": result.state_removed,
            "warnings": warnings,
        }

    @app.get("/api/v1/admin/users")
    async def list_admin_users(
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        return await asyncio.to_thread(_build_admin_users_payload_sync)

    @app.post("/api/v1/admin/users")
    async def create_user(
        payload: AdminUserUpsertRequest,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        service = UserService()
        user_id = payload.user_id or _slugify(payload.display_name)
        try:
            user = await asyncio.to_thread(
                service.create_user,
                user_id,
                display_name=payload.display_name,
                employee_id=payload.employee_id,
                department=payload.department,
                role=payload.role,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"user_id": user.user_id}

    @app.put("/api/v1/admin/users/{user_id}")
    async def update_user(
        user_id: str,
        payload: AdminUserUpsertRequest,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        service = UserService()
        user = await asyncio.to_thread(
            service.update_user,
            user_id,
            display_name=payload.display_name,
            employee_id=payload.employee_id,
            department=payload.department,
            role=payload.role,
        )
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        return {"user_id": user.user_id}

    @app.delete("/api/v1/admin/users/{user_id}")
    async def delete_user(
        user_id: str,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        service = UserService()
        deleted = await asyncio.to_thread(service.delete_user, user_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="user not found")
        return {"status": "deleted"}

    @app.post("/api/v1/admin/users/{user_id}/assistants/bind")
    async def bind_user_assistant(
        user_id: str,
        payload: AdminBindAssistantRequest,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        service = UserService()
        try:
            binding = await asyncio.to_thread(
                service.bind_assistant,
                user_id,
                payload.assistant_id,
                alias=payload.alias,
                source_system=payload.source_system,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"user_id": binding.user_id, "assistant_id": binding.assistant_id}

    @app.delete("/api/v1/admin/users/{user_id}/assistants/{assistant_id}")
    async def unbind_user_assistant(
        user_id: str,
        assistant_id: str,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        service = UserService()
        deleted = await asyncio.to_thread(service.unbind_assistant, user_id, assistant_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="binding not found")
        return {"status": "deleted"}

    @app.get("/api/v1/admin/assistants/{assistant_id}/workspace-items")
    async def list_workspace_items(
        assistant_id: str,
        force: bool = Query(default=False),
        kind: str | None = Query(default=None),
        slug: str | None = Query(default=None),
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        assistant_service = AssistantService()
        await _aget_active_admin_assistant(assistant_service, assistant_id)
        item_index = importlib.import_module("src.workspace_files.item_index")
        filters = item_index.WorkspaceItemIndexFilters(
            assistant_id=assistant_id,
            kind=kind,
            slug=slug,
            page=1,
            page_size=200,
        )
        index_result = await asyncio.to_thread(
            item_index.list_workspace_item_index_page,
            filters,
            workspace_file_repository=_workspace_admin_index_repository(),
        )
        return {
            "items": [
                _workspace_item_summary_payload(item)
                for item in _workspace_item_index_records(index_result)
            ],
            "indexStatus": _workspace_item_index_status(index_result),
            "compat": {
                "force_requested": bool(force),
                "force_scan_disabled": bool(force),
                "refresh_required": bool(force),
            },
        }

    @app.post("/api/v1/admin/assistants/{assistant_id}/workspace-items/install")
    async def install_workspace_item(
        assistant_id: str,
        request: Request,
        kind: str = Form(...),
        record_id: str = Form(""),
        slug: str = Form(""),
        display_name: str = Form(""),
        owner: str = Form(""),
        version: str = Form(""),
        file: UploadFile | None = OPTIONAL_CAPABILITY_UPLOAD_FILE,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        assistant_service = AssistantService()
        await _aget_active_admin_assistant(assistant_service, assistant_id)
        installer = AssistantCapabilityInstallService(
            assistant_service=assistant_service,
            workspace_item_index_store=_workspace_item_index_store(),
        )
        try:
            if record_id.strip():
                result = await installer.ainstall_market_record(
                    assistant_id=assistant_id,
                    kind=kind,
                    record_id=record_id,
                )
            else:
                if file is None:
                    raise ValueError("file is required")
                content = await file.read()
                if not content:
                    raise ValueError("file is empty")
                result = await installer.ainstall_uploaded_package(
                    assistant_id=assistant_id,
                    kind=kind,
                    filename=file.filename or "",
                    content=content,
                    slug=slug,
                    display_name=display_name,
                    owner=owner,
                    version=version,
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        warnings = await _append_capability_audit(
            request=request,
            assistant_id=assistant_id,
            kind=result.kind,
            slug=result.slug,
            version=result.version,
            action=f"capability_{result.action}",
            runtime_reload_status=result.runtime_reload_status,
            runtime_reload_error=result.runtime_reload_error,
        )
        return _workspace_item_install_payload(result, warnings=warnings)

    @app.get("/api/v1/admin/assistants/{assistant_id}/workspace-files/summary")
    async def list_assistant_workspace_files_summary(
        assistant_id: str,
        sync: bool = Query(default=False),
        bucket: str | None = Query(default=None),
        source_type: str | None = Query(default=None),
        sync_status: str | None = Query(default=None),
        updated_after: str | None = Query(default=None),
        query: str | None = Query(default=None),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1),
        view: str = Query(default="flat"),
        parent: str | None = Query(default=None),
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        _validate_workspace_summary_page_size(page_size)
        if view not in {"flat", "tree"}:
            raise HTTPException(status_code=400, detail="view must be flat or tree")
        assistant_service = AssistantService()
        await _aget_active_admin_assistant(assistant_service, assistant_id)
        await _sync_workspace_summaries_if_requested(
            sync=sync,
            assistant_ids=[assistant_id],
            assistant_service=assistant_service,
        )
        index_result = await _query_workspace_admin_index(
            assistant_id=assistant_id,
            assistant_ids=[assistant_id],
            bucket=bucket,
            source_type=source_type,
            sync_status=sync_status,
            updated_after=updated_after,
            query=query,
            page=page,
            page_size=page_size,
            view=view,
            parent=parent,
        )
        return _workspace_summary_response(
            index_result,
            page=page,
            page_size=page_size,
            sync=sync,
            assistant_id=assistant_id,
        )

    @app.get("/api/v1/admin/workspace-folders/summary")
    async def list_workspace_folders_summary(
        sync: bool = Query(default=False),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=WORKSPACE_FOLDER_DEFAULT_PAGE_SIZE, ge=1),
        query: str | None = Query(default=None),
        status: str | None = Query(default=None),
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        _validate_admin_list_page_size(page_size)
        assistant_service = AssistantService()
        assistants = await asyncio.to_thread(assistant_service.list_assistants)
        assistants = _active_admin_assistants(assistants)
        filtered_assistants = _filter_admin_assistants(
            assistants,
            query=query,
            status=status,
            profile=None,
        )
        page_assistants, pagination = _paginate_admin_records(
            filtered_assistants,
            page=page,
            page_size=page_size,
        )
        assistant_ids = [
            str(getattr(assistant, "assistant_id", "")).strip() for assistant in page_assistants
        ]
        assistant_ids = [assistant_id for assistant_id in assistant_ids if assistant_id]
        await _sync_workspace_summaries_if_requested(
            sync=sync,
            assistant_ids=assistant_ids,
            assistant_service=assistant_service,
        )
        folder_result = await _query_workspace_folder_summaries(assistant_ids=assistant_ids)
        return _workspace_folders_response(
            folder_result,
            assistants=page_assistants,
            sync=sync,
            pagination=pagination,
        )

    @app.get("/api/v1/admin/workspace-files/summary")
    async def list_workspace_files_summary(
        sync: bool = Query(default=False),
        bucket: str | None = Query(default=None),
        source_type: str | None = Query(default=None),
        sync_status: str | None = Query(default=None),
        updated_after: str | None = Query(default=None),
        query: str | None = Query(default=None),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1),
        view: str = Query(default="flat"),
        parent: str | None = Query(default=None),
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        _validate_workspace_summary_page_size(page_size)
        if view not in {"flat", "tree"}:
            raise HTTPException(status_code=400, detail="view must be flat or tree")
        assistant_service = AssistantService()
        assistants = await asyncio.to_thread(assistant_service.list_assistants)
        assistants = _active_admin_assistants(assistants)
        assistant_ids = [
            str(getattr(assistant, "assistant_id", "")).strip() for assistant in assistants
        ]
        assistant_ids = [assistant_id for assistant_id in assistant_ids if assistant_id]
        await _sync_workspace_summaries_if_requested(
            sync=sync,
            assistant_ids=assistant_ids,
            assistant_service=assistant_service,
        )
        index_result = await _query_workspace_admin_index(
            assistant_ids=assistant_ids,
            bucket=bucket,
            source_type=source_type,
            sync_status=sync_status,
            updated_after=updated_after,
            query=query,
            page=page,
            page_size=page_size,
            view=view,
            parent=parent,
        )
        return _workspace_summary_response(
            index_result,
            page=page,
            page_size=page_size,
            sync=sync,
            assistant_ids=assistant_ids,
        )

    @app.post("/api/v1/admin/workspace-sync-jobs")
    async def create_workspace_sync_job(
        payload: AdminWorkspaceSyncJobCreateRequest,
        request: Request,
        response: Response,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        assistant_id = (payload.assistant_id or "").strip()
        scope_kind = payload.scope_kind.strip()
        scope_slug = (payload.scope_slug or "").strip()
        mode = payload.mode.strip()
        priority = payload.priority.strip() or "normal"
        if not assistant_id:
            raise HTTPException(status_code=400, detail="assistant_id is required")
        if scope_kind not in {"assistant", "capability", "bucket"}:
            raise HTTPException(status_code=400, detail="invalid scope_kind")
        if scope_kind == "assistant" and scope_slug:
            raise HTTPException(
                status_code=400, detail="assistant scope does not accept scope_slug"
            )
        if scope_kind != "assistant" and not scope_slug:
            raise HTTPException(status_code=400, detail="scope_slug is required")
        if mode != "index_only":
            raise HTTPException(status_code=400, detail="unsupported mode")
        if priority not in {"low", "normal", "high"}:
            raise HTTPException(status_code=400, detail="invalid priority")
        assistant_service = AssistantService()
        await _aget_active_admin_assistant(assistant_service, assistant_id)
        normalized_payload = payload.model_copy(
            update={
                "assistant_id": assistant_id,
                "scope_kind": scope_kind,
                "scope_slug": scope_slug,
                "mode": mode,
                "priority": priority,
            }
        )
        job, reused = await _create_workspace_sync_job(normalized_payload, request)
        job_payload = _sync_job_payload(job)
        terminal_statuses = {"succeeded", "failed", "cancelled"}
        job_status = str(job_payload.get("status", "") or "").strip().lower()
        response.status_code = 200 if reused or job_status in terminal_statuses else 202
        job_payload.setdefault(
            "indexStatus",
            {
                "source": "db",
                "sync_status": job_payload.get("status", "unknown"),
            },
        )
        index_result = await _query_workspace_admin_index(
            assistant_id=assistant_id,
            assistant_ids=[assistant_id],
            page=1,
            page_size=50,
        )
        snapshot = _workspace_summary_response(
            index_result,
            page=1,
            page_size=50,
            sync=False,
            assistant_id=assistant_id,
        )
        job_payload["workspaceFiles"] = snapshot["workspaceFiles"]
        job_payload["pagination"] = snapshot["pagination"]
        job_payload["assistantSummary"] = snapshot["assistantSummary"]
        pending_job_id = (
            job_payload.get("job_id", "") if job_status not in terminal_statuses else ""
        )
        job_payload["indexStatus"] = {
            **snapshot["indexStatus"],
            "sync_job_id": job_payload.get("job_id", ""),
            "pending_job_id": pending_job_id,
            "sync_status": job_payload.get(
                "status", snapshot["indexStatus"].get("sync_status", "unknown")
            ),
        }
        return job_payload

    @app.get("/api/v1/admin/workspace-sync-jobs/{job_id}")
    async def get_workspace_sync_job(
        job_id: str,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        job = await _get_workspace_sync_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="workspace sync job not found")
        return _sync_job_payload(job)

    @app.post("/api/v1/admin/assistants/{assistant_id}/workspace-items/toggle")
    async def toggle_workspace_item(
        request: Request,
        assistant_id: str,
        payload: WorkspaceItemToggleRequest,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        service = AssistantService()
        await _aget_active_admin_assistant(service, assistant_id)
        if payload.kind not in {"skills", "plugins"}:
            raise HTTPException(status_code=400, detail="invalid kind")
        try:
            item = await asyncio.to_thread(
                service.set_workspace_item_enabled,
                assistant_id=assistant_id,
                kind=payload.kind,
                slug=payload.slug,
                enabled=payload.enabled,
            )
            reload_result = await service.areload_workspace_capability(
                assistant_id=assistant_id,
                kind=payload.kind,  # type: ignore[arg-type]
                slug=item.slug,
            )
            await record_workspace_item_toggle(
                store=_workspace_item_index_store(),
                assistant_id=assistant_id,
                kind=payload.kind,  # type: ignore[arg-type]
                slug=item.slug,
                enabled=payload.enabled,
                runtime_reload_status=reload_result.status,  # type: ignore[arg-type]
                runtime_reload_error=reload_result.error,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        action = "capability_enable" if payload.enabled else "capability_disable"
        warnings = await _append_capability_audit(
            request=request,
            assistant_id=assistant_id,
            kind=payload.kind,
            slug=item.slug,
            version=item.version,
            action=action,
            runtime_reload_status=reload_result.status,
            runtime_reload_error=reload_result.error,
        )
        return {
            "slug": item.slug,
            "enabled": item.enabled,
            "version": item.version,
            "runtime_reload_status": reload_result.status,
            "runtime_reload_error": reload_result.error,
            "runtime_effective": reload_result.status != "failed",
            "action": action,
            "warnings": warnings,
        }

    @app.post("/api/v1/admin/assistants/{assistant_id}/workspace-items/reuse")
    async def reuse_workspace_item(
        assistant_id: str,
        payload: WorkspaceItemReuseRequest,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        service = AssistantService()
        await _aget_active_admin_assistant(service, assistant_id)
        await _aget_active_admin_assistant(service, payload.target_assistant_id)
        if payload.kind not in {"skills", "plugins"}:
            raise HTTPException(status_code=400, detail="invalid kind")
        try:
            item = await asyncio.to_thread(
                service.reuse_workspace_item,
                source_assistant_id=assistant_id,
                target_assistant_id=payload.target_assistant_id,
                kind=payload.kind,
                slug=payload.slug,
            )
            await WorkspaceSyncService(
                assistant_service=service,
            ).async_sync_assistant_workspace_scopes(
                item.target_assistant_id,
                [(item.kind, item.slug)],
            )
            reload_result = await service.areload_workspace_capability(
                assistant_id=item.target_assistant_id,
                kind=item.kind,
                slug=item.slug,
            )
            await record_workspace_item_reused(
                store=_workspace_item_index_store(),
                source_assistant_id=assistant_id,
                target_assistant_id=item.target_assistant_id,
                kind=item.kind,
                slug=item.slug,
                install_path=getattr(item, "install_path", ""),
                runtime_reload_status=reload_result.status,  # type: ignore[arg-type]
                runtime_reload_error=reload_result.error,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "slug": item.slug,
            "target_assistant_id": item.target_assistant_id,
            "runtime_reload_status": reload_result.status,
            "runtime_reload_error": reload_result.error,
        }

    @app.delete("/api/v1/admin/assistants/{assistant_id}/workspace-items")
    async def delete_workspace_item(
        assistant_id: str,
        kind: str,
        slug: str,
        request: Request,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        service = AssistantService()
        if kind not in {"skills", "plugins"}:
            raise HTTPException(status_code=400, detail="invalid kind")
        await _aget_active_admin_assistant(service, assistant_id)

        try:
            existing_items = await asyncio.to_thread(service.list_workspace_items, assistant_id)
            existing_item = next(
                (item for item in existing_items if item.kind == kind and item.slug == slug),
                None,
            )
            removed = await asyncio.to_thread(
                service.remove_workspace_item,
                assistant_id=assistant_id,
                kind=kind,  # type: ignore[arg-type]
                slug=slug,
            )
            workspace_records_deleted = await asyncio.to_thread(
                _delete_workspace_item_package_records,
                assistant_id=assistant_id,
                kind=kind,
                slug=removed.slug,
            )
            reload_result = await service.areload_workspace_capability(
                assistant_id=assistant_id,
                kind=kind,  # type: ignore[arg-type]
                slug=removed.slug,
            )
            await record_workspace_item_removed(
                store=_workspace_item_index_store(),
                assistant_id=assistant_id,
                kind=kind,  # type: ignore[arg-type]
                slug=removed.slug,
                runtime_reload_status=reload_result.status,  # type: ignore[arg-type]
                runtime_reload_error=reload_result.error,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        warnings = await _append_capability_audit(
            request=request,
            assistant_id=assistant_id,
            kind=kind,
            slug=removed.slug,
            version=existing_item.version if existing_item is not None else "",
            action="capability_uninstall",
            runtime_reload_status=reload_result.status,
            runtime_reload_error=reload_result.error,
        )

        return {
            "slug": removed.slug,
            "removed": True,
            "cleanupScheduled": False,
            "alreadyAbsent": False,
            "workspaceFilesDeleted": workspace_records_deleted,
            "runtime_reload_status": reload_result.status,
            "runtime_reload_error": reload_result.error,
            "runtime_effective": reload_result.status != "failed",
            "action": "capability_uninstall",
            "warnings": warnings,
        }

    @app.get("/api/v1/admin/models")
    async def list_models(
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        service = ModelRegistryService()
        assistant_service = AssistantService()
        assistants = await asyncio.to_thread(assistant_service.list_assistants)
        items = [
            _build_model_payload(item, service, assistants)
            for item in await asyncio.to_thread(service.import_from_assistants, assistants)
        ]
        return {"items": items}

    @app.get("/api/v1/admin/models/{model_id}")
    async def get_model(
        model_id: str,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        service = ModelRegistryService()
        assistant_service = AssistantService()
        assistants = await asyncio.to_thread(assistant_service.list_assistants)
        await asyncio.to_thread(service.import_from_assistants, assistants)
        item = await asyncio.to_thread(service.get_model, model_id)
        if item is None:
            raise HTTPException(status_code=404, detail="model not found")
        return _build_model_payload(item, service, assistants)

    @app.post("/api/v1/admin/models")
    async def create_model(
        payload: AdminModelUpdateRequest,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        service = ModelRegistryService()
        try:
            item = await asyncio.to_thread(
                service.create_model,
                name=payload.name,
                provider=payload.provider,
                url=payload.url,
                api_key=payload.apiKey,
                status=payload.status,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": item.model_id}

    @app.put("/api/v1/admin/models/{model_id}")
    async def update_model(
        model_id: str,
        payload: AdminModelUpdateRequest,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        service = ModelRegistryService()
        item = await asyncio.to_thread(
            service.update_model,
            model_id,
            name=payload.name,
            provider=payload.provider,
            url=payload.url,
            api_key=payload.apiKey,
            status=payload.status,
        )
        if item is None:
            raise HTTPException(status_code=404, detail="model not found")
        return {"id": item.model_id}

    @app.delete("/api/v1/admin/models/{model_id}")
    async def delete_model(
        model_id: str,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        service = ModelRegistryService()
        assistant_service = AssistantService()
        item = await asyncio.to_thread(service.get_model, model_id)
        if item is None:
            raise HTTPException(status_code=404, detail="model not found")
        assistants = await asyncio.to_thread(assistant_service.list_assistants)
        assistant_ids = service.assistant_ids_for_model(item, assistants)
        if assistant_ids:
            raise HTTPException(
                status_code=400,
                detail=f"model is still used by assistants: {', '.join(assistant_ids)}",
            )
        deleted = await asyncio.to_thread(service.delete_model, model_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="model not found")
        return {"status": "deleted"}

    @app.post("/api/v1/admin/models/{model_id}/probe")
    async def probe_model(
        model_id: str,
        _admin: None = Depends(require_internal_service_token),
    ) -> dict[str, Any]:
        service = ModelRegistryService()
        result = await asyncio.to_thread(service.probe_model, model_id)
        if result is None:
            raise HTTPException(status_code=404, detail="model not found")
        item = await asyncio.to_thread(service.get_model, model_id)
        if (
            item is not None
            and result.get("ok")
            and item.provider == "host_iron_agent"
            and item.name.strip().startswith("openai-codex/")
        ):
            assistant_service = AssistantService()
            assistants = await asyncio.to_thread(assistant_service.list_assistants)
            assistant_ids = service.assistant_ids_for_model(item, assistants)
            if assistant_ids:
                _, probes, _ = await asyncio.to_thread(
                    assistant_service.sync_runtime_views,
                    assistants,
                )
                for assistant_id in assistant_ids:
                    probe = probes.get(assistant_id)
                    message = _runtime_auth_failure_message(
                        str(getattr(probe, "last_error", "") or "")
                    )
                    if message:
                        return {
                            **result,
                            "ok": False,
                            "status": "auth_failed",
                            "message": message,
                            "assistant_ids": assistant_ids,
                        }
        return result

    @app.get("/login")
    async def login(request: Request, next: str = "/") -> RedirectResponse:
        if not _is_loopback_admin_request(request):
            raise HTTPException(status_code=401, detail="admin login is not available")
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        response = RedirectResponse(target, status_code=303)
        _set_admin_direct_session_cookies(response, settings.internal_service_token)
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        if not static_index.exists():
            raise HTTPException(status_code=500, detail="admin index not found")
        response = HTMLResponse(static_index.read_text(encoding="utf-8"))
        if _is_loopback_admin_request(request):
            _set_admin_direct_session_cookies(response, settings.internal_service_token)
        return response

    return app


app = create_admin_web_app()
