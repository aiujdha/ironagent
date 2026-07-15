from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.collaboration.routes import router as collaboration_router
from src.collaboration.routes import service as collaboration_service
from src.market.routes import router as market_router
from src.security_service.file_routes import router as file_router
from src.security_service.routes import drain_approval_bypass_audit_tasks, router
from src.security_service.user_routes import router as user_router
from src.shared.config import approval_mode_disabled, get_settings
from src.shared.database import dispose_database_engines
from src.shared.logging import get_logger
from src.workspace_files.readiness import acheck_workspace_file_metadata_readiness
from src.workspace_files.service import WorkspaceFileService

log = get_logger(__name__)


def _resolve_control_ui_allowed_origins(settings: Any) -> list[str]:
    candidates: list[str] = []
    raw_allowed_origins = (getattr(settings, "control_ui_allowed_origins_json", "") or "").strip()
    if raw_allowed_origins:
        try:
            parsed = json.loads(raw_allowed_origins)
        except json.JSONDecodeError:
            log.warning(
                "security_service_invalid_control_ui_allowed_origins_json",
                raw=raw_allowed_origins,
            )
        else:
            if isinstance(parsed, list):
                candidates.extend(str(item).strip() for item in parsed if str(item).strip())

    control_ui_url = settings.control_ui_public_url.strip()
    if control_ui_url:
        candidates.append(control_ui_url)

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


async def _workspace_result_scan_loop() -> None:
    settings = get_settings()
    service = WorkspaceFileService()
    interval = max(settings.workspace_result_scan_interval_seconds, 1)
    while True:
        try:
            summary = await asyncio.to_thread(service.scan_workspace_results)
            if summary.files_synced or summary.files_deleted or summary.errors:
                log.info(
                    "workspace_result_scan_completed",
                    assistants_scanned=summary.assistants_scanned,
                    files_scanned=summary.files_synced + summary.files_deleted,
                    files_synced=summary.files_synced,
                    files_deleted=summary.files_deleted,
                    errors=len(summary.errors or []),
                )
        except Exception as exc:  # pragma: no cover - defensive background worker guard
            log.warning("workspace_result_scan_failed", error=f"{type(exc).__name__}: {exc}")
        await asyncio.sleep(interval)


async def _preconnect_gateway() -> None:
    """
    预热 Gateway WebSocket 连接。

    在服务启动时建立到 IronAgent Gateway 的 WebSocket 连接并完成握手，
    确保首次审批通知时不需要冷启动连接（TCP + WebSocket 升级 + 3步握手），
    避免 fire-and-forget 通知因连接建立延迟或失败而丢失。

    优先使用已注册 assistant 的 gateway_url，如果无法获取则使用默认配置。
    """
    try:
        from src.approval.gateway_client import get_gateway_client
        from src.assistants.service import AssistantService

        # 尝试从已注册的 assistant 获取实际的 gateway_url
        gateway_url = None
        gateway_token = None
        try:
            svc = AssistantService()
            assistants = await asyncio.to_thread(svc.list_assistants)
            for a in assistants:
                if not a.deleted and a.runtime and a.runtime.gateway_url:
                    gateway_url = a.runtime.gateway_url
                    gateway_token = getattr(a.runtime, "gateway_token", None)
                    break
        except Exception as exc:
            log.debug("preconnect_assistant_lookup_failed", error=str(exc))

        client = get_gateway_client(gateway_url, gateway_token=gateway_token)
        connected = await client.ensure_connected()
        if connected:
            log.info("gateway_preconnect_success", url=client.url)
        else:
            log.warning("gateway_preconnect_failed", url=client.url, reason="handshake_failed")
    except Exception as exc:
        log.warning("gateway_preconnect_error", error=str(exc))


async def _disconnect_gateway() -> None:
    """关闭所有缓存的 Gateway WebSocket 连接"""
    try:
        from src.approval.gateway_client import _gateway_clients

        for _, client in list(_gateway_clients.items()):
            try:
                await client.disconnect()
            except Exception:
                pass
        _gateway_clients.clear()
    except Exception as exc:
        log.debug("gateway_disconnect_error", error=str(exc))


@asynccontextmanager
async def _lifespan(_: FastAPI):
    from src.approval.watchdog import start_watchdog, stop_watchdog

    settings = get_settings()
    watchdog_started = False
    preconnect_task: asyncio.Task[None] | None = None
    if not approval_mode_disabled(settings):
        # 启动审批超时 watchdog
        start_watchdog()
        watchdog_started = True

        # 预热 Gateway WebSocket 连接，避免首次审批通知因冷启动失败
        preconnect_task = asyncio.create_task(_preconnect_gateway())

    scan_task: asyncio.Task[None] | None = None
    if settings.workspace_result_scan_enabled:
        scan_task = asyncio.create_task(_workspace_result_scan_loop())
    try:
        yield
    finally:
        # 停止 watchdog
        if watchdog_started:
            stop_watchdog()
        # 断开可能由审批通知或预连接创建的 Gateway 连接
        if preconnect_task is not None and not preconnect_task.done():
            preconnect_task.cancel()
        await _disconnect_gateway()
        await drain_approval_bypass_audit_tasks()
        if scan_task is not None:
            scan_task.cancel()
            try:
                await scan_task
            except asyncio.CancelledError:
                pass
        collaboration_service.dispose()
        await dispose_database_engines()


def create_security_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="IronAgent Security Service", version="0.1.0", lifespan=_lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_resolve_control_ui_allowed_origins(settings),
        allow_origin_regex=settings.control_ui_allowed_origin_regex,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    app.include_router(market_router)
    app.include_router(user_router)
    app.include_router(file_router)
    app.include_router(collaboration_router)

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        checks: dict[str, Any] = {}
        ready = True
        try:
            checks["workspaceFileMetadata"] = await acheck_workspace_file_metadata_readiness()
        except Exception as exc:
            ready = False
            checks["workspaceFileMetadata"] = {
                "status": "degraded",
                "error": f"{type(exc).__name__}: {exc}",
            }
        try:
            checks["collaboration"] = await asyncio.to_thread(collaboration_service.check_ready)
        except Exception as exc:
            ready = False
            checks["collaboration"] = {
                "status": "degraded",
                "error": f"{type(exc).__name__}: {exc}",
            }
        return JSONResponse(
            {"status": "ok" if ready else "degraded", "checks": checks},
            status_code=200 if ready else 503,
        )

    return app
