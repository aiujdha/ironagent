"""FastAPI application factory for the Assistants Service.

Run standalone::

    uvicorn src.assistants_service.app:app --reload --host 0.0.0.0 --port 8100
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.assistants_service.router import router
from src.shared.database import dispose_database_engines
from src.shared.logging import configure_logging, get_logger

logger = get_logger("assistants_service.app")


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    logger.info("assistants_service.startup")
    try:
        yield
    finally:
        await dispose_database_engines()
        logger.info("assistants_service.shutdown")


def create_assistants_app() -> FastAPI:
    application = FastAPI(
        title="IronAgent Assistants Service",
        description=(
            "Assistant lifecycle management — CRUD, runtime provisioning, "
            "capability profiles."
        ),
        version="0.1.0",
        lifespan=_lifespan,
    )
    application.include_router(router)
    return application


app = create_assistants_app()
