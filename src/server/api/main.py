"""FastAPI application entry point."""

import math
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from src.server.api.errors import ConflictError, decision_required_handler
from src.server.api.middleware import TenantResolutionMiddleware
from src.shared.logging_config import hide_stream_links

# uvicorn has set up its loggers by the time it imports the app.
hide_stream_links()


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to every response."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        return response


from src.server.api.routers import admin, ai, archive, assets, producers, changes, projects, keys, libraries, me, path_filters, tenant, trash, video
from src.server.api.routers.auth import router as auth_router
from src.server.api.routers.users import router as users_router
from src.server.api.routers.artifacts import router as artifacts_router
from src.server.api.routers.playback import router as playback_router
from src.server.api.routers.ingest import router as ingest_router
from src.server.api.routers.maintenance import router as maintenance_router
from src.server.api.routers.upgrade import router as upgrade_router
from src.server.api.routers.facets import router as facets_router
from src.server.api.routers.similarity import router as similarity_router
from src.server.api.routers.public_projects import router as public_projects_router
from src.server.api.routers.export import router as export_router
from src.server.api.routers.ratings import router as ratings_router
from src.server.api.routers.query import router as query_router
from src.server.api.routers.filters import router as filters_router
from src.server.api.routers.views import router as views_router
from src.server.api.routers.upkeep import router as upkeep_router
from src.server.api.routers.people import router as people_router, faces_router
from src.server.api.routers.system import router as system_router
from src.server.api.routers.locations import router as locations_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.environ.get("JWT_SECRET"):
        raise RuntimeError("JWT_SECRET environment variable is required but not set")
    yield


app = FastAPI(title="Lumiverb API", version="0.1.0", lifespan=lifespan)


app.add_exception_handler(ConflictError, decision_required_handler)


@app.exception_handler(RequestValidationError)
async def _request_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's default 422, except that it can echo any input: the default
    crashes when the rejected input holds inf or NaN (not valid JSON)."""

    def finite(value):
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        if isinstance(value, dict):
            return {k: finite(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [finite(v) for v in value]
        return value

    return JSONResponse(status_code=422, content={"detail": finite(jsonable_encoder(exc.errors()))})


def _unavailable(exc: Exception) -> bool:
    """A database error that passes: a lost or refused connection (SQLSTATE
    class 08, 57P01-57P03), a deadlock or serialization failure, too many
    connections, or no SQLSTATE at all (the server couldn't be reached)."""
    if getattr(exc, "connection_invalidated", False) or isinstance(exc, InterfaceError):
        return True
    code = getattr(getattr(exc, "orig", None), "pgcode", None)
    return code is None or code.startswith("08") or code in {"57P01", "57P02", "57P03", "40P01", "40001", "53300"}


@app.exception_handler(OperationalError)
@app.exception_handler(InterfaceError)
async def _database_unavailable(request: Request, exc: Exception) -> JSONResponse:
    """The database away, a lost connection, a deadlock, too many
    connections: the request's trouble for now, not what it carried (a 503
    the scheduler charges no clip for). Any other database error (a value
    too long, a statement timeout, a full disk) recurs: a 500, which the
    scheduler counts as a crash."""
    import logging

    if not _unavailable(exc):
        raise exc
    logging.getLogger(__name__).warning("database unavailable for %s: %s", request.url.path, exc)
    return JSONResponse(status_code=503, content={"error": {
        "code": "database_unavailable", "message": "The database is unavailable; try again shortly.", "details": {}}})
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(TenantResolutionMiddleware)
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(admin.router)
app.include_router(ai.router)
app.include_router(tenant.router)
app.include_router(path_filters.router)
app.include_router(libraries.router)
app.include_router(changes.router)
app.include_router(artifacts_router)
app.include_router(ingest_router)
app.include_router(ratings_router)
app.include_router(query_router)
app.include_router(views_router)
app.include_router(people_router)
app.include_router(faces_router)
app.include_router(filters_router)
app.include_router(facets_router)
app.include_router(playback_router)
app.include_router(locations_router)  # before /v1/assets/{asset_id}
app.include_router(assets.router)
app.include_router(projects.router, prefix="/v1/projects", tags=["projects"])
app.include_router(public_projects_router, prefix="/v1/public/projects", tags=["public_projects"])
app.include_router(export_router)
app.include_router(video.router)
app.include_router(keys.router)
app.include_router(me.router)
app.include_router(trash.router)
app.include_router(archive.router)
app.include_router(producers.router)
app.include_router(similarity_router)
app.include_router(maintenance_router)
app.include_router(upgrade_router)
app.include_router(upkeep_router)
app.include_router(system_router)


@app.get("/health")
def health() -> dict[str, str]:
    """Health check; no auth required."""
    return {"status": "ok"}
