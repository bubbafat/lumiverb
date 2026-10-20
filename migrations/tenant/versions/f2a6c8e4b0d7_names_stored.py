"""Lumiverb's own data stored by its names (ADR-024 phase 1, part 3).

Revision ID: f2a6c8e4b0d7
Revises: f8c4a2e6d0b5
Create Date: 2026-10-19

Every artifact, lineage producer, pause target, crash, settings key and job
kind the account keeps is renamed to the name of what it is
(docs/data-names.md): `vision` becomes `lumiverb.image.description`, its
producer `vision` the same name (a producer is named by what it makes), and
an installed plug-in's work `plugin_<id>` its plug-in's id (but for an id no
plug-in may have now: it stays as it was, and Lumiverb leaves the plug-in out).
Files keep their kinds (part 4): `uploads.artifact`, `artifacts.missing`.

Only the producer that makes each artifact now is renamed: (`transcript`,
`whisper`) becomes the name, but `''` (a failure), `person`, `unknown` and an
older producer stay, so nothing stale turns current and nothing a person
made, or chose to keep, is made again. The same rule renames every lineage
kept as JSON: a scene's description (`video_scenes.lineage`), a redo's
scenes being found (`video_index_chunks.run`), an open upload's
(`uploads.lineage`). The scheduler's last status (`scheduler.status`) has
its kinds renamed, so its learned paces carry over.

The mapping is written here, never imported (src/producers/names.py RENAMED
goes in part 5): an install that skips releases, a restored backup and a
downgrade still run this migration. tests/test_migration_names_stored.py
holds it equal to RENAMED while both exist.

Down: each renamed back, by the same mapping. A plug-in's work goes back
to `plugin_<id>`, whether the up renamed it or part 3's code wrote it; a
word no plug-in may have (every old id, `all`, `upkeep`, a pool, ...) stays
as it is. One the up left alone that could be a plug-in's id (a producer
removed before, its rows left behind) can't be told from a plug-in's work,
and goes to `plugin_<it>` too: both releases ignore an artifact they don't
know.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2a6c8e4b0d7"
down_revision: str | Sequence[str] | None = "f8c4a2e6d0b5"
branch_labels = None
depends_on = None

# Each artifact's old id, and its name.
ARTIFACTS: dict[str, str] = {
    "probe": "lumiverb.video.format",
    "capture": "lumiverb.capture_time",
    "proxy": "lumiverb.preview",
    "video_preview": "lumiverb.video.preview",
    "analysis_proxy": "lumiverb.video.analysis_proxy",
    "scenes": "lumiverb.video.scenes",
    "scene_vision": "lumiverb.video.scene_descriptions",
    "vision": "lumiverb.image.description",
    "ocr": "lumiverb.image.text",
    "clip": "lumiverb.image.likeness",
    "faces": "lumiverb.image.faces",
    "video_faces": "lumiverb.video.faces",
    "transcript": "lumiverb.video.transcript",
    "location": "lumiverb.location.guess",
}
# The producer that makes each now (by its artifact's old id): it alone is renamed, to the artifact's name.
PRODUCERS: dict[str, str] = {
    "probe": "ffprobe", "capture": "exiftool", "proxy": "proxy", "video_preview": "preview",
    "analysis_proxy": "analysis-proxy", "scenes": "scene-detect", "scene_vision": "scene-vision", "vision": "vision",
    "ocr": "ocr", "clip": "clip", "faces": "insightface", "video_faces": "insightface-video", "transcript": "whisper",
    "location": "location",
}
# The scheduler's kinds that weren't their artifact's id.
KINDS: dict[str, str] = {**ARTIFACTS, "render": ARTIFACTS["analysis_proxy"]}
del KINDS["analysis_proxy"], KINDS["proxy"], KINDS["video_preview"]

# An installed plug-in's work: `plugin_` and its id, each dash an underscore (an id has none), and the id.
_ADDED = re.compile(r"^plugin_([a-z][a-z0-9_]{0,39})$")
_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
# Ids no plug-in may have since part 1 (and any starting `lumiverb`): one installed before keeps its work under
# `plugin_<id>`, and Lumiverb leaves the plug-in out until it's installed under another id (ADR-024 section 8).
_RESERVED = frozenset({
    "all", "upkeep", "scans", "scan", "person", "unknown", "cpu", "exif", "transcripts", "analysis-proxy",
    "analysis_proxy", "capture", "clip", "exiftool", "faces", "ffprobe", "insightface", "insightface-video", "location",
    "ocr", "preview", "probe", "proxy", "render", "scene-detect", "scene-vision", "scene_rep", "scene_vision", "scenes",
    "thumbnail", "transcript", "video_faces", "video_preview", "vision", "whisper", "capture_time", "image",
    "metadata", "original", "people", "video",
})
# Lumiverb's pools keep their names (a pool is shared, so no one name fits it); a plug-in's is its work's.
_POOLS = ("scan", "probe", "exif", "render", "scenes", "vision", "clip", "faces", "transcripts", "cpu")


def _could_be_plugins(word: str) -> bool:
    """A plug-in may have this id: one installed now, its work named by it."""
    return bool(_PLUGIN_ID.match(word)) and not word.startswith("lumiverb") and word not in _RESERVED


def _added_id(old: str) -> str | None:
    match = _ADDED.match(old)
    plugin_id = match.group(1).replace("_", "-") if match else None
    return plugin_id if plugin_id is not None and _could_be_plugins(plugin_id) else None


def _added_down(new: str) -> str:
    return "plugin_" + new.replace("-", "_") if _could_be_plugins(new) else new


def _work_up(old: str) -> str:
    """An artifact (a pause target, a crash) as it's named now."""
    return ARTIFACTS.get(old) or _added_id(old) or old


def _work_down(new: str) -> str:
    olds = {v: k for k, v in ARTIFACTS.items()}
    return olds.get(new) or _added_down(new)


def _pool_up(old: str) -> str:
    return _added_id(old) or old


def _pool_down(new: str) -> str:
    return _added_down(new)  # Lumiverb's pools are reserved: they stay


def _kind_up(old: str) -> str:
    if old.startswith("redo_"):
        return "redo:" + _kind_up(old.removeprefix("redo_"))
    return KINDS.get(old) or _added_id(old) or old


def _kind_down(new: str) -> str:
    if new.startswith("redo:"):
        return "redo_" + _kind_down(new.removeprefix("redo:"))
    olds = {v: k for k, v in KINDS.items()}
    return olds.get(new) or _added_down(new)


def _rename_column(conn, table: str, column: str, way) -> None:
    """Each distinct value of a column, as `way` names it (a few values: one statement each)."""
    values = conn.execute(sa.text(f"SELECT DISTINCT {column} FROM {table}")).scalars().all()
    for value in values:
        renamed = way(value)
        if renamed != value:
            conn.execute(sa.text(f"UPDATE {table} SET {column} = :new WHERE {column} = :old"),
                         {"new": renamed, "old": value})


def _own_producers(conn, *, up: bool) -> None:
    """artifact_lineage's producer of Lumiverb's own work, the one that makes each artifact now only, by the
    artifact's old id: before the artifacts are renamed (up), after they're renamed back (down)."""
    for artifact, producer in PRODUCERS.items():
        name = ARTIFACTS[artifact]
        old, new = (producer, name) if up else (name, producer)
        conn.execute(sa.text("UPDATE artifact_lineage SET producer = :new WHERE artifact = :artifact AND producer = :old"),
                     {"new": new, "artifact": artifact, "old": old})


def _plugin_producers(conn, *, up: bool) -> None:
    """An installed plug-in's work's producer, which is its artifact: renamed with it, while they're the same
    (before the artifacts are renamed, either way)."""
    rows = conn.execute(sa.text("SELECT DISTINCT artifact FROM artifact_lineage WHERE producer = artifact")).scalars()
    for artifact in rows.all():
        renamed = _added_id(artifact) if up else _work_down(artifact)
        if renamed and renamed != artifact and (up or not renamed.startswith("lumiverb.")) \
                and artifact not in ARTIFACTS.values():
            conn.execute(sa.text("UPDATE artifact_lineage SET producer = :new WHERE artifact = :a AND producer = :a"),
                         {"new": renamed, "a": artifact})


def _json_lineage(conn, table: str, column: str, where: str, old: str, new: str) -> None:
    conn.execute(sa.text(f"UPDATE {table} SET {column} = jsonb_set({column}, '{{producer}}', to_jsonb(CAST(:new AS text)))"
                         f" WHERE {where} AND {column} ->> 'producer' = :old"), {"new": new, "old": old})


def _jsons(conn, *, up: bool) -> None:
    pairs = [("video_scenes", "lineage", "true", "scene_vision"),
             ("video_index_chunks", "run", "true", "scenes"),
             ("uploads", "lineage", "artifact = 'video_preview'", "video_preview"),
             ("uploads", "lineage", "artifact = 'analysis_proxy'", "analysis_proxy")]
    for table, column, where, artifact in pairs:
        producer, name = PRODUCERS[artifact], ARTIFACTS[artifact]
        _json_lineage(conn, table, column, where, *((producer, name) if up else (name, producer)))


def _settings_keys(conn, *, up: bool) -> None:
    """system_metadata's `producer.<artifact>` (an account's settings) and `.version_seen`, exact keys only (a
    name can be the start of another's: lumiverb.location, lumiverb.location.guess)."""
    keys = conn.execute(sa.text("SELECT key FROM system_metadata WHERE key LIKE 'producer.%'")).scalars().all()
    for key in keys:
        rest = key.removeprefix("producer.")
        artifact, seen = (rest.removesuffix(".version_seen"), ".version_seen") if rest.endswith(".version_seen") \
            else (rest, "")
        renamed = (_work_up(artifact) if up else _work_down(artifact))
        if renamed != artifact:
            conn.execute(sa.text("UPDATE system_metadata SET key = :new WHERE key = :old"),
                         {"new": f"producer.{renamed}{seen}", "old": key})


def _status(conn, *, up: bool) -> None:
    """The scheduler's last status: its kinds (and a plug-in's pool and waits) renamed, its learned paces kept."""
    raw = conn.execute(sa.text("SELECT value FROM system_metadata WHERE key = 'scheduler.status'")).scalar()
    try:
        status = json.loads(raw) if raw else None
    except ValueError:
        return
    if not isinstance(status, dict):
        return
    kind, work = (_kind_up, _work_up) if up else (_kind_down, _work_down)
    for field in ("running", "waiting", "unlisted", "pace"):
        if isinstance(status.get(field), dict):
            status[field] = {kind(k): v for k, v in status[field].items()}
    pool = _pool_up if up else _pool_down
    if isinstance(status.get("plugin_waits"), dict):
        status["plugin_waits"] = {work(k): v for k, v in status["plugin_waits"].items()}
    if isinstance(status.get("pools"), dict):
        status["pools"] = {pool(k): v for k, v in status["pools"].items()}
    for field in ("jobs", "held"):
        for row in status.get(field) or []:
            if isinstance(row, dict) and isinstance(row.get("kind"), str):
                row["kind"] = kind(row["kind"])
    conn.execute(sa.text("UPDATE system_metadata SET value = :v WHERE key = 'scheduler.status'"),
                 {"v": json.dumps(status)})


def upgrade() -> None:
    conn = op.get_bind()
    _own_producers(conn, up=True)
    _plugin_producers(conn, up=True)
    _rename_column(conn, "artifact_lineage", "artifact", _work_up)
    _rename_column(conn, "producer_crashes", "artifact", _work_up)
    _rename_column(conn, "producer_pauses", "target", _work_up)
    _jsons(conn, up=True)
    _settings_keys(conn, up=True)
    _status(conn, up=True)


def downgrade() -> None:
    conn = op.get_bind()
    _plugin_producers(conn, up=False)
    _rename_column(conn, "artifact_lineage", "artifact", _work_down)
    _own_producers(conn, up=False)
    _rename_column(conn, "producer_crashes", "artifact", _work_down)
    _rename_column(conn, "producer_pauses", "target", _work_down)
    _jsons(conn, up=False)
    _settings_keys(conn, up=False)
    _status(conn, up=False)
