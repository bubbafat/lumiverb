# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Edits keep only the latest (Robert, Oct 9).

A person's description, tags, OCR text or transcript is the one copy: the
machine's is dropped, no machine copy is kept underneath, and machine
output never replaces it. A person's tag edit fixes the whole list.
Removing what a person wrote leaves none, and the machine makes it again;
removing the machine's transcript is for good.
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
    return {"srt": d.get("transcript_srt"), "source": d.get("transcript_source")}


def _remove(env, vid: str, which: str | None = "manual", status: int = 204, headers=None):
    """Remove the transcript shown, saying whose it is ("manual": a person's; None: whatever's shown)."""
    client, own, *_ = env
    params = {"which": which} if which else {}
    r = client.delete(f"/v1/assets/{vid}/transcript", params=params, headers=headers or own)
    assert r.status_code == status, r.text
    return r


def _lineage_row(env, vid: str, artifact: str):
    with _db(env) as s:
        return s.execute(text("SELECT producer FROM artifact_lineage WHERE asset_id = :a AND artifact = :k"),
                         {"a": vid, "k": artifact}).scalar()


@pytest.mark.slow
def test_a_persons_transcript_replaces_the_machines_and_keeps_no_copy(env):
    lib, vid, sha = _video(env, "CorrTranscript")
    _transcribe(env, vid, SRT_MACHINE, sha)
    _transcribe(env, vid, SRT_PERSON, source="manual")
    assert _shown(env, vid) == {"srt": SRT_PERSON, "source": "manual"}
    assert _row(env, vid, "transcript")[0] == P.PERSON

    # The machine never replaces it, and nothing of its output is kept.
    assert _transcribe(env, vid, SRT_MACHINE_2, sha) == "kept_manual"
    assert _shown(env, vid)["srt"] == SRT_PERSON

    # Removing theirs leaves none, and it's missing again: the machine makes one.
    _remove(env, vid)
    assert _shown(env, vid) == {"srt": None, "source": None}
    assert _lineage_row(env, vid, "transcript") is None
    assert _due(lib, "missing_transcription") == [vid]
    _transcribe(env, vid, SRT_MACHINE_2, sha)
    assert _shown(env, vid) == {"srt": SRT_MACHINE_2, "source": "whisper"}


@pytest.mark.slow
def test_the_latest_machine_transcript_is_the_one(env):
    lib, vid, sha = _video(env, "CorrLatestMachine")
    _transcribe(env, vid, SRT_MACHINE, sha)
    _transcribe(env, vid, SRT_MACHINE_2, sha)
    assert _shown(env, vid) == {"srt": SRT_MACHINE_2, "source": "whisper"}


@pytest.mark.slow
def test_a_persons_transcript_over_no_speech_and_removed_is_made_again(env):
    lib, vid, sha = _video(env, "CorrNoSpeech")
    assert _transcribe(env, vid, "", sha) == "no_speech"
    _transcribe(env, vid, SRT_PERSON, source="manual")
    _remove(env, vid)
    assert _shown(env, vid)["srt"] is None
    assert _due(lib, "missing_transcription") == [vid]


@pytest.mark.slow
def test_removing_the_machines_transcript_is_for_good(env):
    # A person who removes the machine's transcript doesn't want it back.
    lib, vid, sha = _video(env, "CorrMachineGone")
    _transcribe(env, vid, SRT_MACHINE, sha)
    _remove(env, vid, "machine")
    assert _shown(env, vid) == {"srt": None, "source": None}
    assert _due(lib, "missing_transcription") == []
    assert _row(env, vid, "transcript")[0] == P.PERSON


@pytest.mark.slow
def test_removing_twice_never_removes_more_than_meant(env):
    # A second click on "remove mine" once the machine has made one again
    # must not take the machine's.
    lib, vid, sha = _video(env, "CorrRemoveTwice")
    _transcribe(env, vid, SRT_PERSON, source="manual")
    _remove(env, vid, "manual")
    _remove(env, vid, "manual")  # nothing shown: nothing to do
    _transcribe(env, vid, SRT_MACHINE, sha)
    r = _remove(env, vid, "manual", status=409)
    assert r.json()["error"]["code"] == "transcript_changed"
    assert r.json()["error"]["details"] == {"shown": "machine"}
    assert _shown(env, vid)["srt"] == SRT_MACHINE


@pytest.mark.slow
def test_removing_without_saying_whose_removes_whats_shown(env):
    lib, vid, sha = _video(env, "CorrRemoveShown")
    _transcribe(env, vid, SRT_MACHINE, sha)
    _remove(env, vid, None)
    assert _shown(env, vid)["srt"] is None and _due(lib, "missing_transcription") == []
    client, headers, *_ = env
    assert client.delete(f"/v1/assets/{vid}/transcript", params={"which": "all"},
                         headers=headers).status_code == 422


@pytest.mark.slow
def test_a_transcript_from_before_sources_is_the_machines(env):
    # Transcripts made before transcript_source existed have none.
    lib, vid, sha = _video(env, "CorrNullSource")
    _transcribe(env, vid, SRT_MACHINE, sha)
    with _db(env) as s:
        s.execute(text("UPDATE assets SET transcript_source = NULL WHERE asset_id = :a"), {"a": vid})
        s.commit()
    r = _remove(env, vid, "manual", status=409)
    assert r.json()["error"]["details"] == {"shown": "machine"}
    _remove(env, vid, "machine")
    assert _shown(env, vid) == {"srt": None, "source": None}


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
        assert d["ai_description"] == "Mittens" and d["ai_tags"] == ["Mittens"] and d["corrected"] == []
        assert d["transcript_source"] is None
        assert not {"machine_description", "machine_tags", "machine_ocr_text", "machine_transcript"} & d.keys()


@pytest.mark.slow
def test_a_public_page_doesnt_say_whose_it_is(env):
    from src.server.api.routers.assets import AssetResponse, _project_visitor_view

    lib, vid, sha = _video(env, "CorrPublic")
    _transcribe(env, vid, SRT_PERSON, source="manual")
    client, headers, *_ = env
    full = AssetResponse(**client.get(f"/v1/assets/{vid}", headers=headers).json())
    assert full.transcript_source == "manual"
    assert _project_visitor_view(full).transcript_source is None


# ---------------------------------------------------------------------------
# Descriptions, OCR and tags: what a person writes is the one copy.
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


def _stored(env, clip: str) -> dict:
    """What the machine's description store holds for the clip (its latest row's data)."""
    with _db(env) as s:
        return s.execute(text("SELECT data FROM asset_metadata WHERE asset_id = :a ORDER BY generated_at DESC"
                              " LIMIT 1"), {"a": clip}).scalar()


@pytest.mark.slow
def test_a_persons_description_is_the_one_copy(env):
    lib, clip, sha = _image(env, "CorrDesc")
    _describe_as(env, clip, "a cat on a sofa", ["cat", "sofa"])
    r = _correct(env, clip, description="Mittens on Grandma's sofa")
    assert r.status_code == 200, r.text
    d = _detail(env, clip)
    assert d["ai_description"] == "Mittens on Grandma's sofa" and d["corrected"] == ["description"]
    assert "machine_description" not in d
    assert "description" not in _stored(env, clip) and _stored(env, clip)["tags"] == ["cat", "sofa"]

    # Describing again never replaces it, and keeps no copy under it; the tags it isn't the person's update.
    _describe_as(env, clip, "a grey cat on a couch", ["cat", "couch"])
    d = _detail(env, clip)
    assert d["ai_description"] == "Mittens on Grandma's sofa" and d["ai_tags"] == ["cat", "couch"]
    assert "description" not in _stored(env, clip)

    # Removing it leaves none, and the machine makes it again.
    assert _correct(env, clip, description=None).status_code == 200
    d = _detail(env, clip)
    assert d["ai_description"] is None and d["corrected"] == []
    assert _due(lib, "missing_vision") == [clip] and _lineage_row(env, clip, "vision") is None
    _describe_as(env, clip, "a grey cat asleep", ["cat"])
    assert _detail(env, clip)["ai_description"] == "a grey cat asleep"


@pytest.mark.slow
def test_a_persons_tags_fix_the_whole_list(env):
    lib, clip, sha = _image(env, "CorrTags")
    _describe_as(env, clip, "a dog on a beach", ["dog", "beach", "ocean"])
    assert _correct(env, clip, tags=["dog", "beach", "Rex"]).status_code == 200
    assert _detail(env, clip)["ai_tags"] == ["dog", "beach", "Rex"]
    assert "tags" not in _stored(env, clip)

    # A new machine list changes nothing: the person's list is the clip's.
    _describe_as(env, clip, "a dog by the sea", ["dog", "ocean", "sand"])
    d = _detail(env, clip)
    assert d["ai_tags"] == ["dog", "beach", "Rex"] and d["corrected"] == ["tags"]
    assert d["ai_description"] == "a dog by the sea"
    assert "machine_tags" not in d

    # An empty list is the person's too: no tags.
    _correct(env, clip, tags=[])
    _describe_as(env, clip, "a dog", ["dog"])
    assert _detail(env, clip)["ai_tags"] == []


@pytest.mark.slow
def test_tags_that_arent_a_list_dont_break_anything(env):
    client, headers, *_ = env
    lib, clip, sha = _image(env, "CorrNullTags")
    for odd in ("null", '"cat"', '{"a": 1}'):
        _describe_as(env, clip, "a cat", ["cat"])
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
def test_undoing_every_edit_while_search_is_down_still_reaches_search(env, monkeypatch):
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
def test_a_persons_ocr_is_the_one_copy(env):
    client, headers, *_ = env
    lib, clip, sha = _image(env, "CorrOcr")
    client.post(f"/v1/assets/{clip}/ocr", json={"ocr_text": "PLATFORN 9"}, headers=headers)
    _correct(env, clip, ocr_text="PLATFORM 9")
    client.post(f"/v1/assets/{clip}/ocr", json={"ocr_text": "PLATF0RM 9"}, headers=headers)
    d = _detail(env, clip)
    assert d["ocr_text"] == "PLATFORM 9" and "machine_ocr_text" not in d
    with _db(env) as s:
        assert s.execute(text("SELECT text FROM asset_ocr WHERE asset_id = :a"), {"a": clip}).scalar() == ""
    _correct(env, clip, ocr_text=None)
    assert _detail(env, clip)["ocr_text"] is None
    assert _due(lib, "missing_ocr") == [clip]


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
def test_a_clip_with_no_description_can_be_given_one_and_the_machine_leaves_it(env):
    lib, clip, sha = _image(env, "CorrNoMachineDesc")
    assert _correct(env, clip, description="The old station", tags=["station"]).status_code == 200
    d = _detail(env, clip)
    assert d["ai_description"] == "The old station" and d["ai_tags"] == ["station"]
    _describe_as(env, clip, "a building", ["building"])  # it was missing: the machine makes it
    d = _detail(env, clip)
    assert d["ai_description"] == "The old station" and d["ai_tags"] == ["station"]
    assert _due(lib, "missing_vision") == []


@pytest.mark.slow
def test_a_correction_returns_the_clips_whole_detail(env):
    client, headers, *_ = env
    lib, vid, sha = _video(env, "CorrPatchDetail")
    _transcribe(env, vid, SRT_PERSON, source="manual")
    r = _correct(env, vid, description="The ship's horn")
    assert r.status_code == 200, r.text
    detail = client.get(f"/v1/assets/{vid}", headers=headers).json()
    assert r.json() == detail
    assert detail["transcript_source"] == "manual" and detail["ai_description"] == "The ship's horn"


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

    lib, clip, sha = _image(env, "CorrPublicView")
    _describe_as(env, clip, "a cat", ["cat"])
    _correct(env, clip, description="Mittens")
    visitor = _project_visitor_view(AssetResponse(**_detail(env, clip)))
    assert visitor.ai_description == "Mittens" and visitor.corrected == []
    from src.server.repository.tenant import AssetRepository

    with _db(env) as s:
        assert AssetRepository(s).has_human_data(clip)


# ---------------------------------------------------------------------------
# Review: what a person wrote isn't the machine's work to redo, and their
# choices hold against machine output arriving late or at the same time.
# ---------------------------------------------------------------------------


@pytest.fixture
def vision_on(env):
    from tests.test_redo_on_change import VISION, _set_model

    _set_model(env, "vision", VISION)


@pytest.mark.slow
def test_text_a_person_wrote_isnt_read_again_or_counted_for_a_new_model(env, vision_on):
    from src.server.repository import lineage
    from tests.test_redo_on_change import _due as _redo_due, _made_old

    lib = _library(env, "CorrOcrTheirs")
    sha = _sha()
    clip = _ingest_with(lib, "a.jpg", sha, None)
    _made_old(lib, clip, sha, "ocr")
    assert _redo_due(lib, "redo_ocr") == [clip]
    assert _correct(lib, clip, ocr_text="PLATFORM 9").status_code == 200
    assert _redo_due(lib, "redo_ocr") == []
    with _db(lib) as s:
        assert lineage.made_by_a_producer(s, ["ocr"]).get("ocr", 0) == 0
    # On a clip never read, it isn't missing either: the reading would only be dropped.
    unread = _ingest_with(lib, "b.jpg", _sha(), None)
    _correct(lib, unread, ocr_text="EXIT")
    assert unread not in _due(lib, "missing_ocr")
    # Removed, it's the machine's to read again.
    _correct(lib, unread, ocr_text=None)
    assert unread in _due(lib, "missing_ocr")


@pytest.mark.slow
def test_a_description_and_tags_a_person_wrote_arent_described_again(env, vision_on):
    from tests.test_redo_on_change import _due as _redo_due, _made_old

    lib = _library(env, "CorrVisionTheirs")
    sha = _sha()
    clip = _ingest_with(lib, "a.jpg", sha, None)
    _made_old(lib, clip, sha, "vision")
    _correct(lib, clip, description="Mittens")
    assert _redo_due(lib, "redo_vision") == [clip]  # the machine's tags are still its to redo
    _correct(lib, clip, tags=["cat"])
    assert _redo_due(lib, "redo_vision") == []
    unread = _ingest_with(lib, "b.jpg", _sha(), None)
    _correct(lib, unread, description="The old station", tags=["station"])
    assert unread not in _due(lib, "missing_vision")
    assert _detail(lib, unread)["ai_description"] == "The old station"
    # Removing the description makes it the machine's again; the person's tags stay theirs.
    _correct(lib, unread, description=None)
    assert unread in _due(lib, "missing_vision")
    assert _detail(lib, unread)["ai_tags"] == ["station"]


@pytest.mark.slow
def test_a_late_machine_transcript_doesnt_undo_removing_the_machines(env):
    # A transcription in flight when a person removed the machine's for good.
    lib, vid, sha = _video(env, "CorrLateMachine")
    _transcribe(env, vid, SRT_MACHINE, sha)
    _remove(env, vid, "machine")
    assert _transcribe(env, vid, SRT_MACHINE_2, sha) == "kept_manual"
    assert _shown(env, vid) == {"srt": None, "source": None}
    assert _row(env, vid, "transcript")[0] == P.PERSON
    # A person's own goes on, as always.
    _transcribe(env, vid, SRT_PERSON, source="manual")
    assert _shown(env, vid)["srt"] == SRT_PERSON


@pytest.mark.slow
def test_removing_a_transcript_while_search_is_down_still_reaches_search(env, monkeypatch):
    import src.server.search.sync as sync

    lib, vid, sha = _video(env, "CorrTranscriptSearchDown")
    _transcribe(env, vid, SRT_MACHINE, sha)
    with _db(env) as s:
        s.execute(text("UPDATE assets SET search_synced_at = now() WHERE asset_id = :a"), {"a": vid})
        s.commit()
    monkeypatch.setattr(sync, "try_sync_asset", lambda *a, **k: None)  # Quickwit down
    _remove(env, vid, "machine")
    with _db(env) as s:
        assert s.execute(text(f"SELECT {sync.STALE_SEARCH} FROM active_assets a WHERE a.asset_id = :a"),
                         {"a": vid}).scalar() is True
