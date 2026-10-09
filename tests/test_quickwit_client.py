"""Unit tests for QuickwitClient tenant-scoped methods."""

from unittest.mock import MagicMock, patch

import pytest

from src.server.search.quickwit_client import QuickwitClient


def _make_client(enabled: bool = True) -> QuickwitClient:
    mock_settings = MagicMock()
    mock_settings.quickwit_enabled = enabled
    mock_settings.quickwit_url = "http://localhost:7280"
    with patch("src.server.search.quickwit_client.get_settings", return_value=mock_settings):
        return QuickwitClient()


# ---------------------------------------------------------------------------
# Index naming
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_tenant_index_id():
    qw = _make_client()
    assert qw.tenant_index_id("tnt_abc") == "lumiverb_tenant_tnt_abc"


@pytest.mark.fast
def test_tenant_scene_index_id():
    qw = _make_client()
    assert qw.tenant_scene_index_id("tnt_abc") == "lumiverb_tenant_tnt_abc_scenes"


# ---------------------------------------------------------------------------
# Library filter query building
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_apply_library_filter_none():
    """No library_ids → query unchanged."""
    assert QuickwitClient._apply_library_filter("sunset beach", None) == "sunset beach"


@pytest.mark.fast
def test_apply_library_filter_empty_list():
    assert QuickwitClient._apply_library_filter("sunset beach", []) == "sunset beach"


@pytest.mark.fast
def test_apply_library_filter_single():
    result = QuickwitClient._apply_library_filter("sunset beach", ["lib_01"])
    assert result == 'library_id:"lib_01" AND (sunset beach)'


@pytest.mark.fast
def test_apply_library_filter_multiple():
    result = QuickwitClient._apply_library_filter("sunset", ["lib_01", "lib_02", "lib_03"])
    assert result == '(library_id:"lib_01" OR library_id:"lib_02" OR library_id:"lib_03") AND (sunset)'


# ---------------------------------------------------------------------------
# Search (mocked HTTP)
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_search_tenant_no_library_filter():
    """search_tenant without library_ids queries with raw query."""
    qw = _make_client()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "hits": [
            {"_source": {"asset_id": "ast_1", "library_id": "lib_A", "rel_path": "a.jpg",
                         "description": "sunset", "tags": ["nature"]}}
        ]
    }
    with patch("src.server.search.quickwit_client.requests.post", return_value=mock_resp) as mock_post:
        results = qw.search_tenant("tnt_1", "sunset", max_hits=10)

    assert len(results) == 1
    assert results[0]["asset_id"] == "ast_1"
    assert results[0]["library_id"] == "lib_A"
    # Verify the query was not wrapped in a library filter
    call_body = mock_post.call_args[1]["json"]
    assert call_body["query"] == "sunset"


@pytest.mark.fast
def test_search_tenant_with_single_library():
    """search_tenant with one library_id filters the query."""
    qw = _make_client()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"hits": []}
    with patch("src.server.search.quickwit_client.requests.post", return_value=mock_resp) as mock_post:
        qw.search_tenant("tnt_1", "sunset", library_ids=["lib_A"])

    call_body = mock_post.call_args[1]["json"]
    assert call_body["query"] == 'library_id:"lib_A" AND (sunset)'


@pytest.mark.fast
def test_search_tenant_with_multiple_libraries():
    """search_tenant with multiple library_ids builds OR clause."""
    qw = _make_client()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"hits": []}
    with patch("src.server.search.quickwit_client.requests.post", return_value=mock_resp) as mock_post:
        qw.search_tenant("tnt_1", "sunset", library_ids=["lib_A", "lib_B"])

    call_body = mock_post.call_args[1]["json"]
    assert call_body["query"] == '(library_id:"lib_A" OR library_id:"lib_B") AND (sunset)'


@pytest.mark.fast
def test_search_tenant_scenes_with_library_filter():
    """search_tenant_scenes applies library filter."""
    qw = _make_client()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "hits": [
            {"_source": {"scene_id": "sc_1", "asset_id": "ast_1", "library_id": "lib_A",
                         "rel_path": "vid.mp4", "description": "beach", "tags": []}}
        ]
    }
    with patch("src.server.search.quickwit_client.requests.post", return_value=mock_resp) as mock_post:
        results = qw.search_tenant_scenes("tnt_1", "beach", library_ids=["lib_A"])

    assert len(results) == 1
    assert results[0]["scene_id"] == "sc_1"
    call_body = mock_post.call_args[1]["json"]
    assert 'library_id:"lib_A"' in call_body["query"]


@pytest.mark.fast
def test_search_tenant_disabled_returns_empty():
    qw = _make_client(enabled=False)
    results = qw.search_tenant("tnt_1", "sunset")
    assert results == []


# ---------------------------------------------------------------------------
# Ingest (mocked HTTP)
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_ingest_tenant_documents_batching():
    """1100 docs should produce 3 HTTP calls (500 + 500 + 100)."""
    qw = _make_client()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    with patch("src.server.search.quickwit_client.requests.post", return_value=mock_resp) as mock_post:
        docs = [{"id": f"doc_{i}"} for i in range(1100)]
        qw.ingest_tenant_documents("tnt_1", docs)

    assert mock_post.call_count == 3


@pytest.mark.fast
def test_ingest_tenant_documents_empty():
    """Empty doc list makes no HTTP calls."""
    qw = _make_client()
    with patch("src.server.search.quickwit_client.requests.post") as mock_post:
        qw.ingest_tenant_documents("tnt_1", [])

    mock_post.assert_not_called()


@pytest.mark.fast
def test_ingest_tenant_documents_disabled():
    qw = _make_client(enabled=False)
    with patch("src.server.search.quickwit_client.requests.post") as mock_post:
        qw.ingest_tenant_documents("tnt_1", [{"id": "doc_1"}])

    mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# Delete (mocked HTTP)
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_delete_by_asset_id_calls_all_indexes():
    """delete_tenant_documents_by_asset_id hits asset, scene, and transcript indexes."""
    qw = _make_client()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    with patch("src.server.search.quickwit_client.requests.post", return_value=mock_resp) as mock_post:
        qw.delete_tenant_documents_by_asset_id("tnt_1", "ast_123")

    assert mock_post.call_count == 3
    urls = [call.args[0] for call in mock_post.call_args_list]
    assert any("lumiverb_tenant_tnt_1/delete-tasks" in u for u in urls)
    assert any("lumiverb_tenant_tnt_1_scenes/delete-tasks" in u for u in urls)
    assert any("lumiverb_tenant_tnt_1_transcripts/delete-tasks" in u for u in urls)


@pytest.mark.fast
def test_delete_by_library_id_calls_all_indexes():
    """delete_tenant_documents_by_library_id hits all indexes with library_id query."""
    qw = _make_client()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    with patch("src.server.search.quickwit_client.requests.post", return_value=mock_resp) as mock_post:
        qw.delete_tenant_documents_by_library_id("tnt_1", "lib_old")

    assert mock_post.call_count == 3
    for call in mock_post.call_args_list:
        body = call[1]["json"]
        assert body["query"] == 'library_id:"lib_old"'


@pytest.mark.fast
def test_delete_disabled_no_calls():
    qw = _make_client(enabled=False)
    with patch("src.server.search.quickwit_client.requests.post") as mock_post:
        qw.delete_tenant_documents_by_asset_id("tnt_1", "ast_1")
        qw.delete_tenant_documents_by_library_id("tnt_1", "lib_1")

    mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# Ensure index (mocked HTTP)
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_ensure_tenant_index_creates_when_missing(tmp_path):
    """ensure_tenant_index creates the index when GET returns 404."""
    schema = tmp_path / "asset_index_schema.json"
    schema.write_text('{"doc_mapping": {}}')

    mock_settings = MagicMock()
    mock_settings.quickwit_enabled = True
    mock_settings.quickwit_url = "http://localhost:7280"
    with patch("src.server.search.quickwit_client.get_settings", return_value=mock_settings):
        qw = QuickwitClient(schema_dir=tmp_path)

    get_resp = MagicMock(status_code=404)
    post_resp = MagicMock(status_code=200)
    with patch("src.server.search.quickwit_client.requests.get", return_value=get_resp), \
         patch("src.server.search.quickwit_client.requests.post", return_value=post_resp) as mock_post:
        qw.ensure_tenant_index("tnt_1")

    # The create, then one empty ingest that finds the new index ready.
    assert mock_post.call_count == 2
    import json
    create, probe = mock_post.call_args_list
    assert create.args[0] == "http://localhost:7280/api/v1/indexes"
    assert json.loads(create.kwargs["data"])["index_id"] == "lumiverb_tenant_tnt_1"
    assert probe.args[0] == "http://localhost:7280/api/v1/lumiverb_tenant_tnt_1/ingest"


@pytest.mark.fast
def test_ensure_tenant_index_skips_when_exists(tmp_path):
    """ensure_tenant_index skips POST when GET returns 200."""
    mock_settings = MagicMock()
    mock_settings.quickwit_enabled = True
    mock_settings.quickwit_url = "http://localhost:7280"
    with patch("src.server.search.quickwit_client.get_settings", return_value=mock_settings):
        qw = QuickwitClient(schema_dir=tmp_path)

    get_resp = MagicMock(status_code=200)
    with patch("src.server.search.quickwit_client.requests.get", return_value=get_resp), \
         patch("src.server.search.quickwit_client.requests.post") as mock_post:
        qw.ensure_tenant_index("tnt_1")

    mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# A new index is ready before ensure returns (bounded wait)
# ---------------------------------------------------------------------------


class _FakeClock:
    """time.monotonic/time.sleep for the client: sleeping moves the clock, so a
    wait that never ends would show up as a test that never ends."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        assert s >= 0
        self.slept.append(s)
        self.now += s


def _new_index_client(tmp_path) -> QuickwitClient:
    (tmp_path / "asset_index_schema.json").write_text('{"doc_mapping": {}}')
    mock_settings = MagicMock()
    mock_settings.quickwit_enabled = True
    mock_settings.quickwit_url = "http://localhost:7280"
    with patch("src.server.search.quickwit_client.get_settings", return_value=mock_settings):
        return QuickwitClient(schema_dir=tmp_path)


def _create_then_ingest(ingest_statuses: list[int]):
    """requests.post: 200 for the index create, then `ingest_statuses` in turn
    for ingests into the new index (the last one repeats)."""
    calls: list[str] = []

    def post(url, **kwargs):
        calls.append(url)
        if url.endswith("/api/v1/indexes"):
            return MagicMock(status_code=200)
        assert "/lumiverb_tenant_tnt_1/ingest" in url, url
        n = sum(1 for c in calls if "/ingest" in c)
        return MagicMock(status_code=ingest_statuses[min(n, len(ingest_statuses)) - 1])

    return post, calls


@pytest.mark.fast
def test_new_index_is_waited_on_until_it_takes_an_ingest(tmp_path):
    """Quickwit can answer an ingest with 404 just after it makes an index; the
    client keeps asking until the index takes one, so the first sync isn't lost."""
    qw = _new_index_client(tmp_path)
    clock = _FakeClock()
    post, calls = _create_then_ingest([404, 404, 200])
    with patch("src.server.search.quickwit_client.requests.get", return_value=MagicMock(status_code=404)), \
         patch("src.server.search.quickwit_client.requests.post", side_effect=post), \
         patch("src.server.search.quickwit_client.time", clock):
        qw.ensure_tenant_index("tnt_1")

    ingests = [c for c in calls if "/ingest" in c]
    assert len(ingests) == 3
    assert "commit" not in ingests[0]  # an empty ingest adds nothing and forces no commit
    assert len(clock.slept) == 2


@pytest.mark.fast
def test_new_index_that_never_gets_ready_gives_up_within_the_bound(tmp_path, caplog):
    """Never ready: ensure returns (no exception) once the bound is spent, with a
    warning; the sweep picks up whatever the first sync couldn't send."""
    from src.server.search import quickwit_client

    qw = _new_index_client(tmp_path)
    clock = _FakeClock()
    start = clock.now
    post, calls = _create_then_ingest([404])
    with patch("src.server.search.quickwit_client.requests.get", return_value=MagicMock(status_code=404)), \
         patch("src.server.search.quickwit_client.requests.post", side_effect=post), \
         patch("src.server.search.quickwit_client.time", clock), \
         caplog.at_level("WARNING", logger="src.server.search.quickwit_client"):
        qw.ensure_tenant_index("tnt_1")

    assert clock.now - start <= quickwit_client.INDEX_READY_TIMEOUT_S
    assert clock.now - start >= quickwit_client.INDEX_READY_TIMEOUT_S - 0.01  # it did try for the whole bound
    assert 2 < len([c for c in calls if "/ingest" in c]) < 50
    assert "lumiverb_tenant_tnt_1" in caplog.text and "not ready" in caplog.text


@pytest.mark.fast
def test_new_index_wait_survives_connection_errors(tmp_path):
    """A probe that can't connect counts as not ready yet, not as a failure."""
    import requests as real_requests

    qw = _new_index_client(tmp_path)
    clock = _FakeClock()
    post, calls = _create_then_ingest([200])
    errors = iter([real_requests.ConnectionError("refused")])

    def flaky_post(url, **kwargs):
        if "/ingest" in url:
            err = next(errors, None)
            if err:
                calls.append(url)
                raise err
        return post(url, **kwargs)

    with patch("src.server.search.quickwit_client.requests.get", return_value=MagicMock(status_code=404)), \
         patch("src.server.search.quickwit_client.requests.post", side_effect=flaky_post), \
         patch("src.server.search.quickwit_client.time", clock):
        qw.ensure_tenant_index("tnt_1")

    assert len([c for c in calls if "/ingest" in c]) == 2
