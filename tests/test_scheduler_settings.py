"""The scheduler's own settings and the CLI's enrich (ADR-016 phase 4).

The scheduler reads this machine's way of processing from its environment
(/etc/lumiverb/env on the brain), never the CLI's config; the server and
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
from src.server.scheduler.settings import SchedulerSettings, api_url

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
        monkeypatch.setenv("LUMIVERB_API_URL", "http://brain:9000/")
        assert api_url() == "http://brain:9000"
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


def _enrich(monkeypatch, *args: str, answer: dict | None = None):
    from typer.testing import CliRunner

    import importlib

    cli = importlib.import_module("src.client.cli.main")

    client = MagicMock()
    client.get.return_value.json.return_value = [{"library_id": "lib_1", "name": "Footage"}]
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
    answer = {"producers": [{"artifact": "faces", "title": "Faces", "missing": 3, "redo": 120, "retried": 1,
                             "paused": False, "redo_stopped": True}]}
    result, client = _enrich(monkeypatch, "faces", "--library", "Footage", "--redo", answer=answer)
    assert result.exit_code == 0, result.output
    assert client.raw.call_args.args == ("POST", "/v1/producers/run")
    assert client.raw.call_args.kwargs["json"] == {"producer": "faces", "scope": "redo", "library_ids": ["lib_1"]}
    assert "Faces: 3 to make, 120 to make again, 1 tried again (redo stopped)" in result.output
    result, client = _enrich(monkeypatch, "all", "--all")
    assert client.raw.call_args.kwargs["json"] == {"producer": "all", "scope": "new", "all": True}
