"""Producers added while Lumiverb runs (ADR-023 phase 2: each installed
plug-in's work). Installing a plug-in mustn't need the API or the scheduler
started again, so the list of producers can change while they run: what's
worked out from it (lineage's rules, the missing_* conditions, the
scheduler's kinds and the queue's join) is worked out again, the scheduler
takes up a kind added, and one taken away is handed out no more.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from src.producers import registry
from src.producers import use_added as _use_added
from src.producers.contract import FIND, IMAGE, REDO, AiJob, Pool, ProducerSpec
from src.server.repository.lineage import Pauses

pytestmark = pytest.mark.fast

NAME = "example-facts"  # a plug-in's work is named by its plug-in's id (ADR-024): a dash in it too
BY = "test"  # what a test adds is its own: the installed plug-ins' come and go apart


def use_added(specs) -> bool:
    return _use_added(specs, by=BY)


def _spec(name: str = NAME, **changes) -> ProducerSpec:
    spec = ProducerSpec(
        artifact=name, plugin=name, version="1", media=IMAGE, title="Example facts",
        applies="a.media_type = 'image'",
        # Made: its own lineage row (a failure's names no producer).
        made=(f"EXISTS (SELECT 1 FROM artifact_lineage x WHERE x.asset_id = a.asset_id AND x.artifact = '{name}'"
              f" AND x.producer = '{name}')"),
        run="tests.test_added_producers:Nothing", tier=FIND,
        pool=Pool(name, slots=1), batch=10)
    return replace(spec, **changes)


@pytest.fixture(autouse=True)
def _none_added_after():
    yield
    use_added(())


def test_a_producer_added_shows_everywhere_the_producers_are_read():
    from src.producers.names import remakes_of, sql_spelling
    from src.server.repository import lineage
    from src.server.repository.assets import MISSING_CONDITIONS, OUTSTANDING_CONDITIONS
    from src.server.scheduler.kinds import KINDS, QUEUED
    from src.server.scheduler.service import REDONE
    from src.shared.producers import (
        ARTIFACTS,
        PRODUCERS,
        SCHEDULED,
        effective_settings,
        pause_targets,
    )

    def seen() -> dict:
        return {
            "listed": NAME in PRODUCERS and NAME in ARTIFACTS and list(PRODUCERS)[-1] == NAME,
            "scheduled": NAME in SCHEDULED,
            "rules": [lineage.APPLIES.get(NAME), NAME in lineage.MADE, lineage.RECHECK.get(NAME)],
            "joined": f"src_{sql_spelling(NAME)}" in lineage.lineage_join()
            and f"wait_{sql_spelling(NAME)}" in lineage.lineage_join(),
            "conditions": NAME in MISSING_CONDITIONS and NAME in OUTSTANDING_CONDITIONS,
            "kinds": [KINDS[k].spec.tier for k in (NAME, remakes_of(NAME)) if k in KINDS],
            "queued": sorted(k.name for k in QUEUED if NAME in k.name),
            "redone": NAME in REDONE and lineage.redoable(NAME),
            "switch": NAME in pause_targets(),
        }

    assert seen() == {"listed": False, "scheduled": False, "rules": [None, False, None], "joined": False,
                      "conditions": False, "kinds": [], "queued": [], "redone": False, "switch": False}
    assert use_added([_spec()]) is True
    assert seen() == {"listed": True, "scheduled": True, "rules": ["a.media_type = 'image'", True, "false"],
                      "joined": True, "conditions": True, "kinds": [FIND, REDO],
                      "queued": [NAME, f"redo:{NAME}"], "redone": True, "switch": True}
    assert effective_settings(NAME) == {}
    assert use_added([_spec()]) is False  # the same again changes nothing
    assert use_added(()) is True
    assert seen()["listed"] is False and seen()["kinds"] == [] and not seen()["joined"]


def test_the_list_handed_out_never_changes_under_whoever_holds_it():
    before = registry()
    use_added([_spec()])
    assert NAME not in before and NAME in registry()
    assert registry() is registry()  # made once for each change


@pytest.mark.parametrize("changes, says", [
    ({"artifact": "example"}, "is its plug-in's id"),
    ({"artifact": "lumiverb.image.facts", "plugin": "lumiverb.image.facts"}, "is its plug-in's id"),
    ({"storage": True}, "from what Lumiverb keeps"),
    ({"made_at_scan": True}, "from what Lumiverb keeps"),
    ({"pool": Pool(NAME, job=AiJob("geo", "Places", guard="x:Y"))}, "from what Lumiverb keeps"),
    ({"run": "", "pool": None}, "from what Lumiverb keeps"),
    ({"pool": Pool("cpu", slots=2)}, "pool is its own"),
    ({"pool": Pool(NAME, sized_by="renders")}, "pool is its own"),
    ({"reads": ("lumiverb.teleport",)}, "reads what nothing makes"),
    ({"needs": ("lumiverb.preview",)}, "worked out from its reads, never declared"),
])
def test_a_producer_added_is_a_plugins_work_and_nothing_else(changes, says):
    use_added([_spec("kept-one")])
    before = registry()
    with pytest.raises(ValueError, match=says):
        use_added([_spec(**changes)])
    assert registry() is before  # refused: the list stays as it was


def test_one_of_lumiverbs_own_is_named_by_what_it_makes():
    from src.producers import _check

    with pytest.raises(ValueError, match="is named by what it makes"):
        _check([_spec()])


def _account() -> MagicMock:
    acct = MagicMock(tenant_id="t1", settings_ready=True)
    acct.stopping = threading.Event()
    acct.library_ids.return_value = ["lib_1"]
    acct.capacity.return_value = 1
    acct.storage_seen.return_value = {}
    return acct


def _scheduler(monkeypatch, ran: list, asked: list):
    from src.server.scheduler import runners
    from src.server.scheduler.kinds import QUEUED
    from src.server.scheduler.service import Scheduler

    def runner(kind: str):
        return lambda acct, job: ran.append((kind, tuple(job.asset_ids)))

    # The runners it loads itself, as it does when none are handed to it: a kind taken up later's too.
    monkeypatch.setattr(runners, "runners", lambda: {k: runner(k) for k in dict.fromkeys(q.base for q in QUEUED)})
    # Its plug-in running (a plug-in's work waits while it isn't: test_plugin_work).
    monkeypatch.setattr(Scheduler, "_plugins_not_running", lambda self: {})

    def candidates(tenant_id, kind, libraries, skip=()):
        asked.append(kind.name)
        return [{"asset_id": f"{kind.name}-1", "created_at": "2026-10-18", "library_id": "lib_1",
                 "rel_path": "a.jpg"}] if kind.name == NAME else []

    accounts = {"t1": _account()}
    return Scheduler(lambda: accounts, capacity={"scan": 1}, candidates=candidates, scan=MagicMock(),
                     inline_refill=True, on_hold=lambda tenant_id: Pauses(), retry_requested=lambda tenant_id: None,
                     write_status=lambda tenant_id, status: None, last_status=lambda tenant_id: None,
                     crashed=lambda *a: [], new_versions=lambda tenant_id: None)


def _settle(s) -> None:
    deadline = time.monotonic() + 60
    while s.running and time.monotonic() < deadline:
        s.collect(timeout=0.1)


def test_the_scheduler_takes_up_a_kind_added_while_it_runs_and_lets_go_of_one_taken_away(monkeypatch):
    ran: list = []
    asked: list = []
    s = _scheduler(monkeypatch, ran, asked)
    s.tick()
    _settle(s)
    assert NAME not in asked and ran == []

    use_added([_spec()])
    s.tick()
    _settle(s)
    assert NAME in asked
    assert ran == [(NAME, (f"{NAME}-1",))]  # its runner loaded, its pool's one slot given
    assert s.dispatcher.free(NAME) == 1  # the job done, its slot free again

    use_added(())
    asked.clear()
    s.tick()
    _settle(s)
    assert NAME not in asked  # listed no more
    assert ran == [(NAME, (f"{NAME}-1",))]
