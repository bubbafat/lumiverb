"""Enrichment reads analysis proxies, not originals (ADR-016 phase 2).

`render` makes a proxy for each video while its storage is reachable.
Transcription, scene detection and scene vision then read the proxy, so
they keep going while the storage holding the originals sleeps. Probing
and rendering wait for it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from src.client.cli.config import CLIConfig, save_config
from src.client.cli.repair import run_repair

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
    (mount / "Footage" / "a.mov").write_bytes(b"original a")
    save_config(CLIConfig(root_map={MAC: str(mount)}))
    return {"library_id": "lib_1", "name": "Footage", "root_path": f"{MAC}/Footage"}


@pytest.fixture
def asleep(home: Path) -> dict:
    """A library whose storage isn't reachable from here right now."""
    save_config(CLIConfig(root_map={MAC: str(home / "not-mounted")}))
    return {"library_id": "lib_1", "name": "Footage", "root_path": f"{MAC}/Footage"}


def _cached(home: Path, asset_id: str, body: bytes = b"proxy") -> Path:
    path = home / ".cache" / "lumiverb" / "analysis" / f"{asset_id}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _run(client: MagicMock, library: dict, job_type: str, summary: dict, pages: dict[str, list[dict]], **patches):
    def page_missing(_client, _library_id, **flags):
        [flag] = [k for k, v in flags.items() if v]
        return pages.get(flag, [])

    with (
        patch("src.client.cli.repair.get_repair_summary", return_value={"total_assets": 2, **summary}),
        patch("src.client.cli.repair._page_missing", side_effect=page_missing),
    ):
        run_repair(client, library, job_type=job_type, console=Console(quiet=True))


def _posts(client: MagicMock, suffix: str) -> list[str]:
    return [c.args[0] for c in client.post.call_args_list if c.args and c.args[0].endswith(suffix)]


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_render_is_an_enrich_job() -> None:
    from src.client.cli.main import ENRICH_TYPES
    from src.client.cli.repair import REPAIR_TYPES

    assert "render" in ENRICH_TYPES and "render" in REPAIR_TYPES


@pytest.mark.fast
def test_render_makes_uploads_and_caches_a_proxy(home: Path, library: dict) -> None:
    client = MagicMock()
    rendered_from: list[Path] = []

    def fake_render(source: Path, dest: Path, settings=None, *, timeout=None) -> None:
        rendered_from.append(source)
        dest.write_bytes(b"rendered from " + source.read_bytes())

    pages = {"missing_analysis_proxy": [
        {"asset_id": "ast_a", "rel_path": "a.mov", "duration_sec": 4.0},
        {"asset_id": "ast_b", "rel_path": "b.mov", "duration_sec": 4.0},  # not on disk
    ]}
    with patch("src.client.cli.repair.render_analysis_proxy", side_effect=fake_render):
        _run(client, library, "render", {"missing_analysis_proxy": 2}, pages)

    assert [p.name for p in rendered_from] == ["a.mov"]
    assert _posts(client, "/artifacts/analysis_proxy") == ["/v1/assets/ast_a/artifacts/analysis_proxy"]
    cache = home / ".cache" / "lumiverb" / "analysis"
    assert (cache / "ast_a.mp4").read_bytes() == b"rendered from original a"
    assert sorted(p.name for p in cache.iterdir()) == ["ast_a.mp4"]


@pytest.mark.fast
def test_render_waits_while_storage_sleeps(home: Path, asleep: dict) -> None:
    client = MagicMock()
    pages = {"missing_analysis_proxy": [{"asset_id": "ast_a", "rel_path": "a.mov"}]}
    with patch("src.client.cli.repair.render_analysis_proxy") as render:
        _run(client, asleep, "render", {"missing_analysis_proxy": 1}, pages)
    render.assert_not_called()
    assert client.post.call_count == 0


@pytest.mark.fast
def test_a_failed_render_uploads_nothing_and_leaves_nothing(home: Path, library: dict) -> None:
    from src.client.video.analysis_proxy import RenderError

    client = MagicMock()
    pages = {"missing_analysis_proxy": [{"asset_id": "ast_a", "rel_path": "a.mov"}]}
    with patch("src.client.cli.repair.render_analysis_proxy", side_effect=RenderError("bad file")):
        _run(client, library, "render", {"missing_analysis_proxy": 1}, pages)
    assert _posts(client, "/artifacts/analysis_proxy") == []
    cache = home / ".cache" / "lumiverb" / "analysis"
    assert not cache.exists() or list(cache.iterdir()) == []


@pytest.mark.fast
def test_a_failed_upload_keeps_nothing_in_the_cache(home: Path, library: dict) -> None:
    client = MagicMock()
    client.post.side_effect = RuntimeError("server went away")

    def fake_render(source: Path, dest: Path, settings=None, *, timeout=None) -> None:
        dest.write_bytes(b"proxy")

    pages = {"missing_analysis_proxy": [{"asset_id": "ast_a", "rel_path": "a.mov"}]}
    with patch("src.client.cli.repair.render_analysis_proxy", side_effect=fake_render):
        _run(client, library, "render", {"missing_analysis_proxy": 1}, pages)
    cache = home / ".cache" / "lumiverb" / "analysis"
    assert not cache.exists() or list(cache.iterdir()) == []


# ---------------------------------------------------------------------------
# transcription, scenes and scene vision read the proxy
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_transcription_reads_the_proxy_while_storage_sleeps(home: Path, asleep: dict) -> None:
    proxy = _cached(home, "ast_a")
    client = MagicMock()
    pages = {"missing_transcription": [
        {"asset_id": "ast_a", "rel_path": "a.mov", "has_analysis_proxy": True},
        {"asset_id": "ast_b", "rel_path": "b.mov", "has_analysis_proxy": False},
    ]}
    with patch("src.client.cli.repair._transcribe_one", return_value=("1\n00:00:00,000 --> 00:00:01,000\nhi\n", "en")) as tr:
        _run(client, asleep, "transcribe", {"missing_transcription": 2}, pages)

    assert [c.args[0] for c in tr.call_args_list] == [proxy]
    assert _posts(client, "/transcript") == ["/v1/assets/ast_a/transcript"]


@pytest.mark.fast
def test_transcription_downloads_a_proxy_it_does_not_have(home: Path, asleep: dict) -> None:
    from contextlib import contextmanager

    client = MagicMock()

    @contextmanager
    def stream(path: str, **kw):
        resp = MagicMock(status_code=200)
        resp.iter_bytes.return_value = [b"downloaded"]
        yield resp

    client.stream.side_effect = stream
    pages = {"missing_transcription": [{"asset_id": "ast_a", "rel_path": "a.mov", "has_analysis_proxy": True}]}
    with patch("src.client.cli.repair._transcribe_one", return_value=("", "")) as tr:
        _run(client, asleep, "transcribe", {"missing_transcription": 1}, pages)
    [call] = tr.call_args_list
    assert call.args[0].read_bytes() == b"downloaded"


@pytest.mark.fast
def test_scene_detection_reads_the_proxy_while_storage_sleeps(home: Path, asleep: dict) -> None:
    proxy = _cached(home, "ast_a")
    client = MagicMock()
    pages = {"missing_video_scenes": [
        {"asset_id": "ast_a", "rel_path": "a.mov", "duration_sec": 4.0, "has_analysis_proxy": True},
        {"asset_id": "ast_b", "rel_path": "b.mov", "duration_sec": 4.0, "has_analysis_proxy": False},
    ]}
    with patch("src.client.cli.video_index.index_video_scenes",
               return_value={"scenes": 1, "chunks": 1, "elapsed": 0.1}) as idx:
        _run(client, asleep, "video-scenes", {"missing_video_scenes": 2}, pages)
    assert [c.kwargs["source_path"] for c in idx.call_args_list] == [proxy]


@pytest.mark.fast
def test_scene_vision_reads_the_proxy_while_storage_sleeps(home: Path, asleep: dict) -> None:
    proxy = _cached(home, "ast_a")
    client = MagicMock()
    pages = {"missing_scene_vision": [
        {"asset_id": "ast_a", "rel_path": "a.mov", "has_analysis_proxy": True},
        {"asset_id": "ast_b", "rel_path": "b.mov", "has_analysis_proxy": False},
    ]}
    with (
        patch("src.client.cli.ingest._resolve_vision_config", return_value=(None, None, None, "none")),
        patch("src.client.cli.video_index.enrich_video_scenes",
              return_value={"enriched": 1, "skipped": 0, "failed": 0, "elapsed": 0.1}) as enr,
    ):
        _run(client, asleep, "scene-vision", {"missing_scene_vision": 2}, pages)
    assert [c.kwargs["source_path"] for c in enr.call_args_list] == [proxy]


@pytest.mark.fast
def test_all_renders_before_reading_proxies(home: Path, library: dict) -> None:
    order: list[str] = []
    client = MagicMock()

    def page_missing(_client, _library_id, **flags):
        [flag] = [k for k, v in flags.items() if v]
        order.append(flag)
        return []

    summary = {"total_assets": 1, "missing_probe": 1, "missing_analysis_proxy": 1, "missing_transcription": 1,
               "missing_video_scenes": 1, "missing_scene_vision": 1}
    with (
        patch("src.client.cli.repair.get_repair_summary", return_value=summary),
        patch("src.client.cli.repair._page_missing", side_effect=page_missing),
        patch("src.client.cli.ingest._resolve_vision_config", return_value=(None, None, None, "none")),
    ):
        run_repair(client, library, job_type="all", console=Console(quiet=True))
    assert order.index("missing_probe") < order.index("missing_analysis_proxy") < order.index("missing_transcription")
    assert order.index("missing_analysis_proxy") < order.index("missing_video_scenes")


@pytest.mark.fast
def test_scene_steps_count_what_they_did(home: Path, asleep: dict) -> None:
    _cached(home, "ast_a")
    client = MagicMock()
    pages = {"missing_video_scenes": [
        {"asset_id": "ast_a", "rel_path": "a.mov", "duration_sec": 4.0, "has_analysis_proxy": True},
    ]}
    console = Console(record=True, width=200)

    def page_missing(_client, _library_id, **flags):
        [flag] = [k for k, v in flags.items() if v]
        return pages.get(flag, [])

    with (
        patch("src.client.cli.repair.get_repair_summary", return_value={"total_assets": 1, "missing_video_scenes": 1}),
        patch("src.client.cli.repair._page_missing", side_effect=page_missing),
        patch("src.client.cli.video_index.index_video_scenes", return_value={"scenes": 1, "chunks": 1, "elapsed": 0.1}),
    ):
        run_repair(client, asleep, job_type="video-scenes", console=console)
    assert "1 fixed" in console.export_text()
