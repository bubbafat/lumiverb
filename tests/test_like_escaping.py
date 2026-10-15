# ruff: noqa: F811 — pytest fixtures are named as parameters
"""What someone types is taken literally: a path or a person's name with %
or _ matches those characters in a LIKE, never everything; a text search
reads them as no word at all (src/server/search/query_builder.py)."""

from __future__ import annotations

import pytest

from src.shared.utils import escape_like
from tests.test_browse_api import (  # noqa: F401 — the shared fixture
    _headers,
    _ingest_asset,
    browse_env,
)


def _ids(client, api_key, *f: str) -> set[str]:
    r = client.get("/v1/query", params={"f": list(f)}, headers=_headers(api_key))
    assert r.status_code == 200, r.text
    return {i["asset_id"] for i in r.json()["items"]}


@pytest.mark.fast
def test_escape_like():
    assert escape_like(r"a%b_c\d") == r"a\%b\_c\\d"


@pytest.mark.slow
def test_a_path_is_taken_literally(browse_env):
    client, api_key, lib, _ = browse_env
    under = _ingest_asset(client, api_key, lib, "esc/a_b/x.jpg")
    other = _ingest_asset(client, api_key, lib, "esc/axb/y.jpg")
    assert _ids(client, api_key, f"library:{lib}", "path:%") == set()
    assert _ids(client, api_key, f"library:{lib}", "path:esc/a_b") == {under}
    assert _ids(client, api_key, f"library:{lib}", "path:esc/axb") == {other}


@pytest.mark.slow
def test_a_text_search_takes_what_was_typed_literally(browse_env):
    """% and _ are no wildcards in a search: alone they find nothing (never
    everything), and between words they only part them."""
    client, api_key, lib, _ = browse_env
    # Words of two letters: matched whole, never as a prefix.
    parted = _ingest_asset(client, api_key, lib, "lit/qu_zi/x.jpg")
    joined = _ingest_asset(client, api_key, lib, "lit/quxzi/y.jpg")
    r = client.post("/v1/upkeep/search-sync", headers=_headers(api_key))  # their paths' words, searchable
    assert r.status_code == 200 and r.json()["failed"] == 0, r.text
    assert _ids(client, api_key, f"library:{lib}", "query:lit") >= {parted, joined}  # both found by a word

    for typed in ("%", "_", "%%", "_%_"):
        assert _ids(client, api_key, f"library:{lib}", f"query:{typed}") == set(), typed
    assert _ids(client, api_key, f"library:{lib}", "query:qu_zi") == {parted}


@pytest.mark.slow
def test_a_people_search_is_taken_literally(browse_env):
    client, api_key, *_ = browse_env
    for name in ("Ann_Lee", "AnnXLee"):
        assert client.post("/v1/people", json={"display_name": name}, headers=_headers(api_key)).status_code == 201

    def names(q: str) -> set[str]:
        r = client.get("/v1/people", params={"q": q}, headers=_headers(api_key))
        assert r.status_code == 200, r.text
        return {p["display_name"] for p in r.json()["items"]}

    assert names("%") == set()
    assert names("ann_") == {"Ann_Lee"}
