# ruff: noqa: F811 — pytest fixtures are named as parameters
"""A person's corrections sit beside the machine's values (ADR-016 phase 3, piece 4).

Transcripts: a person's is shown on top of the machine's, which is kept
underneath with how it was made. Re-transcribing updates the one
underneath and never touches theirs; removing theirs brings it back.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from src.shared import producers as P
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _row, _sha, _want
from tests.test_reconciler import _due, _library

SRT_MACHINE = "1\n00:00:00,000 --> 00:00:02,000\nhello from whisper\n"
SRT_MACHINE_2 = "1\n00:00:00,000 --> 00:00:02,000\nhello again from whisper\n"
SRT_PERSON = "1\n00:00:00,000 --> 00:00:02,000\nhello from Robert\n"


def _video(env, name: str) -> tuple[tuple, str, str]:
    lib = _library(env, name)
    sha = _sha()
    vid = _ingest_with(lib, "a.mov", sha, None, media_type="video")
    with _db(env) as s:
        s.execute(text("UPDATE assets SET duration_sec = 30 WHERE asset_id = :a"), {"a": vid})
        s.commit()
    return lib, vid, sha


def _transcribe(env, vid: str, srt: str, sha: str | None = None, source: str = "whisper"):
    client, headers, *_ = env
    body = {"srt": srt, "language": "en", "source": source}
    if source != "manual":
        body["lineage"] = _want(env, "transcript", sha)
    r = client.post(f"/v1/assets/{vid}/transcript", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["status"]


def _shown(env, vid: str) -> dict:
    client, headers, *_ = env
    d = client.get(f"/v1/assets/{vid}", headers=headers).json()
    return {"srt": d.get("transcript_srt"), "source": d.get("transcript_source"),
            "underneath": d.get("machine_transcript")}


def _remove(env, vid: str, which: str = "manual", status: int = 204, headers=None):
    """Remove the transcript shown, saying whose it is ("manual": a person's)."""
    client, own, *_ = env
    r = client.delete(f"/v1/assets/{vid}/transcript", params={"which": which}, headers=headers or own)
    assert r.status_code == status, r.text
    return r


@pytest.mark.slow
def test_a_persons_transcript_sits_on_the_machines(env):
    lib, vid, sha = _video(env, "CorrTranscript")
    _transcribe(env, vid, SRT_MACHINE, sha)
    _transcribe(env, vid, SRT_PERSON, source="manual")
    assert _shown(env, vid) == {"srt": SRT_PERSON, "source": "manual", "underneath": True}
    assert _row(env, vid, "transcript")[0] == P.PERSON

    # Re-transcribing goes under it, never over it.
    assert _transcribe(env, vid, SRT_MACHINE_2, sha) == "kept_manual"
    assert _shown(env, vid)["srt"] == SRT_PERSON

    # Removing theirs brings the machine's back, the latest one, as current.
    _remove(env, vid)
    assert _shown(env, vid) == {"srt": SRT_MACHINE_2, "source": "whisper", "underneath": False}
    assert _row(env, vid, "transcript")[0] == "whisper"
    assert _due(lib, "missing_transcription") == []


@pytest.mark.slow
def test_removing_the_only_transcript_leaves_none_for_good(env):
    lib, vid, sha = _video(env, "CorrNoMachine")
    _transcribe(env, vid, SRT_PERSON, source="manual")
    assert _shown(env, vid)["underneath"] is False
    _remove(env, vid)
    assert _shown(env, vid)["srt"] is None
    assert _due(lib, "missing_transcription") == []  # the person said none: not regenerated


@pytest.mark.slow
def test_a_machine_transcript_with_no_speech_comes_back_as_no_speech(env):
    lib, vid, sha = _video(env, "CorrNoSpeech")
    assert _transcribe(env, vid, "", sha) == "no_speech"
    _transcribe(env, vid, SRT_PERSON, source="manual")
    _remove(env, vid)
    shown = _shown(env, vid)
    assert shown["srt"] is None and shown["source"] == "whisper"
    with _db(env) as s:
        assert s.execute(text("SELECT has_transcript FROM assets WHERE asset_id = :a"), {"a": vid}).scalar() is False


@pytest.mark.slow
def test_removing_the_machines_transcript_is_for_good(env):
    # A person who removes the machine's transcript doesn't want it back,
    # not even after adding and removing one of their own.
    lib, vid, sha = _video(env, "CorrMachineGone")
    _transcribe(env, vid, SRT_MACHINE, sha)
    _remove(env, vid, "machine")
    assert _shown(env, vid) == {"srt": None, "source": None, "underneath": False}
    _transcribe(env, vid, SRT_PERSON, source="manual")
    assert _shown(env, vid)["underneath"] is False
    _remove(env, vid)
    assert _shown(env, vid)["srt"] is None
    assert _due(lib, "missing_transcription") == []


@pytest.mark.slow
def test_removing_twice_never_removes_both(env):
    # A double click on "remove mine" must not take the machine's too.
    lib, vid, sha = _video(env, "CorrRemoveTwice")
    _transcribe(env, vid, SRT_MACHINE, sha)
    _transcribe(env, vid, SRT_PERSON, source="manual")
    _remove(env, vid, "manual")
    r = _remove(env, vid, "manual", status=409)
    assert r.json()["error"]["code"] == "transcript_changed"
    assert r.json()["error"]["details"] == {"shown": "machine"}
    assert _shown(env, vid)["srt"] == SRT_MACHINE

    # Removing what's already gone is done already.
    _remove(env, vid, "machine")
    _remove(env, vid, "machine")
    assert _shown(env, vid)["srt"] is None


@pytest.mark.slow
def test_removing_a_transcript_says_whose(env):
    client, headers, *_ = env
    lib, vid, sha = _video(env, "CorrRemoveWhose")
    _transcribe(env, vid, SRT_MACHINE, sha)
    assert client.delete(f"/v1/assets/{vid}/transcript", headers=headers).status_code == 422
    assert client.delete(f"/v1/assets/{vid}/transcript", params={"which": "all"}, headers=headers).status_code == 422
    r = _remove(env, vid, "manual", status=409)
    assert r.json()["error"]["details"] == {"shown": "machine"}
    assert _shown(env, vid)["srt"] == SRT_MACHINE


@pytest.mark.slow
def test_only_editors_upload_or_remove_a_transcript(env):
    from tests.test_archive_trash_safety import _key_with_role

    client, headers, *_ = env
    lib, vid, sha = _video(env, "CorrTranscriptRole")
    viewer, editor = _key_with_role(env, "viewer"), _key_with_role(env, "editor")
    person = {"srt": SRT_PERSON, "language": "en", "source": "manual"}
    assert client.post(f"/v1/assets/{vid}/transcript", json=person, headers=viewer).status_code == 403
    assert client.post(f"/v1/assets/{vid}/transcript", json=person, headers=editor).status_code == 200
    _remove(env, vid, "manual", status=403, headers=viewer)
    _remove(env, vid, "manual", headers=editor)


@pytest.mark.slow
def test_a_public_librarys_page_shows_only_what_a_person_sees(env):
    client, headers, *_ = env
    lib, vid, sha = _video(env, "CorrPublicLibrary")
    _transcribe(env, vid, SRT_MACHINE, sha)
    _transcribe(env, vid, SRT_PERSON, source="manual")
    _describe_as(env, vid, "a cat", ["cat"])
    assert _correct(env, vid, description="Mittens", tags=["Mittens"]).status_code == 200
    library_id = lib[2]
    assert client.patch(f"/v1/libraries/{library_id}", json={"is_public": True}, headers=headers).status_code == 200
    try:
        pages = [client.get(f"/v1/assets/{vid}", params={"public_library_id": library_id}),
                 client.get("/v1/assets/by-path", params={"library_id": library_id, "rel_path": "a.mov"})]
    finally:
        client.patch(f"/v1/libraries/{library_id}", json={"is_public": False}, headers=headers)
    for r in pages:
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["ai_description"] == "Mittens" and d["ai_tags"] == ["Mittens"]
        assert d["machine_description"] is None and d["machine_tags"] == [] and d["corrected"] == []
        assert d["transcript_source"] is None and d["machine_transcript"] is False


@pytest.mark.slow
def test_a_public_page_doesnt_say_whats_underneath(env):
    from src.server.api.routers.assets import AssetResponse, _project_visitor_view

    lib, vid, sha = _video(env, "CorrPublic")
    _transcribe(env, vid, SRT_MACHINE, sha)
    _transcribe(env, vid, SRT_PERSON, source="manual")
    client, headers, *_ = env
    full = AssetResponse(**client.get(f"/v1/assets/{vid}", headers=headers).json())
    assert full.machine_transcript is True and full.transcript_source == "manual"
    visitor = _project_visitor_view(full)
    assert visitor.machine_transcript is False and visitor.transcript_source is None


# ---------------------------------------------------------------------------
# Descriptions, OCR and tags: a person's edit wins, the machine's value is
# kept underneath; tags are adds and removes on top of the machine's list.
# ---------------------------------------------------------------------------


def _image(env, name: str) -> tuple[tuple, str, str]:
    lib = _library(env, name)
    sha = _sha()
    return lib, _ingest_with(lib, "a.jpg", sha, None), sha


def _describe_as(env, clip: str, description: str, tags: list[str], model: str = "qwen3-vl:8b"):
    client, headers, *_ = env
    r = client.post(f"/v1/assets/{clip}/vision", json={"model_id": model, "description": description, "tags": tags},
                    headers=headers)
    assert r.status_code == 200, r.text


def _correct(env, clip: str, headers=None, **body):
    client, own, *_ = env
    return client.patch(f"/v1/assets/{clip}/corrections", json=body, headers=headers or own)


def _detail(env, clip: str) -> dict:
    client, headers, *_ = env
    return client.get(f"/v1/assets/{clip}", headers=headers).json()


@pytest.mark.slow
def test_a_corrected_description_wins_and_the_machines_is_kept(env):
    lib, clip, sha = _image(env, "CorrDesc")
    _describe_as(env, clip, "a cat on a sofa", ["cat", "sofa"])
    r = _correct(env, clip, description="Mittens on Grandma's sofa")
    assert r.status_code == 200, r.text
    d = _detail(env, clip)
    assert d["ai_description"] == "Mittens on Grandma's sofa"
    assert d["machine_description"] == "a cat on a sofa"
    assert d["corrected"] == ["description"]

    # Describing again changes what's underneath, never the correction.
    _describe_as(env, clip, "a grey cat on a couch", ["cat", "couch"])
    d = _detail(env, clip)
    assert d["ai_description"] == "Mittens on Grandma's sofa" and d["machine_description"] == "a grey cat on a couch"

    # Back to the machine's.
    assert _correct(env, clip, description=None).status_code == 200
    d = _detail(env, clip)
    assert d["ai_description"] == "a grey cat on a couch" and d["corrected"] == []


@pytest.mark.slow
def test_tag_edits_are_adds_and_removes_on_the_machines_list(env):
    lib, clip, sha = _image(env, "CorrTags")
    _describe_as(env, clip, "a dog on a beach", ["dog", "beach", "ocean"])
    assert _correct(env, clip, tags=["dog", "beach", "Rex"]).status_code == 200  # ocean out, Rex in
    assert sorted(_detail(env, clip)["ai_tags"]) == ["Rex", "beach", "dog"]

    # A new machine list keeps the person's adds and removes.
    _describe_as(env, clip, "a dog by the sea", ["dog", "ocean", "sand"])
    d = _detail(env, clip)
    assert sorted(d["ai_tags"]) == ["Rex", "dog", "sand"]
    assert sorted(d["machine_tags"]) == ["dog", "ocean", "sand"]
    assert d["corrected"] == ["tags"]


@pytest.mark.slow
def test_tag_edits_last_through_the_machine_changing_its_mind(env):
    lib, clip, sha = _image(env, "CorrTagsLast")
    _describe_as(env, clip, "a dog on a beach", ["dog", "beach", "ocean"])
    _correct(env, clip, tags=["dog", "beach", "Rex"])  # ocean out, Rex in

    # The machine drops ocean and finds Rex itself; the person then takes out sand.
    _describe_as(env, clip, "Rex on the sand", ["dog", "sand", "Rex"])
    assert sorted(_detail(env, clip)["ai_tags"]) == ["Rex", "dog", "sand"]
    _correct(env, clip, tags=["dog", "Rex"])

    # Ocean stays out and Rex stays in, whatever the machine says next.
    _describe_as(env, clip, "a dog by the ocean", ["dog", "ocean", "beach"])
    assert sorted(_detail(env, clip)["ai_tags"]) == ["Rex", "beach", "dog"]

    # Putting a removed tag back undoes its removal.
    _correct(env, clip, tags=["dog", "Rex", "beach", "ocean"])
    _describe_as(env, clip, "a dog by the ocean", ["dog", "ocean"])
    assert sorted(_detail(env, clip)["ai_tags"]) == ["Rex", "dog", "ocean"]


@pytest.mark.slow
def test_tags_that_arent_a_list_dont_break_anything(env):
    client, headers, *_ = env
    lib, clip, sha = _image(env, "CorrNullTags")
    _describe_as(env, clip, "a cat", ["cat"])
    for odd in ("null", '"cat"', '{"a": 1}'):
        with _db(env) as s:
            s.execute(text("UPDATE asset_metadata SET data = jsonb_set(data, '{tags}', CAST(:v AS jsonb))"
                           " WHERE asset_id = :a"), {"a": clip, "v": odd})
            s.commit()
        assert _detail(env, clip)["ai_tags"] == []
        r = client.get("/v1/query", params=[("f", f"library:{lib[2]}"), ("f", "tag:cat")], headers=headers)
        assert r.status_code == 200 and r.json()["items"] == [], r.text
        r = client.get("/v1/assets/facets", params=[("f", f"library:{lib[2]}")], headers=headers)
        assert r.status_code == 200, r.text
        assert _correct(env, clip, tags=["Mittens"]).status_code == 200
        assert _detail(env, clip)["ai_tags"] == ["Mittens"]
        _correct(env, clip, tags=None)


@pytest.mark.slow
def test_undoing_every_correction_while_search_is_down_still_reaches_search(env, monkeypatch):
    import src.server.search.sync as sync

    lib, clip, sha = _image(env, "CorrSearchDown")
    _describe_as(env, clip, "a cat", ["cat"])
    _correct(env, clip, description="Mittens")
    with _db(env) as s:
        s.execute(text("UPDATE assets SET search_synced_at = now() WHERE asset_id = :a"), {"a": clip})
        s.commit()
    monkeypatch.setattr(sync, "try_sync_asset", lambda *a, **k: None)  # Quickwit down
    assert _correct(env, clip, description=None).status_code == 200
    with _db(env) as s:
        stale = s.execute(text(f"SELECT {sync.STALE_SEARCH} FROM active_assets a WHERE a.asset_id = :a"),
                          {"a": clip}).scalar()
    assert stale is True


@pytest.mark.slow
def test_a_corrected_ocr_wins_and_a_new_reading_goes_underneath(env):
    client, headers, *_ = env
    lib, clip, sha = _image(env, "CorrOcr")
    client.post(f"/v1/assets/{clip}/ocr", json={"ocr_text": "PLATFORN 9"}, headers=headers)
    _correct(env, clip, ocr_text="PLATFORM 9")
    client.post(f"/v1/assets/{clip}/ocr", json={"ocr_text": "PLATF0RM 9"}, headers=headers)
    d = _detail(env, clip)
    assert d["ocr_text"] == "PLATFORM 9" and d["machine_ocr_text"] == "PLATF0RM 9"


@pytest.mark.slow
def test_filters_and_search_see_what_the_person_sees(env):
    from src.server.search.postgres_search import search_assets

    client, headers, *_ = env
    lib, clip, sha = _image(env, "CorrFilters")
    _describe_as(env, clip, "a cat on a sofa", ["cat", "sofa"])
    _correct(env, clip, description="Mittens asleep", tags=["cat", "Mittens"])

    def tagged(tag: str) -> list[str]:
        r = client.get("/v1/query", params=[("f", f"library:{lib[2]}"), ("f", f"tag:{tag}")], headers=headers)
        assert r.status_code == 200, r.text
        return [i["asset_id"] for i in r.json()["items"]]

    assert tagged("Mittens") == [clip] and tagged("sofa") == []
    with _db(env) as s:
        assert [h["asset_id"] for h in search_assets(s, lib[2], "mittens asleep")] == [clip]
        assert search_assets(s, lib[2], "sofa") == []


@pytest.mark.slow
def test_a_clip_with_no_description_can_be_given_one(env):
    lib, clip, sha = _image(env, "CorrNoMachineDesc")
    assert _correct(env, clip, description="The old station", tags=["station"]).status_code == 200
    d = _detail(env, clip)
    assert d["ai_description"] == "The old station" and d["ai_tags"] == ["station"]
    assert d["machine_description"] is None


@pytest.mark.slow
def test_only_editors_correct(env):
    from tests.test_archive_trash_safety import _key_with_role

    lib, clip, sha = _image(env, "CorrRole")
    assert _correct(env, clip, headers=_key_with_role(env, "viewer"), description="x").status_code == 403
    assert _correct(env, clip, headers=_key_with_role(env, "editor"), description="x").status_code == 200
    client, headers, *_ = env
    assert client.patch("/v1/assets/ast_nope/corrections", json={"description": "x"},
                        headers=headers).status_code == 404


@pytest.mark.slow
def test_a_correction_is_human_data_and_a_public_page_shows_only_the_result(env):
    from src.server.api.routers.assets import AssetResponse, _project_visitor_view

    client, headers, *_ = env
    lib, clip, sha = _image(env, "CorrPublicView")
    _describe_as(env, clip, "a cat", ["cat"])
    _correct(env, clip, description="Mittens")
    visitor = _project_visitor_view(AssetResponse(**_detail(env, clip)))
    assert visitor.ai_description == "Mittens"
    assert visitor.machine_description is None and visitor.corrected == []
    from src.server.repository.tenant import AssetRepository

    with _db(env) as s:
        assert AssetRepository(s).has_human_data(clip)
