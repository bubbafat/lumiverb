"""Producers as plug-ins (ADR-016 phase 4): each declares itself in one
folder (src/producers/<artifact>/), and everything that lists producers
reads them from the registry.

The test of that: a producer added as one folder, and nothing else, shows
in the queue, the reconciler, the runners and the list GET /v1/producers
serves (which Settings → Processing renders as it comes).
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from src.producers import load, registry
from src.producers.contract import ProducerSpec

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
from src.producers.contract import IMAGE, ProducerSpec

PRODUCER = ProducerSpec(
    artifact="example", producer="example", version="1", media=IMAGE, title="An example", order=999,
    applies="a.media_type = 'image'", made="false", defaults={"strength": 3}, needs=("proxy",),
    kind="example", flag="missing_example", run="example_runner:run", pool="gpu",
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
        from src.server.scheduler.runners import RUNNERS

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
            "runner": RUNNERS["example"].__module__,
        }}))
    ''')
    out = subprocess.run([sys.executable, "-c", script], cwd=REPO, capture_output=True, text=True,
                         env={"PYTHONPATH": str(REPO), "JWT_SECRET": "x", "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr[-2000:]
    seen = json.loads(out.stdout.strip().splitlines()[-1])
    assert seen == {
        "listed": "example", "flag": "example", "settings": {"strength": 3},
        "applies": "a.media_type = 'image'", "due": True, "redo": True, "joined": True, "missing": True,
        "kind": [3, "gpu", "(a.proxy_key IS NOT NULL)"], "redo_kind": 4, "runner": "example_runner",
    }


def test_two_producers_cant_make_one_artifact(tmp_path, monkeypatch):
    import src.producers as producers

    folder = tmp_path / "again"
    folder.mkdir()
    (folder / "__init__.py").write_text(_EXAMPLE.replace('artifact="example"', 'artifact="clip"'))
    monkeypatch.setattr(producers, "__path__", [*producers.__path__, str(tmp_path)])
    monkeypatch.setattr(producers, "_registry", None)
    with pytest.raises(ValueError, match="Two producers make one artifact"):
        producers.registry()
