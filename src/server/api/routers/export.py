"""Editor export formats, for the format chooser (ADR-016 phase 1)."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from src.server.export import EXPORT_PROVIDERS

router = APIRouter(prefix="/v1/export", tags=["export"])


class ExportFormat(BaseModel):
    id: str
    label: str
    file_extension: str


class ExportFormatsResponse(BaseModel):
    items: list[ExportFormat]


@router.get("/formats", response_model=ExportFormatsResponse)
def list_export_formats() -> ExportFormatsResponse:
    """Formats GET /v1/projects/{id}/export accepts, in display order."""
    return ExportFormatsResponse(
        items=[
            ExportFormat(id=p.id, label=p.label, file_extension=p.file_extension)
            for p in EXPORT_PROVIDERS.values()
        ]
    )
