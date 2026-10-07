"""Editor exports: a project becomes a bin of master clips (ADR-016 phase 1).

Providers are registered by id, so a new format or a template fix doesn't
touch the endpoint. Each one renders an ExportBin to bytes.
"""

from __future__ import annotations

from src.server.export.base import ExportBin, ExportClip, ExportProvider, timecode_to_frames
from src.server.export.fcp7 import Fcp7XmlProvider
from src.server.export.fcpxml import FcpxmlProvider

EXPORT_PROVIDERS: dict[str, ExportProvider] = {
    p.id: p for p in (Fcp7XmlProvider(), FcpxmlProvider())
}

__all__ = [
    "EXPORT_PROVIDERS",
    "ExportBin",
    "ExportClip",
    "ExportProvider",
    "timecode_to_frames",
]
