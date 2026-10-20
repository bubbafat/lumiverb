# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Tenant migration f2a6c8e4b0d7: Lumiverb's own data stored by its names
(ADR-024 phase 1 part 3).

- Its mapping is its own, and while src/producers/names.py RENAMED exists the
  two are the same.
- An account from before part 3 upgraded straight to the head: every row it
  renames, written out here as each is named after (never read from RENAMED,
  so the test stays when part 5 removes it), and what it keeps; down, every
  row as it was.
- An account made through today's code (lineage made, empty, failed, stale,
  an older version, new files only, a new version held, pauses of work and
  redo, crashes, settings, a plug-in's work, scenes and their run, an upload
  open across it), taken down and up again: every row, every producer's
  counts, waits and switches and every kind's due clips as they were, no new
  version held, and the upload finished after.
- No old id left where an artifact, a producer or a kind belongs, in every
  text and jsonb column and system_metadata, on that account and on the
  tester's (tests/plugins/tester_named_data_seeded.json, written from
  v0.1.104's own code paths) upgraded to the head.

Runs Alembic against throwaway databases only.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest
from sqlalchemy import bindparam, create_engine, text
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from tests.clips import make_clip
from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_migration_lineage import _alembic

REPO = Path(__file__).resolve().parents[1]
BEFORE, AFTER = "f8c4a2e6d0b5", "f2a6c8e4b0d7"

# What part 3 renamed, as v0.1.104 kept it: every artifact's id, the producer that made each, and the kinds
# that weren't an artifact's id. Written out (never imported) so the net stays when RENAMED goes.
OLD_ARTIFACTS = {"probe", "capture", "proxy", "video_preview", "analysis_proxy", "scenes", "scene_vision", "vision",
                 "ocr", "clip", "faces", "video_faces", "transcript", "location"}
CURRENT = {"probe": "ffprobe", "capture": "exiftool", "proxy": "proxy", "video_preview": "preview",
           "analysis_proxy": "analysis-proxy", "scenes": "scene-detect", "scene_vision": "scene-vision",
           "vision": "vision", "ocr": "ocr", "clip": "clip", "faces": "insightface", "video_faces": "insightface-video",
           "transcript": "whisper", "location": "location"}
OLD_KINDS = (OLD_ARTIFACTS - {"proxy", "video_preview", "analysis_proxy"}) | {"render"}
OLD_WORDS = OLD_ARTIFACTS | set(CURRENT.values()) | OLD_KINDS
_OLD_KEY = re.compile(rf"^(producer\.({'|'.join(OLD_ARTIFACTS)}|plugin_[a-z0-9_]+)(\.version_seen)?"
                      rf"|redo_({'|'.join(OLD_KINDS)})|plugin_(?!waits$)[a-z][a-z0-9_]*)$")  # plugin_waits: a field


def _migration():
    path = REPO / "migrations" / "tenant" / "versions" / "f2a6c8e4b0d7_names_stored.py"
    spec = importlib.util.spec_from_file_location("names_stored", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_the_migration_s_mapping_is_renamed():
    # Its mapping is its own (part 5 deletes RENAMED); while both exist they're the same.
    from src.producers import registry
    from src.producers.names import ARTIFACTS, KINDS, PRODUCERS, RESERVED

    m = _migration()
    assert dict(ARTIFACTS) == m.ARTIFACTS
    assert {(a, p): ARTIFACTS[a] for a, p in m.PRODUCERS.items()} == dict(PRODUCERS)
    assert dict(KINDS) == m.KINDS
    assert set(m._POOLS) == {p.pool.name for p in registry().values() if p.pool and not p.plugin} | {"scan"}
    assert m._RESERVED == RESERVED
    # Down: a word no plug-in may have stays, every way; one that could be a plug-in's id is its work.
    for word in sorted(RESERVED):
        assert (m._work_down(word), m._kind_down(word), m._pool_down(word)) == (word, word, word), word
    assert (m._work_down("my-facts"), m._kind_down("redo:my-facts")) == ("plugin_my_facts", "redo_plugin_my_facts")
    # And this file's own, which outlives both.
    assert (set(m.ARTIFACTS), m.PRODUCERS) == (OLD_ARTIFACTS, CURRENT)


# ---------------------------------------------------------------------------
# An account from before part 3, upgraded straight to the head
# ---------------------------------------------------------------------------


def _fresh(pg) -> str:
    url = _ensure_psycopg2(pg.get_connection_url())
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    engine.dispose()
    return url


# (clip, artifact, producer, outcome) as v0.1.104 kept them, and as the head does.
LINEAGE = [
    ("photo", "proxy", "proxy", "ok", "lumiverb.preview", "lumiverb.preview"),
    ("photo", "capture", "exiftool", "ok", "lumiverb.capture_time", "lumiverb.capture_time"),
    ("photo", "vision", "vision", "ok", "lumiverb.image.description", "lumiverb.image.description"),
    ("photo", "ocr", "", "failed", "lumiverb.image.text", ""),  # a failure says no producer
    ("photo", "clip", "clip", "empty", "lumiverb.image.likeness", "lumiverb.image.likeness"),
    ("photo", "faces", "insightface", "ok", "lumiverb.image.faces", "lumiverb.image.faces"),
    ("photo", "location", "location", "ok", "lumiverb.location.guess", "lumiverb.location.guess"),
    ("photo", "plugin_my_facts", "plugin_my_facts", "ok", "my-facts", "my-facts"),  # a plug-in's work
    ("other", "vision", "person", "ok", "lumiverb.image.description", "person"),  # a person's
    ("other", "ocr", "vision", "ok", "lumiverb.image.text", "vision"),  # another producer's: stays stale
    ("other", "clip", "unknown", "ok", "lumiverb.image.likeness", "unknown"),
    ("other", "plugin_my_facts", "person", "ok", "my-facts", "person"),
    ("video", "probe", "ffprobe", "ok", "lumiverb.video.format", "lumiverb.video.format"),
    ("video", "video_preview", "preview", "ok", "lumiverb.video.preview", "lumiverb.video.preview"),
    ("video", "analysis_proxy", "analysis-proxy", "ok", "lumiverb.video.analysis_proxy",
     "lumiverb.video.analysis_proxy"),
    ("video", "scenes", "scene-detect", "ok", "lumiverb.video.scenes", "lumiverb.video.scenes"),
    ("video", "scene_vision", "scene-vision", "ok", "lumiverb.video.scene_descriptions",
     "lumiverb.video.scene_descriptions"),
    ("video", "video_faces", "insightface-video", "ok", "lumiverb.video.faces", "lumiverb.video.faces"),
    ("video", "transcript", "whisper", "ok", "lumiverb.video.transcript", "lumiverb.video.transcript"),
    ("dubbed", "transcript", "person", "ok", "lumiverb.video.transcript", "person"),
    ("dubbed", "scenes", "pyscenedetect", "ok", "lumiverb.video.scenes", "pyscenedetect"),  # an older producer
    # A plug-in installed before part 1 with an id no plug-in may have now: its work stays as it was (section 8).
    ("photo", "plugin_faces", "plugin_faces", "ok", "plugin_faces", "plugin_faces"),
]
PAUSES = [  # (target, scope, paused_by) before, and the target after
    ("vision", "work", "usr_1", "lumiverb.image.description"),
    ("transcript", "redo", None, "lumiverb.video.transcript"),  # a new version's hold
    ("analysis_proxy", "redo", "usr_1", "lumiverb.video.analysis_proxy"),  # new files only
    ("plugin_my_facts", "work", "usr_1", "my-facts"),
    ("plugin_faces", "work", "usr_1", "plugin_faces"),
    ("all", "work", "usr_1", "all"),
    ("upkeep", "work", "usr_1", "upkeep"),
    ("scans", "work", "usr_1", "scans"),  # left behind (each library's since aec154348364): stays, both ways
]
CRASHES = [("photo", "faces", "lumiverb.image.faces"), ("photo", "plugin_my_facts", "my-facts")]
KEYS = [  # system_metadata's keys before, and after
    ("producer.vision", "producer.lumiverb.image.description"),
    ("producer.vision.version_seen", "producer.lumiverb.image.description.version_seen"),
    ("producer.location", "producer.lumiverb.location.guess"),
    ("producer.location.version_seen", "producer.lumiverb.location.guess.version_seen"),
    ("producer.analysis_proxy", "producer.lumiverb.video.analysis_proxy"),
    ("producer.faces", "producer.lumiverb.image.faces"),
    ("producer.plugin_my_facts.version_seen", "producer.my-facts.version_seen"),
    ("producer.plugin_faces.version_seen", "producer.plugin_faces.version_seen"),
    ("artifacts.missing", "artifacts.missing"),  # files keep their kinds (part 4)
    ("search.synced", "search.synced"),
]
STATUS_BEFORE = {
    "at": "2026-10-19T12:00:00+00:00", "running": {"render": 1, "redo_vision": 1, "plugin_my_facts": 1, "scan": 1},
    "waiting": {"capture": 3, "redo_scene_vision": 2}, "unlisted": {"ocr": "boom"},
    "pace": {"render": 0.5, "vision": 6.0, "plugin_my_facts": 2.0},
    "pools": {"render": [1, 2], "vision": [1, 3], "clip": [0, 1], "plugin_my_facts": [1, 1]},
    "jobs": [{"kind": "render", "units": 30.0, "elapsed": 2.0}, {"kind": "redo_vision", "units": 1.0, "elapsed": 1.0}],
    "held": [{"kind": "capture", "reason": "held", "library_id": "lib_a", "clips": 1}],
    "plugin_waits": {"plugin_my_facts": "starting"}, "ai_waits": {"vision": "unusable"},
}
STATUS_AFTER = {
    "at": "2026-10-19T12:00:00+00:00",
    "running": {"lumiverb.video.analysis_proxy": 1, "redo:lumiverb.image.description": 1, "my-facts": 1, "scan": 1},
    "waiting": {"lumiverb.capture_time": 3, "redo:lumiverb.video.scene_descriptions": 2},
    "unlisted": {"lumiverb.image.text": "boom"},
    "pace": {"lumiverb.video.analysis_proxy": 0.5, "lumiverb.image.description": 6.0, "my-facts": 2.0},
    "pools": {"render": [1, 2], "vision": [1, 3], "clip": [0, 1], "my-facts": [1, 1]},  # Lumiverb's pools keep theirs
    "jobs": [{"kind": "lumiverb.video.analysis_proxy", "units": 30.0, "elapsed": 2.0},
             {"kind": "redo:lumiverb.image.description", "units": 1.0, "elapsed": 1.0}],
    "held": [{"kind": "lumiverb.capture_time", "reason": "held", "library_id": "lib_a", "clips": 1}],
    "plugin_waits": {"my-facts": "starting"}, "ai_waits": {"vision": "unusable"},  # AI jobs keep theirs
}


def _seed_before(url: str) -> dict[str, str]:
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at)"
                          " VALUES ('lib_a', 'a', '/a', 'active', now(), now())"))
    with Session(engine) as session:
        clips = {"photo": make_clip(session, "lib_a", "photo.jpg"), "other": make_clip(session, "lib_a", "other.jpg"),
                 "video": make_clip(session, "lib_a", "video.mov", media_type="video"),
                 "dubbed": make_clip(session, "lib_a", "dubbed.mov", media_type="video")}
        session.commit()
    with engine.begin() as conn:
        for clip, artifact, producer, outcome, *_ in LINEAGE:
            conn.execute(text("INSERT INTO artifact_lineage (asset_id, artifact, producer, producer_version,"
                              " settings_hash, source_hash, produced_at, outcome, attempts) VALUES (:a, :b, :p, '1',"
                              " 'h', 'sha256:x', now(), :o, 0)"), {"a": clips[clip], "b": artifact, "p": producer,
                                                                    "o": outcome})
        for target, scope, by, _ in PAUSES:
            conn.execute(text("INSERT INTO producer_pauses (target, scope, paused_by, paused_at)"
                              " VALUES (:t, :s, :by, now())"), {"t": target, "s": scope, "by": by})
        for clip, artifact, _ in CRASHES:
            conn.execute(text("INSERT INTO producer_crashes (asset_id, artifact, crashes, error, updated_at)"
                              " VALUES (:a, :b, 2, 'gone', now())"), {"a": clips[clip], "b": artifact})
        for key, _ in KEYS:
            conn.execute(text("INSERT INTO system_metadata (key, value, updated_at) VALUES (:k, :v, now())"),
                         {"k": key, "v": json.dumps([f"{clips['photo']}:proxy"]) if key == "artifacts.missing"
                          else "2" if key.endswith(".version_seen") else '{"detail": "high"}'})
        conn.execute(text("INSERT INTO system_metadata (key, value, updated_at) VALUES ('scheduler.status', :v,"
                          " now())"), {"v": json.dumps(STATUS_BEFORE)})
        # Each JSON lineage: a scene's description, a redo's scenes being found, open uploads.
        for i, producer in enumerate(("scene-vision", "person")):
            conn.execute(text("INSERT INTO video_scenes (scene_id, asset_id, scene_index, start_ms, end_ms,"
                              " rep_frame_ms, created_at, description, lineage) VALUES (:s, :a, :i, :i * 6000,"
                              " :i * 6000 + 6000, :i * 6000, now(), 'a scene', CAST(:l AS jsonb))"),
                         {"s": f"scn_{i}", "a": clips["video"], "i": i,
                          "l": json.dumps({"producer": producer, "version": "1", "settings_hash": "h"})})
        conn.execute(text("INSERT INTO video_index_chunks (chunk_id, asset_id, chunk_index, start_ms, end_ms, status,"
                          " created_at, run) VALUES ('chk_0', :a, 0, 0, 12000, 'pending', now(), CAST(:r AS jsonb))"),
                     {"a": clips["video"], "r": json.dumps({"producer": "scene-detect", "version": "4",
                                                            "settings_hash": "h", "level": 1})})
        for i, (upload, artifact, producer) in enumerate((("upl_a", "analysis_proxy", "analysis-proxy"),
                                                          ("upl_p", "video_preview", "preview"),
                                                          ("upl_o", "analysis_proxy", "analysis-proxy-0"))):
            conn.execute(text("INSERT INTO uploads (upload_id, asset_id, artifact, size, sha256, lineage, received,"
                              " created_at, expires_at) VALUES (:u, :a, :b, 10, :sha, CAST(:l AS jsonb), 0, now(),"
                              " now() + interval '1 day')"),
                         {"u": upload, "a": clips["video"], "b": artifact, "sha": str(i) * 64,
                          "l": json.dumps({"producer": producer, "version": "2", "settings_hash": "h"})})
    engine.dispose()
    return clips


def _kept(url: str) -> dict:
    """Every row the migration could touch, by what it holds (not its clip's id)."""
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            names = {r[0]: r[1] for r in conn.execute(text("SELECT asset_id, rel_path FROM assets"))}
            status = conn.execute(text("SELECT value FROM system_metadata WHERE key = 'scheduler.status'")).scalar()
            return {
                "lineage": sorted((names[a].split(".")[0], b, p, o) for a, b, p, o in conn.execute(text(
                    "SELECT asset_id, artifact, producer, outcome FROM artifact_lineage"))),
                "pauses": sorted(tuple(r) for r in conn.execute(text(
                    "SELECT target, scope, paused_by FROM producer_pauses"))),
                "crashes": sorted((names[a].split(".")[0], b) for a, b in conn.execute(text(
                    "SELECT asset_id, artifact FROM producer_crashes"))),
                "keys": sorted(r[0] for r in conn.execute(text(
                    "SELECT key FROM system_metadata WHERE key <> 'scheduler.status'"))),
                "status": json.loads(status),
                "scenes": sorted(r[0]["producer"] for r in conn.execute(text("SELECT lineage FROM video_scenes"))),
                "runs": [r[0] for r in conn.execute(text("SELECT run FROM video_index_chunks"))],
                "uploads": sorted((r[0], r[1], r[2]["producer"]) for r in conn.execute(text(
                    "SELECT upload_id, artifact, lineage FROM uploads"))),
            }
    finally:
        engine.dispose()


@pytest.mark.slow
def test_an_account_from_before_part_3_upgraded_straight_to_head() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _fresh(pg)
        _alembic(url, "upgrade", BEFORE)
        _seed_before(url)
        before = _kept(url)
        with pytest.raises(AssertionError, match="producer.vision.version_seen"):
            _net(url)  # the net catches what's left
        _alembic(url, "upgrade", "head")
        after = _kept(url)
        assert after["lineage"] == sorted((c, b, p, o) for c, _, _, o, b, p in LINEAGE)
        assert after["pauses"] == sorted((t, s, by) for _, s, by, t in PAUSES)
        assert after["crashes"] == sorted((c, b) for c, _, b in CRASHES)
        assert {new for _, new in KEYS} <= set(after["keys"])
        assert not {old for old, new in KEYS if old != new} & set(after["keys"])
        assert after["status"] == STATUS_AFTER
        assert after["scenes"] == ["lumiverb.video.scene_descriptions", "person"]
        assert after["runs"] == [{"producer": "lumiverb.video.scenes", "version": "4", "settings_hash": "h",
                                  "level": 1}]
        assert after["uploads"] == [  # the files by their names too, from part 4 (a3c9e5f1b7d2)
            ("upl_a", "lumiverb.video.analysis_proxy", "lumiverb.video.analysis_proxy"),
            ("upl_o", "lumiverb.video.analysis_proxy", "analysis-proxy-0"),  # not the producer now: kept
            ("upl_p", "lumiverb.video.preview", "lumiverb.video.preview")]
        _net(url)

        _alembic(url, "downgrade", BEFORE)
        assert _kept(url) == before, "down, every row as it was"
        _alembic(url, "upgrade", "head")
        assert _kept(url) == after, "and up again the same"


# Every column that holds what an artifact, a producer or a kind is called, and what the migration does with it.
RENAMED_COLUMNS = {
    ("artifact_lineage", "artifact"), ("artifact_lineage", "producer"), ("producer_crashes", "artifact"),
    ("producer_pauses", "target"), ("video_scenes", "lineage"), ("video_index_chunks", "run"), ("uploads", "lineage"),
}
KEPT_FILE_KINDS = {("uploads", "artifact")}  # renamed by part 4's migration, a3c9e5f1b7d2


@pytest.mark.slow
def test_every_column_holding_a_producer_s_word_is_one_it_renames() -> None:
    # A table added since that keeps an artifact, a producer, a kind or a lineage is renamed by a migration too.
    with PostgresContainer(PG_IMAGE) as pg:
        url = _fresh(pg)
        _alembic(url, "upgrade", "head")
        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                held = {tuple(r) for r in conn.execute(text(
                    "SELECT c.table_name, c.column_name FROM information_schema.columns c JOIN information_schema.tables"
                    " t USING (table_schema, table_name) WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'"
                    " AND c.column_name IN ('artifact', 'producer', 'target', 'kind', 'lineage', 'run')"))}
        finally:
            engine.dispose()
    assert held == RENAMED_COLUMNS | KEPT_FILE_KINDS


# ---------------------------------------------------------------------------
# No old id where a name belongs
# ---------------------------------------------------------------------------

# Where an old word stays, by its family (section 6 of the plan): (table, column) -> the words.
KEPT_COLUMNS = {
    ("asset_embeddings", "model_id"): {"clip"},  # a model's id
    ("faces", "detection_model"): {"insightface"},  # a model's id
    ("assets", "transcript_source"): {"whisper"},  # who made a transcript: a person, or a provider
}
_TEXT_TYPES = ("text", "varchar", "jsonb", "json")


def _kept_as_was(word: str) -> bool:
    """The work of a plug-in installed with an id no plug-in may have now, kept as it was (section 8)."""
    work = re.search(r"plugin_([a-z][a-z0-9_]*?)(\.version_seen)?$", word)
    plugin_id = work.group(1).replace("_", "-") if work else ""
    return bool(work) and (plugin_id.startswith("lumiverb") or plugin_id in _migration()._RESERVED)


def _old(word: str) -> bool:
    return (word in OLD_WORDS or bool(_OLD_KEY.match(word))) and not _kept_as_was(word)


def _old_in(value, *, key: str | None = None) -> list[str]:
    """Every old word in a JSON value, by where it is."""
    found: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            if _old(k):
                found.append(f"key {k}")
            found += _old_in(v, key=k)
    elif isinstance(value, list):
        for v in value:
            found += _old_in(v, key=key)
    elif isinstance(value, str) and _old(value):
        found.append(f"{key}: {value}")
    return found


def _json_of(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _net(url: str) -> None:
    """No old id where an artifact, a producer or a kind belongs: every text and jsonb column, and
    system_metadata's keys and values, but for the kept families and the older producers kept as they were."""
    engine = create_engine(url)
    found: list[str] = []
    try:
        with engine.connect() as conn:
            columns = conn.execute(text(
                "SELECT c.table_name, c.column_name FROM information_schema.columns c JOIN information_schema.tables"
                " t USING (table_schema, table_name) WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'"
                " AND c.udt_name IN :types AND c.table_name <> 'alembic_version'").bindparams(
                    bindparam("types", expanding=True)), {"types": list(_TEXT_TYPES)}).all()
            for table, column in columns:
                if (table, column) in {("artifact_lineage", "producer"), ("system_metadata", "value")}:
                    continue  # below, with what each row is
                kept = KEPT_COLUMNS.get((table, column), set())
                for (value,) in conn.execute(text(f'SELECT DISTINCT "{column}"::text FROM "{table}"'
                                                  f' WHERE "{column}" IS NOT NULL')):
                    for old in _old_in(_json_of(value), key=column):
                        if old.split(": ")[-1] not in kept:
                            found.append(f"{table}.{column}: {old}")
            # A lineage's producer may be an older one, kept as it was; never the one that made it now.
            olds = {v: k for k, v in _migration().ARTIFACTS.items()}
            for artifact, producer in conn.execute(text("SELECT DISTINCT artifact, producer FROM artifact_lineage")):
                if CURRENT.get(olds.get(artifact, "")) == producer and producer != artifact:
                    found.append(f"artifact_lineage.producer: {producer} (of {artifact})")
            for key, value in conn.execute(text("SELECT key, value FROM system_metadata")):
                said = _json_of(value)
                if key == "scheduler.status" and isinstance(said, dict):
                    # Lumiverb's pools and the AI jobs keep their names; a plug-in's pool is its work's.
                    found += [f"scheduler.status pools: {name}" for name in said.get("pools") or {} if _old(name)
                              and name not in _migration()._POOLS]
                    said = {k: v for k, v in said.items() if k not in ("pools", "ai_waits")}
                found += [f"system_metadata {key}: {old}" for old in _old_in(said, key=key)]
    finally:
        engine.dispose()
    assert found == [], "old ids left: " + "; ".join(found)


@pytest.mark.slow
def test_the_tester_s_account_from_v0_1_104_has_no_old_id_once_upgraded() -> None:
    from tests.test_named_data_acceptance import SEEDED, _copied_in

    seeded = json.loads(SEEDED.read_text())
    assert seeded["revision"] == BEFORE
    with PostgresContainer(PG_IMAGE) as pg:
        url = _fresh(pg)
        _alembic(url, "upgrade", BEFORE)
        engine = create_engine(url)
        with Session(engine) as session:
            _copied_in(session, seeded)
        engine.dispose()
        _alembic(url, "upgrade", "head")
        _net(url)


# ---------------------------------------------------------------------------
# An account made through today's code, down and up again
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    """The server's own folder for this test: where uploads wait."""
    from src.server.config import get_settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield tmp_path / "data"
    get_settings.cache_clear()


def _said(env, library_id: str) -> dict:
    """What clients are told of the account, and what the scheduler would hand out."""
    from src.server.scheduler.kinds import KINDS
    from tests.producer_fakes import due

    client, headers, *_ = env
    r = client.get("/v1/producers", params={"library_id": library_id}, headers=headers)
    assert r.status_code == 200, r.text
    queue = client.get("/v1/producers/queue", headers=headers).json()
    return {"producers": r.json()["producers"], "switches": queue["switches"], "running": queue["running"],
            "failures": client.get("/v1/producers/failures", headers=headers).json(),
            "due": {kind: sorted(c["asset_id"] for c in due(env[-1], kind, [library_id]))
                    for kind in sorted(KINDS) if kind != "scan"}}


def _rows(env) -> dict:
    engine = create_engine(env[-1])
    try:
        with engine.connect() as conn:
            return {table: sorted(map(str, conn.execute(text(f"SELECT * FROM {table}")).all())) for table in (
                "artifact_lineage", "producer_pauses", "producer_crashes", "system_metadata", "uploads",
                "video_scenes", "video_index_chunks")}
    finally:
        engine.dispose()


@pytest.mark.slow
def test_an_account_made_today_goes_down_and_up_as_it_was(env, data_dir):
    from src.producers.names import (
        ANALYSIS_PROXY,
        CAPTURE_TIME,
        DESCRIPTION,
        SCENES,
        TEXT,
        TRANSCRIPT,
        remakes_of,
    )
    from src.server.repository import lineage
    from src.server.scheduler.service import _crashed_in_database
    from src.shared.producers import PRODUCERS
    from tests.derived import saved
    from tests.scenes import Scene, described_scenes
    from tests.test_crashes_and_new_versions import _made_by_an_older_version
    from tests.test_lineage_api import _db, _ingest_with, _sha, _state, _want
    from tests.test_reconciler import _library
    from tests.videos import playable

    lib = _library(env, "NamesStored")
    client, headers, library_id, _, tenant_id, url = lib
    sha = _sha()
    photo = _ingest_with(lib, "photo.jpg", sha)
    r = saved(client, "vision", photo, json={"model_id": "m", "description": "a dog",
                                             "lineage": _want(lib, DESCRIPTION, sha)}, headers=headers)
    assert r.status_code == 200, r.text
    r = saved(client, "ocr", photo, json={"ocr_text": "", "model_id": "m", "lineage": _want(lib, TEXT, sha)},
              headers=headers)  # nothing read: empty
    assert r.status_code == 200, r.text
    failed = _ingest_with(lib, "failed.jpg", _sha())
    r = client.post("/v1/producers/failures", headers=headers, json={"items": [
        {"asset_id": failed, "artifact": CAPTURE_TIME, "error": "exiftool couldn't read it"}]})
    assert r.status_code == 200, r.text
    _crashed_in_database(tenant_id, TEXT, [failed], "boom")
    _made_by_an_older_version(lib, "older.jpg")
    with _db(lib) as s:
        assert lineage.hold_new_version(s, DESCRIPTION) is True  # its redo held until an admin resumes it
    stale = _ingest_with(lib, "stale.jpg", sha2 := _sha())
    with _db(lib) as s:  # made by a producer that isn't the one now
        s.execute(text("INSERT INTO artifact_lineage (asset_id, artifact, producer, producer_version, settings_hash,"
                       " source_hash, produced_at, outcome, attempts) VALUES (:a, :b, 'ocr-v0', '1', 'h', :h,"
                       " now(), 'ok', 0)"), {"a": stale, "b": TEXT, "h": sha2})
        # A plug-in's work (its id has a dash: `plugin_my_facts` before part 3).
        s.execute(text("INSERT INTO artifact_lineage (asset_id, artifact, producer, producer_version, settings_hash,"
                       " source_hash, produced_at, outcome, attempts) VALUES (:a, 'my-facts', 'my-facts', '1', 'h',"
                       " :h, now(), 'ok', 0)"), {"a": photo, "h": sha})
        s.execute(text("INSERT INTO producer_pauses (target, scope, paused_by, paused_at) VALUES"
                       " ('my-facts', 'work', 'usr_1', now())"))
        s.commit()
    assert client.post(f"/v1/producers/{TEXT}/pause", json={"scope": "work"}, headers=headers).status_code == 204

    video = _ingest_with(lib, "video.mov", vsha := _sha(), media_type="video")
    with _db(lib) as s:
        s.execute(text("UPDATE assets SET duration_sec = 12 WHERE asset_id = :a"), {"a": video})
        s.commit()
    r = client.post(f"/v1/assets/{video}/transcript", headers=headers, json={
        "srt": "1\n00:00:00,000 --> 00:00:01,000\nhello\n", "source": "whisper",
        "lineage": _want(lib, TRANSCRIPT, vsha)})
    assert r.status_code == 200, r.text
    r = client.put(f"/v1/producers/{TRANSCRIPT}/settings", headers=headers,
                   json={"settings": {"vad_min_silence_ms": 800}, "redo": False})  # new files only
    assert r.status_code == 200, r.text
    described_scenes(client, headers, video, [Scene(0, 6000, "A card"), Scene(6000, 12000, "Bars")])
    with _db(lib) as s:  # a redo's scenes being found: how they're found, kept with its chunks
        s.execute(text("UPDATE video_index_chunks SET run = CAST(:r AS jsonb) WHERE asset_id = :a"),
                  {"a": video, "r": json.dumps(_want(lib, SCENES, vsha))})
        s.execute(text("INSERT INTO system_metadata (key, value, updated_at) VALUES ('scheduler.status', :v, now())"
                       " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"), {"v": json.dumps({
                           "at": "2026-10-19T12:00:00+00:00", "running": {ANALYSIS_PROXY: 1, "my-facts": 1},
                           "waiting": {remakes_of(DESCRIPTION): 1}, "pace": {ANALYSIS_PROXY: 0.5},
                           "pools": {"render": [1, 2], "my-facts": [1, 1]}, "plugin_waits": {"my-facts": "starting"},
                           "jobs": [{"kind": ANALYSIS_PROXY, "units": 12.0, "elapsed": 1.0}]})})
        s.commit()
    other = _ingest_with(lib, "other.mov", osha := _sha(), media_type="video")
    data = playable(2.0)
    r = client.post(f"/v1/assets/{other}/uploads", headers=headers, json={
        "artifact": "analysis_proxy", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "lineage": _want(lib, ANALYSIS_PROXY, osha)})
    assert r.status_code == 200, r.text
    upload = r.json()

    before, rows = _said(lib, library_id), _rows(lib)
    with _db(lib) as s:
        unseen = {name for name in PRODUCERS if lineage.new_version_unseen(s, name)}
    _alembic(url, "downgrade", BEFORE)
    down = _rows(lib)
    assert any(f"'{photo}', 'vision', 'vision'" in r for r in down["artifact_lineage"])
    assert any(f"'{stale}', 'ocr', 'ocr-v0'" in r for r in down["artifact_lineage"]), "an older producer kept"
    assert any(f"'{photo}', 'plugin_my_facts', 'plugin_my_facts'" in r for r in down["artifact_lineage"])
    assert {r.split("'")[1] for r in down["producer_pauses"]} == {"vision", "ocr", "transcript", "plugin_my_facts"}
    assert any("'producer.transcript'" in r for r in down["system_metadata"])
    assert any("'producer.vision.version_seen'" in r for r in down["system_metadata"])
    assert any("'producer': 'analysis-proxy'" in r for r in down["uploads"])
    assert all("'producer': 'scene-vision'" in r for r in down["video_scenes"])
    assert all("'producer': 'scene-detect'" in r for r in down["video_index_chunks"])

    _alembic(url, "upgrade", "head")
    assert _rows(lib) == rows, "every row as it was"
    after = _said(lib, library_id)
    for key in before:
        assert after[key] == before[key], key
    with _db(lib) as s:
        assert {name for name in PRODUCERS if lineage.new_version_unseen(s, name)} == unseen
        assert not any(lineage.hold_new_version(s, name) for name in PRODUCERS), "no new version held"
    _net(url)

    # The upload open across it is finished after, its analysis proxy as it would have been.
    r = client.put(upload["chunk_url"], params={"offset": 0}, content=data,
                   headers={**headers, "Content-Type": "application/octet-stream"})
    assert r.status_code == 200, r.text
    r = client.post(f"{upload['chunk_url']}/finish", headers=headers)
    assert r.status_code == 200, r.text
    assert _state(lib, other, ANALYSIS_PROXY) == "current"
