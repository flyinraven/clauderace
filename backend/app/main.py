"""FastAPI application entry point."""

from __future__ import annotations

import logging
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api import admin, auth, documents, exams, jobs, osce, questions
from app.config import settings
from app.db import session_scope
from app.services.errors import log_error
from app.services.jobs.runner import start_worker, stop_worker
from app.startup import bootstrap_admin_user, run_migrations

# Importing these modules runs their @register_handler decorators, which is how
# the job worker learns what it can execute. Without them, queued jobs would
# fail with "no handler registered".
from app.services.answers import generate as _answers_handlers  # noqa: F401
from app.services.generate import questions as _generation_handlers  # noqa: F401
from app.services.grading import grade as _grading_handlers  # noqa: F401
from app.services.imagesearch import service as _imagesearch_handlers  # noqa: F401
from app.services.ingest import pipeline as _ingest_handlers  # noqa: F401
from app.services.osce import prompts as _osce_handlers  # noqa: F401

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_migrations()
    with session_scope() as db:
        bootstrap_admin_user(db)
    start_worker()
    logger.info("%s v%s ready (%s)", settings.app_name, __version__, settings.environment)
    yield
    stop_worker()


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api")
app.include_router(admin.router, prefix="/api")
app.include_router(documents.router, prefix="/api")
app.include_router(questions.router, prefix="/api")
app.include_router(exams.router, prefix="/api")
app.include_router(osce.router, prefix="/api")
app.include_router(jobs.router, prefix="/api")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Record unexpected failures in the admin error log.

    Without this, a Render free instance failure is invisible - there is no
    persistent log to read after the container is recycled.
    """
    detail = traceback.format_exc()
    logger.error("Unhandled error on %s %s: %s", request.method, request.url.path, exc)
    try:
        with session_scope() as db:
            log_error(
                db,
                source="http",
                message=f"{type(exc).__name__}: {exc}",
                detail=detail,
                context={"method": request.method, "path": str(request.url.path)},
            )
    except Exception:  # noqa: BLE001
        logger.exception("Could not persist error log entry")

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An unexpected error occurred. The administrator has been notified."},
    )


@app.get("/api/health", tags=["system"])
def health() -> dict[str, str]:
    """Liveness probe. Also the endpoint an uptime pinger should hit to keep
    the Render free instance from sleeping before an exam sitting."""
    return {"status": "ok", "version": __version__, "environment": settings.environment}


@app.get("/api/ready", tags=["system"])
def ready() -> dict[str, object]:
    """Readiness probe that actually touches the database."""
    from sqlalchemy import text

    from app.db import engine

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "degraded", "database": f"unavailable: {exc}"}
