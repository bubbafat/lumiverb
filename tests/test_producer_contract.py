"""Producers as plug-ins (ADR-016 phase 4): each declares itself in one
folder (src/producers/<artifact>/), and everything that lists producers
reads them from the registry.

The test of that: a producer added as one folder, and nothing else, shows
in the scheduler's queue, kinds, pools and runners, the reconciler, and
PRODUCERS, which GET /v1/producers lists (Settings → Processing renders it
as it comes). What's still named by hand is listed in contract.py.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from src.producers import load, registry
from src.producers.contract import ProducerSpec, Setting

pytestmark = pytest.mark.fast

REPO = Path(__file__).resolve().parent.parent


def test_every_producer_declares_what_the_rest_needs():
    producers = registry()
    assert list(producers) == ["probe", "proxy", "video_preview", "analysis_proxy", "scenes", "scene_vision",
                               "vision", "ocr", "clip", "faces", "transcript"]
    for p in producers.values():
        assert isinstance(p, ProducerSpec) and p.applies and p.made
        assert all(n in producers for n in p.needs), (p.artifact, p.needs)
        if p.scheduled:
            assert p.flag.startswith("missing_") and p.pool and callable(load(p.run)), p.artifact
        if p.job:
            assert "model" in p.defaults, p.artifact
    flags = [p.flag for p in producers.values() if p.flag]
    assert len(set(flags)) == len(flags)


def test_the_scheduler_waits_for_what_a_producer_is_made_from():
    from src.server.scheduler.kinds import KINDS

    assert KINDS["clip"].extra == "(a.proxy_key IS NOT NULL)"
    assert KINDS["transcript"].extra == "(a.analysis_proxy_key IS NOT NULL)"
    assert KINDS["probe"].extra == "true"


_EXAMPLE = '''
from src.producers.contract import IMAGE, ProducerSpec, Setting

PRODUCER = ProducerSpec(
    artifact="example", producer="example", version="1", media=IMAGE, title="An example", order=999,
    applies="a.media_type = 'image'", made="false", settings=(Setting("strength", 3, "Strength"),), needs=("proxy",),
    kind="example", flag="missing_example", run="example_runner:run", pool="example-pool", slots=2,
)
'''


def test_a_producer_is_one_folder(tmp_path):
    folder = tmp_path / "producers" / "example"
    folder.mkdir(parents=True)
    (folder / "__init__.py").write_text(_EXAMPLE)
    (tmp_path / "example_runner.py").write_text("def run(account, job):\n    return None\n")
    script = textwrap.dedent(f'''
        import json, sys
        sys.path.insert(0, {str(tmp_path)!r})
        import src.producers
        src.producers.__path__.append({str(tmp_path / "producers")!r})

        from src.shared.producers import MISSING_FLAGS, PRODUCERS, effective_settings
        from src.server.repository import lineage
        from src.server.repository.tenant import MISSING_CONDITIONS
        from src.server.scheduler.kinds import KINDS
        from src.server.scheduler.runners import runners
        from src.server.scheduler.service import default_capacity

        print(json.dumps({{
            "listed": list(PRODUCERS)[-1],
            "flag": MISSING_FLAGS.get("missing_example"),
            "settings": effective_settings("example"),
            "applies": lineage.APPLIES["example"],
            "due": "a.media_type = 'image'" in lineage.due("example"),
            "redo": lineage.redoable("example") and bool(lineage.redo_due("example")),
            "joined": "src_example" in lineage.LINEAGE_JOIN,
            "missing": "missing_example" in MISSING_CONDITIONS,
            "kind": [KINDS["example"].spec.tier, KINDS["example"].spec.pool, KINDS["example"].extra],
            "redo_kind": KINDS["redo_example"].spec.tier,
            "runner": runners()["example"].__module__,
            "slots": default_capacity(type("Cfg", (), {{"render_concurrency": 1}})())["example-pool"],
        }}))
    ''')
    out = subprocess.run([sys.executable, "-c", script], cwd=REPO, capture_output=True, text=True,
                         env={"PYTHONPATH": str(REPO), "JWT_SECRET": "x", "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr[-2000:]
    seen = json.loads(out.stdout.strip().splitlines()[-1])
    assert seen == {
        "listed": "example", "flag": "example", "settings": {"strength": 3},
        "applies": "a.media_type = 'image'", "due": True, "redo": True, "joined": True, "missing": True,
        "kind": [3, "example-pool", "(a.proxy_key IS NOT NULL)"], "redo_kind": 4, "runner": "example_runner",
        "slots": 2,
    }


@pytest.mark.parametrize("change, says", [
    (('artifact="example"', 'artifact="clip"'), "Two producers share a artifact"),
    (('kind="example"', 'kind="clip"'), "Two producers share a kind"),
    (('run="example_runner:run", ', ''), "needs a flag, a run and a pool"),
    (('needs=("proxy",)', 'needs=("teleport",)'), "needs what no producer makes"),
    (('pool="example-pool", slots=2', 'pool="gpu", per_account=True, job="vision"'), "pool = job"),
    (("PRODUCER = ProducerSpec(", "NOT_A_PRODUCER = ProducerSpec("), "declares no PRODUCER"),
])
def test_a_folder_that_doesnt_declare_a_whole_producer_is_refused(tmp_path, monkeypatch, change, says):
    # Left out or half-wired quietly, it would never run and nothing would say why.
    import src.producers as producers

    folder = tmp_path / "broken"
    folder.mkdir()
    (folder / "__init__.py").write_text(_EXAMPLE.replace(*change))
    monkeypatch.delitem(sys.modules, "src.producers.broken", raising=False)  # each case's own
    monkeypatch.setattr(producers, "__path__", [*producers.__path__, str(tmp_path)])
    monkeypatch.setattr(producers, "_registry", None)
    with pytest.raises(ValueError, match=re.escape(says)):
        producers.registry()


def test_two_producers_cant_make_one_artifact(tmp_path, monkeypatch):
    import src.producers as producers

    folder = tmp_path / "again"
    folder.mkdir()
    monkeypatch.delitem(sys.modules, "src.producers.again", raising=False)
    (folder / "__init__.py").write_text(_EXAMPLE.replace('artifact="example"', 'artifact="clip"'))
    monkeypatch.setattr(producers, "__path__", [*producers.__path__, str(tmp_path)])
    monkeypatch.setattr(producers, "_registry", None)
    with pytest.raises(ValueError, match="Two producers share a artifact"):
        producers.registry()


# ── A setting checks what an admin gives it ──────────────────────────────

_TEMP = Setting("temperature", 0.2, "Temperature", kind="float", minimum=0, maximum=2)
_WHOLE = Setting("vad", 500, "Silence", minimum=100, maximum=2000, unit="ms")
_TEXT = Setting("prompt", "Describe", "Prompt", kind="text")


@pytest.mark.parametrize("setting, value", [
    (_TEMP, float("nan")), (_TEMP, float("inf")), (_TEMP, float("-inf")), (_TEMP, 10**400),
    (_WHOLE, float("nan")), (_WHOLE, float("inf")), (_WHOLE, 10**400),
])
def test_a_number_that_isnt_finite_is_refused_as_out_of_bounds(setting, value):
    with pytest.raises(ValueError, match="from"):
        setting.check(value)


def test_a_whole_number_given_as_800_point_0_is_800():
    assert _WHOLE.check(800.0) == 800 and isinstance(_WHOLE.check(800.0), int)


@pytest.mark.parametrize("value, says", [("", "empty"), ("   \n", "empty"), ("x" * 4001, "4,000"),
                                         ("a\x00b", "4,000"), (5, "text")])
def test_text_says_whats_wrong_with_it(value, says):
    with pytest.raises(ValueError, match=says):
        _TEXT.check(value)


def test_the_image_size_sent_goes_no_larger_than_the_proxies_it_comes_from():
    from src.client.cli.repair import PROXY_CACHE_EDGE
    from src.shared.producers import PRODUCERS

    for artifact in ("vision", "ocr", "scene_vision"):
        assert PRODUCERS[artifact].setting("max_edge").maximum <= PROXY_CACHE_EDGE, artifact
