"""Text search on a capped public page searches what the page shows.

No whole-transcript field, and no scene or transcript hits from past the
public playback cap; signed in, everything is searched.
"""

from __future__ import annotations

import pytest

from src.server.models.query_filter import SearchTerm

pytestmark = pytest.mark.fast


class FakeQuickwit:
    enabled = True

    def __init__(self):
        self.asset_queries: list[str] = []

    def search_tenant(self, *, tenant_id, query, library_ids, max_hits):
        self.asset_queries.append(query)
        return []

    def search_tenant_scenes(self, *, tenant_id, query, library_ids, max_hits):
        return [
            {"asset_id": "ast_early_scene", "score": 2.0, "description": "a gate", "start_ms": 4_000, "end_ms": 6_000},
            {"asset_id": "ast_late_scene", "score": 3.0, "description": "the vault", "start_ms": 60_000, "end_ms": 62_000},
            {"asset_id": "ast_no_time", "score": 1.0, "description": "untimed"},
        ]

    def search_tenant_transcripts(self, *, tenant_id, query, library_ids, max_hits):
        return [
            {"asset_id": "ast_early_words", "score": 2.0, "text": "hello", "start_ms": 1_000, "end_ms": 2_000},
            {"asset_id": "ast_late_words", "score": 5.0, "text": "the password", "start_ms": 90_000, "end_ms": 92_000},
        ]


@pytest.fixture
def quickwit(monkeypatch):
    fake = FakeQuickwit()
    monkeypatch.setattr("src.server.search.quickwit_client.QuickwitClient", lambda: fake)
    return fake


def _search(cap_ms):
    from src.server.api.routers.query import _run_quickwit_search

    return _run_quickwit_search("ten_1", [SearchTerm(q="word")], ["lib_1"], limit=50, public_cap_ms=cap_ms)


def test_capped_public_search_drops_hits_past_the_cap(quickwit):
    scores, contexts, _ = _search(10_000)
    assert set(scores) == {"ast_early_scene", "ast_early_words"}
    assert all(c.start_ms is not None and c.start_ms < 10_000 for c in contexts.values())


def test_capped_public_search_leaves_the_whole_transcript_out(quickwit):
    _search(10_000)
    assert quickwit.asset_queries and all("transcript_text" not in q for q in quickwit.asset_queries)


def test_signed_in_search_has_everything(quickwit):
    scores, _, _ = _search(None)
    assert set(scores) == {"ast_early_scene", "ast_late_scene", "ast_no_time", "ast_early_words", "ast_late_words"}
    assert any("transcript_text" in q for q in quickwit.asset_queries)
