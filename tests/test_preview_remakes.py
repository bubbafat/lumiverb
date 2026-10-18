# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Scans remake previews made another way than now (ADR-018 phase 7, decision 6).

A clip's preview and thumbnail and a video's preview are made by a scan, so
a scan remakes them too: an unchanged file whose previews were made with
other settings is uploaded again (the plan's reason remake), after the new
and changed files, a few at each scan (REMAKES_PER_SCAN), while their
remakes run. New files only (their redo stopped) keeps them as they are
until someone remakes them; a dry run remakes nothing.

Real scans (run_scan) of real files against the API in-process.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from src.processing import scan as scan_module
from src.shared import producers as shared
from tests.scan_plan import scan_would
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_scan_outcomes import Lib, _cache_here, _image, _scan, _video, api, lib  # noqa: F401

pytestmark = pytest.mark.slow


def _hash(artifact: str, notch: str) -> str:
    return shared.settings_hash(shared.effective_settings(artifact, {"quality": notch}))


def _made(env, lib: Lib) -> dict[str, dict[str, str]]:
    """Each clip's previews as made: {rel_path: {artifact: settings_hash}}."""
    engine = create_engine(env[5])
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT a.rel_path, l.artifact, l.settings_hash FROM assets a"
                " JOIN artifact_lineage l ON l.asset_id = a.asset_id"
                " WHERE a.library_id = :l AND a.deleted_at IS NULL AND l.artifact IN ('proxy', 'video_preview')"),
                {"l": lib.id}).all()
    finally:
        engine.dispose()
    out: dict[str, dict[str, str]] = {}
    for rel_path, artifact, settings_hash in rows:
        out.setdefault(rel_path, {})[artifact] = settings_hash
    return out


def _quality(env, artifact: str, notch: str | None, redo: bool) -> None:
    client, headers, *_ = env
    r = client.put(f"/v1/producers/{artifact}/settings", json={"settings": {"quality": notch}, "redo": redo},
                   headers=headers)
    assert r.status_code == 200, r.text


def _back_to_defaults(env) -> None:
    client, headers, *_ = env
    for artifact in ("proxy", "video_preview"):
        _quality(env, artifact, None, True)
        client.post(f"/v1/producers/{artifact}/resume", json={"scope": "redo"}, headers=headers)


@pytest.fixture
def uploaded(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Each file a scan uploads, in the order it does (one at a time: _scan's)."""
    names: list[str] = []
    for name in ("_scan_one", "_scan_one_video"):
        real = getattr(scan_module, name)

        def spy(*args, _real=real, **kwargs):
            names.append(kwargs["f"]["rel_path"])
            return _real(*args, **kwargs)

        monkeypatch.setattr(scan_module, name, spy)
    return names


def test_a_scan_remakes_previews_made_another_way_after_the_new_files(env, api, lib, uploaded):
    _image(lib, "a.png", 1)
    _image(lib, "b.png", 2)
    _video(lib, "c.mp4")
    _scan(api, lib)
    better = {"proxy": _hash("proxy", "better")}
    assert _made(env, lib) == {"a.png": better, "b.png": better,
                               "c.mp4": {**better, "video_preview": _hash("video_preview", "better")}}
    try:
        _quality(env, "proxy", "best", True)  # also remake them
        _quality(env, "video_preview", "best", True)
        _image(lib, "d.png", 4)
        uploaded.clear()
        _scan(api, lib)
        assert uploaded[0] == "d.png" and sorted(uploaded[1:]) == ["a.png", "b.png", "c.mp4"]
        best = {"proxy": _hash("proxy", "best")}
        assert _made(env, lib) == {"a.png": best, "b.png": best, "d.png": best,
                                   "c.mp4": {**best, "video_preview": _hash("video_preview", "best")}}
        uploaded.clear()
        _scan(api, lib)
        assert uploaded == [], "all made as now: nothing to remake"
        # A reader holding its remakes back (a device on battery) asks for none.
        _quality(env, "proxy", "good", True)
        _scan(api, lib, remakes=False)
        assert uploaded == []
        _scan(api, lib)
        assert sorted(uploaded) == ["a.png", "b.png", "c.mp4", "d.png"]
    finally:
        _back_to_defaults(env)


def test_new_files_only_keeps_them_and_a_remake_goes_a_few_at_a_scan(env, api, lib, uploaded, monkeypatch):
    client, headers, *_ = env
    for n in range(3):
        _image(lib, f"{n}.png", n)
    _scan(api, lib)
    try:
        _quality(env, "proxy", "good", False)  # new files only
        uploaded.clear()
        _scan(api, lib)
        assert uploaded == [] and {m["proxy"] for m in _made(env, lib).values()} == {_hash("proxy", "better")}
        # Remake: the standing offer.
        r = client.post("/v1/producers/proxy/resume", json={"scope": "redo"}, headers=headers)
        assert r.status_code == 204, r.text
        monkeypatch.setattr("src.server.scans.REMAKES_PER_SCAN", 2)
        done = []
        for _ in range(3):
            uploaded.clear()
            _scan(api, lib)
            done.append(len(uploaded))
        assert done == [2, 1, 0]
        assert {m["proxy"] for m in _made(env, lib).values()} == {_hash("proxy", "good")}
    finally:
        _back_to_defaults(env)


def test_a_dry_run_remakes_nothing(env, api, lib):
    client, headers, *_ = env
    path = _image(lib, "a.png", 1)
    _scan(api, lib)
    found = [scan_module._found({"rel_path": "a.png", "file_size": path.stat().st_size, "media_type": "image",
                                 "file_mtime": scan_module.datetime.fromtimestamp(path.stat().st_mtime,
                                                                                  scan_module.UTC),
                                 "settling": False})]
    try:
        _quality(env, "proxy", "best", True)
        assert scan_would(client, headers, lib.id, found) == {"a.png": "unchanged"}
        assert scan_would(client, headers, lib.id, found, dry_run=False) == {"a.png": "upload"}
    finally:
        _back_to_defaults(env)
