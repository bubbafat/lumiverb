"""The worker tells the server what it couldn't make (ADR-016 phase 3, piece 2).

Each step reports an item that failed, with its error, so the server waits
before handing it out again. A file that isn't on disk isn't a failure of
the step (the scan deals with it); a step that's stopped between items
reports nothing for the items it didn't take.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from src.client.cli.config import CLIConfig, save_config
from src.client.cli.failure_report import FailureReport
from src.client.cli.repair import run_repair

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
def endpoint_offers_the_model():
    """The vision endpoint answers (vision_guard asks it before vision steps)."""
    with patch("src.client.cli.vision_guard.check_model", return_value=None):
        yield


def _cached(home: Path, asset_id: str) -> None:
    path = home / ".cache" / "lumiverb" / "analysis" / f"{asset_id}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"proxy")


def _run(client: MagicMock, library: dict, job_type: str, flag: str, page: list[dict]) -> None:
    with (
        patch("src.client.cli.repair.get_repair_summary", return_value={"total_assets": len(page), flag: len(page)}),
        patch("src.client.cli.repair._page_missing", return_value=page),
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


def test_an_older_server_is_told_once_and_then_left_alone():
    from src.client.cli.client import LumiverbAPIError

    client = MagicMock()
    client.post.side_effect = LumiverbAPIError("not_found", "Not Found", 404)
    report = FailureReport(client)
    report.add("vision", "ast_a", "x")
    report.flush()
    report.add("vision", "ast_b", "x")
    report.flush()
    assert client.post.call_count == 1


def test_a_report_that_cant_be_sent_doesnt_stop_the_work():
    client = MagicMock()
    client.post.side_effect = RuntimeError("connection reset")
    report = FailureReport(client)
    report.add("vision", "ast_a", "x")
    report.flush()  # no raise


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
    from src.client.video.analysis_proxy import RenderError

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
        patch("src.client.workers.embeddings.clip_provider.CLIPEmbeddingProvider"),
        patch("src.client.cli.repair._repair_embed_one", side_effect=RuntimeError("CUDA out of memory")),
    ):
        _run(client, library, "embed", "missing_embeddings", page)
    assert _reported(client) == [{"asset_id": "ast_a", "artifact": "clip", "error": "CUDA out of memory"}]


def test_scenes_that_fail_are_reported(home: Path, library: dict):
    _cached(home, "ast_a")
    client = MagicMock()
    page = [{"asset_id": "ast_a", "rel_path": "a.mov", "duration_sec": 4.0, "has_analysis_proxy": True}]
    with patch("src.client.cli.video_index.index_video_scenes", side_effect=RuntimeError("no keyframes")):
        _run(client, library, "video-scenes", "missing_video_scenes", page)
    assert _reported(client) == [{"asset_id": "ast_a", "artifact": "scenes", "error": "no keyframes"}]


def test_a_description_that_fails_is_reported(home: Path, library: dict):
    client = MagicMock()
    client.get.return_value.json.return_value = {"items": [{"asset_id": "ast_a", "rel_path": "a.jpg"}]}
    with (
        patch("src.client.cli.repair.get_repair_summary", return_value={"total_assets": 1, "missing_vision": 1}),
        patch("src.client.cli.ingest._resolve_vision_config", return_value=("http://vision", None, "m", "test")),
        patch("src.client.workers.captions.factory.get_caption_provider"),
        patch("src.client.cli.ingest._backfill_one", return_value=None),
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
