"""API routes for workspace file push, upload, sync, download, and delete."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from src.shared.auth import require_internal_service_token
from src.shared.logging import get_logger
from src.shared.workspace_paths import runtime_workspace_path
from src.workspace_files.models import TargetBucket
from src.workspace_files.service import (
    FileSecurityError,
    WorkspaceFileService,
    WorkspaceScanSummary,
)
from src.workspace_files.sync_service import WorkspaceSyncService

if TYPE_CHECKING:
    from src.approval.service import ApprovalService

router = APIRouter(tags=["workspace-files"])
log = get_logger(__name__)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def _get_service() -> WorkspaceFileService:
    return WorkspaceFileService()


def _get_approval_service() -> ApprovalService:
    from src.approval.service import ApprovalService

    return ApprovalService()


def _resolve_approval_scope(result: Any) -> Literal["user", "admin"] | None:
    """Resolve approval_scope from ExternalPushRequest by querying the approval record."""
    from src.workspace_files.models import ExternalPushRequest

    if not isinstance(result, ExternalPushRequest):
        return None
    if not result.approval_id:
        return None
    approval_service = _get_approval_service()
    approval = approval_service.get_request(result.approval_id)
    if approval is None:
        return None
    return approval.approval_scope


class FileResponse(BaseModel):
    file_id: str
    assistant_id: str
    bucket: TargetBucket
    filename: str
    relative_path: str
    direction: str
    source_file_id: str | None = None
    owner_user_id: str
    size_bytes: int
    content_type: str
    uploader_user_id: str
    source_type: str
    source_ref: str
    origin: str
    storage_provider: str
    storage_key: str
    storage_path: str
    storage_status: str
    storage_error: str
    storage_backend: str
    storage_url: str
    object_key: str
    object_url: str
    checksum_sha256: str
    sync_status: str
    sync_error: str
    file_role: str
    workspace_path: str
    workspace_relative_path: str
    runtime_workspace_path: str
    workspace_present: bool
    workspace_mtime: datetime | None = None
    workspace_size: int | None = None
    object_storage_status: str
    status: str
    download_url: str
    workspace_last_modified_at: datetime | None = None
    last_synced_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    deleted: bool


class FileListResponse(BaseModel):
    files: list[FileResponse]
    total: int


class PushResponse(BaseModel):
    status: Literal["accepted", "pending_approval", "rejected"]
    file_id: str | None = None
    push_id: str | None = None
    approval_id: str | None = None
    approval_scope: Literal["user", "admin"] | None = None
    assistant_id: str
    filename: str
    bucket: TargetBucket
    sync_status: str = ""
    storage_backend: str = ""
    storage_key: str = ""
    storage_url: str = ""
    message: str = ""


class FileEventResponse(BaseModel):
    event_id: str
    file_id: str
    assistant_id: str
    action: str
    user_id: str
    source_type: str
    source_ref: str
    detail: dict[str, Any]
    created_at: datetime


class DeleteResponse(BaseModel):
    status: Literal["deleted", "pending_approval"]
    file_id: str
    approval_id: str | None = None
    approval_scope: Literal["user", "admin"] | None = None
    message: str = ""


class WorkspaceScanResponse(BaseModel):
    assistants_scanned: int
    files_scanned: int
    files_synced: int
    files_deleted: int
    errors: list[str]


DownloadDisposition = Literal["inline", "attachment"]


def _frontend_status(record) -> str:
    if record.storage_status == "disabled":
        return "workspace_only"
    if record.storage_status == "failed" or record.sync_status == "failed":
        return "storage_failed"
    if record.storage_status in {"synced", "stored"} or record.sync_status in {
        "synced",
        "synced_to_workspace",
    }:
        return "ready"
    return "pending"


def _api_sync_status(record) -> str:
    if record.sync_status == "synced_to_workspace":
        return "synced"
    return record.sync_status


def _download_url(file_id: str) -> str:
    return f"/api/v1/workspace-files/{file_id}/download"


def _parse_changed_after_query(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    if "." in raw:
        prefix, suffix = raw.split(".", 1)
        tz_start = len(suffix)
        for marker in ("+", "-"):
            marker_index = suffix.find(marker)
            if marker_index > 0:
                tz_start = min(tz_start, marker_index)
        fraction = suffix[:tz_start]
        timezone_suffix = suffix[tz_start:]
        raw = prefix + "." + fraction[:6].ljust(min(len(fraction), 6), "0") + timezone_suffix
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _content_disposition_header(filename: str, disposition: DownloadDisposition) -> str:
    sanitized = filename.replace('"', "")
    ascii_fallback = sanitized.encode("ascii", "ignore").decode("ascii").strip() or "download"
    encoded = quote(sanitized, safe="")
    return f"{disposition}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


def _file_payload(
    record,
    *,
    svc: WorkspaceFileService,
    workspace_root_cache: dict[str, Path] | None = None,
) -> dict[str, Any]:
    workspace_relative_path = (
        (record.relative_path or f"{record.bucket}/{record.filename}")
        .replace("\\", "/")
        .lstrip("/")
    )
    workspace_path = runtime_workspace_path(workspace_relative_path)
    workspace_cache = workspace_root_cache if workspace_root_cache is not None else {}
    workspace_file_path = _record_workspace_file_path(
        record,
        svc=svc,
        workspace_root_cache=workspace_cache,
    )
    workspace_stat = (
        workspace_file_path.stat()
        if workspace_file_path is not None and workspace_file_path.is_file()
        else None
    )
    return {
        "file_id": record.file_id,
        "assistant_id": record.assistant_id,
        "bucket": record.bucket,
        "filename": record.filename,
        "relative_path": record.relative_path,
        "direction": record.direction,
        "source_file_id": record.source_file_id,
        "owner_user_id": record.owner_user_id,
        "size_bytes": record.size_bytes,
        "content_type": record.content_type,
        "uploader_user_id": record.uploader_user_id,
        "source_type": record.source_type,
        "source_ref": record.source_ref,
        "origin": record.origin,
        "storage_provider": record.storage_provider,
        "storage_key": record.storage_key or record.object_key,
        "storage_path": record.storage_path,
        "storage_status": record.storage_status,
        "storage_error": record.storage_error,
        "storage_backend": record.storage_backend,
        "storage_url": record.storage_url or record.object_url,
        "object_key": record.object_key,
        "object_url": record.object_url,
        "checksum_sha256": record.checksum_sha256,
        "sync_status": _api_sync_status(record),
        "sync_error": record.sync_error,
        "file_role": record.file_role,
        "workspace_path": workspace_path,
        "workspace_relative_path": workspace_relative_path,
        "runtime_workspace_path": workspace_path,
        "workspace_present": workspace_stat is not None,
        "workspace_mtime": (
            datetime.fromtimestamp(workspace_stat.st_mtime, tz=UTC)
            if workspace_stat is not None
            else None
        ),
        "workspace_size": workspace_stat.st_size if workspace_stat is not None else None,
        "object_storage_status": record.storage_status or record.sync_status,
        "status": _frontend_status(record),
        "download_url": _download_url(record.file_id),
        "workspace_last_modified_at": record.workspace_last_modified_at,
        "last_synced_at": record.last_synced_at,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "deleted": record.deleted,
    }


def _record_workspace_file_path(
    record,
    *,
    svc: WorkspaceFileService,
    workspace_root_cache: dict[str, Path],
) -> Path | None:
    if record.bucket not in {"persistent", "temp", "skills", "plugins"}:
        return None

    workspace_root = workspace_root_cache.get(record.assistant_id)
    if workspace_root is None:
        workspace_root = svc._workspace_dir(record.assistant_id).resolve()
        workspace_root_cache[record.assistant_id] = workspace_root

    relative_path = (
        (record.relative_path or f"{record.bucket}/{record.filename}")
        .replace("\\", "/")
        .lstrip("/")
    )
    expected_prefix = f"{record.bucket}/"
    if relative_path == record.bucket:
        return None
    if not relative_path.startswith(expected_prefix):
        relative_path = f"{record.bucket}/{record.filename}".replace("\\", "/").lstrip("/")

    candidate = (workspace_root / relative_path).resolve()
    try:
        candidate.relative_to(workspace_root)
    except ValueError:
        return None
    return candidate


def _is_live_workspace_file(
    record,
    *,
    svc: WorkspaceFileService,
    workspace_root_cache: dict[str, Path],
) -> bool:
    path = _record_workspace_file_path(
        record,
        svc=svc,
        workspace_root_cache=workspace_root_cache,
    )
    return path is not None and path.is_file()


def _to_push_response(
    result: Any,
    *,
    assistant_id: str,
    filename: str,
    bucket: TargetBucket,
    success_message: str,
    approval_scope: Literal["user", "admin"] | None = None,
) -> PushResponse:
    from src.workspace_files.models import ExternalPushRequest

    if isinstance(result, ExternalPushRequest):
        return PushResponse(
            status=result.status,
            push_id=result.push_id,
            approval_id=result.approval_id,
            approval_scope=approval_scope,
            assistant_id=result.assistant_id,
            filename=result.filename,
            bucket=result.bucket,
            message="operation requires approval",
        )
    return PushResponse(
        status="accepted",
        file_id=result.file_id,
        assistant_id=assistant_id,
        filename=result.filename,
        bucket=bucket,
        sync_status=_api_sync_status(result),
        storage_backend=result.storage_backend,
        storage_key=result.storage_key or result.object_key,
        storage_url=result.storage_url or result.object_url,
        message=success_message,
    )


@router.post(
    "/api/v1/workspace-files/upload",
    response_model=PushResponse,
    dependencies=[Depends(require_internal_service_token)],
)
def upload_file(
    file: UploadFile = File(...),
    assistant_id: str | None = Form(default=None),
    user_id: str | None = Form(default=None),
    target_bucket: TargetBucket = Form(default="temp"),
    overwrite: bool = Form(default=False),
    target_path: str = Form(default=""),
    source_type: str = Form(default=""),
    source_ref: str = Form(default=""),
    owner_user_id: str = Form(default=""),
    uploader_user_id: str = Form(default=""),
    svc: WorkspaceFileService = Depends(_get_service),
) -> PushResponse:
    filename = file.filename or "unnamed"
    log.info(
        "工作空间文件上传开始",
        assistant_id=assistant_id,
        user_id=user_id,
        filename=filename,
        bucket=target_bucket,
        overwrite=overwrite,
        source_type=source_type,
        source_ref=source_ref,
    )
    try:
        content = file.file.read()
        if len(content) > MAX_UPLOAD_BYTES:
            log.warning(
                "工作空间文件上传失败",
                assistant_id=assistant_id,
                user_id=user_id,
                filename=filename,
                bucket=target_bucket,
                reason="file_too_large",
                size_bytes=len(content),
                max_bytes=MAX_UPLOAD_BYTES,
            )
            raise HTTPException(
                status_code=413, detail=f"file too large; max {MAX_UPLOAD_BYTES} bytes"
            )
        result = svc.upload_file(
            assistant_id=assistant_id,
            user_id=user_id,
            bucket=target_bucket,
            filename=filename,
            content=content,
            content_type=file.content_type or "",
            overwrite=overwrite,
            target_path=target_path,
            source_type=source_type,
            source_ref=source_ref,
            owner_user_id=owner_user_id,
            uploader_user_id=uploader_user_id,
        )
    except FileSecurityError as exc:
        log.warning(
            "工作空间文件上传失败",
            assistant_id=assistant_id,
            user_id=user_id,
            filename=filename,
            bucket=target_bucket,
            reason="security_error",
            error=str(exc),
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        log.warning(
            "工作空间文件上传失败",
            assistant_id=assistant_id,
            user_id=user_id,
            filename=filename,
            bucket=target_bucket,
            reason="validation_error",
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    resolved_assistant_id = (
        result.assistant_id if hasattr(result, "assistant_id") else (assistant_id or "")
    )
    response = _to_push_response(
        result,
        assistant_id=resolved_assistant_id,
        filename=filename,
        bucket=target_bucket,
        success_message="file uploaded successfully",
        approval_scope=_resolve_approval_scope(result),
    )
    log.info(
        "工作空间文件上传完成",
        assistant_id=response.assistant_id,
        user_id=user_id,
        file_id=response.file_id,
        push_id=response.push_id,
        approval_id=response.approval_id,
        status=response.status,
        filename=response.filename,
        bucket=response.bucket,
        storage_backend=response.storage_backend,
        sync_status=response.sync_status,
    )
    return response


@router.post(
    "/api/v1/workspace-files/push",
    response_model=PushResponse,
    dependencies=[Depends(require_internal_service_token)],
)
def push_file(
    file: UploadFile = File(...),
    assistant_id: str | None = Form(default=None),
    user_id: str | None = Form(default=None),
    target_bucket: TargetBucket = Form(default="temp"),
    overwrite: bool = Form(default=False),
    source_type: str = Form(default=""),
    source_ref: str = Form(default=""),
    svc: WorkspaceFileService = Depends(_get_service),
) -> PushResponse:
    filename = file.filename or "unnamed"
    log.info(
        "工作空间文件推送开始",
        assistant_id=assistant_id,
        user_id=user_id,
        filename=filename,
        bucket=target_bucket,
        overwrite=overwrite,
        source_type=source_type,
        source_ref=source_ref,
    )
    try:
        content = file.file.read()
        if len(content) > MAX_UPLOAD_BYTES:
            log.warning(
                "工作空间文件推送失败",
                assistant_id=assistant_id,
                user_id=user_id,
                filename=filename,
                bucket=target_bucket,
                reason="file_too_large",
                size_bytes=len(content),
                max_bytes=MAX_UPLOAD_BYTES,
            )
            raise HTTPException(
                status_code=413, detail=f"file too large; max {MAX_UPLOAD_BYTES} bytes"
            )
        result = svc.upload_file(
            assistant_id=assistant_id,
            user_id=user_id,
            bucket=target_bucket,
            filename=filename,
            content=content,
            content_type=file.content_type or "",
            overwrite=overwrite,
            source_type=source_type,
            source_ref=source_ref,
            owner_user_id=user_id or "",
            uploader_user_id=user_id or "",
        )
    except FileSecurityError as exc:
        log.warning(
            "工作空间文件推送失败",
            assistant_id=assistant_id,
            user_id=user_id,
            filename=filename,
            bucket=target_bucket,
            reason="security_error",
            error=str(exc),
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        log.warning(
            "工作空间文件推送失败",
            assistant_id=assistant_id,
            user_id=user_id,
            filename=filename,
            bucket=target_bucket,
            reason="validation_error",
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    resolved_assistant_id = (
        result.assistant_id if hasattr(result, "assistant_id") else (assistant_id or "")
    )
    response = _to_push_response(
        result,
        assistant_id=resolved_assistant_id,
        filename=filename,
        bucket=target_bucket,
        success_message="file pushed successfully",
        approval_scope=_resolve_approval_scope(result),
    )
    log.info(
        "工作空间文件推送完成",
        assistant_id=response.assistant_id,
        user_id=user_id,
        file_id=response.file_id,
        push_id=response.push_id,
        approval_id=response.approval_id,
        status=response.status,
        filename=response.filename,
        bucket=response.bucket,
        storage_backend=response.storage_backend,
        sync_status=response.sync_status,
    )
    return response


@router.post(
    "/api/v1/workspace-files/sync-results",
    response_model=WorkspaceScanResponse,
    dependencies=[Depends(require_internal_service_token)],
)
def sync_workspace_results(
    request: Request,
    assistant_id: str | None = Query(default=None),
    bucket: TargetBucket | None = Query(default=None),
    user_id: str | None = Query(default=None),
    changed_after: datetime | None = Query(default=None),
    result_path_prefix: str | None = None,
    task_id: str | None = None,
    source_ref: str | None = None,
    svc: WorkspaceFileService = Depends(_get_service),
) -> WorkspaceScanResponse:
    resolved_user_id = (user_id or request.query_params.get("user_id") or "").strip()
    resolved_changed_after = changed_after or _parse_changed_after_query(
        request.query_params.get("changed_after")
    )
    resolved_result_path_prefix = (
        result_path_prefix or request.query_params.get("result_path_prefix") or ""
    ).strip()
    resolved_task_id = (
        task_id or request.query_params.get("task_id") or request.query_params.get("taskId") or ""
    ).strip()
    explicit_source_ref = (source_ref or request.query_params.get("source_ref") or "").strip()
    resolved_source_ref = (explicit_source_ref or resolved_task_id).strip()
    if not resolved_result_path_prefix and (
        resolved_task_id or explicit_source_ref or resolved_user_id
    ):
        log.warning(
            "工作空间结果同步请求无效",
            reason="missing_result_path_prefix",
            assistant_id=assistant_id,
            bucket=bucket,
            user_id=resolved_user_id,
            task_id=resolved_task_id,
            source_ref=resolved_source_ref,
        )
        raise HTTPException(
            status_code=400,
            detail="result_path_prefix is required for task-scoped result sync",
        )
    log.info(
        "工作空间结果同步开始",
        assistant_id=assistant_id,
        bucket=bucket,
        user_id=resolved_user_id,
        changed_after=resolved_changed_after,
        result_path_prefix=resolved_result_path_prefix,
        task_id=resolved_task_id,
        source_ref=resolved_source_ref,
    )
    try:
        summary = svc.scan_workspace_results(
            assistant_id=assistant_id,
            bucket=bucket,
            user_id=resolved_user_id,
            changed_after=resolved_changed_after,
            result_path_prefix=resolved_result_path_prefix,
            result_source_ref=resolved_source_ref,
        )
    except FileSecurityError as exc:
        log.warning(
            "工作空间结果同步失败",
            reason="file_security_error",
            assistant_id=assistant_id,
            bucket=bucket,
            user_id=resolved_user_id,
            result_path_prefix=resolved_result_path_prefix,
            task_id=resolved_task_id,
            source_ref=resolved_source_ref,
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception(
            "工作空间结果同步失败",
            reason="unexpected_error",
            assistant_id=assistant_id,
            bucket=bucket,
            user_id=resolved_user_id,
            result_path_prefix=resolved_result_path_prefix,
            task_id=resolved_task_id,
            source_ref=resolved_source_ref,
            error=str(exc),
        )
        raise
    response = WorkspaceScanResponse(**_scan_summary_payload(summary))
    log.info(
        "工作空间结果同步完成",
        assistant_id=assistant_id,
        bucket=bucket,
        assistants_scanned=response.assistants_scanned,
        files_scanned=response.files_scanned,
        files_synced=response.files_synced,
        files_deleted=response.files_deleted,
        result_path_prefix=resolved_result_path_prefix,
        task_id=resolved_task_id,
        source_ref=resolved_source_ref,
        errors=len(response.errors),
    )
    return response


@router.get(
    "/api/v1/workspace-files",
    response_model=FileListResponse,
    dependencies=[Depends(require_internal_service_token)],
)
def list_files(
    assistant_id: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    bucket: TargetBucket | None = Query(default=None),
    direction: str | None = Query(default=None),
    file_role: str | None = Query(default=None),
    sync: bool = Query(default=False),
    svc: WorkspaceFileService = Depends(_get_service),
) -> FileListResponse:
    log.info(
        "工作空间文件列表查询开始",
        assistant_id=assistant_id,
        user_id=user_id,
        bucket=bucket,
        direction=direction,
        file_role=file_role,
        sync=sync,
    )
    if assistant_id is not None:
        if sync:
            try:
                WorkspaceSyncService().sync_assistant_workspace(assistant_id)
            except Exception as exc:
                log.warning(
                    "工作空间文件列表同步刷新失败",
                    assistant_id=assistant_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
        records = svc.list_files(
            assistant_id,
            bucket=bucket,
            direction=direction,
            file_role=file_role,
        )
    elif user_id is not None:
        records = svc.list_files_for_user(
            user_id,
            bucket=bucket,
            direction=direction,
            file_role=file_role,
        )
    else:
        log.warning("工作空间文件列表查询失败", reason="missing_assistant_or_user")
        raise HTTPException(status_code=400, detail="assistant_id or user_id is required")
    workspace_root_cache: dict[str, Path] = {}
    live_records = [
        item
        for item in records
        if _is_live_workspace_file(item, svc=svc, workspace_root_cache=workspace_root_cache)
    ]
    items = [
        FileResponse(**_file_payload(item, svc=svc, workspace_root_cache=workspace_root_cache))
        for item in live_records
    ]
    response = FileListResponse(files=items, total=len(items))
    log.info(
        "工作空间文件列表查询完成",
        assistant_id=assistant_id,
        user_id=user_id,
        bucket=bucket,
        total=response.total,
    )
    return response


@router.get("/api/v1/users/{user_id}/workspace-files", response_model=FileListResponse)
def list_user_files(
    user_id: str,
    bucket: TargetBucket | None = Query(default=None),
    svc: WorkspaceFileService = Depends(_get_service),
) -> FileListResponse:
    log.info("用户工作空间文件列表查询开始", user_id=user_id, bucket=bucket)
    workspace_root_cache: dict[str, Path] = {}
    live_records = [
        item
        for item in svc.list_files_for_user(user_id, bucket=bucket)
        if _is_live_workspace_file(item, svc=svc, workspace_root_cache=workspace_root_cache)
    ]
    items = [
        FileResponse(**_file_payload(item, svc=svc, workspace_root_cache=workspace_root_cache))
        for item in live_records
    ]
    response = FileListResponse(files=items, total=len(items))
    log.info("用户工作空间文件列表查询完成", user_id=user_id, bucket=bucket, total=response.total)
    return response


@router.get(
    "/api/v1/workspace-files/overview",
    dependencies=[Depends(require_internal_service_token)],
)
def overview(
    assistant_id: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    svc: WorkspaceFileService = Depends(_get_service),
) -> dict[str, Any]:
    log.info("工作空间文件概览查询开始", assistant_id=assistant_id, user_id=user_id)
    if assistant_id is not None:
        files = svc.list_files(assistant_id)
        resolved_assistant_id = assistant_id
    elif user_id is not None:
        files = svc.list_files_for_user(user_id)
        bindings = svc.user_svc.resolve_assistant_ids(user_id)
        resolved_assistant_id = bindings[0] if bindings else ""
    else:
        log.warning("工作空间文件概览查询失败", reason="missing_assistant_or_user")
        raise HTTPException(status_code=400, detail="assistant_id or user_id is required")
    input_files = [item for item in files if item.file_role == "input"]
    result_files = [item for item in files if item.file_role == "result"]
    latest_results = sorted(result_files, key=lambda item: item.updated_at, reverse=True)[:5]
    response = {
        "assistant_id": resolved_assistant_id,
        "total": len(files),
        "input_count": len(input_files),
        "result_count": len(result_files),
        "latest_results": [_file_payload(item, svc=svc) for item in latest_results],
    }
    log.info(
        "工作空间文件概览查询完成",
        assistant_id=resolved_assistant_id,
        user_id=user_id,
        total=response["total"],
        input_count=response["input_count"],
        result_count=response["result_count"],
    )
    return response


@router.get(
    "/api/v1/workspace-files/{file_id}",
    response_model=FileResponse,
    dependencies=[Depends(require_internal_service_token)],
)
def get_file(
    file_id: str,
    svc: WorkspaceFileService = Depends(_get_service),
) -> FileResponse:
    log.info("工作空间文件元信息查询开始", file_id=file_id)
    record = svc.get_file(file_id)
    if record is None or record.deleted:
        log.warning("工作空间文件元信息查询失败", file_id=file_id, reason="file_not_found")
        raise HTTPException(status_code=404, detail="file not found")
    response = FileResponse(**_file_payload(record, svc=svc))
    log.info(
        "工作空间文件元信息查询完成",
        file_id=response.file_id,
        assistant_id=response.assistant_id,
        filename=response.filename,
        bucket=response.bucket,
        size_bytes=response.size_bytes,
        content_type=response.content_type,
    )
    return response


@router.get(
    "/api/v1/workspace-files/{file_id}/download",
    dependencies=[Depends(require_internal_service_token)],
)
def download_file(
    file_id: str,
    disposition: DownloadDisposition = Query(default="attachment"),
    svc: WorkspaceFileService = Depends(_get_service),
) -> Response:
    log.info("工作空间文件下载开始", file_id=file_id, disposition=disposition)
    record = svc.get_file(file_id)
    if record is None or record.deleted:
        log.warning("工作空间文件下载失败", file_id=file_id, reason="file_not_found")
        raise HTTPException(status_code=404, detail="file not found")
    result = svc.download_file(file_id)
    if result is None:
        log.warning("工作空间文件下载失败", file_id=file_id, reason="content_not_found")
        raise HTTPException(status_code=404, detail="content_not_found")
    record = result.record
    content = result.content
    download_source = result.download_source
    log.info(
        "工作空间文件下载完成",
        file_id=file_id,
        assistant_id=record.assistant_id,
        filename=record.filename,
        bucket=record.bucket,
        size_bytes=len(content),
        content_type=record.content_type,
        download_source=download_source,
        storage_backend=record.storage_backend,
    )
    return Response(
        content=content,
        media_type=record.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": _content_disposition_header(record.filename, disposition),
            "X-IronAgent-Download-Source": download_source,
            "X-IronAgent-Storage-Backend": record.storage_backend or "",
        },
    )


@router.get("/api/v1/users/{user_id}/workspace-files/{file_id}/download")
def download_user_file(
    user_id: str,
    file_id: str,
    disposition: DownloadDisposition = Query(default="attachment"),
    svc: WorkspaceFileService = Depends(_get_service),
) -> Response:
    log.info("用户工作空间文件下载开始", user_id=user_id, file_id=file_id, disposition=disposition)
    record = svc.get_file(file_id)
    if record is None or record.deleted:
        log.warning(
            "用户工作空间文件下载失败", user_id=user_id, file_id=file_id, reason="file_not_found"
        )
        raise HTTPException(status_code=404, detail="file not found")
    if record.owner_user_id != user_id:
        log.warning(
            "用户工作空间文件下载失败",
            user_id=user_id,
            file_id=file_id,
            assistant_id=record.assistant_id,
            owner_user_id=record.owner_user_id,
            reason="owner_mismatch",
        )
        raise HTTPException(status_code=403, detail="owner mismatch")
    result = svc.download_file(file_id)
    if result is None:
        log.warning(
            "用户工作空间文件下载失败",
            user_id=user_id,
            file_id=file_id,
            reason="content_not_found",
        )
        raise HTTPException(status_code=404, detail="content_not_found")
    file_record = result.record
    content = result.content
    download_source = result.download_source
    log.info(
        "用户工作空间文件下载完成",
        user_id=user_id,
        file_id=file_id,
        assistant_id=file_record.assistant_id,
        filename=file_record.filename,
        bucket=file_record.bucket,
        size_bytes=len(content),
        content_type=file_record.content_type,
        download_source=download_source,
        storage_backend=file_record.storage_backend,
    )
    return Response(
        content=content,
        media_type=file_record.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": _content_disposition_header(file_record.filename, disposition),
            "X-IronAgent-Download-Source": download_source,
            "X-IronAgent-Storage-Backend": file_record.storage_backend or "",
        },
    )


@router.get(
    "/api/v1/workspace-files/{file_id}/events",
    response_model=list[FileEventResponse],
    dependencies=[Depends(require_internal_service_token)],
)
def list_file_events(
    file_id: str,
    svc: WorkspaceFileService = Depends(_get_service),
) -> list[FileEventResponse]:
    return [FileEventResponse(**item.model_dump()) for item in svc.list_events(file_id=file_id)]


@router.post(
    "/api/v1/workspace-files/{file_id}/sync",
    dependencies=[Depends(require_internal_service_token)],
)
def sync_file(
    file_id: str,
    svc: WorkspaceFileService = Depends(_get_service),
) -> dict[str, Any]:
    record = svc.sync_file(file_id)
    if record is None:
        raise HTTPException(status_code=404, detail="file not found")
    return {"status": "synced", "file": _file_payload(record, svc=svc)}


@router.post(
    "/api/v1/workspace-files/scan",
    dependencies=[Depends(require_internal_service_token)],
)
def scan_files(
    assistant_id: str = Form(...),
    bucket: TargetBucket | None = Form(default=None),
    svc: WorkspaceFileService = Depends(_get_service),
) -> dict[str, Any]:
    summary = svc.scan_workspace_results(assistant_id=assistant_id, bucket=bucket)
    return {
        "assistant_id": assistant_id,
        "scanned": len(summary),
        "files": [_file_payload(item, svc=svc) for item in summary],
    }


@router.get(
    "/api/v1/workspace-files/{file_id}/download-info",
    dependencies=[Depends(require_internal_service_token)],
)
def download_info(
    file_id: str,
    svc: WorkspaceFileService = Depends(_get_service),
) -> dict[str, Any]:
    record = svc.get_file(file_id)
    if record is None or record.deleted:
        raise HTTPException(status_code=404, detail="file not found")
    payload = _file_payload(record, svc=svc)
    return {
        "file_id": record.file_id,
        "filename": record.filename,
        "workspace_relative_path": payload["workspace_relative_path"],
        "runtime_workspace_path": payload["runtime_workspace_path"],
        "workspace_present": payload["workspace_present"],
        "workspace_mtime": payload["workspace_mtime"],
        "workspace_size": payload["workspace_size"],
        "object_storage_status": payload["object_storage_status"],
        "download_url": _download_url(record.file_id),
    }


@router.post(
    "/api/v1/workspace-files/{file_id}/delete",
    response_model=DeleteResponse,
    dependencies=[Depends(require_internal_service_token)],
)
def delete_file(
    file_id: str,
    user_id: str = Query(default=""),
    svc: WorkspaceFileService = Depends(_get_service),
) -> DeleteResponse:
    log.info("工作空间文件删除开始", file_id=file_id, user_id=user_id)
    try:
        result = svc.delete_file(file_id, user_id=user_id)
    except FileSecurityError as exc:
        log.warning(
            "工作空间文件删除失败",
            file_id=file_id,
            user_id=user_id,
            reason="security_error",
            error=str(exc),
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if result is None:
        log.warning(
            "工作空间文件删除失败", file_id=file_id, user_id=user_id, reason="file_not_found"
        )
        raise HTTPException(status_code=404, detail="file not found")
    from src.workspace_files.models import ExternalPushRequest

    if isinstance(result, ExternalPushRequest):
        response = DeleteResponse(
            status="pending_approval",
            file_id=file_id,
            approval_id=result.approval_id,
            approval_scope="user",
            message="persistent file deletion requires approval",
        )
        log.info(
            "工作空间文件删除等待审批",
            file_id=file_id,
            user_id=user_id,
            assistant_id=result.assistant_id,
            approval_id=result.approval_id,
            approval_scope=response.approval_scope,
        )
        return response
    response = DeleteResponse(status="deleted", file_id=file_id, message="file deleted")
    log.info(
        "工作空间文件删除完成",
        file_id=file_id,
        user_id=user_id,
        assistant_id=result.assistant_id,
        filename=result.filename,
        bucket=result.bucket,
    )
    return response


def _scan_summary_payload(summary: WorkspaceScanSummary) -> dict[str, Any]:
    return {
        "assistants_scanned": summary.assistants_scanned,
        "files_scanned": summary.files_scanned,
        "files_synced": summary.files_synced,
        "files_deleted": summary.files_deleted,
        "errors": summary.errors or [],
    }
