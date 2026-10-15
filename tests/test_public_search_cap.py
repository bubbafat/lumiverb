# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Text search on a capped public page searches what the page shows.

No whole-transcript words, and no scene or transcript hits from past the
public playback cap (10 s until an admin lifts it); never a note, capped or
not; signed in, everything is searched. The real search (ADR-019) over real
clips: scenes found and described through the chunks API (tests/scenes.py),
descriptions, transcripts and notes saved through the API.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext

import pytest

from tests.machine_lineage import made
from tests.scenes import Scene, described_scenes
from tests.test_public_library_middleware import _ingest, public_lib_client  # noqa: F401

# What public pages play until an admin says otherwise (tenant_settings.PUBLIC_DEFAULT_SECONDS).
CAP_MS = 10_000


def _auth(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


def _srt(start_ms: int, end_ms: int, words: str) -> str:
    def stamp(ms: int) -> str:
        return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    return f"1\n{stamp(start_ms)} --> {stamp(end_ms)}\n{words}\n"


def _transcribe(env, asset_id: str, srt: str) -> None:
    client, api_key, *_ = env
    r = client.post(f"/v1/assets/{asset_id}/transcript", json={"source": "manual", "srt": srt, "language": "en"},
                    headers=_auth(api_key))
    assert r.status_code == 200, r.text


def _describe(env, asset_id: str, description: str) -> None:
    client, api_key, *_ = env
    r = client.post(f"/v1/assets/{asset_id}/vision", headers=_auth(api_key), json={
        "model_id": "test-vision", "model_version": "1", "description": description, "tags": [],
        "lineage": made("vision")})
    assert r.status_code == 200, r.text


def _note(env, asset_id: str, note: str) -> None:
    client, api_key, *_ = env
    r = client.put(f"/v1/assets/{asset_id}/note", json={"text": note}, headers=_auth(api_key))
    assert r.status_code == 200, r.text


def _search(env, word: str, *, signed_in: bool = False) -> dict[str, dict]:
    """What GET /v1/query finds for a word in the public library, as a
    visitor or signed in: {asset_id: its search_context}."""
    client, api_key, library_id, _ = env
    r = client.get("/v1/query", params=[("f", f"library:{library_id}"), ("f", f"query:{word}")],
                   headers=_auth(api_key) if signed_in else {})
    assert r.status_code == 200, r.text
    return {i["asset_id"]: i["search_context"] for i in r.json()["items"]}


@contextmanager
def _cap_lifted(env):
    """Public pages play whole videos (an admin's choice) for the while."""
    client, api_key, *_ = env
    r = client.patch("/v1/tenant/settings", json={"public_video_preview_max_seconds": None}, headers=_auth(api_key))
    assert r.status_code == 200, r.text
    try:
        yield
    finally:
        r = client.patch("/v1/tenant/settings", json={"public_video_preview_max_seconds": CAP_MS // 1000},
                         headers=_auth(api_key))
        assert r.status_code == 200, r.text


def _either_side_of_the_cap(env, word: str, folder: str) -> dict[str, str]:
    """Four public videos saying `word`: in a scene and in a transcript
    segment, each before the cap and past it."""
    client, api_key, library_id, _ = env
    clips = {name: _ingest(client, api_key, library_id, f"{folder}/{name}.mov")
             for name in ("early_scene", "late_scene", "early_words", "late_words")}
    described_scenes(client, _auth(api_key), clips["early_scene"], [Scene(4_000, 6_000, f"A {word} by the gate")])
    described_scenes(client, _auth(api_key), clips["late_scene"], [Scene(60_000, 62_000, f"A {word} in the vault")])
    _transcribe(env, clips["early_words"], _srt(1_000, 2_000, f"hello {word}"))
    _transcribe(env, clips["late_words"], _srt(90_000, 92_000, f"the {word} password"))
    return clips


@pytest.mark.slow
def test_capped_public_search_drops_hits_past_the_cap(public_lib_client):
    clips = _either_side_of_the_cap(public_lib_client, "quokka", "drops")
    found = _search(public_lib_client, "quokka")
    assert set(found) == {clips["early_scene"], clips["early_words"]}
    assert all(c["start_ms"] is not None and c["start_ms"] < CAP_MS for c in found.values())
    assert found[clips["early_scene"]]["hit_type"] == "scene"
    assert found[clips["early_words"]]["hit_type"] == "transcript"


@pytest.mark.slow
def test_capped_public_search_leaves_the_whole_transcript_out(public_lib_client):
    # Its only words are past the cap: neither the segment nor the whole
    # transcript (which the page doesn't show) finds it.
    client, api_key, library_id, _ = public_lib_client
    clip = _ingest(client, api_key, library_id, "whole/talk.mov")
    _transcribe(public_lib_client, clip, _srt(60_000, 63_000, "the narwhal surfaced"))

    assert clip not in _search(public_lib_client, "narwhal")
    assert clip in _search(public_lib_client, "narwhal", signed_in=True)
    with _cap_lifted(public_lib_client):  # the page shows the whole transcript: its search reads it
        assert clip in _search(public_lib_client, "narwhal")


@pytest.mark.slow
def test_signed_in_search_has_everything(public_lib_client):
    clips = _either_side_of_the_cap(public_lib_client, "wombat", "everything")
    found = _search(public_lib_client, "wombat", signed_in=True)
    assert set(found) == set(clips.values())
    assert found[clips["late_scene"]]["start_ms"] == 60_000
    assert found[clips["late_words"]]["start_ms"] == 90_000


@pytest.mark.fast
def test_the_public_guard_takes_only_a_plain_and_of_filters():
    # An OR could widen a visitor's scope past the library filter.
    from unittest.mock import MagicMock

    from fastapi import HTTPException

    from src.server.api.routers.query import guard_public_spec
    from src.server.models.query_filter import (
        Combinator,
        GroupFilter,
        LibraryScope,
        MediaType,
        QuerySpec,
    )

    request = MagicMock()
    request.state.is_public_request = True
    session = MagicMock()
    lib = LibraryScope(library_ids=("lib_pub",))
    nested = QuerySpec(root=GroupFilter(children=(lib, GroupFilter(combinator=Combinator.OR, children=(MediaType(types=("video",)),)))))
    with pytest.raises(HTTPException) as e:
        guard_public_spec(request, session, nested)
    assert e.value.status_code == 403
    either = QuerySpec(root=GroupFilter(combinator=Combinator.OR, children=(lib,)))
    with pytest.raises(HTTPException):
        guard_public_spec(request, session, either)


@pytest.mark.slow
def test_a_capped_pages_ranking_doesnt_read_the_transcript_when_matching_doesnt(public_lib_client):
    # Two videos described alike; one's transcript says the word again, past
    # the cap. A visitor's search ranks them alike: what the page doesn't
    # show doesn't count. Signed in, the transcript counts too.
    client, api_key, library_id, _ = public_lib_client
    plain = _ingest(client, api_key, library_id, "rank-t/plain.mov")
    spoken = _ingest(client, api_key, library_id, "rank-t/spoken.mov")
    for clip in (plain, spoken):
        _describe(public_lib_client, clip, "A heron by the lake")
    _transcribe(public_lib_client, spoken, _srt(60_000, 63_000, "heron heron heron"))

    public = _search(public_lib_client, "heron")
    assert set(public) == {plain, spoken}
    assert public[plain]["score"] == public[spoken]["score"]
    signed_in = _search(public_lib_client, "heron", signed_in=True)
    assert signed_in[spoken]["score"] > signed_in[plain]["score"]


@pytest.mark.slow
def test_public_search_leaves_notes_out_even_uncapped(public_lib_client):
    client, api_key, library_id, _ = public_lib_client
    clip = _ingest(client, api_key, library_id, "notes-uncapped/clip.mov")
    _note(public_lib_client, clip, "the pangolin shot is for the client only")

    with _cap_lifted(public_lib_client):
        assert clip not in _search(public_lib_client, "pangolin")
        assert clip in _search(public_lib_client, "pangolin", signed_in=True)


@pytest.mark.slow
def test_a_visitors_ranking_leaves_notes_out(public_lib_client):
    # Two photos described alike; one's note says the word again. Visitors
    # (capped page or not) rank them alike; signed in, the note counts.
    client, api_key, library_id, _ = public_lib_client
    plain = _ingest(client, api_key, library_id, "rank-n/plain.jpg", media_type="image")
    noted = _ingest(client, api_key, library_id, "rank-n/noted.jpg", media_type="image")
    for clip in (plain, noted):
        _describe(public_lib_client, clip, "A pelican on a post")
    _note(public_lib_client, noted, "pelican, pelican: the best pelican")

    for lifted in (False, True):
        with _cap_lifted(public_lib_client) if lifted else nullcontext():
            public = _search(public_lib_client, "pelican")
            assert set(public) == {plain, noted}, lifted
            assert public[plain]["score"] == public[noted]["score"], lifted
    signed_in = _search(public_lib_client, "pelican", signed_in=True)
    assert signed_in[noted]["score"] > signed_in[plain]["score"]
