"""Liveness and readiness probes."""

from __future__ import annotations

import structlog
from anyio import to_thread
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from inleverboks.adapters.storage import StorageError

router = APIRouter()
log = structlog.get_logger(__name__)

NO_STORE = {"Cache-Control": "no-store"}


@router.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"}, headers=NO_STORE)


@router.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> JSONResponse:
    checks: dict[str, str] = {}

    def _db_ping() -> None:
        with request.app.state.engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    try:
        await to_thread.run_sync(_db_ping)
        checks["database"] = "ok"
    except Exception as exc:
        log.warning("readyz.database_failed", error=type(exc).__name__)
        checks["database"] = "error"

    try:
        await request.app.state.storage.healthcheck()
        checks["storage"] = "ok"
    except StorageError as exc:
        log.warning("readyz.storage_failed", error=str(exc))
        checks["storage"] = "error"

    ready = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {"status": "ok" if ready else "error", "checks": checks},
        status_code=200 if ready else 503,
        headers=NO_STORE,
    )
