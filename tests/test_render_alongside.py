"""The worker renders analysis proxies alongside the other steps.

On the brain, rendering ran first in every 15-minute cycle and never
finished: 1,200 renders at ~30 an hour held descriptions, faces,
embeddings, OCR, transcripts and scenes back for days, the GPU idle.
Rendering is CPU work on the originals, the rest mostly GPU work on
proxies, so they run at once.
"""

from __future__ import annotations

import io
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from src.client.cli.config import CLIConfig, save_config
from src.client.cli.repair import run_repair

pytestmark = pytest.mark.fast

MAC = "/Volumes/media-01"


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    mount = tmp_path / "mnt"
    (mount / "Footage").mkdir(parents=True)
    (mount / "Footage" / "b.mov").write_bytes(b"original")
    save_config(CLIConfig(root_map={MAC: str(mount)}))
    proxy = home / ".cache" / "lumiverb" / "analysis" / "ast_a.mp4"
    proxy.parent.mkdir(parents=True)
    proxy.write_bytes(b"proxy")
    return {"library_id": "lib_1", "name": "Footage", "root_path": f"{MAC}/Footage"}


def _run(library: dict, *, render_alongside: bool, render, transcribe, should_stop=None) -> None:
    pages = {
        "missing_analysis_proxy": [{"asset_id": "ast_b", "rel_path": "b.mov", "duration_sec": 4.0}],
        "missing_transcription": [{"asset_id": "ast_a", "rel_path": "a.mov", "duration_sec": 4.0,
                                   "has_analysis_proxy": True}],
    }

    def page_missing(_client, _library_id, **flags):
        [flag] = [k for k, v in flags.items() if v]
        return pages.get(flag, [])

    # A real console (not quiet): two live progress displays at once must work.
    console = Console(file=io.StringIO(), force_terminal=False, width=100)
    with (
        patch("src.client.cli.repair.get_repair_summary",
              return_value={"total_assets": 2, "missing_analysis_proxy": 1, "missing_transcription": 1}),
        patch("src.client.cli.repair._page_missing", side_effect=page_missing),
        patch("src.client.cli.repair._render_one", side_effect=render),
        patch("src.client.cli.repair._transcribe_one", side_effect=transcribe),
    ):
        run_repair(MagicMock(), library, job_type="all", console=console, should_stop=should_stop,
                   render_alongside=render_alongside)


def test_transcription_doesnt_wait_for_rendering(library: dict) -> None:
    transcribed = threading.Event()
    rendered = []

    def render(*args, **kwargs):
        # A long render: it ends only once transcription has run beside it.
        assert transcribed.wait(10), "transcription waited for rendering"
        rendered.append(args[2]["asset_id"])
        return "ok"

    def transcribe(*args, **kwargs):
        transcribed.set()
        return ("", "")

    _run(library, render_alongside=True, render=render, transcribe=transcribe)
    assert transcribed.is_set() and rendered == ["ast_b"]  # and the render finished before run_repair returned


def test_rendering_stops_when_the_cycle_ends(library: dict) -> None:
    calls = []
    stop = threading.Event()

    def render(*args, **kwargs):
        calls.append(1)
        stop.set()
        return "ok"

    _run(library, render_alongside=True, render=render, transcribe=lambda *a, **k: ("", ""),
         should_stop=stop.is_set)
    assert len(calls) == 1


def test_the_cli_still_renders_first(library: dict) -> None:
    order = []
    _run(library, render_alongside=False,
         render=lambda *a, **k: order.append("render") or "ok",
         transcribe=lambda *a, **k: order.append("transcribe") or ("", ""))
    assert order == ["render", "transcribe"]


def test_the_worker_renders_alongside() -> None:
    import inspect

    from src.client.cli import worker

    assert "render_alongside=True" in inspect.getsource(worker._enrich_library)
