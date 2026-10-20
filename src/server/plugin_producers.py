"""Each installed plug-in's work as a producer (ADR-023 phase 2): made from
its manifest's `work`, added to the producers while the API and the
scheduler run (src/producers/__init__.py use_added), so its work is queued,
paused, counted, held back with why, charged its failures and remade as any
producer's is.

Its spec, from the manifest:

- its artifact (its producer in lineage, its kind and its pool too) is its
  plug-in's id (ADR-024: `plugin_<id>` before part 3; an id Lumiverb uses
  is refused at install, src/producers/names.py RESERVED);
- it applies to its media's clips that have its inputs
  (src/server/repository/clip_inputs.py), while it's on in the account
  (its row, else the server admin's default: repository/plugins.py on_sql);
- a clip has it made when its plugin_outputs row was made from the inputs
  the clip has now (their hash);
- its version is its work's (the manifest's work.version): a new one holds
  the remakes for an admin to start, as for any producer;
- the settings in its lineage are its settings that remake (an account's,
  kept in plugin_settings): changing one asks what becomes of what was made;
- what it reads is its inputs' names (manifest.INPUT_NAMES): a photo's preview, when it reads it, is
  made first (its needs, worked out from them);
- its work is src/producers/plugin_work.py's, a pool of its own, one job at
  a time (a plug-in is one process), its manifest's batch of clips a job.

The API reads the installed plug-ins again at most every REFRESH_EVERY_SEC
(``keep_current``, on an account's requests), and the scheduler as it lists
the accounts (scheduler/service.py Accounts): a plug-in installed shows on
Processing and starts working within about half a minute.
"""

from __future__ import annotations

import logging
import threading
import time

from src.plugins.manifest import INPUT_NAMES
from src.producers import INSTALLED, use_added
from src.producers.contract import FIND, Pool, ProducerSpec, Setting
from src.server.plugin_catalog import Installed, installed
from src.server.repository.clip_inputs import hash_sql, present_sql
from src.server.repository.plugins import on_sql

logger = logging.getLogger(__name__)

REFRESH_EVERY_SEC = 30.0
WORK = "src.producers.plugin_work:PluginWork"
_KIND = {"int": "int", "float": "float", "bool": "bool", "text": "text", "choice": "text"}


def artifact_of(plugin_id: str) -> str:
    """Its work's artifact: its id (ADR-024)."""
    return plugin_id


def spec_for(plugin: Installed) -> ProducerSpec:
    """The producer of an installed plug-in's work (it has one: manifest.work)."""
    m = plugin.manifest
    work = m.work
    if work is None:
        raise ValueError(f"{m.plugin_id} has no work")
    artifact = artifact_of(m.plugin_id)
    media = ", ".join(f"'{x}'" for x in work.media)
    return ProducerSpec(
        artifact=artifact, version=work.version, media=work.media, title=work.title,
        applies=(f"({on_sql(m.spec(), plugin.default)})"
                 f" AND a.media_type IN ({media}) AND ({present_sql(work.inputs)})"),
        made=("EXISTS (SELECT 1 FROM plugin_outputs po WHERE po.asset_id = a.asset_id"
              f" AND po.plugin_id = '{m.plugin_id}' AND po.inputs_hash = {hash_sql(work.inputs)})"),
        settings=tuple(Setting(s.key, s.default, s.label, kind=_KIND[s.type], minimum=s.minimum,
                               maximum=s.maximum, unit=s.unit, advanced=s.advanced)
                       for s in m.settings if s.remakes),
        reads=tuple(INPUT_NAMES[i] for i in work.inputs), plugin=m.plugin_id, order=900,
        run=WORK, tier=FIND, pool=Pool(artifact, slots=1),
        batch=work.batch,
    )


def refresh() -> bool:
    """The producers of the plug-ins installed now, in place of those before. Whether they changed."""
    specs = [spec_for(p) for p in installed().values() if p.manifest.work is not None]
    changed = use_added(specs, by=INSTALLED)
    if changed:
        logger.info("Plug-ins' work: %s", ", ".join(sorted(s.title for s in specs)) or "none")
    return changed


_next_at = 0.0
_failing = False
_gate = threading.Lock()


def keep_current() -> None:
    """refresh(), at most every REFRESH_EVERY_SEC (the first call at once). Trouble reading what's installed
    is said once, and the producers stay as they were."""
    global _next_at, _failing
    now = time.monotonic()
    if now < _next_at:
        return
    with _gate:
        if now < _next_at:
            return
        _next_at = now + REFRESH_EVERY_SEC
    try:
        refresh()
    except Exception:  # noqa: BLE001 — what was read before stands
        if not _failing:
            logger.exception("Reading the installed plug-ins failed; their work stays as it was")
        _failing = True
    else:
        _failing = False
