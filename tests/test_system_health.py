"""The Admin page's system health: six rows, each green, yellow or red with
a one-line reason and where to fix it (src/server/system_health.py).

These are the rules alone, on plain inputs; tests/test_system_health_api.py
drives the endpoint that gathers them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.server import system_health as h

pytestmark = pytest.mark.fast

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def ago(**kw) -> datetime:
    return NOW - timedelta(**kw)


# -- Website ------------------------------------------------------------------


def test_website_is_green_when_the_database_answers():
    row = h.website_row(db_error=None, now=NOW)
    assert row.key == "website" and row.state == h.GREEN
    assert "database" in row.reason


def test_website_is_red_when_the_database_doesnt_answer():
    row = h.website_row(db_error="connection refused", now=NOW)
    assert row.state == h.RED and "connection refused" in row.reason


def test_website_is_yellow_in_maintenance_mode():
    row = h.website_row(db_error=None, now=NOW, maintenance="Upgrading Postgres")
    assert row.state == h.YELLOW and "Upgrading Postgres" in row.reason


# -- Processing -----------------------------------------------------------------


def test_processing_is_green_while_the_scheduler_runs():
    row = h.processing_row(at=ago(seconds=5), now=NOW)
    assert row.state == h.GREEN and row.reason == "Running."
    assert row.link == "/settings/processing" and row.checked_at == ago(seconds=5)


def test_processing_is_yellow_when_the_status_cant_be_read():
    row = h.processing_row(at=None, now=NOW, unreadable=True)
    assert row.state == h.YELLOW and "Couldn't read" in row.reason


def test_processing_is_red_when_the_scheduler_never_ran():
    row = h.processing_row(at=None, now=NOW)
    assert row.state == h.RED and "never" in row.reason


def test_processing_is_red_when_the_scheduler_is_silent_past_30_seconds():
    assert h.processing_row(at=ago(seconds=29), now=NOW).state == h.GREEN
    row = h.processing_row(at=ago(minutes=3), now=NOW)
    assert row.state == h.RED and "3 minutes" in row.reason


def test_processing_is_red_when_all_is_paused():
    row = h.processing_row(at=ago(seconds=5), now=NOW, paused_all=True)
    assert row.state == h.RED and row.reason.startswith("All paused")


def test_processing_is_yellow_when_partly_paused():
    row = h.processing_row(at=ago(seconds=5), now=NOW, scans_paused=True, paused_producers=["Faces"])
    assert row.state == h.YELLOW and row.reason.startswith("Partly paused")
    assert "scans" in row.reason and "Faces" in row.reason


def test_processing_is_yellow_when_clips_failed_lately():
    row = h.processing_row(at=ago(seconds=5), now=NOW, failing=3)
    assert row.state == h.YELLOW and "3 clips failed" in row.reason
    assert h.processing_row(at=ago(seconds=5), now=NOW, failing=1).reason.count("1 clip failed") == 1


def test_a_silent_scheduler_outranks_a_pause():
    row = h.processing_row(at=ago(minutes=5), now=NOW, paused_all=True)
    assert row.state == h.RED and "hasn't reported" in row.reason


# -- AI machines ----------------------------------------------------------------


def machine(name="GPU box", *, jobs=("vision",), enabled=True, online=True, checked=None, error=""):
    return {"name": name, "jobs": list(jobs), "enabled": enabled, "online": online, "error": error,
            "checked_at": checked if checked is not None else ago(seconds=30)}


VISION_ON = {"vision": "qwen3-vl", "transcripts": ""}


def test_ai_is_green_when_every_enabled_machine_is_online():
    row = h.ai_row(machines=[machine(), machine("Other")], job_models=VISION_ON, starved=set(), now=NOW)
    assert row.state == h.GREEN and row.reason == "All 2 machines online." and row.link == "/settings/ai"


def test_ai_is_green_with_no_ai_jobs_on():
    row = h.ai_row(machines=[machine(online=False)], job_models={"vision": "", "transcripts": ""},
                   starved=set(), now=NOW)
    assert row.state == h.GREEN and "No AI jobs" in row.reason


def test_ai_is_yellow_when_a_machine_is_offline():
    row = h.ai_row(machines=[machine(), machine("Laptop", online=False, error="refused")],
                   job_models=VISION_ON, starved=set(), now=NOW)
    assert row.state == h.YELLOW and "Laptop is offline" in row.reason


def test_an_off_machine_or_one_for_a_job_thats_off_isnt_counted():
    row = h.ai_row(machines=[machine(), machine("Off", enabled=False, online=False),
                             machine("Whisper", jobs=("transcripts",), online=False)],
                   job_models=VISION_ON, starved=set(), now=NOW)
    assert row.state == h.GREEN and row.reason == "The machine is online."


def test_ai_is_yellow_when_a_job_has_no_machine():
    row = h.ai_row(machines=[machine()], job_models={"vision": "qwen3-vl", "transcripts": "small"},
                   starved=set(), now=NOW)
    assert row.state == h.YELLOW and "No machine does transcripts" in row.reason


def test_ai_is_red_when_a_job_with_work_has_no_machine():
    row = h.ai_row(machines=[machine(online=False)], job_models=VISION_ON, starved={"vision"}, now=NOW)
    assert row.state == h.RED and "descriptions & text" in row.reason.lower()


def test_starved_jobs_that_are_off_arent_red():
    row = h.ai_row(machines=[machine()], job_models=VISION_ON, starved={"transcripts"}, now=NOW)
    assert row.state == h.GREEN


def test_ai_is_yellow_when_a_machine_hasnt_been_checked_for_long():
    """The scheduler checks each machine of a job that's on every minute."""
    fresh = h.ai_row(machines=[machine(checked=ago(minutes=9))], job_models=VISION_ON, starved=set(), now=NOW)
    assert fresh.state == h.GREEN
    row = h.ai_row(machines=[machine(checked=ago(hours=2))], job_models=VISION_ON, starved=set(), now=NOW)
    assert row.state == h.YELLOW and "hasn't been checked for 2 hours" in row.reason


def test_with_the_scheduler_down_the_machines_arent_called_stale_one_by_one():
    """Nothing checks them while it's down: one line says so (Processing is red for it)."""
    row = h.ai_row(machines=[machine(checked=ago(hours=12)), machine("Other", checked=ago(hours=12)),
                             machine("Laptop", online=False, checked=ago(hours=12))],
                   job_models=VISION_ON, starved=set(), now=NOW, scheduler_live=False)
    assert row.state == h.YELLOW
    assert row.reason == "Laptop is offline. Not checked for 12 hours: the scheduler isn't running."


def test_ai_is_yellow_when_a_machine_was_never_checked():
    m = {**machine(), "online": None, "checked_at": None}
    row = h.ai_row(machines=[m], job_models=VISION_ON, starved=set(), now=NOW)
    assert row.state == h.YELLOW and "hasn't been checked yet" in row.reason


# -- Search -----------------------------------------------------------------------


def search(**kw):
    args = {"enabled": True, "fallback_on": True, "quickwit": "ok", "quickwit_error": "",
            "last_fallback": None, "last_failure": None, "unsynced": 0, "now": NOW}
    return h.search_row(**{**args, **kw})


def test_search_is_green_when_quickwit_answers():
    row = search()
    assert row.state == h.GREEN and "Quickwit" in row.reason


def test_search_is_yellow_when_quickwit_is_down_and_postgres_stands_in():
    row = search(quickwit="down", quickwit_error="ConnectionError")
    assert row.state == h.YELLOW
    assert row.reason == "Quickwit isn't answering (ConnectionError): search uses Postgres (simpler matching)."


def test_search_is_yellow_when_the_index_is_missing():
    row = search(quickwit="missing")
    assert row.state == h.YELLOW and "index is missing" in row.reason and "Postgres" in row.reason


def test_search_is_yellow_when_quickwit_is_off():
    row = search(enabled=False, quickwit=None)
    assert row.state == h.YELLOW and "Postgres" in row.reason


def test_search_is_yellow_after_a_recent_fallback():
    row = search(last_fallback=(ago(minutes=2), "Quickwit timed out"))
    assert row.state == h.YELLOW and "Quickwit timed out" in row.reason and "2 minutes" in row.reason
    assert search(last_fallback=(ago(minutes=30), "old")).state == h.GREEN


def test_search_is_yellow_while_many_clips_wait_to_be_indexed():
    assert search(unsynced=99).state == h.GREEN
    row = search(unsynced=1500)
    assert row.state == h.YELLOW and "1,500 clips" in row.reason


def test_search_is_red_when_searches_failed_lately():
    row = search(last_failure=(ago(minutes=1), "database timeout"))
    assert row.state == h.RED and "database timeout" in row.reason
    assert search(last_failure=(ago(hours=1), "old")).state == h.GREEN


def test_search_is_red_when_quickwit_is_down_and_the_fallback_is_off():
    row = search(quickwit="down", fallback_on=False)
    assert row.state == h.RED and "fallback is off" in row.reason


# -- Storage ----------------------------------------------------------------------


LIBS = [("lib_a", "Photos"), ("lib_b", "Footage")]


def test_storage_is_green_when_every_library_is_reachable():
    row = h.storage_row(libraries=LIBS, unreachable=[], at=ago(seconds=5), now=NOW)
    assert row.state == h.GREEN and "2 libraries" in row.reason


def test_storage_is_red_when_a_library_is_unreachable():
    row = h.storage_row(libraries=LIBS, unreachable=["lib_b"], at=ago(seconds=5), now=NOW)
    assert row.state == h.RED and "Footage" in row.reason and "Photos" not in row.reason
    assert row.link == "/libraries/lib_b/settings"


def test_storage_with_several_unreachable_links_to_the_libraries():
    row = h.storage_row(libraries=LIBS, unreachable=["lib_a", "lib_b"], at=ago(seconds=5), now=NOW)
    assert row.state == h.RED and row.link == "/libraries"


def test_storage_ignores_unreachable_ids_that_arent_libraries_any_more():
    row = h.storage_row(libraries=LIBS, unreachable=["lib_gone"], at=ago(seconds=5), now=NOW)
    assert row.state == h.GREEN


def test_storage_is_yellow_when_nothing_has_checked_lately():
    row = h.storage_row(libraries=LIBS, unreachable=[], at=ago(minutes=10), now=NOW)
    assert row.state == h.YELLOW and "isn't running" in row.reason
    assert h.storage_row(libraries=LIBS, unreachable=[], at=None, now=NOW).state == h.YELLOW


def test_storage_is_green_with_no_libraries():
    assert h.storage_row(libraries=[], unreachable=[], at=None, now=NOW).state == h.GREEN


# -- Disk -------------------------------------------------------------------------

TB = 1024 ** 4


def test_disk_is_green_with_plenty_free():
    row = h.disk_row(free=TB // 2, total=2 * TB, now=NOW)
    assert row.state == h.GREEN and "25%" in row.reason and "512.0 GB" in row.reason


def test_disk_is_yellow_under_15_percent_free():
    assert h.disk_row(free=int(0.16 * TB), total=TB, now=NOW).state == h.GREEN
    assert h.disk_row(free=int(0.14 * TB), total=TB, now=NOW).state == h.YELLOW


def test_disk_is_red_under_5_percent_free():
    row = h.disk_row(free=int(0.04 * TB), total=TB, now=NOW)
    assert row.state == h.RED and "4%" in row.reason


def test_disk_is_yellow_when_it_cant_be_read():
    row = h.disk_row(free=None, total=None, now=NOW, error="No such file or directory")
    assert row.state == h.YELLOW and "No such file" in row.reason


# -- Overall ----------------------------------------------------------------------


def test_overall_is_the_worst_row():
    green = h.disk_row(free=TB, total=TB, now=NOW)
    yellow = h.disk_row(free=int(0.1 * TB), total=TB, now=NOW)
    red = h.disk_row(free=0, total=TB, now=NOW)
    assert h.overall([green, green]) == h.GREEN
    assert h.overall([green, yellow]) == h.YELLOW
    assert h.overall([yellow, red, green]) == h.RED


def test_ago_reads_plainly():
    assert h.ago(timedelta(seconds=40)) == "40 seconds"
    assert h.ago(timedelta(minutes=1)) == "1 minute"
    assert h.ago(timedelta(minutes=90)) == "2 hours"
    assert h.ago(timedelta(days=3)) == "3 days"


# -- Quickwit: whether the account's index is there, and a missing one made again --


def test_ensure_index_says_it_made_a_missing_one(tmp_path):
    from src.server.search.quickwit_client import QuickwitClient

    (tmp_path / "asset_index_schema.json").write_text('{"doc_mapping": {}}')
    settings = MagicMock(quickwit_enabled=True, quickwit_url="http://localhost:7280")
    with patch("src.server.search.quickwit_client.get_settings", return_value=settings):
        qw = QuickwitClient(schema_dir=tmp_path)
    with patch("src.server.search.quickwit_client.requests.get", return_value=MagicMock(status_code=404)), \
         patch("src.server.search.quickwit_client.requests.post", return_value=MagicMock(status_code=200)):
        assert qw.ensure_tenant_index("tnt_1") is True
    with patch("src.server.search.quickwit_client.requests.get",
               return_value=MagicMock(status_code=200, json=lambda: {})), \
         patch("src.server.search.quickwit_client.requests.post") as post:
        assert qw.ensure_tenant_index("tnt_1") is False
        post.assert_not_called()
    # Quickwit can't say (a 5xx): not taken for missing, so nothing is reindexed.
    with patch("src.server.search.quickwit_client.requests.get", return_value=MagicMock(status_code=503)), \
         patch("src.server.search.quickwit_client.requests.post") as post:
        with pytest.raises(RuntimeError):
            qw.ensure_tenant_index("tnt_1")
        post.assert_not_called()


def test_index_state_reads_quickwit(tmp_path):
    import requests

    from src.server.search.quickwit_client import QuickwitClient

    settings = MagicMock(quickwit_enabled=True, quickwit_url="http://localhost:7280")
    with patch("src.server.search.quickwit_client.get_settings", return_value=settings):
        qw = QuickwitClient(schema_dir=tmp_path)
    get = "src.server.search.quickwit_client.requests.get"
    with patch(get, return_value=MagicMock(status_code=200)):
        assert qw.tenant_index_state("t") == ("ok", "")
    with patch(get, return_value=MagicMock(status_code=404)):
        assert qw.tenant_index_state("t") == ("missing", "")
    with patch(get, return_value=MagicMock(status_code=503)):
        assert qw.tenant_index_state("t") == ("down", "it answered 503")
    with patch(get, side_effect=requests.ConnectionError("refused")):
        state, why = qw.tenant_index_state("t")
    assert (state, why) == ("down", "ConnectionError")
