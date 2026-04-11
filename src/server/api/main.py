"""FastAPI application entry point."""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from src.server.api.middleware import TenantResolutionMiddleware


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


from src.server.api.routers import admin, assets, collections, keys, libraries, me, path_filters, tenant, trash, video
from src.server.api.routers.auth import router as auth_router
from src.server.api.routers.users import router as users_router
from src.server.api.routers.artifacts import router as artifacts_router
from src.server.api.routers.ingest import router as ingest_router
from src.server.api.routers.maintenance import router as maintenance_router
from src.server.api.routers.upgrade import router as upgrade_router
from src.server.api.routers.search import router as search_router
from src.server.api.routers.facets import router as facets_router
from src.server.api.routers.similarity import router as similarity_router
from src.server.api.routers.public_collections import router as public_collections_router
from src.server.api.routers.ratings import router as ratings_router
from src.server.api.routers.browse import router as browse_router
from src.server.api.routers.query import router as query_router
from src.server.api.routers.filters import router as filters_router
from src.server.api.routers.views import router as views_router
from src.server.api.routers.upkeep import router as upkeep_router
from src.server.api.routers.people import router as people_router, faces_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.environ.get("JWT_SECRET"):
        raise RuntimeError("JWT_SECRET environment variable is required but not set")
    yield


app = FastAPI(title="Lumiverb API", version="0.1.0", lifespan=lifespan)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(TenantResolutionMiddleware)
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(admin.router)
app.include_router(tenant.router)
app.include_router(path_filters.router)
app.include_router(libraries.router)
app.include_router(artifacts_router)
app.include_router(ingest_router)
app.include_router(ratings_router)
app.include_router(browse_router)
app.include_router(query_router)
app.include_router(views_router)
app.include_router(people_router)
app.include_router(faces_router)
app.include_router(filters_router)
app.include_router(facets_router)
app.include_router(assets.router)
app.include_router(collections.router)
app.include_router(public_collections_router)
app.include_router(video.router)
app.include_router(keys.router)
app.include_router(me.router)
app.include_router(trash.router)
app.include_router(search_router)
app.include_router(similarity_router)
app.include_router(maintenance_router)
app.include_router(upgrade_router)
app.include_router(upkeep_router)


@app.get("/health")
def health() -> dict[str, str]:
    """Health check; no auth required."""
    return {"status": "ok"}
