from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from starlette.background import BackgroundTask

from src.market.audit import capability_audit_event
from src.market.service import MarketService
from src.shared.auth import require_internal_service_token
from src.shared.logging import get_logger
from src.shared.repositories import build_audit_repository

router = APIRouter(prefix="/api/v1/market", tags=["market"])
service = MarketService()
upload_file_field = File(...)
log = get_logger(__name__)


def _download_error_status(error: ValueError) -> int:
    return 404 if "not found" in str(error).lower() else 502


def _operator_id_from_request(request: Request) -> str:
    return request.headers.get("x-admin-operator", "").strip() or "admin-web"


def _install_response(
    result,
    *,
    audit_error: str = "",
) -> dict[str, str | int | bool | list[str]]:
    warnings: list[str] = []
    if result.runtime_reload_status == "failed":
        warnings.append("runtime reload failed")
    if audit_error:
        warnings.append(audit_error)
    return {
        "assistant_id": result.assistant_id,
        "install_path": result.install_path,
        "install_count": result.install_count,
        "sync_status": result.sync_status,
        "sync_error": result.sync_error,
        "runtime_reload_status": result.runtime_reload_status,
        "runtime_reload_error": result.runtime_reload_error,
        "runtime_effective": result.runtime_reload_status != "failed",
        "action": f"capability_{result.action}",
        "previous_version": result.previous_version,
        "warnings": warnings,
    }


async def _append_capability_audit(
    *,
    request: Request,
    assistant_id: str,
    kind: str,
    capability_id: str,
    slug: str,
    version: str,
    previous_version: str,
    action: str,
    runtime_reload_status: str,
    runtime_reload_error: str,
) -> str:
    try:
        await build_audit_repository().aappend_event(
            capability_audit_event(
                operator_id=_operator_id_from_request(request),
                assistant_id=assistant_id,
                kind=kind,
                capability_id=capability_id,
                slug=slug,
                version=version,
                previous_version=previous_version,
                action=action,
                runtime_reload_status=runtime_reload_status,
                runtime_reload_error=runtime_reload_error,
        )
            )
    except Exception as exc:
        error = f"audit log failed: {type(exc).__name__}: {exc}"
        log.warning(
            "capability_install_audit_failed",
            assistant_id=assistant_id,
            kind=kind,
            capability_id=capability_id,
            slug=slug,
            action=action,
            error=error,
        )
        return error
    return ""


@router.get("/skills")
async def list_skills(sync_cos: bool = Query(False)) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in service.list_skills(sync_cos=sync_cos)]


@router.post("/skills/upload")
async def upload_skill(
    slug: str = Form(...),
    display_name: str = Form(...),
    owner: str = Form(""),
    version: str = Form("1.0.0"),
    tags: str = Form(""),
    file: UploadFile = upload_file_field,
    _admin: None = Depends(require_internal_service_token),
) -> dict[str, Any]:
    try:
        content = await file.read()
        record = service.upload_skill(
            filename=file.filename or "skill-package.zip",
            content=content,
            slug=slug,
            display_name=display_name,
            owner=owner,
            version=version,
            tags=tags,
        )
        return record.model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/skills/{skill_id}/approve")
async def approve_skill(
    skill_id: str,
    _admin: None = Depends(require_internal_service_token),
) -> dict[str, Any]:
    try:
        return service.approve_skill(skill_id).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/skills/{skill_id}/install")
async def install_skill(
    request: Request,
    skill_id: str,
    assistant_id: str = Form(...),
    _admin: None = Depends(require_internal_service_token),
) -> dict[str, str | int | bool | list[str]]:
    try:
        record = service.get_skill(skill_id)
        if record is None:
            raise ValueError("skill not found")
        result = await service.ainstall_skill(record_id=skill_id, assistant_id=assistant_id)
        action = f"capability_{result.action}"
        audit_error = await _append_capability_audit(
            request=request,
            assistant_id=assistant_id,
            kind="skills",
            capability_id=record.id,
            slug=record.slug,
            version=record.version,
            previous_version=result.previous_version,
            action=action,
            runtime_reload_status=result.runtime_reload_status,
            runtime_reload_error=result.runtime_reload_error,
        )
        return _install_response(result, audit_error=audit_error)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/skills/{skill_id}")
async def delete_skill(
    skill_id: str,
    _admin: None = Depends(require_internal_service_token),
) -> dict[str, str]:
    try:
        service.delete_skill(skill_id)
        return {"status": "deleted"}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/skills/{skill_id}/download", response_model=None)
async def download_skill(skill_id: str) -> Response:
    try:
        target = service.get_skill_download_target(skill_id)
    except ValueError as exc:
        raise HTTPException(status_code=_download_error_status(exc), detail=str(exc)) from exc
    if target.source == "remote":
        return RedirectResponse(url=target.path, status_code=307)
    background = (
        BackgroundTask(lambda: Path(target.path).unlink(missing_ok=True))
        if target.cleanup
        else None
    )
    return FileResponse(path=target.path, filename=target.filename, background=background)


@router.get("/plugins")
async def list_plugins(sync_cos: bool = Query(False)) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in service.list_plugins(sync_cos=sync_cos)]


@router.post("/plugins/upload")
async def upload_plugin(
    slug: str = Form(...),
    display_name: str = Form(...),
    owner: str = Form(""),
    version: str = Form("1.0.0"),
    tags: str = Form(""),
    file: UploadFile = upload_file_field,
    _admin: None = Depends(require_internal_service_token),
) -> dict[str, Any]:
    try:
        content = await file.read()
        record = service.upload_plugin(
            filename=file.filename or "plugin-package.zip",
            content=content,
            slug=slug,
            display_name=display_name,
            owner=owner,
            version=version,
            tags=tags,
        )
        return record.model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/plugins/{plugin_id}/approve")
async def approve_plugin(
    plugin_id: str,
    _admin: None = Depends(require_internal_service_token),
) -> dict[str, Any]:
    try:
        return service.approve_plugin(plugin_id).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/plugins/{plugin_id}/install")
async def install_plugin(
    request: Request,
    plugin_id: str,
    assistant_id: str = Form(...),
    _admin: None = Depends(require_internal_service_token),
) -> dict[str, str | int | bool | list[str]]:
    try:
        record = service.get_plugin(plugin_id)
        if record is None:
            raise ValueError("plugin not found")
        result = await service.ainstall_plugin(record_id=plugin_id, assistant_id=assistant_id)
        action = f"capability_{result.action}"
        audit_error = await _append_capability_audit(
            request=request,
            assistant_id=assistant_id,
            kind="plugins",
            capability_id=record.id,
            slug=record.slug,
            version=record.version,
            previous_version=result.previous_version,
            action=action,
            runtime_reload_status=result.runtime_reload_status,
            runtime_reload_error=result.runtime_reload_error,
        )
        return _install_response(result, audit_error=audit_error)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/plugins/{plugin_id}")
async def delete_plugin(
    plugin_id: str,
    _admin: None = Depends(require_internal_service_token),
) -> dict[str, str]:
    try:
        service.delete_plugin(plugin_id)
        return {"status": "deleted"}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/plugins/{plugin_id}/download", response_model=None)
async def download_plugin(plugin_id: str) -> Response:
    try:
        target = service.get_plugin_download_target(plugin_id)
    except ValueError as exc:
        raise HTTPException(status_code=_download_error_status(exc), detail=str(exc)) from exc
    if target.source == "remote":
        return RedirectResponse(url=target.path, status_code=307)
    background = (
        BackgroundTask(lambda: Path(target.path).unlink(missing_ok=True))
        if target.cleanup
        else None
    )
    return FileResponse(path=target.path, filename=target.filename, background=background)
