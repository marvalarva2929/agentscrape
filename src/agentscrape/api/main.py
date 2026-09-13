"""FastAPI application."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..config import settings
from ..db.session import dispose_engine
from .errors import register_exception_handlers
from .routes import admin, artifacts, auth, health, records, runs, sites

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
)
log = logging.getLogger("agentscrape")

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    log.info("agentscrape api starting (model=%s)", settings.llm_model)

    # Retention is swept on startup rather than by cron: the box is stopped when
    # idle, so a schedule would silently never fire.
    import asyncio

    from ..storage.artifacts import sweep_expired_exports, sweep_expired_screenshots

    async def _sweep() -> None:
        try:
            await sweep_expired_screenshots()
            await sweep_expired_exports()
        except Exception:
            log.exception("startup retention sweep failed")

    sweep_task = asyncio.create_task(_sweep())
    yield
    sweep_task.cancel()
    await dispose_engine()
    log.info("agentscrape api stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="agentscrape",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=f"{API_PREFIX}/docs",
        openapi_url=f"{API_PREFIX}/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )
    register_exception_handlers(app)

    for router in (
        health.router,
        auth.router,
        runs.router,
        records.router,
        sites.router,
        sites.meta_router,
        admin.router,
        artifacts.router,
    ):
        app.include_router(router, prefix=API_PREFIX)
    return app


app = create_app()
