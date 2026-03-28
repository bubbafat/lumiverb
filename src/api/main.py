"""FastAPI application entry point."""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.middleware import TenantResolutionMiddleware
from src.api.routers import admin, assets, keys, libraries, me, path_filters, scans, tenant, trash, video
from src.api.routers.auth import router as auth_router
from src.api.routers.users import router as users_router
from src.api.routers.artifacts import router as artifacts_router
from src.api.routers.ingest import router as ingest_router
from src.api.routers.maintenance import router as maintenance_router
from src.api.routers.upgrade import router as upgrade_router
from src.api.routers.search import router as search_router
from src.api.routers.facets import router as facets_router
from src.api.routers.similarity import router as similarity_router
from src.api.routers.upkeep import router as upkeep_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.environ.get("JWT_SECRET"):
        raise RuntimeError("JWT_SECRET environment variable is required but not set")
    yield


app = FastAPI(title="Lumiverb API", version="0.1.0", lifespan=lifespan)
app.add_middleware(TenantResolutionMiddleware)
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(admin.router)
app.include_router(tenant.router)
app.include_router(path_filters.router)
app.include_router(libraries.router)
app.include_router(scans.router)
app.include_router(artifacts_router)
app.include_router(ingest_router)
app.include_router(assets.router)
app.include_router(facets_router)
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
