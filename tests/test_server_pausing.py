# ruff: noqa: F811 — pytest fixtures are named as parameters
"""`lumiverb-server pause|resume` (src/server/lifecycle/cli.py): the pause
switches from the machine itself, without an API key, doing what Settings ›
Processing and `lumiverb pause|resume` do (the same routes' code). What
they leave is read back through the API, as the web app reads it."""

from __future__ import annotations

import os

import pytest

from src.server.lifecycle import cli
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture

pytestmark = pytest.mark.slow


@pytest.fixture
def env_file(env, tmp_path):
    """The install's env file, as the server command reads it."""
    path = tmp_path / "env"
    path.write_text(
        f"CONTROL_PLANE_DATABASE_URL={os.environ['CONTROL_PLANE_DATABASE_URL']}\n"
        f"TENANT_DATABASE_URL_TEMPLATE={os.environ['TENANT_DATABASE_URL_TEMPLATE']}\n")
    yield path
    _server(env, path, "resume", "all")  # every test starts running


def _server(env, env_file, *args: str) -> int:
    return cli.main([*args, "--env", str(env_file)])


def _queue(env) -> dict:
    client, headers, *_ = env
    r = client.get("/v1/producers/queue", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _paused(env) -> set[str]:
    queue = _queue(env)
    return ({s["target"] for s in queue["switches"] if s["paused"]}
            | {f"scans of {lib['name']}" for lib in queue["libraries"] if lib["paused"]})


def test_pause_all_and_resume_all(env, env_file, capsys):
    assert _server(env, env_file, "pause", "all") == 0
    assert capsys.readouterr().out.strip() == "✓ AnalysisTenant: processing paused"
    assert _queue(env)["state"] == "paused"

    assert _server(env, env_file, "resume", "all") == 0
    assert capsys.readouterr().out.strip() == "✓ AnalysisTenant: processing running"
    assert _queue(env)["state"] == "running" and _paused(env) == set()


def test_one_switch_and_one_librarys_scans(env, env_file, capsys):
    # A producer by the name of what it makes, with or without lumiverb. (ADR-024 phase 1 part 2).
    assert _server(env, env_file, "pause", "image.description") == 0
    assert _paused(env) == {"lumiverb.image.description"}
    assert _server(env, env_file, "pause", "scans", "--library", "Footage") == 0
    assert _paused(env) == {"lumiverb.image.description", "scans of Footage"}
    assert capsys.readouterr().out.strip().endswith("processing partly paused")

    assert _server(env, env_file, "resume", "scans", "--library", "Footage", "--account", "AnalysisTenant") == 0
    assert _paused(env) == {"lumiverb.image.description"}
    assert _server(env, env_file, "resume", "lumiverb.image.description") == 0
    assert _paused(env) == set()


def test_an_old_word_is_refused_saying_the_name_to_type(env, env_file, capsys):
    # Since ADR-024's part 5: nothing changes.
    assert _server(env, env_file, "pause", "vision") == 2
    assert "✗ vision is called image.description now: type that" in capsys.readouterr().err
    assert _paused(env) == set()


def test_resuming_one_while_all_is_paused_leaves_the_rest_paused(env, env_file):
    # As the API does: Pause all becomes each switch and library paused on its own.
    assert _server(env, env_file, "pause", "all") == 0
    assert _server(env, env_file, "resume", "scans") == 0
    queue = _queue(env)
    assert queue["state"] == "partly"
    assert all(s["paused"] for s in queue["switches"]) and not any(lib["paused"] for lib in queue["libraries"])


@pytest.mark.parametrize(("args", "code", "said"), [
    (["pause", "nothing-like-this"], 2, "Nothing here is called nothing-like-this: all, scans, upkeep, video.format,"),
    (["pause", "image.description", "--library", "Footage"], 2, "--library goes with scans"),
    (["pause", "scans", "--library", "No such library"], 1, "No library here is No such library"),
    (["pause", "all", "--account", "Nobody"], 1, "No account here is Nobody"),
])
def test_what_isnt_there_is_said_and_nothing_changes(env, env_file, capsys, args, code, said):
    assert _server(env, env_file, *args) == code
    assert said in capsys.readouterr().err
    assert _paused(env) == set()
