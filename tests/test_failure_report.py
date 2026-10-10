"""The worker tells the server what it couldn't make (ADR-016 phase 3, piece 2).

Each step reports an item that failed, with its error, so the server waits
before handing it out again. A file that isn't on disk isn't a failure of
the step (the scan deals with it); a step that's stopped between items
reports nothing for the items it didn't take.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from rich.console import Console

from src.client.cli.config import CLIConfig, save_config
from src.processing.failure_report import FailureReport
from src.client.cli.repair import run_repair
from tests.ai_machine_fakes import built_in_whisper

pytestmark = pytest.mark.fast

MAC = "/Volumes/media-01"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(Path, "home", lambda: h)
    return h


@pytest.fixture
def library(home: Path, tmp_path: Path) -> dict:
    mount = tmp_path / "mnt"
    (mount / "Footage").mkdir(parents=True)
    for name in ("a.mov", "b.mov", "a.jpg"):
        (mount / "Footage" / name).write_bytes(b"original")
    save_config(CLIConfig(root_map={MAC: str(mount)}))
    return {"library_id": "lib_1", "name": "Footage", "root_path": f"{MAC}/Footage"}


@pytest.fixture(autouse=True)
def machine_offers_the_model():
    """An AI machine doing vision offers the model (checked before vision steps)."""
    from tests.ai_machine_fakes import one_machine

    with one_machine("m"):
        yield


def _cached(home: Path, asset_id: str) -> None:
    path = home / ".cache" / "lumiverb" / "analysis" / f"{asset_id}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"proxy")


def _run(client: MagicMock, library: dict, job_type: str, flag: str, page: list[dict]) -> None:
    with (
        patch("src.client.cli.repair.get_repair_summary", return_value={"total_assets": len(page), flag: len(page)}),
        patch("src.client.cli.repair._page_missing", return_value=page),
        built_in_whisper(),
    ):
        run_repair(client, library, job_type=job_type, console=Console(quiet=True))


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


def test_a_probe_that_fails_is_reported_with_its_error(library: dict):
    client = MagicMock()
    page = [{"asset_id": "ast_a", "rel_path": "a.mov"}, {"asset_id": "ast_x", "rel_path": "gone.mov"}]
    with patch("src.client.cli.repair.probe_video", side_effect=RuntimeError("moov atom not found")):
        _run(client, library, "probe", "missing_probe", page)
    # The file that isn't on disk is the scan's business, not a failure.
    assert _reported(client) == [{"asset_id": "ast_a", "artifact": "probe", "error": "moov atom not found"}]


def test_a_render_that_fails_is_reported(home: Path, library: dict):
    from src.processing.video.analysis_proxy import RenderError

    client = MagicMock()
    page = [{"asset_id": "ast_a", "rel_path": "a.mov", "duration_sec": 4.0}]
    with patch("src.client.cli.repair.render_analysis_proxy", side_effect=RenderError("ffmpeg exited 1")):
        _run(client, library, "render", "missing_analysis_proxy", page)
    assert _reported(client) == [{"asset_id": "ast_a", "artifact": "analysis_proxy", "error": "ffmpeg exited 1"}]


def test_a_transcription_that_fails_is_reported(home: Path, library: dict):
    _cached(home, "ast_a")
    client = MagicMock()
    page = [{"asset_id": "ast_a", "rel_path": "a.mov", "duration_sec": 4.0, "has_analysis_proxy": True}]
    with patch("src.client.cli.repair._transcribe_one", return_value=None):
        _run(client, library, "transcribe", "missing_transcription", page)
    [item] = _reported(client)
    assert (item["asset_id"], item["artifact"]) == ("ast_a", "transcript")


def test_an_embedding_that_fails_is_reported(home: Path, library: dict):
    client = MagicMock()
    page = [{"asset_id": "ast_a", "rel_path": "a.jpg"}]
    with (
        patch("src.processing.workers.embeddings.clip_provider.CLIPEmbeddingProvider"),
        patch("src.client.cli.repair._repair_embed_one", side_effect=RuntimeError("CUDA out of memory")),
    ):
        _run(client, library, "embed", "missing_embeddings", page)
    assert _reported(client) == [{"asset_id": "ast_a", "artifact": "clip", "error": "CUDA out of memory"}]


def test_scenes_that_fail_are_reported(home: Path, library: dict):
    _cached(home, "ast_a")
    client = MagicMock()
    page = [{"asset_id": "ast_a", "rel_path": "a.mov", "duration_sec": 4.0, "has_analysis_proxy": True}]
    with patch("src.processing.video_index.index_video_scenes", side_effect=RuntimeError("no keyframes")):
        _run(client, library, "video-scenes", "missing_video_scenes", page)
    assert _reported(client) == [{"asset_id": "ast_a", "artifact": "scenes", "error": "no keyframes"}]


def test_a_description_that_fails_is_reported(home: Path, library: dict):
    client = MagicMock()
    client.get.return_value.json.return_value = {"items": [{"asset_id": "ast_a", "rel_path": "a.jpg"}]}
    with (
        patch("src.client.cli.repair.get_repair_summary", return_value={"total_assets": 1, "missing_vision": 1}),
        patch("src.processing.workers.captions.factory.get_caption_provider"),
        patch("src.processing.ingest._backfill_one", return_value=None),
    ):
        run_repair(client, library, job_type="vision", console=Console(quiet=True))
    [item] = _reported(client)
    assert (item["asset_id"], item["artifact"]) == ("ast_a", "vision")


def test_success_reports_nothing(home: Path, library: dict):
    client = MagicMock()
    page = [{"asset_id": "ast_a", "rel_path": "a.mov"}]
    facet = MagicMock()
    facet.to_dict.return_value = {}
    with patch("src.client.cli.repair.probe_video", return_value=facet):
        _run(client, library, "probe", "missing_probe", page)
    assert _reported(client) == []


@pytest.mark.fast
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
