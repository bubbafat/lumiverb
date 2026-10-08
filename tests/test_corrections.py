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


def _remove(env, vid: str):
    client, headers, *_ = env
    assert client.delete(f"/v1/assets/{vid}/transcript", headers=headers).status_code == 204


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
