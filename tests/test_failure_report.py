"""The scheduler tells the server what it couldn't make (ADR-016 phase 3, piece 2).

A clip that failed is reported with its error, so the server waits before
handing it out again; the reports go in batches, and what can't be sent is
kept for the next flush. (Which failures are a clip's: tests/test_scheduler_runners.py.)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from src.processing.failure_report import FailureReport

pytestmark = pytest.mark.fast

def _reported(client: MagicMock) -> list[dict]:
    return [item for c in client.post.call_args_list if c.args and c.args[0] == "/v1/producers/failures"
            for item in c.kwargs["json"]["items"]]


# ---------------------------------------------------------------------------
# The report itself
# ---------------------------------------------------------------------------


def test_failures_go_in_batches():
    client = MagicMock()
    report = FailureReport(client)
    for i in range(501):
        report.add("vision", f"ast_{i}", "timed out")
    report.flush()
    sizes = [len(c.kwargs["json"]["items"]) for c in client.post.call_args_list]
    assert sizes == [500, 1]


def test_an_error_is_kept_short_and_never_empty():
    client = MagicMock()
    report = FailureReport(client)
    report.add("vision", "ast_a", "x" * 5000)
    report.add("vision", "ast_b", TimeoutError())
    report.flush()
    a, b = client.post.call_args.kwargs["json"]["items"]
    assert len(a["error"]) == 2000 and b["error"] == "TimeoutError"


def _sent(client: MagicMock) -> list[str]:
    return [i["asset_id"] for c in client.post.call_args_list for i in c.kwargs["json"]["items"]]


def test_a_report_that_cant_be_sent_doesnt_stop_the_work_and_is_sent_later():
    # Review (Oct 9): a failed POST dropped the batch, so the clips were never charged.
    client = MagicMock()
    client.post.side_effect = httpx.ConnectError("connection reset")
    report = FailureReport(client)
    report.add("vision", "ast_a", "x")
    report.flush()  # no raise
    assert report.pending() == 1
    report.add("vision", "ast_b", "x")
    client.post.side_effect = None
    client.post.reset_mock()
    report.flush()
    assert _sent(client) == ["ast_a", "ast_b"] and report.pending() == 0


def test_a_server_404_keeps_the_report_too():
    # No "older server" branch: a 404 is the server's trouble now, not a reason to stop reporting.
    from src.client.cli.client import LumiverbAPIError

    client = MagicMock()
    client.post.side_effect = LumiverbAPIError("not_found", "Not Found", 404)
    report = FailureReport(client)
    report.add("vision", "ast_a", "x")
    report.flush()
    report.flush()
    assert client.post.call_count == 2 and report.pending() == 1


def test_one_item_the_server_refuses_doesnt_sink_its_batch():
    from src.client.cli.client import LumiverbAPIError

    client = MagicMock()

    def post(path, json):
        if any(i["asset_id"] == "ast_bad" for i in json["items"]):
            raise LumiverbAPIError("validation_error", "bad item", 422)

    client.post.side_effect = post
    report = FailureReport(client)
    for asset_id in ("ast_1", "ast_2", "ast_bad", "ast_3", "ast_4"):
        report.add("vision", asset_id, "x")
    report.flush()
    accepted = [i["asset_id"] for c in client.post.call_args_list for i in c.kwargs["json"]["items"]
                if "ast_bad" not in [j["asset_id"] for j in c.kwargs["json"]["items"]]]
    assert sorted(accepted) == ["ast_1", "ast_2", "ast_3", "ast_4"]
    assert report.pending() == 0


def test_what_the_server_cant_take_isnt_reported():
    client = MagicMock()
    report = FailureReport(client)
    report.add("no_such_artifact", "ast_a", "x")
    report.add("vision", "a" * 65, "x")
    report.add("vision", "ast_b", "nul\x00inside")
    report.flush()
    (item,) = client.post.call_args.kwargs["json"]["items"]
    assert item == {"asset_id": "ast_b", "artifact": "vision", "error": "nulinside"}


def test_unsent_reports_are_kept_up_to_a_limit(monkeypatch: pytest.MonkeyPatch):
    from src.processing import failure_report

    monkeypatch.setattr(failure_report, "KEEP", 3)
    client = MagicMock()
    client.post.side_effect = httpx.ConnectError("down")
    report = FailureReport(client)
    for i in range(5):
        report.add("vision", f"ast_{i}", "x")
    report.flush()
    assert report.pending() == 3
    client.post.side_effect = None
    client.post.reset_mock()
    report.flush()
    assert _sent(client) == ["ast_2", "ast_3", "ast_4"]  # the newest kept


# ---------------------------------------------------------------------------
# Each step reports what it couldn't make
# ---------------------------------------------------------------------------


def test_it_says_which_of_a_jobs_clips_it_charged_since_the_job_began() -> None:
    clock = [100.0]
    report = FailureReport(MagicMock(), clock=lambda: clock[0])
    report.add("scenes", "old", "before the job")
    clock[0] = 200.0
    report.add("scenes", "a", "bad file")
    report.add("vision", "b", "another artifact")
    assert report.charged("scenes", ["a", "b", "old", "c"], since=150.0) == {"a"}


def test_one_item_that_keeps_failing_otherwise_doesnt_block_the_rest_for_long():
    # A 500 caused by one item: it's tried alone a few times, then dropped;
    # the others go meanwhile.
    from src.processing import failure_report
    from src.client.cli.client import LumiverbAPIError

    client = MagicMock()

    def post(path, json):
        if any(i["asset_id"] == "ast_bad" for i in json["items"]):
            raise LumiverbAPIError("internal", "Internal Server Error", 500)

    client.post.side_effect = post
    report = FailureReport(client)
    for asset_id in ("ast_bad", "ast_1", "ast_2"):
        report.add("vision", asset_id, "x")
    for _ in range(failure_report.SINGLE_TRIES):
        report.flush()
    accepted = {i["asset_id"] for c in client.post.call_args_list for i in c.kwargs["json"]["items"]
                if all(j["asset_id"] != "ast_bad" for j in c.kwargs["json"]["items"])}
    assert accepted == {"ast_1", "ast_2"} and report.pending() == 0


def test_an_error_that_isnt_utf_8_is_made_sendable():
    import json

    client = MagicMock()
    report = FailureReport(client)
    report.add("probe", "ast_a", "ffprobe: /mnt/caf\udce9.mov: Invalid data")
    report.flush()
    (item,) = client.post.call_args.kwargs["json"]["items"]
    json.dumps(item).encode("utf-8")  # no lone surrogate left
    assert "Invalid data" in item["error"]
