"""CLI tests for the search command (GET /v1/query with f=query: filters)."""

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from src.client.cli.main import app

runner = CliRunner()

ASSET_HIT = {
    "asset_id": "ast_1",
    "rel_path": "photos/sunset.jpg",
    "media_type": "image",
    "camera_model": "X100V",
    "search_context": {"score": 0.9, "hit_type": "asset", "snippet": "A sunset over water"},
}
SCENE_HIT = {
    "asset_id": "ast_2",
    "rel_path": "clips/beach.mov",
    "media_type": "video",
    "search_context": {
        "score": 0.8, "hit_type": "scene", "snippet": "Waves", "start_ms": 12000, "end_ms": 18000,
    },
}


def _libraries() -> MagicMock:
    return MagicMock(json=lambda: [{"library_id": "lib_abc", "name": "MyLib", "root_path": "/x"}])


def _page(items: list[dict], next_cursor: str | None = None, source: str = "quickwit") -> MagicMock:
    return MagicMock(
        status_code=200,
        json=lambda: {"items": items, "next_cursor": next_cursor, "search_source": source},
    )


def _run(client: MagicMock, *args: str):
    with patch("src.client.cli.main.LumiverbClient", return_value=client):
        return runner.invoke(app, ["search", "-l", "MyLib", "--query", "sunset", *args])


def _query_calls(client: MagicMock) -> list:
    return [c for c in client.get.call_args_list if c[0][0] == "/v1/query"]


@pytest.mark.fast
def test_search_invalid_output_exits_1() -> None:
    client = MagicMock()
    result = _run(client, "--output", "xml")

    assert result.exit_code == 1
    assert "table, json, text" in result.output
    client.get.assert_not_called()


@pytest.mark.fast
def test_search_calls_query_with_library_and_text_filters() -> None:
    client = MagicMock()
    client.get.side_effect = [_libraries(), _page([ASSET_HIT])]

    result = _run(client, "--limit", "10")

    assert result.exit_code == 0, result.output
    [call] = _query_calls(client)
    assert call[1]["params"] == {"f": ["library:lib_abc", "query:sunset"], "limit": 10}
    assert "photos/sunset.jpg" in result.output
    assert "A sunset over water" in result.output
    assert "quickwit" in result.output


@pytest.mark.fast
@pytest.mark.parametrize(("media", "extra"), [("video", ["media:video"]), ("all", [])])
def test_search_media_type_filter(media: str, extra: list[str]) -> None:
    client = MagicMock()
    client.get.side_effect = [_libraries(), _page([ASSET_HIT])]

    _run(client, "--media-type", media)

    [call] = _query_calls(client)
    assert call[1]["params"]["f"] == ["library:lib_abc", "query:sunset", *extra]


@pytest.mark.fast
def test_search_limit_zero_follows_cursor() -> None:
    client = MagicMock()
    client.get.side_effect = [_libraries(), _page([ASSET_HIT], next_cursor="c1"), _page([SCENE_HIT])]

    result = _run(client, "--limit", "0", "-o", "text")

    assert result.exit_code == 0, result.output
    first, second = _query_calls(client)
    assert "after" not in first[1]["params"]
    assert second[1]["params"]["after"] == "c1"
    assert "photos/sunset.jpg" in result.output
    assert "clips/beach.mov" in result.output


@pytest.mark.fast
def test_search_limit_spans_pages_and_stops_at_limit() -> None:
    client = MagicMock()
    client.get.side_effect = [
        _libraries(),
        _page([ASSET_HIT, SCENE_HIT], next_cursor="c1"),
        _page([dict(ASSET_HIT, rel_path="photos/third.jpg")], next_cursor="c2"),
    ]

    result = _run(client, "--limit", "3", "-o", "text")

    first, second = _query_calls(client)
    assert first[1]["params"]["limit"] == 3
    assert second[1]["params"]["limit"] == 1
    assert len(_query_calls(client)) == 2
    assert "photos/third.jpg" in result.output


@pytest.mark.fast
def test_search_table_shows_scene_time_range() -> None:
    client = MagicMock()
    client.get.side_effect = [_libraries(), _page([SCENE_HIT])]

    result = _run(client)

    assert "scene" in result.output
    assert "12s – 18s" in result.output


@pytest.mark.fast
def test_search_no_results_exit_0() -> None:
    client = MagicMock()
    client.get.side_effect = [_libraries(), _page([], source="postgres_fallback")]

    result = _run(client)

    assert result.exit_code == 0
    assert "No results." in result.output


@pytest.mark.fast
def test_search_json_output() -> None:
    client = MagicMock()
    client.get.side_effect = [_libraries(), _page([ASSET_HIT])]

    result = _run(client, "-o", "json")

    assert result.exit_code == 0
    assert '"rel_path": "photos/sunset.jpg"' in result.output
    assert "A sunset over water" in result.output
