"""EXIF metadata worker. API-only — no direct DB access."""

from __future__ import annotations

import logging
from pathlib import Path

from src.workers.base import BaseWorker
from src.workers.exif_extract import (
    compute_sha256,
    extract_exif,
    parse_gps,
    parse_taken_at,
)

logger = logging.getLogger(__name__)


class ExifWorker(BaseWorker):
    job_type = "exif"

    def process(self, job: dict) -> dict:
        rel_path = job["rel_path"]
        root_path = job["root_path"]

        root = Path(root_path).resolve()
        source_path = (root / rel_path).resolve()
        if not source_path.is_relative_to(root):
            raise ValueError(f"rel_path escapes library root: {rel_path!r}")
        if not source_path.exists():
            raise FileNotFoundError(f"Source file not found: {source_path}")

        exif_data = extract_exif(source_path)
        sha256 = compute_sha256(source_path)
        gps_lat, gps_lon = parse_gps(exif_data)
        taken_at = parse_taken_at(exif_data)

        return {
            "sha256": sha256,
            "exif": exif_data,
            "camera_make": exif_data.get("Make"),
            "camera_model": exif_data.get("Model"),
            "taken_at": taken_at.isoformat() if taken_at else None,
            "gps_lat": gps_lat,
            "gps_lon": gps_lon,
        }
