# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Two transcript saves for one clip at once: search ends up with the latest
transcript's segments, or the clip is left stale for the sweep; never an
older transcript's segments marked current."""

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


class _Index:
    """Quickwit's transcript index as the save sees it: it keeps what it's
    given (no upsert) and deletes by clip. on_delete runs once, after the
    first delete. The asset index isn't kept (False / None for the rest)."""

    enabled = True

    def __init__(self) -> None:
        self.docs: list[dict] = []
        self.on_delete = None

    def __getattr__(self, name: str):
        return lambda *a, **k: False

    def delete_tenant_transcript_documents(self, tenant_id: str, asset_id: str) -> None:
        self.docs = [d for d in self.docs if d["asset_id"] != asset_id]
        hook, self.on_delete = self.on_delete, None
        if hook:
            hook()

    def ingest_tenant_transcript_documents(self, tenant_id: str, docs: list[dict]) -> None:
        self.docs.extend(docs)


@pytest.mark.slow
def test_overlapping_transcript_saves_never_leave_the_older_one_current(env):
    client, headers, *_ = env
    lib = _library(env, "TranscriptRace")
    clip = _ingest_with(lib, "t.mov", _sha(), None, media_type="video")
    index = _Index()
    newer_done = threading.Event()

    def save(words: str) -> None:
        r = client.post(f"/v1/assets/{clip}/transcript", json={"srt": _srt(words), "source": "manual"},
                        headers=headers)
        assert r.status_code == 200, r.text

    def newer() -> None:
        save("the newer words")
        newer_done.set()

    def the_newer_save_runs_now() -> None:
        # Saved, then indexed whole while the older one's indexing is under
        # way (unless that holds the clip: then it waits for it).
        threading.Thread(target=newer).start()
        newer_done.wait(timeout=3)

    index.on_delete = the_newer_save_runs_now
    with patch("src.server.search.quickwit_client.QuickwitClient", return_value=index), \
         patch("src.server.search.sync.QuickwitClient", return_value=index):
        save("the older words")
        assert newer_done.wait(timeout=30)

    texts = {d["text"] for d in index.docs}
    with _db(env) as s:
        srt, current = s.execute(text("SELECT transcript_srt, transcript_synced_at >= transcribed_at"
                                      " FROM assets WHERE asset_id = :a"), {"a": clip}).one()
    assert "the newer words" in srt
    assert texts == {"the newer words"} or not current, \
        f"search has {texts} for the clip, and it's marked current"
