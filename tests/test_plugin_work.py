"""An installed plug-in's work on each clip (ADR-023 phase 2), against a
real install, its account's database and a real plug-in process run by the
runner (tests/plugins/facts: written against the protocol alone).

Installed, its work is a producer: Processing lists it, its due clips are
those it applies to (on here, its media, every input it declares), a job
hands the plug-in its declared inputs and the account's settings and
nothing else, and what it gives back is kept once Lumiverb has checked it,
with the hash of the inputs it was made from: a clip whose inputs change is
due again, and keeps what it showed until then. Whose an error is decides
what's charged; a plug-in that isn't running holds its clips back,
uncharged; a setting that remakes asks what becomes of what was made.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from src.plugins.manifest import ManifestError, checked_outputs, parse, read
from src.producers import INSTALLED, use_added
from src.producers.plugin_work import PluginWork
from src.producers.runner import Crashed
from src.server.config import get_settings
from src.server.database import get_tenant_session, tenant_database_url
from src.server.lifecycle import cli
from src.server.plugin_producers import refresh
from src.server.repository.plugin_outputs import PluginOutputRepository
from src.shared.producers import PRODUCERS
from src.shared.waits import HELD, PLUGIN_DOWN
from tests.clips import make_clip
from tests.producer_fakes import FakeAccount, due, run_job, scheduler_client
from tests.test_first_run_setup import _env_file, control  # noqa: F401 (control: a fixture)
from tests.test_plugin_install import _listed
from tests.test_plugin_runner import _until
from tests.test_plugin_runner import data as short_data  # noqa: F401 (a fixture)
from tests.test_server_admins import install  # noqa: F401 (a fixture)

FACTS = Path(__file__).parent / "plugins" / "facts"
ARTIFACT = "plugin_facts"
CREDIT = {"text": "Facts' places", "url": "https://example.org/facts",
          "license": {"text": "CC BY 4.0", "url": "https://creativecommons.org/licenses/by/4.0/"}}


# ---------------------------------------------------------------------------
# What it gives back, checked against its manifest
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_only_what_it_declares_is_kept_each_a_fact_of_plain_text():
    work = read(FACTS).work
    assert checked_outputs(work, {"where": {"text": "  At  the\nharbour ", "url": "https://example.org/x"}}) == {
        "where": {"text": "At the harbour", "url": "https://example.org/x"}, "when": None, "what": None}
    for wrong, says in [
        ({"elsewhere": {"text": "x"}}, "elsewhere isn't one it declares"),
        ({"when": "a string"}, "a fact"),
        ({"when": {"text": ""}}, "plain text"),
        ({"when": {"text": "x" * 201}}, "plain text"),
        ({"when": {"text": "<b>bold</b>"}}, "no HTML"),
        ({"when": {"text": "a\x07bell"}}, "plain text"),
        ({"where": {"text": "x", "url": "javascript:alert(1)"}}, "http or https"),
        ({"where": {"text": "x", "url": "https://"}}, "http or https"),
        ({"where": {"text": "x", "extra": 1}}, "a fact"),
        ([], "an object"),
    ]:
        with pytest.raises(ValueError, match=says):
            checked_outputs(work, wrong)
    assert checked_outputs(work, {"when": {"text": "Tom & Jerry < 3"}})["when"] == {"text": "Tom & Jerry < 3"}


@pytest.mark.fast
@pytest.mark.parametrize("change", [{"scope": "server"}, {"type": "secret", "default": ""}])
def test_only_an_accounts_setting_that_isnt_a_secret_remakes(change):
    manifest = json.loads((FACTS / "lumiverb-plugin.json").read_text())
    manifest["settings"][0] = {**manifest["settings"][0], **change}
    with pytest.raises(ManifestError, match="remakes: only an account's setting, and never a secret"):
        parse(manifest)


# ---------------------------------------------------------------------------
# Installed and running: its work
# ---------------------------------------------------------------------------


@pytest.fixture
def facts(install, control, short_data, tmp_path, monkeypatch):  # noqa: F811
    """The facts plug-in installed on by default and running, its work a producer; a library with two photos
    that have every input it reads and one with no location (it doesn't apply to that one)."""
    from src.server.plugin_runner import Runner

    client, robert, tenant_id = install
    env = _env_file(tmp_path, control)
    env.write_text(env.read_text() + f"DATA_DIR={short_data}\n")
    monkeypatch.setenv("DATA_DIR", str(short_data))
    get_settings.cache_clear()
    assert cli.main(["plugins", "install", str(FACTS), "--on", "--yes", "--env", str(env)]) == 0
    refresh()
    runner = Runner(short_data)
    try:
        _until(runner, lambda: _listed(client, robert)["facts"]["running"]["state"] == "ready")
        r = client.post("/v1/libraries", json={"name": "Facts", "root_path": "/Volumes/facts"}, headers=robert)
        assert r.status_code == 200, r.text
        library_id = r.json()["library_id"]
        with get_tenant_session(tenant_id) as session:
            clips = [make_clip(session, library_id, name) for name in ("a.jpg", "b.jpg", "nowhere.jpg")]
            session.execute(text("UPDATE assets SET gps_lat = 41.0, gps_lon = -73.0, width = 64, height = 36,"
                                 " taken_at = '2024-05-01T12:00:00Z' WHERE asset_id = ANY(:ids)"), {"ids": clips[:2]})
            session.execute(text("UPDATE assets SET taken_at = '2024-05-01T12:00:00Z' WHERE asset_id = :a"),
                            {"a": clips[2]})
            session.commit()
        yield SimpleNamespace(client=client, robert=robert, tenant_id=tenant_id, library_id=library_id, clips=clips,
                              runner=runner, data=short_data, env=env)
    finally:
        runner.stop_all()
        use_added((), by=INSTALLED)


def _due(f, kind: str = ARTIFACT) -> list[str]:
    return [c["asset_id"] for c in due(tenant_database_url(f.tenant_id), kind, [f.library_id])]


def _job(f, kind: str = ARTIFACT):
    """One job of its due clips through the real runner: (the account, its failures charged, its outcome)."""
    acct = FakeAccount(scheduler_client(f.tenant_id))
    acct.tenant_id = f.tenant_id
    items = due(tenant_database_url(f.tenant_id), kind, [f.library_id])
    assert items, f"nothing of {kind} is due"
    try:
        outcome = run_job(PluginWork, acct, f.tenant_id, kind, items)
    except Crashed as crashed:
        outcome = crashed
    return acct.failures.charged, outcome


def _outputs(f, asset_id: str) -> dict | None:
    with get_tenant_session(f.tenant_id) as session:
        mine = PluginOutputRepository(session).of(asset_id).get("facts")
    return mine["outputs"] if mine else None


def _set(f, settings: dict, **more):
    return f.client.put("/v1/plugins/facts/settings", json={"settings": settings, **more}, headers=f.robert)


def _row(f) -> dict:
    r = f.client.get("/v1/producers", params={"library_id": f.library_id}, headers=f.robert)
    assert r.status_code == 200, r.text
    return {p["artifact"]: p for p in r.json()["producers"]}[ARTIFACT]


@pytest.mark.slow
def test_its_work_makes_each_clips_facts_from_what_it_declares_and_nothing_else(facts):
    f = facts
    a, b, nowhere = f.clips
    row = _row(f)
    assert (row["title"], row["plugin"], row["counts"]["missing"]) == (
        "Facts", {"plugin_id": "facts", "title": "Facts", "on": True}, 2)
    assert [(x["key"], x["fixed"]) for x in row["fields"]] == [("word", "Set in Settings › Plug-ins")]
    assert _due(f) == [a, b], "not the clip with no location: it doesn't apply to that one"
    r = f.client.put("/v1/plugins/facts/server-settings", json={"settings": {"token": "s3cret"}}, headers=f.robert)
    assert r.status_code == 200, r.text

    charged, outcome = _job(f)
    assert (charged, outcome) == ([], None)
    assert _outputs(f, a) == {
        "where": {"text": "Seen at 41.000, -73.000 (file)", "url": "https://www.openstreetmap.org/?mlat=41.0&mlon=-73.0"},
        "when": {"text": "Seen 2024-05-01T12:00:00Z"}, "what": {"text": "Seen: image 64x36"}}
    assert _due(f) == [] and _row(f)["counts"]["current"] == 2

    handed = (f.data / "plugins" / "facts" / "data" / "last-work.json").read_text()
    sent = json.loads(handed)
    assert sent["settings"] == {"word": "Seen", "behave": "well", "token": "s3cret"}
    assert [c["id"] for c in sent["clips"]] == ["1", "2"]
    assert {k for c in sent["clips"] for k in c} == {"id", "location", "capture_time", "metadata"}
    assert sent["clips"][0]["location"] == {"lat": 41.0, "lon": -73.0, "sure_m": 0.0, "source": "file"}
    assert sent["clips"][0]["capture_time"] == {"utc": "2024-05-01T12:00:00Z", "offset_min": None}
    for private in (f.tenant_id, a, b, f.library_id, "a.jpg"):
        assert private not in handed, f"{private} was handed to the plug-in"


@pytest.mark.slow
def test_a_clip_whose_inputs_change_is_due_again_and_shows_what_it_had_until_then(facts):
    f = facts
    a, b, _ = f.clips
    _job(f)
    with get_tenant_session(f.tenant_id) as session:
        session.execute(text("UPDATE assets SET gps_lat = 42.5 WHERE asset_id = :a"), {"a": a})
        session.commit()
    assert _due(f) == [a]
    assert _outputs(f, a)["where"]["text"] == "Seen at 41.000, -73.000 (file)", "kept until replaced"
    _job(f)
    assert _outputs(f, a)["where"]["text"] == "Seen at 42.500, -73.000 (file)"
    assert _due(f) == []


@pytest.mark.slow
@pytest.mark.parametrize("behave", ["clip-error", "own-error", "nonsense", "html", "leave-out", "slow"])
def test_whose_an_error_is_decides_what_is_charged(facts, behave, monkeypatch):
    import src.producers.plugin_work as plugin_work

    f = facts
    a, b, _ = f.clips
    monkeypatch.setattr(plugin_work, "WORK_TIMEOUT_SEC", 1.0)  # slow: it takes a minute
    assert _set(f, {"behave": behave}).status_code == 200
    charged, outcome = _job(f)
    if behave == "clip-error":  # the clip's: charged
        assert charged == [(ARTIFACT, a, "can't make sense of this one"), (ARTIFACT, b, "can't make sense of this one")]
    elif behave in ("own-error", "slow"):  # its own, or it didn't answer: nobody's, they wait
        assert charged == [] and outcome == [a, b]
        assert outcome.why == ({a: HELD, b: HELD} if behave == "own-error" else {a: PLUGIN_DOWN, b: PLUGIN_DOWN})
    else:  # what the protocol doesn't say: its fault, a crash (counted, uncharged now)
        assert charged == [] and isinstance(outcome, Crashed)
        assert outcome.asset_ids == ([a] if behave == "leave-out" else [a, b])
    saved = [x for x in (a, b) if _outputs(f, x) is not None]
    assert saved == ([b] if behave == "leave-out" else [])


@pytest.mark.slow
def test_a_plug_in_that_isnt_running_holds_its_clips_back_uncharged(facts):
    f = facts
    a, b, _ = f.clips
    f.runner.stop_all()
    charged, outcome = _job(f)
    assert charged == [] and outcome == [a, b] and outcome.why == {a: PLUGIN_DOWN, b: PLUGIN_DOWN}
    assert _outputs(f, a) is None


@pytest.mark.slow
def test_a_setting_that_remakes_asks_what_becomes_of_what_it_made(facts):
    f = facts
    a, b, _ = f.clips
    _job(f)
    r = _set(f, {"word": "Found"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "redo_on_change", r.text
    assert r.json()["error"]["details"]["clips"] == 2
    assert {s["key"]: s["value"] for s in _listed(f.client, f.robert)["facts"]["settings"]}["word"] == "Seen"

    assert _set(f, {"word": "Found"}, redo=False).status_code == 200
    row = _row(f)  # new ones only: what was made stays, its redo stopped by this person
    assert (row["redo_stopped"], row["counts"]["stale"]) == (True, 2)
    assert _set(f, {"word": "Spotted"}, redo=True).status_code == 200
    assert _row(f)["redo_stopped"] is False and _due(f, f"redo_{ARTIFACT}") == [a, b]
    _job(f, f"redo_{ARTIFACT}")
    assert _outputs(f, a)["when"] == {"text": "Spotted 2024-05-01T12:00:00Z"}
    assert _due(f, f"redo_{ARTIFACT}") == [] and _row(f)["counts"]["current"] == 2
    assert _set(f, {"behave": "html"}).status_code == 200, "a setting that doesn't remake asks nothing"


@pytest.mark.slow
def test_its_api_hands_out_only_what_it_declares_and_keeps_only_what_is_checked(facts):
    f = facts
    a, b, nowhere = f.clips
    r = f.client.post("/v1/plugins/facts/inputs", json={"asset_ids": [a, nowhere, "nope"]}, headers=f.robert)
    assert r.status_code == 200, r.text
    [(clip, given)] = r.json()["clips"].items()
    assert clip == a and set(given["inputs"]) == {"location", "capture_time", "metadata"} and given["inputs_hash"]
    lineage = PRODUCERS[ARTIFACT].producer

    def save(items, made: dict | None = None):
        made = made if made is not None else {"producer": lineage, "version": "1", "settings_hash": "x"}
        return f.client.post("/v1/plugins/facts/outputs", json={"items": items, "lineage": made}, headers=f.robert)

    fact = {"when": {"text": "Then"}}
    r = save([{"asset_id": a, "inputs_hash": "older", "outputs": fact},
              {"asset_id": nowhere, "inputs_hash": "x", "outputs": fact},
              {"asset_id": b, "inputs_hash": given["inputs_hash"], "outputs": fact}])
    assert r.status_code == 200 and r.json() == {"saved": 1, "stale": [a], "gone": [nowhere]}, r.text
    assert _outputs(f, b) == {"where": None, "when": {"text": "Then"}, "what": None}, "the same inputs as a"
    r = save([{"asset_id": a, "inputs_hash": given["inputs_hash"], "outputs": {"elsewhere": {"text": "x"}}}])
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_outputs", r.text
    r = save([{"asset_id": a, "inputs_hash": given["inputs_hash"], "outputs": fact}], made={})
    assert r.status_code == 422 and r.json()["error"]["code"] == "lineage_required", r.text

    from tests.real_account import someone

    viewer = someone(f.client, f.tenant_id, "viewer")
    assert f.client.post("/v1/plugins/facts/inputs", json={"asset_ids": [a]}, headers=viewer).status_code == 403
    assert f.client.post("/v1/plugins/nope/inputs", json={"asset_ids": [a]}, headers=f.robert).status_code == 404

    r = f.client.put("/v1/plugins/facts", json={"enabled": False}, headers=f.robert)
    assert r.status_code == 200, r.text
    r = f.client.post("/v1/plugins/facts/inputs", json={"asset_ids": [a]}, headers=f.robert)
    assert r.status_code == 409 and r.json()["error"]["code"] == "plugin_off", r.text
    assert _due(f) == [] and _outputs(f, b) is not None, "off: nothing more is made, and what was made stays"


@pytest.mark.slow
def test_installed_its_work_is_a_producer_and_uninstalled_it_isnt(facts):
    f = facts
    assert ARTIFACT in PRODUCERS
    assert cli.main(["plugins", "uninstall", "facts", "--yes", "--env", str(f.env)]) == 0
    refresh()
    assert ARTIFACT not in PRODUCERS
    r = f.client.get("/v1/producers", headers=f.robert)
    assert ARTIFACT not in {p["artifact"] for p in r.json()["producers"]}


@pytest.mark.slow
def test_what_an_uninstalled_plug_ins_work_left_breaks_nothing(facts):
    """Its lineage (made, made with nothing to say, failed), its settings and its pause stay in the account once
    it's gone (as a removed producer's do): every list, retry and run leaves them out."""
    f = facts
    a, b, _ = f.clips
    assert _set(f, {"word": "Kept"}).status_code == 200
    _job(f)  # a and b made
    with get_tenant_session(f.tenant_id) as session:
        session.execute(text("UPDATE assets SET gps_lat = 42.5 WHERE asset_id = :b"), {"b": b})
        session.commit()
    r = f.client.post("/v1/producers/failures", json={"items": [
        {"asset_id": clip, "artifact": ARTIFACT, "error": "it couldn't"} for clip in (a, b)]}, headers=f.robert)
    assert r.status_code == 200 and r.json() == {"recorded": 2}, r.text
    assert f.client.post(f"/v1/producers/{ARTIFACT}/pause", json={"scope": "work"}, headers=f.robert).status_code == 204
    failing = f.client.get("/v1/producers/failures", headers=f.robert).json()["items"]
    assert [i["artifact"] for i in failing] == [ARTIFACT, ARTIFACT]
    assert cli.main(["plugins", "uninstall", "facts", "--yes", "--env", str(f.env)]) == 0
    refresh()
    assert ARTIFACT not in PRODUCERS

    r = f.client.get("/v1/producers", params={"library_id": f.library_id}, headers=f.robert)
    assert r.status_code == 200 and ARTIFACT not in {p["artifact"] for p in r.json()["producers"]}, r.text
    r = f.client.get("/v1/producers/queue", headers=f.robert)
    assert r.status_code == 200 and ARTIFACT not in {s["target"] for s in r.json()["switches"]}, r.text
    r = f.client.get("/v1/producers/failures", params={"limit": 1}, headers=f.robert)
    assert r.status_code == 200 and r.json() == {"items": [], "next_cursor": None}, "no page of nothing pointing on"
    r = f.client.post("/v1/producers/failures/retry", json={"all": True}, headers=f.robert)
    assert r.status_code == 200 and r.json() == {"retried": 0}, r.text
    for scope in ("new", "redo"):
        r = f.client.post("/v1/producers/run", json={"producer": "all", "all": True, "scope": scope}, headers=f.robert)
        assert r.status_code == 200 and ARTIFACT not in {p["artifact"] for p in r.json()["producers"]}, r.text
    r = f.client.get(f"/v1/assets/{a}", headers=f.robert)
    assert r.status_code == 200 and r.json()["facts"] == [], "uninstalled: what it made isn't shown"


@pytest.mark.slow
def test_it_runs_only_while_its_on_in_some_account(facts):
    """Installed on by default, it runs; turned off by default with no account having chosen, it's on nowhere
    and stopped; an account turning it on starts it again (the runner reads each account's switch)."""
    f = facts
    assert cli.main(["plugins", "default", "facts", "off", "--yes", "--env", str(f.env)]) == 0
    _until(f.runner, lambda: _listed(f.client, f.robert)["facts"]["running"]["state"] == "off")
    assert _listed(f.client, f.robert)["facts"]["running"]["why"] == (
        "It's off in every account: it starts when an account turns it on")
    assert f.runner.plugins["facts"].process is None
    r = f.client.put("/v1/plugins/facts", json={"enabled": True}, headers=f.robert)
    assert r.status_code == 200, r.text
    _until(f.runner, lambda: _listed(f.client, f.robert)["facts"]["running"]["state"] == "ready")


# ---------------------------------------------------------------------------
# What it made, shown with the clip and searched (phase 2c)
# ---------------------------------------------------------------------------


def _found(f, words: str) -> set[str]:
    r = f.client.get(f"/v1/query?f=query:{words}&f=library:{f.library_id}", headers=f.robert)
    assert r.status_code == 200, r.text
    return {item["asset_id"] for item in r.json()["items"]}


@pytest.mark.slow
def test_its_facts_show_with_the_clip_and_are_found_by_search_while_it_is_on(facts):
    f = facts
    a, b, nowhere = f.clips
    assert _set(f, {"word": "Zanzibar"}).status_code == 200  # nothing made yet: nothing to ask
    _job(f)
    r = f.client.get(f"/v1/assets/{a}", headers=f.robert)
    assert r.status_code == 200, r.text
    assert r.json()["facts"] == [{"plugin_id": "facts", "title": "Facts", "credit": CREDIT, "facts": [
        {"key": "where", "label": "Where", "text": "Zanzibar at 41.000, -73.000 (file)",
         "url": "https://www.openstreetmap.org/?mlat=41.0&mlon=-73.0", "empty": False},
        {"key": "when", "label": "When", "text": "Zanzibar 2024-05-01T12:00:00Z", "url": None, "empty": False},
        {"key": "what", "label": "What", "text": "Zanzibar: image 64x36", "url": None, "empty": False}]}]
    assert f.client.get(f"/v1/assets/{nowhere}", headers=f.robert).json()["facts"] == []

    assert _found(f, "zanzibar") == {a, b}, "its searchable fact (when) finds them"
    r = f.client.get(f"/v1/query?f=query:zanzibar&f=library:{f.library_id}", headers=f.robert)
    assert {i["search_context"]["snippet"] for i in r.json()["items"]} == {"Zanzibar 2024-05-01T12:00:00Z"}
    assert _found(f, "file") == set(), "a fact that isn't searchable (where) finds nothing"

    assert f.client.put("/v1/plugins/facts", json={"enabled": False}, headers=f.robert).status_code == 200
    assert f.client.get(f"/v1/assets/{a}", headers=f.robert).json()["facts"] == [], "off: hidden"
    assert _found(f, "zanzibar") == set(), "off: not searched"
    assert _outputs(f, a) is not None, "and kept"
    assert f.client.put("/v1/plugins/facts", json={"enabled": True}, headers=f.robert).status_code == 200
    assert _found(f, "zanzibar") == {a, b}, "on again: found again, nothing made again"


@pytest.mark.slow
def test_a_fact_is_shown_and_found_only_while_its_clip_has_what_it_was_made_from(facts):
    """A clip whose location a person clears no longer has an input the plug-in reads: what it made is kept but
    neither shown nor searched (it would never be made again); the location back, it shows again."""
    f = facts
    a, b, _ = f.clips
    assert _set(f, {"word": "Zanzibar"}).status_code == 200
    _job(f)
    with get_tenant_session(f.tenant_id) as session:
        session.execute(text("UPDATE assets SET gps_lat = NULL, gps_lon = NULL WHERE asset_id = :a"), {"a": a})
        session.commit()
    assert f.client.get(f"/v1/assets/{a}", headers=f.robert).json()["facts"] == []
    assert _found(f, "zanzibar") == {b}
    assert _outputs(f, a) is not None, "kept"
    assert _due(f) == [], "and not one it applies to now"
    with get_tenant_session(f.tenant_id) as session:
        session.execute(text("UPDATE assets SET gps_lat = 41.0, gps_lon = -73.0 WHERE asset_id = :a"), {"a": a})
        session.commit()
    assert [x["text"] for x in f.client.get(f"/v1/assets/{a}", headers=f.robert).json()["facts"][0]["facts"]][0] == (
        "Zanzibar at 41.000, -73.000 (file)")
    assert _found(f, "zanzibar") == {a, b}


@pytest.mark.slow
def test_an_output_with_nothing_to_say_shows_its_empty_line_and_isnt_searched(facts):
    f = facts
    a, _, _ = f.clips
    assert _set(f, {"behave": "quiet", "word": "Zanzibar"}).status_code == 200
    _job(f)
    assert _outputs(f, a)["where"] is None, "nothing to say is what's kept"
    [shown] = f.client.get(f"/v1/assets/{a}", headers=f.robert).json()["facts"]
    assert shown["facts"][0] == {"key": "where", "label": "Where", "text": "Nowhere it knows", "url": None,
                                 "empty": True}
    assert _found(f, "nowhere") == set(), "an empty line is never searched"


# ---------------------------------------------------------------------------
# Its place lookup: Set location's Place search (phase 2d)
# ---------------------------------------------------------------------------


def _places(f, q: str):
    return f.client.get("/v1/places", params={"q": q}, headers=f.robert)


@pytest.mark.slow
def test_place_search_asks_each_plug_in_that_finds_places_and_says_which_couldnt(facts, monkeypatch):
    import src.server.plugin_lookups as lookups

    f = facts
    assert _listed(f.client, f.robert)["facts"]["lookups"] == ["places"]
    r = _places(f, "har")
    assert r.status_code == 200, r.text
    assert r.json() == {"places": [{
        "place_id": "facts:harbour", "name": "Harbour Point", "region": "Testshire", "country": "Testland",
        "label": "Harbour Point, Testshire, Testland", "lat": 41.1, "lon": -73.2, "population": 0,
        "kind": "lighthouse", "matched": None, "source": "facts", "source_title": "Facts"}],
        "credit": None, "credits": [{"source": "facts", **CREDIT}], "unavailable": []}
    asked = json.loads((f.data / "plugins" / "facts" / "data" / "last-lookup.json").read_text())
    assert asked == {"q": "har", "limit": 10, "settings": {"word": "Seen", "behave": "well", "token": ""}}

    assert _set(f, {"behave": "nonsense"}).status_code == 200
    r = _places(f, "har")
    assert r.status_code == 200 and r.json()["places"] == [], r.text
    [why] = r.json()["unavailable"]
    assert (why["plugin_id"], why["title"]) == ("facts", "Facts") and "doesn't say" in why["why"]
    monkeypatch.setattr(lookups, "LOOKUP_TIMEOUT_SEC", 1.0)  # slow: it takes a minute
    assert _set(f, {"behave": "slow"}).status_code == 200
    assert "didn't answer" in _places(f, "har").json()["unavailable"][0]["why"]

    assert f.client.put("/v1/plugins/facts", json={"enabled": False}, headers=f.robert).status_code == 200
    r = _places(f, "har")
    assert r.status_code == 409 and r.json()["error"]["code"] == "plugin_off", "nothing on here finds places"


@pytest.mark.slow
def test_a_location_set_from_a_plug_ins_place_says_where_it_came_from(facts):
    f = facts
    a, b, nowhere = f.clips

    def put(place: str):
        return f.client.put("/v1/assets/locations", json={"asset_ids": [nowhere], "place_id": place},
                            headers=f.robert)

    r = put("facts:hill")
    assert r.status_code == 200 and r.json()["updated"] == [nowhere], r.text
    location = f.client.get(f"/v1/assets/{nowhere}", headers=f.robert).json()["location"]
    assert (location["lat"], location["lon"], location["source"]) == (41.3, -73.4, "person")
    assert location["basis_summary"] == "Hill Top, Testland"
    assert location["credit"] == CREDIT
    for unknown in ("facts:nope", "elsewhere:hill", "facts:"):
        r = put(unknown)
        assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_place", (unknown, r.text)
    f.runner.stop_all()
    r = put("facts:harbour")
    assert r.status_code == 503 and r.json()["error"]["code"] == "plugin_unavailable", r.text
