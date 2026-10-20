"""Lumiverb's data, named (ADR-024 phase 1): each producer's `makes` and
`reads`, what Lumiverb keeps itself (src/producers/names.py), and what's
worked out from them.

The names change nothing the scheduler works out but its words: the
registry is held to what it was before names (tests/fixtures/
producers_before_names.json, written from main before part 1), each old word
read as its name through RENAMED, and what each producer reads to the SQL
that decides whether it's due. The registry's own refusals are in
test_producer_contract.py; the migration that renamed what's stored (part 3),
test_migration_names_stored.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from src.plugins.manifest import INPUT_NAMES, INPUTS, read
from src.producers import names, registry, use_added
from src.producers.names import (
    ARTIFACTS,
    FILES,
    KINDS,
    LUMIVERB,
    MACHINE_WORDS,
    PRODUCERS,
    RENAMED,
    RESERVED,
    artifact_in,
    file_in,
    is_reserved,
    kind_in,
    makers,
    needs_of,
    page,
    producer_in,
    sql_spelling,
)

pytestmark = pytest.mark.fast

REPO = Path(__file__).resolve().parent.parent
BEFORE = REPO / "tests" / "fixtures" / "producers_before_names.json"


def _now() -> dict:
    """What tests/fixtures/producers_before_names.json holds, as worked out now (by name)."""
    from src.server.repository.lineage import APPLIES, MADE, RECHECK
    from src.server.scheduler.kinds import AI_JOB_KINDS
    from src.server.scheduler.kinds import KINDS as SCHEDULER_KINDS
    from src.shared.producers import SCHEDULED, pause_targets

    out = {"producers": {}, "kinds": {}, "scheduled": sorted(SCHEDULED), "pause_targets": list(pause_targets()),
           "applies": dict(APPLIES), "made": dict(MADE), "recheck": dict(RECHECK),
           "ai_job_kinds": {k: list(v) for k, v in AI_JOB_KINDS.items()}}
    for a, p in registry().items():
        out["producers"][a] = {"needs": list(p.needs), "part_of": p.part_of, "redo_also": list(p.redo_also),
                               "tier": p.tier, "pool": p.pool.name if p.pool else None, "media": list(p.media),
                               "made_at_scan": p.made_at_scan, "storage": p.storage}
    for n, k in SCHEDULER_KINDS.items():
        out["kinds"][n] = {"artifact": k.artifact, "storage": k.storage, "redo": k.redo, "tier": k.spec.tier,
                           "pool": k.spec.pool, "batch": k.spec.batch, "same_as": k.spec.same_as,
                           "by_seconds": k.spec.by_seconds}
    return out


def _sql_named(sql: str) -> str:
    """Main's SQL with each artifact, producer and settings key it names as it's named now."""
    for (old, producer), name in PRODUCERS.items():
        sql = re.sub(rf"(\bartifact = )'{re.escape(old)}'", rf"\1'{name}'", sql)
        sql = re.sub(rf"(\bproducer = )'{re.escape(producer)}'", rf"\1'{name}'", sql)
        sql = sql.replace(f"'producer.{old}'", f"'producer.{name}'")
    return sql


def _before_named() -> dict:
    """tests/fixtures/producers_before_names.json, each old word as its name (RENAMED)."""
    before = json.loads(BEFORE.read_text())
    return {
        "producers": {artifact_in(a): {"needs": [artifact_in(n) for n in p["needs"]],
                                       "part_of": artifact_in(p["part_of"]) if p["part_of"] else "",
                                       "redo_also": [artifact_in(n) for n in p["redo_also"]], "tier": p["tier"],
                                       "pool": p["pool"], "media": p["media"], "made_at_scan": p["made_at_scan"],
                                       "storage": p["storage"]}
                      for a, p in before["producers"].items()},
        "kinds": {kind_in(n): {"artifact": artifact_in(before["missing_flags"][k["flag"]]) if k["flag"] else "",
                               "storage": k["storage"], "redo": k["redo"], "tier": k["tier"], "pool": k["pool"],
                               "batch": k["batch"], "same_as": kind_in(k["same_as"]) if k["same_as"] else "",
                               "by_seconds": k["by_seconds"]} for n, k in before["kinds"].items()},
        "scheduled": sorted(artifact_in(a) for a in before["missing_flags"].values()),
        "pause_targets": [artifact_in(t) for t in before["pause_targets"]],
        **{rule: {artifact_in(a): _sql_named(sql) for a, sql in before[rule].items()}
           for rule in ("applies", "made", "recheck")},
        "ai_job_kinds": {job: [kind_in(k) for k in kinds] for job, kinds in before["ai_job_kinds"].items()},
    }


def test_the_registry_works_out_what_it_did_before_names():
    # needs worked out from reads, the same as declared before; nothing queued, paused or kept differently: only
    # each word is its name now (ADR-024 part 3).
    assert _now() == _before_named()


# What a producer's SQL may read that's no one's data: the clip's own row, the library's switch, the lineage
# its made says it by, the account's settings.
_NOT_DATA_COLUMNS = {"asset_id", "media_type", "library_id"}
_NOT_DATA_TABLES = {"artifact_lineage", "system_metadata", "libraries"}


def _read_by(sql: str) -> set[str]:
    """What a producer's applies, made or recheck reads: assets.<column> and the tables it selects from."""
    columns = {f"assets.{c}" for c in re.findall(r"\ba\.([a-z_]+)", sql)} - {f"assets.{c}" for c in _NOT_DATA_COLUMNS}
    tables = set(re.findall(r"\b(?:FROM|JOIN)\s+([a-z_]+)", sql)) - _NOT_DATA_TABLES
    return columns | tables


def test_what_each_producer_reads_is_what_its_sql_reads():
    # Its reads say what it's made from; the SQL that decides whether it's due reads only that (or what it makes).
    from src.server.repository.lineage import APPLIES, MADE, RECHECK

    made = makers()
    for a, p in registry().items():
        stored = {s for n in (*p.reads, *p.reads_others, p.makes.name) for s in made[n].stored}
        for which, sql in (("applies", APPLIES[a]), ("made", MADE[a]), ("recheck", RECHECK.get(a) or "")):
            assert _read_by(sql) <= stored, (a, which, _read_by(sql) - stored)


def test_the_page_is_what_the_code_says():
    assert names.PAGE.read_text() == page(), f"docs/data-names.md is stale: {names.WRITE}"


def test_every_name_is_made_once_and_spelt_once_for_sql():
    every = names.names()
    assert len(every) == len(set(every)) == len(makers())
    assert len({sql_spelling(n) for n in every}) == len(every)
    assert all(names.NAME.match(n) for n in every)
    assert {d.name for d in LUMIVERB}.isdisjoint(p.makes.name for p in registry().values())


def test_renamed_is_the_registry():
    # Each family's old words, to the names the registry has: every name one of them, and main's words.
    from src.server.api.routers.artifacts import ALLOWED_ARTIFACT_TYPES
    from src.server.scheduler.kinds import KINDS as SCHEDULER_KINDS

    before = json.loads(BEFORE.read_text())
    own = {a for a, p in registry().items() if not p.plugin}
    assert dict(ARTIFACTS) == {a: artifact_in(a) for a in before["producers"]} and set(ARTIFACTS.values()) == own
    assert set(PRODUCERS) == {(a, p["producer"]) for a, p in before["producers"].items()}
    assert all(name == ARTIFACTS[a] for (a, _), name in PRODUCERS.items())
    assert set(KINDS) == {p["kind"] for p in before["producers"].values() if p["kind"]}
    assert {KINDS[k] for k in KINDS} == {k for k in SCHEDULER_KINDS if k != "scan" and not k.startswith("redo:")}
    assert set(FILES.values()) == ALLOWED_ARTIFACT_TYPES
    assert all(f in makers() or f.rpartition(".")[0] in makers() for f in FILES)
    assert set(RENAMED) == {"artifact", "producer", "kind", "file"}
    assert RENAMED["file"] == {old: new for new, old in FILES.items()}


def test_either_spelling_comes_in_as_its_name():
    # From part 3 what's stored and sent is the name; an old word is taken until part 5.
    assert artifact_in("lumiverb.image.text") == artifact_in("ocr") == "lumiverb.image.text"
    assert artifact_in("analysis_proxy") == "lumiverb.video.analysis_proxy"
    assert artifact_in("plugin_facts") == artifact_in("facts") == "facts"  # a plug-in's work, its id
    assert artifact_in("plugin_my_tool") == "my-tool"  # each underscore was a dash
    assert artifact_in("upkeep") == "upkeep" and artifact_in("all") == "all"
    assert file_in("lumiverb.preview") == file_in("proxy") == "proxy"  # files keep their kinds until part 4
    assert file_in("lumiverb.preview.thumbnail") == "thumbnail"
    assert file_in("lumiverb.video.scenes.picture") == "scene_rep"
    assert producer_in("lumiverb.image.faces", "insightface") == producer_in("faces", "insightface") \
        == producer_in("faces", "lumiverb.image.faces") == "lumiverb.image.faces"
    assert producer_in("facts", "plugin_facts") == "facts"
    # Only the producer that makes it now, for its own artifact; another (an older one, a person's) as it came.
    assert producer_in("lumiverb.image.description", "insightface") == "insightface"
    assert producer_in("lumiverb.image.description", "manual") == "manual"
    assert kind_in("render") == "lumiverb.video.analysis_proxy"
    assert kind_in("redo_vision") == "redo:lumiverb.image.description"
    assert kind_in("plugin_facts") == "facts" and kind_in("redo_plugin_facts") == "redo:facts"
    assert kind_in("scan") == "scan" and kind_in("lumiverb.image.text") == "lumiverb.image.text"


def test_needs_are_the_artifacts_its_reads_are_made_by():
    made_by = {"lumiverb.preview": "proxy", "lumiverb.video.scenes": "scenes"}
    assert needs_of(("lumiverb.metadata", "lumiverb.preview", "lumiverb.preview"), made_by) == ("proxy",)
    assert needs_of(("lumiverb.video.scenes", "lumiverb.preview"), made_by) == ("scenes", "proxy")
    assert needs_of((), made_by) == ()


def test_what_a_person_writes_is_what_corrections_keep():
    # The fields a person writes over, by artifact (src/server/repository/corrections.py), and a transcript,
    # which a person uploads whole (its language and its SRT: the segments).
    from src.server.api.routers.assets import TranscriptSubmitRequest
    from src.server.repository.corrections import FIELDS

    written = {a: p.makes.person for a, p in registry().items() if p.makes.person}
    assert set(written) == {*FIELDS.values(), "lumiverb.video.transcript"}
    assert {f: a for f, a in FIELDS.items()} == {
        "description": "lumiverb.image.description", "tags": "lumiverb.image.description",
        "ocr_text": "lumiverb.image.text"}
    assert written["lumiverb.image.description"] == ("description", "tags")
    assert written["lumiverb.image.text"] == ("text",)
    assert written["lumiverb.video.transcript"] == ("language", "segments")
    assert {"language", "srt"} <= set(TranscriptSubmitRequest.model_fields)
    for a, fields in written.items():
        assert set(fields) <= {f.key for f in registry()[a].makes.fields}, a


def test_a_plug_ins_inputs_are_names_and_it_is_handed_them_as_before():
    # Protocol 1's words are a plug-in's (its manifest's, what it's handed): never renamed, each a name.
    from src.server.plugin_catalog import Installed
    from src.server.plugin_producers import spec_for

    assert set(INPUT_NAMES) == set(INPUTS)
    assert set(INPUT_NAMES.values()) <= set(makers())
    m = read(REPO / "tests" / "plugins" / "facts")
    m = replace(m, work=replace(m.work, inputs=("preview", "location", "capture_time")))
    when = datetime(2026, 10, 19)
    spec = spec_for(Installed(m, on_by_default=False, acknowledged_version=None, installed_at=when, changed_at=when))
    assert spec.reads == ("lumiverb.preview", "lumiverb.location", "lumiverb.capture_time") and spec.needs == ()
    try:
        use_added([spec], by="test_data_names")
        added = registry()["facts"]  # its work by its plug-in's id
        # The preview's producer first, as before names; its capture time as the clip has it (never the capture
        # producer's, as the location producer waits for).
        assert added.needs == ("lumiverb.preview",)
        assert added.plugin == "facts"
    finally:
        use_added([], by="test_data_names")
    assert "facts" not in registry()


def test_a_plug_in_reading_what_nothing_makes_is_refused():
    from src.server.plugin_catalog import Installed
    from src.server.plugin_producers import spec_for

    m = read(REPO / "tests" / "plugins" / "facts")
    when = datetime(2026, 10, 19)
    spec = replace(spec_for(Installed(m, False, None, when, when)), reads=("lumiverb.teleport",))
    with pytest.raises(ValueError, match="reads what nothing makes"):
        use_added([spec], by="test_data_names")
    assert "facts" not in registry()


def test_the_ids_a_plug_in_cant_have():
    # Written out, so they stay refused after part 5 deletes RENAMED: the machine's words, the pools, every old
    # word of every family and the first word of each name, as they are now.
    old = {*ARTIFACTS, *(p for _, p in PRODUCERS), *KINDS, *FILES.values()}
    pools = {p.pool.name for p in registry().values() if p.pool and not p.plugin}
    first = {n.split(".")[1] for n in names.names()}
    assert {*MACHINE_WORDS, *pools, *old, *first} == RESERVED
    assert set(MACHINE_WORDS) == {"all", "upkeep", "scans", "scan", "person", "unknown"}
    assert is_reserved("lumiverb") and is_reserved("lumiverb-tools") and is_reserved("probe")
    assert not is_reserved("geonames") and not is_reserved("prober")
    # Every plug-in Lumiverb ships or tests with keeps its id.
    shipped = [*REPO.glob("src/own_plugins/*/lumiverb-plugin.json"), *REPO.glob("src/plugin_examples/*/lumiverb-plugin.json"),
               *REPO.glob("tests/plugins/*/lumiverb-plugin.json")]
    assert len(shipped) >= 5
    for f in shipped:
        assert not is_reserved(json.loads(f.read_text())["id"]), f


def test_the_names_add_one_account_migration_and_none_of_the_control_plane_s():
    # Part 3's renames what's stored (its rollback, migrate --to f8c4a2e6d0b5); parts 1, 2 and 3 touch no control
    # database.
    from alembic.script import ScriptDirectory

    tenant = ScriptDirectory(str(REPO / "migrations" / "tenant"))
    assert tenant.get_heads() == ["f2a6c8e4b0d7"]
    assert tenant.get_revision("f2a6c8e4b0d7").down_revision == "f8c4a2e6d0b5"
    assert ScriptDirectory(str(REPO / "migrations" / "control")).get_heads() == ["d7f3b9e1c5a2"]


@pytest.mark.parametrize("value", ["redo:lumiverb.image.text", "redo_ocr", "Lumiverb.image", "lumiverb.", "lumiverb..x",
                                   "lumiverb.Image", "plugin_facts", "a b", ""])
def test_sql_spelling_takes_only_a_name_or_a_plug_ins_id(value):
    with pytest.raises(ValueError, match="neither a name nor a plug-in's id"):
        sql_spelling(value)


def test_sql_spelling():
    assert sql_spelling("lumiverb.image.text") == "lumiverb_image_text"
    assert sql_spelling("lumiverb.video.analysis_proxy") == "lumiverb_video_analysis_proxy"
    assert sql_spelling("my-plug-in") == "my_plug_in"


def test_the_kinds_lists_kept_by_hand_name_every_kind():
    # Settings › Processing's labels and cursor-api's list of the queue's kinds.
    from src.server.scheduler.kinds import KINDS as SCHEDULER_KINDS

    kinds = {k for k in SCHEDULER_KINDS if not k.startswith("redo:")}
    tsx = (REPO / "src/ui/web/src/pages/settings/ProcessingSection.tsx").read_text()
    labels = re.search(r"const KIND_LABELS[^{]*\{(.*?)\n\};", tsx, re.S).group(1)
    assert set(re.findall(r'^\s+"?([a-z_.]+)"?:', labels, re.M)) == kinds
    doc = (REPO / "docs/cursor-api.md").read_text()
    listed = re.search(r"Kinds: (.*?), an installed plug-in's work's", doc).group(1)
    assert set(re.findall(r"`([a-z_.]+)`", listed)) == kinds


@pytest.mark.parametrize("word", ["lumiverb.preview", "lumiverb.location", "lumiverb.image.description"])
def test_protocol_1s_inputs_are_never_names(word):
    # A manifest says protocol 1's words; a name in their place is refused, never mapped (ADR-024).
    from src.plugins.manifest import ManifestError, parse

    data = json.loads((REPO / "tests" / "plugins" / "facts" / "lumiverb-plugin.json").read_text())
    data["work"]["inputs"] = [word]
    with pytest.raises(ManifestError, match=r"work\.inputs\[0\]: one of location, capture_time, metadata, preview"):
        parse(data)


def test_a_plug_in_with_a_reserved_id_isnt_installed(tmp_path):
    from src.server.plugin_install import PluginInstallError, plan

    folder = tmp_path / "probe"
    folder.mkdir()
    data = json.loads((REPO / "tests" / "plugins" / "hello" / "lumiverb-plugin.json").read_text())
    for plugin_id in ("probe", "transcripts", "lumiverb-extra"):
        (folder / "lumiverb-plugin.json").write_text(json.dumps({**data, "id": plugin_id}))
        with pytest.raises(PluginInstallError, match=f"A plug-in's id can't be '{plugin_id}'"):
            plan(folder, session=None, data=tmp_path / "data")  # refused before anything is read or written
    assert not (tmp_path / "data").exists()
