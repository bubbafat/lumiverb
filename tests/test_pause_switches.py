"""Pause switches (Robert, Oct 9, after #50): one per processing action, and
the global state derived from them.

Each switch is Upkeep or a producer the scheduler makes; scans are paused
per library (ADR-018 phase 1). Pause all is stored as one "all" pause
(repository/lineage.py pauses() reads it as every switch, those added later
too, and every library's scans), and the global state is green (all
running), yellow (some paused, some running) or red (all paused).
"""

from __future__ import annotations

import pytest

from src.shared.producers import PAUSE_SCANS, PAUSE_UPKEEP, PRODUCERS, pause_state, pause_targets

pytestmark = pytest.mark.fast


def test_the_switches_are_upkeep_and_each_producer_the_scheduler_makes():
    targets = pause_targets()
    assert targets[0] == PAUSE_UPKEEP
    assert set(targets[1:]) == {a for a, p in PRODUCERS.items() if p.scheduled}
    assert PAUSE_SCANS not in targets  # each library's own
    assert "lumiverb.preview" not in targets  # scans make it: a library's Scans pause pauses it


def test_nothing_paused_is_green_some_is_yellow_all_is_red():
    targets = set(pause_targets())
    assert pause_state(set()) == "running"
    assert pause_state({PAUSE_UPKEEP}) == "partly"
    assert pause_state(targets) == "paused"


def test_unpausing_one_switch_while_all_are_paused_is_yellow():
    targets = set(pause_targets())
    assert pause_state(targets - {"lumiverb.image.description"}) == "partly"


def test_a_new_producer_while_all_are_paused_is_running_so_yellow():
    # Switches paused one by one: a producer added since has no row, so it runs
    # (Pause all is stored, so it would be paused: test_processing_pause_api).
    before = set(pause_targets()) - {"lumiverb.image.text"}
    assert pause_state(before) == "partly"


def test_a_row_for_what_is_no_longer_a_switch_counts_for_nothing():
    assert pause_state({"all", "gone_producer", PAUSE_SCANS}) == "running"
    assert pause_state(set(pause_targets()) | {"gone_producer"}) == "paused"


def test_libraries_scans_count_toward_the_state():
    targets = set(pause_targets())
    assert pause_state(set(), libraries_paused=1, libraries=3) == "partly"
    assert pause_state(targets, libraries_paused=2, libraries=3) == "partly"
    assert pause_state(targets, libraries_paused=3, libraries=3) == "paused"
    assert pause_state(set(), libraries_paused=0, libraries=3) == "running"
