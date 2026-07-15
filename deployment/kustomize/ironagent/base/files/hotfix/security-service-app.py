from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

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
    return app
