"""FastAPI application entry point."""

from fastapi import FastAPI

from src.api.middleware import TenantResolutionMiddleware
from src.api.routers import admin, assets, jobs, keys, libraries, path_filters, scans, tenant, trash, video
from src.api.routers.search import router as search_router
from src.api.routers.similarity import router as similarity_router

app = FastAPI(title="Lumiverb API", version="0.1.0")
app.add_middleware(TenantResolutionMiddleware)
app.include_router(admin.router)
app.include_router(tenant.router)
app.include_router(jobs.router)
app.include_router(path_filters.router)
app.include_router(libraries.router)
app.include_router(scans.router)
app.include_router(assets.router)
app.include_router(video.router)
app.include_router(keys.router)
app.include_router(trash.router)
app.include_router(search_router)
app.include_router(similarity_router)


@app.get("/health")
def health() -> dict[str, str]:
    """Health check; no auth required."""
    return {"status": "ok"}
