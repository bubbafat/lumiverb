"""Pause switches (Robert, Oct 9, after #50): one per processing action, and
the global state derived from them.

Each switch is Scans, Upkeep or a producer the scheduler makes. Nothing
stores "pause all": it pauses every switch, and the global state is green
(all running), yellow (some paused, some running) or red (all paused).
"""

from __future__ import annotations

import pytest

from src.shared.producers import PAUSE_SCANS, PAUSE_UPKEEP, PRODUCERS, pause_state, pause_targets

pytestmark = pytest.mark.fast


def test_the_switches_are_scans_upkeep_and_each_producer_the_scheduler_makes():
    targets = pause_targets()
    assert targets[:2] == (PAUSE_SCANS, PAUSE_UPKEEP)
    assert set(targets[2:]) == {a for a, p in PRODUCERS.items() if p.scheduled}
    assert "proxy" not in targets  # scans make it: Scans pauses it


def test_nothing_paused_is_green_some_is_yellow_all_is_red():
    targets = set(pause_targets())
    assert pause_state(set()) == "running"
    assert pause_state({PAUSE_UPKEEP}) == "partly"
    assert pause_state(targets) == "paused"


def test_unpausing_one_switch_while_all_are_paused_is_yellow():
    targets = set(pause_targets())
    assert pause_state(targets - {"vision"}) == "partly"


def test_a_new_producer_while_all_are_paused_is_running_so_yellow():
    # Everything that existed was paused; a producer added since has no row: it runs.
    before = set(pause_targets()) - {"ocr"}
    assert pause_state(before) == "partly"


def test_a_row_for_what_is_no_longer_a_switch_counts_for_nothing():
    assert pause_state({"all", "gone_producer"}) == "running"
    assert pause_state(set(pause_targets()) | {"gone_producer"}) == "paused"
