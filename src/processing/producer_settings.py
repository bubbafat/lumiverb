"""What the server says each producer makes its artifact with now, and the
lineage each write carries (ADR-016 phase 3).

The worker makes artifacts with the server's settings (GET /v1/producers),
so what it records is what's current. Fetched once per run; a server that
predates producers gets the registry's defaults.
"""

from __future__ import annotations

import logging
from typing import Any

from src.shared.producers import PRODUCERS, effective_settings, lineage, use_settings

logger = logging.getLogger(__name__)


class ProducerSettings:
    def __init__(self, client: Any | None = None, *, fetch: bool = True) -> None:
        """fetch=False: the registry's defaults until refresh() reads the server."""
        self._client = client
        self._settings: dict[str, dict[str, Any]] = {a: effective_settings(a) for a in PRODUCERS}
        self._uses: dict[str, dict[str, Any]] = {a: use_settings(a) for a in PRODUCERS}
        if fetch:
            self.refresh()

    def refresh(self) -> bool:
        """Read the server's settings again. Built aside and swapped in at once,
        so a job running meanwhile never sees a mix; when the server can't say,
        what was read before stays (the registry's defaults the first time).
        True when the server said."""
        fresh = {a: effective_settings(a) for a in PRODUCERS}
        uses = {a: use_settings(a) for a in PRODUCERS}
        if self._client is None:
            self._settings, self._uses = fresh, uses
            return False
        try:
            data = self._client.get("/v1/producers", params={"counts": "false"}).json()
        except Exception as e:  # noqa: BLE001 — an older server, or not answering yet
            logger.info("producers: the server didn't say (%s); keeping the settings read before", e)
            if not self._settings:
                self._settings = fresh
            return False
        for p in data.get("producers", []):
            if p.get("artifact") in fresh and isinstance(p.get("settings"), dict):
                fresh[p["artifact"]] = dict(p["settings"])
            # Settings that don't remake (not in lineage) are among its fields.
            for f in p.get("fields") or []:
                if p.get("artifact") in uses and f.get("key") in uses[p["artifact"]] and f.get("remakes") is False:
                    uses[p["artifact"]][f["key"]] = f.get("value")
        self._settings, self._uses = fresh, uses
        return True

    def settings(self, artifact: str) -> dict[str, Any]:
        return dict(self._settings[artifact])

    def uses(self, artifact: str) -> dict[str, Any]:
        """Its settings that don't remake (not in lineage): what's done with what's made."""
        return dict(self._uses[artifact])

    def with_model(self, artifact: str, model: str) -> dict[str, Any]:
        """The settings with the model actually used: the account's, as read
        for this step (it may have changed since these settings were read)."""
        return {**self.settings(artifact), "model": model}

    def lineage(self, artifact: str, source_sha256: str | None, used: dict[str, Any] | None = None) -> dict:
        """What a write records: this producer, its version, the hash of the
        settings actually used (the server's unless `used` says otherwise),
        and the source file's SHA-256."""
        return lineage(artifact, self.settings(artifact) if used is None else used, source_sha256)
