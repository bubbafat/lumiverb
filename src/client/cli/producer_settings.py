"""What the server says each producer makes its artifact with now, and the
lineage each write carries (ADR-016 phase 3).

The worker makes artifacts with the server's settings (GET /v1/producers),
so what it records is what's current. Fetched once per run; a server that
predates producers gets the registry's defaults.
"""

from __future__ import annotations

import logging
from typing import Any

from src.shared.producers import PRODUCERS, effective_settings, lineage

logger = logging.getLogger(__name__)


class ProducerSettings:
    def __init__(self, client: Any | None = None) -> None:
        self._client = client
        self._settings: dict[str, dict[str, Any]] = {}
        self.refresh()

    def refresh(self) -> None:
        self._settings = {a: effective_settings(a) for a in PRODUCERS}
        if self._client is None:
            return
        try:
            data = self._client.get("/v1/producers", params={"counts": "false"}).json()
        except Exception as e:  # noqa: BLE001 — an older server: the registry's defaults
            logger.info("producers: the server didn't say (%s); using the registry's settings", e)
            return
        for p in data.get("producers", []):
            if p.get("artifact") in self._settings and isinstance(p.get("settings"), dict):
                self._settings[p["artifact"]] = dict(p["settings"])

    def settings(self, artifact: str) -> dict[str, Any]:
        return dict(self._settings[artifact])

    def lineage(self, artifact: str, source_sha256: str | None, used: dict[str, Any] | None = None) -> dict:
        """What a write records: this producer, its version, the hash of the
        settings actually used (the server's unless `used` says otherwise),
        and the source file's SHA-256."""
        return lineage(artifact, self.settings(artifact) if used is None else used, source_sha256)
