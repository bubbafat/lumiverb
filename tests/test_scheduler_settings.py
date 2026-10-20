"""The scheduler's own settings and the CLI's enrich (ADR-016 phase 4).

The scheduler reads this machine's way of processing from its environment
(/etc/lumiverb/env on Linux), never the CLI's config; the server and
processing import nothing of the CLI's. `lumiverb enrich` only asks the
scheduler, naming the producer and the libraries.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.processing.machine import Machine, default_render_concurrency
from src.server.scheduler.settings import SchedulerSettings, SettingError, api_url, read_settings

pytestmark = pytest.mark.fast

REPO = Path(__file__).resolve().parent.parent


def test_the_schedulers_settings_come_from_its_environment(monkeypatch) -> None:
    monkeypatch.setenv("LUMIVERB_ROOT_MAP", json.dumps({"/Volumes/media-01": "/mnt/media-01"}))
    monkeypatch.setenv("LUMIVERB_ANALYSIS_PROXY_DECODER", "cuda")
    monkeypatch.setenv("LUMIVERB_RENDER_CONCURRENCY", "2")
    monkeypatch.setenv("LUMIVERB_FACE_BATCHES_PER_PROCESS", "8")
    here = SchedulerSettings().machine()
    assert here.root_map == {"/Volumes/media-01": "/mnt/media-01"}
    assert (here.analysis_proxy_decoder, here.renders, here.face_batches_per_process) == ("cuda", 2, 8)
    assert here.gpu_decodes_here == 1
    assert Machine(analysis_proxy_decoder="cpu").gpu_decodes_here == 0


def test_each_library_is_scanned_in_full_every_15_minutes_unless_set(monkeypatch, tmp_path) -> None:
    # Robert, Oct 13: nothing reports changes on a Mac running the server, so
    # its full scans are how new files are found; once a day was too seldom.
    monkeypatch.chdir(tmp_path)  # no .env or .env.local of this checkout's
    monkeypatch.delenv("LUMIVERB_FULL_SCAN_EVERY_MIN", raising=False)
    assert read_settings().machine().full_scan_every_min == 15
    assert Machine().full_scan_every_min == 15  # whatever runs processing without saying
    monkeypatch.setenv("LUMIVERB_FULL_SCAN_EVERY_MIN", "120")  # a big library on a network share
    assert read_settings().machine().full_scan_every_min == 120
    monkeypatch.setenv("LUMIVERB_FULL_SCAN_EVERY_MIN", "0.5")
    assert read_settings().machine().full_scan_every_min == 0.5


def test_the_schedulers_caches_have_caps_and_the_clis_previews_none_yet(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)  # no .env or .env.local of this checkout's
    for name in ("LUMIVERB_PREVIEW_CACHE_GB", "LUMIVERB_ANALYSIS_CACHE_GB"):
        monkeypatch.delenv(name, raising=False)
    here = read_settings().machine()
    assert (here.preview_cache_gb, here.analysis_cache_gb) == (2.0, 50.0)
    assert Machine().preview_cache_gb == 0, "the CLI's, which the Mac app browses (ADR-020 phase 5)"
    monkeypatch.setenv("LUMIVERB_PREVIEW_CACHE_GB", "0.5")
    assert read_settings().machine().preview_cache_gb == 0.5
    monkeypatch.setenv("LUMIVERB_PREVIEW_CACHE_GB", "0")
    with pytest.raises(SettingError, match="LUMIVERB_PREVIEW_CACHE_GB"):
        read_settings()


@pytest.mark.parametrize("bad", ["0", "-15", "soon", "", "inf", "nan"])
def test_a_full_scan_interval_that_isnt_a_positive_number_stops_the_scheduler_saying_why(
        monkeypatch, tmp_path, caplog, bad) -> None:
    # Before, any setting it couldn't read stopped it with nothing said:
    # entry()'s exit came before the traceback.
    from src.server import database
    from src.server.scheduler import service

    class GotPastTheSettings:
        def __init__(self, *a, **kw) -> None:
            raise AssertionError("the scheduler went on with a setting it can't use")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LUMIVERB_FULL_SCAN_EVERY_MIN", bad)
    monkeypatch.setattr(service, "configure_logging", lambda: None)  # the test run's logging stays as it is
    monkeypatch.setattr(database, "APPLICATION_NAME", database.APPLICATION_NAME)  # put back after
    monkeypatch.setattr("src.server.scheduler.scans.ServiceLock", GotPastTheSettings)
    with caplog.at_level("ERROR", logger="src.server.scheduler.service"):
        assert service.main() == 1
    [said] = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert said.startswith("scheduler: not starting, a setting is wrong: LUMIVERB_FULL_SCAN_EVERY_MIN: ")
    assert f"(it says {bad!r})" in said


def test_a_setting_that_isnt_json_is_named(monkeypatch, tmp_path) -> None:
    from src.server.scheduler.settings import SettingError

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LUMIVERB_ROOT_MAP", "{not json")
    with pytest.raises(SettingError, match='"root_map".*isn\'t JSON'):
        read_settings()


@pytest.mark.parametrize("decoder,vulkan,videotoolbox,decodes", [
    ("auto", True, False, 1),  # a Linux GPU: renders take requests from the AI machines sharing it
    ("auto", False, True, 0),  # a Mac: the media engine decodes, the GPU is the models' (Ollama's)
    ("auto", False, False, 0),  # the CPU decodes
    ("videotoolbox", False, False, 0),
    ("vulkan", False, False, 1),
    ("cpu", True, True, 0),
])
def test_renders_hold_the_gpu_only_where_they_decode_on_it(monkeypatch, decoder, vulkan, videotoolbox,
                                                           decodes) -> None:
    from src.processing.video import ffmpeg_run

    monkeypatch.setattr(ffmpeg_run, "vulkan_decodes", lambda: vulkan)
    monkeypatch.setattr(ffmpeg_run, "videotoolbox_decodes", lambda: videotoolbox)
    assert Machine(analysis_proxy_decoder=decoder).gpu_decodes_here == decodes
    assert Machine(analysis_proxy_decoder=decoder, gpu_decodes=0).gpu_decodes_here == 0


def test_render_concurrency_follows_the_cores(monkeypatch) -> None:
    for cores, renders in ((4, 1), (12, 2), (32, 3)):
        monkeypatch.setattr("os.cpu_count", lambda c=cores: c)
        assert default_render_concurrency() == renders
    assert Machine(render_concurrency=5).renders == 5


def test_the_api_is_found_where_it_listens(monkeypatch) -> None:
    from src.server.config import get_settings

    monkeypatch.delenv("LUMIVERB_API_URL", raising=False)
    monkeypatch.setenv("API_PORT", "8100")
    monkeypatch.setenv("API_LISTEN_HOST", "0.0.0.0")
    get_settings.cache_clear()
    try:
        assert api_url() == "http://127.0.0.1:8100"
        monkeypatch.setenv("LUMIVERB_API_URL", "http://server:9000/")
        assert api_url() == "http://server:9000"
    finally:
        get_settings.cache_clear()


def test_nothing_server_side_or_in_processing_imports_the_cli() -> None:
    """Finding 1: the scheduler was built from the CLI's private functions and config."""
    found = []
    for root in ("src/server", "src/processing", "src/producers", "src/shared"):
        for path in (REPO / root).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                found += [f"{path.relative_to(REPO)}: {n}" for n in names if n.startswith("src.client")]
    assert found == []


def test_the_env_helper_carries_the_clis_settings_over_once_and_maps_roots(tmp_path) -> None:
    env, cfg = tmp_path / "env", tmp_path / "config.json"
    env.write_text("API_PORT=8100\nLUMIVERB_RENDER_CONCURRENCY=1\n")
    cfg.write_text(json.dumps({"api_key": "k", "root_map": {"/Volumes/media-01/": "/mnt/media-01"},
                               "render_concurrency": 3, "gpu_decodes": 1, "analysis_proxy_decoder": "cuda"}))
    script = [sys.executable, str(REPO / "scripts" / "scheduler-env.py"), str(env)]
    subprocess.run([*script, "--from-cli-config", str(cfg)], check=True, capture_output=True)
    subprocess.run([*script, "--from-cli-config", str(cfg), "--root-map", "/Volumes/media-02=/mnt/media-02/"],
                   check=True, capture_output=True)
    lines = env.read_text().splitlines()
    assert "LUMIVERB_RENDER_CONCURRENCY=1" in lines  # what the env file says stays
    assert "LUMIVERB_ANALYSIS_PROXY_DECODER=cuda" in lines and not any("GPU_DECODES" in x for x in lines)
    [root_map] = [x for x in lines if x.startswith("LUMIVERB_ROOT_MAP=")]
    assert json.loads(root_map.split("=", 1)[1].strip("'")) == {"/Volumes/media-01": "/mnt/media-01",
                                                                "/Volumes/media-02": "/mnt/media-02"}


# ---------------------------------------------------------------------------
# lumiverb enrich asks the scheduler
# ---------------------------------------------------------------------------


def _enrich(monkeypatch, *args: str, answer: dict | None = None, listed: tuple[str, ...] = ("faces", "vision")):
    """listed: the producers the server lists, as it spells them (ids before ADR-024 part 3, names after)."""
    from typer.testing import CliRunner

    import importlib

    cli = importlib.import_module("src.client.cli.main")

    def get(path, params=None):
        r = MagicMock()
        r.json.return_value = ({"producers": [{"artifact": a} for a in listed]} if path == "/v1/producers"
                               else [{"library_id": "lib_1", "name": "Footage"}])
        return r

    client = MagicMock()
    client.get.side_effect = get
    client.raw.return_value = MagicMock(status_code=200, json=lambda: answer or {"producers": []})
    monkeypatch.setattr("src.client.cli.commands.enrich.LumiverbClient", lambda: client)
    monkeypatch.setattr("src.client.cli.commands.archive.LumiverbClient", lambda: client, raising=False)
    return CliRunner().invoke(cli.app, ["enrich", *args]), client


def test_enrich_names_its_producer_and_libraries(monkeypatch) -> None:
    for args in ((), ("faces",), ("faces", "--library", "Footage", "--all"), ("--all",)):
        result, client = _enrich(monkeypatch, *args)
        assert result.exit_code == 2 and "Usage" in result.output, args
        client.raw.assert_not_called()


def test_enrich_asks_the_scheduler_and_says_what_it_has_to_do(monkeypatch) -> None:
    answer = {"producers": [{"artifact": "lumiverb.image.faces", "title": "Faces", "missing": 3, "redo": 120, "retried": 1,
                             "paused": False, "redo_stopped": True}]}
    result, client = _enrich(monkeypatch, "faces", "--library", "Footage", "--redo", answer=answer)
    assert result.exit_code == 0, result.output
    assert client.raw.call_args.args == ("POST", "/v1/producers/run")
    assert client.raw.call_args.kwargs["json"] == {"producer": "faces", "scope": "redo", "library_ids": ["lib_1"]}
    assert "Faces: 3 to make, 120 to make again, 1 tried again (redo stopped)" in result.output
    result, client = _enrich(monkeypatch, "all", "--all")
    assert client.raw.call_args.kwargs["json"] == {"producer": "all", "scope": "new", "all": True}


@pytest.mark.parametrize("listed, sent", [(("faces", "vision"), "faces"),
                                          (("lumiverb.image.faces", "lumiverb.image.description"), "lumiverb.image.faces")])
def test_enrich_takes_a_name_and_sends_it_as_the_server_lists_it(monkeypatch, listed, sent) -> None:
    # ADR-024 phase 1 part 2: a server before part 3 lists today's ids, one from part 3 the names.
    for typed in ("image.faces", "lumiverb.image.faces", "faces"):
        result, client = _enrich(monkeypatch, typed, "--all", listed=listed)
        assert result.exit_code == 0, result.output
        assert client.raw.call_args.kwargs["json"]["producer"] == sent, typed
        assert ("faces is called image.faces now" in result.output) == (typed == "faces"), typed
    result, client = _enrich(monkeypatch, "teleport", "--all", listed=listed)
    assert result.exit_code == 2 and "No producer is called teleport: one of all, image.faces, image.description" in \
        result.output.replace("\n", " ")
    client.raw.assert_not_called()


def test_an_error_that_stops_the_scheduler_is_said_before_it_goes(tmp_path) -> None:
    # entry() ends the process at once (os._exit), before Python would print
    # the traceback: a scheduler stopping at start said nothing, every time.
    code = ("from src.server.scheduler import service\n"
            "def main():\n"
            "    raise RuntimeError('a required setting is missing')\n"
            "service.main = main\n"
            "service.entry()\n")
    done = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert done.returncode == 1
    assert "scheduler: stopped by an error" in done.stderr
    assert "RuntimeError: a required setting is missing" in done.stderr
