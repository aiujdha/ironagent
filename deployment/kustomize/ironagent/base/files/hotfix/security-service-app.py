from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.collaboration.routes import router as collaboration_router
from src.market.routes import router as market_router
from src.security_service.file_routes import router as file_router
from src.security_service.routes import router
from src.security_service.user_routes import router as user_router
from src.shared.config import get_settings
from src.shared.logging import get_logger
from src.workspace_files.service import WorkspaceFileService

log = get_logger(__name__)


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
                    files_scanned=summary.files_scanned,
                    files_synced=summary.files_synced,
                    files_deleted=summary.files_deleted,
                    errors=len(summary.errors or []),
                )
        except Exception as exc:  # pragma: no cover - defensive background worker guard
            log.warning("workspace_result_scan_failed", error=f"{type(exc).__name__}: {exc}")
        await asyncio.sleep(interval)


@asynccontextmanager
async def _lifespan(_: FastAPI):
    settings = get_settings()
    scan_task: asyncio.Task[None] | None = None
    if settings.workspace_result_scan_enabled:
        scan_task = asyncio.create_task(_workspace_result_scan_loop())
    try:
        yield
    finally:
        if scan_task is not None:
            scan_task.cancel()
            try:
                await scan_task
            except asyncio.CancelledError:
                pass


def create_security_app() -> FastAPI:
    app = FastAPI(title="IronAgent Security Service", version="0.1.0", lifespan=_lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )
    app.include_router(router)
    app.include_router(market_router)
    app.include_router(user_router)
    app.include_router(file_router)
    app.include_router(collaboration_router)
    return app
