# ruff: noqa: F811 — pytest fixtures are named as parameters
"""What a library's reader has to do (ADR-018 decision 3, phase 3): one
list, computed from how each clip's artifacts were made
(src/server/reader_work.py), GET /v1/libraries/{id}/reader-work.

Every reason is in it: a preview a scan makes that's missing, and what each
producer that reads the originals makes (a video's facts, capture facts,
analysis proxies) that's due; remakes apart. What's made leaves it by
itself, what's paused isn't listed, and for each producer it's what this
server's scheduler is handed (its queue): the rules are in one place.
"""

from __future__ import annotations

import io
import json

import pytest
from sqlalchemy import text

from src.shared.producers import PRODUCERS
from tests.producer_fakes import due
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _sha, _want
from tests.videos import playable

ALL_WORK = ["previews", "probe", "capture", "analysis_proxy"]


def _library(env, name: str) -> tuple:
    client, headers, *_ = env
    r = client.post("/v1/libraries", json={"name": name, "root_path": f"/tmp/{name}"}, headers=headers)
    assert r.status_code == 200, r.text
    return (client, headers, r.json()["library_id"], *env[3:])


def _work(env, **params) -> dict[str, tuple[list[str], list[str]]]:
    """The whole list, a page at a time: {asset_id: (work, remakes)}."""
    client, headers, library_id, *_ = env
    out: dict[str, tuple[list[str], list[str]]] = {}
    after = None
    while True:
        r = client.get(f"/v1/libraries/{library_id}/reader-work", params={**params, **({"after": after} if after else {})},
                       headers=headers)
        assert r.status_code == 200, r.text
        page = r.json()
        for c in page["clips"]:
            assert c["asset_id"] not in out, "a clip on two pages"
            out[c["asset_id"]] = (c["work"], c["remakes"])
        after = page["next_cursor"]
        if after is None:
            return out


def _photo(env, rel_path: str, *, capture: bool = True) -> tuple[str, str]:
    """A photo as a scan sends it: its preview, and its capture facts read with the EXIF."""
    sha = _sha()
    made = {"proxy": _want(env, "proxy", sha), **({"capture": _want(env, "capture", sha)} if capture else {})}
    return _ingest_with(env, rel_path, sha, made), sha


def _video(env, rel_path: str, *, capture: bool = True) -> tuple[str, str]:
    """A video whose scan sent its poster and capture facts, but not yet its preview, probe or analysis proxy."""
    sha = _sha()
    made = {"proxy": _want(env, "proxy", sha), **({"capture": _want(env, "capture", sha)} if capture else {})}
    return _ingest_with(env, rel_path, sha, made, media_type="video"), sha


def _preview_sent(env, clip: str, sha: str) -> None:
    client, headers, *_ = env
    r = client.post(f"/v1/assets/{clip}/artifacts/video_preview", headers=headers,
                    files={"file": ("p.mp4", io.BytesIO(playable()), "video/mp4")},
                    data={"lineage": json.dumps(_want(env, "video_preview", sha))})
    assert r.status_code == 200, r.text


def _probed(env, clip: str, sha: str) -> None:
    client, headers, *_ = env
    r = client.put(f"/v1/assets/{clip}/video-facet", json={"duration_sec": 30.0, "lineage": _want(env, "probe", sha)},
                   headers=headers)
    assert r.status_code == 200, r.text


def _analysis_proxy_made(env, clip: str, made: dict) -> None:
    """Its analysis proxy kept, with how it was made (the file isn't read here)."""
    from src.server.repository import lineage

    with _db(env) as s:
        s.execute(text("UPDATE assets SET analysis_proxy_key = :k WHERE asset_id = :a"),
                  {"k": f"analysis/{clip}.mp4", "a": clip})
        lineage.record(s, clip, "analysis_proxy", made, commit=False)
        s.commit()


def _all_made(env, rel_path: str) -> tuple[str, str]:
    clip, sha = _video(env, rel_path)
    _preview_sent(env, clip, sha)
    _probed(env, clip, sha)
    _analysis_proxy_made(env, clip, _want(env, "analysis_proxy", sha))
    return clip, sha


def _post(env, path: str, body: dict) -> None:
    client, headers, *_ = env
    r = client.post(path, json=body, headers=headers)
    assert r.status_code == 204, r.text


def _set(env, artifact: str, **settings) -> None:
    client, headers, *_ = env
    r = client.put(f"/v1/producers/{artifact}/settings", json={"settings": settings, "redo": True}, headers=headers)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# What's listed, and what leaves it
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_new_clip_lists_what_its_original_is_still_needed_for(env):
    lib = _library(env, "ReaderNew")
    photo, _ = _photo(lib, "a.jpg")
    unread, _ = _photo(lib, "b.jpg", capture=False)
    video, _ = _video(lib, "c.mov")
    older, _ = _video(lib, "d.mov", capture=False)  # sent by a scanner that didn't read capture facts
    assert _work(lib) == {unread: (["capture"], []), video: (["previews", "probe", "analysis_proxy"], []),
                          older: (ALL_WORK, [])}
    assert photo not in _work(lib)  # its scan made everything it needs the original for


@pytest.mark.slow
def test_what_is_made_leaves_the_list_by_itself(env):
    lib = _library(env, "ReaderMade")
    video, sha = _video(lib, "a.mov")
    _preview_sent(lib, video, sha)
    assert _work(lib) == {video: (["probe", "analysis_proxy"], [])}
    _probed(lib, video, sha)
    assert _work(lib) == {video: (["analysis_proxy"], [])}
    _analysis_proxy_made(lib, video, _want(lib, "analysis_proxy", sha))
    assert _work(lib) == {}


@pytest.mark.slow
def test_a_lost_preview_is_listed_again(env):
    # The missing-file repair clears a lost file's key (src/server/missing_artifacts.py): the reader makes it again.
    lib = _library(env, "ReaderLost")
    photo, _ = _photo(lib, "a.jpg")
    video, _ = _all_made(lib, "b.mov")
    assert _work(lib) == {}
    with _db(lib) as s:
        s.execute(text("UPDATE assets SET proxy_key = NULL, thumbnail_key = NULL WHERE asset_id = :a"), {"a": photo})
        s.execute(text("UPDATE assets SET video_preview_key = NULL WHERE asset_id = :a"), {"a": video})
        s.commit()
    assert _work(lib) == {photo: (["previews"], []), video: (["previews"], [])}


@pytest.mark.slow
def test_a_failure_waits_its_turn(env):
    client, headers, *_ = env
    lib = _library(env, "ReaderFailed")
    photo, _ = _photo(lib, "a.jpg", capture=False)
    r = client.post("/v1/producers/failures", json={"items": [
        {"asset_id": photo, "artifact": "capture", "error": "exiftool couldn't read it"}]}, headers=headers)
    assert r.status_code == 200 and r.json() == {"recorded": 1}, r.text
    assert _work(lib) == {}
    with _db(lib) as s:  # its turn comes
        s.execute(text("UPDATE artifact_lineage SET retry_at = now() - interval '1 second'"
                       " WHERE asset_id = :a AND artifact = 'capture'"), {"a": photo})
        s.commit()
    assert _work(lib) == {photo: (["capture"], [])}


# ---------------------------------------------------------------------------
# Remakes: apart, and only while the producer's redo runs
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_remakes_are_listed_apart_while_the_redo_runs(env):
    lib = _library(env, "ReaderRemake")
    video, _ = _all_made(lib, "a.mov")
    _set(lib, "analysis_proxy", quality="good")
    try:
        assert _work(lib) == {video: ([], ["analysis_proxy"])}
        _post(lib, "/v1/producers/analysis_proxy/pause", {"scope": "redo"})
        assert _work(lib) == {}  # its redo stopped: the clip keeps what it has
        _post(lib, "/v1/producers/analysis_proxy/resume", {"scope": "redo"})
        assert _work(lib) == {video: ([], ["analysis_proxy"])}
    finally:
        _set(lib, "analysis_proxy", quality=None)
    assert _work(lib) == {}  # low, high, low again: nothing to do


@pytest.mark.slow
def test_a_new_version_waits_to_be_seen_before_its_remakes_are_listed(env):
    from src.server.repository import lineage

    lib = _library(env, "ReaderVersion")
    video, sha = _all_made(lib, "a.mov")
    older = {**_want(lib, "analysis_proxy", sha), "version": "1"}  # made by the version before
    assert older["version"] != PRODUCERS["analysis_proxy"].version
    _analysis_proxy_made(lib, video, older)
    key = "producer.analysis_proxy.version_seen"
    with _db(lib) as s:
        s.execute(text("DELETE FROM system_metadata WHERE key = :k"), {"k": key})
        s.commit()
    try:
        assert _work(lib) == {}  # nobody approved the new version: the scheduler stops its redo when it sees it
        with _db(lib) as s:
            assert lineage.hold_new_version(s, "analysis_proxy")  # as the scheduler does
        assert _work(lib) == {}
        _post(lib, "/v1/producers/analysis_proxy/resume", {"scope": "redo"})  # an admin's yes
        assert _work(lib) == {video: ([], ["analysis_proxy"])}
    finally:
        _analysis_proxy_made(lib, video, _want(lib, "analysis_proxy", sha))
        _post(lib, "/v1/producers/analysis_proxy/resume", {"scope": "redo"})
    assert _work(lib) == {}


# ---------------------------------------------------------------------------
# Pauses
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_paused_work_is_not_listed(env):
    lib = _library(env, "ReaderPaused")
    library_id = lib[2]
    video, _ = _video(lib, "a.mov", capture=False)
    _post(lib, "/v1/producers/capture/pause", {"scope": "work"})
    try:
        assert _work(lib) == {video: (["previews", "probe", "analysis_proxy"], [])}
    finally:
        _post(lib, "/v1/producers/capture/resume", {"scope": "work"})
    _post(lib, "/v1/libraries/scans/pause", {"library_ids": [library_id]})
    try:  # only a scan makes previews
        assert _work(lib) == {video: (["probe", "capture", "analysis_proxy"], [])}
    finally:
        _post(lib, "/v1/libraries/scans/resume", {"library_ids": [library_id]})
    _post(lib, "/v1/producers/all/pause", {"scope": "work"})
    try:
        assert _work(lib) == {}
    finally:
        _post(lib, "/v1/producers/all/resume", {"scope": "work"})
    assert _work(lib) == {video: (ALL_WORK, [])}


# ---------------------------------------------------------------------------
# One rule: what the scheduler is handed
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_each_producers_work_is_what_the_scheduler_is_handed(env):
    lib = _library(env, "ReaderAgree")
    library_id, tenant_url = lib[2], lib[-1]
    _photo(lib, "a.jpg")
    _photo(lib, "b.jpg", capture=False)
    _video(lib, "c.mov", capture=False)
    probed, sha = _video(lib, "d.mov")
    _probed(lib, probed, sha)
    remade, _ = _all_made(lib, "e.mov")
    _all_made(lib, "f.mov")
    _set(lib, "analysis_proxy", quality="good")
    try:
        listed = _work(lib)
        for artifact in ("probe", "capture", "analysis_proxy"):
            kind = PRODUCERS[artifact].kind
            handed = {c["asset_id"] for c in due(tenant_url, kind, [library_id])}
            assert handed == {a for a, (work, _) in listed.items() if artifact in work}, artifact
            again = {c["asset_id"] for c in due(tenant_url, f"redo_{kind}", [library_id])}
            assert again == {a for a, (_, remakes) in listed.items() if artifact in remakes}, artifact
        assert listed[remade][1] == ["analysis_proxy"]
    finally:
        _set(lib, "analysis_proxy", quality=None)


# ---------------------------------------------------------------------------
# The call itself
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_the_list_comes_a_page_at_a_time_oldest_first(env):
    client, headers, *_ = env
    lib = _library(env, "ReaderPages")
    clips = [_photo(lib, f"{i}.jpg", capture=False)[0] for i in range(5)]
    r = client.get(f"/v1/libraries/{lib[2]}/reader-work", params={"limit": 2}, headers=headers)
    assert r.status_code == 200, r.text
    first = r.json()
    assert [c["asset_id"] for c in first["clips"]] == sorted(clips)[:2] and first["next_cursor"] == sorted(clips)[1]
    assert first["clips"][0] | {"asset_id": None} == {
        "asset_id": None, "rel_path": first["clips"][0]["rel_path"], "media_type": "image", "file_size": 1000,
        "content_hash": first["clips"][0]["content_hash"], "work": ["capture"], "remakes": []}
    assert list(_work(lib, limit=2)) == sorted(clips)


@pytest.mark.slow
def test_who_can_ask_and_about_what(env):
    from tests.test_archive_trash_safety import _key_with_role

    client, headers, *_ = env
    lib = _library(env, "ReaderWho")
    library_id = lib[2]
    viewer = _key_with_role(env, "viewer")
    assert client.get(f"/v1/libraries/{library_id}/reader-work", headers=viewer).status_code == 403
    editor = _key_with_role(env, "editor")
    assert client.get(f"/v1/libraries/{library_id}/reader-work", headers=editor).status_code == 200
    assert client.get("/v1/libraries/lib_nope/reader-work", headers=headers).status_code == 404
    assert client.get(f"/v1/libraries/{library_id}/reader-work", params={"limit": 0}, headers=headers).status_code == 422
    r = client.delete(f"/v1/libraries/{library_id}", headers=headers)
    assert r.status_code == 204, r.text
    r = client.get(f"/v1/libraries/{library_id}/reader-work", headers=headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "library_trashed", r.text
