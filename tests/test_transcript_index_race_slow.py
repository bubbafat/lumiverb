# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Two transcript saves for one clip at once: search ends up with the latest
transcript's segments, marked current (one indexing at a time per clip,
each reading the transcript once it holds the clip); never an older
transcript's segments marked current."""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest
from sqlalchemy import text

from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _sha
from tests.test_reconciler import _library


def _srt(words: str) -> str:
    return f"1\n00:00:01,000 --> 00:00:02,000\n{words}\n"


@pytest.mark.slow
def test_overlapping_transcript_saves_never_leave_the_older_one_current(env):
    from src.server.search import sync

    client, headers, *_ = env
    lib = _library(env, "TranscriptRace")
    clip = _ingest_with(lib, "t.mov", _sha(), None, media_type="video")
    newer_done = threading.Event()

    def save(words: str) -> None:
        r = client.post(f"/v1/assets/{clip}/transcript", json={"srt": _srt(words), "source": "manual"},
                        headers=headers)
        assert r.status_code == 200, r.text

    def newer() -> None:
        save("the newer words")
        newer_done.set()

    real = sync._put_segments
    fired: list[int] = []

    def put_segments(session, asset_ids, rows):
        real(session, asset_ids, rows)
        if not fired:
            # The older save's segments written, not yet committed or marked:
            # the newer save is saved, then indexed whole, while the older
            # one's indexing is under way (unless that holds the clip: then
            # it waits for it).
            fired.append(1)
            threading.Thread(target=newer).start()
            newer_done.wait(timeout=3)

    with patch.object(sync, "_put_segments", put_segments):
        save("the older words")
        assert newer_done.wait(timeout=30)

    with _db(env) as s:
        texts = set(s.execute(text("SELECT text FROM transcript_segments WHERE asset_id = :a"),
                              {"a": clip}).scalars())
        srt, current = s.execute(text("SELECT transcript_srt, transcript_synced_at >= transcribed_at"
                                      " FROM assets WHERE asset_id = :a"), {"a": clip}).one()
    assert fired and "the newer words" in srt
    assert texts == {"the newer words"} or not current, \
        f"search has {texts} for the clip, and it's marked current"
    # And the later save's segments win, at once: no sweep needed.
    assert texts == {"the newer words"} and current, f"search has {texts} for the clip (current: {current})"
