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
    assert list(producers) == ["probe", "capture", "proxy", "video_preview", "analysis_proxy", "scenes", "scene_vision",
                               "vision", "ocr", "clip", "faces", "transcript"]
    for p in producers.values():
        assert isinstance(p, ProducerSpec) and p.applies and p.made
        assert all(n in producers for n in p.needs), (p.artifact, p.needs)
        if p.scheduled:
            assert p.flag.startswith("missing_") and p.pool and callable(load(p.run)), p.artifact
        if p.job:
            assert "model" in p.defaults, p.artifact
        # A setting that doesn't remake is outside lineage: something must act when it changes.
        if any(not s.remakes for s in p.settings):
            assert callable(load(p.regroup)), p.artifact
    flags = [p.flag for p in producers.values() if p.flag]
    assert len(set(flags)) == len(flags)


def test_the_scheduler_waits_for_what_a_producer_is_made_from():
    from src.server.scheduler.kinds import KINDS

    assert KINDS["clip"].extra == "(a.proxy_key IS NOT NULL)"
    assert KINDS["transcript"].extra == "(a.analysis_proxy_key IS NOT NULL)"
    assert KINDS["probe"].extra == "true"


_EXAMPLE = """
from src.producers.contract import IMAGE, WHILE_RUNNING, AiJob, Pool, ProducerSpec, Setting

# A new AI job, with machines of its own, and a pool of the producer's own.
GEO = AiJob("geo", "Places", guard="src.producers.example.work:GeoGuard", default_model="geo-1", per_request=3)

PRODUCER = ProducerSpec(
    artifact="example", producer="example", version="1", media=IMAGE, title="An example", order=999,
    applies="a.media_type = 'image'", made="false", needs=("proxy",), redo_also=("vision",),
    settings=(Setting("strength", 3, "Strength"), Setting("model", "", "Model", kind="text", fixed="Settings → AI")),
    kind="example", flag="missing_example", run="src.producers.example.work:Example",
    pool=Pool("example", job=GEO),
)
"""

_EXAMPLE_WORK = """
from src.producers.runner import Work


class GeoGuard:
    def __init__(self, client):
        self.model, self.down = "geo-1", False
        self.pool = type("P", (), {"set_gpu_hold": lambda self, n: None})()

    def charges(self, error):
        return True


class Example(Work):
    artifact = "example"

    def make(self, clip):
        return {"place": "Lisbon"}

    def save(self, client, made):
        for clip, result in made:
            client.post(f"/v1/example/{clip['asset_id']}", json={**result, "lineage": self.lineage(clip)})
"""

# A producer with a shared GPU-holding pool, sized by a slot count, no AI job.
_OTHER = """
from src.producers.contract import VIDEO, WHILE_RUNNING, Pool, ProducerSpec

PRODUCER = ProducerSpec(
    artifact="other", producer="other", version="1", media=VIDEO, title="Another", order=998,
    applies="a.media_type = 'video'", made="false",
    kind="other", flag="missing_other", run="src.producers.other.work:Other",
    pool=Pool("other-pool", slots=2, gpu_hold=WHILE_RUNNING),
)
"""


def _folder(root, name: str, init: str, work: str = "") -> None:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "__init__.py").write_text(init)
    if work:
        (folder / "work.py").write_text(work)


def test_a_producer_is_one_folder(tmp_path):
    """ADR-016's test: a producer ships as one folder with no pipeline edits.
    Two dropped in (one bringing an AI job of its own) show everywhere the
    producers are read from: lineage and the reconciler, the scheduler's
    kinds, pools (their slots, their GPU sharing, an AI job's per_request),
    runners and AI jobs, where an account keeps the job's model, the asset
    page's missing_* filter and the repair summary, what goes when it's made
    again, and GET /v1/producers, and its work runs through the runner."""
    _folder(tmp_path / "producers", "example", _EXAMPLE, _EXAMPLE_WORK)
    _folder(tmp_path / "producers", "other", _OTHER, "from src.producers.runner import Work\n\n\nclass Other(Work):\n    artifact = 'other'\n")
    script = textwrap.dedent(f"""
        import json, sys, threading
        from unittest.mock import MagicMock
        import src.producers
        src.producers.__path__.append({str(tmp_path / "producers")!r})

        from src.processing.machine import Machine
        from src.shared.ai_jobs import AI_JOBS, BUILT_IN_JOBS, JOBS
        from src.shared.producers import MISSING_FLAGS, PRODUCERS, effective_settings, pause_targets
        from src.server.api.routers.assets import RepairSummary, missing_filters
        from src.server.models.control_plane import Tenant
        from src.server.repository import ai_machines, lineage
        from src.server.repository.tenant import MISSING_CONDITIONS
        from src.server.scheduler.kinds import AI_JOB_KINDS, KINDS
        from src.server.scheduler.runners import runners
        from src.server.scheduler.service import Scheduler, default_capacity, gpu_holds
        from src.server.scheduler.dispatch import Job
        import inspect

        tenant = Tenant(tenant_id="t", name="t", ai_job_models={{}})
        ai_machines.set_job_model(tenant, "vision", "qwen")

        acct = MagicMock()
        acct.stopping = threading.Event()
        acct.producers.lineage.return_value = {{"producer": "example"}}
        job = Job("t", "example", 3, ({{"asset_id": "a1", "library_id": "l", "rel_path": "a.jpg", "sha256": "s"}},))
        outcome = runners()["example"](acct, job)

        from src.server.scheduler.account import Account
        account = Account("t", MagicMock(), MagicMock())
        s = Scheduler(lambda: {{}}, capacity={{}}, candidates=lambda *a, **k: [], runners={{}}, scan=MagicMock(),
                      inline_refill=True, on_hold=lambda t: lineage.Pauses(), retry_requested=lambda t: None,
                      write_status=lambda t, st: None)
        fake = MagicMock(settings_ready=False, capacity=lambda job: 2)
        fake.library_ids.return_value = []
        s._refill("t", fake)

        print(json.dumps({{
            "listed": list(PRODUCERS)[-2:],
            "flag": MISSING_FLAGS.get("missing_example"),
            "settings": effective_settings("example", account={{"geo": "geo-2"}}),
            "applies": lineage.APPLIES["example"],
            "due": "a.media_type = 'image'" in lineage.due("example"),
            "redo": lineage.redoable("example") and bool(lineage.redo_due("example")),
            "made_from": list(lineage.made_from("example")),
            "joined": "src_example" in lineage.LINEAGE_JOIN,
            "missing": "missing_example" in MISSING_CONDITIONS,
            "page_filter": "missing_example" in inspect.signature(missing_filters).parameters,
            "summary": "missing_example" in RepairSummary.model_fields,
            "kind": [KINDS["example"].spec.tier, KINDS["example"].spec.pool, KINDS["example"].spec.per_account,
                     KINDS["example"].extra],
            "redo_kind": KINDS["redo_example"].spec.tier,
            "runner": outcome,
            "saved": acct.client.post.call_args.args[0],
            "job": [JOBS.get("geo"), AI_JOB_KINDS.get("geo"), "geo" in BUILT_IN_JOBS],
            "model_kept": [ai_machines.job_model(tenant, "geo"), ai_machines.job_models(tenant)["geo"],
                           ai_machines.job_model(tenant, "vision")],
            "guard": type(account.guard("geo")).__name__,
            "slots": s.dispatcher._capacity.get("example@t"),
            "pools": [default_capacity(Machine()).get("other-pool"), gpu_holds().get("other-pool")],
            "switches": "example" in pause_targets() and "other" in pause_targets(),
        }}))
    """)
    out = subprocess.run([sys.executable, "-c", script], cwd=REPO, capture_output=True, text=True,
                         env={"PYTHONPATH": str(REPO), "JWT_SECRET": "x", "PATH": "/usr/bin:/bin",
                              "CONTROL_PLANE_DATABASE_URL": "postgresql://x/x",
                              "TENANT_DATABASE_URL_TEMPLATE": "postgresql://x/{tenant_id}"})
    assert out.returncode == 0, out.stderr[-3000:]
    seen = json.loads(out.stdout.strip().splitlines()[-1])
    assert seen == {
        "listed": ["other", "example"], "flag": "example", "settings": {"strength": 3, "model": "geo-2"},
        "applies": "a.media_type = 'image'", "due": True, "redo": True, "made_from": ["example", "vision"],
        "joined": True, "missing": True, "page_filter": True, "summary": True,
        "kind": [3, "example", True, "(a.proxy_key IS NOT NULL)"], "redo_kind": 4,
        "runner": None, "saved": "/v1/example/a1",
        "job": ["Places", ["example", "redo_example"], False],
        "model_kept": ["geo-1", "geo-1", "qwen"], "guard": "GeoGuard",
        "slots": 6,  # its machines' 2 requests at once, times the job's per_request
        "pools": [2, "while_running"], "switches": True,
    }


@pytest.mark.parametrize("change, says", [
    (('artifact="example"', 'artifact="clip"'), "Two producers share a artifact"),
    (('kind="example"', 'kind="clip"'), "Two producers share a kind"),
    (('run="src.producers.example.work:Example",', ''), "needs a flag, a run and a pool"),
    (('needs=("proxy",)', 'needs=("teleport",)'), "names what no producer makes"),
    (('pool=Pool("example", job=GEO),', 'pool=Pool("example", job=GEO), unit="minute",'), "unit is a second"),
    (('flag="missing_example"', 'flag="example_missing"'), "its flag is a missing_* filter"),
    # A pool or AI job shared by name is declared alike, or its slots would depend on who's read first.
    (('pool=Pool("example", job=GEO),', 'pool=Pool("gpu", slots=4),'), "the pool 'gpu' is declared another way"),
    (('AiJob("geo", "Places"', 'AiJob("vision", "Places"'), "the AI job 'vision' is declared another way"),
    (('pool=Pool("example", job=GEO),', 'pool=Pool("example", sized_by="nope"),'), "no setting of this machine's"),
    (('Setting("model", "", "Model", kind="text", fixed="Settings → AI")', ''), "takes its model"),
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
    from src.producers.clip import PROXY_CACHE_EDGE
    from src.shared.producers import PRODUCERS

    for artifact in ("vision", "ocr", "scene_vision"):
        assert PRODUCERS[artifact].setting("max_edge").maximum <= PROXY_CACHE_EDGE, artifact



def test_video_producers_are_timed_by_the_second_and_the_rest_by_the_clip():
    from src.server.scheduler.kinds import KINDS
    from src.shared.producers import PRODUCERS

    by_second = {a for a, p in PRODUCERS.items() if p.unit == "second"}
    assert by_second == {"analysis_proxy", "transcript", "scenes", "scene_vision"}
    assert KINDS["render"].spec.by_seconds and KINDS["redo_transcript"].spec.by_seconds
    assert not KINDS["vision"].spec.by_seconds
