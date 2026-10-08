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
