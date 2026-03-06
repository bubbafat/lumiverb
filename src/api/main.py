"""FastAPI application entry point."""

from fastapi import FastAPI

from src.api.middleware import TenantResolutionMiddleware
from src.api.routers import admin, libraries

app = FastAPI(title="Lumiverb API", version="0.1.0")
app.add_middleware(TenantResolutionMiddleware)
app.include_router(admin.router)
app.include_router(libraries.router)


@app.get("/health")
def health() -> dict[str, str]:
    """Health check; no auth required."""
    return {"status": "ok"}
