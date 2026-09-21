"""FastAPI application."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..config import settings
from ..db.session import dispose_engine
from .errors import register_exception_handlers
from .routes import (
    admin,
    artifacts,
    auth,
    health,
    records,
    runs,
    schools,
    sites,
)

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

    if settings.seed_demo_on_startup:
        # Before serving, so the first request already sees the schools.
        from ..demo import seed_if_empty

        try:
            await seed_if_empty()
        except Exception:
            log.exception("seeding the empty database failed; the school list will be empty")
    # After the demo seed, which only runs on an empty database. The fixed list
    # of schools is the only thing loaded at startup; a problem with it is
    # logged and never stops the API, since the schools already stored still work.
    from ..db.session import session_scope
    from ..school_catalog import import_catalog

    try:
        async with session_scope() as session:
            result = await import_catalog(session)
        log.info("school catalog synchronized: %s", result)
    except Exception:
        log.exception("importing the school catalog failed; the school list is unchanged")
    if settings.resume_runs_on_startup:
        from ..orchestrator.service import resume_interrupted_runs

        try:
            await resume_interrupted_runs()
        except Exception:
            log.exception("resuming interrupted runs failed; restart them from the UI")

    # Keeps the queue moving after a restart: a run left running by a dead
    # process only shows itself once its heartbeat stops.
    from ..orchestrator.scheduler import supervise

    supervisor = asyncio.create_task(supervise())
    yield
    supervisor.cancel()
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
    # Explicit origins. The frontend is served from GitHub Pages, a different
    # origin from this API, so the browser enforces CORS on every call.
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )
    log.info("CORS origins: %s", ", ".join(origins))
    register_exception_handlers(app)

    for router in (
        health.router,
        auth.router,
        runs.router,
        records.router,
        schools.router,
        sites.router,
        sites.meta_router,
        admin.router,
        artifacts.router,
    ):
        app.include_router(router, prefix=API_PREFIX)
    return app


app = create_app()
