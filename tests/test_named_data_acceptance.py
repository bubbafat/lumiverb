"""The tester's acceptance tests for ADR-024 (named data between producers),
written apart from the builder's.

Phase 1 names Lumiverb's own data (`lumiverb.*`), and Robert chose that
today's ids are renamed to the names wherever they're stored and sent (the
plan's option (a)). Held here against what v0.1.104 said, written from its
code before phase 1, through the rename (RENAMED: today's id -> its name):

- every row of GET /v1/producers but for the fields phase 1 adds (`makes`,
  `reads`, `reads_others`), the work request a protocol 1 plug-in is handed,
  to the byte, and the hash its outputs are kept with (a changed hash makes
  every plug-in's work due again): tests/plugins/tester_named_data_before.json;
- an account as v0.1.104 kept it, every table and key holding a producer's id
  (tests/plugins/tester_named_data_seeded.json, written by the tester's
  generator from v0.1.104's own code paths), copied in at its revision and
  migrated up: what the API, the queue and the scheduler's due lists said of
  it, unchanged through the rename; migrated down: every row as it was.

    LUMIVERB_WRITE_TESTER_NAMED_DATA=1 uv run pytest tests/test_named_data_acceptance.py

writes the first file again: only on purpose, and only when what it holds was
meant to change.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from src.server.database import get_tenant_session, tenant_database_url
from tests.producer_fakes import due
from tests.test_first_run_setup import control  # noqa: F401 (a fixture)
from tests.test_plugin_runner import data as short_data  # noqa: F401 (a fixture)
from tests.test_plugin_work import _job, facts  # noqa: F401 (facts: a fixture)
from tests.test_server_admins import install  # noqa: F401 (a fixture)

pytestmark = pytest.mark.slow

BEFORE = Path(__file__).parent / "plugins" / "tester_named_data_before.json"
WRITE = "LUMIVERB_WRITE_TESTER_NAMED_DATA"
# What phase 1 adds to each producer's row (the plan's section 2).
ADDED = ("makes", "reads", "reads_others")


def _as_then(row: dict) -> dict:
    """A producer's row without what was added since v0.1.104: phase 1's fields, and its counts' lacking (what
    its clips may never have: an installed plug-in's, ADR-023)."""
    row = {k: v for k, v in row.items() if k not in ADDED}
    if row.get("counts"):
        row["counts"] = {k: v for k, v in row["counts"].items() if k != "lacking"}
    return row


def _said(f) -> dict:
    """What clients and the plug-in get, with nothing that differs between runs (an account's ids)."""
    charged, outcome = _job(f)
    assert (charged, outcome) == ([], None)
    r = f.client.get("/v1/producers", params={"library_id": f.library_id}, headers=f.robert)
    assert r.status_code == 200, r.text
    rows = [_as_then(row) for row in r.json()["producers"]]
    handed = (f.data / "plugins" / "facts" / "data" / "last-work.json").read_text()
    with get_tenant_session(f.tenant_id) as session:
        hashes = [session.execute(text("SELECT inputs_hash FROM plugin_outputs WHERE asset_id = :a AND plugin_id ="
                                       " 'facts'"), {"a": clip}).scalar_one() for clip in f.clips[:2]]
    said = json.dumps({"producers": rows, "handed": json.loads(handed), "inputs_hash": hashes}, sort_keys=True)
    for private in (f.tenant_id, f.library_id, *f.clips):
        said = said.replace(private, f"<{'account' if private == f.tenant_id else 'id'}>")
    return json.loads(said)


def test_phase1_clients_and_plug_ins_get_what_they_got_before(facts):  # noqa: F811
    """Every GET /v1/producers row as before (but for the fields phase 1 adds, and its ids renamed: RENAMED), a
    protocol 1 plug-in handed the same request, its outputs kept with the same inputs' hash."""
    now = _said(facts)
    if os.environ.get(WRITE):
        BEFORE.write_text(json.dumps(now, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    before = json.loads(BEFORE.read_text())
    for key in ("handed", "inputs_hash"):
        assert now[key] == before[key], f"{key} isn't what it was before phase 1"
    mine = {row["artifact"]: row for row in now["producers"]}
    theirs = {row["artifact"]: row for row in _renamed(before["producers"])}
    assert list(mine) == list(theirs), "the same producers, in the same order"
    changed = {a: {k: (theirs[a].get(k), mine[a].get(k)) for k in set(theirs[a]) | set(mine[a])
                   if theirs[a].get(k) != mine[a].get(k)} for a in theirs if mine[a] != theirs[a]}
    assert changed == {}, f"rows that changed (before, now): {changed}"


# ---------------------------------------------------------------------------
# The rename (the plan's option (a), Robert's choice): an account as v0.1.104 kept it, migrated up and down
# ---------------------------------------------------------------------------

SEEDED = Path(__file__).parent / "plugins" / "tester_named_data_seeded.json"
# Today's id -> its name, as the rename stores and sends it (the plan's section 1, part 3), written out here
# apart from the builder's mapping; an installed plug-in's work, `plugin_<id>`, is its plug-in's id.
RENAMED: dict[str, str] = {
    "probe": "lumiverb.video.format", "capture": "lumiverb.capture_time", "proxy": "lumiverb.preview",
    "video_preview": "lumiverb.video.preview", "analysis_proxy": "lumiverb.video.analysis_proxy",
    "scenes": "lumiverb.video.scenes", "scene_vision": "lumiverb.video.scene_descriptions",
    "vision": "lumiverb.image.description", "ocr": "lumiverb.image.text", "clip": "lumiverb.image.likeness",
    "faces": "lumiverb.image.faces", "video_faces": "lumiverb.video.faces", "transcript": "lumiverb.video.transcript",
    "location": "lumiverb.location.guess", "plugin_facts": "facts",
}


def _kind(kind: str) -> str:
    """A job's kind as the rename says it: its producer's name (the analysis proxy's was `render`), its remakes'
    `redo:<name>`; the scan's stays."""
    if kind.startswith("redo_"):
        return "redo:" + _kind(kind.removeprefix("redo_"))
    return RENAMED["analysis_proxy"] if kind == "render" else RENAMED.get(kind, kind)
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?")


def _tenant_alembic(url: str, verb: str, revision: str) -> None:
    said = subprocess.run([sys.executable, "-m", "alembic", "-c", "alembic-tenant.ini", verb, revision],
                          cwd=Path(__file__).resolve().parents[1], env={**os.environ, "ALEMBIC_TENANT_URL": url},
                          capture_output=True, text=True, timeout=300)
    assert said.returncode == 0, said.stdout + said.stderr


def _copied_in(session, seeded: dict) -> None:
    """The account's rows inserted as they were (their own triggers off: the copy is the state), each time
    moved on by as long as it's been since they were written, so what waits its turn still does."""
    shift = f"{(datetime.now(UTC) - datetime.fromisoformat(seeded['written_at'])).total_seconds()} seconds"
    session.execute(text("SET session_replication_role = replica"))
    for t in seeded["tables"]:
        names = ", ".join(f'"{c}"' for c, _ in t["columns"])
        values = ", ".join(f"(CAST(:v{i} AS timestamptz) + CAST(:shift AS interval))" if udt == "timestamptz"
                           else f"CAST(:v{i} AS {udt})" for i, (_, udt) in enumerate(t["columns"]))
        for row in t["rows"]:
            session.execute(text(f"INSERT INTO {t['table']} ({names}) VALUES ({values})"),
                            {"shift": shift, **{f"v{i}": v for i, v in enumerate(row)}})
    session.execute(text("SET session_replication_role = origin"))
    for table, column in session.execute(text(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public'"
            " AND column_default LIKE 'nextval(%'")).all():
        session.execute(text(f"SELECT setval(pg_get_serial_sequence(:t, :c), GREATEST(1, (SELECT max({column})"
                             f" FROM {table})))"), {"t": table, "c": column})
    session.commit()


def _rows(session, seeded: dict) -> dict:
    """The copied tables' rows now, as text, in a fixed order."""
    out = {}
    for t in seeded["tables"]:
        select = ", ".join(f'"{c}"::text' for c, _ in t["columns"])
        out[t["table"]] = sorted([str(v) for v in r] for r in session.execute(text(f"SELECT {select} FROM {t['table']}")))
    return out


def _renamed(value):
    """What v0.1.104 said, said as the rename says it: an id where an id is, renamed (not a job's or a title's,
    which keep their words), and a producer's row without its producer, a field part 5 removed (a producer is
    named by what it makes)."""
    if isinstance(value, list):
        return [_renamed(v) for v in value]
    if isinstance(value, dict):
        out = {k: (RENAMED.get(v, v) if k in ("artifact", "part_of", "target") and isinstance(v, str)
                   else [RENAMED.get(x, x) for x in v] if k == "needs" and isinstance(v, list)
                   else _renamed(v)) for k, v in value.items()}
        if "producer" in out and isinstance(out.get("artifact"), str) and "title" in out:
            del out["producer"]
        return out
    return value


def _timeless(value):
    return json.loads(_TIME.sub("<time>", json.dumps(value, sort_keys=True)))


def test_rename_an_account_from_before_migrated_up_answers_as_it_did_and_down_is_as_it_was(install):  # noqa: F811
    """An account as v0.1.104 kept it (every table and key holding a producer's id: lineage made, empty, failed
    and stale, a producer's new version seen, settings changed, work and a redo paused, upkeep paused, a crash,
    an open upload, a missing file reported, a plug-in's lineage), migrated up: every producer's counts, waits
    and switches, the failures and every kind's due clips are what v0.1.104 said, through the rename (nothing
    remade, nothing lost); migrated down again: every row as it was."""
    client, robert, tenant_id = install
    seeded = json.loads(SEEDED.read_text())
    url = tenant_database_url(tenant_id)
    if RENAMED:
        _tenant_alembic(url, "downgrade", seeded["revision"])
    with get_tenant_session(tenant_id) as session:
        _copied_in(session, seeded)
        inserted = _rows(session, seeded)
    _tenant_alembic(url, "upgrade", "head")

    library_id = next(t for t in seeded["tables"] if t["table"] == "libraries")["rows"][0][0]
    clips = seeded["clips"]
    r = client.get("/v1/producers", params={"library_id": library_id}, headers=robert)
    assert r.status_code == 200, r.text
    now = {"producers": [_as_then(row) for row in r.json()["producers"]],
           "switches": client.get("/v1/producers/queue", headers=robert).json().get("switches"),
           "failures": client.get("/v1/producers/failures", headers=robert).json(),
           "due": {kind: sorted(clips[c["asset_id"]] for c in due(url, kind, [library_id]))
                   for kind in sorted(_kind(k) for k in seeded["said"]["due"])}}
    then = _renamed(seeded["said"])
    then["due"] = {_kind(k): v for k, v in then["due"].items()}
    now, then = _timeless(now), _timeless(then)
    mine = {row["artifact"]: row for row in now["producers"]}
    theirs = {row["artifact"]: row for row in then["producers"]}
    assert list(mine) == list(theirs), "the same producers, in the same order"
    changed = {a: {k: (theirs[a].get(k), mine[a].get(k)) for k in set(theirs[a]) | set(mine[a])
                   if theirs[a].get(k) != mine[a].get(k)} for a in theirs if mine[a] != theirs[a]}
    assert changed == {}, f"producers whose row changed (before, now): {changed}"
    for key in ("switches", "failures", "due"):
        assert now[key] == then[key], f"{key}: before {then[key]}, now {now[key]}"

    if RENAMED:
        _tenant_alembic(url, "downgrade", seeded["revision"])
        with get_tenant_session(tenant_id) as session:
            down = _rows(session, seeded)
        # Every row as it was, but each library's reader mark: the renames moved it on, up and down, as any change
        # to what its reader is told does (so it fetches what it was told again, in the words it's said in now).
        libraries = next(t for t in seeded["tables"] if t["table"] == "libraries")
        mark = [c for c, _ in libraries["columns"]].index("reader_mark")
        for before, after in zip(inserted["libraries"], down["libraries"], strict=True):
            assert int(after[mark]) > int(before[mark]), "its reader is told to fetch what changed"
            before[mark] = after[mark]
        assert down == inserted, "migrated down, every row as it was"
        _tenant_alembic(url, "upgrade", "head")
