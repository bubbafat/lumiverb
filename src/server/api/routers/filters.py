"""Filter capabilities endpoint — returns the list of known filter types."""

from fastapi import APIRouter

from src.server.models.filter_registry import capabilities

router = APIRouter(prefix="/v1/filters", tags=["filters"])


@router.get("/capabilities")
def get_filter_capabilities() -> dict:
    """Return all known filter types with their prefix, label, and value kind.

    Clients use this to render filter UI generically — no client-side knowledge
    of individual filter types is needed.
    """
    return {"filters": capabilities()}
