"""FastAPI application entry point."""

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DataError, InterfaceError, OperationalError

from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from src.server.database import set_application_name
from src.server.telemetry import NO_TELEMETRY
from src.server.api.errors import (
    ApiError,
    api_error_handler,
    bad_cursor_handler,
    envelope,
    http_exception_handler,
    not_text_handler,
    secrets_key_missing_handler,
    storage_unavailable_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from src.server.cursors import BadCursorError
from src.server.sealing import SecretsKeyMissingError
from src.server.storage.store import Unavailable
from src.server.api import devices, library_access, no_nul
from src.server.api.exposure import ExposureGate
from src.server.api.https_seen import NoteHttps
from src.server.api.middleware import TenantResolutionMiddleware
from src.server.api.no_nul import NoNul
from src.shared import secrets_env
from src.shared.logging_config import configure_api_logging, escape_log_lines, hide_stream_links

# Each worker uvicorn starts imports this: from here its environment (the env file's secrets) and memory are closed
# to the other processes of its user, a plug-in's among them (uvicorn's own process: src/server/api/__main__.py).
secrets_env.not_dumpable()
# Served by python -m src.server.api: its settings read (and kept: get_settings), its secrets leave the
# environment, as the scheduler's do, so what it starts (its face process, ffprobe, git) has none.
if os.environ.get(secrets_env.API_FORGETS):
    from src.server.config import get_settings

    get_settings()
    secrets_env.forget()
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
from src.server.api.routers.visits import router as visits_router
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
    from src.server.config import get_settings

    # Its settings, or its environment while it still has it (forgotten once read when served: API_FORGETS).
    if not (os.environ.get("JWT_SECRET") or get_settings().jwt_secret):
        raise RuntimeError("JWT_SECRET environment variable is required but not set")
    # The install's Store, its provider read now: one there isn't stops the API here (ADR-020 decision 2).
    from src.server.storage.store import get_store

    get_store()
    # AI models' weights in one folder, the scheduler's too (ADR-020 decision 3):
    # what was kept elsewhere before is moved in, before search loads one.
    from src.processing.models_dir import adopt_old_models, use_models_dir

    use_models_dir()
    adopt_old_models()
    # What its stop needs, imported now: an upgrade can remove this version's files before it stops (brew upgrade's
    # cleanup, Oct 30: "No module named 'src.server.faces'" as 0.1.158 stopped).
    from src.server.faces import server_face_provider

    playback.start_cuts()
    yield
    # Running ffmpeg cuts would otherwise hold the worker's exit for minutes.
    playback.stop_cuts()
    server_face_provider.close()  # search by image's face process


set_application_name("lumiverb-api")  # its connections, as PostgreSQL shows them
# A body holding a NUL or a lone surrogate is a 400 before the gates or the route look at it (no_nul.py). A
# device's key goes no further than the routes a scan calls (src/server/api/devices.py); a person no further
# than the libraries they can see, editing where they're an editor (library_access.py).
# No docs pages or schema served (/docs, /redoc, /openapi.json), as the AI worker serves none: the API's
# contract is docs/cursor-api.md, and a map of every route is no one's to read anonymously.
app = FastAPI(title="Lumiverb API", version="0.1.0", lifespan=lifespan, telemetry=NO_TELEMETRY,
              docs_url=None, redoc_url=None, openapi_url=None,
              dependencies=[Depends(no_nul.gate), Depends(devices.gate), Depends(library_access.gate)])


# Every error in one envelope: {"error": {"code", "message", "details"}} (errors.py).
app.add_exception_handler(ApiError, api_error_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(Unavailable, storage_unavailable_handler)
app.add_exception_handler(SecretsKeyMissingError, secrets_key_missing_handler)
app.add_exception_handler(BadCursorError, bad_cursor_handler)
# A NUL or a lone surrogate the database refused, past no_nul.py's gates: the same 400 (any other is raised on;
# a UnicodeEncodeError is a ValueError).
app.add_exception_handler(ValueError, not_text_handler)
app.add_exception_handler(DataError, not_text_handler)


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
# Before sign-in and the public pages' lookups: text that isn't text (a NUL, a lone surrogate, bytes that aren't
# UTF-8) in the path or a query parameter is a 400 (no_nul.py).
app.add_middleware(NoNul, headers=SECURITY_HEADERS)
app.add_middleware(NoteHttps)  # whether a device has used HTTPS yet (Settings › Network)
# Outermost: what isn't exposed from elsewhere (LUMIVERB_EXPOSURE) reaches nothing else.
app.add_middleware(ExposureGate, headers=SECURITY_HEADERS)
app.include_router(auth_router)
app.include_router(setup_router)
app.include_router(users_router)
app.include_router(admin.router)
app.include_router(ai.router)
app.include_router(tenant.router)
app.include_router(path_filters.router)
app.include_router(visits_router)  # before /v1/libraries/{id} and /v1/projects/{id}
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
