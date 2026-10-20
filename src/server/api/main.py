"""FastAPI application entry point."""

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError

from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from src.server.database import set_application_name
from src.server.telemetry import NO_TELEMETRY
from src.server.api.errors import (
    ApiError,
    api_error_handler,
    envelope,
    http_exception_handler,
    storage_unavailable_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from src.server.storage.store import Unavailable
from src.server.api import devices, library_access
from src.server.api.https_seen import NoteHttps
from src.server.api.middleware import TenantResolutionMiddleware
from src.shared.logging_config import configure_api_logging, escape_log_lines, hide_stream_links

# uvicorn has set up its loggers by the time it imports the app.
hide_stream_links()
escape_log_lines()
configure_api_logging()


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    ),
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to every response (an unhandled error's 500 is
    made outside every middleware: _unhandled adds them to it)."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response


from src.server.api.routers import admin, ai, archive, assets, producers, changes, projects, keys, libraries, me, path_filters, readers, scans, tenant, trash, uploads, video
from src.server.api.routers import devices as devices_routes
from src.server.api.routers import library_people
from src.server.api.routers.auth import router as auth_router
from src.server.api.routers.users import router as users_router
from src.server.api.routers.setup import router as setup_router
from src.server.api.routers.artifacts import router as artifacts_router
from src.server.api.routers import playback
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
from src.server.api.routers.places import router as places_router
from src.server.api.routers.plugins import router as plugins_router
from src.server.api.routers.plugin_work import router as plugin_work_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.environ.get("JWT_SECRET"):
        raise RuntimeError("JWT_SECRET environment variable is required but not set")
    # The install's Store, its provider read now: one there isn't stops the API here (ADR-020 decision 2).
    from src.server.storage.store import get_store

    get_store()
    # AI models' weights in one folder, the scheduler's too (ADR-020 decision 3):
    # what was kept elsewhere before is moved in, before search loads one.
    from src.processing.models_dir import adopt_old_models, use_models_dir

    use_models_dir()
    adopt_old_models()
    playback.start_cuts()
    yield
    # Running ffmpeg cuts would otherwise hold the worker's exit for minutes.
    playback.stop_cuts()
    from src.server.faces import server_face_provider

    server_face_provider.close()  # search by image's face process


set_application_name("lumiverb-api")  # its connections, as PostgreSQL shows them
# A device's key goes no further than the routes a scan calls (src/server/api/devices.py); a person
# no further than the libraries they can see, editing where they're an editor (library_access.py).
# No docs pages or schema served (/docs, /redoc, /openapi.json), as the AI worker serves none: the API's
# contract is docs/cursor-api.md, and a map of every route is no one's to read anonymously.
app = FastAPI(title="Lumiverb API", version="0.1.0", lifespan=lifespan, telemetry=NO_TELEMETRY,
              docs_url=None, redoc_url=None, openapi_url=None,
              dependencies=[Depends(devices.gate), Depends(library_access.gate)])


# Every error in one envelope: {"error": {"code", "message", "details"}} (errors.py).
app.add_exception_handler(ApiError, api_error_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(Unavailable, storage_unavailable_handler)


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    response = await unhandled_error_handler(request, exc)
    response.headers.update(SECURITY_HEADERS)
    return response


app.add_exception_handler(Exception, _unhandled)


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
    return envelope(503, "database_unavailable", "The database is unavailable; try again shortly.")
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(TenantResolutionMiddleware)
app.add_middleware(NoteHttps)  # whether a device has used HTTPS yet (Settings › Network)
app.include_router(auth_router)
app.include_router(setup_router)
app.include_router(users_router)
app.include_router(admin.router)
app.include_router(ai.router)
app.include_router(tenant.router)
app.include_router(path_filters.router)
app.include_router(libraries.router)
app.include_router(library_people.router)
app.include_router(changes.router)
app.include_router(scans.router)
app.include_router(readers.router)
app.include_router(devices_routes.router)
app.include_router(uploads.router)
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
app.include_router(plugins_router)
app.include_router(plugin_work_router)
app.include_router(places_router)
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
