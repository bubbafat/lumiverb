"""FastAPI application entry point."""

from fastapi import FastAPI

from src.api.routers import admin

app = FastAPI(title="Lumiverb API", version="0.1.0")
app.include_router(admin.router)


@app.get("/health")
def health() -> dict[str, str]:
    """Health check; no auth required."""
    return {"status": "ok"}
