"""CLI tests for search command and enqueue --library requirement."""

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from src.cli.main import app

runner = CliRunner()


@pytest.mark.fast
def test_search_invalid_output_exits_1() -> None:
    """--output other than table/json/text prints error and exits 1."""
    mock_client = MagicMock()
    mock_client.get.return_value.json.return_value = [{"library_id": "lib_1", "name": "Lib", "root_path": "/path"}]

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["search", "-l", "Lib", "query", "--output", "xml"])

    assert result.exit_code == 1
    assert "table, json, text" in result.output
    mock_client.get.assert_not_called()


@pytest.mark.fast
def test_search_calls_api_with_library_id_and_query() -> None:
    """Resolve library by name, then GET /v1/search with library_id, q, limit, offset."""
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        MagicMock(json=lambda: [{"library_id": "lib_abc", "name": "MyLib", "root_path": "/x"}]),
        MagicMock(
            status_code=200,
            json=lambda: {
                "query": "sunset",
                "hits": [
                    {
                        "asset_id": "ast_1",
                        "rel_path": "photos/sunset.jpg",
                        "thumbnail_key": None,
                        "proxy_key": None,
                        "description": "A sunset",
                        "tags": ["outdoor"],
                        "score": 0.9,
                        "source": "quickwit",
                    }
                ],
                "total": 1,
                "source": "quickwit",
            },
        ),
    ]

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["search", "--library", "MyLib", "sunset", "--limit", "10"])

    assert result.exit_code == 0
    assert mock_client.get.call_count == 2
    assert mock_client.get.call_args_list[0][0][0] == "/v1/libraries"
    call_args = mock_client.get.call_args_list[1]
    assert call_args[0][0] == "/v1/search"
    assert call_args[1]["params"]["library_id"] == "lib_abc"
    assert call_args[1]["params"]["q"] == "sunset"
    assert call_args[1]["params"]["limit"] == 10
    assert "sunset.jpg" in result.output
    # Quickwit source: Score column is omitted, so no score in table
    assert "quickwit" in result.output


@pytest.mark.fast
def test_search_no_results_exit_0() -> None:
    """Empty hits: print 'No results.', exit 0."""
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        MagicMock(json=lambda: [{"library_id": "lib_1", "name": "EmptyLib", "root_path": "/path"}]),
        MagicMock(status_code=200, json=lambda: {"query": "xyz", "hits": [], "total": 0, "source": "postgres"}),
    ]

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["search", "-l", "EmptyLib", "xyz"])

    assert result.exit_code == 0
    assert "No results." in result.output


@pytest.mark.fast
def test_search_json_output() -> None:
    """--output json prints JSON array of hits."""
    hit = {
        "asset_id": "ast_1",
        "rel_path": "a/b.jpg",
        "thumbnail_key": None,
        "proxy_key": None,
        "description": "Desc",
        "tags": ["t1"],
        "score": 0.5,
        "source": "postgres",
    }
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        MagicMock(json=lambda: [{"library_id": "lib_1", "name": "J", "root_path": "/"}]),
        MagicMock(status_code=200, json=lambda: {"query": "q", "hits": [hit], "total": 1, "source": "postgres"}),
    ]

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["search", "-l", "J", "q", "-o", "json"])

    assert result.exit_code == 0
    assert "rel_path" in result.output
    assert "a/b.jpg" in result.output
    assert "Desc" in result.output


@pytest.mark.fast
def test_enqueue_requires_library_option() -> None:
    """enqueue without --library/-l fails with missing option."""
    result = runner.invoke(app, ["enqueue", "--job-type", "proxy"])
    assert result.exit_code != 0
    assert "library" in result.output.lower() or "Missing" in result.output


@pytest.mark.fast
def test_enqueue_embed_calls_api_with_job_type_embed() -> None:
    """enqueue --job-type embed calls POST /v1/jobs/enqueue with job_type embed."""
    mock_client = MagicMock()
    mock_client.get.return_value.json.return_value = [
        {"library_id": "lib_1", "name": "Lib", "root_path": "/path"}
    ]
    mock_client.post.return_value.json.return_value = {"enqueued": 5}

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["enqueue", "-l", "Lib", "--job-type", "embed"])

    assert result.exit_code == 0
    mock_client.post.assert_called_once()
    call_kw = mock_client.post.call_args[1]
    assert call_kw["json"]["job_type"] == "embed"
