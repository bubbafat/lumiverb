"""An installed plug-in's work for one job (ADR-023 phase 2): the job's
clips' inputs from the API, one request to the plug-in over its socket
(POST /v1/work), and what it gave back saved through the API, every
clip's in one request.

The plug-in is handed its declared inputs, the account's settings (and the
server's) and ids that mean nothing outside the request: never which
account, a path or a key. Its settings, secrets too, are read here from the
databases, never sent over the API. A photo's preview goes as it's kept,
base64, with its type.

Whose an error is:
- the plug-in isn't running or doesn't answer (its socket, its time limit,
  an HTTP error): nobody's, its clips wait (plugin_down), uncharged;
- it says a clip's error is the clip's ({"error": {"whose": "clip"}}):
  charged to the clip, as any failure;
- it says it's its own: the clip waits, uncharged (held);
- it answers what the protocol doesn't say (a clip left out, outputs it
  didn't declare, too long, HTML): its fault, a crash, uncharged but
  counted, so a plug-in that keeps doing it has the clip charged in the end.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

from src.producers.runner import Died, Failed, Waits, Work
from src.shared.waits import PLUGIN_DOWN

logger = logging.getLogger(__name__)

# A job's request: the plug-in has this long to answer for all its clips.
WORK_TIMEOUT_SEC = 120.0
ANSWER_MAX_BYTES = 8 * 1024 * 1024


def _image_type(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "application/octet-stream"


class PluginWork(Work):
    together = True  # the job's clips saved in one request

    def __init__(self, acct: Any, job: Any) -> None:
        from src.server.scheduler.kinds import KINDS
        from src.shared.producers import PRODUCERS

        super().__init__(acct, job)
        self.artifact = KINDS[job.kind].artifact
        self.plugin_id = PRODUCERS[self.artifact].plugin
        self._answers: dict[str, dict[str, Any]] | None = None
        self._trouble: Exception | None = None
        self._used: dict[str, Any] = {}  # the settings in its lineage, as the plug-in was handed them

    # -- the one request ----------------------------------------------------

    def _manifest(self) -> Any:
        from src.server.plugin_catalog import find

        entry = find(self.plugin_id)
        if entry is None or entry.installed.manifest.work is None:
            raise Waits(f"{self.plugin_id} isn't installed now")
        return entry.installed.manifest

    def _settings(self, manifest: Any) -> dict[str, Any]:
        """What it's handed: every setting, the account's as chosen here and the server's. Those that remake
        are what its lineage says it was made with, as the server works them out (lineage.desired)."""
        from src.server.database import get_control_session, get_tenant_session
        from src.server.plugin_settings import ACCOUNT, SERVER, PluginSettingsRepository, values
        from src.shared.producers import effective_settings

        with get_tenant_session(self.acct.tenant_id) as session:
            account = PluginSettingsRepository(session, ACCOUNT).get(self.plugin_id)
        with get_control_session() as control:
            server = PluginSettingsRepository(control, SERVER).get(self.plugin_id)
        self._used = effective_settings(self.artifact, account)
        return values(manifest, account, server)

    def _ready(self, manifest: Any) -> Path:
        """Its socket, once the runner says it's running (Waits, plugin_down, otherwise)."""
        from src.server.config import get_settings
        from src.server.plugin_client import socket_path
        from src.server.plugin_runner import not_ready, read_state

        data = Path(get_settings().data_dir)
        if (why := not_ready(read_state(data), self.plugin_id)) is not None:
            raise Waits(f"{manifest.title}: {why}", reason=PLUGIN_DOWN)
        return socket_path(data, self.plugin_id)

    def _inputs(self) -> dict[str, dict[str, Any]]:
        r = self.acct.client.raw("POST", f"/v1/plugins/{self.plugin_id}/inputs", retry=True,
                                 json={"asset_ids": [c["asset_id"] for c in self.job.items]})
        if r.status_code in (404, 409):
            # Removed, or turned off here, since it was handed out: tried again later.
            raise Waits(((r.json() or {}).get("error") or {}).get("message") or f"HTTP {r.status_code}")
        return self.acct.client.checked(r).json()["clips"]

    def answers(self) -> dict[str, dict[str, Any]]:
        """Each clip's answer by asset id: {"inputs_hash", "outputs"} or {"error": {"whose", "message"}}; a
        clip it doesn't apply to now is left out. What went wrong for the whole request is raised again for
        each clip."""
        if self._trouble is not None:
            raise self._trouble
        if self._answers is None:
            try:
                self._answers = self._ask()
            except Exception as exc:  # noqa: BLE001 — each clip judged by it (the runner's rules)
                self._trouble = exc
                raise
        return self._answers

    def _ask(self) -> dict[str, dict[str, Any]]:
        from src.plugins.manifest import checked_outputs
        from src.server.plugin_client import PluginAnswerError, ask

        manifest = self._manifest()
        sock = self._ready(manifest)
        found = self._inputs()
        clips = [c for c in self.job.items if c["asset_id"] in found]
        if not clips:
            return {}
        ids = {str(n): c["asset_id"] for n, c in enumerate(clips, 1)}
        sent = []
        for n, clip in zip(ids, clips, strict=True):
            one: dict[str, Any] = {"id": n, **found[clip["asset_id"]]["inputs"]}
            if "preview" in manifest.work.inputs:
                data = self.inputs.preview_bytes(clip)
                if data is None:
                    raise Waits("its preview isn't there yet")
                one["preview"] = {"type": _image_type(data), "base64": base64.b64encode(data).decode()}
            sent.append(one)
        try:
            answer = ask(sock, "POST", "/v1/work", body={"settings": self._settings(manifest), "clips": sent},
                         timeout=WORK_TIMEOUT_SEC, max_bytes=ANSWER_MAX_BYTES)
        except PluginAnswerError as exc:
            raise Waits(f"{manifest.title} didn't answer: {exc}", reason=PLUGIN_DOWN) from exc
        said = answer.get("clips")
        if not isinstance(said, dict):
            raise Died(f"{manifest.title} answered what the protocol doesn't say: no clips")
        out: dict[str, dict[str, Any]] = {}
        for n, asset_id in ids.items():
            mine = said.get(n)
            if not isinstance(mine, dict):
                out[asset_id] = {"broken": f"{manifest.title} said nothing of it"}
            elif isinstance(mine.get("error"), dict):
                error = mine["error"]
                out[asset_id] = {"error": {"whose": "clip" if error.get("whose") == "clip" else "plugin",
                                           "message": str(error.get("message") or "it failed")[:500]}}
            else:
                try:
                    out[asset_id] = {"inputs_hash": found[asset_id]["inputs_hash"],
                                     "outputs": checked_outputs(manifest.work, mine.get("outputs"))}
                except ValueError as exc:
                    out[asset_id] = {"broken": f"{manifest.title} answered what it doesn't declare: {exc}"}
        return out

    # -- each clip ----------------------------------------------------------

    def make(self, clip: dict) -> dict[str, Any] | None:
        mine = self.answers().get(clip["asset_id"])
        if mine is None:
            return None  # out of sight, or not one it applies to now: nothing to make
        if "broken" in mine:
            raise Died(mine["broken"])
        if "error" in mine:
            if mine["error"]["whose"] == "clip":
                raise Failed(mine["error"]["message"])
            raise Waits(mine["error"]["message"])
        return mine

    def save(self, client: Any, made: list[tuple[dict, Any]]) -> None:
        items = [{"asset_id": clip["asset_id"], "inputs_hash": result["inputs_hash"], "outputs": result["outputs"],
                  "source_hash": clip.get("content_hash")} for clip, result in made]
        from src.shared.producers import lineage

        # Its own lineage, from the settings it was handed (each item gives its clip's file).
        client.post(f"/v1/plugins/{self.plugin_id}/outputs",
                    json={"items": items, "lineage": lineage(self.artifact, self._used, None)})
