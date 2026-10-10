# ruff: noqa: F811 — pytest fixtures are named as parameters
"""What someone types is taken literally in a LIKE: a path, a search or a
person's name with % or _ matches those characters, never everything."""

from __future__ import annotations

from unittest.mock import MagicMock

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


@pytest.mark.fast
@pytest.mark.parametrize(("typed", "bound"), [("%", r"%\%%"), ("_", r"%\_%"), ("a_b", "%a%")])
def test_the_postgres_search_takes_what_was_typed_literally(typed, bound):
    from src.server.search.postgres_search import search_assets

    session = MagicMock()
    session.execute.return_value.fetchall.return_value = []
    search_assets(session, None, typed, limit=10)
    sql, params = session.execute.call_args.args
    assert params["like_0"] == bound
    assert str(sql).count("ILIKE :like_0 ESCAPE '\\'") >= 7


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
